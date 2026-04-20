#!/bin/bash
# Claude Code Hook - JARVIS UI에 응답 전송
# after_assistant_response 이벤트에서 호출됨

WHISPERFLOW_DIR="$(cd "$(dirname "$0")/.." && pwd)"

# 응답 텍스트를 stdin에서 읽음
RESPONSE=$(cat)

# 빈 응답 무시
[ -z "$RESPONSE" ] && exit 0

# 응답의 처음 500자만 전송 (UI 표시용)
TRUNCATED="${RESPONSE:0:500}"

# JARVIS UI로 전송 (백그라운드, 실패해도 무시)
python3 "$WHISPERFLOW_DIR/whisperflow/jarvis_send.py" output "$TRUNCATED" 2>/dev/null &

exit 0
