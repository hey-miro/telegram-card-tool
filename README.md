# Telegram 名片分享与群组工具

本地运行的 Web 应用，用 Telethon（Telegram MTProto 用户接口）实现账号登录、会话/群组管理，以及向明确选中的目标批量分享真实手机号名片。

仅操作你自有或已获授权的账号、会话与群组。登录验证码、两步验证密码、api_hash 不会写入日志，日志中的手机号会自动脱敏。

## 功能

- API 设置：填写 `api_id` / `api_hash`
- 账号管理：手机号登录（验证码 + 可选两步验证）、导入已有 Session、验证、移除
- 会话/群组管理：按账号拉取频道、群组、私聊，支持筛选和名称搜索
- 名片分享：解析 `手机号` 或 `手机号;姓名;姓氏`，发送真实名片到选中的目标，支持轮数、发送间隔、回退姓名、获取缺失姓名、跳过未关联、允许空姓名、停止任务、实时日志

## 环境

- Python 3.12
- 项目内已包含 `venv`（Telethon 1.44、FastAPI、Uvicorn）

首次运行（如果没有 `venv`）：

```bash
python3 -m venv venv
./venv/bin/pip install -r requirements.txt
```

## 获取 api_id / api_hash

1. 打开 <https://my.telegram.org>
2. 用手机号登录，进入 **API development tools**
3. 创建一个应用，得到 `api_id`（数字）和 `api_hash`（字符串）
4. 在本工具右上角「API 设置」中填写

## 启动

```bash
./venv/bin/python -m uvicorn app:app --host 127.0.0.1 --port 8765
```

浏览器打开 <http://127.0.0.1:8765>。

## 分发给用户（双击即用）

打包后的程序会自动选取未被占用的本机端口并打开浏览器。用户不需要安装 Python；API 设置、Telegram Session 和数据库只保存在该用户自己的电脑。

### macOS

在构建机执行一次：

```bash
./venv/bin/pip install -r requirements.txt
chmod +x build-macos.sh
./build-macos.sh
```

将 `dist/Telegram名片工具.app` 发给用户即可。用户双击应用后会自动在浏览器打开工具，无需安装 Python。

- 每台用户电脑的 API 设置和 Telegram Session 都只保存在本机，不会随应用一起分发。
- macOS 可能因未签名而提示安全警告；用户可在“系统设置 → 隐私与安全性”中确认打开。

### Windows（64 位）

在一台 Windows 电脑上解压源代码并双击 `build-windows.bat`。首次构建会自动创建 Python 虚拟环境和安装打包依赖；需要预先安装 Python 3.12。

完成后将整个 `dist\TelegramCardTool` 文件夹压缩成 ZIP 发给 Windows 用户。用户解压后双击 `TelegramCardTool.exe`，即可自动打开浏览器使用。

> macOS `.app` 和 Windows `.exe` 必须分别在对应系统上构建，不能互用。Windows Defender 也可能对未签名程序显示提示；企业或大规模分发建议后续给两个安装包配置代码签名。

## 使用流程

1. 配置 API ID / API Hash
2. 在「账号管理」登录账号：输入手机号 → 点「发送验证码」→ 输入收到的验证码 → 如需两步验证再输入密码
3. 在「名片分享」选择账号 → 拉取会话 → 筛选并勾选目标会话/群组
4. 粘贴号码，例如：

```text
+8613800138000
13800138001;张三;张
```

5. 设置轮数、发送间隔等参数，点「批量开始分享」

## 数据与安全

- 账号 Session 和配置保存在本机 `data/tool.db`，请勿外传
- 登录密码/验证码只在使用时传递，不落库、不写日志
- 日志手机号脱敏，例如 `1380****8000`
- 未关联号码可按「跳过未关联号码」规则跳过

## 已实现 / 待扩展

已实现：API 配置、账号登录/导入/验证/移除、会话拉取与筛选、名片批量分享（轮数、间隔、姓名回退、跳过未关联、停止、脱敏日志）。

待扩展：多账号轮换分享、群组管理的批量「移除联系人/清空对话」、客户跟进视图。
