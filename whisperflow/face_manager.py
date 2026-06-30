"""
face_manager.py - 얼굴 인식/등록 관리 모듈

인물 DB는 ~/.whisperflow/faces/ 에 JSON으로 저장.
각 인물의 사진은 ~/.whisperflow/faces/{name}/ 디렉토리에 JPEG로 저장.

Usage (standalone):
    python -m whisperflow.face_manager
    python -m whisperflow.face_manager --test
    python -m whisperflow.face_manager --test --camera 1
    python -m whisperflow.face_manager --web
    python -m whisperflow.face_manager --web --camera 1
"""

import base64
import json
import logging
import os
import shutil
import time
from datetime import datetime
from pathlib import Path

import numpy as np

logger = logging.getLogger(__name__)


class FaceManager:
    """얼굴 등록/인식/관리 클래스 (InsightFace + FER 기반).

    인물 DB 구조 (faces_db.json):
    {
        "people": {
            "<name>": {
                "name": str,
                "encodings": [[512-dim float], ...],
                "registered_at": "ISO8601 string",
                "description": str
            },
            ...
        }
    }
    """

    DB_FILENAME = "faces_db.json"

    def __init__(self, db_dir: str = "~/.whisperflow/faces"):
        self.db_dir = Path(db_dir).expanduser()
        self.db_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.db_dir / self.DB_FILENAME

        # InsightFace 초기화 (한 번만 로드)
        from insightface.app import FaceAnalysis
        self._face_app = FaceAnalysis(name='buffalo_l', providers=['CPUExecutionProvider'])
        self._face_app.prepare(ctx_id=0, det_size=(640, 640))

        # FER 감정 인식 초기화 (한 번만 로드)
        from fer.fer import FER
        self._fer = FER(mtcnn=False)

        # 인물별 인코딩을 메모리에 캐시 (numpy array 형태)
        self._people: dict[str, dict] = {}
        self._load_db()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def register(self, image_b64: str, name: str, description: str = "", face_index: int = 0) -> dict:
        """이미지에서 얼굴 인코딩 추출 후 이름과 함께 저장.

        동일 이름으로 재등록하면 기존 인코딩에 추가(앙상블 방식).

        Args:
            image_b64: base64 인코딩된 JPEG/PNG 이미지 문자열
            name: 등록할 인물 이름
            description: 설명 (선택)
            face_index: 여러 얼굴 중 등록할 얼굴 번호 (0-based, 기본 0)

        Returns:
            {"success": bool, "message": str, "face_count": int}
        """
        name = name.strip()
        if not name:
            return {"success": False, "message": "이름을 입력해주세요.", "face_count": 0}

        # base64 → numpy 이미지
        image = _b64_to_rgb_array(image_b64)
        if image is None:
            return {"success": False, "message": "이미지 디코딩에 실패했습니다.", "face_count": 0}

        # InsightFace로 얼굴 감지+인코딩 추출
        faces = self._face_app.get(image)
        if not faces:
            return {"success": False, "message": "이미지에서 얼굴을 찾을 수 없습니다.", "face_count": 0}
        if face_index < 0 or face_index >= len(faces):
            return {
                "success": False,
                "message": f"face_index={face_index} 범위 밖입니다. 감지된 얼굴: {len(faces)}개 (0~{len(faces)-1})",
                "face_count": len(faces),
            }

        new_encoding = faces[face_index].embedding  # shape (512,)

        # 기존 인물이면 인코딩 추가, 없으면 신규 생성
        if name in self._people:
            person = self._people[name]
            person["encodings"].append(new_encoding)
            action = "추가"
        else:
            person = {
                "name": name,
                "encodings": [new_encoding],
                "registered_at": datetime.now().isoformat(),
                "description": description,
            }
            self._people[name] = person
            action = "등록"

        # 사진 저장
        photo_path = self._save_photo(image_b64, name)
        logger.debug("[FaceManager] 사진 저장: %s", photo_path)

        self._save_db()

        total_encodings = len(person["encodings"])
        return {
            "success": True,
            "message": f"'{name}' {action} 완료 (인코딩 누적 {total_encodings}개)",
            "face_count": total_encodings,
        }

    def recognize(self, image_b64: str, tolerance: float = 0.4) -> list:
        """이미지에서 얼굴을 찾고 등록된 인물과 매칭.

        Args:
            image_b64: base64 인코딩된 JPEG/PNG 이미지 문자열
            tolerance: 코사인 유사도 임계값 (이 값 이상이면 같은 사람, 기본 0.4)

        Returns:
            [
                {
                    "name": str,          # 등록된 이름 또는 "unknown"
                    "confidence": float,   # 코사인 유사도 (0~1)
                    "location": (top, right, bottom, left),
                    "emotion": str,
                    "emotion_score": float,
                    "age": int,
                    "gender": str
                },
                ...
            ]
        """
        image = _b64_to_rgb_array(image_b64)
        if image is None:
            return []

        return self.recognize_frame(image, tolerance=tolerance)

    def recognize_frame(self, rgb_image: "np.ndarray", tolerance: float = 0.4) -> list:
        """RGB numpy array에서 직접 얼굴 인식 (base64 변환 불필요).

        run_test() 등 OpenCV 파이프라인에서 매 프레임마다 호출할 때 사용.
        반환 형식은 recognize()와 동일.
        """
        if rgb_image is None or rgb_image.size == 0:
            return []

        # InsightFace로 얼굴 감지 + 인코딩 + 나이/성별 한 번에 추출
        faces = self._face_app.get(rgb_image)
        if not faces:
            return []

        # 등록된 인물 목록 구성
        known_names: list[str] = []
        known_encodings: list[np.ndarray] = []
        for person in self._people.values():
            for enc in person["encodings"]:
                known_names.append(person["name"])
                known_encodings.append(enc)

        results = []
        for face in faces:
            # bbox → (top, right, bottom, left) 형식으로 변환
            x1, y1, x2, y2 = face.bbox.astype(int)
            location = (int(y1), int(x2), int(y2), int(x1))  # (top, right, bottom, left)

            embedding = face.embedding  # 512-dim
            age = int(face.age) if face.age is not None else 0
            gender = 'M' if face.gender == 1 else 'F'

            # 감정 인식: InsightFace bbox 기반으로 얼굴 crop 후 FER 수행
            emotion = "neutral"
            emotion_score = 0.0
            try:
                h, w = rgb_image.shape[:2]
                crop_y1 = max(0, int(y1))
                crop_y2 = min(h, int(y2))
                crop_x1 = max(0, int(x1))
                crop_x2 = min(w, int(x2))
                face_crop = rgb_image[crop_y1:crop_y2, crop_x1:crop_x2]
                if face_crop.size > 0:
                    fer_results = self._fer.detect_emotions(face_crop)
                    if fer_results:
                        emotions = fer_results[0].get("emotions", {})
                        if emotions:
                            top_emotion = max(emotions, key=emotions.get)
                            emotion = top_emotion
                            emotion_score = round(float(emotions[top_emotion]), 4)
            except Exception as e:
                logger.debug("[FaceManager] 감정 인식 실패: %s", e)

            # 랜드마크 추출 (코사인 매칭 전에 공통으로 사용)
            _landmarks = None
            if hasattr(face, 'landmark_2d_106') and face.landmark_2d_106 is not None:
                _landmarks = face.landmark_2d_106
            elif hasattr(face, 'landmark_3d_68') and face.landmark_3d_68 is not None:
                _landmarks = face.landmark_3d_68[:, :2]

            # 코사인 유사도 매칭
            if not known_encodings:
                results.append({
                    "name": "unknown",
                    "confidence": 0.0,
                    "location": location,
                    "emotion": emotion,
                    "emotion_score": emotion_score,
                    "age": age,
                    "gender": gender,
                    "landmarks": _landmarks,
                })
                continue

            # 코사인 유사도 계산
            similarities = []
            for known_enc in known_encodings:
                sim = np.dot(embedding, known_enc) / (
                    np.linalg.norm(embedding) * np.linalg.norm(known_enc)
                )
                similarities.append(float(sim))

            best_idx = int(np.argmax(similarities))
            best_similarity = similarities[best_idx]

            if best_similarity >= tolerance:
                matched_name = known_names[best_idx]
                confidence = round(best_similarity, 4)
            else:
                matched_name = "unknown"
                confidence = round(best_similarity, 4)

            results.append({
                "name": matched_name,
                "confidence": confidence,
                "location": location,
                "emotion": emotion,
                "emotion_score": emotion_score,
                "age": age,
                "gender": gender,
                "landmarks": _landmarks,
            })

        return results

    def list_people(self) -> list:
        """등록된 인물 목록 반환 (등록 시각 오름차순).

        Returns:
            [
                {"name": str, "registered_at": str, "description": str, "encoding_count": int},
                ...
            ]
        """
        people = []
        for person in self._people.values():
            people.append({
                "name": person["name"],
                "registered_at": person["registered_at"],
                "description": person.get("description", ""),
                "encoding_count": len(person["encodings"]),
            })
        people.sort(key=lambda p: p["registered_at"])
        return people

    def delete(self, name: str) -> bool:
        """등록된 인물과 저장된 사진을 삭제.

        Args:
            name: 삭제할 인물 이름

        Returns:
            True if deleted, False if not found
        """
        name = name.strip()
        if name not in self._people:
            logger.warning("[FaceManager] '%s' 인물을 찾을 수 없습니다.", name)
            return False

        del self._people[name]
        self._save_db()

        # 사진 디렉토리 삭제
        photo_dir = self.db_dir / name
        if photo_dir.exists():
            shutil.rmtree(photo_dir)
            logger.debug("[FaceManager] 사진 디렉토리 삭제: %s", photo_dir)

        return True

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _load_db(self):
        """JSON DB 로드. encodings는 numpy array로 변환."""
        if not self.db_path.exists():
            self._people = {}
            return

        try:
            with open(self.db_path, "r", encoding="utf-8") as f:
                data = json.load(f)

            people_raw = data.get("people", {})
            self._people = {}
            for name, person in people_raw.items():
                encodings_raw = person.get("encodings", [])
                encodings = [np.array(enc, dtype=np.float64) for enc in encodings_raw]
                self._people[name] = {
                    "name": person.get("name", name),
                    "encodings": encodings,
                    "registered_at": person.get("registered_at", ""),
                    "description": person.get("description", ""),
                }
            logger.debug("[FaceManager] DB 로드 완료: %d명", len(self._people))
        except (json.JSONDecodeError, KeyError, ValueError) as e:
            logger.error("[FaceManager] DB 로드 실패: %s", e)
            self._people = {}

    def _save_db(self):
        """DB를 JSON으로 저장. numpy array는 list로 변환."""
        people_serializable = {}
        for name, person in self._people.items():
            encodings_list = [
                enc.tolist() if isinstance(enc, np.ndarray) else list(enc)
                for enc in person["encodings"]
            ]
            people_serializable[name] = {
                "name": person["name"],
                "encodings": encodings_list,
                "registered_at": person["registered_at"],
                "description": person.get("description", ""),
            }

        data = {"people": people_serializable}
        tmp_path = self.db_path.with_suffix(".tmp")
        try:
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            os.replace(tmp_path, self.db_path)
            logger.debug("[FaceManager] DB 저장 완료: %s", self.db_path)
        except OSError as e:
            logger.error("[FaceManager] DB 저장 실패: %s", e)
            if tmp_path.exists():
                tmp_path.unlink(missing_ok=True)

    def _save_photo(self, image_b64: str, name: str) -> Path:
        """사진을 인물별 디렉토리에 JPEG로 저장.

        파일명: {timestamp}.jpg (밀리초 단위)
        """
        photo_dir = self.db_dir / name
        photo_dir.mkdir(parents=True, exist_ok=True)

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        photo_path = photo_dir / f"{timestamp}.jpg"

        # base64에 data URI prefix가 있으면 제거
        b64_data = _strip_data_uri(image_b64)
        image_bytes = base64.b64decode(b64_data)

        with open(photo_path, "wb") as f:
            f.write(image_bytes)

        return photo_path


