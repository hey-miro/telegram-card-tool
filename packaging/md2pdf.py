#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把使用文档 Markdown 转成带排版的 HTML，供 Chrome headless 打印为 PDF。"""
import markdown
from pathlib import Path

BASE = Path(__file__).resolve().parent
SRC = BASE / "Telegram名片工具-使用文档.md"
OUT = BASE / "Telegram名片工具-使用文档.html"

md_text = SRC.read_text(encoding="utf-8")

body = markdown.markdown(
    md_text,
    extensions=["tables", "fenced_code", "nl2br"],
)

CSS = """
@page { size: A4; margin: 18mm 16mm; }
@font-face {
  font-family: "CardToolCJK";
  src: local("Arial Unicode MS"), url("file:///Library/Fonts/Arial Unicode.ttf") format("truetype");
  font-weight: normal;
  font-style: normal;
}
* { box-sizing: border-box; }
body {
  font-family: "CardToolCJK", "PingFang SC", "Hiragino Sans GB", "Microsoft YaHei", sans-serif;
  font-size: 12.5px; line-height: 1.68; color: #24292f; margin: 0;
}
h1 {
  font-size: 26px; color: #0f5fbf; text-align: center;
  margin: 0 0 4px 0; letter-spacing: 1px;
}
h2 {
  font-size: 17px; color: #0f5fbf; border-left: 5px solid #0f5fbf;
  background: #eef4fd; padding: 7px 12px; margin: 22px 0 10px 0;
  border-radius: 4px; page-break-after: avoid;
}
h3 { font-size: 14.5px; color: #1a1a1a; margin: 18px 0 8px 0; page-break-after: avoid; }
p { margin: 7px 0; }
ul, ol { margin: 6px 0 6px 0; padding-left: 22px; }
li { margin: 3px 0; }
blockquote {
  margin: 10px 0; padding: 8px 14px; background: #fff8e6;
  border-left: 4px solid #f0a020; color: #5c4a1e; border-radius: 0 4px 4px 0;
}
blockquote p { margin: 3px 0; }
code {
  font-family: "CardToolCJK", "SF Mono", Menlo, Consolas, monospace; font-size: 11px;
  background: #f2f2f2; padding: 1px 5px; border-radius: 3px; color: #c7254e;
}
pre { background: #f6f8fa; padding: 12px; border-radius: 6px; overflow-x: auto; }
pre code { background: none; color: #24292f; padding: 0; }
table {
  border-collapse: collapse; width: 100%; margin: 12px 0; font-size: 12px;
}
th, td { border: 1px solid #d0d7de; padding: 7px 10px; text-align: left; }
th { background: #eef4fd; color: #0f5fbf; font-weight: 600; }
tr:nth-child(even) td { background: #fafbfc; }
hr { border: none; border-top: 1px solid #e1e4e8; margin: 22px 0; }
strong { color: #0f5fbf; }
"""

html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>Telegram 名片工具 · 使用文档</title>
<style>{CSS}</style>
</head>
<body>
{body}
</body>
</html>
"""

OUT.write_text(html, encoding="utf-8")
print(f"HTML 已生成: {OUT}")
