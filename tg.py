import asyncio
import math
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
    SlowModeWaitError,
)
from telethon.sessions import StringSession
from telethon.tl.functions.contacts import (
    DeleteContactsRequest,
    ImportContactsRequest,
)
from telethon.tl.functions.users import GetUsersRequest
from telethon.tl.types import (
    InputMediaContact,
    InputPeerChannel,
    InputPeerChat,
    InputPeerUser,
    InputPhoneContact,
    InputUser,
)

import store


_pending = {}
_tasks = {}
_task_counter = 0
_active_account_tasks = {}

REGISTERED_CACHE_TTL = 30 * 24 * 60 * 60
UNREGISTERED_CACHE_TTL = 3 * 24 * 60 * 60
MAX_AUTO_FLOOD_WAIT = 60 * 60
MAX_FLOOD_RETRIES_PER_MESSAGE = 2


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
    return TelegramClient(
        session,
        int(cfg["api_id"]),
        cfg["api_hash"],
        proxy=proxy,
        # 由任务队列统一展示倒计时、响应停止操作并从当前名片续传。
        flood_sleep_threshold=0,
    )


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
    store.upsert_account(phone, session_string, status="valid")
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
            acc = store.get_account_by_phone(phone)
            if acc:
                store.set_account_status(acc["id"], "invalid")
            raise ValueError("Session 无效，未能取得账号信息")
        saved = client.session.save()
    finally:
        await client.disconnect()

    store.upsert_account(phone, saved, status="valid")
    return {"phone": phone, "user": _user_info(me)}


async def verify_account(account_id):
    account = store.get_account(account_id)
    if not account:
        raise ValueError("账号不存在")

    client = _client(account["session"])
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
            store.set_account_status(account_id, "invalid")
            raise ValueError("Session 已失效，请重新登录")
        saved = client.session.save()
    finally:
        await client.disconnect()

    store.upsert_account(account["phone"], saved, status="valid")
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


async def _import_contacts(client, task, account_id, parsed, name_overrides=None):
    """把号码批量导入当前账号的 Telegram 通讯录。

    作用：
    1. 让 Telegram 建立号码 → 用户的关联，发出的名片会自动带头像/姓名（完整卡片样式）
    2. 顺带检测每个号码是否注册了 Telegram（返回结果只含已注册号码）

    name_overrides: {纯数字号码: (first, last)}，导入时优先使用的姓名。

    返回 {纯数字号码: (user_id, access_hash)}，仅包含已注册 Telegram 的号码。
    """
    overrides = name_overrides or {}
    contacts = []
    for idx, number in enumerate(parsed):
        phone = number["phone"]
        digits = re.sub(r"\D", "", phone)
        if digits in overrides:
            first, last = overrides[digits]
        else:
            first = number["first_name"] or f"联系人{phone[-4:]}"
            last = number["last_name"] or ""
        contacts.append((idx, phone, first, last))

    users = {}
    batch_size = 50
    for start in range(0, len(contacts), batch_size):
        chunk = contacts[start : start + batch_size]
        try:
            resp = await asyncio.wait_for(
                client(
                    ImportContactsRequest(
                        [
                            InputPhoneContact(
                                client_id=start + j,
                                phone=phone,
                                first_name=first,
                                last_name=last,
                            )
                            for j, (_, phone, first, last) in enumerate(chunk)
                        ]
                    )
                ),
                timeout=60,
            )
        except FloodWaitError:
            raise
        except (RPCError, asyncio.TimeoutError) as exc:
            _log(
                task,
                "warn",
                f"通讯录导入第 {start // batch_size + 1} 批失败({type(exc).__name__})，相关名片可能无头像",
            )
            continue
        chunk_by_client_id = {
            start + j: re.sub(r"\D", "", phone)
            for j, (_, phone, _, _) in enumerate(chunk)
        }
        response_users = {
            getattr(user, "id", None): user
            for user in (getattr(resp, "users", []) or [])
        }
        resolved_phones = set()
        cache_rows = []
        for imported in getattr(resp, "imported", []) or []:
            phone_digits = chunk_by_client_id.get(
                getattr(imported, "client_id", None)
            )
            user = response_users.get(getattr(imported, "user_id", None))
            if not phone_digits or user is None:
                continue
            users[phone_digits] = (user.id, user.access_hash)
            resolved_phones.add(phone_digits)
            cache_rows.append(
                {
                    "phone_digits": phone_digits,
                    "user_id": user.id,
                    "access_hash": user.access_hash,
                    # importContacts 返回的姓名可能是本地备注，不当作账号资料名缓存。
                    "first_name": "",
                    "last_name": "",
                    "is_registered": True,
                }
            )

        # 兼容 Telegram 只在 users 中返回号码、imported 信息不完整的情况。
        for user in getattr(resp, "users", []) or []:
            uphone = re.sub(r"\D", "", str(getattr(user, "phone", None) or ""))
            if uphone:
                users[uphone] = (user.id, user.access_hash)
                if uphone not in resolved_phones:
                    resolved_phones.add(uphone)
                    cache_rows.append(
                        {
                            "phone_digits": uphone,
                            "user_id": user.id,
                            "access_hash": user.access_hash,
                            "first_name": "",
                            "last_name": "",
                            "is_registered": True,
                        }
                    )

        retry_ids = set(getattr(resp, "retry_contacts", []) or [])
        for client_id, phone_digits in chunk_by_client_id.items():
            if phone_digits not in resolved_phones and client_id not in retry_ids:
                cache_rows.append(
                    {
                        "phone_digits": phone_digits,
                        "is_registered": False,
                    }
                )
        store.upsert_contact_cache(account_id, cache_rows)
        if retry_ids:
            _log(
                task,
                "warn",
                f"本批有 {len(retry_ids)} 个号码被 Telegram 要求稍后重试，未写入未注册缓存",
            )
    return users


