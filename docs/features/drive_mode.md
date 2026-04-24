# 드라이브 모드 상시 청취 + JARVIS 음성 피드백

## 현재 상태
구현 완료 (이슈 #13, #14 closed)

## 구현된 기능

### 상시 청취
- "헤이 자비스" 웨이크 워드로 핸즈프리 음성 입력
- Silero VAD (딥러닝 기반) 묵음 감지 — 3초 묵음 시 녹음 종료
- 마이크 자동 프리셋: 에어팟(gain=12), 맥북(gain=20) 자동 감지
- 드라이브 모드 ON/OFF 시 always_listen 시작/정지

### 연속 대화 모드
- TTS 응답 후 자동으로 대화 모드 진입 (웨이크 워드 스킵)
- 10초 묵음 시 대화 종료 → "Standing by, sir." → 웨이크 워드 대기

### 영어 자비스 효과음 (Qwen TTS clone:jarvis, 1.4배속)
- yes_sir.wav — "Yes, sir." (웨이크 워드 감지)
- processing.wav — "Let me check on that, sir." (녹음 완료)
- ack_1~3.wav — "Right away/I'll get on it/On it, sir." (응답 전 랜덤)
- standby.wav — "Standing by, sir." (대화 종료)
- camera_on/off.wav — "Camera activated/deactivated, sir."
- camera_fail.wav — "Camera connection failed, sir."

### 카메라 음성 명령 (LLM 바이패스, 30자 제한)
- "카메라 켜줘" → 아이폰 카메라 (index=1)
- "맥북 카메라 켜줘" → 맥북 카메라 (index=0)
- "카메라 꺼줘" → 카메라 종료
- 중복 시작 방지

### TTS 설정
- "빠른 응답 (say 선행)" 토글 — ON: say+Qwen, OFF: Qwen만
- auto-tts.sh에서 config.json의 tts_say_first 설정 읽어 적용

### JARVIS UI
- 드라이브 모드에서 WS 연결 시 HUD 자동 표시
- Claude 응답을 JARVIS UI에 전송

## 알려진 이슈
- 아이폰 카메라: 사이드카 연결 시 Continuity Camera 사용 불가
- 카메라 연결 실패 감지는 CameraFeed 내부에서 처리 (test_cap 방식은 리소스 점유 문제)

## 다음 할 것
- 이슈 #14 추가 기능: 응답 선행 재생 (미리 녹음된 WAV 먼저 + Qwen TTS 동시 생성)
- Silero VAD 파라미터 튜닝 (환경별)
- 카메라 연결 실패 시 피드백 개선

## 관련 파일
- whisperflow/always_listen.py — 상시 청취 + Silero VAD
- whisperflow/app.py — 드라이브 모드 로직, 카메라 명령
- whisperflow/filming_scenarios.py — 시나리오 효과음
- ~/.claude/hooks/auto-tts.sh — TTS 훅 (랜덤 ack + 대화 모드)
- whisperflow/static/sounds/ — 전체 효과음 파일
