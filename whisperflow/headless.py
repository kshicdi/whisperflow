"""
WhisperFlow Headless Server

Runs the JARVIS WebSocket + HTTP static server without any GUI,
macOS menu bar, or audio dependencies.

Usage:
    python -m whisperflow.headless

Environment variables:
    WS_HOST   Bind address (default: 127.0.0.1)
    WS_PORT   Port (default: 8767)
"""

import asyncio
import logging
import os
import signal
import sys

# ── Logging setup ────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("whisperflow.headless")


async def _main():
    # Import here (not at module level) so that ws_server.py reads the env vars
    # after the caller has had a chance to set WS_HOST / WS_PORT.
    from whisperflow.ws_server import WhisperFlowWSServer, WS_HOST, WS_PORT

    server = WhisperFlowWSServer()

    # Register headless TTS handler (Qwen TTS → WebSocket tts_audio)
    from whisperflow.headless_tts import HeadlessTTSHandler, warmup
    tts_handler = HeadlessTTSHandler(server)
    server._on_chat_tts = tts_handler.handle
    logger.info("HeadlessTTSHandler registered on ws_server._on_chat_tts")
    warmup()  # Qwen 모델 프리로드 (백그라운드, 실패해도 무해)

    # Register a stop event for clean shutdown
    loop = asyncio.get_running_loop()
    stop = asyncio.Event()

    def _handle_signal(signum, frame):
        logger.info("Signal %s received — shutting down…", signum)
        loop.call_soon_threadsafe(stop.set)

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    logger.info("Starting WhisperFlow headless server on %s:%d", WS_HOST, WS_PORT)

    # Start ws_server in its own background thread (as designed)
    server.start()

    logger.info("Server ready — http://%s:%d/", WS_HOST, WS_PORT)

    # Block until a SIGTERM/SIGINT arrives
    await stop.wait()

    logger.info("Stopping server…")
    server.stop()
    logger.info("Done.")


def main():
    try:
        asyncio.run(_main())
    except KeyboardInterrupt:
        pass
    sys.exit(0)


if __name__ == "__main__":
    main()
