# 📱 헤드리스 서버 셋업 가이드 — 아이폰에서 어디서나 JARVIS 쓰기

집에 상시 켜져 있는 맥 한 대(맥미니, 안 쓰는 맥북 등)를 서버로 만들어,
아이폰/안드로이드 브라우저에서 언제 어디서나 JARVIS에 접속하는 구성입니다.

```
[아이폰 브라우저] ──HTTPS(Tailscale)──> [상시구동 맥]
                                          ├─ 헤드리스 서버 (웹 UI + WebSocket, 포트 8767)
                                          ├─ Claude Code CLI (응답 생성)
                                          └─ (선택) Qwen TTS 서버 (JARVIS 목소리, 포트 9093)
```

- 채팅/음성 입력 → 텍스트 응답 + 음성 응답(TTS)을 브라우저로 스트리밍
- TTS 엔진이 없으면 macOS `say` 목소리로 자동 폴백 (기능은 전부 동작)

---

## 요구사항

| 항목 | 내용 |
|------|------|
| 서버 머신 | 상시 켜둘 macOS 기기 1대 (Apple Silicon 권장, 램 8GB+) |
| Python | 3.11+ |
| Claude Code CLI | [설치](https://claude.com/claude-code) + 본인 구독 계정 |
| Tailscale | [무료 계정](https://tailscale.com) — 외부 접속 + HTTPS용 |
| (선택) Qwen TTS | JARVIS 스타일 목소리를 원할 때만 (아래 참고) |

> 상시구동 팁: 시스템 설정 → 에너지 절약에서 "전원 어댑터 연결 시 잠자기 방지"를 켜거나
> `caffeinate -s`를 launchd로 돌려두세요. 서버는 화면/키보드 없이(헤드리스) 동작합니다.

---

## 1. 헤드리스 서버 설치

```bash
git clone https://github.com/kshicdi/whisperflow.git
cd whisperflow
python3 -m venv venv
./venv/bin/pip install -r requirements-headless.txt   # websockets 하나뿐
```

동작 확인:

```bash
WS_HOST=0.0.0.0 ./venv/bin/python -m whisperflow.headless
# → "Server ready — http://0.0.0.0:8767/" 로그가 뜨면 성공
# 같은 와이파이의 폰 브라우저에서 http://<맥의 IP>:8767 접속해보세요
```

| 환경변수 | 기본값 | 설명 |
|----------|--------|------|
| `WS_HOST` | `127.0.0.1` | 외부 접속을 받으려면 `0.0.0.0` |
| `WS_PORT` | `8767` | 웹 UI + WebSocket 포트 |
| `QWEN_TTS_URL` | `http://localhost:9093` | TTS 서버 주소 |
| `JARVIS_TTS_VOICE` | `clone:jarvis` | TTS 목소리 ID (본인 목소리로 교체 가능) |
| `JARVIS_TTS_SPEED` | `1.4` | 배속 (ffmpeg 필요, 없으면 원속) |
| `QWEN_TTS_TIMEOUT` | `120` | TTS 생성 대기 상한(초) |

## 2. Claude CLI 인증 (헤드리스 핵심)

헤드리스 서버에서는 키체인 OAuth가 동작하지 않으므로 **토큰 방식**을 사용합니다.

```bash
# 아무 맥에서나 한 번 실행해 장기 토큰 발급:
claude setup-token
# 발급된 sk-ant-oat01-... 토큰을 서버의 환경변수로 설정 (launchd plist에 넣는 걸 권장)
```

> ⚠️ 토큰은 절대 git에 커밋하지 마세요. launchd plist나 로컬 env 파일에만 보관.

## 3. 상시구동 등록 (launchd)

`~/Library/LaunchAgents/com.user.jarvis.plist` 생성 — 경로 3곳과 토큰을 본인 것으로 교체:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.user.jarvis</string>
  <key>ProgramArguments</key>
  <array>
    <string>/Users/USER/whisperflow/venv/bin/python</string>
    <string>-m</string>
    <string>whisperflow.headless</string>
  </array>
  <key>WorkingDirectory</key><string>/Users/USER/whisperflow</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>WS_HOST</key><string>0.0.0.0</string>
    <key>PATH</key><string>/Users/USER/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin</string>
    <key>CLAUDE_CODE_OAUTH_TOKEN</key><string>여기에-토큰</string>
  </dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>/tmp/jarvis.log</string>
  <key>StandardErrorPath</key><string>/tmp/jarvis.log</string>
</dict>
</plist>
```

```bash
launchctl load ~/Library/LaunchAgents/com.user.jarvis.plist
curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:8767/   # 200이면 성공
```

## 4. 외부 접속 (Tailscale HTTPS)

모바일 브라우저는 HTTPS가 아니면 마이크/오디오를 제한하므로 Tailscale serve를 사용합니다.

```bash
# 서버 맥에 Tailscale 설치 + 로그인 후:
tailscale serve --bg http://localhost:8767
# → https://<머신이름>.<tailnet>.ts.net 주소가 발급됨 (tailnet 내부 전용)
```

아이폰에도 Tailscale 앱을 설치하고 같은 계정으로 로그인하면, 위 주소로 어디서나 접속됩니다.
사파리에서 "홈 화면에 추가"를 하면 PWA 앱처럼 쓸 수 있습니다.

## 5. (선택) JARVIS 목소리 — Qwen TTS 서버

TTS 서버는 이 레포에 포함되어 있지 않습니다. 아래 **HTTP 규격만 맞으면 어떤 TTS 서버든**
연결됩니다 (`QWEN_TTS_URL`로 지정):

| 엔드포인트 | 규격 |
|-----------|------|
| `GET /health` | 200 응답이면 사용 가능으로 판단 |
| `POST /generate` | 요청: `{"text": "...", "voice": "clone:이름", "seed": 42, "instruct": "톤 지시문"}` → 응답: WAV 바이트 (`audio/wav`) |

추천 구성 (Apple Silicon):
- [mlx-audio](https://github.com/Blaizzy/mlx-audio) + HuggingFace의
  `mlx-community/Qwen3-TTS-12Hz-1.7B-Base-4bit` 모델 (약 2.2GB, Apache 2.0)
- 10~20초짜리 본인(또는 원하는) 목소리 WAV + 그 대사 텍스트를 참조 샘플로 주면
  음성 클로닝이 됩니다 → `JARVIS_TTS_VOICE=clone:<샘플이름>`
- 배속을 쓰려면 서버 맥에 `brew install ffmpeg`

## 6. 사용법 & 팁

- **첫 터치**: 아이폰 사파리는 화면을 한 번 터치해야 오디오 재생이 풀립니다.
- **음성 입력**: 주소 뒤에 `/jarvis.html`을 붙여 접속하면 폰 마이크(Web Speech API)로 말할 수 있습니다.
- **첫 응답 지연**: 서버 시작 직후 TTS 모델 프리로드가 돌지만, 오래 쉰 뒤 첫 응답은 수 초 걸릴 수 있습니다.

## 트러블슈팅

| 증상 | 원인/해결 |
|------|-----------|
| "Claude CLI not found" | PATH에 claude가 없거나 토큰 미설정 → plist의 PATH/토큰 확인 |
| 음성이 기계음(say)으로 나옴 | TTS 서버 다운/타임아웃 → `/health` 확인, 로그의 "폴백" 메시지 확인 |
| 접속은 되는데 마이크가 안 됨 | HTTP로 접속 중 → Tailscale HTTPS 주소로 접속 |
| 응답이 아예 없음 | 서버 로그(`/tmp/jarvis.log`) 확인 |
