#!/bin/bash
# Claude Code 도구 사용 시각화 훅
# PreToolUse/PostToolUse 이벤트에서 호출됨

YOUTUBE_FILE="/Users/USER/.whisperflow_youtube_tts"
[ -f "$YOUTUBE_FILE" ] || exit 0

JARVIS_SEND="/Users/USER/Documents/아이디어프로그램/05.Whisperflow/whisperflow/jarvis_send.py"
VENV_PYTHON="/Users/USER/Documents/아이디어프로그램/05.Whisperflow/venv/bin/python"
[ -f "$JARVIS_SEND" ] || exit 0

# stdin에서 JSON 읽기
INPUT=$(cat)

# tool_name과 tool_input 추출 (jq 없이 python으로)
PARSED=$(/usr/bin/python3 -c "
import json, sys
try:
    data = json.loads('''$INPUT''')
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
