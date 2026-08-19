import asyncio
import os
import sys
import time

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from typing import Literal, Optional, Union

import store
import tg
import license_client


# 授权相关配置
_license_cache = {"status": None, "expires_at": 0}
_license_cache_ttl = 300  # 5 分钟


async def _license_status(force_remote: bool = False) -> dict:
    """获取当前授权状态,带本地缓存避免频繁请求授权服务器."""
    now = time.time()
    if not force_remote and _license_cache["expires_at"] > now:
        return _license_cache["status"]
    # license_client 使用 requests 做同步 HTTP,放到线程池避免阻塞事件循环
    status = await asyncio.to_thread(
        license_client.get_license_status,
        check_remote=True,
    )
    _license_cache["status"] = status
    _license_cache["expires_at"] = now + _license_cache_ttl
    return status


def _invalidate_license_cache():
    _license_cache["expires_at"] = 0


async def _require_license():
    status = await _license_status()
    if status["status"] != "valid":
        raise HTTPException(status_code=403, detail=f"未授权: {status['message']}")
    return status


BASE_DIR = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
STATIC_DIR = os.path.join(BASE_DIR, "static")

store.init_db()

app = FastAPI(title="Telegram 名片分享与群组工具")


# ---------------------------------------------------------------------------
# 授权中间件:未授权时阻止主业务 API,但允许授权页面、静态资源、授权接口
# ---------------------------------------------------------------------------
@app.middleware("http")
async def license_middleware(request: Request, call_next):
    path = request.url.path
    public_paths = {
        "/",
        "/api/health",
        "/api/license/status",
        "/api/license/activate",
        "/api/license/refresh",
        "/api/license/clear",
    }
    if path.startswith("/static/") or path in public_paths:
        return await call_next(request)

    status = await _license_status()
    if status["status"] != "valid":
        return JSONResponse(
            status_code=403,
            content={"detail": f"未授权: {status.get('message', '请激活软件')}"},
        )
    return await call_next(request)


# ---------------------------------------------------------------------------
# 授权接口
# ---------------------------------------------------------------------------
class LicenseActivateIn(BaseModel):
    code: str = Field(min_length=1, max_length=64)


@app.get("/api/license/status")
async def license_status():
    return await _license_status(force_remote=False)


@app.post("/api/license/activate")
async def license_activate(body: LicenseActivateIn):
    try:
        result = await asyncio.to_thread(license_client.activate_license, body.code)
        _invalidate_license_cache()
        return {"ok": True, **result}
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"激活失败: {exc}")


@app.post("/api/license/refresh")
async def license_refresh():
    info = license_client.load_license()
    if not info or not info.get("token"):
        raise HTTPException(status_code=400, detail="本地无授权凭证")
    try:
        device_hash = await asyncio.to_thread(license_client.get_device_hash)
        result = await asyncio.to_thread(
            license_client.verify_license_remote, info["token"], device_hash
        )
        _invalidate_license_cache()
        return {"ok": True, **result}
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.post("/api/license/clear")
async def license_clear():
    await asyncio.to_thread(license_client.clear_license)
    _invalidate_license_cache()
    return {"ok": True}


class ConfigIn(BaseModel):
    api_id: int = Field(gt=0)
    api_hash: Optional[str] = None
    proxy_enabled: bool = False
    proxy_type: str = "socks5"
    proxy_host: str = ""
    proxy_port: str = ""


class PhoneIn(BaseModel):
    phone: str = Field(min_length=1, max_length=32)


class CodeIn(BaseModel):
    phone: str = Field(min_length=1, max_length=32)
    code: str = Field(min_length=1, max_length=32)


class PasswordIn(BaseModel):
    phone: str = Field(min_length=1, max_length=32)
    password: str = Field(min_length=1, max_length=256)


class ImportIn(BaseModel):
    phone: str = Field(min_length=1, max_length=32)
    session: str = Field(min_length=1, max_length=4096)


class ShareTarget(BaseModel):
    type: Literal["user", "chat", "channel"]
    id: int = Field(gt=0)
    name: Optional[str] = None
    access_hash: Optional[Union[int, str]] = None


class ShareOptions(BaseModel):
    rounds: int = Field(default=1, ge=1, le=100)
    interval: float = Field(default=15, ge=1, le=3600)
    batch_size: int = Field(default=20, ge=1, le=100)
    batch_pause: float = Field(default=300, ge=0, le=3600)
    fetch_missing_names: bool = False
    skip_unresolved: bool = False
    allow_empty_name: bool = True
    fallback_first_name: str = ""
    fallback_last_name: str = ""


