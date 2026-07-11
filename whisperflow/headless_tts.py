"""
headless_tts.py — Headless(GUI 없음) 환경용 Qwen TTS 핸들러

맥미니처럼 rumps/GUI가 없는 환경에서 채팅 응답 텍스트를 받아:
  1. ACK 효과음을 즉시 tts_audio 웹소켓 메시지로 브로드캐스트
  2. 문장 청크 단위로 Qwen TTS /generate 호출 → ffmpeg 배속 → tts_audio 브로드캐스트
  3. Qwen 서버 불가 시 macOS `say` 폴백으로 WAV 생성 후 동일 경로 처리
  4. 전체 완료 시 tts_done 브로드캐스트

환경변수:
    QWEN_TTS_URL        Qwen TTS 서버 URL (기본: http://localhost:9093)
    JARVIS_TTS_VOICE    음성 ID (기본: clone:jarvis)
    JARVIS_TTS_SPEED    배속 (기본: 1.4)

표준 라이브러리만 사용. 외부 pip 의존성 없음.
"""

import base64
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import threading
import urllib.request

logger = logging.getLogger("whisperflow.headless_tts")

# ── 환경변수 기반 설정 ────────────────────────────────────────────────────────
_QWEN_URL = os.environ.get("QWEN_TTS_URL", "http://localhost:9093")
_VOICE = os.environ.get("JARVIS_TTS_VOICE", "clone:jarvis")
_SPEED = float(os.environ.get("JARVIS_TTS_SPEED", "1.4"))
# 콜드 스타트(모델 로딩) 포함 생성 대기 상한. 60초는 첫 요청에서 초과할 수 있음.
_TIMEOUT = float(os.environ.get("QWEN_TTS_TIMEOUT", "120"))
_SEED = 42
_INSTRUCT = "Calm and warm narration tone, speaking at a steady moderate pace"
_MAX_TEXT = 500


# ── 텍스트 전처리 ────────────────────────────────────────────────────────────

_PRONUNCIATION_MAP = {
    'AI': '에이아이', 'MIT': '엠아이티', 'ADHD': '에이디에이치디',
    'IQ': '아이큐', 'EQ': '이큐', 'OECD': '오이시디',
    'UNESCO': '유네스코', 'WHO': '더블유에이치오', 'GDP': '지디피',
    'ICT': '아이씨티', 'IoT': '아이오티', 'GPT': '지피티',
    'LLM': '엘엘엠', 'STEM': '스템', 'SAT': '에스에이티',
    'GPA': '지피에이', 'PhD': '피에이치디', 'MBA': '엠비에이',
    'TOEFL': '토플', 'IELTS': '아이엘츠', 'AP': '에이피',
    'IB': '아이비', 'P5': '피파이브',
    'CLAUDE.md': '클로드 엠디', 'CLAUDE': '클로드', 'Claude': '클로드',
    'README.md': '리드미', 'TTS': '티티에스', 'STT': '에스티티',
    'API': '에이피아이', 'URL': '유알엘', 'CLI': '씨엘아이',
    'UI': '유아이', 'PR': '피알', 'JSON': '제이슨',
    'HTML': '에이치티엠엘', 'CSS': '씨에스에스',
    'macOS': '맥오에스', 'WhisperFlow': '위스퍼플로우', 'Qwen': '큐웬',
}

_DIGIT_KR = {
    '0': '영', '1': '일', '2': '이', '3': '삼', '4': '사',
    '5': '오', '6': '육', '7': '칠', '8': '팔', '9': '구',
}


def _decimal_to_kr(m):
    integer_part = m.group(1)
    decimal_digits = ''.join(_DIGIT_KR[d] for d in m.group(2))
    return integer_part + '점' + decimal_digits


def _apply_pronunciation(text: str) -> str:
    text = re.sub(r'["""‘’“”]', '', text)
    text = re.sub(r'(\d+)\.(\d+)', _decimal_to_kr, text)
    for eng, kor in _PRONUNCIATION_MAP.items():
        pattern = r'(?<![A-Za-z])' + re.escape(eng) + r'(?![A-Za-z])'
        text = re.sub(pattern, kor, text)
    return text


