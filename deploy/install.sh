#!/bin/bash
# ==============================================================================
# Telegram 名片工具 - 授权服务器一键部署脚本
# ==============================================================================
# 用法: 在云服务器终端中执行 bash install.sh
# 支持系统: Alibaba Cloud Linux 3 / CentOS 8+ / Ubuntu 20.04+ / Debian 11+
# ==============================================================================
set -euo pipefail

# ---------- 配置区(可自定义) ----------
INSTALL_DIR="/opt/tg-license"
SERVICE_NAME="tg-license"
LISTEN_PORT="${LICENSE_PORT:-9000}"

# 颜色输出
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'

info()  { echo -e "${GREEN}[INFO]${NC} $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC} $*"; }
error() { echo -e "${RED}[ERROR]${NC} $*"; exit 1; }

# ---------- 前置检查 ----------
[[ $EUID -eq 0 ]] || error "请用 root 用户执行: sudo bash install.sh"

info "检查操作系统..."
if [[ -f /etc/os-release ]]; then
    . /etc/os-release
    info "系统: $PRETTY_NAME"
else
    warn "无法识别操作系统类型,继续尝试..."
fi

# ---------- 安装 Python ----------
info "检查 Python 3..."
PYTHON_BIN=""
for cmd in python3 python3.11 python3.10 python3.12; do
    if command -v "$cmd" &>/dev/null; then
        ver=$("$cmd" -c 'import sys; print(sys.version_info[:2])' 2>/dev/null || echo "(0,0)")
        ver_str=$(echo "$ver" | tr -d '() ')
        major=$(echo "$ver_str" | cut -d, -f1)
        minor=$(echo "$ver_str" | cut -d, -f2)
        if [[ "$major" -ge 3 && "$minor" -ge 10 ]]; then
            PYTHON_BIN="$cmd"
            info "找到 $cmd (Python ${major}.${minor})"
            break
        fi
    fi
done

if [[ -z "$PYTHON_BIN" ]]; then
    info "系统未安装 Python 3.10+,正在安装..."
    if command -v dnf &>/dev/null; then
        dnf install -y python3 python3-pip
    elif command -v yum &>/dev/null; then
        yum install -y python3 python3-pip
    elif command -v apt &>/dev/null; then
        apt update -y && apt install -y python3 python3-pip python3-venv
    else
        error "无法自动安装 Python,请手动安装 Python 3.10+ 后重试"
    fi
    PYTHON_BIN="python3"
fi

"$PYTHON_BIN" -c 'import sys; assert sys.version_info >= (3,10)' 2>/dev/null || error "Python 版本过低,需要 3.10+"

# ---------- 安装 venv 支持 ----------
info "确保 venv 模块可用..."
if ! "$PYTHON_BIN" -c 'import venv' 2>/dev/null; then
    if command -v dnf &>/dev/null; then
        dnf install -y python3-venv 2>/dev/null || dnf install -y python3-devel
    elif command -v apt &>/dev/null; then
        apt install -y python3-venv
    fi
fi

# ---------- 创建目录 ----------
info "创建安装目录 $INSTALL_DIR ..."
mkdir -p "$INSTALL_DIR"
mkdir -p "$INSTALL_DIR/data"
cd "$INSTALL_DIR"

# ---------- 写入 license_server.py ----------
info "写入 license_server.py ..."
cat << 'PYEOF' > "$INSTALL_DIR/license_server.py"
"""
Telegram 名片工具授权服务器
===========================
独立部署的授权服务,负责生成/管理授权码、绑定设备、校验续期。
不接收 Telegram 手机号、验证码、API Hash 或 Session 等任何业务数据,
仅接收不可逆设备哈希。

环境变量:
    LICENSE_JWT_SECRET    JWT 签名密钥(必填,生产请设置强随机字符串)
    LICENSE_ADMIN_KEY     管理接口 API Key(必填)
    LICENSE_DB_PATH       SQLite 数据库路径,默认 ./data/license.db
    LICENSE_HOST          监听地址,默认 0.0.0.0
    LICENSE_PORT          监听端口,默认 9000
"""

