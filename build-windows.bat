@echo off
setlocal
cd /d "%~dp0"

REM ===== 授权服务器地址 =====
if not "%TG_CARD_LICENSE_URL%"=="" (
  powershell -NoProfile -Command "$u='%TG_CARD_LICENSE_URL%'.TrimEnd('/'); @{license_url=$u} | ConvertTo-Json | Set-Content -Encoding utf8 static\license_config.json"
  echo License server URL: %TG_CARD_LICENSE_URL%
) else (
  echo Using existing static\license_config.json
)

REM ===== Python 环境 =====
if not exist venv\Scripts\python.exe (
  echo Creating virtual environment...
  py -3.12 -m venv venv
)

echo Installing dependencies...
venv\Scripts\python.exe -m pip install --upgrade pip
venv\Scripts\python.exe -m pip install -r requirements.txt

REM ===== PyInstaller 打包 =====
echo Building Windows executable...
venv\Scripts\python.exe -m PyInstaller ^
  --noconfirm ^
  --windowed ^
  --name "TelegramCardTool" ^
  --add-data "static;static" ^
  --add-data "packaging\Telegram名片工具-使用文档.pdf;." ^
  --collect-all telethon ^
  desktop.py

if errorlevel 1 (
  echo.
  echo [ERROR] Build failed!
  exit /b 1
)

REM ===== 打包 zip =====
copy /Y "packaging\Telegram名片工具-使用文档.pdf" "dist\TelegramCardTool\Telegram名片工具-使用文档.pdf" >nul
if errorlevel 1 (
  echo [ERROR] Failed to copy PDF guide!
  exit /b 1
)

echo Zipping output...
powershell -NoProfile -Command "Compress-Archive -Path 'dist\TelegramCardTool\*' -DestinationPath 'dist\TelegramCardTool-Windows-x64.zip' -Force"

echo.
echo ============================================
echo  Build complete!
echo  Output: %CD%\dist\TelegramCardTool-Windows-x64.zip
echo ============================================
endlocal
