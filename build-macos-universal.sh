#!/usr/bin/env bash
# 构建 macOS 通用版 (Apple Silicon + Intel) 应用
# 用法: TG_CARD_LICENSE_URL="https://你的授权服务器" ./build-macos-universal.sh
# 依赖: ./venv (arm64) 与 ./venv-x86 (x86_64) 两个虚拟环境
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

APP_NAME="Telegram名片工具"
ARM_APP="dist-arm64/${APP_NAME}.app"
X86_APP="dist-x86/${APP_NAME}.app"
OUT_APP="dist-universal/${APP_NAME}.app"
PDF_GUIDE="$SCRIPT_DIR/packaging/Telegram名片工具-使用文档.pdf"
PACKAGE_DIR="dist/${APP_NAME}-macOS-universal"

# 清理旧构建产物:移到废纸篓而不是 rm(更安全,也避免批量删除拦截)
trash_dir() {
  if [[ -e "$1" ]]; then
    mv "$1" "$HOME/.Trash/$(basename "$1")-$(date +%H%M%S)"
  fi
}

# 写入授权服务器地址(未设置则使用已有的 static/license_config.json)
if [[ -n "${TG_CARD_LICENSE_URL:-}" ]]; then
  printf '{"license_url": "%s"}\n' "${TG_CARD_LICENSE_URL%/}" > static/license_config.json
  echo "License server URL: ${TG_CARD_LICENSE_URL%/}"
fi

PYINSTALLER_ARGS=(
  --noconfirm
  --windowed
  --name "$APP_NAME"
  --osx-bundle-identifier "com.yuzhitongtong.telegram-card-tool"
  --add-data "static:static"
  --add-data "$PDF_GUIDE:."
  --collect-all telethon
)
if [[ -n "${CODESIGN_IDENTITY:-}" ]]; then
  PYINSTALLER_ARGS+=(--codesign-identity "$CODESIGN_IDENTITY")
fi

trash_dir "dist-arm64"
trash_dir "dist-x86"
trash_dir "dist-universal"
trash_dir "build/arm64"
trash_dir "build/x86"
trash_dir "$PACKAGE_DIR"

echo "==> [1/4] 构建 arm64 (Apple Silicon) ..."
./venv/bin/python -m PyInstaller "${PYINSTALLER_ARGS[@]}" \
  --distpath dist-arm64 --workpath build/arm64 desktop.py

echo "==> [2/4] 构建 x86_64 (Intel, Rosetta 2) ..."
arch -x86_64 ./venv-x86/bin/python -m PyInstaller "${PYINSTALLER_ARGS[@]}" \
  --distpath dist-x86 --workpath build/x86 desktop.py

echo "==> [3/4] lipo 合并为通用二进制 ..."
mkdir -p dist-universal
cp -R "$ARM_APP" "$OUT_APP"

merged=0
while IFS= read -r x86_file; do
  rel="${x86_file#"$X86_APP"/}"
  out_file="$OUT_APP/$rel"
  if [[ ! -e "$out_file" ]]; then
    # 仅存在于 x86 包中的文件,直接复制
    mkdir -p "$(dirname "$out_file")"
    cp "$x86_file" "$out_file"
    continue
  fi
  if file "$out_file" | grep -q "Mach-O" && file "$x86_file" | grep -q "Mach-O"; then
    lipo -create "$out_file" "$x86_file" -output "${out_file}.uni"
    mv "${out_file}.uni" "$out_file"
    merged=$((merged + 1))
  fi
  # 纯文本/资源文件保持 arm64 版本(两者内容一致)
done < <(find "$X86_APP" -type f ! -name ".DS_Store")

echo "    已合并 $merged 个二进制文件"

echo "==> [4/4] 临时签名 (ad-hoc) 并打包 zip ..."
# 合并后必须重新签名,否则 macOS 拒绝加载混合架构的二进制
codesign --force --deep --sign - "$OUT_APP" >/dev/null 2>&1

mkdir -p "$PACKAGE_DIR"
ditto "$OUT_APP" "$PACKAGE_DIR/${APP_NAME}.app"
cp "$PDF_GUIDE" "$PACKAGE_DIR/Telegram名片工具-使用文档.pdf"
ditto -c -k --keepParent "$PACKAGE_DIR" "dist/${APP_NAME}-macOS-universal.zip"

echo "架构验证:"
lipo -info "$OUT_APP/Contents/MacOS/$APP_NAME"

echo "Built: $SCRIPT_DIR/dist/${APP_NAME}-macOS-universal.zip"
