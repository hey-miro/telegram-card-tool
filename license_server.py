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