import hashlib
import os
import secrets
import string
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Optional

import jwt
from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field


def _db_path() -> str:
    path = os.environ.get("LICENSE_DB_PATH", "./data/license.db")
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    return path


DB_PATH = _db_path()
JWT_SECRET = os.environ.get("LICENSE_JWT_SECRET", "")
ADMIN_KEY = os.environ.get("LICENSE_ADMIN_KEY", "")
ALGORITHM = "HS256"
ACCESS_TOKEN_DAYS = 30  # 每次激活/续期后签发的 token 有效期


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def init_db():
    import sqlite3

    with sqlite3.connect(DB_PATH) as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS licenses (
                code TEXT PRIMARY KEY,
                max_devices INTEGER NOT NULL DEFAULT 1,
                expires_at TEXT NOT NULL,
                is_active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                notes TEXT
            );

            CREATE TABLE IF NOT EXISTS license_devices (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                license_code TEXT NOT NULL,
                device_hash TEXT NOT NULL,
                activated_at TEXT NOT NULL,
                last_seen TEXT NOT NULL,
                UNIQUE(license_code, device_hash),
                FOREIGN KEY (license_code) REFERENCES licenses(code)
            );

            CREATE INDEX IF NOT EXISTS idx_license_devices_code
                ON license_devices(license_code);
            """
        )


@asynccontextmanager
async def lifespan(app: FastAPI):
    if not JWT_SECRET or len(JWT_SECRET) < 16:
        raise RuntimeError("请设置环境变量 LICENSE_JWT_SECRET(至少 16 位)")
    if not ADMIN_KEY or len(ADMIN_KEY) < 8:
        raise RuntimeError("请设置环境变量 LICENSE_ADMIN_KEY(至少 8 位)")
    init_db()
    yield


app = FastAPI(title="Telegram 名片工具授权服务", lifespan=lifespan)


# ---------------------------------------------------------------------------
# 数据模型
# ---------------------------------------------------------------------------
class LicenseCreate(BaseModel):
    max_devices: int = Field(default=1, ge=1, le=100)
    expires_at: str = Field(..., min_length=10, max_length=32)
    notes: str = ""


class LicenseRenew(BaseModel):
    expires_at: Optional[str] = None
    extend_days: Optional[int] = Field(default=None, ge=1, le=3650)


class ActivateIn(BaseModel):
    code: str = Field(..., min_length=1, max_length=64)
    device_hash: str = Field(..., min_length=16, max_length=128)


class VerifyIn(BaseModel):
    token: str = Field(..., min_length=1)
    device_hash: str = Field(..., min_length=16, max_length=128)


# ---------------------------------------------------------------------------
# 管理接口依赖
# ---------------------------------------------------------------------------
def require_admin(x_admin_key: str = Header(default="")):
    if not x_admin_key or x_admin_key != ADMIN_KEY:
        raise HTTPException(status_code=401, detail="管理员密钥无效")
    return x_admin_key


# ---------------------------------------------------------------------------
# 授权码工具
# ---------------------------------------------------------------------------
def generate_license_code() -> str:
    """生成 TGCARD-XXXX-XXXX-XXXX 格式授权码."""
    chars = string.ascii_uppercase + string.digits
    parts = ["".join(secrets.choice(chars) for _ in range(4)) for _ in range(3)]
    return "TGCARD-" + "-".join(parts)


def normalize_code(code: str) -> str:
    return code.strip().upper()


# ---------------------------------------------------------------------------
# 数据库辅助
# ---------------------------------------------------------------------------
def _conn():
    import sqlite3

    return sqlite3.connect(DB_PATH)


def _row_to_license(row) -> dict:
    return {
        "code": row[0],
        "max_devices": row[1],
        "expires_at": row[2],
        "is_active": bool(row[3]),
        "created_at": row[4],
        "notes": row[5],
    }


def _parse_expires(value: str) -> datetime:
    """支持 ISO 8601 或普通日期字符串."""
    value = value.strip()
    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(value, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    raise ValueError(f"无法解析到期时间: {value}")


# ---------------------------------------------------------------------------
# JWT
# ---------------------------------------------------------------------------
def sign_token(license_code: str, device_hash: str, expires: datetime) -> str:
    now = _utcnow()
    # token 有效期不超过授权码本身的到期时间,也不超过 ACCESS_TOKEN_DAYS
    token_exp = min(expires, now + timedelta(days=ACCESS_TOKEN_DAYS))
    payload = {
        "iss": "tg-card-license",
        "sub": license_code,
        "device_hash": device_hash,
        "iat": now,
        "exp": token_exp,
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=ALGORITHM)


def verify_token(token: str, device_hash: str) -> dict:
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[ALGORITHM], issuer="tg-card-license")
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="授权已过期,请重新激活")
    except jwt.InvalidTokenError as exc:
        raise HTTPException(status_code=401, detail=f"授权凭证无效: {exc}")

    if payload.get("device_hash") != device_hash:
        raise HTTPException(status_code=401, detail="授权与当前设备不匹配")

    return payload


# ---------------------------------------------------------------------------
# 业务逻辑
# ---------------------------------------------------------------------------
def _get_license(code: str):
    with _conn() as conn:
        row = conn.execute(
            "SELECT code, max_devices, expires_at, is_active, created_at, notes FROM licenses WHERE code = ?",
            (code,),
        ).fetchone()
    if not row:
        return None
    return _row_to_license(row)


def _device_count(code: str) -> int:
    with _conn() as conn:
        row = conn.execute(
            "SELECT COUNT(*) FROM license_devices WHERE license_code = ?", (code,)
        ).fetchone()
    return row[0] if row else 0


def _bind_device(code: str, device_hash: str):
    now = _utcnow().isoformat()
    with _conn() as conn:
        conn.execute(
            """
            INSERT INTO license_devices(license_code, device_hash, activated_at, last_seen)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(license_code, device_hash) DO UPDATE SET
                last_seen = excluded.last_seen
            """,
            (code, device_hash, now, now),
        )


def _activate_or_verify(code: str, device_hash: str) -> dict:
    license_info = _get_license(code)
    if not license_info:
        raise HTTPException(status_code=404, detail="授权码不存在")
    if not license_info["is_active"]:
        raise HTTPException(status_code=403, detail="授权码已被禁用")

    expires = _parse_expires(license_info["expires_at"])
    if _utcnow() > expires:
        raise HTTPException(status_code=403, detail="授权已到期,请联系管理员续期")

    # 检查设备绑定数量
    bound_count = _device_count(code)
    with _conn() as conn:
        existing = conn.execute(
            "SELECT 1 FROM license_devices WHERE license_code = ? AND device_hash = ?",
            (code, device_hash),
        ).fetchone()

    if not existing and bound_count >= license_info["max_devices"]:
        raise HTTPException(
            status_code=403,
            detail=f"该授权码已绑定 {bound_count}/{license_info['max_devices']} 台设备,无法在新设备激活",
        )

    _bind_device(code, device_hash)
    token = sign_token(code, device_hash, expires)

    return {
        "ok": True,
        "license_code": code,
        "expires_at": expires.isoformat(),
        "token": token,
        "max_devices": license_info["max_devices"],
        "bound_devices": bound_count + (0 if existing else 1),
    }


# ---------------------------------------------------------------------------
# 公开 API
# ---------------------------------------------------------------------------
@app.post("/licenses/activate")
async def activate(body: ActivateIn):
    code = normalize_code(body.code)
    return _activate_or_verify(code, body.device_hash)


@app.post("/licenses/verify")
async def verify(body: VerifyIn):
    payload = verify_token(body.token, body.device_hash)
    code = payload["sub"]
    license_info = _get_license(code)
    if not license_info or not license_info["is_active"]:
        raise HTTPException(status_code=403, detail="授权码已失效")

    expires = _parse_expires(license_info["expires_at"])
    if _utcnow() > expires:
        raise HTTPException(status_code=403, detail="授权已到期,请联系管理员续期")

    # 刷新最后使用时间
    _bind_device(code, body.device_hash)

    return {
        "ok": True,
        "license_code": code,
        "expires_at": expires.isoformat(),
        "max_devices": license_info["max_devices"],
        "bound_devices": _device_count(code),
    }


# ---------------------------------------------------------------------------
# 管理 API
# ---------------------------------------------------------------------------
@app.post("/admin/licenses", dependencies=[Depends(require_admin)])
async def admin_create_license(body: LicenseCreate):
    code = generate_license_code()
    try:
        expires = _parse_expires(body.expires_at)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    with _conn() as conn:
        conn.execute(
            """
            INSERT INTO licenses(code, max_devices, expires_at, is_active, created_at, notes)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                code,
                body.max_devices,
                expires.isoformat(),
                1,
                _utcnow().isoformat(),
                body.notes,
            ),
        )

    return {"ok": True, "code": code, "expires_at": expires.isoformat()}


