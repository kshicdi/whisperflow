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
