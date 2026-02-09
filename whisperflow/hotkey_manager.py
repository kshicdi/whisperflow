"""전역 단축키 관리 모듈 - Command+Control+Option+Shift 지원"""

import threading
import time
from typing import Callable, Optional, Set
from pynput import keyboard


class HotkeyManager:
    """Command+Control+Option+Shift 기반 단축키 관리 클래스

    - 꾹 누르기: 누르는 동안 녹음, 떼면 중지
    - 더블 클릭: 토글 모드 (계속 녹음, 다시 누르면 중지)
    """

    DOUBLE_CLICK_THRESHOLD = 0.5  # 더블 클릭 판정 시간 (초)
    HOLD_THRESHOLD = 0.2  # 꾹 누르기 판정 시간 (초)

    # 키 매핑 (modifier)
    KEY_MAP = {
        "cmd": keyboard.Key.cmd,
        "ctrl": keyboard.Key.ctrl,
        "option": keyboard.Key.alt,
        "shift": keyboard.Key.shift,
    }

    # 일반 키
    CHAR_KEY = "a"  # 추가 키

    def __init__(self,
                 on_hold_start: Optional[Callable] = None,
                 on_hold_end: Optional[Callable] = None,
                 on_toggle: Optional[Callable] = None,
                 on_hotkey: Optional[Callable] = None):

        self._listener: Optional[keyboard.Listener] = None
        self._lock = threading.Lock()

        # 콜백
        self.on_hold_start = on_hold_start  # 꾹 누르기 시작
        self.on_hold_end = on_hold_end      # 꾹 누르기 끝
        self.on_toggle = on_toggle          # 더블 클릭 토글
        self.on_hotkey = on_hotkey          # 기존 호환용

        # 단축키 조합 (기본값)
        from .config import config
        self._load_modifiers_from_config(config.hotkey)

        # 현재 눌린 키
        self._pressed_keys: Set = set()

        # 상태
        self._hotkey_press_time = 0
        self._last_hotkey_release_time = 0
        self._is_holding = False
        self._toggle_mode = False
        self._hold_timer: Optional[threading.Timer] = None
        self._hotkey_active = False

    def _load_modifiers_from_config(self, hotkey_str: str) -> None:
        """설정에서 modifier 로드"""
        keys = hotkey_str.lower().replace(" ", "").split("+")
        self.HOTKEY_MODIFIERS = set()
        for key in keys:
            if key in self.KEY_MAP:
                self.HOTKEY_MODIFIERS.add(self.KEY_MAP[key])

    def update_modifiers(self, modifiers: list) -> None:
        """단축키 modifier 업데이트"""
        self.HOTKEY_MODIFIERS = set()
        for key in modifiers:
            if key in self.KEY_MAP:
                self.HOTKEY_MODIFIERS.add(self.KEY_MAP[key])
        # 상태 초기화
        self._pressed_keys.clear()
        self._hotkey_active = False
        self._is_holding = False
        self._toggle_mode = False
        print(f"[단축키] 업데이트: {modifiers}")

    def _normalize_key(self, key):
        """키를 정규화 (좌/우 구분 없이)"""
        if key in (keyboard.Key.cmd, keyboard.Key.cmd_l, keyboard.Key.cmd_r):
            return keyboard.Key.cmd
        elif key in (keyboard.Key.ctrl, keyboard.Key.ctrl_l, keyboard.Key.ctrl_r):
            return keyboard.Key.ctrl
        elif key in (keyboard.Key.alt, keyboard.Key.alt_l, keyboard.Key.alt_r):
            return keyboard.Key.alt
        elif key in (keyboard.Key.shift, keyboard.Key.shift_l, keyboard.Key.shift_r):
            return keyboard.Key.shift
        # 일반 키 (a, b, c 등)
        elif hasattr(key, 'char') and key.char:
            return key.char.lower()
        return None

    def _is_hotkey_pressed(self) -> bool:
        """단축키 조합이 눌렸는지 확인"""
        modifiers_ok = self.HOTKEY_MODIFIERS.issubset(self._pressed_keys)
        char_ok = self.CHAR_KEY in self._pressed_keys if self.CHAR_KEY else True
        return modifiers_ok and char_ok

    def _on_press(self, key) -> None:
        """키 누름 이벤트"""
        normalized = self._normalize_key(key)
        if normalized is None:
            return

        with self._lock:
            self._pressed_keys.add(normalized)

            # 단축키 조합이 처음 완성됨
            if self._is_hotkey_pressed() and not self._hotkey_active:
                self._hotkey_active = True
                now = time.time()
                self._hotkey_press_time = now

                # 토글 모드 중이면 무시 (release에서 처리)
                if self._toggle_mode:
                    return

                # 홀드 타이머 시작
                if self._hold_timer:
                    self._hold_timer.cancel()

                self._hold_timer = threading.Timer(
                    self.HOLD_THRESHOLD,
                    self._start_hold_recording
                )
                self._hold_timer.start()

    def _start_hold_recording(self):
        """홀드 녹음 시작"""
        with self._lock:
            if not self._toggle_mode and self._hotkey_active:
                self._is_holding = True
                print("[단축키] 꾹 누르기 - 녹음 시작")
                if self.on_hold_start:
                    threading.Thread(target=self.on_hold_start, daemon=True).start()

    def _on_release(self, key) -> None:
        """키 뗌 이벤트"""
        normalized = self._normalize_key(key)
        if normalized is None:
            return

        with self._lock:
            was_hotkey_active = self._hotkey_active

            self._pressed_keys.discard(normalized)

            # 단축키 조합이 해제됨
            if was_hotkey_active and not self._is_hotkey_pressed():
                self._hotkey_active = False
                now = time.time()
                press_duration = now - self._hotkey_press_time
                time_since_last_release = now - self._last_hotkey_release_time

                # 홀드 타이머 취소
                if self._hold_timer:
                    self._hold_timer.cancel()
                    self._hold_timer = None

                # 홀드 모드였으면 녹음 중지
                if self._is_holding:
                    self._is_holding = False
                    print("[단축키] 꾹 누르기 끝 - 녹음 중지")
                    if self.on_hold_end:
                        threading.Thread(target=self.on_hold_end, daemon=True).start()
                    self._last_hotkey_release_time = now
                    return

                # 토글 모드 중이면 녹음 중지
                if self._toggle_mode:
                    self._toggle_mode = False
                    print("[단축키] 토글 모드 종료 - 녹음 중지")
                    if self.on_hold_end:
                        threading.Thread(target=self.on_hold_end, daemon=True).start()
                    self._last_hotkey_release_time = now
                    return

                # 짧게 눌렀으면 (홀드 아님)
                if press_duration < self.HOLD_THRESHOLD + 0.1:
                    # 더블 클릭 확인
                    if time_since_last_release < self.DOUBLE_CLICK_THRESHOLD:
                        # 더블 클릭! 토글 모드 시작
                        self._toggle_mode = True
                        print("[단축키] 더블 클릭 - 토글 녹음 시작")
                        if self.on_hold_start:
                            threading.Thread(target=self.on_hold_start, daemon=True).start()

                self._last_hotkey_release_time = now

    def start(self) -> None:
        """단축키 리스닝 시작"""
        if self._listener is not None:
            return

        self._listener = keyboard.Listener(
            on_press=self._on_press,
            on_release=self._on_release
        )
        self._listener.start()
        # 현재 설정된 키 표시
        key_names = []
        for key in self.HOTKEY_MODIFIERS:
            if key == keyboard.Key.cmd:
                key_names.append("Cmd")
            elif key == keyboard.Key.ctrl:
                key_names.append("Ctrl")
            elif key == keyboard.Key.alt:
                key_names.append("Option")
            elif key == keyboard.Key.shift:
                key_names.append("Shift")
        if self.CHAR_KEY:
            key_names.append(self.CHAR_KEY.upper())
        print(f"[단축키] {'+'.join(key_names)} 리스닝 시작")
        print("  - 꾹 누르기: 누르는 동안 녹음")
        print("  - 더블 클릭: 토글 모드 (다시 누르면 중지)")

    def stop(self) -> None:
        """단축키 리스닝 중지"""
        if self._hold_timer:
            self._hold_timer.cancel()
        if self._listener:
            self._listener.stop()
            self._listener = None