@app.get("/admin/licenses", dependencies=[Depends(require_admin)])
async def admin_list_licenses():
    with _conn() as conn:
        rows = conn.execute(
            "SELECT code, max_devices, expires_at, is_active, created_at, notes FROM licenses ORDER BY created_at DESC"
        ).fetchall()
    licenses = [_row_to_license(row) for row in rows]
    return {"licenses": licenses}


@app.post("/admin/licenses/{code}/renew", dependencies=[Depends(require_admin)])
async def admin_renew_license(code: str, body: LicenseRenew):
    code = normalize_code(code)
    license_info = _get_license(code)
    if not license_info:
        raise HTTPException(status_code=404, detail="授权码不存在")

    if body.extend_days:
        current = _parse_expires(license_info["expires_at"])
        if current < _utcnow():
            current = _utcnow()
        new_expires = current + timedelta(days=body.extend_days)
    elif body.expires_at:
        new_expires = _parse_expires(body.expires_at)
    else:
        raise HTTPException(status_code=400, detail="请提供 expires_at 或 extend_days")

    with _conn() as conn:
        conn.execute(
            "UPDATE licenses SET expires_at = ? WHERE code = ?",
            (new_expires.isoformat(), code),
        )

    return {"ok": True, "code": code, "expires_at": new_expires.isoformat()}


