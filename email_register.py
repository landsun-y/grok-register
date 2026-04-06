
from __future__ import annotations

import json
import os
import random
import re
import string
import time
from email import policy
from email.parser import BytesParser
from html import unescape
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

try:
    from curl_cffi import requests as curl_requests
except ImportError:
    curl_requests = None

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# ============================================================
# 临时邮箱配置（从 config.json 加载）
# ============================================================

_config_path = Path(__file__).parent / "config.json"
_conf: Dict[str, Any] = {}
if _config_path.exists():
    with _config_path.open("r", encoding="utf-8") as _f:
        _conf = json.load(_f)

TEMP_MAIL_API_BASE = str(
    _conf.get("temp_mail_api_base")
    or _conf.get("duckmail_api_base")
    or ""
)
TEMP_MAIL_ADMIN_PASSWORD = str(
    _conf.get("temp_mail_admin_password")
    or _conf.get("duckmail_api_key")
    or _conf.get("duckmail_bearer")
    or ""
)
TEMP_MAIL_DOMAIN = str(_conf.get("temp_mail_domain") or _conf.get("duckmail_domain") or "")
TEMP_MAIL_SITE_PASSWORD = str(_conf.get("temp_mail_site_password", ""))
PROXY = str(_conf.get("proxy", ""))
TEMP_MAIL_PROVIDER = str(_conf.get("temp_mail_provider") or "").strip().lower()

_vmail_warn_at: Dict[str, float] = {}


def _vmail_warn_throttled(key: str, message: str, interval_sec: float = 12.0) -> None:
    """避免轮询时刷屏；同一 key 间隔内只打一条。"""
    now = time.time()
    last = _vmail_warn_at.get(key, 0.0)
    if now - last < interval_sec:
        return
    _vmail_warn_at[key] = now
    print(message)


def _vmail_debug() -> bool:
    return os.environ.get("VMAIL_DEBUG", "").strip().lower() in ("1", "true", "yes", "on")


# ============================================================
# 适配层：为 DrissionPage_example.py 提供简单接口
# ============================================================

_temp_email_cache: Dict[str, str] = {}


def get_email_and_token() -> Tuple[Optional[str], Optional[str]]:
    """
    创建临时邮箱并返回 (email, mail_token)。
    供 DrissionPage_example.py 调用。
    """
    email, _password, mail_token = create_temp_email()
    if email and mail_token:
        _temp_email_cache[email] = mail_token
        return email, mail_token
    return None, None


def get_oai_code(dev_token: str, email: str, timeout: int = 120) -> Optional[str]:
    """
    轮询收件箱获取 OTP 验证码。
    供 DrissionPage_example.py 调用。

    Returns:
        验证码字符串（去除连字符，如 "MM0SF3"）或 None
    """
    code = wait_for_verification_code(mail_token=dev_token, timeout=timeout)
    if code:
        code = code.replace("-", "")
    return code


# ============================================================
# 临时邮箱核心函数
# ============================================================


def _provider_label() -> str:
    return "VMAIL"


def _normalize_api_base(api_base: str) -> str:
    api_base = api_base.strip().rstrip("/")
    if api_base.endswith("/api-docs"):
        api_base = api_base[: -len("/api-docs")]
    if api_base.endswith("/api/v1"):
        return api_base
    if api_base.endswith("/api"):
        return f"{api_base}/v1"
    return f"{api_base}/api/v1"


def _build_vmail_headers() -> Dict[str, str]:
    if not TEMP_MAIL_ADMIN_PASSWORD:
        raise Exception("temp_mail_admin_password 未设置，无法访问 VMAIL")
    return _build_headers({"X-API-Key": TEMP_MAIL_ADMIN_PASSWORD})


def _configured_domains() -> List[str]:
    return [part.strip() for part in TEMP_MAIL_DOMAIN.split(",") if part.strip()]


def _choose_vmail_domain() -> str:
    domains = _configured_domains()
    if not domains:
        return ""
    return random.choice(domains)

def _create_session():
    """创建请求会话（优先 curl_cffi）。"""
    if curl_requests:
        session = curl_requests.Session()
        session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Accept": "application/json",
            "Content-Type": "application/json",
        })
        if PROXY:
            session.proxies = {"http": PROXY, "https": PROXY}
        return session, True

    s = requests.Session()
    retry = Retry(total=3, backoff_factor=1, status_forcelist=[429, 500, 502, 503, 504])
    adapter = HTTPAdapter(max_retries=retry)
    s.mount("https://", adapter)
    s.mount("http://", adapter)
    s.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept": "application/json",
        "Content-Type": "application/json",
    })
    if PROXY:
        s.proxies = {"http": PROXY, "https": PROXY}
    return s, False


def _do_request(session, use_cffi, method, url, **kwargs):
    """统一请求，curl_cffi 自动附带 impersonate。"""
    if use_cffi:
        kwargs.setdefault("impersonate", "chrome131")
    return getattr(session, method)(url, **kwargs)


