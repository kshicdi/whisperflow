"""히스토리 저장 모듈 - 음성 파일과 변환 텍스트 저장"""

import json
import shutil
from datetime import datetime
from pathlib import Path
from typing import Optional, List, Dict

from .config import config


class HistoryManager:
    """녹음 히스토리 관리 클래스"""

    def __init__(self):
        self.history_dir = Path.home() / ".whisperflow" / "history"
        self.history_dir.mkdir(parents=True, exist_ok=True)

    def save(self, audio_path: str, text: str, language: str = None,
             model: str = None, duration: float = None) -> Optional[Path]:
        """음성 파일과 텍스트를 히스토리로 저장

        Args:
            audio_path: 원본 오디오 파일 경로
            text: 변환된 텍스트
            language: 사용된 언어
            model: 사용된 모델
            duration: 녹음 길이 (초)

        Returns:
            저장된 오디오 파일 경로
        """
        if not config.history_enabled:
            return None

        now = datetime.now()
        date_str = now.strftime("%Y-%m-%d")
        time_str = now.strftime("%H-%M-%S")

        # 날짜별 폴더 생성
        date_dir = self.history_dir / date_str
        date_dir.mkdir(parents=True, exist_ok=True)

        # 오디오 파일 복사 (시간_audio.wav)
        audio_dest = date_dir / f"{time_str}_audio.wav"
        try:
            shutil.copy2(audio_path, audio_dest)
        except Exception as e:
            print(f"[히스토리] 오디오 복사 실패: {e}")

        # 텍스트 저장 (시간_text.txt)
        text_path = date_dir / f"{time_str}_text.txt"
        with open(text_path, "w", encoding="utf-8") as f:
            f.write(text)

        print(f"[히스토리] 저장됨: {date_dir}/{time_str}")
        return audio_dest

    def get_recent(self, limit: int = 10) -> List[Dict]:
        """최근 히스토리 목록 반환

        Args:
            limit: 반환할 최대 개수

        Returns:
            히스토리 항목 리스트 (최신순)
        """
        entries = []
        if not self.history_dir.exists():
            return entries

        # 날짜 폴더 목록 (최신순 정렬)
        date_dirs = sorted(
            [d for d in self.history_dir.iterdir() if d.is_dir()],
            reverse=True
        )

        for date_dir in date_dirs:
            # 텍스트 파일들 찾기 (최신순)
            text_files = sorted(date_dir.glob("*_text.txt"), reverse=True)

            for text_path in text_files:
                if len(entries) >= limit:
                    return entries

                time_str = text_path.stem.replace("_text", "")
                audio_path = date_dir / f"{time_str}_audio.wav"

                entry = {
                    "date": date_dir.name,
                    "time": time_str,
                    "audio_path": audio_path if audio_path.exists() else None,
                    "text_path": text_path,
                }

                with open(text_path, "r", encoding="utf-8") as f:
                    entry["text"] = f.read()

                entries.append(entry)

        return entries

    def get_history_dir(self) -> Path:
        """히스토리 디렉토리 경로 반환"""
        return self.history_dir

    def clear_all(self) -> int:
        """모든 히스토리 삭제

        Returns:
            삭제된 항목 수
        """
        count = 0
        if self.history_dir.exists():
            for entry_dir in self.history_dir.iterdir():
                if entry_dir.is_dir():
                    shutil.rmtree(entry_dir)
                    count += 1
        print(f"[히스토리] {count}개 삭제됨")
        return count


# 전역 히스토리 매니저 인스턴스
history_manager = HistoryManager()
