@echo off
chcp 65001 >nul
python -c "import requests, playwright, imageio_ffmpeg, yt_dlp, curl_cffi" 2>nul || (
    echo 처음 실행 또는 업데이트: 필요한 패키지 설치 중...
    python -m pip install -q requests playwright imageio-ffmpeg "yt-dlp[default,curl-cffi]"
)
python "%~dp0vdl.py" %*
pause
