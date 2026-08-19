#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

APP_NAME="Telegram名片工具"
PDF_GUIDE="$SCRIPT_DIR/packaging/Telegram名片工具-使用文档.pdf"
PACKAGE_DIR="$SCRIPT_DIR/dist/${APP_NAME}-macOS-arm64"
ZIP_PATH="$SCRIPT_DIR/dist/${APP_NAME}-macOS-arm64.zip"
APP_OUTPUT="$SCRIPT_DIR/dist/${APP_NAME}.app"
BUILD_ROOT="$(mktemp -d "${TMPDIR:-/tmp}/telegram-card-tool-macos.XXXXXX")"
STAGED_PACKAGE_DIR="$BUILD_ROOT/${APP_NAME}-macOS-arm64"
STAGED_ZIP="$BUILD_ROOT/${APP_NAME}-macOS-arm64.zip"

cleanup() {
  rm -rf "$BUILD_ROOT"
}
trap cleanup EXIT

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
  --add-data "$SCRIPT_DIR/static:static"
  --add-data "$PDF_GUIDE:."
  --collect-all telethon
)

if [[ -n "${CODESIGN_IDENTITY:-}" ]]; then
  PYINSTALLER_ARGS+=(--codesign-identity "$CODESIGN_IDENTITY")
fi

./venv/bin/python -m PyInstaller \
  --distpath "$BUILD_ROOT/dist" \
  --workpath "$BUILD_ROOT/build" \
  --specpath "$BUILD_ROOT" \
  "${PYINSTALLER_ARGS[@]}" \
  desktop.py

mkdir -p "$STAGED_PACKAGE_DIR"
ditto --norsrc --noextattr \
  "$BUILD_ROOT/dist/${APP_NAME}.app" \
  "$STAGED_PACKAGE_DIR/${APP_NAME}.app"
cp "$PDF_GUIDE" "$STAGED_PACKAGE_DIR/Telegram名片工具-使用文档.pdf"

if find "$STAGED_PACKAGE_DIR/${APP_NAME}.app" -name '._*' -print -quit | grep -q .; then
  echo "Error: AppleDouble metadata files found in app bundle" >&2
  exit 1
fi
codesign --verify --deep --strict "$STAGED_PACKAGE_DIR/${APP_NAME}.app"
ditto -c -k --norsrc --noextattr --keepParent "$STAGED_PACKAGE_DIR" "$STAGED_ZIP"

mkdir -p "$SCRIPT_DIR/dist"
STAMP="$(date +%Y%m%d-%H%M%S)"
for OLD_PATH in "$APP_OUTPUT" "$PACKAGE_DIR" "$ZIP_PATH"; do
  if [[ -e "$OLD_PATH" ]]; then
    mv "$OLD_PATH" "/Users/miro/.Trash/$(basename "$OLD_PATH")-$STAMP"
  fi
done

ditto --norsrc --noextattr "$STAGED_PACKAGE_DIR/${APP_NAME}.app" "$APP_OUTPUT"
ditto --norsrc --noextattr "$STAGED_PACKAGE_DIR" "$PACKAGE_DIR"
cp "$STAGED_ZIP" "$ZIP_PATH"
codesign --verify --deep --strict "$APP_OUTPUT"
codesign --verify --deep --strict "$PACKAGE_DIR/${APP_NAME}.app"

echo "Built: $ZIP_PATH"