@app.post("/admin/licenses/{code}/toggle", dependencies=[Depends(require_admin)])
async def admin_toggle_license(code: str):
    code = normalize_code(code)
    license_info = _get_license(code)
    if not license_info:
        raise HTTPException(status_code=404, detail="授权码不存在")

    new_state = 0 if license_info["is_active"] else 1
    with _conn() as conn:
        conn.execute("UPDATE licenses SET is_active = ? WHERE code = ?", (new_state, code))

    return {"ok": True, "code": code, "is_active": bool(new_state)}


@app.get("/admin/licenses/{code}/devices", dependencies=[Depends(require_admin)])
async def admin_license_devices(code: str):
    code = normalize_code(code)
    with _conn() as conn:
        rows = conn.execute(
            "SELECT device_hash, activated_at, last_seen FROM license_devices WHERE license_code = ? ORDER BY last_seen DESC",
            (code,),
        ).fetchall()
    return {
        "code": code,
        "devices": [
            {
                "device_hash": row[0][:16] + "..." + row[0][-8:],  # 脱敏展示
                "activated_at": row[1],
                "last_seen": row[2],
            }
            for row in rows
        ],
    }


# ---------------------------------------------------------------------------
# 错误处理
# ---------------------------------------------------------------------------
@app.exception_handler(HTTPException)
async def http_exception_handler(request, exc):
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})


if __name__ == "__main__":
    import uvicorn

    host = os.environ.get("LICENSE_HOST", "0.0.0.0")
    port = int(os.environ.get("LICENSE_PORT", "9000"))
    uvicorn.run(app, host=host, port=port)
PYEOF

