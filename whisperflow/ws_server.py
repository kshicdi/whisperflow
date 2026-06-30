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
import time as _time
from pathlib import Path
from typing import Optional, Set

import websockets
from websockets.server import WebSocketServerProtocol
from websockets.http11 import Request, Response
from websockets.datastructures import Headers

from whisperflow.hue_controller import HueController
from whisperflow.assistant_session import session_manager

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
        # 외부에서 등록하는 콜백 (app.py에서 remote_record 처리용)
        self._on_remote_record = None
        self._on_conversation_continue = None
        self._on_chat_tts = None  # 콜백: app.py에서 채팅 응답 TTS 실행용
        self._on_tts_interrupt = None
        # Hue 조명 제어
        self._hue = HueController()
        # Chat message persistence
        self._chat_history_path = Path.home() / ".whisperflow" / "chat_messages.json"
        self._chat_history_lock = threading.Lock()
        self._chat_history_max = 500
        # Accumulate assistant response chunks per tab_id during streaming
        self._streaming_buffers: dict[str, str] = {}

    # ------------------------------------------------------------------
    # Chat history file persistence
    # ------------------------------------------------------------------

    def _load_chat_history(self, tab_id: str) -> list:
        """Load chat messages for a tab from disk."""
        with self._chat_history_lock:
            try:
                if self._chat_history_path.exists():
                    data = json.loads(self._chat_history_path.read_text(encoding="utf-8"))
                    return data.get(tab_id, [])
            except Exception as e:
                logger.error("Failed to load chat history: %s", e)
            return []

    def _save_chat_message(self, tab_id: str, role: str, content: str, timestamp: int = None):
        """Append a single message and persist to disk."""
        if timestamp is None:
            timestamp = int(_time.time() * 1000)
        msg = {"role": role, "content": content, "timestamp": timestamp}
        with self._chat_history_lock:
            try:
                self._chat_history_path.parent.mkdir(parents=True, exist_ok=True)
                data = {}
                if self._chat_history_path.exists():
                    data = json.loads(self._chat_history_path.read_text(encoding="utf-8"))
                messages = data.get(tab_id, [])
                messages.append(msg)
                # Keep only the last N messages
                if len(messages) > self._chat_history_max:
                    messages = messages[-self._chat_history_max:]
                data[tab_id] = messages
                self._chat_history_path.write_text(
                    json.dumps(data, ensure_ascii=False), encoding="utf-8"
                )
            except Exception as e:
                logger.error("Failed to save chat message: %s", e)

    # ------------------------------------------------------------------
    # HTTP static file handler (called via process_request hook)
    # ------------------------------------------------------------------

    def _serve_static(self, path: str, user_agent: str = "") -> Response:
        """Return an HTTP Response for a static file request."""
        if path == "/":
            # Auto-detect mobile → assistant.html, PC → jarvis.html
            ua = user_agent.lower()
            if any(m in ua for m in ('iphone', 'android', 'mobile')):
                path = "/assistant.html"
            else:
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
        ua = request.headers.get("User-Agent", "")
        return self._serve_static(request.path, user_agent=ua)

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

                    # ── Chat & Session messages (unicast to sender) ──
                    if msg_type == "chat_input":
                        await self._handle_chat_input(websocket, data)
                        continue
                    if msg_type == "session_create":
                        await self._handle_session_create(websocket, data)
                        continue
                    if msg_type == "session_delete":
                        await self._handle_session_delete(websocket, data)
                        continue
                    if msg_type == "session_reset":
                        await self._handle_session_reset(websocket, data)
                        continue
                    if msg_type == "session_rename":
                        await self._handle_session_rename(websocket, data)
                        continue
                    if msg_type == "session_list":
                        await self._handle_session_list(websocket)
                        continue
                    if msg_type == "session_switch":
                        # UI-only action; acknowledge with session info
                        tab_id = data.get("tab_id", "")
                        session = session_manager.get_session(tab_id)
                        info = session.status() if session else None
                        await websocket.send(json.dumps({
                            "type": "session_switched", "tab_id": tab_id,
                            "session": info,
                        }))
                        continue
                    if msg_type == "chat_history_request":
                        tab_id = data.get("tab_id", "")
                        messages = self._load_chat_history(tab_id)
                        await websocket.send(json.dumps({
                            "type": "chat_history",
                            "tab_id": tab_id,
                            "messages": messages,
                        }))
                        continue
                    if msg_type == "file_upload":
                        await self._handle_file_upload(websocket, data)
                        continue
                    if msg_type == "session_model_change":
                        tab_id = data.get("tab_id", "")
                        model = data.get("model", "haiku")
                        session = session_manager.get_session(tab_id)
                        if session:
                            with session.lock:
                                session.change_model(model)
                            with session_manager._lock:
                                session_manager._save_sessions_unlocked()
                            await websocket.send(json.dumps({
                                "type": "model_changed", "tab_id": tab_id,
                                "model": model,
                            }))
                        continue

                    # ── Existing broadcast messages ──
                    if msg_type in ("input", "output", "output_chunk", "state", "transcript", "audio_level", "browser_frame", "browser_stop", "code_action", "ui_action", "camera_frame", "face_recognized", "remote_record", "tts_audio", "tts_interrupt", "gesture", "conversation_continue", "stl_view", "stl_close"):
                        if msg_type == "state":
                            self._current_state = data.get("value", "idle")
                            # Hue는 별도 스레드에서 호출 (타임아웃 시 이벤트 루프 블로킹 방지)
                            threading.Thread(target=self._hue.set_state, args=(self._current_state,), daemon=True).start()
                        # conversation_continue: 대화 모드 진입
                        if msg_type == "conversation_continue":
                            print("[WS] conversation_continue 수신")
                            if self._on_conversation_continue:
                                try:
                                    self._on_conversation_continue()
                                except Exception as e:
                                    logger.error("conversation_continue callback error: %s", e)
                            else:
                                print("[WS] conversation_continue 콜백 미등록")
                        # tts_interrupt: TTS 재생 중단 → recording 상태로 전환 (브로드캐스트 안 함)
                        if msg_type == "tts_interrupt":
                            print("[WS] tts_interrupt 수신")
                            if self._on_tts_interrupt:
                                try:
                                    self._on_tts_interrupt()
                                except Exception as e:
                                    logger.error("tts_interrupt callback error: %s", e)
                            continue
                        # remote_record: 앱 콜백 호출 (녹음 토글)
                        if msg_type == "remote_record" and self._on_remote_record:
                            try:
                                self._on_remote_record(data)
                            except Exception as e:
                                logger.error("remote_record callback error: %s", e)
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
    # File Upload handler (via WebSocket base64)
    # ------------------------------------------------------------------

    async def _handle_file_upload(self, websocket, data: dict):
        """Save base64 file to vault/inbox/, auto-convert HEIC to JPG."""
        import base64, re, subprocess as _sp, time as _time
        try:
            filename = data.get("filename", "upload.png")
            b64data = data.get("data", "")
            # Strip data URL prefix if present
            if "," in b64data:
                b64data = b64data.split(",", 1)[1]
            file_bytes = base64.b64decode(b64data)

            inbox_dir = Path.home() / "Documents/idea/07second-brain/vault/inbox"
            inbox_dir.mkdir(parents=True, exist_ok=True)
            timestamp = _time.strftime("%Y%m%d-%H%M%S")
            safe_name = re.sub(r'[^\w.\-]', '_', filename)
            save_path = inbox_dir / f"{timestamp}_{safe_name}"
            save_path.write_bytes(file_bytes)

            # Auto-convert HEIC/HEIF to JPG using macOS sips
            ext = save_path.suffix.lower()
            if ext in ('.heic', '.heif'):
                jpg_path = save_path.with_suffix('.jpg')
                result = _sp.run(
                    ['sips', '-s', 'format', 'jpeg', str(save_path), '--out', str(jpg_path)],
                    capture_output=True, text=True, timeout=10,
                )
                if result.returncode == 0 and jpg_path.exists():
                    save_path = jpg_path  # Use converted JPG

            await websocket.send(json.dumps({
                "type": "file_uploaded",
                "path": str(save_path),
                "filename": filename,
            }))
        except Exception as e:
            await websocket.send(json.dumps({
                "type": "chat_error",
                "tab_id": "",
                "error": f"Upload failed: {e}",
            }))

    # ------------------------------------------------------------------
    # Chat & Session handlers (unicast to requesting client)
    # ------------------------------------------------------------------

    async def _handle_chat_input(self, websocket, data: dict):
        """chat_input: 별도 스레드에서 스트리밍 → 해당 클라이언트에만 전송."""
        tab_id = data.get("tab_id", "")
        text = data.get("text", "")

        if not tab_id or not text:
            await websocket.send(json.dumps({
                "type": "chat_error", "tab_id": tab_id,
                "error": "tab_id and text are required",
            }))
            return

        # 세션이 없으면 자동 생성 (second-brain 기본 경로)
        if session_manager.get_session(tab_id) is None:
            session_manager.create_session(
                tab_id, name=tab_id,
                cwd=str(Path.home() / "Documents/idea/07second-brain"),
            )

        # Save user message to file
        self._save_chat_message(tab_id, "user", text)

        loop = self._loop

        def stream_worker():
            # Reset streaming buffer for this tab
            self._streaming_buffers[tab_id] = ""
            try:
                for chunk in session_manager.send_stream(tab_id, text):
                    chunk_type = chunk.get("type", "")
                    content = ""

                    if chunk_type == "assistant":
                        msg_data = chunk.get("message", {})
                        full_text = ""
                        for block in msg_data.get("content", []):
                            if block.get("type") == "text":
                                full_text = block.get("text", "")
                        if not full_text:
                            continue
                        prev = self._streaming_buffers.get(tab_id, "")
                        diff = full_text[len(prev):]
                        self._streaming_buffers[tab_id] = full_text
                        if not diff:
                            continue
                        content = diff
                    elif chunk_type == "result":
                        continue
                    elif chunk_type == "error":
                        content = chunk.get("error", "")
                    else:
                        continue

                    if not content:
                        continue

                    msg = json.dumps({
                        "type": "chat_chunk",
                        "tab_id": tab_id,
                        "content": content,
                        "chunk_type": chunk_type,
                    })
                    asyncio.run_coroutine_threadsafe(websocket.send(msg), loop)
            except Exception as e:
                err_msg = json.dumps({
                    "type": "chat_error", "tab_id": tab_id,
                    "error": str(e),
                })
                asyncio.run_coroutine_threadsafe(websocket.send(err_msg), loop)
            finally:
                # Save accumulated assistant response to file
                accumulated = self._streaming_buffers.pop(tab_id, "")
                if accumulated:
                    self._save_chat_message(tab_id, "assistant", accumulated)
                    # TTS: 응답 텍스트를 음성으로 읽기
                    if self._on_chat_tts:
                        try:
                            self._on_chat_tts(accumulated)
                        except Exception as e:
                            logger.error("Chat TTS error: %s", e)

                session = session_manager.get_session(tab_id)
                done_msg = json.dumps({
                    "type": "chat_done", "tab_id": tab_id,
                    "session_id": session.session_id if session else None,
                })
                asyncio.run_coroutine_threadsafe(websocket.send(done_msg), loop)

        threading.Thread(target=stream_worker, daemon=True).start()

    async def _handle_session_create(self, websocket, data: dict):
        tab_id = data.get("tab_id", "")
        name = data.get("name", tab_id)
        cwd = data.get("cwd")
        model = data.get("model")
        kwargs = {"tab_id": tab_id, "name": name}
        if cwd:
            kwargs["cwd"] = cwd
        if model:
            kwargs["model_alias"] = model
        session = session_manager.create_session(**kwargs)
        await websocket.send(json.dumps({
            "type": "session_created", "tab_id": tab_id,
            "session": session.status(),
        }))

    async def _handle_session_delete(self, websocket, data: dict):
        tab_id = data.get("tab_id", "")
        session_manager.delete_session(tab_id)
        await websocket.send(json.dumps({
            "type": "session_deleted", "tab_id": tab_id,
        }))

    async def _handle_session_reset(self, websocket, data: dict):
        tab_id = data.get("tab_id", "")
        try:
            session_manager.reset_session(tab_id)
            session = session_manager.get_session(tab_id)
            await websocket.send(json.dumps({
                "type": "session_reset_done", "tab_id": tab_id,
                "session": session.status() if session else None,
            }))
        except KeyError as e:
            await websocket.send(json.dumps({
                "type": "chat_error", "tab_id": tab_id, "error": str(e),
            }))

    async def _handle_session_rename(self, websocket, data: dict):
        tab_id = data.get("tab_id", "")
        name = data.get("name", "")
        try:
            session_manager.rename_session(tab_id, name)
            await websocket.send(json.dumps({
                "type": "session_renamed", "tab_id": tab_id, "name": name,
            }))
        except KeyError as e:
            await websocket.send(json.dumps({
                "type": "chat_error", "tab_id": tab_id, "error": str(e),
            }))

    async def _handle_session_list(self, websocket):
        sessions = session_manager.list_sessions()
        await websocket.send(json.dumps({
            "type": "session_list_response", "sessions": sessions,
        }))

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
                "127.0.0.1",
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
        self._hue.stop()
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
        # Hue 조명 상태 연동 (별도 스레드에서 호출)
        threading.Thread(target=self._hue.set_state, args=(state,), daemon=True).start()

    def broadcast_audio_level(self, level: float):
        """Broadcast audio level 0.0~1.0"""
        message = json.dumps({"type": "audio_level", "value": round(level, 4)})
        self._schedule(self._broadcast(message))

    def broadcast_transcript(self, text: str):
        """Broadcast transcribed text"""
        message = json.dumps({"type": "transcript", "value": text})
        self._schedule(self._broadcast(message))

    def broadcast_raw(self, message: str):
        """Broadcast a raw JSON string as-is"""
        self._schedule(self._broadcast(message))