async def _prepare_contacts(client, task, account_id, parsed):
    """Resolve only uncached/stale contacts and reuse fresh local results."""
    now = int(time.time())
    phone_digits = [re.sub(r"\D", "", item["phone"]) for item in parsed]
    cached = store.get_contact_cache(account_id, phone_digits)
    users = {}
    profile_names = {}
    unresolved = []

    for item in parsed:
        digits = re.sub(r"\D", "", item["phone"])
        row = cached.get(digits)
        ttl = REGISTERED_CACHE_TTL if row and row["is_registered"] else UNREGISTERED_CACHE_TTL
        if not row or now - row["checked_at"] > ttl:
            unresolved.append(item)
            continue
        if row["is_registered"] and row["user_id"] is not None and row["access_hash"] is not None:
            users[digits] = (row["user_id"], int(row["access_hash"]))
            if row["first_name"] or row["last_name"]:
                profile_names[digits] = (row["first_name"], row["last_name"])

    cache_hits = len(parsed) - len(unresolved)
    if cache_hits:
        _log(task, "info", f"联系人缓存命中 {cache_hits}/{len(parsed)} 个号码")
    if unresolved:
        _log(task, "info", f"仅查询 {len(unresolved)} 个新增或已过期号码")
        users.update(await _import_contacts(client, task, account_id, unresolved))
        refreshed = store.get_contact_cache(account_id, phone_digits)
        for digits, row in refreshed.items():
            if row["first_name"] or row["last_name"]:
                profile_names[digits] = (row["first_name"], row["last_name"])

    return users, profile_names