# ------------------------------------------------------------------
# Module-level utilities
# ------------------------------------------------------------------

def _strip_data_uri(b64_str: str) -> str:
    """'data:image/jpeg;base64,...' 형태에서 실제 base64 부분만 추출."""
    if "," in b64_str:
        return b64_str.split(",", 1)[1]
    return b64_str


def _b64_to_rgb_array(image_b64: str) -> "np.ndarray | None":
    """base64 이미지 문자열을 RGB numpy array로 변환.

    face_recognition은 RGB 순서를 요구하므로, OpenCV BGR → RGB 변환 포함.
    """
    try:
        import cv2
    except ImportError:
        # cv2 없이 PIL 폴백
        try:
            import io
            from PIL import Image
            b64_data = _strip_data_uri(image_b64)
            image_bytes = base64.b64decode(b64_data)
            pil_image = Image.open(io.BytesIO(image_bytes)).convert("RGB")
            return np.array(pil_image)
        except Exception as e:
            logger.error("[FaceManager] 이미지 디코딩 실패 (PIL): %s", e)
            return None

    try:
        b64_data = _strip_data_uri(image_b64)
        image_bytes = base64.b64decode(b64_data)
        np_arr = np.frombuffer(image_bytes, dtype=np.uint8)
        bgr_image = cv2.imdecode(np_arr, cv2.IMREAD_COLOR)
        if bgr_image is None:
            logger.error("[FaceManager] cv2.imdecode 실패 — 유효하지 않은 이미지 데이터")
            return None
        rgb_image = cv2.cvtColor(bgr_image, cv2.COLOR_BGR2RGB)
        return rgb_image
    except Exception as e:
        logger.error("[FaceManager] 이미지 디코딩 실패 (cv2): %s", e)
        return None


