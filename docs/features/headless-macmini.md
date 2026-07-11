# 헤드리스 맥미니 서버 (자비스 상시 구동)

## 목적
맥북이 꺼져 있어도 아이폰에서 자비스에 접속할 수 있도록, 맥미니에서 웹 서버만 상시 구동.

## 현재 상태 (2026-07-11 구축 완료)
- **접속 주소**: `https://<서버-머신>.<tailnet>.ts.net` (테일스케일 tailnet 전용, HTTPS — 실제 주소는 개인 메모 참조)
- 기존 개발머신 serve 주소도 변경 없음, 병행 사용 가능.
- 서버에서 HTTP 200 + Claude CLI 응답 검증 완료.

## 구성 요소
| 항목 | 내용 |
|------|------|
| 진입점 | `python -m whisperflow.headless` (rumps/GUI 미참조) |
| 바인드 | `WS_HOST=0.0.0.0`, `WS_PORT=8767` (환경변수, 기본값은 127.0.0.1:8767) |
| 의존성 | `requirements-headless.txt` — websockets 하나뿐 |
| 맥미니 레포 | `~/server/apps/whisperflow` (pull-all.sh가 30분마다 자동 pull → 맥북에서 push만 하면 반영) |
| venv | `~/server/apps/whisperflow/venv` (python3.13) |
| launchd | `~/Library/LaunchAgents/com.server.jarvis.plist` (KeepAlive, 로그 `~/server/logs/jarvis.log`) |
| Claude 인증 | plist 환경변수 `CLAUDE_CODE_OAUTH_TOKEN` (헤드리스는 파일/env 기반 인증만 동작) |
| HTTPS | `tailscale serve --bg http://localhost:8767` (CLI: `/opt/homebrew/bin/tailscale --socket=/var/run/tailscaled.socket`) |

## 구현 결정 사항
- ws_server.py 자체는 GUI 의존성이 없어 분리 가능 → app.py를 거치지 않는 `headless.py` 진입점만 신설 (기존 맥북 모드 무변경).
- 콜백 4개(_on_remote_record, _on_chat_tts 등) 미등록 시 ws_server가 무시하는 기존 구조를 그대로 활용 — 코드 수정 최소화.
- 맥미니 tailscaled는 스탠드얼론(root LaunchDaemon)이라 CLI는 brew 것을 `--socket=/var/run/tailscaled.socket`으로 붙여 사용. 심볼릭 링크 방식(tailscaled multicall)은 이 빌드에서 안 됨.

## 헤드리스 TTS (2026-07-11 추가 구현)
- `whisperflow/headless_tts.py` — 채팅 응답을 Qwen TTS(clone:jarvis)로 WAV 생성 → base64 `tts_audio` WS 브로드캐스트 (GUI 모드의 app.py `_handle_chat_tts`와 동일 파이프라인, ~/.claude/hooks 비의존 자체 구현).
- Qwen 서버 미응답 시 `say -v Yuna` 폴백. 새 응답 도착 시 세대 카운터로 진행 중 TTS 청크 경계 중단.
- 환경변수: `QWEN_TTS_URL`(기본 localhost:9093), `JARVIS_TTS_VOICE`(clone:jarvis), `JARVIS_TTS_SPEED`(1.4, ffmpeg atempo).
- 맥미니 Qwen TTS: `~/server/apps/qwen3-tts/` (serve.py + 1.7B-Base-4bit 모델 2.2G HF 다운로드 + voices/ 맥북에서 복사), launchd `com.server.qwen-tts` (포트 9093, 로그 `~/server/logs/qwen-tts.log`).
- E2E 검증 완료: chat_input → claude → tts_audio 2건(RIFF WAV) → tts_done. 첫 요청은 모델 로딩으로 ~22초, 이후 빨라짐.

## 제한 사항 (헤드리스 모드)
- **모바일 음성 입력 주의**: 모바일 UA는 `assistant.html`이 자동 서빙되는데, 이 페이지의 마이크 버튼은 `remote_record`(서버 마이크) 방식이라 헤드리스에서 무동작. 음성 입력이 필요하면 `/jarvis.html`로 직접 접속 (Web Speech API, 기기 마이크 사용).
- 히스토리/설정은 맥미니 로컬(`~/.whisperflow/`, `~/.config/whisperflow/`)에 별도 저장 — 맥북과 공유 안 됨.

## 다음 할 것
- [ ] 모바일 UA → jarvis.html 서빙하도록 변경 검토 (assistant.html의 remote_record 의존 해소)
- [ ] iPhone 실기기 테스트 (Web Speech API 음성 입력 + TTS 재생)
- [ ] 첫 요청 TTS 지연(모델 로딩) 개선 — serve.py 기동 시 모델 프리로드 검토

## 관련 파일
- `whisperflow/headless.py` — 헤드리스 진입점
- `whisperflow/ws_server.py:28` — WS_HOST/WS_PORT 환경변수
- 맥미니: `~/Library/LaunchAgents/com.server.jarvis.plist`, `~/server/logs/jarvis.log`
- 이슈: #50
