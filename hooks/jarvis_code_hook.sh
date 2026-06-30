#!/bin/bash
# Claude Code 도구 사용 시각화 훅
# PreToolUse/PostToolUse 이벤트에서 호출됨

export LANG=ko_KR.UTF-8
export LC_ALL=ko_KR.UTF-8

YOUTUBE_FILE="$HOME/.whisperflow_youtube_tts"
[ -f "$YOUTUBE_FILE" ] || exit 0

# WhisperFlow 설치 위치를 환경변수로 지정 (기본값: 홈 디렉토리 탐색)
if [ -z "$WHISPERFLOW_DIR" ]; then
    # 일반적인 설치 경로 탐색
    for path in "$HOME/.local/share/whisperflow" "$HOME/whisperflow" "$(dirname "$(cd "$(dirname "${BASH_SOURCE[0]}")"/.. && pwd)")"; do
        if [ -f "$path/whisperflow/jarvis_send.py" ]; then
            WHISPERFLOW_DIR="$path"
            break
        fi
    done
fi
[ -z "$WHISPERFLOW_DIR" ] && exit 0
JARVIS_SEND="$WHISPERFLOW_DIR/whisperflow/jarvis_send.py"
VENV_PYTHON="$WHISPERFLOW_DIR/venv/bin/python"
[ -f "$JARVIS_SEND" ] || exit 0

# stdin에서 JSON 읽어서 Python으로 파싱
INPUT=$(cat)

# tool_name과 tool_input 추출
PARSED=$(echo "$INPUT" | /usr/bin/python3 -c "
import json, sys
try:
    data = json.loads(sys.stdin.read())
    tool = data.get('tool_name', '')
    inp = data.get('tool_input', {})

    if tool == 'Edit':
        path = inp.get('file_path', '').split('/')[-1]
        old = (inp.get('old_string', '')[:80]).replace('\\n', ' ')
        new = (inp.get('new_string', '')[:80]).replace('\\n', ' ')
        print(f'EDITING|{path}|- {old}|+ {new}')
    elif tool == 'Write':
        path = inp.get('file_path', '').split('/')[-1]
        content = (inp.get('content', '')[:100]).replace('\\n', ' ')
        print(f'WRITING|{path}|{content}')
    elif tool == 'Read':
        path = inp.get('file_path', '').split('/')[-1]
        print(f'READING|{path}|')
    elif tool == 'Bash':
        cmd = (inp.get('command', '')[:100]).replace('\\n', ' ')
        print(f'EXECUTING|bash|{cmd}')
    elif tool == 'Grep':
        pattern = inp.get('pattern', '')
        path = inp.get('path', '').split('/')[-1] if inp.get('path') else '*'
        print(f'SEARCHING|\"{pattern}\"|in {path}')
    elif tool == 'Glob':
        pattern = inp.get('pattern', '')
        print(f'SCANNING|{pattern}|')
    elif tool == 'Agent':
        desc = inp.get('description', '')[:60]
        print(f'AGENT|{desc}|')
    else:
        print(f'{tool.upper()}||')
except:
    pass
" 2>/dev/null)

[ -z "$PARSED" ] && exit 0

"$VENV_PYTHON" "$JARVIS_SEND" code_action "$PARSED" 2>/dev/null &
exit 0