async def _fetch_profile_names(client, task, account_id, users):
    """拉取已注册号码对应 Telegram 账号的资料姓名。

    原理：号码在通讯录中时，接口返回的是我们自己存的备注名；
    删除联系人后返回的才是对方账号资料上设置的名字。
    因此流程为：删除联系人 -> GetUsers 取资料名 -> 再重新导入（保证发名片时仍有关联）。

    users: {纯数字号码: (user_id, access_hash)}
    返回 {纯数字号码: (first_name, last_name)}，可能只包含部分号码（查询失败的不在内）。
    """
    if not users:
        return {}

    input_users = [
        InputUser(user_id=uid, access_hash=ahash) for uid, ahash in users.values()
    ]
    phones_by_uid = {uid: phone for phone, (uid, _) in users.items()}
    profile_names = {}
    pending_flood = None

    try:
        # 1) 删除联系人，让 GetUsers 返回真实资料名
        for start in range(0, len(input_users), 50):
            chunk = input_users[start : start + 50]
            await asyncio.wait_for(
                client(DeleteContactsRequest(id=chunk)), timeout=60
            )
        # 2) 批量拉取资料名
        for start in range(0, len(input_users), 50):
            chunk = input_users[start : start + 50]
            resp = await asyncio.wait_for(
                client(GetUsersRequest(id=chunk)), timeout=60
            )
            for user in resp or []:
                uid = getattr(user, "id", None)
                phone = phones_by_uid.get(uid)
                if not phone:
                    continue
                first = getattr(user, "first_name", None) or ""
                last = getattr(user, "last_name", None) or ""
                if first or last:
                    profile_names[phone] = (first, last)
    except FloodWaitError as exc:
        pending_flood = exc
    except (RPCError, asyncio.TimeoutError) as exc:
        _log(
            task,
            "warn",
            f"拉取 Telegram 资料姓名失败({type(exc).__name__})，将回退到其他命名规则",
        )

    finally:
        # 无论查询是否成功，都恢复刚才删除的联系人，避免限流发生在流程中间时丢联系人。
        restore_names = {
            phone: profile_names.get(phone, (f"联系人{phone[-4:]}", ""))
            for phone in users
        }
        await _reimport_with_names(client, task, restore_names)

    if pending_flood is not None:
        raise pending_flood
    if profile_names:
        store.update_cached_contact_names(account_id, profile_names)
    return profile_names


async def _reimport_with_names(client, task, profile_names):
    """按资料姓名重新导入联系人，恢复号码-账号关联状态."""
    contacts = [
        InputPhoneContact(
            client_id=i, phone=f"+{phone}", first_name=first, last_name=last
        )
        for i, (phone, (first, last)) in enumerate(profile_names.items())
    ]
    for start in range(0, len(contacts), 50):
        chunk = contacts[start : start + 50]
        try:
            await asyncio.wait_for(
                client(ImportContactsRequest(chunk)), timeout=60
            )
        except FloodWaitError:
            raise
        except (RPCError, asyncio.TimeoutError) as exc:
            _log(
                task,
                "warn",
                f"恢复通讯录关联第 {start // 50 + 1} 批失败({type(exc).__name__})，相关名片可能无头像",
            )


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


async def _wait_with_status(task, seconds, reason):
    seconds = max(0, int(math.ceil(seconds)))
    task["status"] = "waiting"
    task["waiting_reason"] = reason
    task["wait_until"] = int(time.time()) + seconds
    await _sleep_or_stop(seconds, task["stop"])
    task["waiting_reason"] = None
    task["wait_until"] = None
    if task["stop"].is_set():
        task["status"] = "stopped"
        return False
    task["status"] = "running"
    return True


async def _handle_rate_limit(task, exc, context, attempt):
    wait = max(1, int(getattr(exc, "seconds", 0) or 1))
    if wait > MAX_AUTO_FLOOD_WAIT or attempt > MAX_FLOOD_RETRIES_PER_MESSAGE:
        task["status"] = "stopped"
        task["error"] = (
            f"{context}触发长时间或重复限流，需要等待 {wait} 秒；"
            "已执行账号熔断，请等待后人工重试"
        )
        _log(task, "error", task["error"])
        return False

    safety_margin = max(5, math.ceil(wait * 0.1))
    total_wait = wait + safety_margin
    _log(
        task,
        "warn",
        f"{context}触发限流，安全暂停 {total_wait} 秒后从当前位置重试",
    )
    return await _wait_with_status(task, total_wait, f"Telegram 限流：{context}")


def _is_account_restriction(exc):
    error_name = type(exc).__name__.upper()
    error_text = str(exc).upper()
    markers = ("PEER_FLOOD", "USER_RESTRICTED", "USER_DEACTIVATED", "ACCOUNT_RESTRICTED")
    return any(marker in error_name or marker in error_text for marker in markers)


