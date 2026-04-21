"""
face_manager.py - 얼굴 인식/등록 관리 모듈

인물 DB는 ~/.whisperflow/faces/ 에 JSON으로 저장.
각 인물의 사진은 ~/.whisperflow/faces/{name}/ 디렉토리에 JPEG로 저장.

Usage (standalone):
    python -m whisperflow.face_manager
"""

import base64
import json
import logging
import os
import shutil
from datetime import datetime
from pathlib import Path

import numpy as np

logger = logging.getLogger(__name__)


class FaceManager:
    """얼굴 등록/인식/관리 클래스.

    인물 DB 구조 (faces_db.json):
    {
        "people": {
            "<name>": {
                "name": str,
                "encodings": [[128-dim float], ...],
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
        # 인물별 인코딩을 메모리에 캐시 (numpy array 형태)
        self._people: dict[str, dict] = {}
        self._load_db()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def register(self, image_b64: str, name: str, description: str = "") -> dict:
        """이미지에서 얼굴 인코딩 추출 후 이름과 함께 저장.

        동일 이름으로 재등록하면 기존 인코딩에 추가(앙상블 방식).

        Args:
            image_b64: base64 인코딩된 JPEG/PNG 이미지 문자열
            name: 등록할 인물 이름
            description: 설명 (선택)

        Returns:
            {"success": bool, "message": str, "face_count": int}
        """
        try:
            import face_recognition
        except ImportError:
            return {
                "success": False,
                "message": "face_recognition 패키지가 설치되지 않았습니다. pip install face_recognition",
                "face_count": 0,
            }

        name = name.strip()
        if not name:
            return {"success": False, "message": "이름을 입력해주세요.", "face_count": 0}

        # base64 → numpy 이미지
        image = _b64_to_rgb_array(image_b64)
        if image is None:
            return {"success": False, "message": "이미지 디코딩에 실패했습니다.", "face_count": 0}

        # 얼굴 인코딩 추출
        encodings = face_recognition.face_encodings(image)
        if not encodings:
            return {"success": False, "message": "이미지에서 얼굴을 찾을 수 없습니다.", "face_count": 0}
        if len(encodings) > 1:
            return {
                "success": False,
                "message": f"이미지에서 얼굴이 {len(encodings)}개 감지되었습니다. 1명만 있는 사진을 사용해주세요.",
                "face_count": len(encodings),
            }

        new_encoding = encodings[0]  # shape (128,)

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

    def recognize(self, image_b64: str, tolerance: float = 0.6) -> list:
        """이미지에서 얼굴을 찾고 등록된 인물과 매칭.

        Args:
            image_b64: base64 인코딩된 JPEG/PNG 이미지 문자열
            tolerance: 거리 임계값 (낮을수록 엄격, 기본 0.6)

        Returns:
            [
                {
                    "name": str,          # 등록된 이름 또는 "unknown"
                    "confidence": float,   # 유사도 (1.0 - 거리, 0~1)
                    "location": (top, right, bottom, left)
                },
                ...
            ]
        """
        try:
            import face_recognition
        except ImportError:
            logger.error("face_recognition 패키지가 없습니다.")
            return []

        image = _b64_to_rgb_array(image_b64)
        if image is None:
            return []

        face_locations = face_recognition.face_locations(image)
        if not face_locations:
            return []

        face_encodings = face_recognition.face_encodings(image, face_locations)

        # 등록된 인물 목록 구성
        known_names: list[str] = []
        known_encodings: list[np.ndarray] = []
        for person in self._people.values():
            for enc in person["encodings"]:
                known_names.append(person["name"])
                known_encodings.append(enc)

        results = []
        for encoding, location in zip(face_encodings, face_locations):
            if not known_encodings:
                results.append({"name": "unknown", "confidence": 0.0, "location": location})
                continue

            # 각 등록 인코딩과의 거리 계산
            distances = face_recognition.face_distance(known_encodings, encoding)
            best_idx = int(np.argmin(distances))
            best_distance = float(distances[best_idx])

            if best_distance <= tolerance:
                matched_name = known_names[best_idx]
                confidence = round(1.0 - best_distance, 4)
            else:
                matched_name = "unknown"
                confidence = round(1.0 - best_distance, 4)

            results.append({
                "name": matched_name,
                "confidence": confidence,
                "location": location,
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
# Standalone test
# ------------------------------------------------------------------

if __name__ == "__main__":
    logging.basicConfig(level=logging.DEBUG, format="%(levelname)s %(message)s")

    fm = FaceManager()
    people = fm.list_people()
    print("등록된 인물:", people if people else "(없음)")
    print(f"DB 경로: {fm.db_path}")
    print(f"사진 디렉토리: {fm.db_dir}")
