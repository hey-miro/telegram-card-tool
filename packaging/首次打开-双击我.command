#!/bin/bash
# Telegram 名片工具 - macOS 首次打开助手
# 作用:清除 macOS 对网络下载文件的隔离标记(免开发者证书场景),
#       双击本文件即可正常启动软件,无需右键操作。
cd "$(dirname "$0")"

APP="Telegram名片工具.app"

if [[ ! -d "$APP" ]]; then
  # 兼容解压后多一层文件夹的情况
  FOUND=$(find . -maxdepth 3 -name "Telegram名片工具.app" -type d | head -1)
  if [[ -n "$FOUND" ]]; then
    APP="$FOUND"
  else
    osascript -e 'display notification "没有找到 Telegram名片工具.app,请确认它和本脚本在同一文件夹内" with title "首次打开失败"'
    exit 1
  fi
fi

xattr -cr "$APP" 2>/dev/null

open "$APP"
osascript -e "display notification \"已解锁并启动,以后可以直接双击软件图标打开\" with title \"Telegram 名片工具\""
