"""py2app 빌드 설정"""

from setuptools import setup

APP = ["whisperflow/app.py"]
DATA_FILES = []
OPTIONS = {
    "argv_emulation": True,
    "plist": {
        "CFBundleName": "WhisperFlow",
        "CFBundleDisplayName": "WhisperFlow",
        "CFBundleIdentifier": "com.whisperflow.app",
        "CFBundleVersion": "0.1.0",
        "CFBundleShortVersionString": "0.1.0",
        "LSUIElement": True,  # 메뉴바 앱 (독에 표시 안 함)
        "NSMicrophoneUsageDescription": "음성을 텍스트로 변환하기 위해 마이크 접근이 필요합니다.",
    },
    "packages": [
        "rumps",
        "faster_whisper",
        "sounddevice",
        "numpy",
        "pynput",
        "pyperclip",
    ],
}

setup(
    name="WhisperFlow",
    app=APP,
    data_files=DATA_FILES,
    options={"py2app": OPTIONS},
    setup_requires=["py2app"],
)
