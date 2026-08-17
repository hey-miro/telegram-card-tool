#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

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
  --collect-all telethon
)

if [[ -n "${CODESIGN_IDENTITY:-}" ]]; then
  PYINSTALLER_ARGS+=(--codesign-identity "$CODESIGN_IDENTITY")
fi

./venv/bin/python -m PyInstaller \
  "${PYINSTALLER_ARGS[@]}" \
  desktop.py

ditto -c -k --keepParent \
  "$SCRIPT_DIR/dist/Telegram名片工具.app" \
  "$SCRIPT_DIR/dist/Telegram名片工具-macOS-arm64.zip"

echo "Built: $SCRIPT_DIR/dist/Telegram名片工具-macOS-arm64.zip"
