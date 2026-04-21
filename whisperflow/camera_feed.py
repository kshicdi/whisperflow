"""
camera_feed.py - Camera live feed capture and JARVIS WebSocket streaming.

Captures frames from a camera (built-in or iPhone via Continuity Camera) using
OpenCV and sends them to the JARVIS WebSocket server.

Usage (standalone):
    python -m whisperflow.camera_feed
    python -m whisperflow.camera_feed --camera 1   # iPhone (Continuity Camera)
    python -m whisperflow.camera_feed --camera 0 --fps 10
"""

import asyncio
import base64
import json
import threading
import time


class CameraFeed:
    def __init__(self, camera_index=0, ws_url="ws://localhost:8767", fps=5):
        self.camera_index = camera_index
        self.ws_url = ws_url
        self.interval = 1.0 / fps
        self._running = False
        self._thread = None
        self._loop = None
        self._current_frame_b64 = None  # 최신 프레임 (분석용)
        self._frame_lock = threading.Lock()
        self._cap = None  # 카메라 객체 참조 (stop 시 해제용)
        self._cap_lock = threading.Lock()

    def start(self):
        """Start capture loop in background thread."""
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()

    def stop(self):
        """Stop capture and release camera."""
        self._running = False
        # 카메라 즉시 해제
        with self._cap_lock:
            if self._cap is not None:
                self._cap.release()
                self._cap = None
        if self._loop and self._loop.is_running():
            self._loop.call_soon_threadsafe(self._loop.stop)
        if self._thread:
            self._thread.join(timeout=5)
            self._thread = None

    def get_current_frame(self) -> str | None:
        """Return the latest frame as base64 JPEG string (for analysis use)."""
        with self._frame_lock:
            return self._current_frame_b64

    def _run_loop(self):
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._capture_loop())
        except Exception as e:
            print(f"[CameraFeed] 루프 에러: {e}", flush=True)
            import traceback
            traceback.print_exc()
        finally:
            self._loop.close()

    async def _capture_loop(self):
        """Main capture and send loop with auto-reconnect."""
        try:
            import cv2
        except ImportError:
            print("[CameraFeed] opencv-python 패키지가 설치되지 않았습니다. pip install opencv-python", flush=True)
            return

        try:
            import websockets
        except ImportError:
            print("[CameraFeed] websockets 패키지가 설치되지 않았습니다. pip install websockets", flush=True)
            return

        _standalone = _is_standalone()

        while self._running:
            # 카메라 열기
            with self._cap_lock:
                if self._cap is None or not self._cap.isOpened():
                    if _standalone:
                        print(f"[CameraFeed] 카메라 {self.camera_index} 열기 시도...", flush=True)
                    self._cap = cv2.VideoCapture(self.camera_index)
                    if not self._cap.isOpened():
                        if _standalone:
                            print(f"[CameraFeed] 카메라 {self.camera_index}를 열 수 없습니다. 재시도...", flush=True)
                        self._cap = None
                        await asyncio.sleep(3)
                        continue
                    if _standalone:
                        print(f"[CameraFeed] 카메라 {self.camera_index} 연결됨.", flush=True)
            cap = self._cap

            # JARVIS WebSocket 연결
            try:
                async with websockets.connect(self.ws_url, max_size=10 * 1024 * 1024) as jarvis_ws:
                    if _standalone:
                        print(f"[CameraFeed] WebSocket connected to {self.ws_url}", flush=True)
                        print("[CameraFeed] Streaming... (Ctrl+C to stop)", flush=True)

                    while self._running:
                        loop_start = asyncio.get_event_loop().time()

                        # 프레임 캡처 (blocking → run_in_executor)
                        loop = asyncio.get_event_loop()
                        ret, frame = await loop.run_in_executor(None, cap.read)

                        if not ret or frame is None:
                            if _standalone:
                                print("[CameraFeed] 프레임 읽기 실패. 카메라 재연결 시도...", flush=True)
                            # 카메라 닫고 재시도
                            with self._cap_lock:
                                if self._cap is not None:
                                    self._cap.release()
                                    self._cap = None
                            break

                        # JPEG 인코딩 (quality 50)
                        encode_param = [int(cv2.IMWRITE_JPEG_QUALITY), 50]
                        _, buffer = cv2.imencode(".jpg", frame, encode_param)
                        b64_data = base64.b64encode(buffer).decode("utf-8")

                        # 최신 프레임 저장 (분석용)
                        with self._frame_lock:
                            self._current_frame_b64 = b64_data

                        # JARVIS로 전송
                        payload = json.dumps({
                            "type": "camera_frame",
                            "value": b64_data,
                        })
                        await jarvis_ws.send(payload)

                        # FPS 유지
                        elapsed = asyncio.get_event_loop().time() - loop_start
                        sleep_time = max(0, self.interval - elapsed)
                        await asyncio.sleep(sleep_time)

            except (OSError, websockets.exceptions.WebSocketException) as e:
                if _standalone:
                    print(f"[CameraFeed] JARVIS WebSocket 연결 실패: {e}. 재연결 중...", flush=True)
                await asyncio.sleep(2)

        # 종료 시 카메라 해제
        with self._cap_lock:
            if self._cap is not None and self._cap.isOpened():
                self._cap.release()
                if _standalone:
                    print("[CameraFeed] 카메라 해제.", flush=True)
                self._cap = None


_running_as_main = False


def _is_standalone():
    return _running_as_main


def main():
    """Standalone entry point."""
    global _running_as_main
    _running_as_main = True

    import argparse

    parser = argparse.ArgumentParser(description="CameraFeed — JARVIS WebSocket 카메라 스트리머")
    parser.add_argument("--camera", type=int, default=0, help="카메라 인덱스 (기본 0, 아이폰은 1)")
    parser.add_argument("--ws-url", type=str, default="ws://localhost:8767", help="JARVIS WebSocket URL")
    parser.add_argument("--fps", type=float, default=5, help="목표 FPS (기본 5)")
    args = parser.parse_args()

    feed = CameraFeed(camera_index=args.camera, ws_url=args.ws_url, fps=args.fps)
    print(f"[CameraFeed] 시작 (camera={args.camera}, fps={args.fps}, ws={args.ws_url})", flush=True)
    feed.start()

    try:
        while True:
            time.sleep(0.5)
    except KeyboardInterrupt:
        print("\n[CameraFeed] 종료 중...", flush=True)
        feed.stop()
        print("[CameraFeed] 완료.", flush=True)


if __name__ == "__main__":
    main()
