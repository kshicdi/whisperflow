"""
filming_scenarios.py - 유튜브 촬영용 고정 시나리오

촬영 모드에서만 동작. 키워드 매칭 → 액션 실행 + 음성 응답 + JARVIS UI 텍스트.
"""

import os
import re
import subprocess
import threading


# 경로 설정
_BASE_DIR = os.path.dirname(__file__)
_SOUNDS_DIR = os.path.join(_BASE_DIR, "static", "sounds")
_JARVIS_SEND = os.path.join(_BASE_DIR, "jarvis_send.py")
_VENV_PYTHON = os.path.join(_BASE_DIR, "..", "venv", "bin", "python")


def _send(msg_type, value):
    if os.path.exists(_JARVIS_SEND):
        subprocess.run([_VENV_PYTHON, _JARVIS_SEND, msg_type, value], capture_output=True)


_always_listen_ref = None  # app.py에서 설정

def _play_and_display(sound_file, text):
    """음성 재생 + JARVIS UI 텍스트 표시 + speaking 파형 (마이크 일시 중지)"""
    path = os.path.join(_SOUNDS_DIR, sound_file)
    if not os.path.exists(path):
        return
    # TTS 재생 중 마이크 음소거 (자기 소리 듣기 방지)
    if _always_listen_ref:
        _always_listen_ref.mute()
    _send("state", "tts_playing")
    _send("output", text)
    p = subprocess.Popen(["afplay", "-r", "1.4", path])
    p.wait()
    _send("state", "idle")
    # 재생 끝나면 마이크 복원 (약간 대기 후)
    import time
    time.sleep(0.5)
    if _always_listen_ref:
        _always_listen_ref.unmute()


def _async_play(sound_file, text):
    """별도 스레드에서 음성 재생 (블로킹 방지)"""
    threading.Thread(target=_play_and_display, args=(sound_file, text), daemon=True).start()


# ============================================================
#  시나리오 정의
# ============================================================

def _handle_system_online(text):
    """시스템 온라인 → 부팅 시퀀스"""
    from .app_launcher import AppLauncher
    threading.Thread(target=AppLauncher.jarvis_online, daemon=True).start()
    return True


def _handle_music(text):
    """음악 틀어줘 → Apple Music + 음성"""
    subprocess.Popen(["open", "-a", "Music"])
    _async_play("music_play.wav", "Playing music, sir.")
    return True


def _handle_youtube(text):
    """유튜브 열어줘 → Chrome 유튜브 + 음성"""
    # 유튜브 검색 패턴 체크
    yt_search = re.search(r'유튜브\s*(?:에서|가서|에)?\s*(.+?)(?:\s*(?:검색|틀어|재생|보여|찾아))', text)
    if yt_search:
        query = yt_search.group(1).strip()
        url = f'https://www.youtube.com/results?search_query={query.replace(" ", "+")}'
    else:
        url = 'https://www.youtube.com'
    subprocess.Popen(["open", "-na", "Google Chrome", "--args", "--new-window", url])
    _async_play("youtube_open.wav", "Opening YouTube, sir.")
    return True


def _handle_kakao_open(text):
    """카카오톡 열어줘 → KakaoTalk 실행 + 음성"""
    subprocess.run(["osascript", "-e", '''
        tell application "KakaoTalk"
            activate
            reopen
        end tell
    '''], capture_output=True)
    _async_play("kakao_open.wav", "Opening KakaoTalk, sir.")
    return True


def _handle_chrome(text):
    """크롬 열어줘 → Chrome + 음성"""
    subprocess.Popen(["open", "-a", "Google Chrome"])
    _async_play("chrome_open.wav", "Opening Chrome, sir.")
    return True


def _handle_kakao(text):
    """카톡 [이름]에게 [메시지] 보내줘"""
    pattern = re.search(r'(?:카카오톡|카톡)(?:에서|에)?\s+(.+?)(?:에게|한테)\s+(.+?)(?:\s*(?:보내|전해|라고|발송|문자))', text)
    if not pattern:
        return False
    friend = pattern.group(1).strip()
    message = pattern.group(2).strip()
    from .app_launcher import AppLauncher
    AppLauncher.send_kakao_message(friend, message, auto_send=True)
    _async_play("kakao_sent.wav", "Message sent via KakaoTalk, sir.")
    return True


