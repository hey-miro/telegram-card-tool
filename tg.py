import asyncio
import re
import time

from telethon import TelegramClient
from telethon.errors import (
    ApiIdInvalidError,
    AuthKeyUnregisteredError,
    FloodWaitError,
    PasswordHashInvalidError,
    PhoneCodeEmptyError,
    PhoneCodeExpiredError,
    PhoneCodeInvalidError,
    PhoneNumberInvalidError,
    RPCError,
    SendCodeUnavailableError,
    SessionPasswordNeededError,
)
from telethon.sessions import StringSession
from telethon.tl.functions.contacts import ImportContactsRequest
from telethon.tl.types import (
    InputMediaContact,
    InputPeerChannel,
    InputPeerChat,
    InputPeerUser,
    InputPhoneContact,
)

import store


_pending = {}
_tasks = {}
_task_counter = 0


def normalize_phone(raw):
    phone = re.sub(r"[\s()\-]", "", str(raw or "").strip())
    if not re.fullmatch(r"\+?\d{5,15}", phone):
        return ""
    return phone


def mask_phone(phone):
    digits = re.sub(r"\D", "", str(phone or ""))
    if not digits:
        return "***"
    if len(digits) <= 7:
        return digits[:2] + "*" * (len(digits) - 2)
    return digits[:4] + "*" * (len(digits) - 7) + digits[-3:]


def parse_numbers(text):
    result = []
    for raw_line in (text or "").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        parts = [p.strip() for p in re.split(r"[;\t,|]", line)]
        phone = normalize_phone(parts[0])
        if not phone:
            continue
        first = parts[1] if len(parts) > 1 else ""
        last = parts[2] if len(parts) > 2 else ""
        result.append(
            {
                "phone": phone,
                "first_name": first,
                "last_name": last,
                "had_name": bool(first or last),
            }
        )
    return result


def _proxy_tuple(cfg):
    """根据配置构造 Telethon 代理参数，未启用或缺少地址时返回 None."""
    if not cfg.get("proxy_enabled") or not cfg.get("proxy_host") or not cfg.get("proxy_port"):
        return None
    try:
        port = int(cfg["proxy_port"])
    except (TypeError, ValueError):
        return None
    if port <= 0 or port > 65535:
        return None
    ptype = (cfg.get("proxy_type") or "socks5").lower()
    if ptype not in ("socks5", "socks4", "http"):
        ptype = "socks5"
    return (ptype, cfg["proxy_host"].strip(), port)


def _client(session_string=None):
    cfg = store.get_config()
    if not cfg["api_id"] or not cfg["api_hash"]:
        raise ValueError("请先在「API 设置」中填写 api_id 与 api_hash")
    session = StringSession(session_string) if session_string else StringSession()
    proxy = _proxy_tuple(cfg)
    return TelegramClient(session, int(cfg["api_id"]), cfg["api_hash"], proxy=proxy)


def _user_info(user):
    if user is None:
        return None
    return {
        "id": getattr(user, "id", None),
        "first_name": getattr(user, "first_name", None),
        "last_name": getattr(user, "last_name", None),
        "username": getattr(user, "username", None),
        "phone": getattr(user, "phone", None),
    }


async def start_login(phone):
    phone = normalize_phone(phone)
    if not phone:
        raise ValueError("手机号不能为空")

    old = _pending.pop(phone, None)
    if old:
        try:
            await old["client"].disconnect()
        except Exception:
            pass

    client = _client()
    try:
        await asyncio.wait_for(client.connect(), timeout=30)
    except Exception as exc:
        try:
            await client.disconnect()
        except Exception:
            pass
        if isinstance(exc, asyncio.TimeoutError):
            raise ValueError(
                "连接 Telegram 超时（30 秒）：网络不通或需要代理，请检查网络后重试"
            )
        raise

    try:
        sent = await asyncio.wait_for(client.send_code_request(phone), timeout=60)
    except asyncio.TimeoutError:
        try:
            await client.disconnect()
        except Exception:
            pass
        raise ValueError(
            "验证码请求超时（60 秒）：Telegram 服务暂不可达或被限流，请稍后重试，不要连续点击"
        )
    except Exception:
        try:
            await client.disconnect()
        except Exception:
            pass
        raise

    _pending[phone] = {
        "client": client,
        "phone_code_hash": sent.phone_code_hash,
    }
    return {
        "next": "code",
        "phone": phone,
        "timeout": getattr(sent, "timeout", None),
    }


async def _finish_login(phone, client, me):
    session_string = client.session.save()
    store.upsert_account(phone, session_string)
    await client.disconnect()
    _pending.pop(phone, None)
    return {"next": "done", "phone": phone, "user": _user_info(me)}


