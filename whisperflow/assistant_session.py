"""
Assistant Session - Claude CLI 멀티 세션 관리 (WhisperFlow 비서 모드용)

tars_session.py와 달리:
- 풀파워 모드: --tools "" 없이 모든 도구 사용, --permission-mode auto
- 스트리밍: --output-format stream-json, send_stream()으로 라인 단위 yield
- 멀티 세션: SessionManager로 여러 세션(탭) 관리
- 세션 저장/복원: ~/.whisperflow/assistant_sessions.json
- 프로젝트 경로: 세션별 cwd 설정

사용:
    from whisperflow.assistant_session import session_manager
    session_manager.create_session("tab_1", "메인 비서", cwd="/path/to/project")
    response = session_manager.send("tab_1", "오늘 할 일 정리해줘")
    for chunk in session_manager.send_stream("tab_1", "파일 분석해줘"):
        print(chunk)
"""

from __future__ import annotations

import json
import os
import subprocess
import threading
import time
from pathlib import Path
from typing import Generator, Optional

MODEL_ALIASES = {
    "haiku": "claude-haiku-4-5-20251001",
    "sonnet": "claude-sonnet-4-20250514",
    "opus": "claude-opus-4-20250514",
}

DEFAULT_MODEL = "haiku"

SESSIONS_DIR = Path.home() / ".whisperflow"
SESSIONS_FILE = SESSIONS_DIR / "assistant_sessions.json"

SECOND_BRAIN_CLAUDE_MD = Path.home() / "Documents/idea/07second-brain/CLAUDE.md"

_PROJECT_MAP_SECTION = ""


def _load_project_map() -> str:
    """CLAUDE.md에서 '## 프로젝트 맵' 섹션을 추출하여 반환."""
    global _PROJECT_MAP_SECTION
    if _PROJECT_MAP_SECTION:
        return _PROJECT_MAP_SECTION
    try:
        text = SECOND_BRAIN_CLAUDE_MD.read_text(encoding="utf-8")
        marker = "## 프로젝트 맵 (비서용)"
        idx = text.find(marker)
        if idx == -1:
            return ""
        _PROJECT_MAP_SECTION = text[idx:].strip()
        return _PROJECT_MAP_SECTION
    except Exception:
        return ""


VAULT_PATH = os.environ.get('OBSIDIAN_VAULT_PATH', '~/Documents/idea/07second-brain/vault/')

SYSTEM_PROMPT_TEMPLATE = f"""\
너는 개인 비서 Jarvis다.
Obsidian vault: {VAULT_PATH}
현재 프로젝트 디렉토리: {{cwd}}
간결하게 답변하고, 처리 결과는 vault에 저장해.

{{project_map}}"""


def check_claude_cli() -> bool:
    """Claude CLI 설치 여부 확인."""
    try:
        r = subprocess.run(
            ["claude", "--version"], capture_output=True, text=True, timeout=5
        )
        return r.returncode == 0
    except Exception:
        return False


