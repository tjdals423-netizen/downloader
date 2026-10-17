@echo off
chcp 65001 >nul
python -c "import requests, playwright, imageio_ffmpeg" 2>nul || (
    echo 처음 실행: 필요한 패키지 설치 중...
    python -m pip install -q requests playwright imageio-ffmpeg
)
python "%~dp0vdl.py" %*
pause