info "写入 license_admin.py ..."
cat << 'PYEOF' > "$INSTALL_DIR/license_admin.py"
"""
授权码管理 CLI
=============
用于管理员生成、查看、续期、禁用授权码。

用法示例:
    # 生成一个 30 天后到期、可绑定 1 台设备的授权码
    TG_CARD_LICENSE_URL=https://license.example.com \
    LICENSE_ADMIN_KEY=your-admin-key \
        python3 license_admin.py create --days 30

    # 生成可绑定 3 台设备的授权码
    TG_CARD_LICENSE_URL=... LICENSE_ADMIN_KEY=... \
        python3 license_admin.py create --days 90 --devices 3 --notes "客户A"

    # 列出所有授权码
    TG_CARD_LICENSE_URL=... LICENSE_ADMIN_KEY=... \
        python3 license_admin.py list
"""

import argparse
import os
import sys
from datetime import datetime, timedelta, timezone

import requests


def _base_url() -> str:
    url = os.environ.get("TG_CARD_LICENSE_URL", "").rstrip("/")
    if not url:
        print("错误: 请设置环境变量 TG_CARD_LICENSE_URL", file=sys.stderr)
        sys.exit(1)
    return url


def _admin_key() -> str:
    key = os.environ.get("LICENSE_ADMIN_KEY", "")
    if not key:
        print("错误: 请设置环境变量 LICENSE_ADMIN_KEY", file=sys.stderr)
        sys.exit(1)
    return key


def _req(method: str, path: str, json_body: dict | None = None) -> dict:
    url = f"{_base_url()}{path}"
    try:
        resp = requests.request(
            method,
            url,
            json=json_body,
            headers={"X-Admin-Key": _admin_key(), "Content-Type": "application/json"},
            timeout=(5, 15),
        )
    except requests.exceptions.RequestException as exc:
        print(f"请求失败: {exc}", file=sys.stderr)
        sys.exit(1)

    try:
        data = resp.json()
    except Exception:
        data = {"detail": resp.text or f"HTTP {resp.status_code}"}

    if not resp.ok:
        print(f"错误: {data.get('detail', resp.status_code)}", file=sys.stderr)
        sys.exit(1)

    return data


def cmd_create(args):
    expires = datetime.now(timezone.utc) + timedelta(days=args.days)
    body = {
        "max_devices": args.devices,
        "expires_at": expires.strftime("%Y-%m-%dT%H:%M:%S"),
        "notes": args.notes or "",
    }
    result = _req("POST", "/admin/licenses", body)
    print("授权码已生成")
    print(f"  授权码: {result['code']}")
    print(f"  到期时间: {result['expires_at']}")
    print(f"  可绑定设备数: {args.devices}")
    if args.notes:
        print(f"  备注: {args.notes}")


def cmd_list(_args):
    result = _req("GET", "/admin/licenses")
    licenses = result.get("licenses", [])
    if not licenses:
        print("暂无授权码")
        return

    print(f"{'授权码':<26} {'到期时间':<22} {'设备数':<8} {'状态':<6} {'备注'}")
    print("-" * 90)
    for lic in licenses:
        status = "启用" if lic["is_active"] else "禁用"
        print(
            f"{lic['code']:<26} {lic['expires_at']:<22} {lic['max_devices']:<8} {status:<6} {lic.get('notes', '')}"
        )


def cmd_renew(args):
    body = {"extend_days": args.days}
    result = _req("POST", f"/admin/licenses/{args.code}/renew", body)
    print(f"授权码 {result['code']} 已续期")
    print(f"  新到期时间: {result['expires_at']}")


def cmd_toggle(args):
    result = _req("POST", f"/admin/licenses/{args.code}/toggle")
    status = "启用" if result["is_active"] else "禁用"
    print(f"授权码 {result['code']} 已{status}")