def _handle_camera_on(text):
    """카메라 켜줘"""
    from .camera_feed import CameraFeed
    # 글로벌 카메라 인스턴스 관리는 app.py에서 하므로 여기선 신호만
    _send("ui_action", "browser_boot")
    _async_play("camera_on.wav", "Camera activated, sir.")
    return True


def _handle_camera_off(text):
    """카메라 꺼줘"""
    _send("browser_stop", "")
    _async_play("camera_off.wav", "Camera deactivated, sir.")
    return True


# ============================================================
#  시나리오 매칭 테이블
# ============================================================

# (키워드 조건, 핸들러) — 위에서부터 순서대로 매칭
SCENARIOS = [
    # 시스템 온라인
    (lambda t: '온라인' in t or '올라인' in t or 'online' in t, _handle_system_online),
    # 카카오톡 메시지 (패턴이 복잡하므로 핸들러 내부에서 검증)
    (lambda t: ('카카오톡' in t or '카톡' in t) and ('에게' in t or '한테' in t), _handle_kakao),
    # 유튜브 (액션 단어 필요)
    (lambda t: '유튜브' in t and any(w in t for w in ['열어', '실행', '켜', '가', '틀어', '검색', '재생']), _handle_youtube),
    # 음악
    (lambda t: ('음악' in t or '뮤직' in t) and any(w in t for w in ['틀어', '들어', '실행', '켜', '열어', '재생']), _handle_music),
    # 카카오톡 앱 실행 (메시지 아닌 단순 열기)
    (lambda t: ('카카오톡' in t or '카톡' in t) and any(w in t for w in ['열어', '실행', '켜']), _handle_kakao_open),
    # 크롬
    (lambda t: '크롬' in t and any(w in t for w in ['열어', '실행', '켜']), _handle_chrome),
    # 카메라 ON
    (lambda t: '카메라' in t and any(w in t for w in ['켜', '열어', '활성', '시작']), _handle_camera_on),
    # 카메라 OFF
    (lambda t: '카메라' in t and any(w in t for w in ['꺼', '닫', '종료', '중지']), _handle_camera_off),
]


def _handle_general(text):
    """시나리오 매칭 안 된 일반 명령 → Claude CLI로 처리"""
    def _run():
        try:
            # Claude CLI 호출 (자비스 스타일 응답 지시)
            prompt = f'[자비스] 다음 질문에 자비스처럼 간결하게 1~3문장으로 답변해. 마크다운 금지. sir로 끝내.: {text}'
            result = subprocess.run(
                ["claude", "-p", prompt],
                capture_output=True, text=True, timeout=120
            )
            response = result.stdout.strip()
            if response:
                # JARVIS UI에 표시
                if _always_listen_ref:
                    _always_listen_ref.mute()
                _send("state", "tts_playing")
                _send("output", response)

                # Qwen TTS로 음성 재생
                try:
                    import urllib.request, json, tempfile
                    data = json.dumps({'text': response, 'voice': 'clone:jarvis', 'speed': 1.0}).encode()
                    req = urllib.request.Request('http://localhost:9093/generate', data=data,
                                               headers={'Content-Type': 'application/json'})
                    resp = urllib.request.urlopen(req, timeout=30)
                    audio = resp.read()
                    tmp = tempfile.NamedTemporaryFile(suffix='.wav', delete=False)
                    tmp.write(audio)
                    tmp.close()
                    p = subprocess.Popen(["afplay", "-r", "1.4", tmp.name])
                    p.wait()
                    os.unlink(tmp.name)
                except Exception:
                    pass

                _send("state", "idle")
                import time
                time.sleep(0.5)
                if _always_listen_ref:
                    _always_listen_ref.unmute()
        except Exception as e:
            print(f"[촬영시나리오] Claude CLI 오류: {e}")

    threading.Thread(target=_run, daemon=True).start()
    return True


def handle(text: str) -> bool:
    """촬영 시나리오 매칭 및 실행. 매칭 안 되면 Claude CLI로 처리.
    Returns: True (항상 촬영 모드에서 처리)"""
    text_lower = text.strip().lower()
    for condition, handler in SCENARIOS:
        if condition(text_lower):
            try:
                return handler(text)
            except Exception:
                return False
    # 매칭 안 되면 Claude CLI로 일반 질문 처리
    return _handle_general(text)
    return False
