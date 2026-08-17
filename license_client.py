"""
Telegram 名片工具本地授权客户端
==============================
负责:生成不可逆设备哈希、本地授权凭证读写、与授权服务器交互。
不传输 Telegram 手机号、验证码、API Hash 或 Session。

配置(优先从环境变量读取,否则从本地配置文件读取):
    TG_CARD_LICENSE_URL      授权服务器地址,例如 https://license.example.com
    TG_CARD_LICENSE_CODE     预置授权码(可选,通常由用户输入)
"""

import hashlib
import json
import os
import platform
import re
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import jwt
import requests


# ---------------------------------------------------------------------------
# 路径与配置
# ---------------------------------------------------------------------------
def _user_data_dir() -> Path:
    system = platform.system()
    if system == "Windows":
        base = Path(os.environ.get("APPDATA", Path.home() / "AppData/Roaming"))
        path = base / "TgCardTool"
    elif system == "Darwin":
        path = Path.home() / "Library/Application Support/TgCardTool"
    else:
        path = Path.home() / ".local/share/TgCardTool"
    path.mkdir(parents=True, exist_ok=True)
    return path


LICENSE_FILE = _user_data_dir() / "license.json"


def _bundled_license_url() -> str:
    """读取打包时内置的授权服务器地址(在 static/license_config.json)."""
    import sys

    candidates = []
    bundled_dir = getattr(sys, "_MEIPASS", None)
    if bundled_dir:
        candidates.append(Path(bundled_dir) / "static" / "license_config.json")
    candidates.append(Path(__file__).resolve().parent / "static" / "license_config.json")

    for cfg in candidates:
        try:
            if cfg.exists():
                data = json.loads(cfg.read_text(encoding="utf-8"))
                url = str(data.get("license_url", "")).rstrip("/")
                if url:
                    return url
        except Exception:
            continue
    return ""


def _default_license_url() -> str:
    # 优先级:环境变量 > 打包内置配置文件(static/license_config.json)
    url = os.environ.get("TG_CARD_LICENSE_URL", "").rstrip("/")
    if url:
        return url
    return _bundled_license_url()


