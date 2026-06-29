"""WhisperFlow 메인 앱"""

import rumps
import sys
import os
import json
import datetime
import threading
import pyperclip
from pathlib import Path

try:
    from PyObjCTools import AppHelper  # noqa: F401 - 존재 여부 확인용
    HAS_PYOBJC = True
except ImportError:
    HAS_PYOBJC = False

LOG_FILE = "/tmp/whisperflow.log"


def log(msg):
    """파일과 콘솔에 로그 출력"""
    timestamp = datetime.datetime.now().strftime("%H:%M:%S")
    line = f"[{timestamp}] {msg}"
    print(line)
    sys.stdout.flush()
    with open(LOG_FILE, "a") as f:
        f.write(line + "\n")


import subprocess
import webbrowser

from .config import config
from .audio_recorder import AudioRecorder
from .transcriber import Transcriber
from .hotkey_manager import HotkeyManager
from .text_output import TextOutput
from .history_manager import history_manager
from .tts_reader import tts_reader

try:
    from .ws_server import WhisperFlowWSServer
except ImportError:
    WhisperFlowWSServer = None

try:
    from .camera_feed import CameraFeed
except ImportError:
    CameraFeed = None

try:
    from .gesture_control import GestureControl
except ImportError:
    GestureControl = None

try:
    from .always_listen import AlwaysListen
except ImportError:
    AlwaysListen = None