class AssistantSession:
    """개별 Claude CLI 세션."""

    def __init__(
        self,
        name: str,
        cwd: str | None = None,
        model_alias: str = DEFAULT_MODEL,
        session_id: str | None = None,
        message_count: int = 0,
        created_at: float | None = None,
        total_cost_usd: float = 0.0,
    ):
        self.lock = threading.Lock()
        self.name = name
        self.cwd = cwd or str(Path.home())
        self.model_alias = model_alias
        self.model = MODEL_ALIASES.get(model_alias, model_alias)
        self.session_id = session_id
        self.message_count = message_count
        self.created_at = created_at
        self.total_cost_usd = total_cost_usd

    @property
    def is_initialized(self) -> bool:
        return self.session_id is not None

    def to_dict(self) -> dict:
        return {
            "session_id": self.session_id,
            "name": self.name,
            "cwd": self.cwd,
            "model": self.model_alias,
            "message_count": self.message_count,
            "created_at": self.created_at,
            "total_cost_usd": self.total_cost_usd,
        }

    @classmethod
    def from_dict(cls, data: dict) -> AssistantSession:
        return cls(
            name=data.get("name", "unnamed"),
            cwd=data.get("cwd"),
            model_alias=data.get("model", DEFAULT_MODEL),
            session_id=data.get("session_id"),
            message_count=data.get("message_count", 0),
            created_at=data.get("created_at"),
            total_cost_usd=data.get("total_cost_usd", 0.0),
        )

    def change_model(self, model_alias: str) -> str:
        """모델 변경 -> 새 세션 시작."""
        self.model_alias = model_alias
        self.model = MODEL_ALIASES.get(model_alias, model_alias)
        self.session_id = None
        self.message_count = 0
        self.created_at = None
        self.total_cost_usd = 0.0
        return self.model

    def reset(self):
        """세션 리셋. 같은 모델로 새 대화 시작."""
        self.session_id = None
        self.message_count = 0
        self.created_at = None
        self.total_cost_usd = 0.0

    def _build_prompt(self, text: str) -> str:
        """첫 메시지면 시스템 프롬프트 포함."""
        if self.session_id is None:
            project_map = _load_project_map()
            system = SYSTEM_PROMPT_TEMPLATE.format(
                cwd=self.cwd, project_map=project_map
            )
            return f"{system}\n\n---\nUser: {text}"
        return text

    def _build_cmd(self, text: str, output_format: str = "json") -> list[str]:
        """CLI 명령어 구성."""
        prompt = self._build_prompt(text)
        cmd = [
            "claude",
            "--print",
            "-p",
            prompt,
            "--model",
            self.model,
            "--output-format",
            output_format,
            "--dangerously-skip-permissions",
        ]
        # stream-json requires --verbose
        if output_format == "stream-json":
            cmd.append("--verbose")
        if self.session_id:
            cmd += ["--resume", self.session_id]
        return cmd

    def _handle_session_id(self, data: dict):
        """응답에서 session_id 추출 및 저장."""
        if "session_id" in data:
            if self.session_id is None:
                self.created_at = time.time()
            self.session_id = data["session_id"]

    def status(self) -> dict:
        """현재 세션 상태 반환."""
        return {
            **self.to_dict(),
            "model_full": self.model,
            "is_initialized": self.is_initialized,
            "uptime_seconds": (
                int(time.time() - self.created_at) if self.created_at else 0
            ),
        }