# ---------------------------------------------------------------------------
# 设备哈希(不可逆)
# ---------------------------------------------------------------------------
def _run_cmd(args: list[str], fallback: str = "") -> str:
    try:
        result = subprocess.run(
            args,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        return result.stdout.strip() or fallback
    except Exception:
        return fallback


def _get_machine_uuid() -> str:
    system = platform.system()
    uuid = ""
    if system == "Darwin":
        out = _run_cmd(["ioreg", "-rd1", "-c", "IOPlatformExpertDevice"])
        match = re.search(r'"IOPlatformUUID"\s*=\s*"([^"]+)"', out)
        if match:
            uuid = match.group(1)
    elif system == "Windows":
        out = _run_cmd(["wmic", "csproduct", "get", "uuid"])
        lines = [line.strip() for line in out.splitlines() if line.strip()]
        if len(lines) >= 2 and lines[0].upper() == "UUID":
            uuid = lines[1]
    else:
        for p in ("/etc/machine-id", "/var/lib/dbus/machine-id"):
            try:
                uuid = Path(p).read_text(encoding="utf-8").strip()
                if uuid:
                    break
            except Exception:
                continue

    # 若无法获取硬件 UUID,则退回到系统信息组合(稳定性略差,但跨平台)
    if not uuid:
        uuid = f"{platform.node()}-{platform.machine()}-{platform.processor()}-{platform.system()}"

    return uuid


def get_device_hash() -> str:
    """生成当前设备的不可逆哈希."""
    raw = "|".join(
        [
            _get_machine_uuid(),
            platform.system(),
            platform.node(),
            platform.machine(),
            platform.processor(),
        ]
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# 本地凭证
# ---------------------------------------------------------------------------
def load_license() -> Optional[dict]:
    if not LICENSE_FILE.exists():
        return None
    try:
        data = json.loads(LICENSE_FILE.read_text(encoding="utf-8"))
        if isinstance(data, dict) and data.get("token") and data.get("device_hash"):
            return data
    except Exception:
        pass
    return None


def save_license(token: str, device_hash: str, license_code: str, expires_at: str):
    LICENSE_FILE.write_text(
        json.dumps(
            {
                "token": token,
                "device_hash": device_hash,
                "license_code": license_code,
                "expires_at": expires_at,
                "saved_at": datetime.now(timezone.utc).isoformat(),
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


def clear_license():
    if LICENSE_FILE.exists():
        LICENSE_FILE.unlink()


# ---------------------------------------------------------------------------
# 远程交互
# ---------------------------------------------------------------------------
def _request(method: str, path: str, json_body: Optional[dict] = None) -> dict:
    base = _default_license_url()
    if not base:
        raise RuntimeError("未配置授权服务器地址(TG_CARD_LICENSE_URL)")

    url = f"{base}{path}"
    try:
        response = requests.request(
            method,
            url,
            json=json_body,
            timeout=(5, 15),
            headers={"Content-Type": "application/json"},
        )
    except requests.exceptions.ConnectionError as exc:
        raise RuntimeError(f"无法连接授权服务器: {exc}")
    except requests.exceptions.Timeout:
        raise RuntimeError("连接授权服务器超时")

    try:
        data = response.json()
    except Exception:
        data = {"detail": response.text or f"HTTP {response.status_code}"}

    if not response.ok:
        detail = data.get("detail", f"请求失败({response.status_code})")
        raise RuntimeError(detail)

    return data


def activate_license(code: str) -> dict:
    """用授权码在线激活当前设备."""
    device_hash = get_device_hash()
    result = _request(
        "POST",
        "/licenses/activate",
        {"code": code.strip().upper(), "device_hash": device_hash},
    )
    save_license(
        token=result["token"],
        device_hash=device_hash,
        license_code=result["license_code"],
        expires_at=result["expires_at"],
    )
    return result


def verify_license_remote(token: str, device_hash: str) -> dict:
    """携带 token 到授权服务器校验并刷新最后使用时间."""
    return _request(
        "POST",
        "/licenses/verify",
        {"token": token, "device_hash": device_hash},
    )


# ---------------------------------------------------------------------------
# 本地校验
# ---------------------------------------------------------------------------
def _verify_token_locally(token: str, device_hash: str, secret: str) -> dict:
    try:
        payload = jwt.decode(
            token,
            secret,
            algorithms=["HS256"],
            issuer="tg-card-license",
        )
    except jwt.ExpiredSignatureError:
        raise RuntimeError("授权已到期")
    except jwt.InvalidTokenError as exc:
        raise RuntimeError(f"授权凭证无效: {exc}")

    if payload.get("device_hash") != device_hash:
        raise RuntimeError("授权与当前设备不匹配")

    return payload


def get_license_status(
    *, check_remote: bool = True, jwt_secret: Optional[str] = None
) -> dict:
    """
    返回当前授权状态.

    状态:
        unauthorized      未激活
        expired           本地 token 已过期
        invalid_device    设备不匹配
        valid             授权有效
        remote_error      在线校验失败(仅在 check_remote=True 时出现)
    """
    info = load_license()
    if not info:
        return {"status": "unauthorized", "message": "尚未激活,请输入授权码"}

    device_hash = get_device_hash()
    if info.get("device_hash") != device_hash:
        return {"status": "invalid_device", "message": "授权与当前设备不匹配"}

    # 本地先检查 token 是否过期
    secret = jwt_secret or os.environ.get("LICENSE_JWT_SECRET", "")
    if secret:
        try:
            _verify_token_locally(info["token"], device_hash, secret)
        except RuntimeError as exc:
            return {"status": "expired", "message": str(exc)}

    # 在线刷新/校验
    if check_remote:
        try:
            remote = verify_license_remote(info["token"], device_hash)
            return {
                "status": "valid",
                "message": "授权有效",
                "license_code": remote.get("license_code"),
                "expires_at": remote.get("expires_at"),
                "max_devices": remote.get("max_devices"),
                "bound_devices": remote.get("bound_devices"),
            }
        except RuntimeError as exc:
            return {"status": "remote_error", "message": str(exc)}

    return {
        "status": "valid",
        "message": "本地授权有效",
        "license_code": info.get("license_code"),
        "expires_at": info.get("expires_at"),
    }


def ensure_authorized(check_remote: bool = True, jwt_secret: Optional[str] = None) -> dict:
    """启动前调用;未授权则抛出 RuntimeError."""
    status = get_license_status(check_remote=check_remote, jwt_secret=jwt_secret)
    if status["status"] != "valid":
        raise RuntimeError(status["message"])
    return status


# ---------------------------------------------------------------------------
# 测试入口
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    print("device_hash:", get_device_hash())
    print("license_file:", LICENSE_FILE)
    print("status:", get_license_status(check_remote=False))