async def start_share(account_id, targets, numbers_text, options):
    account = store.get_account(account_id)
    if not account:
        raise ValueError("账号不存在")
    if not targets:
        raise ValueError("请先选择目标会话/群组")

    parsed = parse_numbers(numbers_text)
    if not parsed:
        raise ValueError("请输入至少一个有效手机号")

    active_task_id = _active_account_tasks.get(account_id)
    active_task = _tasks.get(active_task_id) if active_task_id else None
    if active_task and active_task["status"] in ("running", "waiting"):
        raise ValueError("该账号已有发送任务，请等待完成或先停止当前任务")

    global _task_counter
    _task_counter += 1
    task_id = f"task-{_task_counter}"
    stop_event = asyncio.Event()
    rounds = max(1, int(options.get("rounds", 1)))
    task = {
        "id": task_id,
        "account_id": account_id,
        "status": "running",
        "total": rounds * len(parsed) * len(targets),
        "done": 0,
        "ok": 0,
        "failed": 0,
        "skipped": 0,
        "logs": [],
        "stop": stop_event,
        "error": None,
        "waiting_reason": None,
        "wait_until": None,
    }
    _tasks[task_id] = task
    _active_account_tasks[account_id] = task_id
    asyncio.create_task(_run_share(task, account, targets, parsed, options))
    return task_id