def _build_headers(extra: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    headers: Dict[str, str] = {}
    if TEMP_MAIL_SITE_PASSWORD:
        headers["x-custom-auth"] = TEMP_MAIL_SITE_PASSWORD
    if extra:
        headers.update(extra)
    return headers


def _generate_local_part(length: int = 10) -> str:
    chars = string.ascii_lowercase + string.digits
    return "".join(random.choice(chars) for _ in range(length))


def _generate_mail_password(length: int = 18) -> str:
    chars = string.ascii_letters + string.digits
    return "".join(random.choice(chars) for _ in range(length))


def create_temp_email() -> Tuple[str, str, str]:
    """创建临时邮箱地址，返回 (email, password, mail_token)。"""
    if not TEMP_MAIL_API_BASE:
        raise Exception("temp_mail_api_base 未设置，无法创建临时邮箱")

    api_base = _normalize_api_base(TEMP_MAIL_API_BASE)
    session, use_cffi = _create_session()
    headers = _build_vmail_headers()
    payload: Dict[str, Any] = {"expiresIn": 86400}
    domain = _choose_vmail_domain()
    if domain:
        payload["domain"] = domain

    res = _do_request(
        session,
        use_cffi,
        "post",
        f"{api_base}/mailboxes",
        json=payload,
        headers=headers,
        timeout=20,
    )
    if res.status_code not in {200, 201}:
        raise Exception(f"VMAIL 创建邮箱失败: {res.status_code} - {res.text[:200]}")

    payload = res.json()
    if not isinstance(payload, dict):
        raise Exception("VMAIL 创建邮箱返回格式异常")

    data = payload.get("data") or {}
    email = str(data.get("address") or "")
    mailbox_id = str(data.get("id") or "")
    if not email or not mailbox_id:
        raise Exception(f"VMAIL 创建邮箱缺少 address/id: {payload}")

    print(f"[*] VMAIL 临时邮箱创建成功: {email}")
    return email, "", mailbox_id


def fetch_emails(mail_token: str) -> List[Dict[str, Any]]:
    """获取邮件列表。"""
    try:
        api_base = _normalize_api_base(TEMP_MAIL_API_BASE)
        headers = _build_vmail_headers()
        session, use_cffi = _create_session()
        res = _do_request(
            session,
            use_cffi,
            "get",
            f"{api_base}/mailboxes/{mail_token}/messages",
            params={"page": 1, "limit": 20, "sort": "desc"},
            headers=headers,
            timeout=20,
        )
        if res.status_code == 200:
            data = res.json()
            if isinstance(data, dict):
                items = data.get("data") or []
                if isinstance(items, list):
                    return items
        snippet = (res.text or "")[:400].replace("\n", " ")
        _vmail_warn_throttled(
            "list_http",
            f"[Debug] VMAIL 邮件列表 HTTP {res.status_code}: {snippet}",
        )
    except Exception as exc:
        detail = repr(exc) if _vmail_debug() else str(exc)
        _vmail_warn_throttled("list_exc", f"[Debug] VMAIL 拉取邮件列表异常: {detail}")
    return []


def _normalize_message_id(msg_id: Any) -> str:
    return str(msg_id or "").strip()


def fetch_email_detail(mail_token: str, msg_id: str) -> Optional[Dict[str, Any]]:
    """获取单封邮件详情。"""
    try:
        api_base = _normalize_api_base(TEMP_MAIL_API_BASE)
        headers = _build_vmail_headers()
        session, use_cffi = _create_session()
        res = _do_request(
            session,
            use_cffi,
            "get",
            f"{api_base}/mailboxes/{mail_token}/messages/{_normalize_message_id(msg_id)}",
            headers=headers,
            timeout=20,
        )
        if res.status_code == 200:
            data = res.json()
            if isinstance(data, dict):
                detail = data.get("data") or {}
                if isinstance(detail, dict):
                    return detail
        snippet = (res.text or "")[:400].replace("\n", " ")
        _vmail_warn_throttled(
            f"detail_http_{msg_id}",
            f"[Debug] VMAIL 邮件详情 HTTP {res.status_code} (id={msg_id}): {snippet}",
        )
    except Exception as exc:
        detail = repr(exc) if _vmail_debug() else str(exc)
        _vmail_warn_throttled(f"detail_exc_{msg_id}", f"[Debug] VMAIL 拉取邮件详情异常 (id={msg_id}): {detail}")
    return None


def _list_message_text(msg: Dict[str, Any]) -> str:
    """列表接口常带 subject / preview；不拉详情也可能含验证码（绕过详情 500 等故障）。"""
    parts: List[str] = []
    sub = msg.get("subject")
    if sub:
        parts.append(_stringify_mail_part(sub))
    prev = msg.get("preview") or msg.get("snippet") or msg.get("textPreview")
    if prev:
        parts.append(_stringify_mail_part(prev))
    return "\n".join(p for p in parts if p)


def wait_for_verification_code(mail_token: str, timeout: int = 120) -> Optional[str]:
    """轮询临时邮箱，等待验证码邮件。"""
    start = time.time()
    seen_ids = set()
    last_status_log = start

    while time.time() - start < timeout:
        messages = fetch_emails(mail_token)
        if time.time() - last_status_log >= 30:
            elapsed = int(time.time() - start)
            print(f"[*] VMAIL 轮询收件箱… 已等待 {elapsed}s，当前列表 {len(messages)} 封")
            last_status_log = time.time()
        for msg in messages:
            if not isinstance(msg, dict):
                continue
            msg_id = msg.get("id") or msg.get("messageId") or msg.get("uuid")
            if not msg_id or msg_id in seen_ids:
                continue

            list_text = _list_message_text(msg)
            code = extract_verification_code(list_text)
            if code:
                print(f"[*] 从 {_provider_label()} 列表字段提取到验证码: {code}")
                return code

            detail = fetch_email_detail(mail_token, str(msg_id))
            if not detail:
                # 详情失败（如对方读后删信 500）时不要标记已处理，下次轮询可重试或改走列表
                continue

            content = _extract_mail_content(detail)
            code = extract_verification_code(content)
            if code:
                print(f"[*] 从 {_provider_label()} 提取到验证码: {code}")
                return code
            # 已拉到正文仍无验证码，当作非 OTP 邮件跳过
            seen_ids.add(msg_id)
        time.sleep(3)
    return None


def _stringify_mail_part(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, list):
        parts = [_stringify_mail_part(item) for item in value]
        return "\n".join(part for part in parts if part)
    if isinstance(value, dict):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def _extract_mail_content(detail: Dict[str, Any]) -> str:
    """兼容 text/html/raw MIME 三种内容来源。"""
    direct_parts = [
        detail.get("subject"),
        detail.get("text"),
        detail.get("html"),
        detail.get("raw"),
        detail.get("source"),
    ]
    direct_content = "\n".join(_stringify_mail_part(part) for part in direct_parts if part)
    if detail.get("text") or detail.get("html"):
        return direct_content

    raw = detail.get("raw") or detail.get("source")
    if not raw or not isinstance(raw, str):
        return direct_content
    return f"{direct_content}\n{_parse_raw_email(raw)}"


def _parse_raw_email(raw: str) -> str:
    try:
        message = BytesParser(policy=policy.default).parsebytes(raw.encode("utf-8", errors="ignore"))
    except Exception:
        return raw

    parts: List[str] = []
    subject = message.get("subject")
    if subject:
        parts.append(f"Subject: {subject}")

    if message.is_multipart():
        for part in message.walk():
            if part.get_content_maintype() == "multipart":
                continue
            disposition = (part.get_content_disposition() or "").lower()
            if disposition == "attachment":
                continue
            content = _decode_email_part(part)
            if content:
                parts.append(content)
    else:
        content = _decode_email_part(message)
        if content:
            parts.append(content)
    return "\n".join(parts)


def _decode_email_part(part) -> str:
    try:
        content = part.get_content()
        if isinstance(content, bytes):
            charset = part.get_content_charset() or "utf-8"
            content = content.decode(charset, errors="ignore")
        if not isinstance(content, str):
            content = str(content)
        if "html" in (part.get_content_type() or "").lower():
            content = _html_to_text(content)
        return content.strip()
    except Exception:
        payload = part.get_payload(decode=True)
        if isinstance(payload, bytes):
            charset = part.get_content_charset() or "utf-8"
            return payload.decode(charset, errors="ignore").strip()
    return ""


def _html_to_text(html: str) -> str:
    text = re.sub(r"(?is)<(script|style).*?>.*?</\1>", " ", html)
    text = re.sub(r"(?i)<br\s*/?>", "\n", text)
    text = re.sub(r"(?i)</p>", "\n", text)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    return unescape(re.sub(r"[ \t\r\f\v]+", " ", text)).strip()


def extract_verification_code(content: str) -> Optional[str]:
    """
    从邮件内容提取验证码。
    Grok/x.ai 格式：MM0-SF3（3位-3位字母数字混合）或 6 位纯数字。
    """
    if not content:
        return None

    # 模式 1: Grok 格式 XXX-XXX
    m = re.search(r"(?<![A-Z0-9-])([A-Z0-9]{3}-[A-Z0-9]{3})(?![A-Z0-9-])", content)
    if m:
        return m.group(1)

    # 模式 2: 带标签的验证码
    m = re.search(r"(?:verification code|验证码|your code)[:\s]*[<>\s]*([A-Z0-9]{3}-[A-Z0-9]{3})\b", content, re.IGNORECASE)
    if m:
        return m.group(1)

    # 模式 3: HTML 样式包裹
    m = re.search(r"background-color:\s*#F3F3F3[^>]*>[\s\S]*?([A-Z0-9]{3}-[A-Z0-9]{3})[\s\S]*?</p>", content)
    if m:
        return m.group(1)

    # 模式 4: Subject 行 6 位数字
    m = re.search(r"Subject:.*?(\d{6})", content)
    if m and m.group(1) != "177010":
        return m.group(1)

    # 模式 5: HTML 标签内 6 位数字
    for code in re.findall(r">\s*(\d{6})\s*<", content):
        if code != "177010":
            return code

    # 模式 6: 独立 6 位数字
    for code in re.findall(r"(?<![&#\d])(\d{6})(?![&#\d])", content):
        if code != "177010":
            return code

    return None
