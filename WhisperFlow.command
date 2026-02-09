#!/bin/bash
# WhisperFlow 실행 스크립트

cd "$(dirname "$0")"
source venv/bin/activate
python -m whisperflow
