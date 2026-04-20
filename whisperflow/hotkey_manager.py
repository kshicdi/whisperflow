"""전역 단축키 관리 모듈 - Command+Control+Option+Shift 지원"""

import threading
import time
from typing import Callable, Optional, Set
from pynput import keyboard
from pynput.keyboard import KeyCode


# macOS virtual key code → 문자 매핑
VK_TO_CHAR = {
    0: "a", 1: "s", 2: "d", 3: "f", 4: "h",
    5: "g", 6: "z", 7: "x", 8: "c", 9: "v",
    11: "b", 12: "q", 13: "w", 14: "e", 15: "r",
    16: "y", 17: "t", 18: "1", 19: "2", 20: "3",
    21: "4", 22: "6", 23: "5", 24: "=", 25: "9",
    26: "7", 27: "-", 28: "8", 29: "0", 30: "]",
    31: "o", 32: "u", 33: "[", 34: "i", 35: "p",
    37: "l", 38: "j", 39: "'", 40: "k", 41: ";",
    42: "\\", 43: ",", 44: "/", 45: "n", 46: "m",
    47: ".", 50: "`",
}


class HotkeyManager:
    """Command+Control+Option+Shift 기반 단축키 관리 클래스

    - 짧게 탭: 토글 모드 (탭하면 녹음 시작, 다시 탭하면 중지)
    - 꾹 누르기: 누르는 동안 녹음, 떼면 중지
    """

    HOLD_THRESHOLD = 0.3  # 꾹 누르기 판정 시간 (초)

    # 키 매핑 (modifier)
    KEY_MAP = {
        "cmd": keyboard.Key.cmd,
        "ctrl": keyboard.Key.ctrl,
        "option": keyboard.Key.alt,
        "shift": keyboard.Key.shift,
        "space": keyboard.Key.space,
    }

    # stale key 정리 임계값 (초)
    STALE_KEY_TIMEOUT = 2.0

    def __init__(self,
                 on_hold_start: Optional[Callable] = None,
                 on_hold_end: Optional[Callable] = None,
                 on_toggle: Optional[Callable] = None,
                 on_hotkey: Optional[Callable] = None,
                 on_tts_trigger: Optional[Callable] = None):

        self._listener: Optional[keyboard.Listener] = None
        self._lock = threading.Lock()

        # 콜백
        self.on_hold_start = on_hold_start  # 꾹 누르기 시작
        self.on_hold_end = on_hold_end      # 꾹 누르기 끝
        self.on_toggle = on_toggle          # 더블 클릭 토글
        self.on_hotkey = on_hotkey          # 기존 호환용
        self.on_tts_trigger = on_tts_trigger  # TTS 단축키 콜백

        # 단축키 조합 (기본값)
        from .config import config
        self._load_modifiers_from_config(config.hotkey)
        self._option_hold_enabled = config.option_hold_enabled

        # === TTS 단축키 ===
        self._tts_modifiers: Set = set()
        self._tts_char_key: Optional[str] = None
        self._tts_active = False
        self._tts_enabled = config.tts_enabled
        self._load_tts_hotkey(config.tts_hotkey)

        # 현재 눌린 키
        self._pressed_keys: Set = set()
        # 각 키가 눌린 시각 (stale 감지용)
        self._pressed_times: dict = {}

        # 상태
        self._hotkey_press_time = 0
        self._last_hotkey_release_time = 0
        self._is_holding = False
        self._toggle_mode = False
        self._hold_timer: Optional[threading.Timer] = None
        self._hotkey_active = False

        # Option 키 길게 누르기 상태
        self._option_press_time = 0
        self._option_hold_timer: Optional[threading.Timer] = None
        self._option_is_holding = False

    def _load_tts_hotkey(self, hotkey_str: str) -> None:
        """TTS 단축키 설정 로드"""
        keys = hotkey_str.lower().replace(" ", "").split("+")
        self._tts_modifiers = set()
        self._tts_char_key = None
        for key in keys:
            if key in self.KEY_MAP:
                self._tts_modifiers.add(self.KEY_MAP[key])
            else:
                self._tts_char_key = key
        print(f"[TTS 단축키] 설정됨: {hotkey_str}")

    def update_tts_hotkey(self, hotkey_str: str) -> None:
        """TTS 단축키 업데이트"""
        self._load_tts_hotkey(hotkey_str)
        self._tts_active = False

    def set_tts_enabled(self, enabled: bool) -> None:
        """TTS 기능 활성화/비활성화"""
        self._tts_enabled = enabled
        self._tts_active = False
        print(f"[TTS 단축키] {'활성화' if enabled else '비활성화'}")

    def _is_tts_hotkey_pressed(self) -> bool:
        """TTS 단축키 조합이 눌렸는지 확인"""
        if not self._tts_enabled:
            return False
        modifiers_ok = self._tts_modifiers.issubset(self._pressed_keys)
        char_ok = self._tts_char_key in self._pressed_keys if self._tts_char_key else True
        return modifiers_ok and char_ok

    def _load_modifiers_from_config(self, hotkey_str: str) -> None:
        """설정에서 modifier와 문자 키 로드"""
        keys = hotkey_str.lower().replace(" ", "").split("+")
        self.HOTKEY_MODIFIERS = set()
        self.CHAR_KEY = None  # 문자 키 (없을 수도 있음)
        for key in keys:
            if key in self.KEY_MAP:
                self.HOTKEY_MODIFIERS.add(self.KEY_MAP[key])
            else:
                # KEY_MAP에 없으면 문자 키로 취급
                self.CHAR_KEY = key
                print(f"[단축키] 문자 키 설정: '{key}'")

    def update_modifiers(self, modifiers: list) -> None:
        """단축키 modifier 업데이트"""
        self.HOTKEY_MODIFIERS = set()
        self.CHAR_KEY = None
        for key in modifiers:
            if key in self.KEY_MAP:
                self.HOTKEY_MODIFIERS.add(self.KEY_MAP[key])
            else:
                self.CHAR_KEY = key
        # 상태 초기화
        self._pressed_keys.clear()
        self._pressed_times.clear()
        self._hotkey_active = False
        self._is_holding = False
        self._toggle_mode = False
        print(f"[단축키] 업데이트: {modifiers}")

    def set_option_hold_enabled(self, enabled: bool) -> None:
        """Option 키 길게 누르기 기능 활성화/비활성화"""
        self._option_hold_enabled = enabled
        # 상태 초기화
        if self._option_hold_timer:
            self._option_hold_timer.cancel()
            self._option_hold_timer = None
        self._option_is_holding = False
        print(f"[단축키] Option 키 길게 누르기: {'활성화' if enabled else '비활성화'}")

    def _normalize_key(self, key):
        """키를 정규화 (좌/우 구분 없이)

        macOS에서 Cmd를 누른 상태로 문자 키를 누르면 key.char=None이 되므로,
        key.vk (virtual key code)를 사용하여 문자 키를 감지한다.
        """
        if key in (keyboard.Key.cmd, keyboard.Key.cmd_l, keyboard.Key.cmd_r):
            return keyboard.Key.cmd
        elif key in (keyboard.Key.ctrl, keyboard.Key.ctrl_l, keyboard.Key.ctrl_r):
            return keyboard.Key.ctrl
        elif key in (keyboard.Key.alt, keyboard.Key.alt_l, keyboard.Key.alt_r):
            return keyboard.Key.alt
        elif key in (keyboard.Key.shift, keyboard.Key.shift_l, keyboard.Key.shift_r):
            return keyboard.Key.shift
        elif key == keyboard.Key.space:
            return keyboard.Key.space
        # 일반 키: key.char 또는 vk 코드로 문자 감지
        elif hasattr(key, 'char') or hasattr(key, 'vk'):
            # 먼저 char 속성 확인
            if hasattr(key, 'char') and key.char:
                return key.char.lower()
            # char가 None인 경우 (macOS에서 Cmd+문자 등) vk 코드로 역매핑
            if hasattr(key, 'vk') and key.vk is not None:
                char = VK_TO_CHAR.get(key.vk)
                if char:
                    return char
        return None

    def _is_hotkey_pressed(self) -> bool:
        """단축키 조합이 눌렸는지 확인

        정확히 설정된 modifier만 눌려있어야 함.
        추가 modifier가 있으면 False (하이퍼키 등과 충돌 방지)
        """
        # 현재 눌린 키에서 modifier만 추출
        all_modifiers = {keyboard.Key.cmd, keyboard.Key.ctrl,
                        keyboard.Key.alt, keyboard.Key.shift}
        pressed_modifiers = self._pressed_keys & all_modifiers

        # 설정된 modifier와 정확히 일치해야 함
        if pressed_modifiers != self.HOTKEY_MODIFIERS:
            return False

        char_ok = self.CHAR_KEY in self._pressed_keys if self.CHAR_KEY else True
        return char_ok

    def _is_only_option_pressed(self) -> bool:
        """Option 키만 눌렸는지 확인"""
        return (self._pressed_keys == {keyboard.Key.alt} and
                self._option_hold_enabled)

    def _start_option_hold_recording(self):
        """Option 키 홀드 녹음 시작"""
        with self._lock:
            # 여전히 Option 키만 눌려있는지 확인
            if self._is_only_option_pressed() and not self._option_is_holding:
                self._option_is_holding = True
                print("[단축키] Option 키 길게 누르기 - 녹음 시작")
                if self.on_hold_start:
                    threading.Thread(target=self.on_hold_start, daemon=True).start()

    def _cleanup_stale_keys(self) -> None:
        """일정 시간 이상 눌려있는 stale 키를 정리한다.

        키 릴리즈 이벤트가 누락되면 _pressed_keys에 키가 남아있어
        _hotkey_active가 True로 고정되는 문제를 방지한다.
        """
        now = time.time()
        stale_keys = [
            k for k, t in self._pressed_times.items()
            if now - t > self.STALE_KEY_TIMEOUT
        ]
        if stale_keys:
            for k in stale_keys:
                self._pressed_keys.discard(k)
                del self._pressed_times[k]
            print(f"[단축키] stale 키 정리: {stale_keys}")
            # stale 키 정리 후 hotkey_active 상태도 재확인
            if self._hotkey_active and not self._is_hotkey_pressed():
                self._hotkey_active = False
                print("[단축키] stale 정리로 hotkey_active 리셋")

    def _on_press(self, key) -> None:
        """키 누름 이벤트"""
        normalized = self._normalize_key(key)
        if normalized is None:
            return

        with self._lock:
            # 새 키가 눌릴 때마다 stale 키 정리
            self._cleanup_stale_keys()

            self._pressed_keys.add(normalized)
            self._pressed_times[normalized] = time.time()

            # Option 키만 눌렸을 때 (Option 홀드 모드가 활성화된 경우)
            if self._is_only_option_pressed() and not self._option_is_holding:
                now = time.time()
                self._option_press_time = now

                # Option 홀드 타이머 시작
                if self._option_hold_timer:
                    self._option_hold_timer.cancel()

                self._option_hold_timer = threading.Timer(
                    self.HOLD_THRESHOLD,
                    self._start_option_hold_recording
                )
                self._option_hold_timer.start()
                return  # Option 키만 눌렸을 때는 일반 단축키 로직 스킵

            # Option 키 외 다른 키가 눌리면 Option 홀드 취소
            if self._option_hold_timer and not self._is_only_option_pressed():
                self._option_hold_timer.cancel()
                self._option_hold_timer = None

            # === TTS 중지 단축키 (Ctrl+Option+Cmd 정확히 3개) ===
            tts_stop_keys = {keyboard.Key.ctrl, keyboard.Key.alt, keyboard.Key.cmd}
            pressed_mods = self._pressed_keys & {keyboard.Key.cmd, keyboard.Key.ctrl,
                                                  keyboard.Key.alt, keyboard.Key.shift}
            if pressed_mods == tts_stop_keys:
                import subprocess as _sp
                _sp.run(["killall", "afplay"], capture_output=True)
                _sp.run(["killall", "say"], capture_output=True)
                print("[단축키] TTS 중지됨")
                return

            # === TTS 단축키 감지 ===
            if self._is_tts_hotkey_pressed() and not self._tts_active:
                self._tts_active = True
                print("[TTS 단축키] 트리거됨")
                # 녹음 단축키가 이미 발동됐으면 취소
                if self._hold_timer:
                    self._hold_timer.cancel()
                    self._hold_timer = None
                if self._hotkey_active:
                    self._hotkey_active = False
                if self._is_holding:
                    self._is_holding = False
                    if self.on_hold_end:
                        threading.Thread(target=self.on_hold_end, daemon=True).start()
                if self.on_tts_trigger:
                    threading.Thread(target=self.on_tts_trigger, daemon=True).start()
                return

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
        """홀드 녹음 시작 - 단축키가 여전히 눌려있는지 재확인"""
        with self._lock:
            if not self._toggle_mode and self._hotkey_active:
                # 단축키 조합이 여전히 눌려있는지 재확인
                # 앱 전환 등으로 릴리즈 이벤트가 누락된 경우 방지
                if not self._is_hotkey_pressed():
                    print("[단축키] 홀드 시작 취소 - 키가 이미 떼어짐")
                    self._hotkey_active = False
                    return
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
            # Option 키가 릴리즈되었을 때
            if normalized == keyboard.Key.alt:
                # Option 홀드 타이머 취소
                if self._option_hold_timer:
                    self._option_hold_timer.cancel()
                    self._option_hold_timer = None

                # Option 홀드 녹음 중이었으면 중지
                if self._option_is_holding:
                    self._option_is_holding = False
                    print("[단축키] Option 키 길게 누르기 끝 - 녹음 중지")
                    if self.on_hold_end:
                        threading.Thread(target=self.on_hold_end, daemon=True).start()
                    self._pressed_keys.discard(normalized)
                    self._pressed_times.pop(normalized, None)
                    return

            was_tts_active = self._tts_active
            was_hotkey_active = self._hotkey_active

            self._pressed_keys.discard(normalized)
            self._pressed_times.pop(normalized, None)

            # === TTS 단축키 조합이 해제됨 → 상태 리셋 ===
            if was_tts_active and not self._is_tts_hotkey_pressed():
                self._tts_active = False

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

                # 짧게 눌렀으면 (홀드 아님) -> 토글 모드 시작
                if press_duration < self.HOLD_THRESHOLD + 0.1:
                    self._toggle_mode = True
                    print("[단축키] 짧게 탭 - 토글 녹음 시작")
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
        print("  - 짧게 탭: 토글 모드 (다시 탭하면 중지)")
        print("  - 꾹 누르기: 누르는 동안 녹음")
        # TTS 단축키 표시
        if self._tts_enabled:
            tts_names = []
            for key in self._tts_modifiers:
                if key == keyboard.Key.cmd:
                    tts_names.append("Cmd")
                elif key == keyboard.Key.ctrl:
                    tts_names.append("Ctrl")
                elif key == keyboard.Key.alt:
                    tts_names.append("Option")
                elif key == keyboard.Key.shift:
                    tts_names.append("Shift")
            if self._tts_char_key:
                tts_names.append(self._tts_char_key.upper())
            print(f"[TTS 단축키] {'+'.join(tts_names)} 리스닝 시작")

    def stop(self) -> None:
        """단축키 리스닝 중지"""
        if self._hold_timer:
            self._hold_timer.cancel()
        if self._listener:
            self._listener.stop()
            self._listener = None