def cmd_devices(args):
    result = _req("GET", f"/admin/licenses/{args.code}/devices")
    devices = result.get("devices", [])
    if not devices:
        print("该授权码尚未绑定设备")
        return
    print(f"授权码 {result['code']} 已绑定设备:")
    for dev in devices:
        print(f"  设备哈希: {dev['device_hash']}")
        print(f"  激活时间: {dev['activated_at']}  最后使用: {dev['last_seen']}")
        print()


def main():
    parser = argparse.ArgumentParser(description="授权码管理工具")
    sub = parser.add_subparsers(dest="command", required=True)

    p_create = sub.add_parser("create", help="创建授权码")
    p_create.add_argument("--days", type=int, default=30, help="有效期天数")
    p_create.add_argument("--devices", type=int, default=1, help="最大可绑定设备数")
    p_create.add_argument("--notes", default="", help="备注")
    p_create.set_defaults(func=cmd_create)

    p_list = sub.add_parser("list", help="列出授权码")
    p_list.set_defaults(func=cmd_list)

    p_renew = sub.add_parser("renew", help="续期授权码")
    p_renew.add_argument("code", help="授权码")
    p_renew.add_argument("--days", type=int, default=30, help="续期天数")
    p_renew.set_defaults(func=cmd_renew)

    p_toggle = sub.add_parser("toggle", help="启用/禁用授权码")
    p_toggle.add_argument("code", help="授权码")
    p_toggle.set_defaults(func=cmd_toggle)

    p_devices = sub.add_parser("devices", help="查看授权码绑定设备")
    p_devices.add_argument("code", help="授权码")
    p_devices.set_defaults(func=cmd_devices)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
PYEOF

# ---------- 创建虚拟环境 ----------
info "创建 Python 虚拟环境..."
if [[ ! -d "$INSTALL_DIR/venv" ]]; then
    "$PYTHON_BIN" -m venv "$INSTALL_DIR/venv"
fi
VENV_PY="$INSTALL_DIR/venv/bin/python"

info "安装依赖..."
"$VENV_PY" -m pip install --quiet --upgrade pip
"$VENV_PY" -m pip install --quiet \
    "fastapi>=0.110" \
    "uvicorn[standard]>=0.27" \
    "pyjwt>=2.8" \
    "pydantic>=2.5" \
    "python-multipart>=0.0.6" \
    "requests>=2.31"

info "依赖安装完成"
"$VENV_PY" -c "import fastapi, uvicorn, jwt, pydantic; print('依赖验证通过')"

# ---------- 生成密钥 ----------
ENV_FILE="$INSTALL_DIR/.env"
if [[ -f "$ENV_FILE" ]]; then
    info "发现已有 .env 配置,保留原有密钥"
    source "$ENV_FILE"
else
    info "生成强随机密钥..."
    JWT_SECRET=$("$VENV_PY" -c "import secrets; print(secrets.token_urlsafe(48))")
    ADMIN_KEY=$("$VENV_PY" -c "import secrets; print(secrets.token_urlsafe(24))")

    cat > "$ENV_FILE" << EOF
LICENSE_JWT_SECRET=$JWT_SECRET
LICENSE_ADMIN_KEY=$ADMIN_KEY
LICENSE_DB_PATH=$INSTALL_DIR/data/license.db
LICENSE_HOST=0.0.0.0
LICENSE_PORT=$LISTEN_PORT
EOF
    chmod 600 "$ENV_FILE"
    # 若以 sudo 运行,将 .env owner 改回实际执行命令的用户,否则后续普通用户无法 source
    if [[ -n "${SUDO_USER:-}" ]]; then
        chown "$SUDO_USER:$SUDO_USER" "$ENV_FILE" 2>/dev/null || true
    fi
    info "密钥已生成并保存到 $ENV_FILE"
fi

# ---------- 创建 systemd 服务 ----------
info "配置 systemd 服务..."
cat > "/etc/systemd/system/${SERVICE_NAME}.service" << EOF
[Unit]
Description=Telegram Card Tool - License Server
After=network.target

[Service]
Type=simple
WorkingDirectory=$INSTALL_DIR
EnvironmentFile=$ENV_FILE
ExecStart=$VENV_PY -m uvicorn license_server:app --host 0.0.0.0 --port $LISTEN_PORT
Restart=always
RestartSec=5
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable "$SERVICE_NAME"
systemctl restart "$SERVICE_NAME"

