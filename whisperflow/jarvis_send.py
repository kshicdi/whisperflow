#!/usr/bin/env python3
"""
JARVIS UI에 메시지를 전송하는 유틸리티.

사용법:
  python3 -m whisperflow.jarvis_send output "Claude의 응답 텍스트"
  python3 -m whisperflow.jarvis_send input "사용자 입력 텍스트"
  python3 -m whisperflow.jarvis_send state "idle|recording|processing|tts_playing"

Claude Code Hook에서 호출됩니다.
"""
import asyncio
import json
import sys

WS_URL = "ws://localhost:8767"


async def send_message(msg_type: str, value: str):
    try:
        import websockets
        async with websockets.connect(WS_URL, close_timeout=2, open_timeout=2) as ws:
            await ws.send(json.dumps({"type": msg_type, "value": value}))
            # 서버가 메시지를 처리할 시간 확보 후 정상 종료
            await asyncio.sleep(0.2)
    except Exception:
        # Server not running - silently ignore
        pass


def main():
    if len(sys.argv) < 2:
        print(f"Usage: {sys.argv[0]} <type> <value>")
        print(f"       {sys.argv[0]} <type> --stdin  (read value from stdin)")
        print("  type: input | output | state | transcript | tts_audio")
        sys.exit(1)

    msg_type = sys.argv[1]

    # --stdin 모드: 대용량 데이터(base64 오디오 등)를 stdin으로 받기
    if len(sys.argv) >= 3 and sys.argv[2] == "--stdin":
        value = sys.stdin.read()
    elif len(sys.argv) >= 3:
        value = " ".join(sys.argv[2:])
    else:
        print(f"Usage: {sys.argv[0]} <type> <value>")
        sys.exit(1)

    asyncio.run(send_message(msg_type, value))


if __name__ == "__main__":
    main()
