"""
TARS Session - Claude CLI 세션 관리 (WhisperFlow 통합용)

simulator_server.py의 TarsSession을 독립 모듈로 분리.
--resume 방식으로 대화 컨텍스트를 유지하며, 스레드 안전.

사용:
    from whisperflow.tars_session import tars_session
    response = tars_session.send("Hey TARS, what's your humor setting?")
"""

from __future__ import annotations

import json
import subprocess
import threading
import time
from typing import Optional

from .tars_mode import get_tars_system_prompt, get_tars_lang, set_tars_lang

MODEL_ALIASES = {
    "haiku": "claude-haiku-4-5-20251001",
    "sonnet": "claude-sonnet-4-20250514",
    "opus": "claude-opus-4-20250514",
}

DEFAULT_MODEL = "haiku"


def check_claude_cli() -> bool:
    """Claude CLI 설치 여부 확인."""
    try:
        r = subprocess.run(["claude", "--version"], capture_output=True, text=True, timeout=5)
        return r.returncode == 0
    except Exception:
        return False


class TarsSession:
    """Claude CLI 세션 관리. --resume으로 대화 컨텍스트를 유지."""

    def __init__(self, model_alias: str = DEFAULT_MODEL):
        self._lock = threading.Lock()
        self._model_alias = model_alias
        self._model = MODEL_ALIASES.get(model_alias, model_alias)
        self._session_id: Optional[str] = None
        self._message_count: int = 0
        self._created_at: Optional[float] = None
        self._total_cost_usd: float = 0.0

    @property
    def model_alias(self) -> str:
        return self._model_alias

    @property
    def model(self) -> str:
        return self._model

    @property
    def session_id(self) -> str | None:
        return self._session_id

    @property
    def message_count(self) -> int:
        return self._message_count

    @property
    def total_cost_usd(self) -> float:
        return self._total_cost_usd

    @property
    def is_initialized(self) -> bool:
        return self._session_id is not None

    def change_model(self, model_alias: str) -> str:
        """모델 변경 -> 새 세션 시작 (기존 세션 폐기)."""
        with self._lock:
            self._model_alias = model_alias
            self._model = MODEL_ALIASES.get(model_alias, model_alias)
            self._session_id = None
            self._message_count = 0
            self._created_at = None
            self._total_cost_usd = 0.0
        return self._model

    def reset(self):
        """세션 리셋. 같은 모델로 새 대화 시작."""
        with self._lock:
            self._session_id = None
            self._message_count = 0
            self._created_at = None
            self._total_cost_usd = 0.0

    def send(self, text: str, timeout: int = 30) -> str:
        """메시지 전송 -> 응답 텍스트 반환. 스레드 안전."""
        with self._lock:
            return self._send_locked(text, timeout)

    def _send_locked(self, text: str, timeout: int) -> str:
        """Lock 내부에서 실행. 첫 메시지면 시스템 프롬프트 포함."""
        if self._session_id is None:
            # 첫 메시지: 시스템 프롬프트 + 사용자 메시지
            prompt = f"{get_tars_system_prompt()}\n\n---\nUser: {text}"
            cmd = [
                "claude", "--print", "-p", prompt,
                "--model", self._model,
                "--output-format", "json",
                "--tools", "",
            ]
        else:
            # 이후 메시지: --resume으로 세션 이어서
            cmd = [
                "claude", "--print", "-p", text,
                "--model", self._model,
                "--output-format", "json",
                "--tools", "",
                "--resume", self._session_id,
            ]

        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=timeout
            )

            if result.returncode != 0:
                stderr = result.stderr.strip()
                # 세션 손상 시 리셋 후 재시도
                if self._session_id and (
                    "session" in stderr.lower() or "resume" in stderr.lower()
                ):
                    print(f"[TARS Session] Session corrupted, resetting: {stderr[:100]}")
                    self._session_id = None
                    return self._send_locked(text, timeout)
                return f"Error: {stderr or result.stdout.strip()}"

            data = json.loads(result.stdout.strip())

            # 세션 ID 저장
            if "session_id" in data:
                if self._session_id is None:
                    self._created_at = time.time()
                self._session_id = data["session_id"]

            # 비용 누적
            cost = data.get("total_cost_usd", 0)
            self._total_cost_usd += cost

            self._message_count += 1

            response_text = data.get("result", "")
            if not response_text:
                return "..."

            return response_text

        except subprocess.TimeoutExpired:
            return "Response timed out. Try again."
        except json.JSONDecodeError as e:
            return f"Parse error: {e}"
        except FileNotFoundError:
            return "Claude CLI not found. Install: npm install -g @anthropic-ai/claude-code"
        except Exception as e:
            return f"Error: {str(e)}"

    def change_lang(self, lang: str) -> str:
        """응답 언어 변경 (en/ko). 세션 리셋됨."""
        set_tars_lang(lang)
        self.reset()
        return get_tars_lang()

    def status(self) -> dict:
        """현재 세션 상태 반환."""
        return {
            "model": self._model,
            "model_alias": self._model_alias,
            "session_id": self._session_id,
            "message_count": self._message_count,
            "total_cost_usd": round(self._total_cost_usd, 6),
            "created_at": self._created_at,
            "uptime_seconds": int(time.time() - self._created_at) if self._created_at else 0,
            "lang": get_tars_lang(),
        }


# 전역 싱글턴 세션
tars_session = TarsSession(model_alias=DEFAULT_MODEL)
