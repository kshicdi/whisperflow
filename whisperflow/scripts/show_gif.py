"""
GIF 플로팅 뷰어 — OpenCV로 항상 최상단 창에 애니메이션 표시.
제스처 컨트롤(POINT + 핀치)로 드래그 가능.

Usage:
    python whisperflow/scripts/show_gif.py              # ~/Desktop/10k_thanks.gif
    python whisperflow/scripts/show_gif.py /path/to.gif
"""
import sys
import time
from pathlib import Path

import cv2
import numpy as np
from PIL import Image

GIF_PATH = sys.argv[1] if len(sys.argv) > 1 else str(Path.home() / "Desktop" / "10k_thanks.gif")
WIN_NAME = "10K 팔로우 감사합니다 🎉  [Q/ESC 종료]"


def load_gif_frames(path: str):
    """PIL로 GIF 프레임 로드 → BGR numpy 배열 리스트."""
    img = Image.open(path)
    frames = []
    delays = []
    try:
        while True:
            frame_rgb = img.copy().convert("RGB")
            frames.append(cv2.cvtColor(np.array(frame_rgb), cv2.COLOR_RGB2BGR))
            delays.append(img.info.get("duration", 50) / 1000.0)
            img.seek(img.tell() + 1)
    except EOFError:
        pass
    return frames, delays


def main():
    frames, delays = load_gif_frames(GIF_PATH)
    if not frames:
        print(f"[ERROR] GIF 로드 실패: {GIF_PATH}", file=sys.stderr)
        sys.exit(1)

    cv2.namedWindow(WIN_NAME, cv2.WINDOW_NORMAL | cv2.WINDOW_KEEPRATIO)
    cv2.resizeWindow(WIN_NAME, 600, 600)
    # macOS에서 항상 최상단 플래그 (WINDOW_GUI_NORMAL 대신 workaround)
    cv2.setWindowProperty(WIN_NAME, cv2.WND_PROP_TOPMOST, 1)

    idx = 0
    print(f"[GIF Viewer] {len(frames)} frames 로드 완료. Q 또는 ESC로 종료.")
    while True:
        cv2.imshow(WIN_NAME, frames[idx])
        delay_ms = max(1, int(delays[idx] * 1000))
        key = cv2.waitKey(delay_ms) & 0xFF
        if key in (ord("q"), ord("Q"), 27):  # Q or ESC
            break
        idx = (idx + 1) % len(frames)

    cv2.destroyAllWindows()


if __name__ == "__main__":
    main()
