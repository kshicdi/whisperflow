"""
camera_capture.py - macOS 카메라 캡처 모듈

맥북 내장 카메라 또는 iPhone Continuity Camera로 프레임을 캡처하여
base64 JPEG 문자열로 반환합니다.
"""

import base64
import cv2
import logging

logger = logging.getLogger(__name__)


def list_cameras() -> list:
    """사용 가능한 카메라 목록을 반환합니다.

    Returns:
        list: 사용 가능한 카메라 인덱스 목록 (예: [0, 1])
    """
    available = []
    # macOS에서는 일반적으로 카메라가 0~4 범위에 있음
    for index in range(5):
        cap = cv2.VideoCapture(index)
        if cap.isOpened():
            available.append(index)
            cap.release()
    return available


def capture_frame(camera_index: int = 0, quality: int = 80) -> str | None:
    """카메라에서 프레임 1장을 캡처하여 base64 JPEG 문자열로 반환합니다.

    Args:
        camera_index: 카메라 인덱스 (0 = 맥북 내장, 1 = iPhone Continuity Camera)
        quality: JPEG 압축 품질 (1~100, 기본값 80)

    Returns:
        str: base64 인코딩된 JPEG 이미지 문자열
        None: 캡처 실패 시
    """
    cap = None
    try:
        cap = cv2.VideoCapture(camera_index)
        if not cap.isOpened():
            logger.error("카메라 인덱스 %d 를 열 수 없습니다.", camera_index)
            return None

        # 카메라가 준비될 때까지 몇 프레임 읽어서 안정화
        for _ in range(3):
            cap.read()

        ret, frame = cap.read()
        if not ret or frame is None:
            logger.error("카메라 인덱스 %d 에서 프레임을 읽을 수 없습니다.", camera_index)
            return None

        encode_params = [cv2.IMWRITE_JPEG_QUALITY, quality]
        success, buffer = cv2.imencode(".jpg", frame, encode_params)
        if not success:
            logger.error("JPEG 인코딩에 실패했습니다.")
            return None

        base64_str = base64.b64encode(buffer.tobytes()).decode("utf-8")
        return base64_str

    except Exception as e:
        logger.error("캡처 중 오류 발생: %s", e)
        return None

    finally:
        if cap is not None:
            cap.release()


if __name__ == "__main__":
    print("사용 가능한 카메라:", list_cameras())
    frame = capture_frame(0)
    if frame:
        print(f"Captured: {len(frame)} bytes base64")
    else:
        print("캡처 실패")
