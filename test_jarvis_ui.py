"""
JARVIS UI 테스트 스크립트
WebSocket 서버를 띄우고 데모 시나리오를 재생합니다.

사용법:
  python3 test_jarvis_ui.py

브라우저에서 http://localhost:8767 접속 후 자동 데모가 재생됩니다.
"""
import sys
import os
sys.path.insert(0, os.path.dirname(__file__))

from whisperflow.ws_server import WhisperFlowWSServer
import time
import math
import webbrowser

def main():
    server = WhisperFlowWSServer()
    server.start()
    time.sleep(1)

    print("=" * 50)
    print("  JARVIS UI Test Server")
    print("  http://localhost:8767")
    print("=" * 50)
    print()

    # 브라우저 자동 오픈
    webbrowser.open("http://localhost:8767")

    print("브라우저가 열렸습니다. 데모를 시작합니다...")
    print("Ctrl+C로 종료")
    print()

    try:
        while True:
            # 1) STANDBY (5초)
            print("[STANDBY] 대기 중...")
            server.broadcast_state("idle")
            for i in range(50):
                level = 0.02 + 0.01 * math.sin(time.time() * 2)
                server.broadcast_audio_level(level)
                time.sleep(0.1)

            # 2) RECORDING (5초) - 오디오 레벨 시뮬레이션
            print("[RECORDING] 녹음 중...")
            server.broadcast_state("recording")
            for i in range(50):
                t = time.time()
                level = 0.3 + 0.3 * math.sin(t * 3) + 0.15 * math.sin(t * 7) + 0.1 * math.sin(t * 13)
                level = max(0.05, min(1.0, level))
                server.broadcast_audio_level(level)
                time.sleep(0.1)

            # 3) PROCESSING (3초)
            print("[PROCESSING] 변환 중...")
            server.broadcast_state("processing")
            server.broadcast_audio_level(0.0)
            time.sleep(3)

            # 4) 텍스트 결과 + IDLE
            print("[DONE] 변환 완료")
            server.broadcast_transcript("안녕하세요, 오늘은 자비스 스타일의 AI 어시스턴트를 만들어 보겠습니다")
            server.broadcast_state("idle")
            time.sleep(3)

            # 5) TTS 재생 (4초)
            print("[SPEAKING] TTS 재생 중...")
            server.broadcast_state("tts_playing")
            for i in range(40):
                t = time.time()
                level = 0.4 + 0.3 * math.sin(t * 5)
                server.broadcast_audio_level(level)
                time.sleep(0.1)

            # 6) 다시 IDLE
            server.broadcast_state("idle")
            server.broadcast_audio_level(0.0)
            print("[CYCLE COMPLETE] 3초 후 다시 시작...\n")
            time.sleep(3)

    except KeyboardInterrupt:
        print("\n종료합니다...")
        server.stop()

if __name__ == "__main__":
    main()
