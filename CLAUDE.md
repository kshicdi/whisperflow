# WhisperFlow - Claude Code Instructions

## 공개 레포 개인정보 보호 원칙 (절대 규칙)

이 레포는 **public**이다. 커밋/이슈/PR/문서에 다음을 절대 포함하지 않는다:

- 내부 네트워크 주소: Tailscale MagicDNS 주소(`*.ts.net`), tailnet 이름, 사설 IP(`192.168.*`, `100.64~127.*`), 포트가 붙은 내부 호스트명
- 계정/기기 식별자: macOS 사용자명, 기기 이름(호스트네임), 이메일, 실명
- 인증 정보: 토큰, API 키, OAuth 값, 인증 파일 경로+내용 조합
- 운영 서버의 구체 경로: 다른 머신의 홈 디렉토리 절대경로 (`/Users/<계정>/...` — `~` 표기는 허용)

**대체 방법**: 문서에는 `<서버-머신>.<tailnet>.ts.net` 같은 플레이스홀더를 쓰고, 실제 값은 프로젝트 메모리(로컬)나 개인 노트에만 기록한다.

**커밋 전 체크**: `git diff --cached`를 스캔해 위 패턴(`ts.net`, `192.168.`, `100.`, 사용자명, `sk-ant`, `token`)이 없는지 확인한 후 커밋한다. 이슈/PR 본문도 동일하게 적용.

## 프로젝트 개요
WhisperFlow는 macOS 메뉴바 음성-텍스트 변환 앱입니다. OpenAI의 Whisper 모델을 로컬에서 실행하여 음성을 텍스트로 변환합니다.

## 기술 스택
- Python 3.11+
- rumps: macOS 메뉴바 앱 프레임워크
- pynput: 전역 단축키 감지
- whisper (openai-whisper): 음성 인식
- pyaudio: 오디오 녹음
- pyobjc: macOS 네이티브 기능 (클립보드, 알림)

## 프로젝트 구조
```
whisperflow/
├── __init__.py
├── app.py              # 메인 앱 (rumps.App)
├── config.py           # 설정 관리 (dataclass)
├── hotkey_manager.py   # 전역 단축키 (pynput)
├── audio_recorder.py   # 오디오 녹음 (sounddevice)
├── transcriber.py      # Whisper 변환
├── text_output.py      # 텍스트 출력 (클립보드/타이핑)
└── history_manager.py  # 히스토리 저장
```

## 핵심 기능
1. **단축키 녹음**:
   - 짧게 탭: 토글 모드 (탭하면 녹음 시작, 다시 탭하면 중지) - 긴 녹음에 적합
   - 길게 누르기: 누르는 동안만 녹음, 떼면 변환 - 짧은 녹음에 적합
2. **Option 키 홀드**: Option 키만 길게 눌러서 녹음 (설정에서 활성화)
3. **히스토리 저장**: 음성 파일(wav)과 변환된 텍스트를 자동 저장
4. **다양한 모델**: tiny/base/small/medium/large-v3 선택 가능
5. **다국어 지원**: 한국어, 영어, 일본어, 중국어, 자동 감지

## 설정 파일 위치
`~/.config/whisperflow/config.json`

## 히스토리 저장 위치
`~/.whisperflow/history/`
```
~/.whisperflow/history/
├── 2024-01-15/              # 날짜별 폴더
│   ├── 14-30-25_audio.wav   # 시간_audio.wav
│   ├── 14-30-25_text.txt    # 시간_text.txt
│   ├── 14-35-10_audio.wav
│   ├── 14-35-10_text.txt
│   └── ...
└── 2024-01-16/
    └── ...
```

## 실행 방법
```bash
cd whisperflow
python -m whisperflow
```

## 개발 시 주의사항
- pynput은 macOS에서 접근성 권한 필요
- Whisper 모델은 첫 실행 시 다운로드됨
- 오디오 녹음은 16kHz 샘플레이트 사용 (Whisper 권장)

## 자비스 모드 (드라이브 모드 통합)

드라이브 모드에서 자비스 스타일로 응답. "연기해줘"/"역할극" 포함 시 도구 실행 없이 대사만 응답.

## Second Brain (세컨드 브레인) - 선택사항
Obsidian vault와 통합하려면 다음 환경변수를 설정하세요:
- `OBSIDIAN_VAULT_PATH`: Obsidian vault의 경로
- `HOME_ADDRESS_FILE`: 집주소가 저장된 파일 (map_navigator용)
- 기본값: 환경변수 미설정 시 로컬 기능만 사용
