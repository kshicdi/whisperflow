# TTS 사운드 맵 — 어떤 상황에서 어떤 소리가 나오는가

## 경로 1: 촬영 시나리오 (filming_scenarios.py)

자비스 모드(`~/.whisperflow_jarvis_roleplay` 존재)에서 음성 인식 완료 후 동작.

### 고정 시나리오 (키워드 매칭 → 미리 녹음된 wav)

| 키워드 | 사운드 파일 | 대사 | 비고 |
|--------|-----------|------|------|
| 온라인 | (부팅 시퀀스) | - | AppLauncher.jarvis_online |
| 음악 틀어 | music_play.wav | "Playing music, sir." | Apple Music 실행 |
| 유튜브 열어 | youtube_open.wav | "Opening YouTube, sir." | Chrome 유튜브 |
| 카카오톡 열어 | kakao_open.wav | "Opening KakaoTalk, sir." | 앱 실행 |
| 크롬 열어 | chrome_open.wav | "Opening Chrome, sir." | 앱 실행 |
| 카톡 보내 | kakao_sent.wav | "Message sent via KakaoTalk, sir." | 메시지 전송 |
| 카메라 켜 | camera_on.wav | "Camera activated, sir." | 카메라 피드 |
| 카메라 꺼 | camera_off.wav | "Camera deactivated, sir." | 카메라 종료 |

→ 여기에는 **ack 효과음 없음.** 바로 해당 wav 재생.

### 일반 질문 (키워드 매칭 안 됨 → Claude CLI + Qwen TTS)

```
사용자 음성 → STT 완료
  → filming_scenarios._handle_general()
  → Claude CLI 호출 (subprocess, 자비스 프롬프트)
  → Qwen TTS로 음성 생성 (자비스 목소리 클론)
  → afplay 재생
```

→ 여기에도 **ack 효과음 없음.** Claude 처리 시간이 곧 대기 시간.

---

## 경로 2: 드라이브/유튜브 모드 (auto-tts.sh 훅)

Claude Code 응답 완료(Stop 이벤트) 후 훅에서 동작.

### 유튜브 모드 (`~/.whisperflow_youtube_tts`)

```
Claude 응답 완료
  → qwen_tts_speak.py --no-say (Qwen TTS만)
  → 종료
```

→ **ack 효과음 없음.**

### 드라이브 모드 (`~/.whisperflow_auto_tts`)

```
Claude 응답 완료
  → JARVIS UI에 응답 전송
  → ★ ack 효과음 랜덤 재생 (ack_1~3.wav)  ← 여기!
  → qwen_tts_speak.py (Qwen TTS 본 응답)
  → conversation_continue (대화 모드 진입)
```

→ **ack가 나오는 유일한 경로.** 본 응답 전에 "확인했다" 효과음.

---

## 현재 ack 파일 목록 (3개)

| 파일 | 길이 | 추정 대사 |
|------|------|----------|
| ack_1.wav | 1.0초 | (확인 필요) |
| ack_2.wav | 1.2초 | (확인 필요) |
| ack_3.wav | 0.9초 | (확인 필요) |

→ 3개뿐이라 반복감이 심함.

---

## 추가가 필요한 곳

### 1. 드라이브 모드 ack (auto-tts.sh) — 현재 3개 → 10개 이상 필요

Claude가 처리하는 동안 "알겠습니다" 느낌의 응답. 톤을 다양하게:

| # | 대사 | 톤 |
|---|------|-----|
| 기존1 | (현재 ack_1 내용) | - |
| 기존2 | (현재 ack_2 내용) | - |
| 기존3 | (현재 ack_3 내용) | - |
| 추가4 | "Certainly, sir" | 격식 |
| 추가5 | "As you wish" | 격식 |
| 추가6 | "On it" | 짧음/캐주얼 |
| 추가7 | "Understood" | 사무적 |
| 추가8 | "Consider it done" | 위트 |
| 추가9 | "I thought you'd never ask" | 위트 |
| 추가10 | "One moment" | 사무적 |
| 추가11 | "Processing now" | 사무적 |
| 추가12 | (전자 비프음만) | 무음/효과음 |

### 2. 촬영 시나리오 일반 질문 — ack 없음 (추가 검토)

현재 Claude CLI 호출 동안 무음. 처리 시간이 2~5초인데 이 동안 아무 소리 없음.
→ 여기에도 ack를 넣으면 "자비스가 듣고 처리 중" 느낌이 남.

### 3. 고정 시나리오 — 대사가 하나뿐 (추가 검토)

"Opening YouTube, sir."가 매번 동일.
→ 같은 액션이라도 2~3개 변형을 두면 자연스러움.
예: "YouTube coming right up, sir" / "Loading YouTube now"

---

## 관련 파일

- `whisperflow/static/sounds/ack_*.wav` — ack 효과음
- `~/.claude/hooks/auto-tts.sh` — 드라이브 모드 훅 (ack 재생)
- `whisperflow/filming_scenarios.py` — 촬영 시나리오 (고정 wav + Claude CLI)
- `~/.claude/hooks/qwen_tts_speak.py` — Qwen TTS 호출
