"""
gesture_control.py - MediaPipe 기반 손 제스처 컨트롤 모듈.

맥북 카메라에서 실시간으로 손을 인식하고, 4가지 제스처를 분류하여
JARVIS WebSocket 서버로 액션을 전송한다.

제스처 목록:
    PALM_OPEN  (손바닥 펴기)  → zoom_in
    FIST       (주먹)         → zoom_out
    OK_SIGN    (엄지+검지 원) → remote_record toggle
    PEACE      (피스)         → screenshot_analyze

Usage (standalone):
    python -m whisperflow.gesture_control
    python -m whisperflow.gesture_control --camera 1
"""

import asyncio
import json
import math
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
}

# OK 사인 판정: 엄지 끝과 검지 끝의 최대 거리 (정규화 좌표 기준)
OK_SIGN_THRESHOLD = 0.07

# 제스처 유지 시간 (초) — 이 시간 이상 같은 제스처가 유지되어야 액션 발동
GESTURE_HOLD_SECONDS = 0.5


class GestureControl:
    def __init__(self, camera_index=1, ws_url="ws://localhost:8767"):
        self.camera_index = camera_index
        self.ws_url = ws_url
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
        except ImportError:
            print("[Gesture] mediapipe가 설치되지 않았습니다. pip install mediapipe", flush=True)
            return

        try:
            import websockets
        except ImportError:
            print("[Gesture] websockets가 설치되지 않았습니다. pip install websockets", flush=True)
            return

        _standalone = _is_standalone()

        mp_hands = mp.solutions.hands
        hands = mp_hands.Hands(
            static_image_mode=False,
            max_num_hands=1,
            min_detection_confidence=0.7,
            min_tracking_confidence=0.6,
        )

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

                        # MediaPipe는 RGB 입력을 요구함
                        frame_rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                        results = hands.process(frame_rgb)

                        gesture = None
                        if results.multi_hand_landmarks:
                            hand_landmarks = results.multi_hand_landmarks[0]
                            gesture = self._classify_gesture(hand_landmarks)

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
        hands.close()
        with self._cap_lock:
            if self._cap is not None and self._cap.isOpened():
                self._cap.release()
                if _standalone:
                    print("[Gesture] 카메라 해제.", flush=True)
                self._cap = None

    # ------------------------------------------------------------------
    # Gesture classification
    # ------------------------------------------------------------------

    def _classify_gesture(self, hand_landmarks) -> str | None:
        """
        21개 랜드마크로 제스처를 분류한다.

        반환값: "PALM_OPEN" | "FIST" | "OK_SIGN" | "PEACE" | None
        """
        lm = hand_landmarks.landmark  # 인덱스로 접근 가능한 랜드마크 리스트

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
    args = parser.parse_args()

    ctrl = GestureControl(camera_index=args.camera, ws_url=args.ws_url)
    print(f"[Gesture] 시작 (camera={args.camera}, ws={args.ws_url})", flush=True)
    print("[Gesture] 제스처: PALM_OPEN=zoom_in / FIST=zoom_out / OK=record / PEACE=screenshot", flush=True)
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
