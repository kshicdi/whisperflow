"""마이크 녹음 모듈"""

import numpy as np
import sounddevice as sd
import threading
import tempfile
import wave
from pathlib import Path
from typing import Optional, Callable

from .config import config


class AudioRecorder:
    """마이크 녹음 클래스"""

    def __init__(self, on_recording_start: Optional[Callable] = None,
                 on_recording_stop: Optional[Callable[[str], None]] = None,
                 on_audio_level: Optional[Callable[[float], None]] = None):
        self.sample_rate = config.sample_rate
        self.channels = 1
        self.dtype = np.int16

        self._recording = False
        self._audio_data: list[np.ndarray] = []
        self._stream: Optional[sd.InputStream] = None
        self._lock = threading.Lock()

        self.on_recording_start = on_recording_start
        self.on_recording_stop = on_recording_stop
        self.on_audio_level = on_audio_level  # 오디오 레벨 콜백

    @property
    def is_recording(self) -> bool:
        """녹음 중 여부"""
        return self._recording

    def _audio_callback(self, indata: np.ndarray, frames: int,
                        time_info, status) -> None:
        """오디오 스트림 콜백"""
        if status:
            print(f"Audio status: {status}")

        with self._lock:
            if self._recording:
                self._audio_data.append(indata.copy())

                # 오디오 레벨 계산 (RMS)
                if self.on_audio_level:
                    # int16 -> float 변환 후 RMS 계산
                    audio_float = indata.astype(np.float32) / 32768.0
                    rms = np.sqrt(np.mean(audio_float ** 2))
                    # 0~1 범위로 정규화 (감도 조절)
                    level = min(1.0, rms * 10)
                    self.on_audio_level(level)

    def start_recording(self) -> None:
        """녹음 시작"""
        print("[AudioRecorder] start_recording 호출됨")
        if self._recording:
            print("[AudioRecorder] 이미 녹음 중")
            return

        try:
            with self._lock:
                self._audio_data = []
                self._recording = True

            self._stream = sd.InputStream(
                samplerate=self.sample_rate,
                channels=self.channels,
                dtype=self.dtype,
                callback=self._audio_callback
            )
            self._stream.start()
            print("[AudioRecorder] 스트림 시작됨")

            if self.on_recording_start:
                self.on_recording_start()
        except Exception as e:
            print(f"[AudioRecorder] 오류: {e}")
            self._recording = False

    def stop_recording(self) -> Optional[str]:
        """녹음 종료 및 파일 저장"""
        if not self._recording:
            return None

        with self._lock:
            self._recording = False

        if self._stream:
            self._stream.stop()
            self._stream.close()
            self._stream = None

        # 오디오 데이터 병합
        with self._lock:
            if not self._audio_data:
                return None
            audio = np.concatenate(self._audio_data, axis=0)
            self._audio_data = []

        # 임시 WAV 파일로 저장
        temp_file = tempfile.NamedTemporaryFile(
            suffix=".wav", delete=False
        )
        temp_path = temp_file.name
        temp_file.close()

        with wave.open(temp_path, "wb") as wf:
            wf.setnchannels(self.channels)
            wf.setsampwidth(2)  # 16-bit
            wf.setframerate(self.sample_rate)
            wf.writeframes(audio.tobytes())

        if self.on_recording_stop:
            self.on_recording_stop(temp_path)

        return temp_path

    def toggle_recording(self) -> bool:
        """녹음 토글. 녹음 중이면 True 반환"""
        if self._recording:
            self.stop_recording()
            return False
        else:
            self.start_recording()
            return True