async def _run_share(task, account, targets, parsed, options):
    rounds = max(1, int(options.get("rounds", 1)))
    interval = max(1.0, float(options.get("interval", 15)))
    batch_size = max(1, int(options.get("batch_size", 20)))
    batch_pause = max(0.0, float(options.get("batch_pause", 300)))
    fetch_names = bool(options.get("fetch_missing_names", False))
    skip_unresolved = bool(options.get("skip_unresolved", False))
    allow_empty = bool(options.get("allow_empty_name", True))
    fallback_first = str(options.get("fallback_first_name", "") or "")
    fallback_last = str(options.get("fallback_last_name", "") or "")

    peers = [_peer_from_target(target) for target in targets]
    target_names = [
        (t.get("name") or f"{t.get('type','?')}#{t.get('id','?')}") for t in targets
    ]
    target_summary = ", ".join(target_names[:3])
    if len(target_names) > 3:
        target_summary += f" 等 {len(target_names)} 个目标"

    client = None
    try:
        client = _client(account["session"])
        if not await _connect_or_stop(client, task):
            task["status"] = "stopped"
            _log(task, "warn", "任务已在连接阶段停止")
            return
        _log(task, "info", f"账号 {mask_phone(account['phone'])} 已连接")
        _log(task, "info", f"发送目标: {target_summary}")
        _log(
            task,
            "info",
            f"安全发送：每批 {batch_size} 条、每条间隔 {interval:g} 秒、批次冷却 {batch_pause:g} 秒",
        )

        prepare_attempt = 0
        while True:
            try:
                users, profile_names = await _prepare_contacts(
                    client, task, account["id"], parsed
                )
                break
            except FloodWaitError as exc:
                prepare_attempt += 1
                if not await _handle_rate_limit(
                    task, exc, "联系人准备", prepare_attempt
                ):
                    return
        registered = set(users.keys())
        _log(
            task,
            "info",
            f"通讯录导入完成：{len(registered)}/{len(parsed)} 个号码已关联 Telegram 账号",
        )

        # 勾选「批量获取缺失姓名」时，拉取对方 Telegram 账号资料上设置的姓名
        if fetch_names and registered:
            input_named_phones = {
                re.sub(r"\D", "", item["phone"])
                for item in parsed
                if item["first_name"] or item["last_name"]
            }
            users_needing_names = {
                digits: user
                for digits, user in users.items()
                if digits not in profile_names and digits not in input_named_phones
            }
            fetch_attempt = 0
            while users_needing_names:
                try:
                    fetched_names = await _fetch_profile_names(
                        client, task, account["id"], users_needing_names
                    )
                    profile_names.update(fetched_names)
                    break
                except FloodWaitError as exc:
                    fetch_attempt += 1
                    if not await _handle_rate_limit(
                        task, exc, "资料姓名查询", fetch_attempt
                    ):
                        return
            fetched = sum(
                1
                for n in parsed
                if not (n["first_name"] or n["last_name"])
                and re.sub(r"\D", "", n["phone"]) in profile_names
            )
            _log(
                task,
                "info",
                f"资料姓名获取完成：{fetched} 个号码取到 Telegram 账号姓名",
            )

        if skip_unresolved:
            unreg_count = sum(
                1 for n in parsed if re.sub(r"\D", "", n["phone"]) not in registered
            )
            if unreg_count:
                _log(task, "warn", f"检测到 {unreg_count} 个号码未注册 Telegram，将按规则跳过")
        sent_in_batch = 0
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
                phone_digits = re.sub(r"\D", "", phone)

                if skip_unresolved and phone_digits not in registered:
                    task["skipped"] += len(peers)
                    task["done"] += len(peers)
                    _log(
                        task,
                        "warn",
                        f"{mask_phone(phone)} 未关联 Telegram 账号，已跳过",
                    )
                    continue

                if not first and not last:
                    # 优先级：Telegram 账号资料姓名 > 回退姓名 > 自动命名（联系人+尾号）
                    tg_first, tg_last = profile_names.get(phone_digits, ("", ""))
                    if fetch_names and (tg_first or tg_last):
                        first = tg_first
                        last = tg_last
                        _log(
                            task,
                            "info",
                            f"{mask_phone(phone)} 已获取 Telegram 姓名 {first} {last}".strip(),
                        )
                    elif fallback_first or fallback_last:
                        first = fallback_first
                        last = fallback_last
                    elif allow_empty:
                        first = f"联系人{phone[-4:]}"
                        _log(
                            task,
                            "info",
                            f"{mask_phone(phone)} 无姓名，自动命名「{first}」",
                        )
                    else:
                        task["skipped"] += len(peers)
                        task["done"] += len(peers)
                        _log(task, "warn", f"{mask_phone(phone)} 无姓名，已按规则跳过")
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
                    flood_attempt = 0
                    while not task["stop"].is_set():
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
                            break
                        except (FloodWaitError, SlowModeWaitError) as exc:
                            flood_attempt += 1
                            if not await _handle_rate_limit(
                                task,
                                exc,
                                f"发送 {mask_phone(phone)}",
                                flood_attempt,
                            ):
                                if not task["stop"].is_set():
                                    task["failed"] += 1
                                    task["done"] += 1
                                return
                        except Exception as exc:
                            task["failed"] += 1
                            if _is_account_restriction(exc):
                                task["status"] = "stopped"
                                task["error"] = "Telegram 已限制该账号，任务已熔断；请通过 @SpamBot 检查账号状态"
                                _log(task, "error", task["error"])
                                task["done"] += 1
                                return
                            _log(
                                task,
                                "error",
                                f"发送 {mask_phone(phone)} 失败: {friendly_error(exc)}",
                            )
                            break

                    task["done"] += 1
                    sent_in_batch += 1

                    if task["done"] < task["total"]:
                        await _sleep_or_stop(interval, task["stop"])
                    if (
                        not task["stop"].is_set()
                        and sent_in_batch >= batch_size
                        and task["done"] < task["total"]
                        and batch_pause > 0
                    ):
                        sent_in_batch = 0
                        _log(task, "info", f"本批 {batch_size} 条已完成，开始批次冷却")
                        if not await _wait_with_status(task, batch_pause, "批次冷却"):
                            break

        if task["stop"].is_set():
            task["status"] = "stopped"
        else:
            task["status"] = "done"
    except Exception as exc:
        task["status"] = "error"
        task["error"] = str(exc)
        _log(task, "error", f"任务异常: {exc}")
    finally:
        try:
            if client is not None:
                await client.disconnect()
        finally:
            if _active_account_tasks.get(account["id"]) == task["id"]:
                _active_account_tasks.pop(account["id"], None)


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
        "waiting_reason": task.get("waiting_reason"),
        "wait_until": task.get("wait_until"),
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
