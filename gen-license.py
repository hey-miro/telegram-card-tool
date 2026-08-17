#!/usr/bin/env python3
"""
授权码管理工具（本地版）
======================
在 Mac 本地直接调用远程授权服务器管理授权码，无需 SSH 登录服务器。

用法:
    # 生成授权码（30天有效，1台设备）
    ./venv/bin/python gen-license.py create --days 30

    # 生成授权码（365天，3台设备，带备注）
    ./venv/bin/python gen-license.py create --days 365 --devices 3 --notes "客户A"

    # 列出所有授权码
    ./venv/bin/python gen-license.py list

    # 续期（延长30天）
    ./venv/bin/python gen-license.py renew TGCARD-XXXX-XXXX-XXXX --days 30

    # 禁用/启用
    ./venv/bin/python gen-license.py toggle TGCARD-XXXX-XXXX-XXXX

    # 查看绑定设备
    ./venv/bin/python gen-license.py devices TGCARD-XXXX-XXXX-XXXX

首次使用前:
    1. 编辑下方 LICENSE_URL 和 ADMIN_KEY
    2. 或者通过环境变量传入（推荐，更安全）
"""

import argparse
import os
import sys

import requests

# ====== 配置区（也可以用环境变量覆盖）======
LICENSE_URL = os.environ.get("TG_CARD_LICENSE_URL", "http://47.116.59.161:9000")
ADMIN_KEY = os.environ.get("LICENSE_ADMIN_KEY", "")
# ==========================================


def _req(method: str, path: str, json_body: dict | None = None) -> dict:
    url = f"{LICENSE_URL.rstrip('/')}{path}"
    if not ADMIN_KEY:
        print("错误: 请设置环境变量 LICENSE_ADMIN_KEY", file=sys.stderr)
        print("  方法: export LICENSE_ADMIN_KEY='你的管理员密钥'", file=sys.stderr)
        sys.exit(1)
    try:
        resp = requests.request(
            method,
            url,
            json=json_body,
            headers={"X-Admin-Key": ADMIN_KEY, "Content-Type": "application/json"},
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
    from datetime import datetime, timedelta, timezone

    expires = datetime.now(timezone.utc) + timedelta(days=args.days)
    body = {
        "max_devices": args.devices,
        "expires_at": expires.strftime("%Y-%m-%dT%H:%M:%S"),
        "notes": args.notes or "",
    }
    result = _req("POST", "/admin/licenses", body)
    print("✅ 授权码已生成")
    print(f"   授权码:   {result['code']}")
    print(f"   到期时间: {result['expires_at']}")
    print(f"   设备数:   {args.devices}")
    if args.notes:
        print(f"   备注:     {args.notes}")


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
    print(f"✅ 授权码 {result['code']} 已续期")
    print(f"   新到期时间: {result['expires_at']}")


def cmd_toggle(args):
    result = _req("POST", f"/admin/licenses/{args.code}/toggle")
    status = "启用" if result["is_active"] else "禁用"
    print(f"✅ 授权码 {result['code']} 已{status}")


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
    p_create.add_argument("--days", type=int, default=30, help="有效期天数（默认30）")
    p_create.add_argument("--devices", type=int, default=1, help="最大可绑定设备数（默认1）")
    p_create.add_argument("--notes", type=str, default="", help="备注")
    p_create.set_defaults(func=cmd_create)

    p_list = sub.add_parser("list", help="列出所有授权码")
    p_list.set_defaults(func=cmd_list)

    p_renew = sub.add_parser("renew", help="续期授权码")
    p_renew.add_argument("code", help="授权码")
    p_renew.add_argument("--days", type=int, default=30, help="延长天数（默认30）")
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