class WhisperFlowApp(rumps.App):
    """메뉴바 앱 클래스"""

    # 상태 아이콘 (유니코드 이모지)
    ICON_IDLE = "🎤"
    ICON_RECORDING = "🔴"
    ICON_PROCESSING = "⏳"

    def _set_title_safe(self, new_title: str) -> None:
        """메인 스레드에서 안전하게 타이틀을 변경.

        백그라운드 스레드에서 self.title을 직접 변경하면
        AppKit의 NSView 서브뷰 열거 카운터(_enumeratingSubviewsCount)가
        오버플로되어 SIGABRT 크래시가 발생한다.
        PyObjCTools.AppHelper.callAfter로 메인 런루프에 디스패치한다.
        """
        if threading.current_thread() is threading.main_thread():
            self.title = new_title
            return

        try:
            from PyObjCTools.AppHelper import callAfter
            callAfter(self._apply_title, new_title)
        except Exception as e:
            # callAfter 실패 시 (극히 드묾) 직접 설정 - 크래시보다는 낫다
            log(f"[경고] callAfter 실패, 직접 title 설정: {e}")
            self.title = new_title

    def _apply_title(self, new_title: str) -> None:
        """실제 타이틀 적용 (메인 스레드에서 호출됨)"""
        self.title = new_title

    def __init__(self):
        super().__init__(
            name="WhisperFlow",
            title=self.ICON_IDLE,
            quit_button="종료"
        )

        # WebSocket 서버 초기화 (JARVIS UI)
        try:
            if WhisperFlowWSServer is not None:
                self.ws_server = WhisperFlowWSServer()
                self.ws_server._on_remote_record = self._handle_remote_record
                self.ws_server._on_conversation_continue = self.enter_conversation_mode
                self.ws_server._on_chat_tts = self._handle_chat_tts
                self.ws_server._on_tts_interrupt = self._handle_tts_interrupt
                self.ws_server.start()
            else:
                self.ws_server = None
                log("[WS] websockets 모듈 미설치 - WebSocket 서버 비활성화")
        except Exception as e:
            self.ws_server = None
            log(f"[WS] 서버 초기화 실패 (무시): {e}")

        # Qwen TTS 서버 자동 시작
        self._start_qwen_tts_server()

        # 컴포넌트 초기화
        self.recorder = AudioRecorder(
            on_recording_start=self._on_recording_start,
            on_recording_stop=self._on_recording_stop,
            on_audio_level=self._on_audio_level
        )

        self.transcriber = Transcriber(
            on_transcription_start=self._on_transcription_start,
            on_transcription_done=self._on_transcription_done,
            on_transcription_error=self._on_transcription_error
        )

        self.hotkey_manager = HotkeyManager(
            on_hold_start=self._on_hotkey_start,
            on_hold_end=self._on_hotkey_end,
            on_tts_trigger=self._on_tts_trigger
        )

        self.text_output = TextOutput()

        # 카메라 피드 인스턴스
        self.camera_feed = None

        # 제스처 컨트롤 인스턴스
        self.gesture_control = None

        # 제스처 컨트롤 서브프로세스 (메뉴바 토글 — 카메라 미리보기 창 포함)
        self.gesture_proc = None
        self._gesture_timer = None
        # 앱 종료 시 제스처 서브프로세스 정리 (드래그 버튼 잠김 방지)
        import atexit
        atexit.register(self._stop_gesture_proc)

        # 상시 청취 인스턴스
        self.always_listen = None

        # 녹음 시작/종료를 atomic하게 보호하는 락
        # hotkey_manager는 여러 스레드에서 on_hold_start를 호출할 수 있으므로
        # is_recording 체크 + start_recording 호출을 하나의 임계 구역으로 묶는다
        self._recording_lock = threading.Lock()

        # 메뉴 구성
        self._setup_menu()

        # 단축키 리스닝 시작
        self.hotkey_manager.start()

        # 드라이브 모드가 켜져있으면 상시 청취 자동 시작
        if (Path.home() / ".whisperflow_auto_tts").exists():
            self._start_always_listen(skip_boot_wait=True)
            log("[TTS] 드라이브 모드 자동 시작 (이전 세션 유지)")

    def _setup_menu(self) -> None:
        """메뉴 항목 설정"""
        # 모델 선택 서브메뉴
        self.model_menu = rumps.MenuItem("모델 선택")
        self.model_items = {}
        for model in ["tiny", "base", "small", "medium", "large-v3"]:
            item = rumps.MenuItem(model, callback=self._change_model)
            if model == config.model_size:
                item.state = 1  # 체크 표시
            self.model_items[model] = item
            self.model_menu.add(item)

        # 언어 선택 서브메뉴
        self.lang_menu = rumps.MenuItem("언어 선택")
        self.lang_items = {}
        languages = [
            ("auto", "자동 감지 (한/영 혼합)"),
            ("ko", "한국어"),
            ("en", "English"),
            ("ja", "日本語"),
            ("zh", "中文"),
        ]
        for code, name in languages:
            item = rumps.MenuItem(name, callback=self._change_language)
            item._code = code  # 언어 코드 저장
            if code == config.language:
                item.state = 1
            self.lang_items[code] = item
            self.lang_menu.add(item)

        # 단축키 설정 서브메뉴
        self.hotkey_menu = rumps.MenuItem("단축키 설정")
        self.hotkey_items = {}
        modifiers = [
            ("cmd", "Command (⌘)"),
            ("ctrl", "Control (⌃)"),
            ("option", "Option (⌥)"),
            ("shift", "Shift (⇧)"),
        ]
        # 현재 설정된 단축키 파싱
        current_keys = set(config.hotkey.lower().replace(" ", "").split("+"))

        for key, name in modifiers:
            item = rumps.MenuItem(name, callback=self._toggle_hotkey_modifier)
            item._key = key
            item.state = 1 if key in current_keys else 0
            self.hotkey_items[key] = item
            self.hotkey_menu.add(item)

        # Option 키 길게 누르기 설정
        self.hotkey_menu.add(None)  # 구분선
        self.option_hold_item = rumps.MenuItem(
            "Option(⌥) 길게 누르기",
            callback=self._toggle_option_hold
        )
        self.option_hold_item.state = 1 if config.option_hold_enabled else 0
        self.hotkey_menu.add(self.option_hold_item)

        # 히스토리 설정 서브메뉴
        self.history_menu = rumps.MenuItem("히스토리")
        self.history_enabled_item = rumps.MenuItem(
            "히스토리 저장",
            callback=self._toggle_history
        )
        self.history_enabled_item.state = 1 if config.history_enabled else 0
        self.history_menu.add(self.history_enabled_item)
        self.history_menu.add(None)  # 구분선
        self.history_menu.add(rumps.MenuItem(
            "히스토리 폴더 열기",
            callback=self._open_history_folder
        ))
        self.history_menu.add(rumps.MenuItem(
            "히스토리 전체 삭제",
            callback=self._clear_history
        ))

        # TTS 설정 서브메뉴
        self.tts_menu = rumps.MenuItem("TTS (텍스트 읽기)")
        self.tts_enabled_item = rumps.MenuItem(
            "TTS 활성화",
            callback=self._toggle_tts
        )
        self.tts_enabled_item.state = 1 if config.tts_enabled else 0
        self.tts_menu.add(self.tts_enabled_item)
        self.tts_menu.add(None)  # 구분선

        # TTS 속도 설정
        self.tts_rate_menu = rumps.MenuItem("읽기 속도")
        self.tts_rate_items = {}
        rates = [
            (100, "느리게 (100)"),
            (150, "약간 느리게 (150)"),
            (200, "보통 (200)"),
            (250, "약간 빠르게 (250)"),
            (300, "빠르게 (300)"),
        ]
        for rate, name in rates:
            item = rumps.MenuItem(name, callback=self._change_tts_rate)
            item._rate = rate
            if rate == config.tts_rate:
                item.state = 1
            self.tts_rate_items[rate] = item
            self.tts_rate_menu.add(item)
        self.tts_menu.add(self.tts_rate_menu)
        self.tts_menu.add(None)  # 구분선

        # Qwen TTS 속도 설정
        self.qwen_speed_menu = rumps.MenuItem("Qwen TTS 속도")
        self.qwen_speed_items = {}
        qwen_speeds = [
            (1.0, "보통 (1.0x)"),
            (1.2, "약간 빠르게 (1.2x)"),
            (1.4, "빠르게 (1.4x)"),
            (1.6, "매우 빠르게 (1.6x)"),
            (1.8, "최고 빠르게 (1.8x)"),
        ]
        for speed, name in qwen_speeds:
            item = rumps.MenuItem(name, callback=self._change_qwen_speed)
            item._speed = speed
            if speed == config.qwen_tts_speed:
                item.state = 1
            self.qwen_speed_items[speed] = item
            self.qwen_speed_menu.add(item)
        self.tts_menu.add(self.qwen_speed_menu)

        # 빠른 응답 (say 선행) 토글
        self.say_first_item = rumps.MenuItem(
            "빠른 응답 (say 선행)",
            callback=self._toggle_say_first
        )
        self.say_first_item.state = 1 if config.tts_say_first else 0
        self.tts_menu.add(self.say_first_item)
        self.tts_menu.add(None)  # 구분선

        # 드라이브 모드 (전체 읽기) — 최상위 메뉴에 배치
        self.auto_tts_item = rumps.MenuItem(
            "🚗 드라이브 모드",
            callback=self._toggle_auto_tts
        )
        toggle_file = Path.home() / ".whisperflow_auto_tts"
        self.auto_tts_item.state = 1 if toggle_file.exists() else 0

        # 도서관 모드 (앞부분만 빠르게) — 최상위 메뉴에 배치
        self.library_tts_item = rumps.MenuItem(
            "📚 도서관 모드",
            callback=self._toggle_library_tts
        )
        library_file = Path.home() / ".whisperflow_library_tts"
        self.library_tts_item.state = 1 if library_file.exists() else 0

        self.tts_menu.add(None)  # 구분선
        self.tts_menu.add(rumps.MenuItem(
            "읽기 중지",
            callback=self._stop_tts
        ))

        # TTS 초기 속도 설정
        tts_reader.set_rate(config.tts_rate)

        # 자동 엔터 설정
        self.auto_enter_item = rumps.MenuItem(
            "자동 엔터",
            callback=self._toggle_auto_enter
        )
        self.auto_enter_item.state = 1 if config.auto_enter else 0

        # JARVIS 촬영 모드 토글 메뉴 아이템
        self.jarvis_shoot_item = rumps.MenuItem(
            "🎬 JARVIS 촬영 모드",
            callback=self._toggle_jarvis_shoot_mode
        )
        # 유튜브 TTS + 자비스 역할극이 모두 활성화되어 있으면 촬영 모드 ON 상태
        youtube_file = Path.home() / ".whisperflow_youtube_tts"
        roleplay_file = Path(self.JARVIS_ROLEPLAY_FILE)
        self.jarvis_shoot_item.state = 1 if (youtube_file.exists() and roleplay_file.exists()) else 0

        # 제스처 컨트롤 토글
        self.gesture_item = rumps.MenuItem(
            "🖐 제스처 컨트롤",
            callback=self._toggle_gesture_control
        )
        self.gesture_item.state = 0

        # Hue 조명 연동 토글
        self.hue_item = rumps.MenuItem(
            "💡 Hue 조명 연동",
            callback=self._toggle_hue
        )
        self.hue_item.state = 1 if self.ws_server and self.ws_server._hue._config.get("enabled", True) else 0

        self.menu = [
            rumps.MenuItem("녹음 시작/중지", callback=self._menu_toggle_recording),
            None,  # 구분선
            self.auto_tts_item,
            self.library_tts_item,
            self.jarvis_shoot_item,
            None,
            self.model_menu,
            self.lang_menu,
            self.hotkey_menu,
            self.history_menu,
            self.tts_menu,
            self.auto_enter_item,
            self.gesture_item,
            self.hue_item,
            None,
            rumps.MenuItem("JARVIS UI", callback=self._open_jarvis_ui),
            None,
        ]

    def _toggle_hotkey_modifier(self, sender) -> None:
        """단축키 modifier 토글"""
        key = sender._key
        sender.state = 0 if sender.state else 1

        # 선택된 modifier 수집
        selected = [k for k, item in self.hotkey_items.items() if item.state]

        if not selected:
            # 최소 하나는 선택되어야 함
            sender.state = 1
            TextOutput.show_notification("WhisperFlow", "최소 하나의 키를 선택하세요")
            return

        # 설정 저장
        new_hotkey = "+".join(selected)
        config.hotkey = new_hotkey
        config.save()

        # HotkeyManager 업데이트
        self.hotkey_manager.update_modifiers(selected)

        display = "+".join([k.upper() for k in selected])
        log(f"[설정] 단축키 변경: {display}")
        TextOutput.show_notification("WhisperFlow", f"단축키: {display}")

    def _toggle_option_hold(self, sender) -> None:
        """Option 키 길게 누르기 토글"""
        sender.state = 0 if sender.state else 1
        enabled = bool(sender.state)

        # 설정 저장
        config.option_hold_enabled = enabled
        config.save()

        # HotkeyManager 업데이트
        self.hotkey_manager.set_option_hold_enabled(enabled)

        status = "활성화" if enabled else "비활성화"
        log(f"[설정] Option 키 길게 누르기: {status}")
        TextOutput.show_notification("WhisperFlow", f"Option 키 길게 누르기: {status}")

    def _toggle_history(self, sender) -> None:
        """히스토리 저장 토글"""
        sender.state = 0 if sender.state else 1
        enabled = bool(sender.state)

        config.history_enabled = enabled
        config.save()

        status = "활성화" if enabled else "비활성화"
        log(f"[설정] 히스토리 저장: {status}")
        TextOutput.show_notification("WhisperFlow", f"히스토리 저장: {status}")

    def _open_history_folder(self, sender) -> None:
        """히스토리 폴더 열기"""
        history_dir = history_manager.get_history_dir()
        log(f"[히스토리] 폴더 열기: {history_dir}")
        subprocess.run(["open", str(history_dir)])

    def _clear_history(self, sender) -> None:
        """히스토리 전체 삭제"""
        count = history_manager.clear_all()
        log(f"[히스토리] {count}개 삭제됨")
        TextOutput.show_notification("WhisperFlow", f"히스토리 {count}개 삭제됨")

    def _change_language(self, sender) -> None:
        """언어 변경"""
        new_lang = sender._code
        log(f"[설정] 언어 변경: {config.language} → {new_lang}")

        # 체크 표시 업데이트
        for code, item in self.lang_items.items():
            item.state = 1 if code == new_lang else 0

        # 설정 저장
        config.language = new_lang
        config.save()

        TextOutput.show_notification("WhisperFlow", f"언어 변경: {sender.title}")

    def _change_model(self, sender) -> None:
        """모델 변경"""
        new_model = sender.title
        log(f"[설정] 모델 변경: {config.model_size} → {new_model}")

        # 체크 표시 업데이트
        for model, item in self.model_items.items():
            item.state = 1 if model == new_model else 0

        # 설정 저장
        config.model_size = new_model
        config.save()

        # Transcriber 모델 리로드
        self.transcriber.reload_model()

        TextOutput.show_notification("WhisperFlow", f"모델 변경: {new_model}")

    def _on_hotkey_start(self) -> None:
        """단축키로 녹음 시작 (thread-safe)"""
        with self._recording_lock:
            if self.recorder.is_recording:
                log("[앱] _on_hotkey_start 무시 - 이미 녹음 중")
                return
            TextOutput.save_active_app()
            self.recorder.start_recording()

            # 안전장치: 120초 후 자동 녹음 중지
            if hasattr(self, '_safety_timer') and self._safety_timer:
                self._safety_timer.cancel()
            self._safety_timer = threading.Timer(120.0, self._safety_stop_recording)
            self._safety_timer.daemon = True
            self._safety_timer.start()

    def _safety_stop_recording(self) -> None:
        """안전장치: 녹음이 너무 오래되면 자동 중지"""
        if self.recorder.is_recording:
            log("[앱] 안전장치 - 120초 초과 자동 녹음 중지")
            self.recorder.stop_recording()

    def _on_hotkey_end(self) -> None:
        """단축키로 녹음 종료 (thread-safe)"""
        with self._recording_lock:
            if hasattr(self, '_safety_timer') and self._safety_timer:
                self._safety_timer.cancel()
                self._safety_timer = None
            if not self.recorder.is_recording:
                log("[앱] _on_hotkey_end 무시 - 이미 녹음 중 아님")
                return
            self.recorder.stop_recording()

    _chat_conv_timer = None
    _chat_conv_lock = threading.Lock()
    _tts_proc = None
    _tts_cancelled = threading.Event()

    def _handle_chat_tts(self, text: str) -> None:
        """채팅 응답을 Qwen TTS로 읽고 WebSocket으로 오디오 전송"""
        import random
        self._tts_cancelled.set()
        with self._chat_conv_lock:
            if self._chat_conv_timer:
                self._chat_conv_timer.cancel()
                self._chat_conv_timer = None
        self._tts_cancelled.clear()
        tts_text = text[:500] if len(text) > 500 else text

        def _tts_worker():
            # 1. ack 효과음을 WebSocket tts_audio로 전송
            try:
                sounds_dir = Path(__file__).parent / "static" / "sounds"
                ack_files = sorted(sounds_dir.glob("ack_*.wav"))
                if ack_files:
                    ack_file = random.choice(ack_files)
                    import base64
                    ack_b64 = base64.b64encode(ack_file.read_bytes()).decode('utf-8')
                    self.ws_server.broadcast_raw(
                        json.dumps({"type": "tts_audio", "value": ack_b64})
                    )
            except Exception as e:
                log(f"[TTS] ack sound error: {e}")

            if self._tts_cancelled.is_set():
                return

            # 2. Qwen TTS 생성 + WebSocket 전송
            hook_path = Path.home() / ".claude" / "hooks" / "qwen_tts_speak.py"
            tts_done = False
            if hook_path.exists():
                try:
                    import urllib.request
                    r = urllib.request.urlopen('http://localhost:9093/health', timeout=2)
                    if r.status == 200:
                        cmd = ["/usr/bin/python3", str(hook_path),
                               "--no-say", "--no-play"]
                        cmd.append(tts_text)
                        proc = subprocess.Popen(
                            cmd,
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL
                        )
                        self._tts_proc = proc
                        proc.wait(timeout=120)
                        self._tts_proc = None
                        tts_done = True
                except Exception:
                    self._tts_proc = None
                    self._start_qwen_tts_server()

            if self._tts_cancelled.is_set():
                return

            if not tts_done:
                tts_reader.speak(tts_text)

            # 3. TTS 완료 → 대화 모드 진입 (PC/모바일 통합)
            try:
                self.ws_server.broadcast_raw(
                    json.dumps({"type": "tts_done"})
                )
            except Exception as e:
                log(f"[TTS] tts_done broadcast error: {e}")
            self._enter_post_tts_conversation()

        threading.Thread(target=_tts_worker, daemon=True).start()

    def _enter_post_tts_conversation(self) -> None:
        """TTS 완료 후 대화 모드 — always_listen 유무 관계없이 통합 처리"""
        if self.always_listen:
            self.always_listen.enter_conversation_mode()
        self._ws_broadcast("broadcast_state", "recording")
        log("[채팅TTS] 대화 모드 진입")
        if not self.always_listen:
            with self._chat_conv_lock:
                if self._chat_conv_timer:
                    self._chat_conv_timer.cancel()
                self._chat_conv_timer = threading.Timer(
                    10.0, self._on_chat_conversation_end)
                self._chat_conv_timer.daemon = True
                self._chat_conv_timer.start()

    def _on_chat_conversation_end(self) -> None:
        """채팅 대화 모드 타임아웃 (always_listen 미실행 시)"""
        log("[채팅TTS] 대화 모드 타임아웃 → 대기 모드")
        try:
            import base64
            sound_path = Path(__file__).parent / "static" / "sounds" / "standby.wav"
            if sound_path.exists():
                b64 = base64.b64encode(sound_path.read_bytes()).decode('utf-8')
                self.ws_server.broadcast_raw(
                    json.dumps({"type": "tts_audio", "value": b64})
                )
        except Exception:
            pass
        self._ws_broadcast("broadcast_state", "idle")

    def _handle_tts_interrupt(self) -> None:
        """모바일에서 TTS 중단 요청 — TTS 프로세스 종료 + recording 전환"""
        log("[TTS] 인터럽트 수신 — TTS 중단")
        self._tts_cancelled.set()
        if self._tts_proc:
            try:
                self._tts_proc.terminate()
            except Exception:
                pass
        with self._chat_conv_lock:
            if self._chat_conv_timer:
                self._chat_conv_timer.cancel()
                self._chat_conv_timer = None
        self._ws_broadcast("broadcast_state", "recording")

    _remote_recording = False  # 원격 녹음 여부 플래그

    def _handle_remote_record(self, data: dict) -> None:
        """iPad 등 원격 클라이언트의 녹음 명령 처리 (WS 스레드에서 호출됨)"""
        action = data.get("action", "toggle")
        log(f"[원격] remote_record 수신: action={action}")

        with self._recording_lock:
            if action == "start":
                if self.recorder.is_recording:
                    log("[원격] 이미 녹음 중 - 무시")
                    return
                self._remote_recording = True
                self.recorder.start_recording()
            elif action == "stop":
                if not self.recorder.is_recording:
                    log("[원격] 녹음 중 아님 - 무시")
                    return
                self.recorder.stop_recording()
            else:
                # toggle (기본)
                if self.recorder.is_recording:
                    self.recorder.stop_recording()
                else:
                    self._remote_recording = True
                    self.recorder.start_recording()

    def _menu_toggle_recording(self, sender) -> None:
        """메뉴에서 녹음 토글"""
        log("[메뉴] 녹음 토글 클릭됨")
        self._toggle_recording()

    def _toggle_recording(self) -> None:
        """녹음 토글 (thread-safe)"""
        with self._recording_lock:
            log(f"[앱] _toggle_recording 호출, 현재 녹음 중: {self.recorder.is_recording}")
            if self.recorder.is_recording:
                self.recorder.stop_recording()
            else:
                # 녹음 시작 전 현재 활성 앱 저장
                TextOutput.save_active_app()
                self.recorder.start_recording()

    def _start_qwen_tts_server(self) -> None:
        """Qwen TTS 서버가 안 떠있으면 백그라운드로 시작"""
        try:
            import urllib.request
            urllib.request.urlopen('http://localhost:9093/health', timeout=2)
            log("[TTS] Qwen TTS 서버 이미 실행 중")
        except Exception:
            qwen_dir = os.environ.get('QWEN_TTS_DIR')
            if not qwen_dir:
                log("[TTS] QWEN_TTS_DIR 환경변수 미설정, Qwen TTS 서버 자동 시작 스킵")
                return
            venv_python = os.path.join(qwen_dir, ".venv", "bin", "python")
            serve_script = os.path.join(qwen_dir, "serve.py")
            if os.path.exists(serve_script):
                try:
                    subprocess.Popen(
                        [venv_python, serve_script, "--port", "9093"],
                        stdout=open("/tmp/qwen_tts_server.log", "a"),
                        stderr=subprocess.STDOUT,
                        start_new_session=True
                    )
                    log("[TTS] Qwen TTS 서버 자동 시작")
                except Exception as e:
                    log(f"[TTS] Qwen TTS 서버 시작 실패: {e}")

    def _ws_broadcast(self, method: str, *args) -> None:
        """ws_server 브로드캐스트를 안전하게 호출 (ws_server가 None이거나 예외 시 무시)"""
        if self.ws_server is None:
            return
        try:
            getattr(self.ws_server, method)(*args)
        except Exception:
            pass

    def _on_audio_level(self, level: float) -> None:
        """오디오 레벨 콜백 → WebSocket 브로드캐스트"""
        self._ws_broadcast("broadcast_audio_level", level)

    def _open_jarvis_ui(self, sender) -> None:
        """JARVIS UI를 기본 브라우저에서 열기"""
        webbrowser.open("http://localhost:8767")

    JARVIS_ROLEPLAY_FILE = os.path.expanduser("~/.whisperflow_jarvis_roleplay")

    # === 모드 비활성화 헬퍼 (상호 배타 처리용) ===

    def _deactivate_drive_mode(self) -> None:
        """드라이브 모드 비활성화 (파일 삭제 + 상시 청취 정지 + 메뉴 state 동기화)"""
        (Path.home() / ".whisperflow_auto_tts").unlink(missing_ok=True)
        self.auto_tts_item.state = 0
        # 촬영 모드가 아닌 경우에만 상시 청취 정지
        if not self.jarvis_shoot_item.state:
            self._stop_always_listen()

    def _deactivate_library_mode(self) -> None:
        """도서관 모드 비활성화 (파일 삭제 + 메뉴 state 동기화)"""
        (Path.home() / ".whisperflow_library_tts").unlink(missing_ok=True)
        self.library_tts_item.state = 0

    def _deactivate_jarvis_shoot_mode(self) -> None:
        """JARVIS 촬영 모드 비활성화 (유튜브 TTS + 역할극 + 카메라 + 제스처 종료)"""
        (Path.home() / ".whisperflow_youtube_tts").unlink(missing_ok=True)
        Path(self.JARVIS_ROLEPLAY_FILE).unlink(missing_ok=True)
        self.jarvis_shoot_item.state = 0

        # 카메라 피드 종료
        if self.camera_feed is not None:
            self.camera_feed.stop()
            self.camera_feed = None

        # 제스처 컨트롤 종료
        if self.gesture_control is not None:
            self.gesture_control.stop()
            self.gesture_control = None

        # 상시 청취 종료 (드라이브 모드가 아닌 경우에만)
        if not self.auto_tts_item.state:
            self._stop_always_listen()

        self._ws_broadcast("broadcast_raw", '{"type":"browser_stop"}')

    def _toggle_jarvis_shoot_mode(self, sender) -> None:
        """JARVIS 촬영 모드 ON/OFF 토글 — 유튜브 TTS + 자비스 역할극 + 카메라를 한 번에"""
        if sender.state:
            # --- OFF ---
            self._deactivate_jarvis_shoot_mode()
            log("[촬영] JARVIS 촬영 모드 OFF")
            TextOutput.show_notification("WhisperFlow", "JARVIS 촬영 모드 OFF")
        else:
            # --- ON ---
            # 다른 모드 OFF (sender.state 변경 전에 먼저 비활성화)
            self._deactivate_drive_mode()
            self._deactivate_library_mode()

            sender.state = True

            # 유튜브 TTS ON
            (Path.home() / ".whisperflow_youtube_tts").touch()

            # 자비스 역할극 ON
            Path(self.JARVIS_ROLEPLAY_FILE).touch()

            # 카메라는 촬영 모드에서 자동 시작하지 않음 — 음성 요청 시 수동 활성화

            # 제스처 컨트롤 — 비활성화 (mediapipe 호환 문제, 이슈 #9에서 해결 예정)
            # if GestureControl is not None:
            #     self.gesture_control = GestureControl(camera_index=1)
            #     self.gesture_control.start()

            # 상시 청취 시작 (박수 대기 → 웨이크 워드 → 음성 감지)
            self._start_always_listen(skip_boot_wait=False)
            if self.always_listen:
                log("[촬영] 상시 청취 시작")

            log("[촬영] JARVIS 촬영 모드 ON (박수 2번으로 시스템 온라인)")
            TextOutput.show_notification("WhisperFlow", "JARVIS 촬영 모드 ON")

    # === 상시 청취 공통 헬퍼 ===

    def _detect_mic_preset(self) -> dict:
        """현재 입력 디바이스에 맞는 프리셋 반환"""
        try:
            import sounddevice as sd
            dev = sd.query_devices(sd.default.device[0])
            name = dev['name'].lower()
            if 'airpod' in name:
                log(f"[마이크] 에어팟 감지: {dev['name']}")
                return {'audio_gain': 12, 'wake_threshold': 0.35, 'speech_threshold': 0.5}
            elif 'iphone' in name:
                log(f"[마이크] 아이폰 감지: {dev['name']}")
                return {'audio_gain': 8, 'wake_threshold': 0.4, 'speech_threshold': 0.5}
            else:
                log(f"[마이크] 맥북/기타 감지: {dev['name']}")
                return {'audio_gain': 20, 'wake_threshold': 0.5, 'speech_threshold': 0.5}
        except Exception as e:
            log(f"[마이크] 디바이스 감지 실패: {e}")
            return {'audio_gain': 20, 'wake_threshold': 0.5, 'speech_threshold': 0.5}

    def _start_always_listen(self, skip_boot_wait: bool = False) -> None:
        """상시 청취 시작 (촬영/드라이브 공용, 마이크 자동 감지)"""
        if AlwaysListen is None:
            return
        if self.always_listen is not None:
            self.always_listen.stop()

        preset = self._detect_mic_preset()
        log(f"[상시청취] 프리셋: gain={preset['audio_gain']}, wake={preset['wake_threshold']}, speech={preset['speech_threshold']}")

        self.always_listen = AlwaysListen(
            on_double_clap=self._on_double_clap,
            on_wake=self._on_wake_word,
            on_speech_detected=self._on_speech_detected,
            on_audio_level=self._on_audio_level,
            on_conversation_end=self._on_conversation_end,
            skip_boot_wait=skip_boot_wait,
            audio_gain=preset['audio_gain'],
            wake_threshold=preset['wake_threshold'],
            speech_threshold=preset['speech_threshold'],
        )
        self.always_listen.start()
        # filming_scenarios에 참조 전달 (TTS 중 마이크 음소거용)
        from . import filming_scenarios
        filming_scenarios._always_listen_ref = self.always_listen

    def _stop_always_listen(self) -> None:
        """상시 청취 정지"""
        if self.always_listen is not None:
            self.always_listen.stop()
            self.always_listen = None
        from . import filming_scenarios
        filming_scenarios._always_listen_ref = None

    def _handle_camera_command(self, text: str) -> bool:
        """카메라 켜기/끄기 명령 키워드 매칭 (드라이브 모드용, LLM 바이패스)"""
        # 짧은 명령만 매칭 (긴 문장에서 카메라 언급은 무시)
        if len(text) > 30:
            return False
        # 카메라 켜기: "카메라 켜줘", "아이폰 카메라 연결", "맥북 카메라 켜줘" 등
        if '카메라' in text and any(w in text for w in ['켜', '열어', '활성', '시작', '연결']):
            # 아이폰 vs 맥북 판별
            if '맥북' in text or '맥' in text:
                camera_index = 0  # MacBook 내장 카메라
                camera_name = "맥북"
            else:
                camera_index = 1  # iPhone Continuity Camera
                camera_name = "아이폰"

            if self.camera_feed is not None:
                self.camera_feed.stop()
                self.camera_feed = None
            if CameraFeed is not None:
                self.camera_feed = CameraFeed(camera_index=camera_index, fps=5)
                self.camera_feed.start()
                log(f"[카메라] {camera_name} 카메라 시작 (index={camera_index})")
                self._play_sound("camera_on.wav")
            return True

        # 카메라 전환: "카메라 전환", "카메라 바꿔" 등
        if '카메라' in text and any(w in text for w in ['전환', '바꿔', '바꾸', '스위치', '변경']):
            if self.camera_feed is not None:
                current_index = self.camera_feed.camera_index
                new_index = 0 if current_index == 1 else 1
                new_name = "맥북" if new_index == 0 else "아이폰"
                self.camera_feed.stop()
                self.camera_feed = CameraFeed(camera_index=new_index, fps=5)
                self.camera_feed.start()
                log(f"[카메라] 전환 → {new_name} (index={new_index})")
                self._play_sound("camera_on.wav")
            return True

        # 카메라 끄기: "카메라 꺼줘", "카메라 종료" 등
        if '카메라' in text and any(w in text for w in ['꺼', '닫', '종료', '중지']):
            if self.camera_feed is not None:
                self.camera_feed.stop()
                self.camera_feed = None
                log("[카메라] 카메라 종료")
                self._play_sound("camera_off.wav")
                self._ws_broadcast("broadcast_raw", '{"type":"browser_stop"}')
            return True

        return False

    def _play_sound(self, filename: str) -> None:
        """효과음 재생 (마이크 음소거 + UI speaking 상태)"""
        import time
        sound_path = os.path.join(os.path.dirname(__file__), "static", "sounds", filename)
        if not os.path.exists(sound_path):
            return
        self._ws_broadcast("broadcast_state", "tts_playing")
        if self.always_listen:
            self.always_listen.mute()
        subprocess.Popen(["afplay", sound_path]).wait()
        time.sleep(0.2)
        if self.always_listen:
            self.always_listen.unmute()
        self._ws_broadcast("broadcast_state", "idle")

    def _on_conversation_end(self) -> None:
        """대화 모드 타임아웃 → 대기 모드 효과음 + idle"""
        import time
        log("[상시청취] 대화 모드 종료 → 대기 모드")
        sound_path = os.path.join(os.path.dirname(__file__), "static", "sounds", "standby.wav")
        if self.always_listen and os.path.exists(sound_path):
            self._ws_broadcast("broadcast_state", "tts_playing")
            self.always_listen.mute()
            try:
                import base64
                with open(sound_path, 'rb') as f:
                    b64 = base64.b64encode(f.read()).decode('utf-8')
                self.ws_server.broadcast_raw(
                    json.dumps({"type": "tts_audio", "value": b64})
                )
            except Exception:
                pass
            subprocess.Popen(["afplay", sound_path]).wait()
            time.sleep(0.2)
            if self.always_listen:
                self.always_listen.unmute()
        self._ws_broadcast("broadcast_state", "idle")

    def enter_conversation_mode(self) -> None:
        """외부에서 대화 모드 진입 요청 (TTS 완료 후 호출)"""
        # 드라이브 모드인데 always_listen이 안 떠있으면 자동 시작
        if not self.always_listen and (Path.home() / ".whisperflow_auto_tts").exists():
            self._start_always_listen(skip_boot_wait=True)
            log("[상시청취] 드라이브 모드 감지 — 상시 청취 자동 시작")
        if self.always_listen:
            self.always_listen.enter_conversation_mode()
            self._ws_broadcast("broadcast_state", "recording")
            log("[상시청취] 대화 모드 진입 — 바로 말씀하세요")

    def _on_double_clap(self) -> None:
        """박수 2번 감지 → 시스템 온라인"""
        log("[상시청취] 더블 클랩 감지! → 시스템 온라인")
        from . import filming_scenarios
        filming_scenarios._handle_system_online("")

    def _on_audio_level(self, level: float, low: float = 0, mid: float = 0, high: float = 0) -> None:
        """실시간 3밴드 오디오 레벨 → JARVIS UI 파티클 반응"""
        if self.ws_server:
            self.ws_server.broadcast_audio_level(float(level))

    def _kill_tts(self) -> None:
        """실행 중인 TTS 프로세스(afplay, say, qwen_tts_speak)를 즉시 강제 종료"""
        # 플래그 파일 제거
        Path("/tmp/whisperflow-tts-playing").unlink(missing_ok=True)
        # pkill -9로 즉시 강제 종료
        for pattern in ["afplay", "qwen_tts_speak"]:
            try:
                subprocess.run(["pkill", "-9", "-f", pattern], capture_output=True, timeout=1)
            except Exception:
                pass
        # say는 정확히 매칭 (다른 프로세스 오킬 방지)
        try:
            subprocess.run(["pkill", "-9", "say"], capture_output=True, timeout=1)
        except Exception:
            pass
        log("[TTS] 강제 중지 완료")

    def _on_wake_word(self) -> None:
        """웨이크 워드 감지 → TTS 중지 + 'Yes, sir' 효과음 + JARVIS UI 리스닝 상태

        이 콜백은 _fire_wake 스레드에서 호출되므로 동기 블로킹 OK.
        효과음 재생 중 마이크 음소거 → 재생 끝나면 음소거 해제 + 녹음 버퍼/타이머 리셋.
        """
        import time

        # 실행 중인 TTS를 즉시 중지
        self._kill_tts()
        log("[상시청취] 헤이 자비스 감지! → TTS 중지 + Yes sir 재생 + 녹음 대기")

        sound_path = os.path.join(os.path.dirname(__file__), "static", "sounds", "yes_sir.wav")
        if self.always_listen and os.path.exists(sound_path):
            self._ws_broadcast("broadcast_state", "tts_playing")
            self.always_listen.mute()
            log("[상시청취] Yes sir 재생 시작")
            subprocess.Popen(["afplay", sound_path]).wait()
            log("[상시청취] Yes sir 재생 완료 → 녹음 버퍼 리셋 + unmute")
            time.sleep(0.2)
            # 효과음 재생 중 쌓인 빈 버퍼 제거 + 녹음 타이머 리셋
            self.always_listen.reset_recording()
            self.always_listen.unmute()
            log(f"[상시청취] unmute 완료. state={self.always_listen._state}, muted={self.always_listen._muted}")

        self._ws_broadcast("broadcast_state", "recording")
        log("[상시청취] 녹음 대기 중... (말씀하세요)")

    def _on_speech_detected(self, audio_data, sample_rate) -> None:
        """음성 감지 → 확인 효과음 → Whisper 변환 → 시나리오 실행"""
        log(f"[상시청취] 음성 감지 → Whisper 변환 시작 ({len(audio_data)/sample_rate:.1f}초)")
        import tempfile, wave
        try:
            # 녹음 완료 → speaking으로 효과음 재생 → processing으로 전환
            self._play_sound("processing.wav")
            self._ws_broadcast("broadcast_state", "processing")

            tmp = tempfile.NamedTemporaryFile(suffix='.wav', delete=False)
            # float32 → int16 WAV 저장
            audio_int16 = (audio_data * 32767).astype('int16')
            with wave.open(tmp.name, 'wb') as wf:
                wf.setnchannels(1)
                wf.setsampwidth(2)
                wf.setframerate(sample_rate)
                wf.writeframes(audio_int16.tobytes())
            self._remote_recording = True  # 터미널에 붙여넣기 위해
            self.transcriber.transcribe_async(tmp.name)
        except Exception as e:
            log(f"[상시청취] 변환 오류: {e}")

    def _on_recording_start(self) -> None:
        """녹음 시작 콜백"""
        log("[녹음] 시작")
        self._set_title_safe(self.ICON_RECORDING)
        self._ws_broadcast("broadcast_state", "recording")

    def _on_recording_stop(self, audio_path: str) -> None:
        """녹음 종료 콜백"""
        log(f"[녹음] 종료 - 파일: {audio_path}")
        self._set_title_safe(self.ICON_PROCESSING)
        self._ws_broadcast("broadcast_state", "processing")
        # 비동기로 변환 시작
        self.transcriber.transcribe_async(audio_path)

    def _on_transcription_start(self) -> None:
        """변환 시작 콜백"""
        log("[변환] 시작 (모델 로딩 중...)")
        self._set_title_safe(self.ICON_PROCESSING)

    def _on_transcription_done(self, text: str) -> None:
        """변환 완료 콜백"""
        log(f"[변환] 완료 - 텍스트: {text}")
        self._set_title_safe(self.ICON_IDLE)
        if text:
            # 촬영 모드일 때만 촬영 시나리오 체크 (LLM 바이패스)
            jarvis_roleplay = Path(self.JARVIS_ROLEPLAY_FILE).exists()
            if jarvis_roleplay:
                from . import filming_scenarios
                # 사용자 입력 텍스트를 먼저 UI에 표시
                self._ws_broadcast("broadcast_transcript", text)
                if filming_scenarios.handle(text):
                    log(f"[촬영시나리오] 매칭: {text[:50]}")
                    self._ws_broadcast("broadcast_state", "idle")
                    return

            # 드라이브 모드: 카메라 명령 키워드 매칭 (LLM 바이패스)
            drive_mode = (Path.home() / ".whisperflow_auto_tts").exists()
            if drive_mode and self._handle_camera_command(text):
                # 카메라 명령 처리 후 대화 모드 유지
                self.enter_conversation_mode()
                return

            # 매칭 안 되면 Claude Code로 전달
            self._ws_broadcast("broadcast_state", "thinking")
            self._ws_broadcast("broadcast_transcript", text)
        else:
            self._ws_broadcast("broadcast_state", "idle")

        if text:
            if self._remote_recording:
                # 카메라가 켜져 있으면 최신 프레임 캡처 (고정 경로, 1개만 유지)
                if self.camera_feed is not None:
                    frame_b64 = self.camera_feed.get_current_frame()
                    if frame_b64:
                        import base64
                        frame_path = "/tmp/jarvis_camera_latest.jpg"
                        with open(frame_path, 'wb') as f:
                            f.write(base64.b64decode(frame_b64))
                        text = f"[카메라가 켜져 있음. 이미지 확인이 필요하면: {frame_path}] {text}"
                        log(f"[카메라] 최신 프레임 저장: {frame_path}")

                # 원격 녹음: pbcopy로 클립보드 복사 후 현재 앱에 붙여넣기
                self._remote_recording = False
                import subprocess as _sp
                import time
                # pbcopy로 확실하게 클립보드 복사
                proc = _sp.Popen(["pbcopy"], stdin=_sp.PIPE)
                proc.communicate(text.encode("utf-8"))
                log(f"[출력] pbcopy 완료: {text[:50]}")
                time.sleep(0.3)
                # 현재 활성 앱에 Cmd+V + Enter
                _sp.run(["osascript", "-e", '''
                    tell application "System Events"
                        key code 9 using command down
                        delay 0.3
                        key code 36
                    end tell
                '''], capture_output=True)
                success = True
                log(f"[출력] 원격 → 현재 앱 붙여넣기 완료")
            else:
                success = self.text_output.output(text)
            log(f"[출력] 클립보드 복사: {success}")
            if success:
                # 알림 표시
                preview = text[:50] + "..." if len(text) > 50 else text
                TextOutput.show_notification(
                    "WhisperFlow",
                    f"클립보드에 복사됨: {preview}"
                )
        else:
            TextOutput.show_notification(
                "WhisperFlow",
                "변환된 텍스트가 없습니다"
            )

    def _on_transcription_error(self, error: str) -> None:
        """변환 오류 콜백"""
        log(f"[오류] {error}")
        self._set_title_safe(self.ICON_IDLE)
        self._ws_broadcast("broadcast_state", "idle")
        TextOutput.show_notification("WhisperFlow 오류", error)

    def _change_qwen_speed(self, sender) -> None:
        """Qwen TTS 속도 변경"""
        new_speed = sender._speed
        log(f"[설정] Qwen TTS 속도 변경: {config.qwen_tts_speed} → {new_speed}")

        for speed, item in self.qwen_speed_items.items():
            item.state = 1 if speed == new_speed else 0

        config.qwen_tts_speed = new_speed
        config.save()

        # 훅 스크립트의 SPEED 값도 업데이트
        import re
        hook_path = Path.home() / ".claude" / "hooks" / "qwen_tts_speak.py"
        if hook_path.exists():
            content = hook_path.read_text()
            content = re.sub(r'SPEED = [\d.]+', f'SPEED = {new_speed}', content)
            hook_path.write_text(content)

        TextOutput.show_notification("WhisperFlow", f"Qwen TTS 속도: {new_speed}x")

    def _toggle_say_first(self, sender) -> None:
        """빠른 응답 (say 선행) 토글"""
        sender.state = 0 if sender.state else 1
        config.tts_say_first = bool(sender.state)
        config.save()

        # Claude Code 훅 설정 파일에 --no-say 플래그 반영
        self._update_hook_no_say_flag()

        status = "ON (say+Qwen)" if config.tts_say_first else "OFF (Qwen만)"
        log(f"[설정] 빠른 응답: {status}")
        TextOutput.show_notification("WhisperFlow", f"빠른 응답: {status}")

    def _update_hook_no_say_flag(self) -> None:
        """Claude Code hooks 설정에서 --no-say 플래그 업데이트"""
        settings_path = Path.home() / ".claude" / "settings.json"
        if not settings_path.exists():
            return
        try:
            import json
            data = json.loads(settings_path.read_text())
            hooks = data.get("hooks", {})

            for event_name, event_hooks in hooks.items():
                if not isinstance(event_hooks, list):
                    continue
                for hook in event_hooks:
                    cmd = hook.get("command", "")
                    if "qwen_tts_speak.py" not in cmd:
                        continue
                    # --no-say 플래그 추가/제거
                    if config.tts_say_first:
                        hook["command"] = cmd.replace(" --no-say", "")
                    else:
                        if "--no-say" not in cmd:
                            hook["command"] = cmd + " --no-say"

            settings_path.write_text(json.dumps(data, indent=2, ensure_ascii=False))
            log("[설정] hooks settings.json 업데이트 완료")
        except Exception as e:
            log(f"[설정] hooks 업데이트 실패: {e}")

    # === TTS 관련 메서드 ===

    def _toggle_tts(self, sender) -> None:
        """TTS 활성화/비활성화"""
        sender.state = 0 if sender.state else 1
        enabled = bool(sender.state)

        config.tts_enabled = enabled
        config.save()

        self.hotkey_manager.set_tts_enabled(enabled)

        status = "활성화" if enabled else "비활성화"
        log(f"[설정] TTS: {status}")
        TextOutput.show_notification("WhisperFlow", f"TTS: {status}")

    def _change_tts_rate(self, sender) -> None:
        """TTS 속도 변경"""
        new_rate = sender._rate
        log(f"[설정] TTS 속도 변경: {config.tts_rate} → {new_rate}")

        # 체크 표시 업데이트
        for rate, item in self.tts_rate_items.items():
            item.state = 1 if rate == new_rate else 0

        config.tts_rate = new_rate
        config.save()

        tts_reader.set_rate(new_rate)

        TextOutput.show_notification("WhisperFlow", f"TTS 속도: {new_rate}")

    def _toggle_auto_enter(self, sender) -> None:
        """자동 엔터 토글"""
        sender.state = 0 if sender.state else 1
        config.auto_enter = bool(sender.state)
        config.save()
        status = "ON" if config.auto_enter else "OFF"
        log(f"[설정] 자동 엔터: {status}")
        TextOutput.show_notification("WhisperFlow", f"자동 엔터: {status}")

    def _toggle_auto_tts(self, sender) -> None:
        """드라이브 모드 토글"""
        toggle_file = Path.home() / ".whisperflow_auto_tts"
        sender.state = 0 if sender.state else 1
        enabled = bool(sender.state)

        if enabled:
            toggle_file.touch()
            # 다른 모드 OFF
            self._deactivate_library_mode()
            self._deactivate_jarvis_shoot_mode()
            # 상시 청취 시작 (박수 없이 바로 웨이크 워드 대기)
            self._start_always_listen(skip_boot_wait=True)
            log("[TTS] 드라이브 모드 ON (상시 청취 시작)")
            TextOutput.show_notification("WhisperFlow", "드라이브 모드 ON")
        else:
            toggle_file.unlink(missing_ok=True)
            self._stop_always_listen()
            log("[TTS] 드라이브 모드 OFF")
            TextOutput.show_notification("WhisperFlow", "드라이브 모드 OFF")

    def _toggle_library_tts(self, sender) -> None:
        """도서관 모드 토글"""
        library_file = Path.home() / ".whisperflow_library_tts"
        sender.state = 0 if sender.state else 1
        enabled = bool(sender.state)

        if enabled:
            library_file.touch()
            # 다른 모드 OFF
            self._deactivate_drive_mode()
            self._deactivate_jarvis_shoot_mode()
            log("[TTS] 도서관 모드 ON")
            TextOutput.show_notification("WhisperFlow", "도서관 모드 ON")
        else:
            library_file.unlink(missing_ok=True)
            log("[TTS] 도서관 모드 OFF")
            TextOutput.show_notification("WhisperFlow", "도서관 모드 OFF")

    def _toggle_gesture_control(self, sender) -> None:
        """제스처 컨트롤 ON/OFF 토글 — 카메라 미리보기 창과 함께 별도 프로세스로 실행.

        rumps가 메인 스레드를 점유해 같은 프로세스에서는 OpenCV 창을 못 띄우므로
        미리보기 창이 있는 테스트+맥 제어 모드를 서브프로세스로 돌린다.
        카메라는 CLI 쪽에서 맥북 내장 카메라로 자동 감지된다.
        """
        # 미리보기 창에서 q로 닫은 경우 등 죽은 프로세스 정리
        if self.gesture_proc is not None and self.gesture_proc.poll() is not None:
            self.gesture_proc = None
            sender.state = 0

        if self.gesture_proc is not None:
            # OFF
            self._stop_gesture_proc()
            sender.state = 0
            log("[제스처] 제스처 컨트롤 OFF")
            TextOutput.show_notification("WhisperFlow", "제스처 컨트롤 OFF")
        else:
            # ON
            try:
                project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
                gesture_log = open("/tmp/whisperflow_gesture.log", "a")
                self.gesture_proc = subprocess.Popen(
                    [sys.executable, "-m", "whisperflow.gesture_control", "--test", "--mac"],
                    cwd=project_root,
                    stdout=gesture_log,
                    stderr=gesture_log,
                )
                sender.state = 1
                # 미리보기 창에서 q로 닫았을 때 토글 상태 동기화
                self._gesture_timer = rumps.Timer(self._check_gesture_proc, 2)
                self._gesture_timer.start()
                log(f"[제스처] 제스처 컨트롤 ON (pid={self.gesture_proc.pid})")
                TextOutput.show_notification("WhisperFlow", "제스처 컨트롤 ON — 미리보기 창에서 q로도 종료 가능")
            except Exception as e:
                log(f"[제스처] 시작 실패: {e}")
                TextOutput.show_notification("WhisperFlow", f"제스처 컨트롤 시작 실패: {e}")

    def _check_gesture_proc(self, timer) -> None:
        """제스처 서브프로세스가 스스로 종료됐는지 감시 (q 키 등) → 토글 상태 동기화"""
        if self.gesture_proc is None or self.gesture_proc.poll() is not None:
            self.gesture_proc = None
            self.gesture_item.state = 0
            timer.stop()
            log("[제스처] 프로세스 종료 감지 — 토글 OFF")

    def _stop_gesture_proc(self) -> None:
        """제스처 서브프로세스 종료 (SIGTERM → 드래그 해제 후 종료, 3초 내 미응답 시 강제)"""
        if self._gesture_timer is not None:
            self._gesture_timer.stop()
            self._gesture_timer = None
        if self.gesture_proc is not None:
            if self.gesture_proc.poll() is None:
                self.gesture_proc.terminate()
                try:
                    self.gesture_proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    self.gesture_proc.kill()
            self.gesture_proc = None

    def _toggle_hue(self, sender) -> None:
        """Hue 조명 연동 ON/OFF 토글"""
        sender.state = 0 if sender.state else 1
        enabled = bool(sender.state)
        if self.ws_server:
            self.ws_server._hue._config["enabled"] = enabled
        log(f"[Hue] 조명 연동 {'ON' if enabled else 'OFF'}")
        TextOutput.show_notification("WhisperFlow", f"Hue 조명 연동 {'ON' if enabled else 'OFF'}")

    def _stop_tts(self, sender) -> None:
        """TTS 읽기 중지 (메뉴)"""
        tts_reader.stop()
        self._ws_broadcast("broadcast_state", "idle")
        log("[TTS] 읽기 중지 (메뉴)")

    def _on_tts_trigger(self) -> None:
        """TTS 단축키 콜백 - 선택된 텍스트를 읽기

        1. 현재 클립보드 내용 백업
        2. Cmd+C로 선택 텍스트 복사
        3. 클립보드에서 텍스트 읽기
        4. TTS로 읽기
        5. 원래 클립보드 내용 복원
        """
        import time
        import pyperclip

        # 이미 읽기 중이면 중지
        if tts_reader.is_speaking:
            tts_reader.stop()
            self._ws_broadcast("broadcast_state", "idle")
            log("[TTS] 읽기 중지 (단축키)")
            return

        log("[TTS] 클립보드 텍스트 읽기 시작")

        try:
            # 클립보드에서 텍스트 읽기 (사용자가 미리 Cmd+C로 복사)
            clipboard_text = pyperclip.paste()

            if clipboard_text and clipboard_text.strip():
                preview = clipboard_text[:50] + "..." if len(clipboard_text) > 50 else clipboard_text
                log(f"[TTS] 읽기: {preview}")
                TextOutput.show_notification("WhisperFlow TTS", f"읽는 중: {preview}")

                self._ws_broadcast("broadcast_state", "tts_playing")

                # Qwen TTS 사용 가능하면 Qwen, 아니면 기본 TTS
                try:
                    import urllib.request
                    r = urllib.request.urlopen('http://localhost:9093/health', timeout=2)
                    if r.status == 200:
                        qwen_hook = os.environ.get('QWEN_TTS_HOOK')
                        if qwen_hook and os.path.exists(qwen_hook):
                            cmd = ["/usr/bin/python3", qwen_hook]
                            if not config.tts_say_first:
                                cmd.append("--no-say")
                            cmd.append(clipboard_text)
                            subprocess.Popen(
                                cmd,
                                stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL
                            )
                            return
                except Exception:
                    pass
                # fallback
                tts_reader.speak(clipboard_text)
            else:
                log("[TTS] 클립보드 비어있음")
                TextOutput.show_notification("WhisperFlow", "먼저 텍스트를 복사해주세요 (Cmd+C)")

        except Exception as e:
            log(f"[TTS] 오류: {e}")
            TextOutput.show_notification("WhisperFlow 오류", f"TTS 오류: {e}")


def main():
    """앱 실행"""
    app = WhisperFlowApp()
    app.run()


if __name__ == "__main__":
    main()