async def submit_code(phone, code):
    phone = normalize_phone(phone)
    pending = _pending.get(phone)
    if not pending:
        raise ValueError("没有待处理的登录请求，请重新发送验证码")

    client = pending["client"]
    code_value = str(code).strip()
    if not code_value:
        raise ValueError("请输入验证码")
    try:
        me = await asyncio.wait_for(
            client.sign_in(
                phone,
                code_value,
                phone_code_hash=pending["phone_code_hash"],
            ),
            timeout=60,
        )
    except asyncio.TimeoutError:
        raise ValueError("登录验证超时，请重新发送验证码再试")
    except SessionPasswordNeededError as exc:
        return {"next": "2fa", "hint": getattr(exc, "hint", None)}
    except Exception:
        await client.disconnect()
        _pending.pop(phone, None)
        raise

    return await _finish_login(phone, client, me)


async def submit_2fa(phone, password):
    phone = normalize_phone(phone)
    pending = _pending.get(phone)
    if not pending:
        raise ValueError("没有待处理的登录请求，请重新发送验证码")

    client = pending["client"]
    try:
        me = await asyncio.wait_for(
            client.sign_in(password=str(password or "")), timeout=60
        )
    except asyncio.TimeoutError:
        raise ValueError("两步验证超时，请重新发送验证码再试")
    except Exception:
        await client.disconnect()
        _pending.pop(phone, None)
        raise

    return await _finish_login(phone, client, me)


async def import_session(phone, session_string):
    phone = normalize_phone(phone)
    if not session_string:
        raise ValueError("Session 字符串不能为空")

    client = _client(session_string)
    try:
        await asyncio.wait_for(client.connect(), timeout=30)
    except asyncio.TimeoutError:
        try:
            await client.disconnect()
        except Exception:
            pass
        raise ValueError("连接 Telegram 超时（30 秒）：网络不通或需要代理，请检查网络后重试")
    try:
        me = await asyncio.wait_for(client.get_me(), timeout=30)
        if me is None:
            raise ValueError("Session 无效，未能取得账号信息")
        saved = client.session.save()
    finally:
        await client.disconnect()

    store.upsert_account(phone, saved)
    return {"phone": phone, "user": _user_info(me)}


async def verify_account(account_id):
    account = store.get_account(account_id)
    if not account:
        raise ValueError("账号不存在")

    client = _client(account["session"])
    await client.connect()
    try:
        me = await client.get_me()
        if me is None:
            raise ValueError("Session 已失效，请重新登录")
        saved = client.session.save()
    finally:
        await client.disconnect()

    store.upsert_account(account["phone"], saved)
    return {"phone": account["phone"], "user": _user_info(me), "ok": True}


def _dialog_info(dialog):
    entity = dialog.entity
    if dialog.is_user:
        kind = "private"
        d_type = "user"
        peer_id = getattr(entity, "id", None)
        access_hash = getattr(entity, "access_hash", None)
        name = dialog.name or getattr(entity, "first_name", None) or str(peer_id)
        username = getattr(entity, "username", None)
    elif dialog.is_channel:
        d_type = "channel"
        peer_id = getattr(entity, "id", None)
        access_hash = getattr(entity, "access_hash", None)
        kind = "group" if getattr(entity, "megagroup", False) else "channel"
        name = dialog.name or getattr(entity, "title", None) or str(peer_id)
        username = getattr(entity, "username", None)
    elif dialog.is_group:
        d_type = "chat"
        peer_id = getattr(entity, "id", None)
        access_hash = None
        kind = "group"
        name = dialog.name or getattr(entity, "title", None) or str(peer_id)
        username = None
    else:
        return None

    return {
        "key": f"{d_type}:{peer_id}",
        "type": d_type,
        "id": peer_id,
        "access_hash": str(access_hash) if access_hash is not None else None,
        "kind": kind,
        "name": name,
        "username": username,
    }


async def list_dialogs(account_id, kind="all", limit=300, search=""):
    account = store.get_account(account_id)
    if not account:
        raise ValueError("账号不存在")

    client = _client(account["session"])
    await client.connect()
    try:
        dialogs = await client.get_dialogs(limit=None)
        needle = (search or "").strip().lower()
        result = []
        for dialog in dialogs:
            item = _dialog_info(dialog)
            if item is None:
                continue
            if kind != "all" and item["kind"] != kind:
                continue
            if needle:
                name_hit = needle in (item["name"] or "").lower()
                username_hit = needle in (item["username"] or "").lower()
                if not (name_hit or username_hit):
                    continue
            result.append(item)
            if len(result) >= limit:
                break
        return result
    finally:
        await client.disconnect()


