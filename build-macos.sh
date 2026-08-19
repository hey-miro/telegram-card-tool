#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

APP_NAME="Telegram名片工具"
PDF_GUIDE="$SCRIPT_DIR/packaging/Telegram名片工具-使用文档.pdf"
PACKAGE_DIR="$SCRIPT_DIR/dist/${APP_NAME}-macOS-arm64"

# 写入授权服务器地址(打包后客户端据此连接授权服务;未设置则使用已有的 static/license_config.json)
if [[ -n "${TG_CARD_LICENSE_URL:-}" ]]; then
  printf '{"license_url": "%s"}\n' "${TG_CARD_LICENSE_URL%/}" > static/license_config.json
  echo "License server URL: ${TG_CARD_LICENSE_URL%/}"
fi

PYINSTALLER_ARGS=(
  --noconfirm
  --windowed
  --name "Telegram名片工具"
  --osx-bundle-identifier "com.yuzhitongtong.telegram-card-tool"
  --add-data "static:static"
  --add-data "$PDF_GUIDE:."
  --collect-all telethon
)

if [[ -n "${CODESIGN_IDENTITY:-}" ]]; then
  PYINSTALLER_ARGS+=(--codesign-identity "$CODESIGN_IDENTITY")
fi

./venv/bin/python -m PyInstaller \
  "${PYINSTALLER_ARGS[@]}" \
  desktop.py

if [[ -e "$PACKAGE_DIR" ]]; then
  mv "$PACKAGE_DIR" "$HOME/.Trash/$(basename "$PACKAGE_DIR")-$(date +%H%M%S)"
fi
mkdir -p "$PACKAGE_DIR"
ditto "$SCRIPT_DIR/dist/${APP_NAME}.app" "$PACKAGE_DIR/${APP_NAME}.app"
cp "$PDF_GUIDE" "$PACKAGE_DIR/Telegram名片工具-使用文档.pdf"
ditto -c -k --keepParent "$PACKAGE_DIR" "$SCRIPT_DIR/dist/${APP_NAME}-macOS-arm64.zip"

echo "Built: $SCRIPT_DIR/dist/${APP_NAME}-macOS-arm64.zip"
