"""
gesture_control.py - MediaPipe 기반 손 제스처 컨트롤 모듈.

맥북 카메라에서 실시간으로 손을 인식하고 제스처를 분류하여
JARVIS WebSocket 서버로 액션을 전송한다.

■ 한 손 모드 (포즈 기반 — 손가락 모양으로 분류):
    PALM_OPEN  (손바닥 펴기)  → zoom_in
    FIST       (주먹)         → zoom_out
    OK_SIGN    (엄지+검지 원) → remote_record toggle
    PEACE      (피스)         → screenshot_analyze

■ 두 손 모드 (모션 기반 — wrist 간 거리 변화로 분류):
    SPREAD     (두 손 벌리기) → spread_open
    GATHER     (두 손 모으기) → gather_close

Usage (standalone):
    python -m whisperflow.gesture_control
    python -m whisperflow.gesture_control --camera 1
    python -m whisperflow.gesture_control --test          # WS 없이 테스트
"""

import asyncio
import json
import math
import os
import threading
import time

# 손가락 인덱스 상수 (MediaPipe Hands landmarks)
# 각 손가락: [MCP, PIP, DIP, TIP]
FINGER_INDICES = {
    "thumb":  {"cmc": 1,  "mcp": 2,  "ip": 3,  "tip": 4},
    "index":  {"mcp": 5,  "pip": 6,  "dip": 7,  "tip": 8},
    "middle": {"mcp": 9,  "pip": 10, "dip": 11, "tip": 12},
    "ring":   {"mcp": 13, "pip": 14, "dip": 15, "tip": 16},
    "pinky":  {"mcp": 17, "pip": 18, "dip": 19, "tip": 20},
}

# 제스처 → WebSocket 액션 매핑
GESTURE_ACTIONS = {
    "PALM_OPEN": {"type": "ui_action",     "value": "zoom_in"},
    "FIST":      {"type": "ui_action",     "value": "zoom_out"},
    "OK_SIGN":   {"type": "remote_record", "value": "toggle"},
    "PEACE":     {"type": "ui_action",     "value": "screenshot_analyze"},
    "SPREAD":    {"type": "ui_action",     "value": "spread_open"},
    "GATHER":    {"type": "ui_action",     "value": "gather_close"},
}

# OK 사인 판정: 엄지 끝과 검지 끝의 최대 거리 (정규화 좌표 기준)
OK_SIGN_THRESHOLD = 0.07

# 두 손 모션 판정: wrist 간 거리 변화량 기준 (정규화 좌표 기준)
TWO_HAND_DIST_HISTORY_SIZE = 10   # 거리 히스토리 버퍼 크기 (~0.33초 @ 30fps)
TWO_HAND_SPREAD_DELTA = 0.20      # 이 만큼 거리가 증가하면 SPREAD
TWO_HAND_GATHER_DELTA = 0.20      # 이 만큼 거리가 감소하면 GATHER
TWO_HAND_COOLDOWN = 1.0           # 발동 후 재발동 대기 시간 (초)

# 제스처 유지 시간 (초) — 이 시간 이상 같은 제스처가 유지되어야 액션 발동
GESTURE_HOLD_SECONDS = 0.5

# MediaPipe Tasks API 모델 경로
_MODEL_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "models", "hand_landmarker.task")

# 랜드마크 연결 (mp.solutions.hands.HAND_CONNECTIONS 대체)
HAND_CONNECTIONS = [
    (0, 1), (1, 2), (2, 3), (3, 4),          # thumb
    (0, 5), (5, 6), (6, 7), (7, 8),          # index
    (0, 9), (9, 10), (10, 11), (11, 12),     # middle
    (0, 13), (13, 14), (14, 15), (15, 16),   # ring
    (0, 17), (17, 18), (18, 19), (19, 20),   # pinky
    (5, 9), (9, 13), (13, 17),               # palm
]


def _draw_hand_landmarks(frame, hand_landmarks, h, w):
    """OpenCV로 랜드마크를 직접 그린다 (mp.solutions.drawing_utils 대체)."""
    import cv2
    for lm in hand_landmarks:
        cx, cy = int(lm.x * w), int(lm.y * h)
        cv2.circle(frame, (cx, cy), 4, (0, 255, 0), -1)
    for start, end in HAND_CONNECTIONS:
        s = hand_landmarks[start]
        e = hand_landmarks[end]
        cv2.line(frame, (int(s.x * w), int(s.y * h)), (int(e.x * w), int(e.y * h)), (0, 200, 0), 2)