info "等待服务启动..."
sleep 3

# ---------- 检查服务状态 ----------
if systemctl is-active --quiet "$SERVICE_NAME"; then
    info "服务运行中 ✓"
else
    warn "服务可能未正常启动,查看日志:"
    journalctl -u "$SERVICE_NAME" --no-pager -n 20
    error "服务启动失败"
fi

# ---------- 防火墙 ----------
info "配置防火墙..."
if command -v firewall-cmd &>/dev/null; then
    firewall-cmd --permanent --add-port="$LISTEN_PORT/tcp" 2>/dev/null && firewall-cmd --reload 2>/dev/null
    info "firewalld 已放行端口 $LISTEN_PORT"
elif command -v ufw &>/dev/null; then
    ufw allow "$LISTEN_PORT"/tcp 2>/dev/null
    info "ufw 已放行端口 $LISTEN_PORT"
else
    warn "未检测到防火墙工具,请手动放行端口 $LISTEN_PORT"
fi

# ---------- 获取公网 IP ----------
PUBLIC_IP=$(curl -s --max-time 5 ifconfig.me 2>/dev/null || curl -s --max-time 5 icanhazip.com 2>/dev/null || echo "未知")
LOCAL_TEST=$(curl -s --max-time 5 "http://127.0.0.1:$LISTEN_PORT/docs" -o /dev/null -w "%{http_code}" 2>/dev/null || echo "000")

# ---------- 打印结果 ----------
echo ""
echo "================================================================"
echo -e "${GREEN}  授权服务器部署完成!${NC}"
echo "================================================================"
echo ""
echo "  服务状态:   $(systemctl is-active $SERVICE_NAME)"
echo "  监听端口:   $LISTEN_PORT"
echo "  公网 IP:    $PUBLIC_IP"
echo "  本地测试:   HTTP $LOCAL_TEST"
echo ""
echo "  -------- 重要凭据(请妥善保管) --------"
echo ""
source "$ENV_FILE"
echo "  授权服务地址:  http://$PUBLIC_IP:$LISTEN_PORT"
echo "  管理员密钥:    $LICENSE_ADMIN_KEY"
echo "  JWT 密钥:      ${LICENSE_JWT_SECRET:0:12}...(已隐藏)"
echo ""
echo "  -------- 常用管理命令 --------"
echo ""
echo "  查看服务状态:  systemctl status $SERVICE_NAME"
echo "  重启服务:      systemctl restart $SERVICE_NAME"
echo "  查看日志:      journalctl -u $SERVICE_NAME -f"
echo "  管理授权码:    cd $INSTALL_DIR && source .env && \\"
echo "                  TG_CARD_LICENSE_URL=http://127.0.0.1:$LISTEN_PORT \\"
echo "                  LICENSE_ADMIN_KEY=\$LICENSE_ADMIN_KEY \\"
echo "                  ./venv/bin/python license_admin.py create --days 30 --notes \"客户A\""
echo ""
echo "  -------- 下一步 --------"
echo ""
echo "  1. 阿里云安全组: 在 ECS 控制台 → 安全组 → 添加入方向规则"
echo "     协议 TCP  端口 $LISTEN_PORT  源 0.0.0.0/0"
echo ""
echo "  2. 把授权地址告诉开发者(或自行打包桌面应用):"
echo "     http://$PUBLIC_IP:$LISTEN_PORT"
echo ""
echo "  3. 生成授权码给客户:"
echo "     cd $INSTALL_DIR && source .env"
echo "     TG_CARD_LICENSE_URL=http://127.0.0.1:$LISTEN_PORT \\"
echo "     LICENSE_ADMIN_KEY=\$LICENSE_ADMIN_KEY \\"
echo "     ./venv/bin/python license_admin.py create --days 365 --devices 1 --notes \"客户名称\""
echo ""
echo "================================================================"
