"""
map_navigator.py — 네이버 지도 / 카카오맵 경로 안내 모듈.

집주소 설정 (선택사항):
  환경변수 HOME_ADDRESS_FILE: 집주소가 저장된 마크다운 파일 경로
  파일 포맷: "위도: 37.1234\n경도: 126.5678" 형식

URL (웹 — macOS 브라우저에서 열림):
  네이버: http://map.naver.com/index.nhn?slng=..&slat=..&stext=..&elng=..&elat=..&etext=..&menu=route&pathType=0
  카카오: https://map.kakao.com/link/from/출발지,slat,slng/to/목적지,dlat,dlng
"""

import logging
import os
import re
import subprocess
from pathlib import Path
from urllib.parse import quote

logger = logging.getLogger(__name__)

HOME_ADDRESS_PATH = os.environ.get('HOME_ADDRESS_FILE')

APP_NAME = "com.whisperflow"


# ------------------------------------------------------------------
# 집주소 파싱
# ------------------------------------------------------------------

def read_home_location() -> tuple[float, float]:
    """옵시디언 집주소 파일에서 위도/경도를 읽어 (lat, lng) 튜플로 반환."""
    text = HOME_ADDRESS_PATH.read_text(encoding="utf-8")

    lat_match = re.search(r"위도\s*:\s*([\d.]+)", text)
    lng_match = re.search(r"경도\s*:\s*([\d.]+)", text)

    if not lat_match or not lng_match:
        raise ValueError(f"집주소 파일에서 위도/경도를 찾을 수 없습니다: {HOME_ADDRESS_PATH}")

    return float(lat_match.group(1)), float(lng_match.group(1))


# ------------------------------------------------------------------
# URL 생성
# ------------------------------------------------------------------

def _naver_url(
    start_lat: float,
    start_lng: float,
    end_lat: float,
    end_lng: float,
    start_name: str = "출발지",
    end_name: str = "목적지",
) -> str:
    return (
        f"http://map.naver.com/index.nhn"
        f"?slng={start_lng}&slat={start_lat}&stext={quote(start_name)}"
        f"&elng={end_lng}&elat={end_lat}&etext={quote(end_name)}"
        f"&menu=route&pathType=0"
    )


def _kakao_url(
    start_lat: float,
    start_lng: float,
    end_lat: float,
    end_lng: float,
    start_name: str = "출발지",
    end_name: str = "목적지",
) -> str:
    return (
        f"https://map.kakao.com/link/from/"
        f"{quote(start_name)},{start_lat},{start_lng}/"
        f"to/{quote(end_name)},{end_lat},{end_lng}"
    )


# ------------------------------------------------------------------
# 공개 API
# ------------------------------------------------------------------

def navigate(
    start_lat: float,
    start_lng: float,
    end_lat: float,
    end_lng: float,
    end_name: str = "목적지",
    start_name: str = "출발지",
    provider: str = "naver",
) -> None:
    """지도 앱에서 경로 안내를 엽니다.

    Args:
        start_lat: 출발지 위도
        start_lng: 출발지 경도
        end_lat:   목적지 위도
        end_lng:   목적지 경도
        end_name:  목적지 이름 (지도에 표시)
        start_name: 출발지 이름 (지도에 표시)
        provider:  "naver" 또는 "kakao"
    """
    if provider == "kakao":
        url = _kakao_url(start_lat, start_lng, end_lat, end_lng, start_name, end_name)
    else:
        url = _naver_url(start_lat, start_lng, end_lat, end_lng, start_name, end_name)

    logger.info("지도 열기: %s", url)
    subprocess.run(["open", url], check=False)


def navigate_from_home(
    end_lat: float,
    end_lng: float,
    end_name: str = "목적지",
    provider: str = "naver",
) -> None:
    """집에서 목적지까지 경로 안내를 엽니다. 집주소는 옵시디언에서 읽습니다.

    Args:
        end_lat:  목적지 위도
        end_lng:  목적지 경도
        end_name: 목적지 이름 (지도에 표시)
        provider: "naver" 또는 "kakao"
    """
    home_lat, home_lng = read_home_location()
    navigate(
        start_lat=home_lat,
        start_lng=home_lng,
        end_lat=end_lat,
        end_lng=end_lng,
        end_name=end_name,
        start_name="집",
        provider=provider,
    )


# ------------------------------------------------------------------
# CLI 테스트
# ------------------------------------------------------------------

if __name__ == "__main__":
    # 집 → 완도항 테스트
    navigate_from_home(34.3114, 126.7553, "완도항")