# ------------------------------------------------------------------
# OpenCV 시각화 테스트
# ------------------------------------------------------------------

def _load_korean_font(size: int = 20):
    """macOS 한글 폰트를 로드. 실패 시 None 반환."""
    try:
        from PIL import ImageFont
        for font_path in (
            "/System/Library/Fonts/AppleSDGothicNeo.ttc",
            "/System/Library/Fonts/Supplemental/AppleGothic.ttf",
            "/Library/Fonts/Arial Unicode.ttf",
        ):
            if os.path.exists(font_path):
                return ImageFont.truetype(font_path, size)
    except Exception:
        pass
    return None


def _put_text_pil(frame, text: str, position: tuple, font, color_rgb: tuple):
    """PIL로 한글 텍스트를 OpenCV 프레임 위에 렌더링."""
    from PIL import Image, ImageDraw
    img_pil = Image.fromarray(frame)
    draw = ImageDraw.Draw(img_pil)
    draw.text(position, text, font=font, fill=color_rgb)
    frame[:] = np.array(img_pil)


def _draw_corner_brackets(frame, left, top, right, bottom, color_bgr, thickness=2):
    """코너 브라켓 (L자 모양 4개 코너) 그리기."""
    import cv2
    w = right - left
    h = bottom - top
    corner_len = max(int(min(w, h) * 0.2), 8)

    # 좌상
    cv2.line(frame, (left, top), (left + corner_len, top), color_bgr, thickness)
    cv2.line(frame, (left, top), (left, top + corner_len), color_bgr, thickness)
    # 우상
    cv2.line(frame, (right, top), (right - corner_len, top), color_bgr, thickness)
    cv2.line(frame, (right, top), (right, top + corner_len), color_bgr, thickness)
    # 좌하
    cv2.line(frame, (left, bottom), (left + corner_len, bottom), color_bgr, thickness)
    cv2.line(frame, (left, bottom), (left, bottom - corner_len), color_bgr, thickness)
    # 우하
    cv2.line(frame, (right, bottom), (right - corner_len, bottom), color_bgr, thickness)
    cv2.line(frame, (right, bottom), (right, bottom - corner_len), color_bgr, thickness)


def _draw_translucent_rect(frame, x1, y1, x2, y2, color_bgr, alpha=0.5):
    """반투명 사각형을 프레임에 오버레이."""
    import cv2
    overlay = frame.copy()
    cv2.rectangle(overlay, (x1, y1), (x2, y2), color_bgr, cv2.FILLED)
    cv2.addWeighted(overlay, alpha, frame, 1 - alpha, 0, frame)


