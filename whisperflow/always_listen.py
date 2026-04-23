"""상시 마이크 청취 모듈 - openWakeWord 기반 웨이크 워드 감지 + VAD"""

import threading
import time
from typing import Optional, Callable

import numpy as np
import sounddevice as sd
from openwakeword.model import Model


# 청취 상태 상수
_STATE_BOOT_WAIT = "boot_wait"  # 박수 2번 대기 (시스템 온라인 전)
_STATE_IDLE = "idle"            # 웨이크 워드 대기 중
_STATE_SPEECH = "speech"        # 웨이크 워드 감지 후 녹음 중


class AlwaysListen:
    """
    openWakeWord 기반 상시 마이크 모니터링 클래스.

    흐름:
      1. 대기(IDLE): openWakeWord가 16kHz 오디오를 분석하여 "Hey Jarvis" 감지 대기
      2. 감지: on_wake 콜백 호출 + 녹음 시작
      3. 녹음(SPEECH): VAD로 묵음 1.5초 이상 지속 시 녹음 종료
      4. on_speech_detected 콜백 호출 → 다시 대기 상태
    """

    def __init__(
        self,
        on_double_clap: Optional[Callable[[], None]] = None,
        on_wake: Optional[Callable[[], None]] = None,
        on_speech_detected: Optional[Callable[[np.ndarray, int], None]] = None,
        clap_threshold: float = 0.025,
        wake_threshold: float = 0.05,
        speech_threshold: float = 0.008,
        sample_rate: int = 16000,
    ):
        """
        Args:
            on_wake: 웨이크 워드 감지 시 호출되는 콜백 (인자 없음)
            on_speech_detected: 음성 녹음 완료 시 호출되는 콜백
                                 (audio: np.ndarray, sample_rate: int)
            wake_threshold: 웨이크 워드 감지 점수 임계값 (0~1)
            speech_threshold: 음성/묵음 판별 진폭 임계값 (0~1, float32 기준)
            sample_rate: 샘플레이트 (openWakeWord는 16kHz 필요)
        """
        self.on_double_clap = on_double_clap
        self.on_wake = on_wake
        self.on_speech_detected = on_speech_detected
        self.clap_threshold = clap_threshold
        self.wake_threshold = wake_threshold
        self.speech_threshold = speech_threshold
        self.sample_rate = sample_rate
        self._audio_gain = 50  # 맥북 마이크 증폭 배율

        self._running = False
        self._stream: Optional[sd.InputStream] = None
        self._lock = threading.Lock()

        # --- 상태 (박수 대기부터 시작) ---
        self._state: str = _STATE_BOOT_WAIT

        # --- 박수 감지 ---
        self._clap_prev_quiet: bool = True
        self._clap_last_peak: float = 0.0
        self._clap_fired: bool = False

        # --- 웨이크 워드 감지 쿨다운 ---
        # 감지 후 5초간 재감지 방지
        self._wake_cooldown: float = 5.0
        self._last_wake_time: float = 0.0

        # --- VAD 상태 ---
        self._silence_duration: float = 0.0
        self._silence_end: float = 1.5      # 묵음 이 시간 이상 지속 시 녹음 종료
        self._min_record_time: float = 3.0  # 최소 녹음 시간 (초) — 이 시간 전에는 묵음 무시
        self._record_start_time: float = 0.0
        self._record_buffer: list[np.ndarray] = []

        # --- openWakeWord 모델 (start() 호출 시 로드) ---
        self._oww_model: Optional[Model] = None

        # openWakeWord는 80ms(1280샘플 @ 16kHz) 청크 단위로 처리
        # sounddevice blocksize를 동일하게 맞춤
        self._block_samples: int = 1280  # 80ms @ 16kHz

    # ------------------------------------------------------------------
    # 공개 인터페이스
    # ------------------------------------------------------------------

    def start(self) -> None:
        """openWakeWord 모델을 로드하고 백그라운드 오디오 스트림을 시작한다."""
        if self._running:
            return

        print("[AlwaysListen] openWakeWord 모델 로드 중 (hey_jarvis)...")
        self._oww_model = Model(
            wakeword_models=["hey_jarvis"],
            inference_framework="onnx",
        )
        print("[AlwaysListen] 모델 로드 완료.")

        self._running = True
        self._state = _STATE_BOOT_WAIT
        self._last_wake_time = 0.0
        self._clap_fired = False

        self._stream = sd.InputStream(
            samplerate=self.sample_rate,
            channels=1,
            dtype=np.float32,
            blocksize=self._block_samples,
            callback=self._audio_callback,
        )
        self._stream.start()
        print("[AlwaysListen] 스트림 시작. Hey Jarvis를 기다리는 중...")

    def stop(self) -> None:
        """오디오 스트림을 중지한다."""
        self._running = False
        if self._stream is not None:
            self._stream.stop()
            self._stream.close()
            self._stream = None
        print("[AlwaysListen] 중지됨.")

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
        """sounddevice 실시간 오디오 콜백 (80ms 단위 호출)."""
        if not self._running:
            return
        if status:
            print(f"[AlwaysListen] stream status: {status}")

        # mono float32, shape: (frames, 1) → 1D
        audio_raw = indata[:, 0].copy()  # 원본 (VAD용)
        block_duration = frames / self.sample_rate

        # 증폭 버전 (웨이크 워드 감지용)
        audio_amplified = np.clip(audio_raw * self._audio_gain, -1.0, 1.0)
        audio_int16 = (audio_amplified * 32767).astype(np.int16)

        with self._lock:
            if self._state == _STATE_BOOT_WAIT:
                self._process_clap(audio_raw, block_duration)
            elif self._state == _STATE_IDLE:
                self._process_wake(audio_int16, audio_raw)
            elif self._state == _STATE_SPEECH:
                self._process_vad(audio_raw, block_duration)

    def _process_clap(self, audio: np.ndarray, block_duration: float) -> None:
        """박수(더블 클랩) 감지 → 시스템 온라인 후 IDLE 전환."""
        if self._clap_fired:
            return

        amplitude = float(np.max(np.abs(audio)))
        now = time.monotonic()
        is_loud = amplitude >= self.clap_threshold

        if is_loud and self._clap_prev_quiet:
            # 피크 시작
            gap = now - self._clap_last_peak
            if self._clap_last_peak > 0 and 0.15 <= gap <= 1.0:
                # 더블 클랩!
                self._clap_fired = True
                self._state = _STATE_IDLE  # 박수 후 웨이크 워드 대기로 전환
                print(f"[AlwaysListen] 더블 클랩 감지! → 웨이크 워드 대기로 전환")
                threading.Thread(target=self._fire_clap, daemon=True).start()
            else:
                self._clap_last_peak = now

        self._clap_prev_quiet = not is_loud

    def _fire_clap(self) -> None:
        """더블 클랩 콜백 호출."""
        if self.on_double_clap:
            try:
                self.on_double_clap()
            except Exception as e:
                print(f"[AlwaysListen] on_double_clap 오류: {e}")

    def _process_wake(self, audio_int16: np.ndarray, audio_f32: np.ndarray) -> None:
        """openWakeWord로 웨이크 워드 감지."""
        if self._oww_model is None:
            return

        now = time.monotonic()

        # 쿨다운 중이면 스킵
        if now - self._last_wake_time < self._wake_cooldown:
            return

        prediction = self._oww_model.predict(audio_int16)
        score = prediction.get("hey_jarvis", 0.0)

        if score >= self.wake_threshold:
            self._last_wake_time = now
            self._state = _STATE_SPEECH
            self._silence_duration = 0.0
            self._record_start_time = now
            self._record_buffer.clear()

            print(f"[AlwaysListen] 웨이크 워드 감지! (점수: {score:.3f})")
            threading.Thread(target=self._fire_wake, daemon=True).start()

    def _process_vad(self, audio: np.ndarray, block_duration: float) -> None:
        """웨이크 워드 감지 후 VAD로 묵음 구간 검출."""
        self._record_buffer.append(audio.copy())

        # 최소 녹음 시간 이전에는 묵음 체크 안 함
        elapsed = time.monotonic() - self._record_start_time
        if elapsed < self._min_record_time:
            return

        amplitude = float(np.max(np.abs(audio)))
        is_speech = amplitude >= self.speech_threshold

        if is_speech:
            self._silence_duration = 0.0
        else:
            self._silence_duration += block_duration
            if self._silence_duration >= self._silence_end:
                # 녹음 종료
                self._state = _STATE_IDLE
                recorded = np.concatenate(self._record_buffer)
                self._record_buffer.clear()
                self._silence_duration = 0.0

                print(f"[AlwaysListen] 녹음 종료. ({len(recorded) / self.sample_rate:.1f}초)")
                threading.Thread(
                    target=self._fire_speech,
                    args=(recorded, self.sample_rate),
                    daemon=True,
                ).start()

    def _fire_wake(self) -> None:
        """웨이크 워드 콜백 호출 (별도 스레드)."""
        if self.on_wake:
            try:
                self.on_wake()
            except Exception as e:
                print(f"[AlwaysListen] on_wake 오류: {e}")

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
    def on_wake():
        print("[웨이크] 자비스 감지!")

    def on_speech(audio: np.ndarray, sr: int):
        print(f"[음성] {len(audio) / sr:.1f}초 녹음됨 (샘플 수: {len(audio)})")

    listener = AlwaysListen(on_wake=on_wake, on_speech_detected=on_speech)
    listener.start()
    print("Hey Jarvis 라고 말해보세요...")
    import time
    while True:
        time.sleep(0.5)