class ShareIn(BaseModel):
    targets: list[ShareTarget] = Field(default_factory=list)
    numbers: str = ""
    options: ShareOptions = Field(default_factory=ShareOptions)


def _handle(exc):
    if isinstance(exc, HTTPException):
        raise exc
    raise HTTPException(status_code=400, detail=tg.friendly_error(exc))


@app.get("/api/health")
async def health():
    return {"ok": True}


@app.get("/api/config")
async def get_config():
    cfg = store.get_config()
    return {
        "configured": bool(cfg["api_id"] and cfg["api_hash"]),
        "api_id": cfg["api_id"],
        "api_hash_configured": bool(cfg["api_hash"]),
        "proxy_enabled": cfg["proxy_enabled"],
        "proxy_type": cfg["proxy_type"],
        "proxy_host": cfg["proxy_host"],
        "proxy_port": cfg["proxy_port"],
    }


@app.post("/api/config")
async def set_config(body: ConfigIn):
    api_hash = (body.api_hash or "").strip()
    if not api_hash:
        api_hash = store.get_config()["api_hash"] or ""
    if not api_hash:
        raise HTTPException(status_code=400, detail="api_id 与 api_hash 不能为空")
    proxy_host = (body.proxy_host or "").strip()
    proxy_port = (body.proxy_port or "").strip()
    store.set_config(
        body.api_id,
        api_hash,
        proxy_enabled=body.proxy_enabled,
        proxy_type=(body.proxy_type or "socks5").strip().lower(),
        proxy_host=proxy_host,
        proxy_port=proxy_port,
    )
    return {"ok": True, "configured": True}


@app.get("/api/accounts")
async def accounts():
    return {"accounts": store.list_accounts()}


@app.post("/api/accounts/login/start")
async def login_start(body: PhoneIn):
    try:
        return await tg.start_login(body.phone)
    except Exception as exc:
        _handle(exc)


@app.post("/api/accounts/login/code")
async def login_code(body: CodeIn):
    try:
        return await tg.submit_code(body.phone, body.code)
    except Exception as exc:
        _handle(exc)


@app.post("/api/accounts/login/2fa")
async def login_2fa(body: PasswordIn):
    try:
        return await tg.submit_2fa(body.phone, body.password)
    except Exception as exc:
        _handle(exc)


@app.post("/api/accounts/import")
async def import_account(body: ImportIn):
    try:
        return await tg.import_session(body.phone, body.session)
    except Exception as exc:
        _handle(exc)


@app.post("/api/accounts/{account_id}/verify")
async def verify_account(account_id: int):
    try:
        return await tg.verify_account(account_id)
    except Exception as exc:
        _handle(exc)


@app.post("/api/accounts/{account_id}/send-guard/clear")
async def clear_account_send_guard(account_id: int):
    try:
        tg.clear_account_send_block(account_id)
        return {"ok": True}
    except Exception as exc:
        _handle(exc)


@app.post("/api/accounts/{account_id}/remove")
async def remove_account(account_id: int):
    store.delete_account(account_id)
    return {"ok": True}


@app.get("/api/accounts/{account_id}/dialogs")
async def dialogs(
    account_id: int,
    kind: str = "all",
    search: str = "",
    limit: int = Query(default=300, ge=1, le=1000),
):
    try:
        return {"dialogs": await tg.list_dialogs(account_id, kind, limit, search)}
    except Exception as exc:
        _handle(exc)


@app.post("/api/accounts/{account_id}/share/start")
async def share_start(account_id: int, body: ShareIn):
    targets = [target.model_dump() for target in body.targets]
    options = body.options.model_dump()
    try:
        task_id = await tg.start_share(account_id, targets, body.numbers, options)
        return {"task_id": task_id}
    except Exception as exc:
        _handle(exc)


@app.get("/api/tasks")
async def tasks():
    return {"tasks": tg.list_tasks()}


@app.get("/api/tasks/{task_id}")
async def task(task_id: str):
    snapshot = tg.get_task(task_id)
    if snapshot is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    return snapshot


@app.post("/api/tasks/{task_id}/stop")
async def stop_task(task_id: str):
    try:
        tg.stop_task(task_id)
        return {"ok": True}
    except Exception as exc:
        _handle(exc)


@app.get("/")
async def index():
    status = await _license_status()
    if status["status"] != "valid":
        return FileResponse(os.path.join(STATIC_DIR, "auth.html"))
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