def _emotion_label(emotion: str) -> str:
    """감정명에 이모지 텍스트를 붙여 반환."""
    emoji_map = {
        "happy": "HAPPY :)",
        "sad": "SAD :(",
        "angry": "ANGRY >:",
        "surprise": "SURPRISE :O",
        "neutral": "NEUTRAL :|",
        "fear": "FEAR D:",
        "disgust": "DISGUST :/",
    }
    return emoji_map.get(emotion, emotion.upper())


def _draw_face_mesh(frame, landmarks, color_bgr, alpha=0.3):
    """얼굴 랜드마크를 Delaunay 삼각분할 와이어프레임 메시로 그린다.

    Args:
        frame: BGR OpenCV 프레임 (in-place 수정)
        landmarks: numpy array (N, 2) — 106 또는 68 포인트
        color_bgr: 와이어프레임 색상 (BGR tuple)
        alpha: 오버레이 투명도 (0~1)
    """
    import cv2

    if landmarks is None or len(landmarks) < 3:
        return

    h, w = frame.shape[:2]

    # 정수 좌표로 변환 + 프레임 범위 클리핑
    points = []
    for pt in landmarks:
        x = int(np.clip(pt[0], 0, w - 1))
        y = int(np.clip(pt[1], 0, h - 1))
        points.append((x, y))

    # Delaunay 삼각분할
    rect = (0, 0, w, h)
    subdiv = cv2.Subdiv2D(rect)
    for pt in points:
        try:
            subdiv.insert(pt)
        except cv2.error:
            pass  # 프레임 경계 바깥 포인트 무시

    triangles = subdiv.getTriangleList()
    overlay = frame.copy()

    for tri in triangles:
        x1, y1, x2, y2, x3, y3 = tri.astype(int)
        # 프레임 범위 내의 삼각형만 그리기
        if (0 <= x1 < w and 0 <= y1 < h and
            0 <= x2 < w and 0 <= y2 < h and
            0 <= x3 < w and 0 <= y3 < h):
            cv2.line(overlay, (x1, y1), (x2, y2), color_bgr, 1, cv2.LINE_AA)
            cv2.line(overlay, (x2, y2), (x3, y3), color_bgr, 1, cv2.LINE_AA)
            cv2.line(overlay, (x3, y3), (x1, y1), color_bgr, 1, cv2.LINE_AA)

    # 랜드마크 포인트 점 찍기
    for pt in points:
        cv2.circle(overlay, pt, 1, color_bgr, -1, cv2.LINE_AA)

    # 반투명 합성
    cv2.addWeighted(overlay, alpha, frame, 1 - alpha, 0, frame)


