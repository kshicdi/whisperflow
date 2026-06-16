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
    SPREAD     (두 손 벌리기)   → spread_open
    GATHER     (두 손 모으기)   → gather_close
    PUSH_DOWN  (두 손 아래로)   → push_down
    PULL_UP    (두 손 위로)     → pull_up

Usage (standalone):
    python -m whisperflow.gesture_control
    python -m whisperflow.gesture_control --camera 1
    python -m whisperflow.gesture_control --test          # WS 없이 테스트
    python -m whisperflow.gesture_control --mac           # 맥 시스템 제어 (GUI 없음)
    python -m whisperflow.gesture_control --test --mac    # 테스트 + 맥 제어 동시
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
    "PUSH_DOWN": {"type": "ui_action",     "value": "push_down"},
    "PULL_UP":   {"type": "ui_action",     "value": "pull_up"},
    "POINT":     {"type": "ui_action",     "value": "mouse_control"},
}

# 한 손 스크롤 설정 (PALM_OPEN + 위아래 이동)
SCROLL_Y_THRESHOLD = 0.02       # 프레임 간 y 변화가 이 이상이면 스크롤 발동
SCROLL_COOLDOWN = 0.08          # 스크롤 이벤트 간 최소 간격 (초)
SCROLL_AMOUNT = 8               # 한 번에 스크롤하는 줄 수
SCROLL_WARMUP = 0.5             # PALM_OPEN 유지 후 스크롤 모드 진입 시간 (초)

# 검지 마우스 제어 설정
MOUSE_SMOOTHING = 0.3           # EMA 스무딩 계수 (0=이전값 유지, 1=즉시 반영)
MOUSE_ACTIVE_ZONE = 0.4         # 카메라 프레임 중앙 40%를 활성 영역으로 사용 (손 적게 움직여도 멀리 이동)
MOUSE_PINCH_THRESHOLD = 0.06    # 엄지+검지 거리가 이 이하이면 클릭
MOUSE_PINCH_COOLDOWN = 0.5      # 클릭 간 최소 간격 (초)
MOUSE_DWELL_TIME = 1.0          # 멈춤 클릭 대기 시간 (초)
MOUSE_DWELL_RADIUS = 0.03       # 이 범위 안에 있으면 "멈춤"으로 판정 (정규화 좌표)

# 주먹 드래그 설정 (POINT로 커서 이동 → 주먹 꾸욱 = 잡기 → 이동 → 검지 펴기 = 놓기)
FIST_DRAG_HOLD = 0.35           # 주먹을 이 시간(초) 이상 유지하면 드래그 시작 (짧으면 클릭)
FIST_CLICK_MIN = 0.08           # 주먹이 이 시간(초) 미만이면 인식 플리커로 보고 클릭 무시
FIST_DRAG_LOST_GRACE = 0.5      # 드래그 중 손 인식이 끊겨도 유지하는 시간 (초)

# 맥 시스템 제어 제스처 → osascript 명령 매핑
import subprocess

MAC_GESTURE_COMMANDS: dict[str, str] = {
    "SPREAD":    'tell application "System Events" to key code 126 using control down',       # Ctrl+Up (Mission Control)
    "GATHER":    'tell application "System Events" to key code 126 using control down',       # Ctrl+Up (Mission Control 토글)
    # "PALM_OPEN": 비활성화 — 한 손 오발동 방지
    # "FIST":      비활성화 — 한 손 오발동 방지
    # "PUSH_DOWN": 비활성화 — Show Desktop 단축키 미설정
    "PULL_UP":   None,  # 별도 처리 (restore_windows.scpt)
}


_RESTORE_SCRIPT = os.path.join(os.path.dirname(__file__), "scripts", "restore_windows.scpt")