class SessionManager:
    """멀티 세션 관리자."""

    def __init__(self):
        self._lock = threading.Lock()
        self.sessions: dict[str, AssistantSession] = {}
        self._load_sessions()

    # ── 세션 CRUD ──

    def create_session(
        self,
        tab_id: str,
        name: str,
        cwd: str | None = None,
        model_alias: str = DEFAULT_MODEL,
    ) -> AssistantSession:
        """새 세션 생성. 이미 존재하면 기존 세션 반환."""
        with self._lock:
            if tab_id in self.sessions:
                return self.sessions[tab_id]
            session = AssistantSession(
                name=name, cwd=cwd, model_alias=model_alias
            )
            self.sessions[tab_id] = session
            self._save_sessions_unlocked()
            return session

    def delete_session(self, tab_id: str):
        """세션 삭제."""
        with self._lock:
            self.sessions.pop(tab_id, None)
            self._save_sessions_unlocked()

    def get_session(self, tab_id: str) -> AssistantSession | None:
        """세션 조회."""
        return self.sessions.get(tab_id)

    def list_sessions(self) -> list[dict]:
        """모든 세션 목록 반환."""
        return [
            {"tab_id": tid, **s.status()} for tid, s in self.sessions.items()
        ]

    def rename_session(self, tab_id: str, name: str):
        """세션 이름 변경."""
        session = self._require_session(tab_id)
        with session.lock:
            session.name = name
        with self._lock:
            self._save_sessions_unlocked()

    def reset_session(self, tab_id: str):
        """세션 리셋 (새 대화 시작, 같은 설정 유지)."""
        session = self._require_session(tab_id)
        with session.lock:
            session.reset()
        with self._lock:
            self._save_sessions_unlocked()

    # ── 메시지 전송 ──

    def send(self, tab_id: str, text: str, timeout: int = 120) -> str:
        """동기 응답. 스레드 안전."""
        session = self._require_session(tab_id)
        with session.lock:
            return self._send_sync(session, text, timeout)

    def send_stream(
        self, tab_id: str, text: str, timeout: int = 120
    ) -> Generator[dict, None, None]:
        """스트리밍 응답. subprocess stdout을 라인 단위로 yield.

        yield되는 dict 예시:
            {"type": "assistant", "content": "안녕하세요..."}
            {"type": "tool_use", "tool": "Read", ...}
            {"type": "result", "result": "최종 텍스트", "session_id": "abc123"}
        """
        session = self._require_session(tab_id)
        # 스트리밍은 lock을 잡지 않음 (장시간 블록 방지)
        # 대신 세션별 lock으로 동시 전송 방지
        with session.lock:
            yield from self._send_stream_locked(session, text, timeout)

    # ── 내부 메서드 ──

    def _require_session(self, tab_id: str) -> AssistantSession:
        session = self.sessions.get(tab_id)
        if session is None:
            raise KeyError(f"Session not found: {tab_id}")
        return session

    def _send_sync(self, session: AssistantSession, text: str, timeout: int) -> str:
        """동기 전송 (json 출력)."""
        cmd = session._build_cmd(text, output_format="json")

        try:
            env = {**os.environ, "JARVIS_TTS": "1"}
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout,
                cwd=session.cwd,
                env=env,
            )

            if result.returncode != 0:
                stderr = result.stderr.strip()
                if session.session_id and (
                    "session" in stderr.lower() or "resume" in stderr.lower()
                ):
                    print(
                        f"[Assistant] Session corrupted, resetting: {stderr[:100]}"
                    )
                    session.reset()
                    return self._send_sync(session, text, timeout)
                return f"Error: {stderr or result.stdout.strip()}"

            data = json.loads(result.stdout.strip())
            session._handle_session_id(data)
            session.total_cost_usd += data.get("total_cost_usd", 0)
            session.message_count += 1

            with self._lock:
                self._save_sessions_unlocked()

            return data.get("result", "...")

        except subprocess.TimeoutExpired:
            return "Response timed out. Try again."
        except json.JSONDecodeError as e:
            return f"Parse error: {e}"
        except FileNotFoundError:
            return (
                "Claude CLI not found. Install: npm install -g @anthropic-ai/claude-code"
            )
        except Exception as e:
            return f"Error: {str(e)}"

    def _send_stream_locked(
        self, session: AssistantSession, text: str, timeout: int
    ) -> Generator[dict, None, None]:
        """스트리밍 전송. lock은 호출 측에서 보장."""
        cmd = session._build_cmd(text, output_format="stream-json")

        try:
            env = {**os.environ, "JARVIS_TTS": "1"}
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                cwd=session.cwd,
                env=env,
            )

            last_data: dict = {}
            deadline = time.time() + timeout

            for line in proc.stdout:  # type: ignore[union-attr]
                if time.time() > deadline:
                    proc.kill()
                    yield {"type": "error", "error": "Response timed out."}
                    return

                line = line.strip()
                if not line:
                    continue

                try:
                    data = json.loads(line)
                except json.JSONDecodeError:
                    continue

                last_data = data

                # session_id가 나오면 즉시 저장
                if "session_id" in data:
                    session._handle_session_id(data)

                yield data

            proc.wait(timeout=5)

            # 마지막 데이터에서 비용/세션 정보 업데이트
            if last_data:
                session._handle_session_id(last_data)
                session.total_cost_usd += last_data.get("total_cost_usd", 0)

            session.message_count += 1

            with self._lock:
                self._save_sessions_unlocked()

            if proc.returncode and proc.returncode != 0:
                stderr = proc.stderr.read() if proc.stderr else ""  # type: ignore[union-attr]
                stderr = stderr.strip()
                if session.session_id and (
                    "session" in stderr.lower() or "resume" in stderr.lower()
                ):
                    print(
                        f"[Assistant] Session corrupted, resetting: {stderr[:100]}"
                    )
                    session.reset()
                    yield {
                        "type": "error",
                        "error": "Session corrupted. Reset. Please retry.",
                    }
                elif stderr:
                    yield {"type": "error", "error": stderr}

        except FileNotFoundError:
            yield {
                "type": "error",
                "error": "Claude CLI not found. Install: npm install -g @anthropic-ai/claude-code",
            }
        except Exception as e:
            yield {"type": "error", "error": str(e)}

    # ── 저장/복원 ──

    def _save_sessions_unlocked(self):
        """세션 정보를 파일에 저장. _lock 안에서 호출."""
        try:
            SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
            data = {tid: s.to_dict() for tid, s in self.sessions.items()}
            SESSIONS_FILE.write_text(
                json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except Exception as e:
            print(f"[Assistant] Failed to save sessions: {e}")

    def _load_sessions(self):
        """파일에서 세션 복원."""
        if not SESSIONS_FILE.exists():
            return
        try:
            data = json.loads(SESSIONS_FILE.read_text(encoding="utf-8"))
            for tid, sdata in data.items():
                self.sessions[tid] = AssistantSession.from_dict(sdata)
        except Exception as e:
            print(f"[Assistant] Failed to load sessions: {e}")


# 전역 세션 매니저
session_manager = SessionManager()