def run_test(camera_index: int = 0):
    """카메라 실시간 영상에서 얼굴 인식 — JARVIS HUD 스타일 시각화.

    - 코너 브라켓 바운딩 박스 (등록=시안, unknown=빨간)
    - 정보 패널: 이름, confidence 바, 감정, 나이/성별
    - 상단/하단 HUD 바
    - 'r' 키: 현재 프레임의 얼굴을 등록
    - 'q' 키: 종료
    """
    try:
        import cv2
    except ImportError:
        print("[FaceTest] opencv-python이 설치되지 않았습니다. pip install opencv-python", flush=True)
        return

    fm = FaceManager()
    people = fm.list_people()
    num_people = len(people)
    print(f"[FaceTest] 등록된 인물: {num_people}명", flush=True)
    for p in people:
        print(f"  - {p['name']} (인코딩 {p['encoding_count']}개)", flush=True)

    # 한글 폰트 로드 (여러 사이즈)
    font_title = _load_korean_font(16)
    font_label = _load_korean_font(18)
    font_info = _load_korean_font(14)
    font_hud = _load_korean_font(13)
    use_pil = font_label is not None
    if use_pil:
        print("[FaceTest] 한글 폰트 로드 성공.", flush=True)
    else:
        print("[FaceTest] 한글 폰트 없음 — 영문만 표시.", flush=True)

    print(f"[FaceTest] 카메라 {camera_index} 열기 시도...", flush=True)
    cap = cv2.VideoCapture(camera_index)
    if not cap.isOpened():
        print(f"[FaceTest] 카메라 {camera_index}를 열 수 없습니다.", flush=True)
        return

    cap.set(cv2.CAP_PROP_FRAME_WIDTH, 1280)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 720)
    print("[FaceTest] 카메라 연결됨. 'r': 등록 | 'q': 종료", flush=True)

    # YOLO 객체 감지 모델 로드 (lazy — run_test 안에서만)
    try:
        from ultralytics import YOLO
        yolo_model = YOLO('yolo11s.pt')
        print("[FaceTest] YOLO11 small 모델 로드 완료.", flush=True)
    except ImportError:
        yolo_model = None
        print("[FaceTest] ultralytics 미설치 — 객체 감지 비활성화.", flush=True)

    # 성능: N프레임마다 인식 수행
    RECOGNIZE_EVERY_N = 5
    frame_count = 0
    last_results: list = []
    last_objects: list = []  # YOLO 감지 결과 캐시

    # FPS 계산용
    fps = 0.0
    fps_update_interval = 0.5
    fps_frame_counter = 0
    fps_timer = time.monotonic()

    # JARVIS 색상 정의 (BGR)
    CYAN_BGR = (255, 255, 0)       # #00FFFF in BGR
    RED_BGR = (0x44, 0x44, 0xFF)   # #FF4444 in BGR
    BLACK_BGR = (0, 0, 0)
    CYAN_RGB = (0, 255, 255)
    RED_RGB = (255, 68, 68)
    WHITE_RGB = (255, 255, 255)
    GREEN_RGB = (0, 255, 100)
    DIM_CYAN_RGB = (0, 180, 180)
    OBJ_GREEN_BGR = (102, 255, 102)  # #66FF66 in BGR
    OBJ_GREEN_RGB = (102, 255, 102)

    # 등록 모드 플래그
    register_mode = False
    register_frame = None
    register_results = []  # 등록 모드 진입 시의 인식 결과 (번호 표시용)

    while True:
        if not register_mode:
            ret, frame = cap.read()
            if not ret or frame is None:
                print("[FaceTest] 프레임 읽기 실패.", flush=True)
                break
        else:
            # 등록 모드: freeze된 프레임 사용
            frame = register_frame.copy()

        frame_count += 1
        fps_frame_counter += 1

        # FPS 갱신
        now = time.monotonic()
        elapsed = now - fps_timer
        if elapsed >= fps_update_interval:
            fps = fps_frame_counter / elapsed
            fps_frame_counter = 0
            fps_timer = now

        h_frame, w_frame = frame.shape[:2]

        # N프레임마다 얼굴 인식 + 객체 감지 수행 (등록 모드가 아닐 때만)
        if not register_mode and (frame_count % RECOGNIZE_EVERY_N == 1 or RECOGNIZE_EVERY_N == 1):
            frame_rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            last_results = fm.recognize_frame(frame_rgb)

            # YOLO 객체 감지 (동일 프레임에서 한 번만)
            if yolo_model is not None:
                yolo_results = yolo_model(frame, verbose=False)
                last_objects = []
                for box in yolo_results[0].boxes:
                    cls_id = int(box.cls[0])
                    cls_name = yolo_results[0].names[cls_id]
                    conf = float(box.conf[0])
                    if conf < 0.5:
                        continue  # 낮은 확신도 필터링
                    if cls_name == 'person':
                        continue  # 사람은 얼굴 인식으로 대체
                    x1, y1, x2, y2 = box.xyxy[0].cpu().numpy().astype(int)
                    last_objects.append({
                        "name": cls_name,
                        "confidence": conf,
                        "bbox": (int(x1), int(y1), int(x2), int(y2)),
                    })

        # --- 스캔라인 이펙트 (5px 간격, 매우 희미하게) ---
        scanline_overlay = np.zeros_like(frame, dtype=np.uint8)
        for y in range(0, h_frame, 5):
            scanline_overlay[y, :] = (30, 30, 30)
        cv2.addWeighted(scanline_overlay, 0.15, frame, 1.0, 0, frame)

        # --- 얼굴 바운딩 박스 + 정보 패널 ---
        pil_texts: list[tuple] = []  # (text, x, y, color_rgb, font)

        for result in last_results:
            top, right, bottom, left = result["location"]
            name = result["name"]
            confidence = result["confidence"]
            emotion = result.get("emotion", "neutral")
            emotion_score = result.get("emotion_score", 0.0)
            age = result.get("age", 0)
            gender = result.get("gender", "?")

            is_known = name != "unknown"
            color_bgr = CYAN_BGR if is_known else RED_BGR
            accent_rgb = CYAN_RGB if is_known else RED_RGB

            # 코너 브라켓
            _draw_corner_brackets(frame, left, top, right, bottom, color_bgr, thickness=2)

            # 와이어프레임 메시 오버레이
            landmarks = result.get("landmarks")
            if landmarks is not None:
                _draw_face_mesh(frame, landmarks, color_bgr, alpha=0.3)

            # --- 정보 패널 (박스 오른쪽) ---
            panel_x = right + 8
            panel_w = 160
            panel_h = 82
            panel_y = top

            # 패널이 프레임 오른쪽을 넘으면 왼쪽에 표시
            if panel_x + panel_w > w_frame:
                panel_x = max(left - panel_w - 8, 0)

            # 패널이 프레임 아래를 넘으면 조정
            if panel_y + panel_h > h_frame:
                panel_y = max(h_frame - panel_h - 4, 0)

            # 반투명 검은 배경
            _draw_translucent_rect(frame, panel_x, panel_y, panel_x + panel_w, panel_y + panel_h, BLACK_BGR, alpha=0.55)

            # 이름
            display_name = name.upper() if is_known else "UNKNOWN"
            pil_texts.append((display_name, panel_x + 6, panel_y + 2, accent_rgb, font_label))

            # Confidence 바
            bar_x = panel_x + 6
            bar_y = panel_y + 24
            bar_w = panel_w - 12
            bar_h = 10
            # 배경 바
            cv2.rectangle(frame, (bar_x, bar_y), (bar_x + bar_w, bar_y + bar_h), (40, 40, 40), cv2.FILLED)
            # 채움 바
            fill_w = int(bar_w * min(confidence, 1.0))
            if fill_w > 0:
                fill_color = (255, 255, 0) if is_known else (0x44, 0x44, 0xFF)
                cv2.rectangle(frame, (bar_x, bar_y), (bar_x + fill_w, bar_y + bar_h), fill_color, cv2.FILLED)
            # 퍼센트 텍스트
            conf_text = f"{confidence * 100:.0f}%"
            pil_texts.append((conf_text, bar_x + bar_w + 2 - 36, bar_y - 2, WHITE_RGB, font_info))

            # 감정
            emo_text = _emotion_label(emotion)
            pil_texts.append((emo_text, panel_x + 6, panel_y + 40, DIM_CYAN_RGB, font_info))

            # 나이/성별
            age_gender_text = f"{gender} / {age}"
            pil_texts.append((age_gender_text, panel_x + 6, panel_y + 58, DIM_CYAN_RGB, font_info))

        # --- YOLO 객체 바운딩 박스 ---
        for obj in last_objects:
            ox1, oy1, ox2, oy2 = obj["bbox"]
            obj_conf = obj["confidence"]
            obj_name = obj["name"]

            # 얇은 바운딩 박스
            cv2.rectangle(frame, (ox1, oy1), (ox2, oy2), OBJ_GREEN_BGR, 1)

            # 라벨 텍스트
            label_text = f"{obj_name} {obj_conf:.0%}"
            # 반투명 라벨 배경
            label_w = len(label_text) * 8 + 8
            label_h = 18
            _draw_translucent_rect(frame, ox1, oy1 - label_h, ox1 + label_w, oy1, BLACK_BGR, alpha=0.55)
            pil_texts.append((label_text, ox1 + 4, oy1 - label_h + 1, OBJ_GREEN_RGB, font_hud))

        # --- 상단 HUD 바 ---
        _draw_translucent_rect(frame, 0, 0, w_frame, 28, BLACK_BGR, alpha=0.6)
        pil_texts.append(("JARVIS FACE ID", 10, 4, CYAN_RGB, font_title))
        fps_faces_text = f"FPS: {fps:.1f}  |  FACES: {len(last_results)}"
        pil_texts.append((fps_faces_text, w_frame - 200, 5, GREEN_RGB, font_hud))

        # --- 객체 요약 (상단 HUD 바 아래) ---
        if last_objects:
            unique_names = list(dict.fromkeys(o["name"] for o in last_objects))[:5]
            obj_summary = "OBJECTS: " + ", ".join(unique_names)
            _draw_translucent_rect(frame, 0, 28, w_frame, 48, BLACK_BGR, alpha=0.5)
            pil_texts.append((obj_summary, 10, 30, OBJ_GREEN_RGB, font_hud))

        # --- 하단 HUD 바 ---
        _draw_translucent_rect(frame, 0, h_frame - 26, w_frame, h_frame, BLACK_BGR, alpha=0.6)
        bottom_left_text = f"REGISTERED: {num_people}"
        pil_texts.append((bottom_left_text, 10, h_frame - 22, DIM_CYAN_RGB, font_hud))
        controls_text = "R: REGISTER  |  Q: QUIT"
        pil_texts.append((controls_text, w_frame - 210, h_frame - 22, DIM_CYAN_RGB, font_hud))

        # --- 등록 모드 오버레이 ---
        if register_mode:
            # 각 얼굴에 번호 표시 [1], [2], [3]...
            for idx, reg_result in enumerate(register_results):
                r_top, r_right, r_bottom, r_left = reg_result["location"]
                num_text = f"[{idx + 1}]"
                # 번호 배경
                _draw_translucent_rect(frame, r_left, r_top, r_left + 40, r_top + 32, BLACK_BGR, alpha=0.7)
                pil_texts.append((num_text, r_left + 4, r_top + 2, (255, 200, 0), font_label))

            _draw_translucent_rect(frame, 0, h_frame // 2 - 30, w_frame, h_frame // 2 + 30, BLACK_BGR, alpha=0.7)
            hint_msg = "REGISTER MODE — 터미널에서 '번호 이름' 입력 (예: 1 철수)"
            pil_texts.append((hint_msg, w_frame // 2 - 230, h_frame // 2 - 12, (255, 200, 0), font_label))

        # --- 텍스트 일괄 렌더링 (PIL) ---
        if use_pil and pil_texts:
            from PIL import Image, ImageDraw
            img_pil = Image.fromarray(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))
            draw = ImageDraw.Draw(img_pil)
            for text, x, y, color_rgb, fnt in pil_texts:
                if fnt:
                    draw.text((x, y), text, font=fnt, fill=color_rgb)
                else:
                    draw.text((x, y), text, fill=color_rgb)
            frame[:] = cv2.cvtColor(np.array(img_pil), cv2.COLOR_RGB2BGR)
        else:
            for text, x, y, _, _fnt in pil_texts:
                cv2.putText(frame, text, (x, y + 14),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.45, (255, 255, 255), 1)

        cv2.imshow("JARVIS Face ID", frame)

        # --- 등록 모드 처리 ---
        if register_mode:
            cv2.waitKey(1)
            num_faces = len(register_results)
            # 터미널에서 "번호 이름" 입력 (블로킹)
            try:
                prompt = f"\n[등록] 감지된 얼굴: {num_faces}개 — '번호 이름' 입력 (예: 1 철수) / 빈 칸이면 취소: "
                reg_input = input(prompt).strip()
            except (EOFError, KeyboardInterrupt):
                reg_input = ""

            if not reg_input:
                print("[등록] 취소됨.", flush=True)
            else:
                # 입력 파싱: 첫 토큰이 숫자면 번호, 나머지가 이름
                tokens = reg_input.split(None, 1)
                if len(tokens) >= 2 and tokens[0].isdigit():
                    face_num = int(tokens[0])
                    reg_name = tokens[1].strip()
                elif len(tokens) == 1 and tokens[0].isdigit():
                    print("[등록] 이름을 입력해주세요. (예: 1 철수)", flush=True)
                    register_mode = False
                    register_frame = None
                    register_results = []
                    frame_count = 0
                    continue
                else:
                    # 번호 없이 이름만 — 1번 얼굴로 등록
                    face_num = 1
                    reg_name = reg_input

                face_idx = face_num - 1  # 1-based → 0-based
                if face_idx < 0 or face_idx >= num_faces:
                    print(f"[등록] 번호 {face_num}은 범위 밖입니다. (1~{num_faces})", flush=True)
                else:
                    # 현재 프레임을 base64로 변환하여 register() 호출
                    _, buf = cv2.imencode('.jpg', register_frame)
                    frame_b64 = base64.b64encode(buf).decode('utf-8')
                    result = fm.register(frame_b64, reg_name, face_index=face_idx)
                    print(f"[등록] {result['message']}", flush=True)
                    if result["success"]:
                        num_people = len(fm.list_people())
                        print(f"[등록] 현재 등록 인물: {num_people}명", flush=True)

            register_mode = False
            register_frame = None
            register_results = []
            # 인식 결과 갱신 강제
            frame_count = 0
            continue

        key = cv2.waitKey(1) & 0xFF
        if key == ord('q'):
            print("[FaceTest] 'q' 키 감지. 종료.", flush=True)
            break
        elif key == ord('r'):
            # 등록 모드 진입: 현재 프레임 freeze
            if last_results:
                register_mode = True
                register_frame = frame.copy()
                register_results = list(last_results)
                face_count = len(register_results)
                print(f"\n[등록] 얼굴 {face_count}개가 감지된 프레임을 캡처했습니다.", flush=True)
                for i, r in enumerate(register_results):
                    rname = r["name"]
                    print(f"  [{i+1}] {rname}", flush=True)
            else:
                print("[등록] 현재 프레임에서 얼굴이 감지되지 않았습니다. 얼굴이 보이는 상태에서 다시 시도하세요.", flush=True)

    cap.release()
    cv2.destroyAllWindows()
    print("[FaceTest] 완료.", flush=True)


# ------------------------------------------------------------------
# Web UI mode (WebSocket + HTTP)
# ------------------------------------------------------------------

def run_web(camera_index: int = 0, http_port: int = 9000, ws_port: int = 9001):
    """카메라 + AI 분석 결과를 WebSocket으로 전송하고 웹 UI를 HTTP로 서빙.

    - HTTP 서버: whisperflow/static/ 디렉토리를 http_port에서 서빙
    - WebSocket 서버: ws_port에서 프레임 + 분석 결과를 JSON broadcast
    - 브라우저 자동 열기: http://localhost:{http_port}/face_hud.html
    """
    import asyncio
    import http.server
    import threading
    import webbrowser

    try:
        import cv2
    except ImportError:
        print("[FaceWeb] opencv-python이 설치되지 않았습니다. pip install opencv-python", flush=True)
        return

    try:
        import websockets
    except ImportError:
        print("[FaceWeb] websockets가 설치되지 않았습니다. pip install websockets", flush=True)
        return

    # --- HTTP 서버 (별도 스레드) ---
    static_dir = os.path.join(os.path.dirname(__file__), 'static')
    os.makedirs(static_dir, exist_ok=True)

    class StaticHandler(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=static_dir, **kwargs)

        def log_message(self, format, *args):
            pass  # 로그 억제

    def start_http_server():
        httpd = http.server.HTTPServer(('127.0.0.1', http_port), StaticHandler)
        print(f"[FaceWeb] HTTP 서버 시작: http://localhost:{http_port}/", flush=True)
        httpd.serve_forever()

    http_thread = threading.Thread(target=start_http_server, daemon=True)
    http_thread.start()

    # 브라우저 자동 열기
    webbrowser.open(f"http://localhost:{http_port}/face_hud.html")

    # --- FaceManager + YOLO 초기화 ---
    fm = FaceManager()
    people = fm.list_people()
    num_people = len(people)
    print(f"[FaceWeb] 등록된 인물: {num_people}명", flush=True)

    try:
        from ultralytics import YOLO
        yolo_model = YOLO('yolo11s.pt')
        print("[FaceWeb] YOLO11 small 모델 로드 완료.", flush=True)
    except ImportError:
        yolo_model = None
        print("[FaceWeb] ultralytics 미설치 — 객체 감지 비활성화.", flush=True)

    # --- 카메라 ---
    print(f"[FaceWeb] 카메라 {camera_index} 열기 시도...", flush=True)
    cap = cv2.VideoCapture(camera_index)
    if not cap.isOpened():
        print(f"[FaceWeb] 카메라 {camera_index}를 열 수 없습니다.", flush=True)
        return

    cap.set(cv2.CAP_PROP_FRAME_WIDTH, 1280)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 720)
    print(f"[FaceWeb] 카메라 연결됨. WebSocket: ws://localhost:{ws_port}/", flush=True)

    # --- WebSocket ---
    connected_clients: set = set()

    # 등록 요청을 처리하기 위해 최근 프레임 + 결과를 캐시
    latest_frame_bgr = None
    latest_face_results: list = []

    async def ws_handler(websocket):
        connected_clients.add(websocket)
        try:
            async for msg in websocket:
                try:
                    data = json.loads(msg)
                except (json.JSONDecodeError, TypeError):
                    continue

                if data.get("type") == "register":
                    face_index = data.get("face_index", 0)
                    name = data.get("name", "").strip()
                    if not name:
                        resp = {"type": "register_result", "success": False, "message": "이름을 입력해주세요."}
                    elif latest_frame_bgr is None:
                        resp = {"type": "register_result", "success": False, "message": "프레임이 아직 없습니다."}
                    else:
                        _, buf = cv2.imencode('.jpg', latest_frame_bgr)
                        frame_b64 = base64.b64encode(buf).decode('utf-8')
                        result = fm.register(frame_b64, name, face_index=face_index)
                        resp = {"type": "register_result", "success": result["success"], "message": result["message"]}
                    await websocket.send(json.dumps(resp, ensure_ascii=False))
        except websockets.exceptions.ConnectionClosed:
            pass
        finally:
            connected_clients.discard(websocket)

    async def camera_loop():
        nonlocal latest_frame_bgr, latest_face_results, connected_clients

        RECOGNIZE_EVERY_N = 3
        frame_count = 0
        last_results: list = []
        last_objects: list = []

        # FPS 계산
        fps = 0.0
        fps_update_interval = 0.5
        fps_frame_counter = 0
        fps_timer = time.monotonic()

        while True:
            ret, frame = cap.read()
            if not ret or frame is None:
                await asyncio.sleep(0.1)
                continue

            frame_count += 1
            fps_frame_counter += 1

            now = time.monotonic()
            elapsed = now - fps_timer
            if elapsed >= fps_update_interval:
                fps = fps_frame_counter / elapsed
                fps_frame_counter = 0
                fps_timer = now

            # N프레임마다 AI 분석
            if frame_count % RECOGNIZE_EVERY_N == 1 or RECOGNIZE_EVERY_N == 1:
                frame_rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                last_results = fm.recognize_frame(frame_rgb)
                latest_face_results = last_results

                # YOLO 객체 감지
                if yolo_model is not None:
                    yolo_results = yolo_model(frame, verbose=False)
                    last_objects = []
                    for box in yolo_results[0].boxes:
                        cls_id = int(box.cls[0])
                        cls_name = yolo_results[0].names[cls_id]
                        conf = float(box.conf[0])
                        if conf < 0.5:
                            continue
                        if cls_name == 'person':
                            continue
                        x1, y1, x2, y2 = box.xyxy[0].cpu().numpy().astype(int)
                        last_objects.append({
                            "name": cls_name,
                            "confidence": round(float(conf), 4),
                            "bbox": [int(x1), int(y1), int(x2), int(y2)],
                        })

            latest_frame_bgr = frame

            # JPEG 인코딩 (quality 60)
            encode_params = [cv2.IMWRITE_JPEG_QUALITY, 60]
            _, jpeg_buf = cv2.imencode('.jpg', frame, encode_params)
            frame_b64 = base64.b64encode(jpeg_buf).decode('utf-8')

            # faces 직렬화
            faces_payload = []
            for r in last_results:
                landmarks_raw = r.get("landmarks")
                if landmarks_raw is not None:
                    try:
                        landmarks_list = np.array(landmarks_raw).tolist()
                    except Exception:
                        landmarks_list = []
                else:
                    landmarks_list = []

                faces_payload.append({
                    "name": r["name"],
                    "confidence": r["confidence"],
                    "location": list(r["location"]),
                    "emotion": r.get("emotion", "neutral"),
                    "emotion_score": r.get("emotion_score", 0.0),
                    "age": r.get("age", 0),
                    "gender": r.get("gender", "?"),
                    "landmarks": landmarks_list,
                })

            # objects 직렬화
            objects_payload = []
            for obj in last_objects:
                objects_payload.append({
                    "name": obj["name"],
                    "confidence": obj["confidence"],
                    "bbox": list(obj["bbox"]),
                })

            message = json.dumps({
                "type": "frame_update",
                "frame": f"data:image/jpeg;base64,{frame_b64}",
                "faces": faces_payload,
                "objects": objects_payload,
                "fps": round(fps, 1),
                "registered_count": len(fm.list_people()),
            }, ensure_ascii=False)

            # broadcast
            if connected_clients:
                dead = set()
                for ws in connected_clients:
                    try:
                        await ws.send(message)
                    except websockets.exceptions.ConnectionClosed:
                        dead.add(ws)
                connected_clients -= dead

            await asyncio.sleep(0.033)  # ~30fps

    async def run_all():
        async with websockets.serve(ws_handler, '0.0.0.0', ws_port):
            print(f"[FaceWeb] WebSocket 서버 시작: ws://localhost:{ws_port}/", flush=True)
            await camera_loop()

    try:
        asyncio.run(run_all())
    except KeyboardInterrupt:
        print("\n[FaceWeb] 종료.", flush=True)
    finally:
        cap.release()


# ------------------------------------------------------------------
# Standalone entry
# ------------------------------------------------------------------

def main():
    import argparse
    logging.basicConfig(level=logging.DEBUG, format="%(levelname)s %(message)s")

    parser = argparse.ArgumentParser(
        description="FaceManager — 얼굴 등록/인식 관리 + 시각화 테스트"
    )
    parser.add_argument("--test", action="store_true", help="OpenCV 시각화 테스트 모드")
    parser.add_argument("--web", action="store_true", help="웹 UI 모드 (브라우저에서 JARVIS HUD)")
    parser.add_argument("--camera", type=int, default=0, help="카메라 인덱스 (기본 0)")
    args = parser.parse_args()

    if args.web:
        run_web(camera_index=args.camera)
    elif args.test:
        run_test(camera_index=args.camera)
    else:
        fm = FaceManager()
        people = fm.list_people()
        print("등록된 인물:", people if people else "(없음)")
        print(f"DB 경로: {fm.db_path}")
        print(f"사진 디렉토리: {fm.db_dir}")


if __name__ == "__main__":
    main()
