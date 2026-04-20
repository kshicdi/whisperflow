"""텍스트 출력 모듈"""

import subprocess
import pyperclip
from typing import Literal

from .config import config


class TextOutput:
    """텍스트 출력 클래스"""

    @staticmethod
    def to_clipboard(text: str) -> bool:
        """클립보드에 복사"""
        try:
            pyperclip.copy(text)
            return True
        except Exception as e:
            print(f"클립보드 복사 오류: {e}")
            return False

    # 마지막 활성 앱 저장
    _last_active_app = None

    @classmethod
    def save_active_app(cls):
        """현재 활성화된 앱 저장 (앱 경로에서 실제 이름 추출)"""
        try:
            # 실제 앱 이름 가져오기 (Electron 등 문제 해결)
            script = '''
            tell application "System Events"
                set frontApp to first application process whose frontmost is true
                set appPath to POSIX path of (application file of frontApp as alias)
            end tell
            return appPath
            '''
            result = subprocess.run(
                ["osascript", "-e", script],
                capture_output=True,
                text=True
            )
            app_path = result.stdout.strip()
            # /Applications/AppName.app -> AppName
            if app_path and ".app" in app_path:
                import os
                app_name = os.path.basename(app_path).replace(".app", "")
                if app_name and app_name != "Terminal":
                    cls._last_active_app = app_name
                    print(f"[앱 저장] {app_name}")
        except Exception as e:
            print(f"[앱 저장 오류] {e}")

    @classmethod
    def type_text(cls, text: str) -> bool:
        """클립보드에 복사 후 자동 붙여넣기 (Cmd+V)"""
        import time
        try:
            # 1. 클립보드에 복사
            pyperclip.copy(text)
            print(f"[붙여넣기] 클립보드 복사 완료")

            # 2. 이전 앱으로 전환 후 붙여넣기
            time.sleep(0.3)

            # 키보드 이벤트로 Cmd+V (+ 자동 엔터)
            if config.auto_enter:
                script = '''
                tell application "System Events"
                    key code 9 using command down
                    delay 0.3
                    key code 36
                end tell
                '''
            else:
                script = '''
                tell application "System Events"
                    key code 9 using command down
                end tell
                '''

            result = subprocess.run(
                ["osascript", "-e", script],
                capture_output=True,
                text=True
            )

            if result.returncode != 0:
                print(f"[붙여넣기] AppleScript 오류: {result.stderr}")
                return False

            print(f"[붙여넣기] Cmd+V 전송 완료 -> {cls._last_active_app}")
            return True
        except Exception as e:
            print(f"[붙여넣기] 오류: {e}")
            return False

    @classmethod
    def output(cls, text: str, mode: Literal["clipboard", "type"] = None) -> bool:
        """설정에 따라 텍스트 출력"""
        if mode is None:
            mode = config.output_mode

        if mode == "type":
            return cls.type_text(text)
        else:
            return cls.to_clipboard(text)

    @staticmethod
    def show_notification(title: str, message: str) -> None:
        """macOS 알림 표시"""
        try:
            script = f'''
            display notification "{message.replace('"', '\\"')}" with title "{title.replace('"', '\\"')}"
            '''
            subprocess.run(
                ["osascript", "-e", script],
                check=True,
                capture_output=True
            )
        except subprocess.CalledProcessError:
            pass