def _split_sentences(text: str) -> list:
    """마침표/물음표/느낌표 기준으로 문장 분리 후 청크로 묶기.
    첫 청크 100자, 이후 150자 (qwen_tts_speak.py와 동일 로직).
    """
    sentences = re.split(r'(?<=[.!?])\s+', text)
    sentences = [s.strip() for s in sentences if s.strip()]
    if not sentences:
        return [text]

    chunks = []
    current = ""
    sentence_count = 0
    for s in sentences:
        if current:
            current += " " + s
        else:
            current = s
        sentence_count += 1

        max_len = 100 if not chunks else 150
        if sentence_count >= 3 or len(current) >= max_len:
            chunks.append(current)
            current = ""
            sentence_count = 0

    if current:
        if chunks and len(current) < 50:
            chunks[-1] += " " + current
        else:
            chunks.append(current)

    return chunks if chunks else [text]


# ── Qwen TTS ─────────────────────────────────────────────────────────────────

def _check_qwen_health() -> bool:
    """Qwen TTS 서버 헬스체크 (GET /health, timeout 2초)."""
    try:
        r = urllib.request.urlopen(f"{_QWEN_URL}/health", timeout=2)
        return r.status == 200
    except Exception:
        return False


def _generate_audio(text: str) -> bytes:
    """Qwen TTS /generate 호출 → WAV bytes 반환."""
    payload = json.dumps({
        "text": text,
        "voice": _VOICE,
        "seed": _SEED,
        "instruct": _INSTRUCT,
    }).encode("utf-8")
    req = urllib.request.Request(
        f"{_QWEN_URL}/generate",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    resp = urllib.request.urlopen(req, timeout=_TIMEOUT)
    return resp.read()


def warmup():
    """Qwen 서버 모델 프리로드. 헤드리스 서버 기동 시 백그라운드 스레드에서 호출.

    첫 사용자 요청이 모델 로딩(수십 초)을 기다리지 않도록 짧은 문장을 미리 생성한다.
    Qwen 서버가 아직 안 떠 있을 수 있으므로 헬스체크를 여러 번 재시도한다.
    """
    import time

    def _run():
        for _ in range(12):  # 최대 ~1분 대기
            if _check_qwen_health():
                break
            time.sleep(5)
        else:
            logger.info("[headless_tts] warmup: Qwen 서버 미응답 — 프리로드 생략")
            return
        try:
            _generate_audio("안녕하세요")
            logger.info("[headless_tts] warmup: Qwen 모델 프리로드 완료")
        except Exception as e:
            logger.warning("[headless_tts] warmup 실패 (무시): %s", e)

    threading.Thread(target=_run, daemon=True).start()


def _speed_up_audio(audio_bytes: bytes, speed: float) -> bytes:
    """ffmpeg atempo로 배속 변경 (음높이 유지). ffmpeg 없으면 원본 반환."""
    if not shutil.which("ffmpeg"):
        logger.debug("[headless_tts] ffmpeg 없음 — 원본 속도로 전송")
        return audio_bytes
    tmp_in = tempfile.mktemp(suffix=".wav")
    tmp_out = tempfile.mktemp(suffix=".wav")
    try:
        with open(tmp_in, "wb") as f:
            f.write(audio_bytes)
        subprocess.run(
            ["ffmpeg", "-i", tmp_in, "-filter:a", f"atempo={speed}", "-y", tmp_out],
            capture_output=True,
            timeout=10,
        )
        if os.path.exists(tmp_out):
            with open(tmp_out, "rb") as f:
                return f.read()
    except Exception as e:
        logger.warning("[headless_tts] ffmpeg 배속 실패: %s", e)
    finally:
        for p in (tmp_in, tmp_out):
            try:
                os.unlink(p)
            except Exception:
                pass
    return audio_bytes


def _say_to_wav(text: str) -> bytes | None:
    """macOS `say` 명령으로 WAV 파일 생성 후 bytes 반환. 실패 시 None."""
    tmp_path = tempfile.mktemp(suffix=".wav")
    try:
        result = subprocess.run(
            [
                "say",
                "-v", "Yuna",
                "--data-format=LEI16@22050",
                "-o", tmp_path,
                text,
            ],
            capture_output=True,
            timeout=30,
        )
        if result.returncode == 0 and os.path.exists(tmp_path):
            with open(tmp_path, "rb") as f:
                return f.read()
    except Exception as e:
        logger.warning("[headless_tts] say 폴백 실패: %s", e)
    finally:
        try:
            os.unlink(tmp_path)
        except Exception:
            pass
    return None


# ── 핸들러 ──────────────────────────────────────────────────────────────────

class HeadlessTTSHandler:
    """ws_server._on_chat_tts 콜백으로 등록되는 헤드리스 TTS 핸들러.

    ws_server 인스턴스를 받아 broadcast_raw()로 tts_audio / tts_done 메시지를
    브로드캐스트한다. 이전 TTS 진행 중에 새 요청이 오면 다음 청크 경계에서 중단.
    """

    def __init__(self, ws_server):
        self._ws = ws_server
        # 세대 카운터: handle()마다 증가. 워커는 자기 세대가 아니면 중단.
        # (Event set/clear 방식은 이전 워커가 플래그를 못 볼 틈이 있음)
        self._generation = 0
        self._lock = threading.Lock()

    # ── 내부 헬퍼 ─────────────────────────────────────────────────────────

    def _broadcast(self, msg: dict):
        try:
            self._ws.broadcast_raw(json.dumps(msg))
        except Exception as e:
            logger.error("[headless_tts] broadcast 실패: %s", e)

    def _send_ack(self):
        """ACK 효과음(ack_*.wav 랜덤 1개)을 즉시 tts_audio로 전송."""
        import random
        from pathlib import Path
        try:
            sounds_dir = Path(__file__).parent / "static" / "sounds"
            ack_files = sorted(sounds_dir.glob("ack_*.wav"))
            if ack_files:
                ack_file = random.choice(ack_files)
                ack_b64 = base64.b64encode(ack_file.read_bytes()).decode("utf-8")
                self._broadcast({"type": "tts_audio", "value": ack_b64})
        except Exception as e:
            logger.warning("[headless_tts] ACK 전송 실패: %s", e)

    def _send_audio_chunk(self, audio_bytes: bytes):
        """WAV bytes를 배속 조정 후 base64 인코딩해 tts_audio로 브로드캐스트."""
        fast = _speed_up_audio(audio_bytes, _SPEED)
        b64 = base64.b64encode(fast).decode("utf-8")
        self._broadcast({"type": "tts_audio", "value": b64})

    # ── 워커 ──────────────────────────────────────────────────────────────

    def _is_stale(self, gen: int) -> bool:
        with self._lock:
            return gen != self._generation

    def _tts_worker(self, text: str, gen: int):
        # 1. ACK 효과음 즉시 전송
        self._send_ack()

        if self._is_stale(gen):
            return

        # 2. 텍스트 전처리 + 청크 분리
        processed = _apply_pronunciation(text)
        chunks = _split_sentences(processed)

        # 3. Qwen 서버 상태 확인
        use_qwen = _check_qwen_health()
        if not use_qwen:
            logger.info("[headless_tts] Qwen 서버 미응답 — say 폴백 사용")

        # 4. 청크별 오디오 생성 + 전송
        for chunk in chunks:
            if self._is_stale(gen):
                logger.info("[headless_tts] TTS 취소됨 (새 요청 도착)")
                return

            audio: bytes | None = None

            if use_qwen:
                try:
                    audio = _generate_audio(chunk)
                except Exception as e:
                    # Qwen 서버가 크래시했으면 launchd(KeepAlive)가 곧 되살림 → 한 번 재시도
                    logger.warning("[headless_tts] Qwen generate 실패: %s — 5초 후 재시도", e)
                    import time
                    time.sleep(5)
                    if self._is_stale(gen):
                        return
                    if _check_qwen_health():
                        try:
                            audio = _generate_audio(chunk)
                        except Exception as e2:
                            logger.warning("[headless_tts] Qwen 재시도 실패: %s — say 폴백", e2)
                            use_qwen = False
                    else:
                        logger.warning("[headless_tts] Qwen 서버 다운 — say 폴백")
                        use_qwen = False

            if audio is None:
                # say 폴백: 청크 전체를 한 번에
                audio = _say_to_wav(chunk)

            if audio is None:
                logger.warning("[headless_tts] 청크 생성 실패, 스킵: %r", chunk[:40])
                continue

            if self._is_stale(gen):
                return

            self._send_audio_chunk(audio)

        # 5. 완료 신호
        self._broadcast({"type": "tts_done"})
        logger.info("[headless_tts] TTS 완료")

    # ── 공개 인터페이스 ────────────────────────────────────────────────────

    def handle(self, text: str):
        """ws_server._on_chat_tts 콜백. stream_worker 스레드에서 호출됨.
        이전 TTS를 취소하고 새 데몬 스레드에서 TTS 워커를 시작.
        """
        # 세대 증가 → 진행 중인 이전 워커는 다음 청크 경계에서 스스로 중단
        with self._lock:
            self._generation += 1
            gen = self._generation

        tts_text = text[:_MAX_TEXT] if len(text) > _MAX_TEXT else text
        logger.info("[headless_tts] TTS 시작 (%d자)", len(tts_text))

        threading.Thread(
            target=self._tts_worker,
            args=(tts_text, gen),
            daemon=True,
        ).start()
