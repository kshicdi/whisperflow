"""TTS (Text-to-Speech) 모듈 - macOS NSSpeechSynthesizer 사용"""

import subprocess
import threading
import unicodedata
from typing import Optional

try:
    from AppKit import NSSpeechSynthesizer
    HAS_APPKIT = True
except ImportError:
    HAS_APPKIT = False


def detect_language(text: str) -> str:
    """텍스트의 주요 언어를 유니코드 범위로 감지

    Returns:
        언어 코드: "ko", "ja", "zh", "en"
    """
    if not text:
        return "en"

    counts = {"ko": 0, "ja": 0, "zh": 0, "en": 0}

    for char in text:
        cp = ord(char)
        # 한글 (가-힣, ㄱ-ㅎ, ㅏ-ㅣ)
        if (0xAC00 <= cp <= 0xD7A3 or
                0x3131 <= cp <= 0x318E):
            counts["ko"] += 1
        # 히라가나/카타카나
        elif (0x3040 <= cp <= 0x309F or
              0x30A0 <= cp <= 0x30FF):
            counts["ja"] += 1
        # CJK 통합 한자 (한국어/일본어/중국어 공용)
        elif 0x4E00 <= cp <= 0x9FFF:
            # 한글이나 히라가나/카타카나가 같이 있으면 해당 언어로 분류
            # 단독이면 중국어로 분류
            counts["zh"] += 1
        elif char.isascii() and char.isalpha():
            counts["en"] += 1

    # 한글/일본어가 있으면 CJK 한자는 해당 언어에 포함
    if counts["ko"] > 0 and counts["zh"] > 0:
        counts["ko"] += counts["zh"]
        counts["zh"] = 0
    elif counts["ja"] > 0 and counts["zh"] > 0:
        counts["ja"] += counts["zh"]
        counts["zh"] = 0

    # 가장 많은 언어 반환
    max_lang = max(counts, key=counts.get)
    if counts[max_lang] == 0:
        return "en"
    return max_lang


# 언어별 기본 음성 (macOS 내장)
DEFAULT_VOICES = {
    "ko": "Yuna",        # 한국어
    "en": "Samantha",    # 영어
    "ja": "Kyoko",       # 일본어
    "zh": "Ting-Ting",   # 중국어
}


class TTSReader:
    """macOS NSSpeechSynthesizer를 사용한 TTS 리더

    NSSpeechSynthesizer가 사용 불가능하면 `say` 명령어로 fallback.
    """

    def __init__(self):
        self._synth: Optional[NSSpeechSynthesizer] = None
        self._say_process: Optional[subprocess.Popen] = None
        self._lock = threading.Lock()
        self._rate: int = 200  # words per minute
        self._speaking = False

        if HAS_APPKIT:
            self._synth = NSSpeechSynthesizer.alloc().init()

    @property
    def is_speaking(self) -> bool:
        """현재 읽기 중인지 반환"""
        with self._lock:
            if HAS_APPKIT and self._synth:
                return self._synth.isSpeaking()
            return self._speaking

    def set_rate(self, rate: int) -> None:
        """읽기 속도 설정 (words per minute)"""
        self._rate = rate
        if HAS_APPKIT and self._synth:
            self._synth.setRate_(float(rate))

    def set_voice(self, voice_name: str) -> None:
        """음성 변경"""
        if HAS_APPKIT and self._synth:
            # macOS 음성 식별자 형식: com.apple.speech.synthesis.voice.VoiceName
            # 또는 짧은 이름으로 검색
            voices = NSSpeechSynthesizer.availableVoices()
            for voice_id in voices:
                if voice_name.lower() in voice_id.lower():
                    self._synth.setVoice_(voice_id)
                    return
            print(f"[TTS] 음성을 찾을 수 없음: {voice_name}")

    def _select_voice_for_language(self, lang: str) -> Optional[str]:
        """언어에 맞는 음성을 선택"""
        voice_name = DEFAULT_VOICES.get(lang, DEFAULT_VOICES["en"])

        if HAS_APPKIT:
            voices = NSSpeechSynthesizer.availableVoices()
            # 정확한 이름 매칭 시도
            for voice_id in voices:
                if voice_name.lower() in voice_id.lower():
                    return voice_id
            # 언어 코드로 매칭 시도
            lang_prefix = {
                "ko": "ko", "en": "en", "ja": "ja", "zh": "zh"
            }.get(lang, "en")
            for voice_id in voices:
                if f".{lang_prefix}" in voice_id.lower() or f".{lang_prefix}-" in voice_id.lower():
                    return voice_id

        return voice_name  # fallback에서는 이름 그대로 사용

    def speak(self, text: str) -> None:
        """텍스트를 음성으로 읽기 (비동기)

        언어를 자동 감지하여 적절한 음성을 선택한다.
        이미 읽기 중이면 중지 후 새 텍스트를 읽는다.
        """
        if not text or not text.strip():
            return

        # 이미 읽기 중이면 중지
        if self.is_speaking:
            self.stop()

        lang = detect_language(text)
        voice = self._select_voice_for_language(lang)
        print(f"[TTS] 언어: {lang}, 음성: {voice}, 텍스트: {text[:50]}...")

        if HAS_APPKIT and self._synth:
            self._speak_with_native(text, voice)
        else:
            self._speak_with_say(text, voice, lang)

    def _speak_with_native(self, text: str, voice_id: str) -> None:
        """NSSpeechSynthesizer로 읽기"""
        if voice_id:
            self._synth.setVoice_(voice_id)
        self._synth.setRate_(float(self._rate))
        self._synth.startSpeakingString_(text)

    def _speak_with_say(self, text: str, voice_name: str, lang: str) -> None:
        """say 명령어로 읽기 (fallback)"""
        def _run():
            with self._lock:
                self._speaking = True
            try:
                cmd = ["say"]
                if voice_name:
                    cmd.extend(["-v", voice_name])
                cmd.extend(["-r", str(self._rate)])
                cmd.append(text)

                self._say_process = subprocess.Popen(
                    cmd,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL
                )
                self._say_process.wait()
            except Exception as e:
                print(f"[TTS] say 오류: {e}")
            finally:
                with self._lock:
                    self._speaking = False
                    self._say_process = None

        threading.Thread(target=_run, daemon=True).start()

    def stop(self) -> None:
        """읽기 중지"""
        if HAS_APPKIT and self._synth:
            self._synth.stopSpeaking()

        with self._lock:
            if self._say_process and self._say_process.poll() is None:
                self._say_process.terminate()
                self._say_process = None
            self._speaking = False

        print("[TTS] 읽기 중지")


# 전역 TTS 인스턴스
tts_reader = TTSReader()
