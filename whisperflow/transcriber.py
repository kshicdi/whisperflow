"""Whisper 음성-텍스트 변환 모듈"""

import os
import threading
from typing import Optional, Callable
from pathlib import Path

from faster_whisper import WhisperModel

from .config import config
from .history_manager import history_manager


class Transcriber:
    """faster-whisper 기반 음성 변환 클래스"""

    def __init__(self, on_transcription_start: Optional[Callable] = None,
                 on_transcription_done: Optional[Callable[[str], None]] = None,
                 on_transcription_error: Optional[Callable[[str], None]] = None):
        self._model: Optional[WhisperModel] = None
        self._model_size = config.model_size
        self._loading = False
        self._lock = threading.Lock()

        self.on_transcription_start = on_transcription_start
        self.on_transcription_done = on_transcription_done
        self.on_transcription_error = on_transcription_error

    def _ensure_model(self) -> WhisperModel:
        """모델 로드 (lazy loading)"""
        with self._lock:
            if self._model is None or self._model_size != config.model_size:
                self._model_size = config.model_size
                # CPU에서 int8 양자화로 실행 (메모리 절약)
                self._model = WhisperModel(
                    self._model_size,
                    device="cpu",
                    compute_type="int8"
                )
            return self._model

    def transcribe(self, audio_path: str) -> Optional[str]:
        """오디오 파일을 텍스트로 변환"""
        if not os.path.exists(audio_path):
            if self.on_transcription_error:
                self.on_transcription_error("오디오 파일을 찾을 수 없습니다")
            return None

        if self.on_transcription_start:
            self.on_transcription_start()

        try:
            model = self._ensure_model()

            # 언어 설정 (auto면 None으로 자동 감지)
            lang = None if config.language == "auto" else config.language

            segments, info = model.transcribe(
                audio_path,
                language=lang,
                beam_size=5,
                vad_filter=False,
            )

            # 세그먼트 텍스트 병합
            text = "".join(segment.text for segment in segments).strip()

            # 문장 끝에서 줄바꿈 추가 (. ! ?)
            import re
            text = re.sub(r'([.!?])\s*', r'\1\n', text).strip()

            # 히스토리 저장 (임시 파일 삭제 전에)
            if text:
                history_manager.save(
                    audio_path=audio_path,
                    text=text,
                    language=lang,
                    model=config.model_size
                )

            if self.on_transcription_done:
                self.on_transcription_done(text)

            return text

        except Exception as e:
            error_msg = f"변환 오류: {str(e)}"
            if self.on_transcription_error:
                self.on_transcription_error(error_msg)
            return None
        finally:
            # 임시 파일 정리
            try:
                os.unlink(audio_path)
            except OSError:
                pass

    def transcribe_async(self, audio_path: str) -> None:
        """비동기 변환 (별도 스레드)"""
        thread = threading.Thread(
            target=self.transcribe,
            args=(audio_path,),
            daemon=True
        )
        thread.start()

    def reload_model(self) -> None:
        """모델 강제 리로드"""
        with self._lock:
            self._model = None
            self._model_size = None
        print(f"[Transcriber] 모델 리로드 예약됨 (다음 변환 시 {config.model_size} 로드)")
