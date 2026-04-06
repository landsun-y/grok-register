#!/usr/bin/env python3
"""
VMAIL API 最小联调：创建邮箱 → 列邮件 → 拉一封详情（若存在）。

用法（勿把真实 Key 写进仓库，用环境变量或命令行传入）：

  export VMAIL_API_KEY='你的key'
  python3 scripts/vmail_api_demo.py --base https://vmail.liu954326053.workers.dev

或：

  python3 scripts/vmail_api_demo.py --base https://vmail.liu954326053.workers.dev --key '你的key'

依赖：仅 Python 3 标准库。
"""
from __future__ import annotations

import argparse
import json
import os
import ssl
import sys
import urllib.error
import urllib.request
from typing import Any, Dict, Optional, Tuple


def normalize_api_base(api_base: str) -> str:
    api_base = api_base.strip().rstrip("/")
    if api_base.endswith("/api-docs"):
        api_base = api_base[: -len("/api-docs")]
    if api_base.endswith("/api/v1"):
        return api_base
    if api_base.endswith("/api"):
        return f"{api_base}/v1"
    return f"{api_base}/api/v1"


def http_json(
    method: str,
    url: str,
    *,
    headers: Dict[str, str],
    body: Optional[dict] = None,
    timeout: float = 30.0,
) -> Tuple[int, Any]:
    data = None
    h = dict(headers)
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        h.setdefault("Content-Type", "application/json")
    req = urllib.request.Request(url, data=data, method=method.upper(), headers=h)
    ctx = ssl.create_default_context()
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            code = resp.getcode()
            if not raw.strip():
                return code, None
            return code, json.loads(raw)
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", errors="replace")
        try:
            parsed = json.loads(raw) if raw.strip() else None
        except json.JSONDecodeError:
            parsed = raw
        return e.code, parsed


def main() -> int:
    p = argparse.ArgumentParser(description="VMAIL API smoke test")
    p.add_argument(
        "--base",
        default=os.environ.get("VMAIL_API_BASE", "https://vmail.liu954326053.workers.dev"),
        help="站点根 URL（不要带 /api/v1，脚本会自动补全）",
    )
    p.add_argument(
        "--key",
        default=os.environ.get("VMAIL_API_KEY", ""),
        help="API Key；也可设置环境变量 VMAIL_API_KEY",
    )
    p.add_argument(
        "--domain",
        default=os.environ.get("VMAIL_DOMAIN", ""),
        help="可选，创建邮箱时指定域名",
    )
    p.add_argument(
        "--expires",
        type=int,
        default=86400,
        help="邮箱过期秒数，默认 86400",
    )
    args = p.parse_args()
    if not args.key.strip():
        print("错误: 未提供 API Key。请设置 VMAIL_API_KEY 或使用 --key。", file=sys.stderr)
        return 2

    api_base = normalize_api_base(args.base)
    hdrs = {
        "Accept": "application/json",
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
        ),
        "X-API-Key": args.key.strip(),
    }

    print(f"[*] api_base = {api_base}")

    create_body: Dict[str, Any] = {"expiresIn": args.expires}
    if args.domain.strip():
        create_body["domain"] = args.domain.strip()

    code, create_resp = http_json(
        "POST",
        f"{api_base}/mailboxes",
        headers=hdrs,
        body=create_body,
    )
    print(f"[*] POST /mailboxes -> HTTP {code}")
    print(json.dumps(create_resp, ensure_ascii=False, indent=2))

    if code not in (200, 201) or not isinstance(create_resp, dict):
        return 1

    data = create_resp.get("data") or {}
    mid = str(data.get("id") or "")
    addr = str(data.get("address") or "")
    if not mid or not addr:
        print("错误: 创建响应缺少 data.id / data.address", file=sys.stderr)
        return 1

    print(f"\n[*] mailbox_id={mid} address={addr}\n")

    code2, list_resp = http_json(
        "GET",
        f"{api_base}/mailboxes/{mid}/messages?page=1&limit=20&sort=desc",
        headers=hdrs,
    )
    print(f"[*] GET /mailboxes/{{id}}/messages -> HTTP {code2}")
    print(json.dumps(list_resp, ensure_ascii=False, indent=2))

    if code2 != 200 or not isinstance(list_resp, dict):
        return 1

    messages = list_resp.get("data") or []
    if not messages or not isinstance(messages, list):
        print("\n[*] 当前无邮件；可往该地址发一封测试信后再次运行本脚本（需新邮箱可删 -- 复用需自行改脚本）。")
        return 0

    first = messages[0]
    msg_id = str(first.get("id") or "")
    if not msg_id:
        print("错误: 列表项缺少 id", file=sys.stderr)
        return 1

    code3, detail_resp = http_json(
        "GET",
        f"{api_base}/mailboxes/{mid}/messages/{msg_id}",
        headers=hdrs,
    )
    print(f"\n[*] GET /mailboxes/{{id}}/messages/{{messageId}} -> HTTP {code3}")
    print(json.dumps(detail_resp, ensure_ascii=False, indent=2))

    if code3 != 200:
        print("\n[!] 详情非 200 时，常见为服务端读后删信等内部错误；与官方文档「成功样例」不是同一路径。", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