def _peer_from_target(target):
    d_type = target.get("type")
    peer_id = int(target["id"])
    access_hash = target.get("access_hash")
    if access_hash is not None:
        access_hash = int(access_hash)
    if d_type == "user":
        return InputPeerUser(user_id=peer_id, access_hash=access_hash)
    if d_type == "chat":
        return InputPeerChat(chat_id=peer_id)
    if d_type == "channel":
        if access_hash is None:
            raise ValueError("频道/超级群缺少 access_hash,无法构造发送目标")
        return InputPeerChannel(channel_id=peer_id, access_hash=access_hash)
    raise ValueError(f"未知目标类型: {d_type}")


async def _resolve_name(client, phone):
    try:
        response = await client(
            ImportContactsRequest(
                [
                    InputPhoneContact(
                        client_id=0,
                        phone=phone,
                        first_name="",
                        last_name="",
                    )
                ]
            )
        )
        if response.users:
            user = response.users[0]
            return (
                getattr(user, "first_name", None) or "",
                getattr(user, "last_name", None) or "",
            )
    except RPCError:
        return "", ""
    return "", ""


async def _sleep_or_stop(seconds, stop_event):
    try:
        await asyncio.wait_for(stop_event.wait(), timeout=seconds)
    except asyncio.TimeoutError:
        pass


async def _connect_or_stop(client, task, timeout=45.0):
    """连接 Telegram;期间响应停止请求,超时抛异常.返回 False 表示已请求停止."""
    connect_fut = asyncio.ensure_future(client.connect())
    stop_fut = asyncio.ensure_future(task["stop"].wait())
    try:
        done, _ = await asyncio.wait(
            {connect_fut, stop_fut},
            timeout=timeout,
            return_when=asyncio.FIRST_COMPLETED,
        )
        if stop_fut in done:
            return False
        if connect_fut in done:
            connect_fut.result()  # 触发可能的连接异常
            return True
        raise TimeoutError("连接 Telegram 超时,请检查网络后重试")
    finally:
        for fut in (connect_fut, stop_fut):
            if not fut.done():
                fut.cancel()


def _log(task, level, message):
    task["logs"].append(
        {"t": time.strftime("%H:%M:%S"), "level": level, "msg": message}
    )
    if len(task["logs"]) > 500:
        task["logs"] = task["logs"][-500:]


async def start_share(account_id, targets, numbers_text, options):
    account = store.get_account(account_id)
    if not account:
        raise ValueError("账号不存在")
    if not targets:
        raise ValueError("请先选择目标会话/群组")

    parsed = parse_numbers(numbers_text)
    if not parsed:
        raise ValueError("请输入至少一个有效手机号")

    global _task_counter
    _task_counter += 1
    task_id = f"task-{_task_counter}"
    stop_event = asyncio.Event()
    rounds = max(1, int(options.get("rounds", 1)))
    task = {
        "id": task_id,
        "status": "running",
        "total": rounds * len(parsed) * len(targets),
        "done": 0,
        "ok": 0,
        "failed": 0,
        "skipped": 0,
        "logs": [],
        "stop": stop_event,
        "error": None,
    }
    _tasks[task_id] = task
    asyncio.create_task(_run_share(task, account, targets, parsed, options))
    return task_id