class GestureControl:
    def __init__(self, camera_index=1, ws_url="ws://localhost:8767", test_mode=False):
        self.camera_index = camera_index
        self.ws_url = ws_url
        self.test_mode = test_mode
        self._running = False
        self._thread = None
        self._loop = None
        self._cap = None
        self._cap_lock = threading.Lock()

        # 제스처 유지 추적
        self._current_gesture: str | None = None
        self._gesture_start_time: float = 0.0

        # 중복 발동 방지: 마지막으로 발동한 제스처
        self._last_fired_gesture: str | None = None

        # 두 손 모션 추적
        self._two_hand_dist_history: list[float] = []  # wrist 간 거리 히스토리
        self._two_hand_last_fire: float = 0.0          # 마지막 발동 시각

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def start(self):
        """백그라운드 스레드에서 제스처 인식 루프를 시작한다."""
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()

    def stop(self):
        """제스처 인식을 중단하고 카메라를 해제한다."""
        self._running = False
        with self._cap_lock:
            if self._cap is not None:
                self._cap.release()
                self._cap = None
        if self._loop and self._loop.is_running():
            self._loop.call_soon_threadsafe(self._loop.stop)
        if self._thread:
            self._thread.join(timeout=5)
            self._thread = None

    # ------------------------------------------------------------------
    # Internal — thread / loop
    # ------------------------------------------------------------------

    def _run_loop(self):
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._gesture_loop())
        except Exception as e:
            print(f"[Gesture] 루프 에러: {e}", flush=True)
            import traceback
            traceback.print_exc()
        finally:
            self._loop.close()

    async def _gesture_loop(self):
        """카메라 캡처 + MediaPipe 인식 + WebSocket 전송 메인 루프."""
        try:
            import cv2
        except ImportError:
            print("[Gesture] opencv-python이 설치되지 않았습니다. pip install opencv-python", flush=True)
            return

        try:
            import mediapipe as mp
            from mediapipe.tasks.python.vision import HandLandmarker, HandLandmarkerOptions, RunningMode
            from mediapipe.tasks.python import BaseOptions
        except ImportError:
            print("[Gesture] mediapipe가 설치되지 않았습니다. pip install mediapipe", flush=True)
            return

        try:
            import websockets
        except ImportError:
            print("[Gesture] websockets가 설치되지 않았습니다. pip install websockets", flush=True)
            return

        _standalone = _is_standalone()

        options = HandLandmarkerOptions(
            base_options=BaseOptions(model_asset_path=_MODEL_PATH),
            running_mode=RunningMode.VIDEO,
            num_hands=2,
            min_hand_detection_confidence=0.7,
            min_tracking_confidence=0.6,
        )
        landmarker = HandLandmarker.create_from_options(options)
        t0 = time.monotonic()

        while self._running:
            # 카메라 열기
            with self._cap_lock:
                if self._cap is None or not self._cap.isOpened():
                    if _standalone:
                        print(f"[Gesture] 카메라 {self.camera_index} 열기 시도...", flush=True)
                    self._cap = cv2.VideoCapture(self.camera_index)
                    if not self._cap.isOpened():
                        if _standalone:
                            print(f"[Gesture] 카메라 {self.camera_index}를 열 수 없습니다. 재시도...", flush=True)
                        self._cap = None
                        await asyncio.sleep(3)
                        continue
                    if _standalone:
                        print(f"[Gesture] 카메라 {self.camera_index} 연결됨.", flush=True)
            cap = self._cap

            # WebSocket 연결
            try:
                async with websockets.connect(self.ws_url) as ws:
                    if _standalone:
                        print(f"[Gesture] WebSocket connected to {self.ws_url}", flush=True)
                        print("[Gesture] 제스처 인식 중... (Ctrl+C to stop)", flush=True)

                    while self._running:
                        loop = asyncio.get_event_loop()
                        ret, frame = await loop.run_in_executor(None, cap.read)

                        if not ret or frame is None:
                            if _standalone:
                                print("[Gesture] 프레임 읽기 실패. 카메라 재연결 시도...", flush=True)
                            with self._cap_lock:
                                if self._cap is not None:
                                    self._cap.release()
                                    self._cap = None
                            break

                        # MediaPipe Tasks API: RGB 이미지 + timestamp_ms
                        frame_rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                        timestamp_ms = int((time.monotonic() - t0) * 1000)
                        mp_image = mp.Image(image_format=mp.ImageFormat.SRGB, data=frame_rgb)
                        results = landmarker.detect_for_video(mp_image, timestamp_ms)

                        gesture = self._detect_gesture(results)

                        # 제스처 유지 시간 추적 및 액션 발동
                        now = time.monotonic()
                        if gesture is not None:
                            if gesture != self._current_gesture:
                                # 새 제스처 시작
                                self._current_gesture = gesture
                                self._gesture_start_time = now
                            else:
                                # 같은 제스처 유지 중
                                held = now - self._gesture_start_time
                                if (held >= GESTURE_HOLD_SECONDS
                                        and gesture != self._last_fired_gesture):
                                    # 액션 발동
                                    action = GESTURE_ACTIONS.get(gesture)
                                    if action:
                                        payload = json.dumps(action)
                                        await ws.send(payload)
                                        print(f"[Gesture] {gesture} → {action['value']}", flush=True)
                                    self._last_fired_gesture = gesture
                        else:
                            # 손이 감지되지 않으면 상태 초기화
                            if self._current_gesture is not None:
                                self._current_gesture = None
                                self._gesture_start_time = 0.0
                                # 손이 사라지면 last_fired 초기화 → 다음 등장 시 재발동 가능
                                self._last_fired_gesture = None

                        await asyncio.sleep(0.033)  # ~30fps

            except (OSError, Exception) as e:
                if _standalone:
                    print(f"[Gesture] WebSocket 연결 실패: {e}. 재연결 중...", flush=True)
                await asyncio.sleep(2)

        # 종료 시 정리
        landmarker.close()
        with self._cap_lock:
            if self._cap is not None and self._cap.isOpened():
                self._cap.release()
                if _standalone:
                    print("[Gesture] 카메라 해제.", flush=True)
                self._cap = None

    def run_test(self):
        """테스트 모드: 메인 스레드에서 직접 실행. WebSocket 없이 카메라 + 제스처 인식 + OpenCV 시각화."""
        self._running = True
        try:
            import cv2
        except ImportError:
            print("[Gesture] opencv-python이 설치되지 않았습니다. pip install opencv-python", flush=True)
            return

        try:
            import mediapipe as mp
            from mediapipe.tasks.python.vision import HandLandmarker, HandLandmarkerOptions, RunningMode
            from mediapipe.tasks.python import BaseOptions
        except ImportError:
            print("[Gesture] mediapipe가 설치되지 않았습니다. pip install mediapipe", flush=True)
            return

        options = HandLandmarkerOptions(
            base_options=BaseOptions(model_asset_path=_MODEL_PATH),
            running_mode=RunningMode.VIDEO,
            num_hands=2,
            min_hand_detection_confidence=0.7,
            min_tracking_confidence=0.6,
        )
        landmarker = HandLandmarker.create_from_options(options)
        t0 = time.monotonic()

        print(f"[Test] 카메라 {self.camera_index} 열기 시도...", flush=True)
        cap = cv2.VideoCapture(self.camera_index)
        if not cap.isOpened():
            print(f"[Test] 카메라 {self.camera_index}를 열 수 없습니다.", flush=True)
            return
        print(f"[Test] 카메라 연결됨. 'q' 키로 종료.", flush=True)
        print("[Test] 제스처: PALM_OPEN / FIST / OK_SIGN / PEACE / SPREAD", flush=True)

        last_printed_gesture = None

        while self._running:
            ret, frame = cap.read()
            if not ret or frame is None:
                print("[Test] 프레임 읽기 실패.", flush=True)
                break

            frame_rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            timestamp_ms = int((time.monotonic() - t0) * 1000)
            mp_image = mp.Image(image_format=mp.ImageFormat.SRGB, data=frame_rgb)
            results = landmarker.detect_for_video(mp_image, timestamp_ms)

            # 랜드마크 시각화 (OpenCV 직접 그리기)
            h, w = frame.shape[:2]
            if results.hand_landmarks:
                for hand_lm in results.hand_landmarks:
                    _draw_hand_landmarks(frame, hand_lm, h, w)

            gesture = self._detect_gesture(results)

            # 제스처 변경 시에만 출력
            if gesture != last_printed_gesture:
                if gesture is not None:
                    action = GESTURE_ACTIONS.get(gesture, {})
                    value = action.get("value", "?")
                    print(f"[Test] 제스처: {gesture} → {value}", flush=True)
                else:
                    print("[Test] 제스처: (없음)", flush=True)
                last_printed_gesture = gesture

            # 프레임에 제스처 텍스트 표시
            label = gesture if gesture else "None"
            cv2.putText(frame, f"Gesture: {label}", (10, 40),
                        cv2.FONT_HERSHEY_SIMPLEX, 1.2, (0, 255, 0), 3)

            # 두 손 모드 상태 표시
            num_hands = len(results.hand_landmarks) if results.hand_landmarks else 0
            if num_hands >= 2:
                hist_len = len(self._two_hand_dist_history)
                cv2.putText(frame, f"TWO-HAND mode (buffer: {hist_len}/{TWO_HAND_DIST_HISTORY_SIZE})",
                            (10, 80), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 200, 255), 2)

            cv2.imshow("Gesture Test", frame)
            if cv2.waitKey(1) & 0xFF == ord('q'):
                print("[Test] 'q' 키 감지. 종료.", flush=True)
                self._running = False
                break

        landmarker.close()
        cap.release()
        cv2.destroyAllWindows()
        self._running = False

    # ------------------------------------------------------------------
    # Gesture detection (single + two-hand)
    # ------------------------------------------------------------------

    def _detect_gesture(self, results) -> str | None:
        """
        MediaPipe 결과에서 제스처를 감지한다.

        ■ 두 손 감지 → 모션 기반: wrist 간 거리 변화로 SPREAD/GATHER 판정
        ■ 한 손 감지 → 포즈 기반: 손가락 모양으로 분류
        """
        if not results.hand_landmarks:
            self._two_hand_dist_history.clear()
            return None

        hand_list = results.hand_landmarks

        # ── 두 손 모드: 모션 기반 ──
        if len(hand_list) >= 2:
            wrist_0 = hand_list[0][0]
            wrist_1 = hand_list[1][0]
            dist = math.sqrt(
                (wrist_0.x - wrist_1.x) ** 2
                + (wrist_0.y - wrist_1.y) ** 2
            )

            self._two_hand_dist_history.append(dist)
            if len(self._two_hand_dist_history) > TWO_HAND_DIST_HISTORY_SIZE:
                self._two_hand_dist_history.pop(0)

            # 히스토리가 충분히 쌓이면 거리 변화량 판정
            now = time.monotonic()
            if (len(self._two_hand_dist_history) >= TWO_HAND_DIST_HISTORY_SIZE
                    and now - self._two_hand_last_fire >= TWO_HAND_COOLDOWN):
                oldest = self._two_hand_dist_history[0]
                newest = self._two_hand_dist_history[-1]
                delta = newest - oldest

                if delta >= TWO_HAND_SPREAD_DELTA:
                    self._two_hand_dist_history.clear()
                    self._two_hand_last_fire = now
                    return "SPREAD"
                elif delta <= -TWO_HAND_GATHER_DELTA:
                    self._two_hand_dist_history.clear()
                    self._two_hand_last_fire = now
                    return "GATHER"

            return None  # 두 손 모드에서는 한 손 제스처 무시

        # ── 한 손 모드: 포즈 기반 ──
        self._two_hand_dist_history.clear()
        return self._classify_gesture(hand_list[0])

    # ------------------------------------------------------------------
    # Gesture classification (single hand)
    # ------------------------------------------------------------------

    def _classify_gesture(self, hand_landmarks) -> str | None:
        """
        21개 랜드마크로 제스처를 분류한다.

        반환값: "PALM_OPEN" | "FIST" | "OK_SIGN" | "PEACE" | None
        """
        lm = hand_landmarks  # Tasks API: 리스트로 직접 접근 가능

        thumb_ext  = self._is_finger_extended(lm, "thumb")
        index_ext  = self._is_finger_extended(lm, "index")
        middle_ext = self._is_finger_extended(lm, "middle")
        ring_ext   = self._is_finger_extended(lm, "ring")
        pinky_ext  = self._is_finger_extended(lm, "pinky")

        # PALM_OPEN: 5개 모두 펴짐
        if thumb_ext and index_ext and middle_ext and ring_ext and pinky_ext:
            return "PALM_OPEN"

        # FIST: 5개 모두 접힘
        if (not thumb_ext and not index_ext and not middle_ext
                and not ring_ext and not pinky_ext):
            return "FIST"

        # OK_SIGN: 엄지+검지 끝이 가까움 + 나머지(중지, 약지, 소지) 펴짐
        if middle_ext and ring_ext and pinky_ext:
            thumb_tip = lm[FINGER_INDICES["thumb"]["tip"]]
            index_tip = lm[FINGER_INDICES["index"]["tip"]]
            dist = math.sqrt(
                (thumb_tip.x - index_tip.x) ** 2
                + (thumb_tip.y - index_tip.y) ** 2
            )
            if dist < OK_SIGN_THRESHOLD:
                return "OK_SIGN"

        # PEACE: 검지+중지 펴짐, 나머지 접힘
        if (index_ext and middle_ext
                and not thumb_ext and not ring_ext and not pinky_ext):
            return "PEACE"

        return None

    def _is_finger_extended(self, landmarks, finger: str) -> bool:
        """
        해당 손가락이 펴져 있는지 판별한다.

        엄지(thumb)는 좌우(x축) 방향으로 판별한다.
        나머지 손가락은 tip.y < pip.y (위쪽이 y 감소) 이면 펴진 것으로 판단한다.
        """
        if finger == "thumb":
            tip = landmarks[FINGER_INDICES["thumb"]["tip"]]
            ip  = landmarks[FINGER_INDICES["thumb"]["ip"]]
            mcp = landmarks[FINGER_INDICES["thumb"]["mcp"]]
            # 엄지: tip이 ip보다 손목 반대 방향으로 더 나와 있으면 펴짐
            # 손의 방향(좌/우)에 무관하게 tip과 ip의 x 거리가 충분하면 펴진 것으로 판정
            return abs(tip.x - mcp.x) > abs(ip.x - mcp.x)
        else:
            tip_idx = FINGER_INDICES[finger]["tip"]
            pip_idx = FINGER_INDICES[finger]["pip"]
            tip = landmarks[tip_idx]
            pip = landmarks[pip_idx]
            # 이미지 좌표계에서 y는 아래로 증가 → tip.y < pip.y 이면 펴짐
            return tip.y < pip.y


