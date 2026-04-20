"""
browser_feed.py - Chrome CDP screenshot capture and JARVIS WebSocket streaming.

Captures Chrome browser screenshots via Chrome DevTools Protocol (CDP) and
sends them to the JARVIS WebSocket server.

Usage (standalone):
    python -m whisperflow.browser_feed

Chrome must be running with remote debugging enabled:
    /Applications/Google Chrome.app/Contents/MacOS/Google Chrome --remote-debugging-port=9222
"""

import asyncio
import base64
import json
import threading
import time
import urllib.error
import urllib.request


class BrowserFeed:
    def __init__(self, cdp_port=9222, ws_url="ws://localhost:8767", fps=2):
        self.cdp_port = cdp_port
        self.ws_url = ws_url
        self.interval = 1.0 / fps
        self._running = False
        self._thread = None
        self._loop = None

    def start(self):
        """Start capture loop in background thread."""
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()

    def stop(self):
        """Stop capture."""
        self._running = False
        if self._loop and self._loop.is_running():
            self._loop.call_soon_threadsafe(self._loop.stop)
        if self._thread:
            self._thread.join(timeout=5)
            self._thread = None

    def _run_loop(self):
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._capture_loop())
        except Exception as e:
            print(f"[BrowserFeed] 루프 에러: {e}", flush=True)
            import traceback
            traceback.print_exc()
        finally:
            self._loop.close()

    def _get_cdp_ws_url(self):
        """Get the WebSocket debugger URL from Chrome CDP HTTP endpoint."""
        import subprocess
        try:
            result = subprocess.run(
                ["curl", "-s", f"http://127.0.0.1:{self.cdp_port}/json"],
                capture_output=True, text=True, timeout=5
            )
            if result.returncode != 0:
                return None
            tabs = json.loads(result.stdout)
            # Find the first page tab
            for tab in tabs:
                if tab.get("type") == "page":
                    ws_url = tab.get("webSocketDebuggerUrl")
                    if ws_url:
                        return ws_url.replace("localhost", "127.0.0.1")
            # Fall back to first tab if no page type found
            if tabs:
                ws_url = tabs[0].get("webSocketDebuggerUrl")
                if ws_url:
                    return ws_url.replace("localhost", "127.0.0.1")
        except (OSError, Exception):
            return None
        return None

    async def _capture_loop(self):
        """Main capture and send loop with auto-reconnect."""
        try:
            import websockets
        except ImportError:
            print("websockets 패키지가 설치되지 않았습니다. pip install websockets")
            return

        _standalone = _is_standalone()

        while self._running:
            # Get Chrome CDP WebSocket URL
            cdp_ws_url = self._get_cdp_ws_url()
            if not cdp_ws_url:
                if _standalone:
                    print(
                        "\nChrome이 디버그 모드로 실행되지 않았습니다. 다음 명령으로 실행해주세요:",
                        flush=True
                    )
                    print(
                        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome "
                        "--remote-debugging-port=9222",
                        flush=True
                    )
                await asyncio.sleep(3)
                continue

            try:
                async with websockets.connect(cdp_ws_url, max_size=10*1024*1024) as cdp_ws:
                    if _standalone:
                        print(f"Chrome CDP connected at localhost:{self.cdp_port}", flush=True)

                    # Connect to JARVIS WebSocket
                    try:
                        async with websockets.connect(self.ws_url, max_size=10*1024*1024) as jarvis_ws:
                            if _standalone:
                                print(f"WebSocket connected to {self.ws_url}", flush=True)
                                print("Streaming... (Ctrl+C to stop)", flush=True)

                            msg_id = 0
                            while self._running:
                                loop_start = asyncio.get_event_loop().time()

                                # Capture screenshot via CDP
                                msg_id += 1
                                cmd = json.dumps({
                                    "id": msg_id,
                                    "method": "Page.captureScreenshot",
                                    "params": {
                                        "format": "jpeg",
                                        "quality": 50,
                                    },
                                })
                                await cdp_ws.send(cmd)

                                # Wait for response (with timeout, skip CDP events)
                                screenshot_data = None
                                try:
                                    deadline = asyncio.get_event_loop().time() + 5
                                    while asyncio.get_event_loop().time() < deadline:
                                        raw = await asyncio.wait_for(cdp_ws.recv(), timeout=3)
                                        resp = json.loads(raw)
                                        if resp.get("id") == msg_id:
                                            screenshot_data = resp.get("result", {}).get("data")
                                            break
                                except asyncio.TimeoutError:
                                    continue

                                if screenshot_data:
                                    payload = json.dumps({
                                        "type": "browser_frame",
                                        "value": screenshot_data,
                                    })
                                    await jarvis_ws.send(payload)

                                # Sleep to maintain target FPS
                                elapsed = asyncio.get_event_loop().time() - loop_start
                                sleep_time = max(0, self.interval - elapsed)
                                await asyncio.sleep(sleep_time)

                    except (OSError, websockets.exceptions.WebSocketException) as e:
                        if _standalone:
                            print(f"JARVIS WebSocket 연결 실패: {e}. 재연결 중...", flush=True)
                        await asyncio.sleep(2)

            except (OSError, websockets.exceptions.WebSocketException) as e:
                if _standalone:
                    print(f"Chrome CDP 연결 끊김: {e}. 재연결 중...", flush=True)
                await asyncio.sleep(2)


_running_as_main = False


def _is_standalone():
    return _running_as_main


def main():
    """Standalone entry point."""
    global _running_as_main
    _running_as_main = True

    import sys
    feed = BrowserFeed()
    print("Browser Feed 시작...", flush=True)
    feed.start()

    try:
        while True:
            time.sleep(0.5)
    except KeyboardInterrupt:
        print("\n종료 중...", flush=True)
        feed.stop()
        print("완료.", flush=True)


if __name__ == "__main__":
    main()