async def _run_share(task, account, targets, parsed, options):
    rounds = max(1, int(options.get("rounds", 1)))
    interval = max(0.0, float(options.get("interval", 8)))
    fetch_names = bool(options.get("fetch_missing_names", False))
    skip_unresolved = bool(options.get("skip_unresolved", False))
    allow_empty = bool(options.get("allow_empty_name", False))
    fallback_first = str(options.get("fallback_first_name", "") or "")
    fallback_last = str(options.get("fallback_last_name", "") or "")

    peers = [_peer_from_target(target) for target in targets]
    target_names = [
        (t.get("name") or f"{t.get('type','?')}#{t.get('id','?')}") for t in targets
    ]
    target_summary = ", ".join(target_names[:3])
    if len(target_names) > 3:
        target_summary += f" 等 {len(target_names)} 个目标"

    client = _client(account["session"])
    try:
        if not await _connect_or_stop(client, task):
            task["status"] = "stopped"
            _log(task, "warn", "任务已在连接阶段停止")
            return
        _log(task, "info", f"账号 {mask_phone(account['phone'])} 已连接")
        _log(task, "info", f"发送目标: {target_summary}")
        for round_no in range(1, rounds + 1):
            if task["stop"].is_set():
                break
            _log(task, "info", f"开始第 {round_no}/{rounds} 轮")
            for number in parsed:
                if task["stop"].is_set():
                    break

                phone = number["phone"]
                first = number["first_name"] or ""
                last = number["last_name"] or ""

                if not first and not last and fetch_names:
                    resolved_first, resolved_last = await asyncio.wait_for(
                        _resolve_name(client, phone), timeout=30
                    )
                    if resolved_first or resolved_last:
                        first = resolved_first or ""
                        last = resolved_last or ""
                        _log(
                            task,
                            "info",
                            f"{mask_phone(phone)} 已获取姓名 {first} {last}".strip(),
                        )

                if not first and not last:
                    if fallback_first or fallback_last:
                        first = fallback_first
                        last = fallback_last
                    elif skip_unresolved:
                        task["skipped"] += len(peers)
                        task["done"] += len(peers)
                        _log(
                            task,
                            "warn",
                            f"{mask_phone(phone)} 无姓名，按规则跳过",
                        )
                        continue
                    elif not allow_empty:
                        task["skipped"] += len(peers)
                        task["done"] += len(peers)
                        _log(
                            task,
                            "warn",
                            f"{mask_phone(phone)} 不允许空姓名，已跳过",
                        )
                        continue

                media = InputMediaContact(
                    phone_number=phone,
                    first_name=first,
                    last_name=last,
                    vcard="",
                )
                for peer in peers:
                    if task["stop"].is_set():
                        break
                    try:
                        sent_msg = await asyncio.wait_for(
                            client.send_file(peer, media), timeout=60
                        )
                        task["ok"] += 1
                        msg_id = getattr(sent_msg, "id", None)
                        if msg_id:
                            _log(
                                task,
                                "info",
                                f"{mask_phone(phone)} 已发送 msg_id={msg_id}",
                            )
                    except FloodWaitError as exc:
                        wait = getattr(exc, "seconds", 0)
                        task["failed"] += 1
                        task["error"] = f"触发 Telegram 限流，请 {wait} 秒（约 {wait // 3600} 小时）后再发名片"
                        _log(
                            task,
                            "error",
                            f"发送 {mask_phone(phone)} 触发限流，需等待 {wait} 秒",
                        )
                        return
                    except Exception as exc:
                        task["failed"] += 1
                        _log(
                            task,
                            "error",
                            f"发送 {mask_phone(phone)} 失败: {friendly_error(exc)}",
                        )
                    finally:
                        task["done"] += 1

                    if interval > 0:
                        await _sleep_or_stop(interval, task["stop"])

            if rounds > 1 and round_no < rounds and interval > 0:
                await _sleep_or_stop(interval, task["stop"])

        if task["stop"].is_set():
            task["status"] = "stopped"
        else:
            task["status"] = "done"
    except Exception as exc:
        task["status"] = "error"
        task["error"] = str(exc)
        _log(task, "error", f"任务异常: {exc}")
    finally:
        await client.disconnect()


def get_task(task_id):
    task = _tasks.get(task_id)
    if not task:
        return None
    return {
        "id": task["id"],
        "status": task["status"],
        "total": task["total"],
        "done": task["done"],
        "ok": task["ok"],
        "failed": task["failed"],
        "skipped": task["skipped"],
        "error": task["error"],
        "logs": task["logs"][-500:],
    }


def list_tasks():
    return [get_task(task_id) for task_id in list(_tasks.keys())]


def stop_task(task_id):
    task = _tasks.get(task_id)
    if not task:
        raise ValueError("任务不存在")
    task["stop"].set()
    return True


def friendly_error(exc):
    if isinstance(exc, PhoneCodeInvalidError):
        return "验证码不正确"
    if isinstance(exc, PhoneCodeExpiredError):
        return "验证码已过期，请重新获取"
    if isinstance(exc, PhoneCodeEmptyError):
        return "请输入验证码"
    if isinstance(exc, PhoneNumberInvalidError):
        return "手机号格式不正确"
    if isinstance(exc, PasswordHashInvalidError):
        return "两步验证密码不正确"
    if isinstance(exc, ApiIdInvalidError):
        return "API ID 无效"
    if isinstance(exc, AuthKeyUnregisteredError):
        return "会话已注销，请重新登录"
    if isinstance(exc, FloodWaitError):
        return f"触发限流，请在 {getattr(exc, 'seconds', 60)} 秒后重试"
    if isinstance(exc, SendCodeUnavailableError):
        return "验证码发送暂时不可用：该号码的发送方式已用完或暂时被限流，请等待一段时间后再试，期间不要反复点击发送"
    if isinstance(exc, SessionPasswordNeededError):
        return "该账号需要两步验证密码"
    if isinstance(exc, (ConnectionError, OSError)):
        return "无法连接 Telegram 服务器：网络不通或需要代理，请检查网络后重试"
    return str(exc)