def _execute_mac_control(gesture: str):
    """osascript로 맥 시스템 제어 명령을 전송한다."""
    if gesture == "PULL_UP":
        subprocess.Popen(["osascript", _RESTORE_SCRIPT], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        print(f"[Mac] PULL_UP → 최소화 창 복원", flush=True)
        return
    cmd = MAC_GESTURE_COMMANDS.get(gesture)
    if cmd:
        subprocess.Popen(["osascript", "-e", cmd], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        print(f"[Mac] {gesture} → 실행", flush=True)


def _move_mouse(x: int, y: int):
    """Quartz CGEvent로 마우스 커서 이동."""
    try:
        from Quartz import CGEventCreateMouseEvent, CGEventPost, kCGHIDEventTap, kCGEventMouseMoved, kCGMouseButtonLeft
        event = CGEventCreateMouseEvent(None, kCGEventMouseMoved, (x, y), kCGMouseButtonLeft)
        CGEventPost(kCGHIDEventTap, event)
    except Exception:
        pass


def _click_mouse(x: int, y: int):
    """Quartz CGEvent로 마우스 클릭."""
    try:
        from Quartz import CGEventCreateMouseEvent, CGEventPost, kCGHIDEventTap, kCGEventLeftMouseDown, kCGEventLeftMouseUp, kCGMouseButtonLeft
        event_down = CGEventCreateMouseEvent(None, kCGEventLeftMouseDown, (x, y), kCGMouseButtonLeft)
        event_up = CGEventCreateMouseEvent(None, kCGEventLeftMouseUp, (x, y), kCGMouseButtonLeft)
        CGEventPost(kCGHIDEventTap, event_down)
        CGEventPost(kCGHIDEventTap, event_up)
    except Exception:
        pass


def _mouse_down(x: int, y: int):
    """Quartz CGEvent로 마우스 버튼 누르기 (드래그 시작)."""
    try:
        from Quartz import CGEventCreateMouseEvent, CGEventPost, kCGHIDEventTap, kCGEventLeftMouseDown, kCGMouseButtonLeft
        event = CGEventCreateMouseEvent(None, kCGEventLeftMouseDown, (x, y), kCGMouseButtonLeft)
        CGEventPost(kCGHIDEventTap, event)
    except Exception:
        pass


def _mouse_drag(x: int, y: int):
    """Quartz CGEvent로 마우스 드래그 이동 (버튼 누른 채 이동)."""
    try:
        from Quartz import CGEventCreateMouseEvent, CGEventPost, kCGHIDEventTap, kCGEventLeftMouseDragged, kCGMouseButtonLeft
        event = CGEventCreateMouseEvent(None, kCGEventLeftMouseDragged, (x, y), kCGMouseButtonLeft)
        CGEventPost(kCGHIDEventTap, event)
    except Exception:
        pass


def _mouse_up(x: int, y: int):
    """Quartz CGEvent로 마우스 버튼 놓기 (드래그 종료)."""
    try:
        from Quartz import CGEventCreateMouseEvent, CGEventPost, kCGHIDEventTap, kCGEventLeftMouseUp, kCGMouseButtonLeft
        event = CGEventCreateMouseEvent(None, kCGEventLeftMouseUp, (x, y), kCGMouseButtonLeft)
        CGEventPost(kCGHIDEventTap, event)
    except Exception:
        pass


def find_builtin_camera_index() -> int:
    """맥북 내장 카메라의 OpenCV 인덱스를 찾는다.

    아이폰 연속성 카메라가 연결되면 index 0이 아이폰이 될 수 있다.
    OpenCV avfoundation 백엔드(cap_avfoundation_mac.mm)는
    devicesWithMediaType(Video) + devicesWithMediaType(Muxed)를
    uniqueID 문자열 정렬한 순서로 인덱싱하므로, 동일한 방식으로
    장치 목록을 만들어 내장(BuiltInWideAngle) 카메라의 위치를 찾는다.
    실패 시 0 반환.
    """
    try:
        import AVFoundation as AVF
        video = list(AVF.AVCaptureDevice.devicesWithMediaType_(AVF.AVMediaTypeVideo))
        muxed = list(AVF.AVCaptureDevice.devicesWithMediaType_(AVF.AVMediaTypeMuxed))
        devices = sorted(video + muxed, key=lambda d: str(d.uniqueID()))
        for i, dev in enumerate(devices):
            if dev.deviceType() == AVF.AVCaptureDeviceTypeBuiltInWideAngleCamera:
                print(f"[Gesture] 내장 카메라 발견: index={i} ({dev.localizedName()})", flush=True)
                return i
    except Exception as e:
        print(f"[Gesture] 카메라 장치 조회 실패, index 0 사용: {e}", flush=True)
    return 0


def _get_screen_size() -> tuple[int, int, int, int]:
    """전체 가상 화면 범위 가져오기. (min_x, min_y, total_w, total_h) 반환."""
    try:
        from AppKit import NSScreen
        screens = NSScreen.screens()
        if not screens:
            return (0, 0, 1920, 1080)
        min_x = min(s.frame().origin.x for s in screens)
        min_y = min(s.frame().origin.y for s in screens)
        max_x = max(s.frame().origin.x + s.frame().size.width for s in screens)
        max_y = max(s.frame().origin.y + s.frame().size.height for s in screens)
        return (int(min_x), int(min_y), int(max_x - min_x), int(max_y - min_y))
    except Exception:
        return (0, 0, 1920, 1080)


def _execute_scroll(direction: str, amount: int = SCROLL_AMOUNT):
    """Quartz CGEvent로 마우스 스크롤 휠 이벤트 전송."""
    try:
        from Quartz import CGEventCreateScrollWheelEvent, CGEventPost, kCGHIDEventTap, kCGScrollEventUnitLine
        scroll_val = amount if direction == "up" else -amount
        event = CGEventCreateScrollWheelEvent(None, kCGScrollEventUnitLine, 1, scroll_val)
        CGEventPost(kCGHIDEventTap, event)
    except Exception:
        pass


# OK 사인 판정: 엄지 끝과 검지 끝의 최대 거리 (정규화 좌표 기준)
OK_SIGN_THRESHOLD = 0.07

# 두 손 모션 판정: wrist 간 거리 변화량 기준 (정규화 좌표 기준)
TWO_HAND_DIST_HISTORY_SIZE = 10   # 거리 히스토리 버퍼 크기 (~0.33초 @ 30fps)
TWO_HAND_SPREAD_DELTA = 0.20      # 이 만큼 거리가 증가하면 SPREAD
TWO_HAND_GATHER_DELTA = 0.20      # 이 만큼 거리가 감소하면 GATHER
TWO_HAND_COOLDOWN = 2.0           # 발동 후 재발동 대기 시간 (초)
TWO_HAND_VERTICAL_DELTA = 0.20    # y축 이동량이 이 이상이면 PUSH_DOWN/PULL_UP (SPREAD와 동일 민감도)
TWO_HAND_GRACE_FRAMES = 8         # 한 손이 사라져도 유지하는 프레임 수 (~0.27초 @ 30fps)
TWO_HAND_GRACE_TIMEOUT = 0.5      # grace period 최대 시간 (초)

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
    def __init__(self, camera_index=1, ws_url="ws://localhost:8767", test_mode=False, mac_mode=False):
        self.camera_index = camera_index
        self.ws_url = ws_url
        self.test_mode = test_mode
        self.mac_mode = mac_mode
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

        # 한 손 스크롤 추적
        self._scroll_last_y: float | None = None   # 이전 프레임 wrist y좌표
        self._scroll_last_time: float = 0.0         # 마지막 스크롤 이벤트 시각
        self._scroll_palm_start: float = 0.0        # PALM_OPEN 시작 시각
        self._scroll_ready: bool = False             # 워밍업 완료 여부

        # 검지 마우스 제어 추적
        self._mouse_smooth_x: float | None = None  # EMA 스무딩된 x좌표
        self._mouse_smooth_y: float | None = None   # EMA 스무딩된 y좌표
        self._mouse_last_click: float = 0.0         # 마지막 클릭 시각
        self._mouse_screen_info: tuple[int, int, int, int] | None = None  # (min_x, min_y, w, h)
        self._mouse_active: bool = False             # 마우스 모드 활성 중
        self._mouse_was_point: bool = False          # 이전 프레임에서 POINT였는지 (클릭 감지용)
        self._mouse_dwell_start: float = 0.0         # 멈춤 시작 시각
        self._mouse_dwell_anchor: tuple[float, float] | None = None  # 멈춤 기준 좌표
        self._mouse_dwell_progress: float = 0.0      # 멈춤 진행률 (0~1, UI용)
        # 주먹 드래그 상태
        self._drag_active: bool = False              # 현재 드래그 중 (마우스 버튼 누른 상태)
        self._drag_fist_start: float = 0.0           # 주먹 쥔 시각 (짧으면 클릭, 길면 드래그)
        self._drag_hand_center: tuple[float, float] | None = None  # 이전 프레임 손 중심 (드래그 이동용)
        self._drag_lost_time: float = 0.0            # 드래그 중 손 인식이 끊긴 시각

        # 두 손 모션 추적
        self._two_hand_dist_history: list[float] = []  # wrist 간 x거리 히스토리
        self._two_hand_y_history: list[float] = []     # wrist 평균 y좌표 히스토리
        self._two_hand_last_fire: float = 0.0          # 마지막 발동 시각

        # 두 손 grace period 추적
        self._two_hand_tracking: bool = False           # 두 손 추적 모드 활성 여부
        self._two_hand_last_wrists: tuple | None = None # 마지막 양쪽 wrist 좌표 ((x0,y0),(x1,y1))
        self._two_hand_lost_time: float = 0.0           # 한 손 사라진 시각
        self._two_hand_lost_frames: int = 0             # 한 손 사라진 프레임 수

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
        self._release_drag("stopped")
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
                    # 맥 제어 모드일 때 키 이벤트 전송
                    if self.mac_mode:
                        _execute_mac_control(gesture)
                else:
                    print("[Test] 제스처: (없음)", flush=True)
                last_printed_gesture = gesture

            # ── 손 좌표 기반 오버레이는 flip 전에 그린다 (손과 함께 거울 반전됨) ──
            num_hands = len(results.hand_landmarks) if results.hand_landmarks else 0

            # Dwell click 원형 UI 표시
            if self._mouse_dwell_progress > 0 and self._mouse_active and results.hand_landmarks:
                index_tip = results.hand_landmarks[0][FINGER_INDICES["index"]["tip"]]
                cx = int(index_tip.x * w)
                cy = int(index_tip.y * h)
                radius = int(30 * (1.0 - self._mouse_dwell_progress))  # 30px → 0px
                if radius > 0:
                    cv2.circle(frame, (cx, cy), 30, (50, 50, 50), 2)
                    cv2.circle(frame, (cx, cy), radius, (0, 255, 255), 3)
                else:
                    cv2.circle(frame, (cx, cy), 8, (0, 255, 0), -1)

            # 드래그 중 표시 (손 중심에 빨간 원)
            if self._drag_active and results.hand_landmarks:
                ctr = results.hand_landmarks[0][FINGER_INDICES["middle"]["mcp"]]
                cv2.circle(frame, (int(ctr.x * w), int(ctr.y * h)), 22, (0, 0, 255), 4)

            # ── 거울 반전 (손을 오른쪽으로 = 화면에서도 오른쪽, 커서 방향과 일치) ──
            frame = cv2.flip(frame, 1)

            # ── 텍스트 오버레이는 flip 후에 그린다 (글자가 뒤집히지 않도록) ──
            label = gesture if gesture else "None"
            cv2.putText(frame, f"Gesture: {label}", (10, 40),
                        cv2.FONT_HERSHEY_SIMPLEX, 1.2, (0, 255, 0), 3)
            if self._drag_active:
                cv2.putText(frame, "DRAGGING", (10, h - 20),
                            cv2.FONT_HERSHEY_SIMPLEX, 1.0, (0, 0, 255), 3)
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

    def run_mac(self):
        """맥 제어 전용 모드: GUI 없이 카메라 + 제스처 감지 → 맥 키 이벤트 전송."""
        self._running = True
        try:
            import cv2
        except ImportError:
            print("[Mac] opencv-python이 설치되지 않았습니다. pip install opencv-python", flush=True)
            return

        try:
            import mediapipe as mp
            from mediapipe.tasks.python.vision import HandLandmarker, HandLandmarkerOptions, RunningMode
            from mediapipe.tasks.python import BaseOptions
        except ImportError:
            print("[Mac] mediapipe가 설치되지 않았습니다. pip install mediapipe", flush=True)
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

        print(f"[Mac] 카메라 {self.camera_index} 열기 시도...", flush=True)
        cap = cv2.VideoCapture(self.camera_index)
        if not cap.isOpened():
            print(f"[Mac] 카메라 {self.camera_index}를 열 수 없습니다.", flush=True)
            return
        print(f"[Mac] 카메라 연결됨. Ctrl+C로 종료.", flush=True)
        print("[Mac] SPREAD→Mission Control / GATHER→닫기 / PALM_OPEN→전체화면 / FIST→최소화", flush=True)

        last_printed_gesture = None

        while self._running:
            ret, frame = cap.read()
            if not ret or frame is None:
                print("[Mac] 프레임 읽기 실패.", flush=True)
                break

            frame_rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            timestamp_ms = int((time.monotonic() - t0) * 1000)
            mp_image = mp.Image(image_format=mp.ImageFormat.SRGB, data=frame_rgb)
            results = landmarker.detect_for_video(mp_image, timestamp_ms)

            gesture = self._detect_gesture(results)

            # 제스처 변경 시에만 출력 + 맥 제어
            if gesture != last_printed_gesture:
                if gesture is not None:
                    print(f"[Mac] 제스처: {gesture}", flush=True)
                    _execute_mac_control(gesture)
                else:
                    print("[Mac] 제스처: (없음)", flush=True)
                last_printed_gesture = gesture

            time.sleep(0.033)  # ~30fps

        landmarker.close()
        cap.release()
        self._running = False

    # ------------------------------------------------------------------
    # Gesture detection (single + two-hand)
    # ------------------------------------------------------------------

    def _reset_two_hand_tracking(self):
        """두 손 추적 상태 완전 초기화."""
        self._two_hand_tracking = False
        self._two_hand_last_wrists = None
        self._two_hand_lost_time = 0.0
        self._two_hand_lost_frames = 0
        self._two_hand_dist_history.clear()
        self._two_hand_y_history.clear()

    def _update_two_hand_history(self, w0x, w0y, w1x, w1y):
        """두 손 wrist 좌표로 히스토리 업데이트 + 모션 판정."""
        dist = math.sqrt((w0x - w1x) ** 2 + (w0y - w1y) ** 2)
        avg_y = (w0y + w1y) / 2

        self._two_hand_dist_history.append(dist)
        self._two_hand_y_history.append(avg_y)
        if len(self._two_hand_dist_history) > TWO_HAND_DIST_HISTORY_SIZE:
            self._two_hand_dist_history.pop(0)
            self._two_hand_y_history.pop(0)

        # 히스토리가 충분히 쌓이면 변화량 판정
        now = time.monotonic()
        if (len(self._two_hand_dist_history) >= TWO_HAND_DIST_HISTORY_SIZE
                and now - self._two_hand_last_fire >= TWO_HAND_COOLDOWN):
            dist_delta = self._two_hand_dist_history[-1] - self._two_hand_dist_history[0]
            y_delta = self._two_hand_y_history[-1] - self._two_hand_y_history[0]

            # 주요 축 판별: y축 변화가 x축 변화의 1.5배 이상이면 y축 우세
            abs_dist = abs(dist_delta)
            abs_y = abs(y_delta)

            if abs_y > abs_dist * 1.5 and abs_y >= TWO_HAND_VERTICAL_DELTA:
                # y축 우세 → PUSH_DOWN/PULL_UP
                if y_delta >= TWO_HAND_VERTICAL_DELTA:
                    self._reset_two_hand_tracking()
                    self._two_hand_last_fire = now
                    return "PUSH_DOWN"
                elif y_delta <= -TWO_HAND_VERTICAL_DELTA:
                    self._reset_two_hand_tracking()
                    self._two_hand_last_fire = now
                    return "PULL_UP"
            elif abs_dist >= TWO_HAND_SPREAD_DELTA:
                # x축 우세 → SPREAD/GATHER
                if dist_delta >= TWO_HAND_SPREAD_DELTA:
                    self._reset_two_hand_tracking()
                    self._two_hand_last_fire = now
                    return "SPREAD"
                elif dist_delta <= -TWO_HAND_GATHER_DELTA:
                    self._reset_two_hand_tracking()
                    self._two_hand_last_fire = now
                    return "GATHER"

        return None

    def _detect_gesture(self, results) -> str | None:
        """
        MediaPipe 결과에서 제스처를 감지한다.

        ■ 두 손 감지 → 모션 기반: wrist 간 거리 변화로 SPREAD/GATHER 판정
        ■ 한 손만 + 추적 중 → grace period: 마지막 위치 유지
        ■ 한 손만 + 추적 아님 → 포즈 기반: 손가락 모양으로 분류
        """
        hand_list = results.hand_landmarks if results.hand_landmarks else []
        num_hands = len(hand_list)
        now = time.monotonic()

        # 드래그 중 손 인식이 끊긴 경우: grace 시간 내 복귀 없으면 드랍
        if num_hands == 0:
            if self._drag_active:
                if self._drag_lost_time == 0.0:
                    self._drag_lost_time = now
                elif now - self._drag_lost_time > FIST_DRAG_LOST_GRACE:
                    self._release_drag("hand lost")
            else:
                # 주먹 클릭 타이머 리셋 (오래된 타이머로 인한 오클릭 방지)
                self._drag_fist_start = 0.0
                self._drag_hand_center = None

        # ── 두 손 감지: 마우스 활성 중이면 마우스 손만 처리 ──
        if num_hands >= 2:
            # 드래그(주먹) 중 두 번째 손 등장 → 드랍 (두 손 모드와 충돌·버튼 잠김 방지)
            self._release_drag("two hands")
            if self._mouse_active and self.mac_mode:
                # 양손 분리: 각 손을 개별 분류
                point_hand = None
                scroll_hand = None
                for hand_lm in hand_list:
                    g = self._classify_gesture(hand_lm)
                    if g == "POINT" and point_hand is None:
                        point_hand = hand_lm
                    elif g == "PALM_OPEN" and scroll_hand is None:
                        scroll_hand = hand_lm

                # POINT 손 → 마우스 계속
                if point_hand is not None:
                    self._handle_mouse_point(point_hand, now)
                    self._mouse_was_point = True

                    # PALM_OPEN 손 → 스크롤 동시 처리 (양손 시 fps 저하 보정으로 amount 2배)
                    if scroll_hand is not None:
                        wrist_y = scroll_hand[0].y
                        if self._scroll_last_y is not None:
                            dy = wrist_y - self._scroll_last_y
                            if abs(dy) >= SCROLL_Y_THRESHOLD and now - self._scroll_last_time >= SCROLL_COOLDOWN:
                                if dy > 0:
                                    _execute_scroll("down", SCROLL_AMOUNT * 2)
                                else:
                                    _execute_scroll("up", SCROLL_AMOUNT * 2)
                                self._scroll_last_time = now
                        self._scroll_last_y = wrist_y
                    return "POINT"

                # POINT인 손이 없으면 마우스 모드 해제
                self._exit_mouse_mode("two hands, no point")

            wrist_0 = hand_list[0][0]
            wrist_1 = hand_list[1][0]

            # 추적 모드 진입/유지
            self._two_hand_tracking = True
            self._two_hand_last_wrists = ((wrist_0.x, wrist_0.y), (wrist_1.x, wrist_1.y))
            self._two_hand_lost_frames = 0
            self._two_hand_lost_time = 0.0

            return self._update_two_hand_history(
                wrist_0.x, wrist_0.y, wrist_1.x, wrist_1.y)

        # ── 한 손만 감지 + 추적 중: grace period ──
        if num_hands == 1 and self._two_hand_tracking and self._two_hand_last_wrists:
            if self._two_hand_lost_frames == 0:
                self._two_hand_lost_time = now  # 사라진 시각 기록

            self._two_hand_lost_frames += 1
            elapsed = now - self._two_hand_lost_time

            if (self._two_hand_lost_frames <= TWO_HAND_GRACE_FRAMES
                    and elapsed <= TWO_HAND_GRACE_TIMEOUT):
                # grace period 내: 감지된 손 + 마지막 위치로 추적 계속
                detected_wrist = hand_list[0][0]
                last_w0, last_w1 = self._two_hand_last_wrists

                # 감지된 손이 어느 쪽에 가까운지 판별
                d0 = math.sqrt((detected_wrist.x - last_w0[0]) ** 2 +
                               (detected_wrist.y - last_w0[1]) ** 2)
                d1 = math.sqrt((detected_wrist.x - last_w1[0]) ** 2 +
                               (detected_wrist.y - last_w1[1]) ** 2)

                if d0 < d1:
                    # 감지된 손 = 0번, 사라진 손 = 1번 (마지막 위치 사용)
                    return self._update_two_hand_history(
                        detected_wrist.x, detected_wrist.y, last_w1[0], last_w1[1])
                else:
                    # 감지된 손 = 1번, 사라진 손 = 0번 (마지막 위치 사용)
                    return self._update_two_hand_history(
                        last_w0[0], last_w0[1], detected_wrist.x, detected_wrist.y)
            else:
                # grace period 초과 → 추적 해제
                self._reset_two_hand_tracking()

        # ── 손 없음 + 추적 중: grace period ──
        if num_hands == 0 and self._two_hand_tracking:
            if self._two_hand_lost_frames == 0:
                self._two_hand_lost_time = now

            self._two_hand_lost_frames += 1
            elapsed = now - self._two_hand_lost_time

            if elapsed > TWO_HAND_GRACE_TIMEOUT:
                self._reset_two_hand_tracking()

            return None

        # ── 한 손 모드: 포즈 기반 + 스크롤 ──
        if not self._two_hand_tracking:
            self._two_hand_dist_history.clear()
            self._two_hand_y_history.clear()
            if num_hands >= 1:
                gesture = self._classify_gesture(hand_list[0])

                if self.mac_mode:
                    # PALM_OPEN 상태에서 위아래 이동 → 스크롤
                    if gesture == "PALM_OPEN":
                        # 손을 활짝 폈으면 드랍 + 마우스 모드 완전 종료
                        self._exit_mouse_mode("palm open")
                        # 워밍업: PALM_OPEN이 일정 시간 유지된 후에만 스크롤
                        if not self._scroll_ready:
                            if self._scroll_palm_start == 0.0:
                                self._scroll_palm_start = now
                            elif now - self._scroll_palm_start >= SCROLL_WARMUP:
                                self._scroll_ready = True
                                self._scroll_last_y = hand_list[0][0].y
                        else:
                            wrist_y = hand_list[0][0].y
                            if self._scroll_last_y is not None:
                                dy = wrist_y - self._scroll_last_y
                                if abs(dy) >= SCROLL_Y_THRESHOLD and now - self._scroll_last_time >= SCROLL_COOLDOWN:
                                    if dy > 0:
                                        _execute_scroll("down")
                                    else:
                                        _execute_scroll("up")
                                    self._scroll_last_time = now
                            self._scroll_last_y = wrist_y
                        self._mouse_smooth_x = None
                        self._mouse_smooth_y = None

                    # POINT 상태 → 검지 끝으로 마우스 커서 이동
                    elif gesture == "POINT":
                        self._scroll_last_y = None
                        self._scroll_palm_start = 0.0
                        self._scroll_ready = False
                        # 짧게 주먹 쥐었다 폈으면 → 클릭 (드래그 시작 전이었을 때만)
                        if (not self._drag_active
                                and self._drag_fist_start > 0.0
                                and FIST_CLICK_MIN <= now - self._drag_fist_start < FIST_DRAG_HOLD
                                and self._mouse_smooth_x is not None
                                and now - self._mouse_last_click >= MOUSE_PINCH_COOLDOWN):
                            _click_mouse(int(self._mouse_smooth_x), int(self._mouse_smooth_y))
                            self._mouse_last_click = now
                            print(f"[Mac] FIST CLICK at ({int(self._mouse_smooth_x)}, {int(self._mouse_smooth_y)})", flush=True)
                        # 드래그 중 검지를 다시 폈으면 → 드랍 (+ 주먹 상태 리셋)
                        self._release_drag("finger up")
                        self._handle_mouse_point(hand_list[0], now)
                        self._mouse_active = True
                        self._mouse_was_point = True

                    # 마우스 모드 중 주먹/검지 내림 → 클릭 대기 또는 드래그
                    elif self._mouse_was_point or self._drag_active:
                        self._scroll_last_y = None
                        self._scroll_palm_start = 0.0
                        self._scroll_ready = False
                        self._mouse_active = True  # 드래그 중에도 마우스 모드 유지 (정합성)
                        if self._handle_fist_drag(hand_list[0], gesture, now):
                            return gesture  # 마우스 모드 종료됨 → 해당 제스처 정상 발동
                        return None  # 드래그 조작 중에는 제스처 액션 발동 안 함

                    else:
                        self._scroll_last_y = None
                        self._scroll_palm_start = 0.0
                        self._scroll_ready = False

                return gesture

        return None

    # ------------------------------------------------------------------
    # Mouse control (POINT gesture)
    # ------------------------------------------------------------------

    def _handle_mouse_point(self, hand_landmarks, now: float):
        """검지 끝 좌표로 마우스 커서 이동 + dwell 클릭.

        클릭/드래그 방법:
          - 검지 멈춤 1초 = dwell 클릭
          - 주먹 짧게 쥐었다 펴기 = 클릭
          - 주먹 꾸욱(0.35초+) 쥐고 이동 = 드래그, 검지 펴면 놓기 (_handle_fist_drag)
        """
        if self._mouse_screen_info is None:
            self._mouse_screen_info = _get_screen_size()

        min_x, min_y, screen_w, screen_h = self._mouse_screen_info

        index_tip = hand_landmarks[FINGER_INDICES["index"]["tip"]]

        # Active Zone 매핑
        margin = (1.0 - MOUSE_ACTIVE_ZONE) / 2
        raw_x = 1.0 - index_tip.x
        raw_y = index_tip.y
        norm_x = max(0.0, min(1.0, (raw_x - margin) / MOUSE_ACTIVE_ZONE))
        norm_y = max(0.0, min(1.0, (raw_y - margin) / MOUSE_ACTIVE_ZONE))
        target_x = min_x + norm_x * screen_w
        target_y = min_y + norm_y * screen_h

        # EMA 스무딩
        if self._mouse_smooth_x is None:
            self._mouse_smooth_x = target_x
            self._mouse_smooth_y = target_y
        else:
            self._mouse_smooth_x += MOUSE_SMOOTHING * (target_x - self._mouse_smooth_x)
            self._mouse_smooth_y += MOUSE_SMOOTHING * (target_y - self._mouse_smooth_y)

        sx, sy = int(self._mouse_smooth_x), int(self._mouse_smooth_y)
        _move_mouse(sx, sy)

        # Dwell click: 검지가 일정 범위 안에 멈춰있으면 클릭
        if self._mouse_dwell_anchor is None:
            self._mouse_dwell_anchor = (norm_x, norm_y)
            self._mouse_dwell_start = now
            self._mouse_dwell_progress = 0.0
        else:
            dx = norm_x - self._mouse_dwell_anchor[0]
            dy = norm_y - self._mouse_dwell_anchor[1]
            moved = math.sqrt(dx * dx + dy * dy)

            if moved > MOUSE_DWELL_RADIUS:
                self._mouse_dwell_anchor = (norm_x, norm_y)
                self._mouse_dwell_start = now
                self._mouse_dwell_progress = 0.0
            else:
                elapsed = now - self._mouse_dwell_start
                self._mouse_dwell_progress = min(1.0, elapsed / MOUSE_DWELL_TIME)

                if (elapsed >= MOUSE_DWELL_TIME
                        and now - self._mouse_last_click >= MOUSE_PINCH_COOLDOWN
                        and not self._drag_active):
                    _click_mouse(sx, sy)
                    self._mouse_last_click = now
                    self._mouse_dwell_anchor = None
                    self._mouse_dwell_progress = 0.0
                    print(f"[Mac] DWELL CLICK at ({sx}, {sy})", flush=True)

    # ------------------------------------------------------------------
    # Fist drag (POINT → 주먹 꾸욱 → 이동 → 검지 펴기)
    # ------------------------------------------------------------------

    def _release_drag(self, reason: str):
        """드래그 중이면 마우스 버튼을 놓는다."""
        if self._drag_active:
            if self._mouse_smooth_x is not None and self._mouse_smooth_y is not None:
                _mouse_up(int(self._mouse_smooth_x), int(self._mouse_smooth_y))
            self._drag_active = False
            print(f"[Mac] DRAG END ({reason})", flush=True)
        self._drag_fist_start = 0.0
        self._drag_hand_center = None
        self._drag_lost_time = 0.0

    def _exit_mouse_mode(self, reason: str = "exit"):
        """마우스 모드 전체 종료 — 드래그 해제 + 모든 마우스 상태 리셋.

        마우스/드래그 종료는 반드시 이 헬퍼를 거친다 (분기별 정리 누락 방지).
        """
        self._release_drag(reason)
        self._mouse_active = False
        self._mouse_was_point = False
        self._mouse_smooth_x = None
        self._mouse_smooth_y = None
        self._mouse_dwell_anchor = None
        self._mouse_dwell_progress = 0.0

    def _handle_fist_drag(self, hand_landmarks, gesture: str | None, now: float) -> bool:
        """마우스 모드 중 주먹 쥠 처리.

        - 주먹 짧게(< FIST_DRAG_HOLD) 쥐었다 검지 펴기 → 클릭 (POINT 분기에서 발동)
        - 주먹 꾸욱(>= FIST_DRAG_HOLD) → 드래그 시작, 주먹 쥔 채 이동
        - 검지/손바닥 펴기 → 드랍

        반환: 마우스 모드를 종료했으면 True (호출자가 제스처를 정상 발동시킴).
        """
        # 주먹이 아닌 명확한 다른 제스처 (PEACE, OK_SIGN) → 마우스 모드 종료
        if gesture not in (None, "FIST"):
            self._exit_mouse_mode(f"gesture changed: {gesture}")
            return True

        self._drag_lost_time = 0.0
        # 손 중심 (middle MCP) — 주먹 상태에서는 검지 끝 추적 불가
        center = hand_landmarks[FINGER_INDICES["middle"]["mcp"]]

        if not self._drag_active:
            # 주먹 유지 시간 측정 (클릭 vs 드래그 판별)
            if self._drag_fist_start == 0.0:
                self._drag_fist_start = now
                self._drag_hand_center = (center.x, center.y)
                return False

            if now - self._drag_fist_start >= FIST_DRAG_HOLD:
                # 꾸욱 → 드래그 시작 (커서가 멈춰 있던 현재 위치를 잡는다)
                # 명시적 FIST 분류일 때만 — 단순 인식 실패(None)로는 시작하지 않음
                if gesture == "FIST" and self._mouse_smooth_x is not None:
                    self._drag_active = True
                    self._drag_hand_center = (center.x, center.y)
                    _mouse_down(int(self._mouse_smooth_x), int(self._mouse_smooth_y))
                    print(f"[Mac] DRAG START at ({int(self._mouse_smooth_x)}, {int(self._mouse_smooth_y)})", flush=True)
            return False

        # 드래그 중: 손 중심의 프레임 간 이동량만큼 커서를 끌고 간다
        if (self._mouse_screen_info is None
                or self._mouse_smooth_x is None or self._mouse_smooth_y is None):
            return False
        min_x, min_y, screen_w, screen_h = self._mouse_screen_info
        if self._drag_hand_center is not None:
            # 거울 모드 좌우 반전 + Active Zone 스케일로 화면 좌표 변환
            dx = -(center.x - self._drag_hand_center[0]) / MOUSE_ACTIVE_ZONE * screen_w
            dy = (center.y - self._drag_hand_center[1]) / MOUSE_ACTIVE_ZONE * screen_h
            self._mouse_smooth_x = max(min_x, min(min_x + screen_w - 1, self._mouse_smooth_x + dx))
            self._mouse_smooth_y = max(min_y, min(min_y + screen_h - 1, self._mouse_smooth_y + dy))
        self._drag_hand_center = (center.x, center.y)
        _mouse_drag(int(self._mouse_smooth_x), int(self._mouse_smooth_y))
        return False

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

        # POINT: 검지만 펴짐, 중지/약지/소지 접힘 (엄지 무관)
        if (index_ext and not middle_ext and not ring_ext and not pinky_ext):
            return "POINT"

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
    parser.add_argument("--camera", type=int, default=None, help="카메라 인덱스 (생략 시 맥북 내장 카메라 자동 감지)")
    parser.add_argument("--ws-url", type=str, default="ws://localhost:8767", help="JARVIS WebSocket URL")
    parser.add_argument("--test", action="store_true", help="테스트 모드: WebSocket 없이 제스처 인식만 수행 (OpenCV 윈도우)")
    parser.add_argument("--mac", action="store_true", help="맥 시스템 제어 모드: 제스처로 Mission Control, 전체화면, 최소화 등 제어")
    args = parser.parse_args()

    if args.camera is None:
        args.camera = find_builtin_camera_index()

    ctrl = GestureControl(
        camera_index=args.camera,
        ws_url=args.ws_url,
        test_mode=args.test,
        mac_mode=args.mac,
    )

    # SIGTERM (메뉴바 앱이 토글 OFF로 종료시킬 때): 드래그 해제 후 종료
    # — 드래그 중 강제 종료되면 마우스 버튼이 눌린 채 남기 때문
    import signal

    def _on_sigterm(signum, frame):
        ctrl.stop()
        os._exit(0)

    signal.signal(signal.SIGTERM, _on_sigterm)

    if args.test:
        print(f"[Gesture] 테스트 모드 시작 (camera={args.camera}, mac={args.mac})", flush=True)
        print("[Gesture] 한 손: PALM_OPEN / FIST / OK_SIGN / PEACE", flush=True)
        print("[Gesture] 두 손: SPREAD (벌리기) / GATHER (모으기)", flush=True)
        if args.mac:
            print("[Gesture] 맥 제어 활성화: SPREAD→Mission Control / GATHER→닫기 / PALM→전체화면 / FIST→최소화", flush=True)
        print("[Gesture] 'q' 키로 종료", flush=True)
        try:
            ctrl.run_test()
        except KeyboardInterrupt:
            pass
        print("[Gesture] 완료.", flush=True)
    elif args.mac:
        print(f"[Gesture] 맥 제어 모드 시작 (camera={args.camera})", flush=True)
        try:
            ctrl.run_mac()
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
