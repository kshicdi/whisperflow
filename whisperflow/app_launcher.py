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
        '음악': 'Music',
        '애플뮤직': 'Music',
        '뮤직': 'Music',
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

    # 명령 응답 텍스트 — 텍스트만 등록하면 TTS 자동 생성
    RESPONSE_MAP = {
        'music_play': '네, 음악을 재생하겠습니다, sir.',
        'welcome_home': 'Welcome home, sir.',
        'system_online_final': 'All systems are fully operational, sir! What shall I prepare for you today?',
        'kakao_sent': '카카오톡으로 요청하신 메시지를 전달했습니다, sir.',
        'chrome_open': '크롬을 실행하겠습니다, sir.',
        'youtube_open': '유튜브를 열겠습니다, sir.',
    }

    @classmethod
    def _jarvis_send(cls, msg_type, value):
        jarvis_send = os.path.join(os.path.dirname(__file__), "jarvis_send.py")
        venv_python = os.path.join(os.path.dirname(__file__), "..", "venv", "bin", "python")
        if os.path.exists(jarvis_send):
            subprocess.run([venv_python, jarvis_send, msg_type, value], capture_output=True)

    @classmethod
    def _speak_response(cls, response_key: str) -> None:
        """응답 텍스트로 음성 재생 + JARVIS UI 텍스트 표시 + speaking 파형
        TTS 파일이 없으면 자동 생성"""
        text = cls.RESPONSE_MAP.get(response_key, '')
        if not text:
            return

        sounds_dir = os.path.join(os.path.dirname(__file__), "static", "sounds")
        os.makedirs(sounds_dir, exist_ok=True)
        path = os.path.join(sounds_dir, f'{response_key}.wav')

        # TTS 파일 없으면 자동 생성
        if not os.path.exists(path):
            try:
                import urllib.request, json
                # 자연스러운 발음을 위해 긴 맥락으로 생성
                full_text = text
                data = json.dumps({'text': full_text, 'voice': 'clone:jarvis', 'speed': 1.0}).encode()
                req = urllib.request.Request('http://localhost:9093/generate', data=data,
                                           headers={'Content-Type': 'application/json'})
                resp = urllib.request.urlopen(req, timeout=30)
                with open(path, 'wb') as f:
                    f.write(resp.read())
            except Exception:
                return

        if os.path.exists(path):
            cls._jarvis_send("state", "tts_playing")
            cls._jarvis_send("output", text)
            p = subprocess.Popen(["afplay", "-r", "1.4", path])
            p.wait()
            cls._jarvis_send("state", "idle")

    @classmethod
    def _move_to_secondary_monitor(cls, app_name: str) -> None:
        """앱 창을 보조 모니터로 이동"""
        import time
        time.sleep(0.5)
        try:
            subprocess.run(["osascript", "-e", f'''
                tell application "{app_name}"
                    activate
                    delay 0.3
                    set bounds of front window to {{-1920, 0, 0, 1080}}
                end tell
            '''], capture_output=True, timeout=5)
        except Exception:
            pass

    @classmethod
    def launch_app(cls, app_name: str, move_window: bool = False) -> bool:
        """앱 실행 또는 포커스 (창 이동 없이 그대로)"""
        try:
            subprocess.run(["open", "-a", app_name], check=True, capture_output=True)
            if move_window:
                cls._move_to_secondary_monitor(app_name)
            return True
        except subprocess.CalledProcessError:
            return False

    @classmethod
    def open_url(cls, url: str) -> bool:
        """Chrome에서 URL 열기 (보조 모니터, 신규 창)"""
        try:
            subprocess.run(["open", "-na", "Google Chrome", "--args", "--new-window", url],
                          check=True, capture_output=True)
            cls._move_to_secondary_monitor("Google Chrome")
            return True
        except subprocess.CalledProcessError:
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
        """음성 명령 텍스트를 파싱해서 앱 실행, URL 열기, 카카오톡 메시지 등

        Returns: {"success": bool, "action": str, "target": str}

        지원하는 명령 패턴:
        - "크롬 열어줘", "슬랙 실행해", "터미널 켜줘"
        - "유튜브 열어줘", "아마존 가줘", "쿠팡 보여줘"
        - "카카오톡 남소영에게 잘가 보내줘"
        - "카톡 철수에게 회의 참석해 보내줘"
        """
        import re
        text = text.strip()
        text_lower = text.lower()

        # 카카오톡 메시지 패턴 체크
        # "카카오톡/카톡에서 [이름]에게 [메시지] 보내줘/전해줘/문자"
        kakao_pattern = re.search(r'(?:카카오톡|카톡)(?:에서)?\s+(.+?)(?:에게|한테)\s+(.+?)(?:\s*(?:보내|전해|라고|발송|문자))', text)
        if kakao_pattern:
            friend = kakao_pattern.group(1).strip()
            message = kakao_pattern.group(2).strip()
            result = cls.send_kakao_message(friend, message, auto_send=True)
            return result

        # 유튜브 검색/재생 패턴: "유튜브에서 XX 검색/틀어/재생"
        yt_pattern = re.search(r'유튜브\s*(?:에서|가서|에)?\s*(.+?)(?:\s*(?:검색|틀어|재생|보여|찾아))', text)
        if yt_pattern:
            query = yt_pattern.group(1).strip()
            url = f'https://www.youtube.com/results?search_query={query.replace(" ", "+")}'
            cls.open_url(url)
            return {"success": True, "action": "youtube_search", "target": query}

        # 액션 단어가 있어야만 앱/URL 명령으로 인식
        action_words = ['열어', '실행', '켜줘', '켜봐', '가줘', '가봐', '보여줘', '틀어', '이동']
        has_action = any(w in text_lower for w in action_words)

        # 음악 틀어줘 → Apple Music 창 열기 + 음성 응답 (실제 재생은 안 함)
        if ('음악' in text_lower or '뮤직' in text_lower) and has_action:
            import threading
            cls.launch_app('Music', move_window=False)
            threading.Thread(target=cls._play_preset_sound, args=('music_play.wav',), daemon=True).start()
            return {"success": True, "action": "launch_app_voice", "target": "음악"}

        if has_action:
            # URL 매핑 체크
            for keyword, url in cls.URL_MAP.items():
                if keyword in text_lower:
                    cls.open_url(url)
                    return {"success": True, "action": "open_url", "target": keyword}

            # 앱 매핑 체크
            for keyword, app_name in cls.APP_MAP.items():
                if keyword in text_lower and app_name is not None:
                    success = cls.launch_app(app_name)
                    return {"success": success, "action": "launch_app", "target": keyword}

        # JARVIS 온라인 명령 (별도 스레드로 실행 — 블로킹 방지)
        if '온라인' in text_lower or ('시스템' in text_lower and '온라인' in text_lower):
            import threading
            threading.Thread(target=cls.jarvis_online, daemon=True).start()
            return {"success": True, "action": "jarvis_online", "target": "system"}

        return {"success": False, "action": "unknown", "target": text}

    @classmethod
    def jarvis_online(cls):
        """자비스 온라인 시퀀스: welcome → 부팅 UI → 완료 음성"""
        import time

        sounds_dir = os.path.join(os.path.dirname(__file__), "static", "sounds")
        welcome = os.path.join(sounds_dir, "welcome_home.wav")
        system_online = os.path.join(sounds_dir, "system_online_final.wav")
        jarvis_send = os.path.join(os.path.dirname(__file__), "jarvis_send.py")
        venv_python = os.path.join(os.path.dirname(__file__), "..", "venv", "bin", "python")

        # 0. JARVIS UI를 별도 Chrome 프로필 + 앱 모드 + 전체화면 + 메인 모니터
        subprocess.Popen([
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            "--app=http://localhost:8767",
            "--user-data-dir=" + os.path.expanduser("~/.chrome-jarvis-ui"),
            "--start-fullscreen",
            "--window-position=0,0"
        ])
        time.sleep(3)

        def _send(msg_type, value):
            if os.path.exists(jarvis_send):
                subprocess.run([venv_python, jarvis_send, msg_type, value], capture_output=True)

        # 1. Speaking 상태 + Welcome home, sir.
        _send("state", "tts_playing")
        if os.path.exists(welcome):
            subprocess.Popen(["afplay", "-r", "1.4", welcome])
            time.sleep(2)
        _send("state", "idle")

        # 2. 부팅 시퀀스 UI
        _send("ui_action", "system_boot")

        # 3. 부팅 UI 완료 대기 후 Speaking 상태 + 시스템 완료 음성
        time.sleep(3.5)
        _send("state", "tts_playing")
        if os.path.exists(system_online):
            p = subprocess.Popen(["afplay", "-r", "1.4", system_online])
            p.wait()  # 음성 끝날 때까지 대기
        _send("state", "idle")


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1:
        text = " ".join(sys.argv[1:])
        result = AppLauncher.handle_command(text)
        print(f"결과: {result}")
