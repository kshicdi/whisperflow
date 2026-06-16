"""상시 마이크 청취 모듈 - openWakeWord 기반 웨이크 워드 감지 + VAD"""

import subprocess
import threading
import time
from pathlib import Path
from typing import Optional, Callable

import numpy as np
import sounddevice as sd
import torch
from openwakeword.model import Model
from silero_vad import load_silero_vad


# 청취 상태 상수
_STATE_BOOT_WAIT = "boot_wait"  # 박수 2번 대기 (시스템 온라인 전)
_STATE_IDLE = "idle"            # 웨이크 워드 대기 중
_STATE_SPEECH = "speech"        # 웨이크 워드 감지 후 녹음 중
_STATE_CONV_WAIT = "conv_wait"  # 대화 모드 — 웨이크 워드 없이 음성 대기


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
        on_audio_level: Optional[Callable[[float], None]] = None,
        on_conversation_end: Optional[Callable[[], None]] = None,
        clap_threshold: float = 0.025,
        wake_threshold: float = 0.5,
        speech_threshold: float = 0.5,
        sample_rate: int = 16000,
        skip_boot_wait: bool = False,
        audio_gain: float = 20,
        conversation_timeout: float = 10.0,
    ):
        """
        Args:
            on_wake: 웨이크 워드 감지 시 호출되는 콜백 (인자 없음)
            on_speech_detected: 음성 녹음 완료 시 호출되는 콜백
                                 (audio: np.ndarray, sample_rate: int)
            wake_threshold: 웨이크 워드 감지 점수 임계값 (0~1)
            speech_threshold: Silero VAD 음성 확률 임계값 (0~1, 기본 0.5)
            sample_rate: 샘플레이트 (openWakeWord는 16kHz 필요)
        """
        self.on_double_clap = on_double_clap
        self.on_wake = on_wake
        self.on_speech_detected = on_speech_detected
        self.on_audio_level = on_audio_level
        self.on_conversation_end = on_conversation_end
        self.clap_threshold = clap_threshold
        self.wake_threshold = wake_threshold
        self.speech_threshold = speech_threshold
        self.sample_rate = sample_rate
        self._skip_boot_wait = skip_boot_wait
        self._audio_gain = audio_gain  # 마이크 증폭 배율

        self._running = False
        self._stream: Optional[sd.InputStream] = None
        self._lock = threading.Lock()
        self._muted = False  # TTS 재생 중 마이크 일시 중지

        # --- 상태 (박수 대기부터 시작) ---
        self._state: str = _STATE_BOOT_WAIT

        # --- 박수 감지 ---
        self._clap_prev_quiet: bool = True
        self._clap_last_peak: float = 0.0
        self._clap_fired: bool = False

        # --- TTS 중 박수 감지 ---
        self._tts_clap_prev_quiet: bool = True
        self._tts_clap_last_peak: float = 0.0
        self._tts_interrupted: bool = False  # 박수로 TTS 끊은 직후 플래그

        # --- 웨이크 워드 감지 쿨다운 ---
        # 감지 후 5초간 재감지 방지
        self._wake_cooldown: float = 5.0
        self._last_wake_time: float = 0.0

        # --- VAD 상태 ---
        self._silence_duration: float = 0.0
        self._silence_end: float = 3.0      # 묵음 이 시간 이상 지속 시 녹음 종료
        self._min_record_time: float = 3.0  # 최소 녹음 시간 (초) — 이 시간 전에는 묵음 무시
        self._record_start_time: float = 0.0
        self._record_buffer: list[np.ndarray] = []

        # --- Silero VAD ---
        self._silero_model = None
        self._vad_buffer = np.array([], dtype=np.float32)

        # --- 대화 모드 ---
        self._conversation_timeout = conversation_timeout  # 대화 대기 묵음 타임아웃
        self._conv_wait_start: float = 0.0  # 대화 대기 시작 시각

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

        print("[AlwaysListen] Silero VAD 모델 로드 중...")
        self._silero_model = load_silero_vad()
        print("[AlwaysListen] Silero VAD 로드 완료.")

        print("[AlwaysListen] openWakeWord 모델 로드 중 (hey_jarvis)...")
        self._oww_model = Model(
            wakeword_models=["hey_jarvis"],
            inference_framework="onnx",
        )
        print("[AlwaysListen] 모델 로드 완료.")

        # 이전 세션에서 남은 시그널 파일 정리
        Path("/tmp/whisperflow-conversation-continue").unlink(missing_ok=True)

        self._running = True
        self._state = _STATE_IDLE if self._skip_boot_wait else _STATE_BOOT_WAIT
        # 시작 직후 3초간 웨이크 워드 감지 방지 (잡음 오인식 방지)
        self._last_wake_time = time.monotonic()
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

    def mute(self) -> None:
        """TTS 재생 중 마이크 일시 중지."""
        self._muted = True

    def unmute(self) -> None:
        """TTS 재생 후 마이크 재개."""
        self._muted = False

    def reset_recording(self) -> None:
        """녹음 버퍼 비우고 타이머 리셋 (효과음 재생 후 호출)."""
        self._record_buffer.clear()
        self._record_start_time = time.monotonic()
        self._silence_duration = 0.0
        self._vad_buffer = np.array([], dtype=np.float32)
        if self._silero_model is not None:
            self._silero_model.reset_states()

    def enter_conversation_mode(self) -> None:
        """대화 모드 진입 — 웨이크 워드 없이 바로 음성 대기."""
        with self._lock:
            self._state = _STATE_CONV_WAIT
            self._conv_wait_start = time.monotonic()
            self._silence_duration = 0.0
            self._vad_buffer = np.array([], dtype=np.float32)
            if self._silero_model is not None:
                self._silero_model.reset_states()
        print(f"[AlwaysListen] 대화 모드 진입 (타임아웃: {self._conversation_timeout}초)")

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
        if not self._running or self._muted:
            return
        if status:
            print(f"[AlwaysListen] stream status: {status}")

        # mono float32, shape: (frames, 1) → 1D
        audio_raw = indata[:, 0].copy()  # 원본 (VAD용)
        block_duration = frames / self.sample_rate

        # 증폭 버전 (웨이크 워드 감지용)
        audio_amplified = np.clip(audio_raw * self._audio_gain, -1.0, 1.0)
        audio_int16 = (audio_amplified * 32767).astype(np.int16)

        try:
            # 파일 시그널 기반 conversation_continue 체크
            conv_signal = Path("/tmp/whisperflow-conversation-continue")
            if conv_signal.exists():
                conv_signal.unlink(missing_ok=True)
                # 박수로 TTS를 끊은 직후면 대화 모드 진입 무시
                if self._tts_interrupted:
                    self._tts_interrupted = False
                    print("[AlwaysListen] 파일 시그널 무시 (TTS 박수 중단 직후)")
                    return
                with self._lock:
                    self._state = _STATE_CONV_WAIT
                    self._conv_wait_start = time.monotonic()
                    self._silence_duration = 0.0
                    self._vad_buffer = np.array([], dtype=np.float32)
                    if self._silero_model is not None:
                        self._silero_model.reset_states()
                print("[AlwaysListen] 파일 시그널 → 대화 모드 진입")
                return

            with self._lock:
                if self._state == _STATE_BOOT_WAIT:
                    self._process_clap(audio_raw, block_duration)
                elif self._state == _STATE_IDLE:
                    if self._is_tts_playing():
                        # TTS 재생 중: 웨이크 워드 대신 박수로 끊기
                        self._process_tts_clap(audio_raw)
                    else:
                        self._process_wake(audio_int16, audio_raw)
                elif self._state == _STATE_SPEECH:
                    self._process_vad(audio_raw, block_duration)
                    # 녹음 중 3밴드 오디오 레벨 전송 (파티클 반응용)
                    if self.on_audio_level:
                        self._send_audio_bands(audio_raw)
                elif self._state == _STATE_CONV_WAIT:
                    self._process_conv_wait(audio_raw, block_duration)
        except Exception as e:
            # ONNX 등 추론 에러 시 콜백이 죽지 않도록 보호
            print(f"[AlwaysListen] 오디오 콜백 에러 (무시): {e}")

    def _send_audio_bands(self, audio: np.ndarray) -> None:
        """3밴드 주파수 분석 → on_audio_level 콜백으로 전달."""
        # FFT로 주파수 스펙트럼 추출
        fft = np.abs(np.fft.rfft(audio))
        freq_count = len(fft)

        # 16kHz 샘플레이트 기준: 0~8kHz 범위
        # 저음(0~300Hz), 중음(300~2kHz), 고음(2k~8kHz)
        low_end = int(freq_count * 300 / 8000)
        mid_end = int(freq_count * 2000 / 8000)

        low = float(np.mean(fft[:low_end])) if low_end > 0 else 0
        mid = float(np.mean(fft[low_end:mid_end])) if mid_end > low_end else 0
        high = float(np.mean(fft[mid_end:])) if freq_count > mid_end else 0

        # 정규화 (맥북 마이크 레벨 기준)
        gain = 80
        low = min(1.0, low * gain)
        mid = min(1.0, mid * gain)
        high = min(1.0, high * gain * 1.5)  # 고음은 약하므로 더 증폭

        # RMS 전체 레벨도 함께 전달
        rms = float(np.sqrt(np.mean(audio ** 2)))
        level = min(1.0, rms * 50)

        # 콜백에 딕셔너리로 전달
        self.on_audio_level(level, low, mid, high)

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

    def _is_tts_playing(self) -> bool:
        """TTS 재생 중인지 플래그 파일로 확인. 프로세스 없으면 잔여 파일 정리."""
        flag = Path("/tmp/whisperflow-tts-playing")
        if not flag.exists():
            return False
        # 플래그는 있지만 TTS 프로세스가 실제로 없으면 잔여 파일 → 정리
        try:
            result = subprocess.run(
                ["pgrep", "-f", "auto-tts.sh|qwen_tts_speak|afplay.*sounds"],
                capture_output=True, timeout=0.5,
            )
            if result.returncode != 0:
                # TTS 프로세스 없음 → 잔여 플래그 제거
                flag.unlink(missing_ok=True)
                print("[AlwaysListen] TTS 플래그 잔류 감지 → 제거 (프로세스 없음)")
                return False
        except Exception:
            pass
        return True

    def _is_clap_like(self, audio: np.ndarray) -> bool:
        """주파수 분석으로 박수인지 말소리인지 구분.
        박수: 고주파 비율 높음 + 짧은 임펄스
        말소리: 저주파/중주파 위주 + 여러 프레임 지속
        """
        fft = np.abs(np.fft.rfft(audio))
        freq_count = len(fft)
        # 16kHz 기준: 저음 0~1kHz, 중음 1~3kHz, 고음 3~8kHz
        low_end = int(freq_count * 1000 / 8000)
        mid_end = int(freq_count * 3000 / 8000)
        low_mid = float(np.mean(fft[:mid_end])) if mid_end > 0 else 0.001
        high = float(np.mean(fft[mid_end:])) if freq_count > mid_end else 0
        # 고주파 비율이 0.6 이상이면 박수 (말소리는 보통 0.2~0.4)
        ratio = high / (low_mid + 0.001)
        return ratio >= 0.6

    def _process_tts_clap(self, audio: np.ndarray) -> None:
        """TTS 재생 중 박수 감지 → TTS 중지 + IDLE 복귀 (녹음 아님)."""
        amplitude = float(np.max(np.abs(audio)))
        now = time.monotonic()
        tts_clap_threshold = 0.3
        is_loud = amplitude >= tts_clap_threshold

        if is_loud and self._tts_clap_prev_quiet:
            # 주파수 분석으로 박수인지 확인
            if self._is_clap_like(audio):
                gap = now - self._tts_clap_last_peak
                if self._tts_clap_last_peak > 0 and 0.1 <= gap <= 0.7:
                    # 더블 클랩 감지 → TTS만 중지, IDLE로 복귀
                    self._tts_clap_last_peak = 0.0
                    self._tts_clap_loud_frames = 0
                    print("[AlwaysListen] TTS 중 더블 클랩 감지! → TTS 중지")
                    self._tts_interrupted = True
                    Path("/tmp/whisperflow-conversation-continue").unlink(missing_ok=True)
                    Path("/tmp/whisperflow-tts-playing").unlink(missing_ok=True)
                    threading.Thread(target=self._kill_tts_processes, daemon=True).start()
                else:
                    self._tts_clap_last_peak = now

        self._tts_clap_prev_quiet = not is_loud

    def _kill_tts_processes(self) -> None:
        """TTS 프로세스를 강제 종료 + 인터럽트 응답음 재생."""
        import random
        for pattern in ["afplay", "qwen_tts_speak"]:
            try:
                subprocess.run(["pkill", "-9", "-f", pattern], capture_output=True, timeout=1)
            except Exception:
                pass
        try:
            subprocess.run(["pkill", "-9", "say"], capture_output=True, timeout=1)
        except Exception:
            pass
        print("[AlwaysListen] TTS 프로세스 강제 종료 완료")
        # 랜덤 인터럽트 응답음 재생
        import os
        sounds_dir = os.path.join(os.path.dirname(__file__), "static", "sounds")
        interrupt_files = [f for f in os.listdir(sounds_dir) if f.startswith("interrupt_") and f.endswith(".wav")]
        if interrupt_files:
            chosen = os.path.join(sounds_dir, random.choice(interrupt_files))
            try:
                subprocess.Popen(["afplay", "-r", "1.4", chosen]).wait()
            except Exception:
                pass

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
            self._vad_buffer = np.array([], dtype=np.float32)
            if self._silero_model is not None:
                self._silero_model.reset_states()

            # 감지 후 모델 리셋 (잔류 점수 제거)
            self._oww_model.reset()
            print(f"[AlwaysListen] 웨이크 워드 감지! (점수: {score:.3f})")
            threading.Thread(target=self._fire_wake, daemon=True).start()

    def _process_vad(self, audio: np.ndarray, block_duration: float) -> None:
        """Silero VAD로 음성/비음성 판별."""
        self._record_buffer.append(audio.copy())

        elapsed = time.monotonic() - self._record_start_time

        # 최소 녹음 시간 이전에는 묵음 체크 안 함
        if elapsed < self._min_record_time:
            return

        # Silero VAD는 512 샘플 단위 → 80ms(1280샘플)를 512씩 분할
        self._vad_buffer = np.concatenate([self._vad_buffer, audio]) if len(self._vad_buffer) > 0 else audio.copy()

        is_speech = False
        while len(self._vad_buffer) >= 512:
            chunk = self._vad_buffer[:512]
            self._vad_buffer = self._vad_buffer[512:]
            tensor = torch.from_numpy(chunk)
            speech_prob = self._silero_model(tensor, self.sample_rate).item()
            if speech_prob >= self.speech_threshold:
                is_speech = True

        # 디버그: 1초마다 VAD 상태 출력
        if int(elapsed * 10) % 10 == 0:
            print(f"[VAD-Silero] {elapsed:.1f}s speech={is_speech} silence={self._silence_duration:.1f}s")

        if is_speech:
            self._silence_duration = 0.0
        else:
            self._silence_duration += block_duration
            if self._silence_duration >= self._silence_end:
                # 녹음 종료 → 웨이크 워드 대기로 복귀
                self._state = _STATE_IDLE
                recorded = np.concatenate(self._record_buffer)
                self._record_buffer.clear()
                self._silence_duration = 0.0
                self._vad_buffer = np.array([], dtype=np.float32)
                if self._silero_model is not None:
                    self._silero_model.reset_states()
                # openWakeWord 내부 상태 리셋 (이전 감지 점수 잔류 방지)
                if self._oww_model is not None:
                    self._oww_model.reset()

                print(f"[AlwaysListen] 녹음 종료. ({len(recorded) / self.sample_rate:.1f}초)")
                threading.Thread(
                    target=self._fire_speech,
                    args=(recorded, self.sample_rate),
                    daemon=True,
                ).start()

    def _process_conv_wait(self, audio: np.ndarray, block_duration: float) -> None:
        """대화 모드: Silero VAD로 음성 감지 시 녹음 시작, 타임아웃 시 대화 종료."""
        elapsed = time.monotonic() - self._conv_wait_start

        # Silero VAD로 음성 감지
        self._vad_buffer = np.concatenate([self._vad_buffer, audio]) if len(self._vad_buffer) > 0 else audio.copy()

        is_speech = False
        while len(self._vad_buffer) >= 512:
            chunk = self._vad_buffer[:512]
            self._vad_buffer = self._vad_buffer[512:]
            tensor = torch.from_numpy(chunk)
            speech_prob = self._silero_model(tensor, self.sample_rate).item()
            if speech_prob >= self.speech_threshold:
                is_speech = True

        if is_speech:
            # 음성 감지 → 녹음 모드로 전환 (웨이크 워드 스킵)
            self._state = _STATE_SPEECH
            self._silence_duration = 0.0
            self._record_start_time = time.monotonic()
            self._record_buffer.clear()
            self._record_buffer.append(audio.copy())
            self._vad_buffer = np.array([], dtype=np.float32)
            if self._silero_model is not None:
                self._silero_model.reset_states()
            print(f"[AlwaysListen] 대화 모드 → 음성 감지! 녹음 시작")
            return

        # 타임아웃 체크
        if elapsed >= self._conversation_timeout:
            self._state = _STATE_IDLE
            self._vad_buffer = np.array([], dtype=np.float32)
            if self._silero_model is not None:
                self._silero_model.reset_states()
            print(f"[AlwaysListen] 대화 모드 타임아웃 ({self._conversation_timeout}초) → 웨이크 워드 대기")
            threading.Thread(target=self._fire_conversation_end, daemon=True).start()

    def _fire_conversation_end(self) -> None:
        """대화 종료 콜백 호출."""
        if self.on_conversation_end:
            try:
                self.on_conversation_end()
            except Exception as e:
                print(f"[AlwaysListen] on_conversation_end 오류: {e}")

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
