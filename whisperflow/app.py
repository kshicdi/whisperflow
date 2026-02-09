"""WhisperFlow 메인 앱"""

import rumps
import sys
import datetime

LOG_FILE = "/tmp/whisperflow.log"


def log(msg):
    """파일과 콘솔에 로그 출력"""
    timestamp = datetime.datetime.now().strftime("%H:%M:%S")
    line = f"[{timestamp}] {msg}"
    print(line)
    sys.stdout.flush()
    with open(LOG_FILE, "a") as f:
        f.write(line + "\n")


from .config import config
from .audio_recorder import AudioRecorder
from .transcriber import Transcriber
from .hotkey_manager import HotkeyManager
from .text_output import TextOutput


class WhisperFlowApp(rumps.App):
    """메뉴바 앱 클래스"""

    # 상태 아이콘 (유니코드 이모지)
    ICON_IDLE = "🎤"
    ICON_RECORDING = "🔴"
    ICON_PROCESSING = "⏳"

    def __init__(self):
        super().__init__(
            name="WhisperFlow",
            title=self.ICON_IDLE,
            quit_button="종료"
        )

        # 컴포넌트 초기화
        self.recorder = AudioRecorder(
            on_recording_start=self._on_recording_start,
            on_recording_stop=self._on_recording_stop
        )

        self.transcriber = Transcriber(
            on_transcription_start=self._on_transcription_start,
            on_transcription_done=self._on_transcription_done,
            on_transcription_error=self._on_transcription_error
        )

        self.hotkey_manager = HotkeyManager(
            on_hold_start=self._on_hotkey_start,
            on_hold_end=self._on_hotkey_end
        )

        self.text_output = TextOutput()

        # 메뉴 구성
        self._setup_menu()

        # 단축키 리스닝 시작
        self.hotkey_manager.start()

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

        self.menu = [
            rumps.MenuItem("녹음 시작/중지", callback=self._menu_toggle_recording),
            None,  # 구분선
            self.model_menu,
            self.lang_menu,
            self.hotkey_menu,
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
        """단축키로 녹음 시작"""
        if not self.recorder.is_recording:
            TextOutput.save_active_app()
            self.recorder.start_recording()

    def _on_hotkey_end(self) -> None:
        """단축키로 녹음 종료"""
        if self.recorder.is_recording:
            self.recorder.stop_recording()

    def _menu_toggle_recording(self, sender) -> None:
        """메뉴에서 녹음 토글"""
        log("[메뉴] 녹음 토글 클릭됨")
        self._toggle_recording()

    def _toggle_recording(self) -> None:
        """녹음 토글"""
        log(f"[앱] _toggle_recording 호출, 현재 녹음 중: {self.recorder.is_recording}")
        if self.recorder.is_recording:
            self.recorder.stop_recording()
        else:
            # 녹음 시작 전 현재 활성 앱 저장
            TextOutput.save_active_app()
            self.recorder.start_recording()

    def _on_recording_start(self) -> None:
        """녹음 시작 콜백"""
        log("[녹음] 시작")
        self.title = self.ICON_RECORDING

    def _on_recording_stop(self, audio_path: str) -> None:
        """녹음 종료 콜백"""
        log(f"[녹음] 종료 - 파일: {audio_path}")
        self.title = self.ICON_PROCESSING
        # 비동기로 변환 시작
        self.transcriber.transcribe_async(audio_path)

    def _on_transcription_start(self) -> None:
        """변환 시작 콜백"""
        log("[변환] 시작 (모델 로딩 중...)")
        self.title = self.ICON_PROCESSING

    def _on_transcription_done(self, text: str) -> None:
        """변환 완료 콜백"""
        log(f"[변환] 완료 - 텍스트: {text}")
        self.title = self.ICON_IDLE

        if text:
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
        self.title = self.ICON_IDLE
        TextOutput.show_notification("WhisperFlow 오류", error)


def main():
    """앱 실행"""
    app = WhisperFlowApp()
    app.run()


if __name__ == "__main__":
    main()
