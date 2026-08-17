"""
授权码管理 CLI
=============
用于管理员生成、查看、续期、禁用授权码。

用法示例:
    # 生成一个 30 天后到期、可绑定 1 台设备的授权码
    TG_CARD_LICENSE_URL=https://license.example.com \
    LICENSE_ADMIN_KEY=your-admin-key \
        ./venv/bin/python license_admin.py create --days 30

    # 生成可绑定 3 台设备的授权码
    TG_CARD_LICENSE_URL=... LICENSE_ADMIN_KEY=... \
        ./venv/bin/python license_admin.py create --days 90 --devices 3 --notes "客户A"

    # 列出所有授权码
    TG_CARD_LICENSE_URL=... LICENSE_ADMIN_KEY=... \
        ./venv/bin/python license_admin.py list

    # 续期
    TG_CARD_LICENSE_URL=... LICENSE_ADMIN_KEY=... \
        ./venv/bin/python license_admin.py renew TGCARD-XXXX-XXXX-XXXX --days 30
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
