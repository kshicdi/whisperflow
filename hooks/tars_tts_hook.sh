#!/bin/bash
# TARS TTS Hook (Stop 이벤트용)
# ~/.whisperflow_tars_mode 활성화 시 Claude 응답을 TARS 음성으로 변환/재생
#
# auto-tts.sh와 동일 패턴:
#   1. 플래그 파일 체크
#   2. JSONL에서 최신 응답 추출
#   3. 중복 방지 (해시)
#   4. 필러 재생 -> Qwen TTS(clone:tars) + TARS_FX 후처리 -> 재생

TARS_FILE="$HOME/.whisperflow_tars_mode"
[ -f "$TARS_FILE" ] || exit 0

# JSONL 기록 대기
sleep 0.5

EXTRACT_RESPONSE_SCRIPT="${CLAUDE_HOOKS_DIR:-$HOME/.claude/hooks}/extract_response.py"
RESPONSE=$(/usr/bin/python3 "$EXTRACT_RESPONSE_SCRIPT" 2>/dev/null)
[ -z "$RESPONSE" ] && exit 0

# 같은 응답 중복 방지
HASH_FILE="/tmp/whisperflow-tars-last-hash"
LOCKFILE="/tmp/whisperflow-tars-hash.lock"
(
    while ! mkdir "$LOCKFILE" 2>/dev/null; do sleep 0.1; done
    trap "rmdir $LOCKFILE 2>/dev/null" EXIT

    CURRENT_HASH=$(echo "$RESPONSE" | md5)
    if [ -f "$HASH_FILE" ] && [ "$(cat "$HASH_FILE")" = "$CURRENT_HASH" ]; then
        exit 1
    fi
    echo "$CURRENT_HASH" > "$HASH_FILE"
    exit 0
)
[ $? -ne 0 ] && exit 0

# --- TARS 필러 재생 (응답 생성 대기 중 "Hmm..." 등) ---
FILLERS_DIR="${TARS_FILLERS_DIR:-}"
if [ -n "$FILLERS_DIR" ] && [ -d "$FILLERS_DIR" ]; then
    FILLER_FILES=("$FILLERS_DIR"/*.wav)
    FILLER_COUNT=${#FILLER_FILES[@]}
    if [ "$FILLER_COUNT" -gt 0 ]; then
        FILLER_IDX=$(python3 -c "import random; print(random.randint(0, $FILLER_COUNT - 1))")
        afplay "${FILLER_FILES[$FILLER_IDX]}"
    fi
fi

# --- TTS 재생 중 플래그 ---
TTS_PLAYING_FLAG="/tmp/whisperflow-tts-playing"
touch "$TTS_PLAYING_FLAG"

# --- Qwen TTS (clone:tars) + TARS FX 후처리 ---
TARS_FX="atempo=1.15,highpass=f=300,lowpass=f=3500,equalizer=f=1000:t=q:w=0.5:g=4,equalizer=f=2800:t=q:w=1:g=3,aecho=0.8:0.7:6|10|15|20:0.4|0.3|0.2|0.1,compand=attacks=0.005:decays=0.05:points=-80/-80|-40/-25|-20/-12|0/-8|20/-5:gain=4"
QWEN_URL="http://localhost:9093"

# Python으로 TTS 생성 + 후처리 + 재생
/usr/bin/python3 - "$RESPONSE" "$TARS_FX" "$QWEN_URL" <<'PYEOF'
import sys
import json
import tempfile
import subprocess
import os
import re
import urllib.request

text = sys.argv[1]
tars_fx = sys.argv[2]
qwen_url = sys.argv[3]

# 문장 분리 (2-3문장씩 묶기)
def split_sentences(text):
    sentences = re.split(r'(?<=[.!?])\s+', text)
    sentences = [s.strip() for s in sentences if s.strip()]
    if not sentences:
        return [text]
    chunks = []
    current = ""
    count = 0
    for s in sentences:
        current = (current + " " + s).strip() if current else s
        count += 1
        if count >= 3 or len(current) >= 150:
            chunks.append(current)
            current = ""
            count = 0
    if current:
        if chunks and len(current) < 50:
            chunks[-1] += " " + current
        else:
            chunks.append(current)
    return chunks if chunks else [text]

chunks = split_sentences(text)

for chunk in chunks:
    # Qwen TTS 생성
    payload = json.dumps({
        "text": chunk,
        "voice": "clone:tars",
        "seed": 42,
        "instruct": ""
    }).encode("utf-8")

    req = urllib.request.Request(
        f"{qwen_url}/generate",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST"
    )

    try:
        resp = urllib.request.urlopen(req, timeout=60)
        wav_bytes = resp.read()
    except Exception as e:
        print(f"[TARS TTS] Generate error: {e}", file=sys.stderr)
        continue

    # 후처리
    tmp_in = tempfile.mktemp(suffix=".wav")
    tmp_out = tempfile.mktemp(suffix=".wav")
    try:
        with open(tmp_in, "wb") as f:
            f.write(wav_bytes)

        result = subprocess.run(
            ["ffmpeg", "-i", tmp_in, "-af", tars_fx, tmp_out, "-y"],
            capture_output=True, timeout=10
        )

        play_file = tmp_out if result.returncode == 0 else tmp_in
        subprocess.run(["afplay", play_file], timeout=120)
    except Exception as e:
        print(f"[TARS TTS] Play error: {e}", file=sys.stderr)
    finally:
        for p in (tmp_in, tmp_out):
            try:
                os.unlink(p)
            except OSError:
                pass
PYEOF

# --- TTS 재생 완료 ---
rm -f "$TTS_PLAYING_FLAG"

# TTS 완료 -> 대화 모드 진입 (웨이크 워드 없이 바로 음성 대기)
touch /tmp/whisperflow-conversation-continue

exit 0
