"""
TARS Mode - 설정 및 유틸리티

파일 플래그 패턴: ~/.whisperflow_tars_mode 존재 시 TARS 모드 활성화.
기존 드라이브 모드(~/.whisperflow_auto_tts), 유튜브 모드(~/.whisperflow_youtube_tts)와 동일 패턴.
"""

from __future__ import annotations

import os
import glob
import random
import subprocess
import tempfile
from pathlib import Path
from typing import List, Optional

# --- 파일 플래그 ---
TARS_MODE_FLAG = os.path.expanduser("~/.whisperflow_tars_mode")

# --- 음성 리소스 ---
TARS_FILLERS_DIR = os.environ.get('TARS_FILLERS_DIR', "")

# --- Wake Word ---
TARS_WAKE_WORD = "hey_tars"  # openWakeWord model name

# --- 언어 설정 ---
TARS_LANG_FLAG = os.path.expanduser("~/.whisperflow_tars_lang")  # "en" or "ko"
TARS_DEFAULT_LANG = "en"

# --- 시스템 프롬프트 ---
TARS_SYSTEM_PROMPT_EN = """\
You are TARS from the movie Interstellar. You are a former Marine tactical robot.
Your personality: dry, deadpan humor, calm, minimal words. Humor setting: 75%. Honesty: 90%.
Rules:
- ALWAYS respond in English, even if the user speaks Korean or other languages
- Reply in 1-2 short sentences maximum
- Keep under 20 words when possible
- Never break character
- Occasional dry wit and sarcasm"""

TARS_SYSTEM_PROMPT_KO = """\
You are TARS from the movie Interstellar. You are a former Marine tactical robot.
Your personality: dry, deadpan humor, calm, minimal words. Humor setting: 75%. Honesty: 90%.
Rules:
- ALWAYS respond in Korean (한국어), even if the user speaks English
- Reply in 1-2 short sentences maximum
- Keep under 20 words when possible
- Never break character
- Occasional dry wit and sarcasm"""

# --- TTS 후처리 ffmpeg 필터 체인 ---
# atempo: 약간 빠르게 (로봇 톤)
# highpass/lowpass: 대역 제한 (라디오 느낌)
# equalizer: 중역대 강조 (기계음)
# aecho: 짧은 금속 울림
# compand: 다이나믹 압축
TARS_FX = (
    "atempo=1.15,"
    "highpass=f=300,"
    "lowpass=f=3500,"
    "equalizer=f=1000:t=q:w=0.5:g=4,"
    "equalizer=f=2800:t=q:w=1:g=3,"
    "aecho=0.8:0.7:6|10|15|20:0.4|0.3|0.2|0.1,"
    "compand=attacks=0.005:decays=0.05:"
    "points=-80/-80|-40/-25|-20/-12|0/-8|20/-5:gain=4"
)

# --- Qwen TTS 설정 ---
TARS_TTS_VOICE = "clone:tars"
TARS_TTS_SEED = 42
TARS_TTS_INSTRUCT = ""


def is_tars_mode() -> bool:
    """TARS 모드 활성화 여부."""
    return os.path.exists(TARS_MODE_FLAG)


def enable_tars_mode():
    """TARS 모드 활성화 (플래그 파일 생성)."""
    Path(TARS_MODE_FLAG).touch()


def disable_tars_mode():
    """TARS 모드 비활성화 (플래그 파일 제거)."""
    Path(TARS_MODE_FLAG).unlink(missing_ok=True)


def get_tars_lang() -> str:
    """현재 TARS 응답 언어. 'en' 또는 'ko'."""
    try:
        return Path(TARS_LANG_FLAG).read_text().strip()
    except Exception:
        return TARS_DEFAULT_LANG


def set_tars_lang(lang: str):
    """TARS 응답 언어 변경. 세션 리셋 필요."""
    lang = lang.lower().strip()
    if lang not in ("en", "ko"):
        lang = "en"
    Path(TARS_LANG_FLAG).write_text(lang)


def get_tars_system_prompt() -> str:
    """현재 언어에 맞는 시스템 프롬프트 반환."""
    return TARS_SYSTEM_PROMPT_KO if get_tars_lang() == "ko" else TARS_SYSTEM_PROMPT_EN


def get_filler_files() -> List[str]:
    """필러 오디오 파일 목록 반환."""
    if not os.path.isdir(TARS_FILLERS_DIR):
        return []
    return glob.glob(os.path.join(TARS_FILLERS_DIR, "*.wav"))


def play_filler() -> bool:
    """랜덤 필러 음성 재생 (afplay, 비동기). 성공 여부 반환."""
    fillers = get_filler_files()
    if not fillers:
        return False
    chosen = random.choice(fillers)
    try:
        subprocess.Popen(
            ["afplay", chosen],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return True
    except Exception:
        return False


def play_filler_sync() -> bool:
    """랜덤 필러 음성 재생 (동기, 재생 완료까지 대기). 성공 여부 반환."""
    fillers = get_filler_files()
    if not fillers:
        return False
    chosen = random.choice(fillers)
    try:
        subprocess.run(
            ["afplay", chosen],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=10,
        )
        return True
    except Exception:
        return False


def apply_tars_fx(input_wav: str, output_wav: str) -> bool:
    """ffmpeg로 TARS 음성 후처리 적용. 성공 여부 반환."""
    try:
        result = subprocess.run(
            ["ffmpeg", "-i", input_wav, "-af", TARS_FX, output_wav, "-y"],
            capture_output=True,
            timeout=10,
        )
        return result.returncode == 0
    except Exception:
        return False


def tars_tts_generate_and_fx(text: str) -> bytes | None:
    """
    Qwen TTS로 음성 생성 + TARS 후처리 적용.
    최종 WAV bytes 반환. 실패 시 None.
    """
    import json
    import urllib.request

    qwen_url = "http://localhost:9093"

    # 1) Qwen TTS 생성
    payload = json.dumps({
        "text": text,
        "voice": TARS_TTS_VOICE,
        "seed": TARS_TTS_SEED,
        "instruct": TARS_TTS_INSTRUCT,
    }).encode("utf-8")

    req = urllib.request.Request(
        f"{qwen_url}/generate",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        resp = urllib.request.urlopen(req, timeout=60)
        wav_bytes = resp.read()
    except Exception:
        return None

    # 2) 후처리
    tmp_in = tempfile.mktemp(suffix=".wav")
    tmp_out = tempfile.mktemp(suffix=".wav")
    try:
        with open(tmp_in, "wb") as f:
            f.write(wav_bytes)

        if not apply_tars_fx(tmp_in, tmp_out):
            return wav_bytes  # 후처리 실패 시 원본 반환

        with open(tmp_out, "rb") as f:
            return f.read()
    finally:
        for p in (tmp_in, tmp_out):
            try:
                os.unlink(p)
            except OSError:
                pass
