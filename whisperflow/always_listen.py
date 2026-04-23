"""상시 마이크 청취 모듈 - 박수 감지 및 음성 활동 감지(VAD)"""

import threading
import time
from collections import deque
from typing import Optional, Callable

import numpy as np
import sounddevice as sd


# 청취 상태 상수
_STATE_IDLE = "idle"          # 조용한 상태
_STATE_SPEECH = "speech"      # 음성 감지 중 (녹음 중)
_STATE_SILENCE = "silence"    # 음성 후 묵음 대기 (녹음 종료 판단 중)


class AlwaysListen:
    """
    상시 마이크 모니터링 클래스.

    두 가지 이벤트를 감지한다:
    1. 더블 클랩(박수 2번): 짧은 피크 2개가 0.2~0.8초 간격으로 발생
    2. 음성 감지(VAD): 일정 시간 이상 소리가 지속되면 녹음 시작,
       1초 이상 조용하면 녹음 종료 후 콜백 호출
    """

    def __init__(
        self,
        on_double_clap: Optional[Callable[[], None]] = None,
        on_speech_detected: Optional[Callable[[np.ndarray, int], None]] = None,
        clap_threshold: float = 0.025,
        speech_threshold: float = 0.008,
        sample_rate: int = 48000,
    ):
        """
        Args:
            on_double_clap: 더블 클랩 감지 시 호출되는 콜백 (인자 없음)
            on_speech_detected: 음성 감지 완료 시 호출되는 콜백
                                 (audio_data: np.ndarray, sample_rate: int)
            clap_threshold: 박수 감지 진폭 임계값 (0~1, float32 기준)
            speech_threshold: 음성 감지 진폭 임계값 (0~1, float32 기준)
            sample_rate: 오디오 샘플레이트 (Hz)
        """
        self.on_double_clap = on_double_clap
        self.on_speech_detected = on_speech_detected
        self.clap_threshold = clap_threshold
        self.speech_threshold = speech_threshold
        self.sample_rate = sample_rate

        self._running = False
        self._stream: Optional[sd.InputStream] = None
        self._lock = threading.Lock()

        # --- 박수 감지 상태 ---
        # 마지막 피크 시각 (time.monotonic 기준)
        self._last_peak_time: Optional[float] = None
        # 직전 프레임이 조용했는지 여부 (급격한 상승 판별용)
        self._prev_quiet: bool = True
        # 현재 피크가 시작된 시각 (길이 측정용)
        self._peak_start_time: Optional[float] = None

        # --- VAD 상태 ---
        self._vad_state: str = _STATE_IDLE
        # 소리가 speech_threshold 초과한 연속 구간 누적 시간 (초)
        self._speech_duration: float = 0.0
        # 묵음이 지속된 시간 (초)
        self._silence_duration: float = 0.0
        # 녹음 버퍼 (float32 배열 리스트)
        self._record_buffer: list[np.ndarray] = []

        # VAD 파라미터 (초)
        self._speech_onset = 0.3   # 이 시간 이상 소리 지속 시 녹음 시작
        self._silence_end = 1.0    # 이 시간 이상 묵음 지속 시 녹음 종료

        # 박수 파라미터
        self._clap_min_gap = 0.15  # 두 번째 클랩까지 최소 간격 (초)
        self._clap_max_gap = 1.0   # 두 번째 클랩까지 최대 간격 (초)
        self._clap_max_dur = 0.15  # 클랩 피크의 최대 지속 시간 (150ms)
        self._clap_cooldown = 10.0  # 더블 클랩 후 쿨다운 시간 (초)
        self._last_clap_fired: float = 0  # 마지막 더블 클랩 발동 시각

    # ------------------------------------------------------------------
    # 공개 인터페이스
    # ------------------------------------------------------------------

    def start(self) -> None:
        """백그라운드 오디오 스트림을 시작한다."""
        if self._running:
            return
        self._running = True
        self._stream = sd.InputStream(
            samplerate=self.sample_rate,
            channels=1,
            dtype=np.float32,
            blocksize=int(self.sample_rate * 0.02),  # 20ms 블록
            callback=self._audio_callback,
        )
        self._stream.start()

    def stop(self) -> None:
        """오디오 스트림을 중지한다."""
        self._running = False
        if self._stream is not None:
            self._stream.stop()
            self._stream.close()
            self._stream = None

    # ------------------------------------------------------------------
    # 내부 구현
    # ------------------------------------------------------------------

    def _audio_callback(
        self,
        indata: np.ndarray,
        frames: int,
        time_info,
        status,
    ) -> None:
        """sounddevice 실시간 오디오 콜백 (20ms 단위 호출)."""
        if not self._running:
            return
        if status:
            print(f"[AlwaysListen] stream status: {status}")

        # mono float32, shape: (frames, 1) → 1D
        audio = indata[:, 0]
        amplitude = float(np.max(np.abs(audio)))
        block_duration = frames / self.sample_rate  # 이 블록의 시간(초)
        now = time.monotonic()

        with self._lock:
            self._process_clap(amplitude, block_duration, now)
            self._process_vad(audio, amplitude, block_duration)

    def _process_clap(
        self,
        amplitude: float,
        block_duration: float,
        now: float,
    ) -> None:
        """박수(더블 클랩) 감지 로직."""
        # 쿨다운 중이면 무시
        if (now - self._last_clap_fired) < self._clap_cooldown:
            return

        is_peak = amplitude >= self.clap_threshold

        if is_peak:
            if self._prev_quiet:
                # 새 피크 시작 — 직전이 조용했으므로 급격한 상승
                self._peak_start_time = now

                if self._last_peak_time is not None:
                    gap = now - self._last_peak_time
                    if self._clap_min_gap <= gap <= self._clap_max_gap:
                        # 유효한 더블 클랩
                        self._last_peak_time = None
                        self._peak_start_time = None
                        self._prev_quiet = False
                        # 쿨다운 시작 + 콜백은 락 밖에서 호출
                        self._last_clap_fired = now
                        threading.Thread(
                            target=self._fire_clap, daemon=True
                        ).start()
                        return
                    else:
                        # 간격이 유효 범위 밖 → 첫 번째 클랩으로 갱신
                        self._last_peak_time = now
                else:
                    self._last_peak_time = now

            else:
                # 피크가 계속 이어지는 중
                if (
                    self._peak_start_time is not None
                    and (now - self._peak_start_time) > self._clap_max_dur
                ):
                    # 50ms 넘게 지속 → 클랩이 아닌 긴 소리로 판단, 무효화
                    self._last_peak_time = None
                    self._peak_start_time = None

            self._prev_quiet = False
        else:
            self._prev_quiet = True
            self._peak_start_time = None

            # 마지막 클랩으로부터 최대 간격을 초과하면 리셋
            if (
                self._last_peak_time is not None
                and (now - self._last_peak_time) > self._clap_max_gap
            ):
                self._last_peak_time = None

    def _fire_clap(self) -> None:
        """더블 클랩 콜백 호출 (별도 스레드)."""
        if self.on_double_clap:
            try:
                self.on_double_clap()
            except Exception as e:
                print(f"[AlwaysListen] on_double_clap 오류: {e}")

    def _process_vad(
        self,
        audio: np.ndarray,
        amplitude: float,
        block_duration: float,
    ) -> None:
        """음성 활동 감지(VAD) 로직."""
        is_speech = amplitude >= self.speech_threshold

        if self._vad_state == _STATE_IDLE:
            if is_speech:
                self._speech_duration += block_duration
                self._record_buffer.append(audio.copy())
                if self._speech_duration >= self._speech_onset:
                    self._vad_state = _STATE_SPEECH
                    self._silence_duration = 0.0
            else:
                # 조용한 순간에 누적 초기화
                self._speech_duration = 0.0
                self._record_buffer.clear()

        elif self._vad_state == _STATE_SPEECH:
            self._record_buffer.append(audio.copy())
            if is_speech:
                self._silence_duration = 0.0
            else:
                self._silence_duration += block_duration
                if self._silence_duration >= self._silence_end:
                    # 녹음 종료
                    self._vad_state = _STATE_IDLE
                    recorded = np.concatenate(self._record_buffer)
                    self._record_buffer.clear()
                    self._speech_duration = 0.0
                    self._silence_duration = 0.0
                    threading.Thread(
                        target=self._fire_speech,
                        args=(recorded, self.sample_rate),
                        daemon=True,
                    ).start()

    def _fire_speech(self, audio: np.ndarray, sample_rate: int) -> None:
        """음성 감지 콜백 호출 (별도 스레드)."""
        if self.on_speech_detected:
            try:
                self.on_speech_detected(audio, sample_rate)
            except Exception as e:
                print(f"[AlwaysListen] on_speech_detected 오류: {e}")


# ------------------------------------------------------------------
# Standalone 테스트
# ------------------------------------------------------------------
if __name__ == "__main__":
    def on_clap():
        print("[박수] 더블 클랩 감지!")

    def on_speech(audio: np.ndarray, sr: int):
        print(f"[음성] {len(audio) / sr:.1f}초 녹음됨 (샘플 수: {len(audio)})")

    listener = AlwaysListen(
        on_double_clap=on_clap,
        on_speech_detected=on_speech,
    )
    listener.start()
    print("모니터링 시작. Enter를 누르면 종료합니다.")
    input()
    listener.stop()
    print("종료.")
