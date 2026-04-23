import subprocess
import os


class AppLauncher:
    """macOS 앱 실행 및 URL 열기"""

    # 자주 쓰는 앱 이름 매핑 (한국어 → 앱 이름)
    APP_MAP = {
        '크롬': 'Google Chrome',
        '사파리': 'Safari',
        '터미널': 'Terminal',
        '슬랙': 'Slack',
        '카카오톡': 'KakaoTalk',
        '메모': 'Notes',
        '파인더': 'Finder',
        '설정': 'System Preferences',
        'vscode': 'Visual Studio Code',
        '코드': 'Visual Studio Code',
        '디스코드': 'Discord',
        '텔레그램': 'Telegram',
        '줌': 'zoom.us',
        '유튜브': None,  # URL로 처리
        '아마존': None,
        '쿠팡': None,
        '네이버': None,
        '구글': None,
    }

    # URL 매핑
    URL_MAP = {
        '유튜브': 'https://www.youtube.com',
        '아마존': 'https://www.amazon.com',
        '쿠팡': 'https://www.coupang.com',
        '네이버': 'https://www.naver.com',
        '구글': 'https://www.google.com',
        '깃허브': 'https://github.com',
        '지메일': 'https://mail.google.com',
        '인스타': 'https://www.instagram.com',
    }

    @classmethod
    def launch_app(cls, app_name: str) -> bool:
        """앱 실행 또는 포커스"""
        try:
            subprocess.run(["open", "-a", app_name], check=True, capture_output=True)
            return True
        except subprocess.CalledProcessError:
            return False

    @classmethod
    def open_url(cls, url: str) -> bool:
        """Chrome에서 URL 열기"""
        try:
            script = f'tell application "Google Chrome" to open location "{url}"'
            subprocess.run(["osascript", "-e", script], check=True, capture_output=True)
            # Chrome 활성화
            subprocess.run(["open", "-a", "Google Chrome"], capture_output=True)
            return True
        except subprocess.CalledProcessError:
            # fallback: 기본 브라우저로 열기
            subprocess.run(["open", url], capture_output=True)
            return True

    @classmethod
    def open_chrome_extension(cls, extension_url: str) -> bool:
        """Chrome 확장 프로그램 페이지 열기"""
        return cls.open_url(extension_url)

    @classmethod
    def send_kakao_message(cls, friend_name: str, message: str, auto_send: bool = False) -> dict:
        """카카오톡에서 친구를 찾아 메시지 입력 (기본: 전송 직전 멈춤)

        Args:
            friend_name: 친구 이름
            message: 보낼 메시지
            auto_send: True면 자동 전송, False면 입력까지만

        Returns: {"success": bool, "action": str, "target": str}
        """
        import time
        from pynput.keyboard import Controller, Key

        kb = Controller()

        def clipboard_paste(text):
            proc = subprocess.Popen(["pbcopy"], stdin=subprocess.PIPE)
            proc.communicate(text.encode("utf-8"))
            time.sleep(0.3)
            kb.press(Key.cmd)
            kb.press('v')
            kb.release('v')
            kb.release(Key.cmd)

        try:
            # 1. 카카오톡 열기 + reopen (최소화 복원)
            subprocess.run(["osascript", "-e", '''
                tell application "KakaoTalk"
                    activate
                    reopen
                end tell
            '''], check=True, capture_output=True)
            time.sleep(2)

            # 2. 검색창 열기 (key code 3 = F)
            subprocess.run(["osascript", "-e", '''
                tell application "System Events"
                    tell process "KakaoTalk"
                        set frontmost to true
                        delay 0.5
                        key code 3 using command down
                    end tell
                end tell
            '''], check=True, capture_output=True)
            time.sleep(1)

            # 3. 친구 이름 입력 (pynput + 클립보드)
            clipboard_paste(friend_name)
            time.sleep(1)

            # 4. 아래 화살표 2번 → Enter (첫 번째 결과 선택 → 채팅방)
            kb.press(Key.down)
            kb.release(Key.down)
            time.sleep(0.2)
            kb.press(Key.down)
            kb.release(Key.down)
            time.sleep(0.3)
            kb.press(Key.enter)
            kb.release(Key.enter)
            time.sleep(1)

            # 5. 메시지 입력 (pynput + 클립보드)
            clipboard_paste(message)

            # 6. 전송 (auto_send가 True일 때만)
            if auto_send:
                time.sleep(0.3)
                kb.press(Key.enter)
                kb.release(Key.enter)
                return {"success": True, "action": "kakao_sent", "target": friend_name}

            return {"success": True, "action": "kakao_ready", "target": friend_name}

        except Exception as e:
            return {"success": False, "action": "kakao_error", "target": str(e)}

    @classmethod
    def handle_command(cls, text: str) -> dict:
        """음성 명령 텍스트를 파싱해서 앱 실행 또는 URL 열기

        Returns: {"success": bool, "action": str, "target": str}

        지원하는 명령 패턴:
        - "크롬 열어줘", "슬랙 실행해", "터미널 켜줘"
        - "유튜브 열어줘", "아마존 가줘", "쿠팡 보여줘"
        - "유튜브에서 검색해줘" 등
        """
        text = text.strip().lower()

        # URL 매핑 체크
        for keyword, url in cls.URL_MAP.items():
            if keyword in text:
                cls.open_url(url)
                return {"success": True, "action": "open_url", "target": keyword}

        # 앱 매핑 체크
        for keyword, app_name in cls.APP_MAP.items():
            if keyword in text and app_name is not None:
                success = cls.launch_app(app_name)
                return {"success": success, "action": "launch_app", "target": keyword}

        return {"success": False, "action": "unknown", "target": text}


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1:
        text = " ".join(sys.argv[1:])
        result = AppLauncher.handle_command(text)
        print(f"결과: {result}")