# ------------------------------------------------------------------
# Standalone helpers
# ------------------------------------------------------------------

_running_as_main = False


def _is_standalone():
    return _running_as_main


def main():
    """Standalone entry point."""
    global _running_as_main
    _running_as_main = True

    import argparse

    parser = argparse.ArgumentParser(
        description="GestureControl — MediaPipe 손 제스처 → JARVIS 액션"
    )
    parser.add_argument("--camera", type=int, default=1, help="카메라 인덱스 (기본 1, 맥북 카메라)")
    parser.add_argument("--ws-url", type=str, default="ws://localhost:8767", help="JARVIS WebSocket URL")
    parser.add_argument("--test", action="store_true", help="테스트 모드: WebSocket 없이 제스처 인식만 수행 (OpenCV 윈도우)")
    args = parser.parse_args()

    ctrl = GestureControl(camera_index=args.camera, ws_url=args.ws_url, test_mode=args.test)

    if args.test:
        print(f"[Gesture] 테스트 모드 시작 (camera={args.camera})", flush=True)
        print("[Gesture] 한 손: PALM_OPEN / FIST / OK_SIGN / PEACE", flush=True)
        print("[Gesture] 두 손: SPREAD (벌리기) / GATHER (모으기)", flush=True)
        print("[Gesture] 'q' 키로 종료", flush=True)
        try:
            ctrl.run_test()
        except KeyboardInterrupt:
            pass
        print("[Gesture] 완료.", flush=True)
    else:
        print(f"[Gesture] 시작 (camera={args.camera}, ws={args.ws_url})", flush=True)
        print("[Gesture] 한 손: PALM=zoom_in / FIST=zoom_out / OK=record / PEACE=screenshot", flush=True)
        print("[Gesture] 두 손: SPREAD=spread_open / GATHER=gather_close", flush=True)
        ctrl.start()
        try:
            while True:
                time.sleep(0.5)
        except KeyboardInterrupt:
            print("\n[Gesture] 종료 중...", flush=True)
            ctrl.stop()
            print("[Gesture] 완료.", flush=True)


if __name__ == "__main__":
    main()
