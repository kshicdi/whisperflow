"""
hue_controller.py — Philips Hue Iris controller for JARVIS voice assistant states.

Controls light ID 26 via the Hue Bridge REST API to reflect JARVIS states
with Iron Man-themed colors and animations.

Config file: ~/.config/whisperflow/hue_config.json
"""

import json
import logging
import os
import ssl
import threading
import time
import urllib.request
from pathlib import Path

logger = logging.getLogger(__name__)

DEFAULT_CONFIG = {
    "bridge_ip": "",
    "api_key": "",
    "light_id": 26,
    "enabled": False,
}

CONFIG_PATH = Path.home() / ".config" / "whisperflow" / "hue_config.json"


class HueController:
    """Controls a Philips Hue light to reflect JARVIS assistant states."""

    def __init__(self):
        self._config = self._load_config()
        self._stop_event = threading.Event()
        self._animation_thread: threading.Thread | None = None
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Config
    # ------------------------------------------------------------------

    def _load_config(self) -> dict:
        """Load config from disk, creating it with defaults if missing."""
        if CONFIG_PATH.exists():
            try:
                with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                    config = json.load(f)
                # Merge with defaults so any newly added keys are present
                merged = {**DEFAULT_CONFIG, **config}
                return merged
            except Exception as exc:
                logger.warning("Failed to read hue config (%s), using defaults.", exc)
        else:
            CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
            try:
                with open(CONFIG_PATH, "w", encoding="utf-8") as f:
                    json.dump(DEFAULT_CONFIG, f, indent=2)
                logger.info("Created default hue config at %s", CONFIG_PATH)
            except Exception as exc:
                logger.warning("Could not write default hue config: %s", exc)
        return dict(DEFAULT_CONFIG)

    @property
    def _enabled(self) -> bool:
        return bool(self._config.get("enabled", True))

    @property
    def _bridge_ip(self) -> str:
        return self._config["bridge_ip"]

    @property
    def _api_key(self) -> str:
        return self._config["api_key"]

    @property
    def _light_id(self) -> int:
        return int(self._config["light_id"])

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def set_state(self, state: str) -> None:
        """Change the light to reflect the given JARVIS state.

        States:
            idle          — dim blue arc-reactor standby
            recording     — blue breathing pulse (listening)
            listening     — alias for recording
            processing    — orange/yellow fast pulse (computing)
            speaking      — bright cyan (JARVIS speaking)
            tts_playing   — alias for speaking
            error         — red blink alert
        """
        if not self._enabled:
            return

        # Normalise aliases
        state = state.lower().strip()
        if state == "listening":
            state = "recording"
        if state == "tts_playing":
            state = "speaking"

        self._stop_animation()

        if state == "idle":
            self._set_idle()
        elif state == "recording":
            self._start_animation(state)
        elif state == "processing":
            self._start_animation(state)
        elif state == "speaking":
            self._set_speaking()
        elif state == "error":
            self._start_animation(state)
        else:
            logger.warning("HueController: unknown state '%s', ignoring.", state)

    def stop(self) -> None:
        """Stop all animations and restore idle state."""
        self._stop_animation()
        if self._enabled:
            self._set_idle()

    # ------------------------------------------------------------------
    # Static state helpers
    # ------------------------------------------------------------------

    def _set_idle(self) -> None:
        """Arc reactor standby — dim blue."""
        self._send_light_command({
            "on": True,
            "bri": 50,
            "hue": 46920,
            "sat": 254,
            "transitiontime": 10,  # 1 second
        })

    def _set_speaking(self) -> None:
        """JARVIS speaking — bright cyan."""
        self._send_light_command({
            "on": True,
            "bri": 254,
            "hue": 36000,
            "sat": 200,
            "transitiontime": 4,  # 0.4 seconds
        })

    # ------------------------------------------------------------------
    # Animation helpers
    # ------------------------------------------------------------------

    def _start_animation(self, state: str) -> None:
        """Start an animation thread for the given state."""
        self._stop_event.clear()
        thread = threading.Thread(
            target=self._animate,
            args=(state,),
            daemon=True,
            name=f"hue-{state}",
        )
        with self._lock:
            self._animation_thread = thread
        thread.start()

    def _stop_animation(self) -> None:
        """Signal the running animation thread to stop and wait for it."""
        self._stop_event.set()
        with self._lock:
            thread = self._animation_thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=2.0)
        with self._lock:
            self._animation_thread = None

    def _animate(self, state: str) -> None:
        """Run the animation loop for the given state in a daemon thread."""
        try:
            if state == "recording":
                self._animate_recording()
            elif state == "processing":
                self._animate_processing()
            elif state == "error":
                self._animate_error()
        except Exception as exc:
            logger.error("HueController animation error (%s): %s", state, exc)

    def _animate_recording(self) -> None:
        """Red breathing pulse — oscillate bri between 30 and 200, period ~2 s."""
        period = 2.0  # seconds for one full breath
        low_bri = 30
        high_bri = 200
        transition = max(1, int((period / 2) * 10))  # in 100 ms units

        going_up = True
        while not self._stop_event.is_set():
            target_bri = high_bri if going_up else low_bri
            self._send_light_command({
                "on": True,
                "bri": target_bri,
                "hue": 0,
                "sat": 254,
                "transitiontime": transition,
            })
            going_up = not going_up
            self._stop_event.wait(period / 2)

    def _animate_processing(self) -> None:
        """Orange/yellow fast pulse — oscillate bri between 100 and 254, period ~0.5 s."""
        period = 0.5
        transition = max(1, int((period / 2) * 10))

        going_up = True
        while not self._stop_event.is_set():
            target_bri = 254 if going_up else 100
            self._send_light_command({
                "on": True,
                "bri": target_bri,
                "hue": 8000,
                "sat": 254,
                "transitiontime": transition,
            })
            going_up = not going_up
            self._stop_event.wait(period / 2)

    def _animate_error(self) -> None:
        """Red blink — toggle on/off quickly."""
        blink_interval = 0.3  # seconds between on/off
        on = True
        while not self._stop_event.is_set():
            if on:
                self._send_light_command({
                    "on": True,
                    "bri": 254,
                    "hue": 0,
                    "sat": 254,
                    "transitiontime": 0,
                })
            else:
                self._send_light_command({"on": False, "transitiontime": 0})
            on = not on
            self._stop_event.wait(blink_interval)

    # ------------------------------------------------------------------
    # HTTP helper
    # ------------------------------------------------------------------

    def _send_light_command(self, payload: dict) -> None:
        """PUT payload to /api/{key}/lights/{id}/state on the Hue Bridge."""
        url = (
            f"https://{self._bridge_ip}/api/{self._api_key}"
            f"/lights/{self._light_id}/state"
        )
        data = json.dumps(payload).encode("utf-8")
        ctx = ssl._create_unverified_context()
        req = urllib.request.Request(
            url,
            data=data,
            method="PUT",
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, context=ctx, timeout=3) as resp:
                body = resp.read()
                logger.debug("Hue response: %s", body)
        except Exception as exc:
            logger.warning("HueController: failed to send command to bridge: %s", exc)
