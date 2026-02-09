# WhisperFlow

macOS 메뉴바에서 실행되는 실시간 음성-텍스트 변환 앱

## 개요

OpenAI Whisper 모델을 로컬에서 실행하여 음성을 텍스트로 변환합니다.
단축키로 녹음하고, 변환된 텍스트가 자동으로 커서 위치에 입력됩니다.

## 주요 기능

- **단축키 녹음**: `Cmd+Ctrl+Option+Shift+A` 꾹 누르기 또는 더블클릭
- **자동 입력**: 변환된 텍스트가 커서 위치에 자동 붙여넣기
- **오프라인 동작**: 인터넷 없이 로컬에서 처리 (프라이버시 보장)
- **문장 줄바꿈**: 문장 끝(. ! ?)에서 자동 줄바꿈
- **모델 선택**: tiny / base / small / medium / large-v3
- **언어 선택**: 한국어, 영어, 일본어, 중국어, 자동감지

## 설치

### 요구사항
- macOS 10.13+
- Python 3.9+

### 설치 방법

```bash
cd /Users/USER/Documents/아이디어프로그램/05.Whisperflow
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

## 실행

### 방법 1: 바탕화면 실행 파일
`~/Desktop/WhisperFlow.command` 더블클릭

### 방법 2: 터미널
```bash
cd /Users/USER/Documents/아이디어프로그램/05.Whisperflow
source venv/bin/activate
python -m whisperflow
```

## 사용법

### 단축키
| 동작 | 설명 |
|------|------|
| `Cmd+Ctrl+Option+Shift+A` 꾹 누르기 | 누르는 동안 녹음, 떼면 변환 |
| `Cmd+Ctrl+Option+Shift+A` 더블클릭 | 토글 모드 (다시 누르면 종료) |

### 메뉴바
1. 🎤 아이콘 클릭
2. "녹음 시작/중지" - 수동 녹음
3. "모델 선택" - Whisper 모델 변경
4. "언어 선택" - 인식 언어 변경
5. "단축키 설정" - 키 조합 변경

### 상태 아이콘
| 아이콘 | 상태 |
|--------|------|
| 🎤 | 대기 중 |
| 🔴 | 녹음 중 |
| ⏳ | 변환 중 |

## 설정

설정 파일: `~/.config/whisperflow/config.json`

```json
{
  "model_size": "small",
  "language": "ko",
  "hotkey": "cmd+ctrl+option+shift+a",
  "output_mode": "type",
  "sample_rate": 16000
}
```

### 모델 비교
| 모델 | 정확도 | 속도 | 메모리 |
|------|--------|------|--------|
| tiny | ⭐⭐ | 가장 빠름 | ~1GB |
| base | ⭐⭐⭐ | 빠름 | ~1GB |
| small | ⭐⭐⭐⭐ | 보통 | ~2GB |
| medium | ⭐⭐⭐⭐⭐ | 느림 | ~5GB |
| large-v3 | ⭐⭐⭐⭐⭐⭐ | 가장 느림 | ~10GB |

## 권한 설정

### 필수 권한
1. **마이크**: 시스템 설정 → 개인정보 보호 및 보안 → 마이크 → 터미널 허용
2. **접근성**: 시스템 설정 → 개인정보 보호 및 보안 → 접근성 → 터미널 허용

## 프로젝트 구조

```
whisperflow/
├── __init__.py          # 패키지 초기화
├── __main__.py          # 모듈 실행 진입점
├── app.py               # 메인 메뉴바 앱 (rumps)
├── audio_recorder.py    # sounddevice 녹음
├── config.py            # 설정 관리
├── hotkey_manager.py    # pynput 단축키
├── text_output.py       # 클립보드/타이핑
└── transcriber.py       # faster-whisper 변환
```

## 기술 스택

- **rumps**: macOS 메뉴바 앱 프레임워크
- **faster-whisper**: Whisper보다 4배 빠른 로컬 STT
- **sounddevice**: 오디오 녹음
- **pynput**: 전역 단축키
- **pyperclip**: 클립보드 관리

## 로그인 시 자동 실행

이미 설정됨. 확인/변경:
- 시스템 설정 → 일반 → 로그인 항목 → WhisperFlow.command

## 문제 해결

### 단축키가 안 될 때
1. 접근성 권한 확인
2. 앱 재시작
3. 단축키 조합 변경 (메뉴에서)

### 녹음이 안 될 때
1. 마이크 권한 확인
2. 시스템 환경설정 → 사운드 → 입력 장치 확인

### 변환이 느릴 때
1. 모델을 작은 것으로 변경 (tiny, base)
2. 첫 실행 시 모델 다운로드로 오래 걸림

## 라이선스

MIT License
