#!/bin/bash
# JARVIS Chrome - 디버그 프로필로 Chrome 실행 + 브라우저 피드 시작
# 사용법: bash scripts/jarvis_chrome.sh

CHROME="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
PROFILE="$HOME/.chrome-debug-profile"
PORT=9222
# 선택사항: 커스텀 확장 프로그램 경로를 CHROME_EXTENSIONS 환경변수로 설정 가능
EXTENSIONS="${CHROME_EXTENSIONS:-}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV_PYTHON="$SCRIPT_DIR/venv/bin/python"
JARVIS_SEND="$SCRIPT_DIR/whisperflow/jarvis_send.py"
BROWSER_FEED="$SCRIPT_DIR/whisperflow/browser_feed.py"

# 기존 Chrome 종료
osascript -e 'tell application "Google Chrome" to quit' 2>/dev/null
sleep 2

# Chrome 디버그 모드 실행
"$CHROME" \
  --remote-debugging-port=$PORT \
  --user-data-dir="$PROFILE" \
  --load-extension="$EXTENSIONS" \
  &>/dev/null &

# CDP 연결 대기
echo "Chrome 시작 대기 중..."
for i in $(seq 1 15); do
  if curl -s http://127.0.0.1:$PORT/json &>/dev/null; then
    echo "Chrome CDP 연결 성공"
    break
  fi
  sleep 1
done

# 브라우저 피드 시작
pkill -f "whisperflow.browser_feed" 2>/dev/null
sleep 1
"$VENV_PYTHON" -m whisperflow.browser_feed &>/tmp/browser_feed.log &
sleep 2

# JARVIS UI에 부팅 시퀀스 전송
"$VENV_PYTHON" "$JARVIS_SEND" ui_action "browser_boot" 2>/dev/null

echo "JARVIS Chrome 준비 완료"
