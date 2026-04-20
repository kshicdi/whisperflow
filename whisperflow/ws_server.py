"""
WhisperFlow WebSocket Server

Serves the JARVIS UI via HTTP and provides real-time state updates
via WebSocket, both on the same port (8767).
"""

import asyncio
import json
import logging
import mimetypes
import os
import threading
from pathlib import Path
from typing import Optional, Set

import websockets
from websockets.server import WebSocketServerProtocol
from websockets.http11 import Request, Response
from websockets.datastructures import Headers

logger = logging.getLogger(__name__)

WS_PORT = 8767


class WhisperFlowWSServer:
    def __init__(self, static_dir: str = None):
        """static_dir defaults to whisperflow/static/"""
        if static_dir is None:
            static_dir = os.path.join(os.path.dirname(__file__), "static")
        self.static_dir = static_dir

        self._clients: Set[WebSocketServerProtocol] = set()
        self._current_state: str = "idle"
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._server = None
        self._stop_event: Optional[asyncio.Event] = None

    # ------------------------------------------------------------------
    # HTTP static file handler (called via process_request hook)
    # ------------------------------------------------------------------

    def _serve_static(self, path: str) -> Response:
        """Return an HTTP Response for a static file request."""
        if path == "/":
            path = "/jarvis.html"

        # Strip query string
        path = path.split("?", 1)[0]

        file_path = Path(self.static_dir) / path.lstrip("/")

        try:
            file_path = file_path.resolve()
            static_root = Path(self.static_dir).resolve()
            # Security: ensure the resolved path is inside static_dir
            file_path.relative_to(static_root)
        except (ValueError, RuntimeError):
            body = b"403 Forbidden"
            headers = Headers([
                ("Content-Type", "text/plain"),
                ("Content-Length", str(len(body))),
            ])
            return Response(403, "Forbidden", headers, body)

        if not file_path.exists() or not file_path.is_file():
            body = b"404 Not Found"
            headers = Headers([
                ("Content-Type", "text/plain"),
                ("Content-Length", str(len(body))),
            ])
            return Response(404, "Not Found", headers, body)

        mime_type, _ = mimetypes.guess_type(str(file_path))
        if mime_type is None:
            mime_type = "application/octet-stream"

        body = file_path.read_bytes()
        headers = Headers([
            ("Content-Type", mime_type),
            ("Content-Length", str(len(body))),
            ("Cache-Control", "no-cache"),
        ])
        return Response(200, "OK", headers, body)

    async def _process_request(
        self, connection, request: Request
    ):
        """
        process_request hook for websockets >= 13.
        Return an HTTP Response to serve static files; return None to
        proceed with the WebSocket handshake.
        """
        # WebSocket upgrade requests have an "Upgrade: websocket" header
        upgrade = request.headers.get("Upgrade", "").lower()
        if upgrade == "websocket":
            # Let the library handle the WebSocket handshake
            return None

        # Otherwise serve the static file
        return self._serve_static(request.path)

    # ------------------------------------------------------------------
    # WebSocket handler
    # ------------------------------------------------------------------

    async def _handler(self, websocket: WebSocketServerProtocol):
        """Handle a new WebSocket connection."""
        self._clients.add(websocket)
        logger.info("Client connected: %s (total: %d)", websocket.remote_address, len(self._clients))

        try:
            # Send current state to the newly connected client
            await websocket.send(json.dumps({"type": "state", "value": self._current_state}))

            # Handle incoming messages - broadcast to all OTHER clients
            async for message in websocket:
                logger.debug("Received from client: %s", message)
                try:
                    data = json.loads(message)
                    msg_type = data.get("type", "")
                    # Forward input/output/state/transcript messages
                    if msg_type in ("input", "output", "output_chunk", "state", "transcript", "audio_level", "browser_frame", "browser_stop"):
                        if msg_type == "state":
                            self._current_state = data.get("value", "idle")
                        # Broadcast to all clients except sender
                        for ws in list(self._clients):
                            if ws is not websocket:
                                try:
                                    await ws.send(message)
                                except websockets.ConnectionClosed:
                                    pass
                except (json.JSONDecodeError, Exception):
                    pass
        except websockets.ConnectionClosed:
            pass
        finally:
            self._clients.discard(websocket)
            logger.info("Client disconnected (total: %d)", len(self._clients))

    # ------------------------------------------------------------------
    # Async broadcast helpers
    # ------------------------------------------------------------------

    async def _broadcast(self, message: str):
        """Send a message to all connected clients."""
        if not self._clients:
            return
        disconnected = set()
        for ws in list(self._clients):
            try:
                await ws.send(message)
            except websockets.ConnectionClosed:
                disconnected.add(ws)
        self._clients -= disconnected

    # ------------------------------------------------------------------
    # Server lifecycle
    # ------------------------------------------------------------------

    def _run_loop(self):
        """Entry point for the background thread."""
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._loop.run_until_complete(self._serve())

    async def _serve(self):
        self._stop_event = asyncio.Event()
        try:
            async with websockets.serve(
                self._handler,
                "localhost",
                WS_PORT,
                process_request=self._process_request,
                max_size=10 * 1024 * 1024,  # 10MB for browser screenshots
            ) as server:
                self._server = server
                logger.info("WhisperFlow WS server started on ws://localhost:%d", WS_PORT)
                await self._stop_event.wait()
            logger.info("WhisperFlow WS server stopped")
        except OSError as e:
            logger.error("WS server failed to start (port %d may be in use): %s", WS_PORT, e)
            # 서버 시작 실패해도 앱의 나머지 기능은 정상 동작해야 함

    def start(self):
        """Start WebSocket server in a background thread."""
        if self._thread and self._thread.is_alive():
            logger.warning("Server is already running")
            return
        self._thread = threading.Thread(target=self._run_loop, daemon=True, name="WSServer")
        self._thread.start()

    def stop(self):
        """Stop the server."""
        if self._loop and self._stop_event:
            self._loop.call_soon_threadsafe(self._stop_event.set)
        if self._thread:
            self._thread.join(timeout=5)
            self._thread = None

    # ------------------------------------------------------------------
    # Thread-safe broadcast methods (called from rumps/app thread)
    # ------------------------------------------------------------------

    def _schedule(self, coro):
        """Schedule a coroutine on the server's event loop from any thread."""
        if self._loop is None or not self._loop.is_running():
            return
        asyncio.run_coroutine_threadsafe(coro, self._loop)

    def broadcast_state(self, state: str):
        """Broadcast state change: 'idle', 'recording', 'processing', 'tts_playing'"""
        self._current_state = state
        message = json.dumps({"type": "state", "value": state})
        self._schedule(self._broadcast(message))

    def broadcast_audio_level(self, level: float):
        """Broadcast audio level 0.0~1.0"""
        message = json.dumps({"type": "audio_level", "value": round(level, 4)})
        self._schedule(self._broadcast(message))

    def broadcast_transcript(self, text: str):
        """Broadcast transcribed text"""
        message = json.dumps({"type": "transcript", "value": text})
        self._schedule(self._broadcast(message))
