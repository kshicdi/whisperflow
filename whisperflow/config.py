"""설정 관리 모듈"""

import json
import os
from pathlib import Path
from dataclasses import dataclass, asdict
from typing import Literal


@dataclass
class Config:
    """앱 설정"""
    model_size: str = "base"  # tiny/base/small/medium/large-v3
    language: str = "ko"  # 인식 언어
    hotkey: str = "cmd+shift+r"  # 녹음 단축키
    output_mode: Literal["clipboard", "type"] = "type"
    sample_rate: int = 16000  # Whisper 권장 샘플레이트
    option_hold_enabled: bool = False  # Option 키 길게 누르기로 녹음
    history_enabled: bool = True  # 히스토리 저장 활성화
    tts_hotkey: str = "cmd+shift+s"  # TTS 단축키
    tts_rate: int = 200  # TTS 읽기 속도 (words per minute)
    tts_enabled: bool = True  # TTS 기능 활성화
    auto_enter: bool = False  # 붙여넣기 후 자동 엔터
    qwen_tts_speed: float = 1.4  # Qwen TTS 발음 속도
    tts_say_first: bool = True   # True: say 선행 + Qwen, False: Qwen TTS만 사용

    @classmethod
    def get_config_path(cls) -> Path:
        """설정 파일 경로 반환"""
        config_dir = Path.home() / ".config" / "whisperflow"
        config_dir.mkdir(parents=True, exist_ok=True)
        return config_dir / "config.json"

    @classmethod
    def load(cls) -> "Config":
        """설정 파일에서 로드"""
        config_path = cls.get_config_path()
        if config_path.exists():
            try:
                with open(config_path, "r") as f:
                    data = json.load(f)
                return cls(**data)
            except (json.JSONDecodeError, TypeError):
                pass
        return cls()

    def save(self) -> None:
        """설정 파일에 저장"""
        config_path = self.get_config_path()
        with open(config_path, "w") as f:
            json.dump(asdict(self), f, indent=2, ensure_ascii=False)


# 전역 설정 인스턴스
config = Config.load()
