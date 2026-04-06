from __future__ import annotations

import json
import os
import re
import shutil
import signal
import sqlite3
import subprocess
import threading
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import requests
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field, model_validator

APP_DIR = Path(__file__).resolve().parent
REPO_ROOT = APP_DIR.parents[1]
RUNTIME_DIR = APP_DIR / "runtime"
WORKERS_DIR = RUNTIME_DIR / "workers"
DB_PATH = RUNTIME_DIR / "console.db"
TEMPLATES = Jinja2Templates(directory=str(APP_DIR / "templates"))

SOURCE_PROJECT = Path(os.getenv("GROK_REGISTER_SOURCE_DIR", str(REPO_ROOT))).resolve()
SOURCE_VENV_PYTHON = Path(
    os.getenv("GROK_REGISTER_PYTHON", str(SOURCE_PROJECT / ".venv" / "bin" / "python"))
).expanduser()
MAX_CONCURRENT_TASKS = max(1, int(os.getenv("GROK_REGISTER_CONSOLE_MAX_CONCURRENT_TASKS", "1")))
SUPERVISOR_INTERVAL = max(1.0, float(os.getenv("GROK_REGISTER_CONSOLE_POLL_INTERVAL", "2")))

PROJECT_FILES = ("DrissionPage_example.py", "email_register.py")
PROJECT_DIRS = ("turnstilePatch",)

STATUS_IDLE = "idle"
STATUS_MANUAL_RUNNING = "manual_running"
STATUS_AUTO_IDLE = "auto_idle"
STATUS_AUTO_RUNNING = "auto_running"
STATUS_STOPPING = "stopping"
STATUS_ERROR = "error"

db_lock = threading.RLock()


def now_iso() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def ensure_dirs() -> None:
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    WORKERS_DIR.mkdir(parents=True, exist_ok=True)


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def fetch_one(query: str, params: tuple[Any, ...] = ()) -> sqlite3.Row | None:
    with db_lock, get_conn() as conn:
        return conn.execute(query, params).fetchone()


def execute_no_return(query: str, params: tuple[Any, ...] = ()) -> None:
    with db_lock, get_conn() as conn:
        conn.execute(query, params)
        conn.commit()


def init_db() -> None:
    ensure_dirs()
    with db_lock, get_conn() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            """
        )


def load_source_defaults() -> dict[str, Any]:
    config_path = SOURCE_PROJECT / "config.json"
    if config_path.exists():
        base = json.loads(config_path.read_text(encoding="utf-8"))
    else:
        example_path = SOURCE_PROJECT / "config.example.json"
        if example_path.exists():
            base = json.loads(example_path.read_text(encoding="utf-8"))
        else:
            base = {
                "run": {"count": 50},
                "proxy": "",
                "browser_proxy": "",
                "temp_mail_api_base": "",
                "temp_mail_admin_password": "",
                "temp_mail_domain": "",
                "temp_mail_site_password": "",
                "api": {"endpoint": "", "token": "", "append": True},
                "controller": {
                    "concurrency": 1,
                    "auto_refill_enabled": False,
                    "single_batch_count": 10,
                    "start_threshold": 20,
                    "stop_threshold": 50,
                    "push_batch_size": 10,
                    "poll_interval_sec": 30,
                },
            }

    env_count = os.getenv("GROK_REGISTER_DEFAULT_RUN_COUNT", "").strip()
    if env_count:
        try:
            base.setdefault("run", {})["count"] = max(1, int(env_count))
        except ValueError:
            pass

    env_map = {
        "proxy": "GROK_REGISTER_DEFAULT_PROXY",
        "browser_proxy": "GROK_REGISTER_DEFAULT_BROWSER_PROXY",
        "temp_mail_api_base": "GROK_REGISTER_DEFAULT_TEMP_MAIL_API_BASE",
        "temp_mail_admin_password": "GROK_REGISTER_DEFAULT_TEMP_MAIL_ADMIN_PASSWORD",
        "temp_mail_domain": "GROK_REGISTER_DEFAULT_TEMP_MAIL_DOMAIN",
        "temp_mail_site_password": "GROK_REGISTER_DEFAULT_TEMP_MAIL_SITE_PASSWORD",
    }
    for key, env_name in env_map.items():
        value = os.getenv(env_name)
        if value is not None:
            base[key] = value

    api_base = dict(base.get("api") or {})
    api_env_map = {
        "endpoint": "GROK_REGISTER_DEFAULT_API_ENDPOINT",
        "token": "GROK_REGISTER_DEFAULT_API_TOKEN",
    }
    for key, env_name in api_env_map.items():
        value = os.getenv(env_name)
        if value is not None:
            api_base[key] = value
    append_env = os.getenv("GROK_REGISTER_DEFAULT_API_APPEND")
    if append_env is not None:
        api_base["append"] = append_env.strip().lower() in {"1", "true", "yes", "on"}
    base["api"] = api_base

    controller = dict(base.get("controller") or {})
    controller.setdefault("concurrency", 1)
    controller.setdefault("auto_refill_enabled", False)
    controller.setdefault("single_batch_count", 10)
    controller.setdefault("start_threshold", 20)
    controller.setdefault("stop_threshold", 50)
    controller.setdefault("push_batch_size", 10)
    controller.setdefault("poll_interval_sec", 30)
    base["controller"] = controller
    return base


def _mask_proxy(proxy_url: str) -> str:
    parsed = urlparse(proxy_url)
    if not parsed.scheme or not parsed.netloc:
        return proxy_url
    host = parsed.hostname or ""
    port = f":{parsed.port}" if parsed.port else ""
    return f"{parsed.scheme}://{host}{port}"


def _request_with_optional_proxy(
    url: str,
    proxy_url: str = "",
    method: str = "GET",
    timeout: int = 15,
    headers: dict[str, str] | None = None,
) -> requests.Response:
    proxies = None
    if proxy_url:
        proxies = {"http": proxy_url, "https": proxy_url}
    return requests.request(
        method,
        url,
        timeout=timeout,
        headers=headers,
        proxies=proxies,
        allow_redirects=True,
        verify=False,
    )


def _build_health_item(key: str, label: str, ok: bool, summary: str, detail: str, target: str) -> dict[str, Any]:
    return {
        "key": key,
        "label": label,
        "ok": ok,
        "summary": summary,
        "detail": detail,
        "target": target,
        "checked_at": now_iso(),
    }


def run_health_checks() -> dict[str, Any]:
    defaults = merged_defaults()
    items: list[dict[str, Any]] = []

    browser_proxy = str(defaults.get("browser_proxy", "") or "").strip()
    request_proxy = str(defaults.get("proxy", "") or "").strip()
    api_conf = dict(defaults.get("api") or {})
    api_endpoint = str(api_conf.get("endpoint", "") or "").strip()
    temp_mail_api_base = str(defaults.get("temp_mail_api_base", "") or "").strip()

    warp_target = browser_proxy or request_proxy
    if not warp_target:
        items.append(_build_health_item("warp", "WARP / Proxy", False, "未配置代理出口", "当前系统默认配置里没有 `browser_proxy` 或 `proxy`，无法检查前置网络出口。", "-"))
    else:
        try:
            response = _request_with_optional_proxy("https://www.cloudflare.com/cdn-cgi/trace", proxy_url=warp_target, timeout=20)
            body = response.text
            ip_match = re.search(r"(?m)^ip=(.+)$", body)
            loc_match = re.search(r"(?m)^loc=(.+)$", body)
            warp_match = re.search(r"(?m)^warp=(.+)$", body)
            ip = ip_match.group(1).strip() if ip_match else "unknown"
            loc = loc_match.group(1).strip() if loc_match else "unknown"
            warp_state = warp_match.group(1).strip() if warp_match else "unknown"
            items.append(_build_health_item("warp", "WARP / Proxy", response.status_code == 200, f"HTTP {response.status_code} | IP {ip} | LOC {loc}", f"通过代理 `{_mask_proxy(warp_target)}` 访问 Cloudflare trace 成功，warp={warp_state}。", _mask_proxy(warp_target)))
        except Exception as exc:
            items.append(_build_health_item("warp", "WARP / Proxy", False, "代理出口不可达", f"通过 `{_mask_proxy(warp_target)}` 访问 Cloudflare trace 失败：{exc}", _mask_proxy(warp_target)))

    if not api_endpoint:
        items.append(_build_health_item("grok2api", "grok2api Sink", False, "未配置 token sink", "当前系统默认配置里没有 `api.endpoint`，注册成功后不会自动入池。", "-"))
    else:
        try:
            response = _request_with_optional_proxy(api_endpoint, timeout=15)
            ok = response.status_code in {200, 401, 403, 405}
            items.append(_build_health_item("grok2api", "grok2api Sink", ok, f"HTTP {response.status_code}", "接口已可达。即使返回 401/403，也说明服务本身在线，只是需要正确的管理口令。", api_endpoint))
        except Exception as exc:
            items.append(_build_health_item("grok2api", "grok2api Sink", False, "接口不可达", f"访问 `{api_endpoint}` 失败：{exc}", api_endpoint))

    if not temp_mail_api_base:
        items.append(_build_health_item("temp_mail", "Temp Mail API", False, "未配置临时邮箱 API", "当前系统默认配置里没有 `temp_mail_api_base`，注册流程会在创建邮箱阶段直接失败。", "-"))
    else:
        try:
            response = _request_with_optional_proxy(temp_mail_api_base, proxy_url=request_proxy, timeout=15)
            items.append(_build_health_item("temp_mail", "Temp Mail API", response.status_code < 500, f"HTTP {response.status_code}", "接口地址可达。这里只做基础连通性检查，不会真的创建邮箱地址。", temp_mail_api_base))
        except Exception as exc:
            items.append(_build_health_item("temp_mail", "Temp Mail API", False, "接口不可达", f"访问 `{temp_mail_api_base}` 失败：{exc}", temp_mail_api_base))

    xai_proxy = browser_proxy or request_proxy
    try:
        response = _request_with_optional_proxy("https://accounts.x.ai/sign-up?redirect=grok-com", proxy_url=xai_proxy, timeout=20, headers={"User-Agent": "Mozilla/5.0"})
        ok = response.status_code in {200, 301, 302, 303, 307, 308}
        detail = f"使用 `{_mask_proxy(xai_proxy)}` 访问注册页返回 HTTP {response.status_code}。" if xai_proxy else f"直连访问注册页返回 HTTP {response.status_code}。"
        if not ok and response.status_code in {401, 403, 429}:
            detail += " 这通常说明当前出口被目标站点拦截、限流，或还没完成可用的人机验证链路。"
        items.append(_build_health_item("xai", "x.ai Sign-up", ok, f"HTTP {response.status_code}", detail, "https://accounts.x.ai/sign-up?redirect=grok-com"))
    except Exception as exc:
        items.append(_build_health_item("xai", "x.ai Sign-up", False, "注册页不可达", f"访问 `x.ai` 注册页失败：{exc}", "https://accounts.x.ai/sign-up?redirect=grok-com"))

    return {"items": items, "checked_at": now_iso()}


class SystemSettings(BaseModel):
    proxy: str = ""
    browser_proxy: str = ""
    temp_mail_api_base: str = ""
    temp_mail_admin_password: str = ""
    temp_mail_domain: str = ""
    temp_mail_site_password: str = ""
    api_endpoint: str = ""
    api_token: str = ""
    api_append: bool = True
    push_batch_size: int = Field(10, ge=1, le=10000)
    poll_interval_sec: int = Field(30, ge=5, le=3600)


class ControllerSettingsPayload(BaseModel):
    concurrency: int = Field(1, ge=1, le=32)
    auto_refill_enabled: bool = False
    single_batch_count: int = Field(10, ge=1, le=100000)
    start_threshold: int = Field(20, ge=0, le=1000000)
    stop_threshold: int = Field(50, ge=1, le=1000000)

    @model_validator(mode="after")
    def validate_thresholds(self) -> "ControllerSettingsPayload":
        if self.auto_refill_enabled and self.start_threshold >= self.stop_threshold:
            raise ValueError("启动阈值必须小于停止阈值")
        return self


@dataclass
class WorkerProcess:
    slot: int
    process: subprocess.Popen[Any]
    task_dir: Path
    console_path: Path
    result_path: Path
    output_path: Path
    started_at: str
    log_handle: Any


@dataclass
class ControllerRuntime:
    status: str = STATUS_IDLE
    controller_enabled: bool = False
    desired_running: bool = False
    manual_mode: bool = False
    stop_requested: bool = False
    completed_count: int = 0
    failed_count: int = 0
    current_round: int = 0
    current_phase: str = "idle"
    current_running_workers: int = 0
    remote_token_count: int = 0
    pending_token_count: int = 0
    last_email: str = ""
    last_error: str = ""
    last_check_at: str = ""
    last_push_at: str = ""
    last_push_result: str = ""
    last_started_at: str = ""
    last_stopped_at: str = ""
    last_worker_started_at: str = ""
    loop_errors: int = 0
    total_pushed_count: int = 0
    worker_seq: int = 0
    pending_tokens: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "controller_enabled": self.controller_enabled,
            "desired_running": self.desired_running,
            "manual_mode": self.manual_mode,
            "stop_requested": self.stop_requested,
            "completed_count": self.completed_count,
            "failed_count": self.failed_count,
            "current_round": self.current_round,
            "current_phase": self.current_phase,
            "current_running_workers": self.current_running_workers,
            "remote_token_count": self.remote_token_count,
            "pending_token_count": len(self.pending_tokens),
            "last_email": self.last_email,
            "last_error": self.last_error,
            "last_check_at": self.last_check_at,
            "last_push_at": self.last_push_at,
            "last_push_result": self.last_push_result,
            "last_started_at": self.last_started_at,
            "last_stopped_at": self.last_stopped_at,
            "last_worker_started_at": self.last_worker_started_at,
            "loop_errors": self.loop_errors,
            "total_pushed_count": self.total_pushed_count,
            "worker_seq": self.worker_seq,
        }


def read_settings() -> dict[str, Any]:
    row = fetch_one("SELECT value FROM settings WHERE key = ?", ("system",))
    if not row:
        return {}
    try:
        data = json.loads(row["value"])
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def write_settings_data(data: dict[str, Any]) -> dict[str, Any]:
    execute_no_return(
        """
        INSERT INTO settings (key, value, updated_at)
        VALUES (?, ?, ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
        """,
        ("system", json.dumps(data, ensure_ascii=False), now_iso()),
    )
    return data


def write_settings(settings: SystemSettings) -> dict[str, Any]:
    current = read_settings()
    data = dict(current)
    data.update(settings.model_dump())
    return write_settings_data(data)


def write_controller_settings(payload: ControllerSettingsPayload) -> dict[str, Any]:
    current = read_settings()
    data = dict(current)
    data.update(payload.model_dump())
    return write_settings_data(data)


def merged_defaults() -> dict[str, Any]:
    base = load_source_defaults()
    saved = read_settings()
    if saved.get("proxy") is not None:
        base["proxy"] = str(saved.get("proxy", ""))
    if saved.get("browser_proxy") is not None:
        base["browser_proxy"] = str(saved.get("browser_proxy", ""))
    for key in ("temp_mail_api_base", "temp_mail_admin_password", "temp_mail_domain", "temp_mail_site_password"):
        if key in saved:
            base[key] = str(saved.get(key, ""))
    api_base = dict(base.get("api") or {})
    if "api_endpoint" in saved:
        api_base["endpoint"] = str(saved.get("api_endpoint", ""))
    if "api_token" in saved:
        api_base["token"] = str(saved.get("api_token", ""))
    if "api_append" in saved:
        api_base["append"] = bool(saved.get("api_append", True))
    base["api"] = api_base

    controller = dict(base.get("controller") or {})
    for key in ("concurrency", "auto_refill_enabled", "single_batch_count", "start_threshold", "stop_threshold", "push_batch_size", "poll_interval_sec"):
        if key in saved:
            controller[key] = saved[key]
    controller["concurrency"] = max(1, min(int(controller.get("concurrency", 1)), MAX_CONCURRENT_TASKS))
    controller["single_batch_count"] = max(1, int(controller.get("single_batch_count", 10)))
    controller["push_batch_size"] = max(1, int(controller.get("push_batch_size", 10)))
    controller["poll_interval_sec"] = max(5, int(controller.get("poll_interval_sec", 30)))
    controller["start_threshold"] = max(0, int(controller.get("start_threshold", 20)))
    controller["stop_threshold"] = max(1, int(controller.get("stop_threshold", 50)))
    controller["auto_refill_enabled"] = bool(controller.get("auto_refill_enabled", False))
    base["controller"] = controller
    return base


def build_worker_config(settings: dict[str, Any]) -> dict[str, Any]:
    defaults = merged_defaults()
    api_defaults = dict(defaults.get("api") or {})
    return {
        "run": {"count": 1},
        "proxy": str(settings.get("proxy", defaults.get("proxy", "")) or ""),
        "browser_proxy": str(settings.get("browser_proxy", defaults.get("browser_proxy", "")) or ""),
        "temp_mail_api_base": str(settings.get("temp_mail_api_base", defaults.get("temp_mail_api_base", "")) or ""),
        "temp_mail_admin_password": str(settings.get("temp_mail_admin_password", defaults.get("temp_mail_admin_password", "")) or ""),
        "temp_mail_domain": str(settings.get("temp_mail_domain", defaults.get("temp_mail_domain", "")) or ""),
        "temp_mail_site_password": str(settings.get("temp_mail_site_password", defaults.get("temp_mail_site_password", "")) or ""),
        "api": {
            "endpoint": str(settings.get("api_endpoint", api_defaults.get("endpoint", "")) or ""),
            "token": str(settings.get("api_token", api_defaults.get("token", "")) or ""),
            "append": bool(settings.get("api_append", api_defaults.get("append", True))),
        },
    }


def build_api_conf(settings: dict[str, Any]) -> dict[str, Any]:
    defaults = merged_defaults()
    api_defaults = dict(defaults.get("api") or {})
    return {
        "endpoint": str(settings.get("api_endpoint", api_defaults.get("endpoint", "")) or "").strip(),
        "token": str(settings.get("api_token", api_defaults.get("token", "")) or "").strip(),
        "append": bool(settings.get("api_append", api_defaults.get("append", True))),
    }


def parse_tokens_payload(data: Any) -> list[str]:
    if not isinstance(data, dict):
        return []
    if isinstance(data.get("tokens"), dict):
        items = data["tokens"].get("ssoBasic", [])
    else:
        items = data.get("ssoBasic", [])
    tokens: list[str] = []
    if not isinstance(items, list):
        return tokens
    for item in items:
        token = item.get("token") if isinstance(item, dict) else str(item)
        token = str(token or "").strip()
        if token:
            tokens.append(token)
    return tokens


def fetch_remote_tokens(api_conf: dict[str, Any]) -> list[str]:
    endpoint = api_conf.get("endpoint", "")
    api_token = api_conf.get("token", "")
    if not endpoint or not api_token:
        raise RuntimeError("未配置 api.endpoint 或 api.token")
    response = requests.get(endpoint, headers={"Authorization": f"Bearer {api_token}"}, timeout=20, verify=False)
    if response.status_code != 200:
        raise RuntimeError(f"查询远端 token 失败: HTTP {response.status_code} {response.text[:200]}")
    return parse_tokens_payload(response.json())


def fetch_remote_sso_count(api_conf: dict[str, Any]) -> int:
    return len(fetch_remote_tokens(api_conf))


def push_sso_batch(api_conf: dict[str, Any], new_tokens: list[str]) -> tuple[int, int]:
    endpoint = api_conf.get("endpoint", "")
    api_token = api_conf.get("token", "")
    append_mode = bool(api_conf.get("append", True))
    if not endpoint or not api_token:
        raise RuntimeError("未配置 api.endpoint 或 api.token")
    tokens_to_push = [str(token).strip() for token in new_tokens if str(token).strip()]
    if not tokens_to_push:
        return 0, 0
    existing_count = 0
    if append_mode:
        existing = fetch_remote_tokens(api_conf)
        existing_count = len(existing)
        seen = set()
        merged: list[str] = []
        for token in existing + tokens_to_push:
            if token not in seen:
                seen.add(token)
                merged.append(token)
        tokens_to_push = merged
    response = requests.post(
        endpoint,
        json={"ssoBasic": tokens_to_push},
        headers={"Authorization": f"Bearer {api_token}", "Content-Type": "application/json"},
        timeout=60,
        verify=False,
    )
    if response.status_code != 200:
        raise RuntimeError(f"推送 token 失败: HTTP {response.status_code} {response.text[:200]}")
    return existing_count, len(tokens_to_push)


def read_log_lines(path: Path, limit: int = 200) -> list[str]:
    if not path.exists():
        return []
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    return lines[-limit:]


class SingleControllerSupervisor:
    def __init__(self) -> None:
        self._workers: dict[int, WorkerProcess] = {}
        self._runtime = ControllerRuntime()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._stop = threading.Event()
        self._lock = threading.RLock()
        self._push_lock = threading.RLock()
        self._last_poll = 0.0
        self._controller_log = RUNTIME_DIR / "controller.log"

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self.request_stop(flush_pending=False)

    def get_runtime(self) -> dict[str, Any]:
        with self._lock:
            self._runtime.current_running_workers = len(self._workers)
            return self._runtime.to_dict()

    def read_logs(self, limit: int = 300) -> list[str]:
        return read_log_lines(self._controller_log, limit=limit)

    def append_log(self, message: str) -> None:
        line = f"{now_iso()} | {message}"
        self._controller_log.parent.mkdir(parents=True, exist_ok=True)
        with self._controller_log.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")

    def request_start(self) -> None:
        with self._lock:
            self._runtime.controller_enabled = True
            self._runtime.desired_running = True
            self._runtime.manual_mode = False
            self._runtime.stop_requested = False
            self._runtime.completed_count = 0
            self._runtime.failed_count = 0
            self._runtime.status = STATUS_MANUAL_RUNNING
            self._runtime.current_phase = "controller_enabled"
            self._runtime.last_started_at = now_iso()
        self.append_log("[controller] 总开关已开启")

    def request_stop(self, flush_pending: bool = True) -> None:
        with self._lock:
            was_enabled = self._runtime.controller_enabled
            self._runtime.controller_enabled = False
            self._runtime.desired_running = False
            self._runtime.stop_requested = True
            self._runtime.manual_mode = False
            if self._workers:
                self._runtime.status = STATUS_STOPPING
                self._runtime.current_phase = "stopping_workers"
            else:
                self._runtime.status = STATUS_IDLE
                self._runtime.current_phase = "idle"
            self._runtime.last_stopped_at = now_iso()
        if was_enabled:
            self.append_log("[controller] 总开关已关闭")
        for slot, worker in list(self._workers.items()):
            try:
                os.killpg(worker.process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            except Exception as exc:
                self.append_log(f"[controller] 停止 worker-{slot} 失败: {exc}")
        if flush_pending:
            self._flush_pending(force=True)

    def trigger_remote_check(self) -> dict[str, Any]:
        self._check_remote(force=True)
        return self.get_runtime()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self._refresh_workers()
                self._check_remote(force=False)
                self._reconcile_mode()
                self._launch_workers_if_needed()
                self._flush_pending(force=False)
            except Exception as exc:
                with self._lock:
                    self._runtime.loop_errors += 1
                    self._runtime.status = STATUS_ERROR
                    self._runtime.current_phase = "loop_error"
                    self._runtime.last_error = str(exc)
                self.append_log(f"[controller] 主循环异常: {exc}")
            time.sleep(SUPERVISOR_INTERVAL)

    def _refresh_workers(self) -> None:
        finished_slots: list[int] = []
        for slot, worker in list(self._workers.items()):
            exit_code = worker.process.poll()
            if exit_code is None:
                continue
            result = self._read_worker_result(worker.result_path)
            with self._lock:
                self._runtime.current_running_workers = max(0, len(self._workers) - 1)
                self._runtime.current_round += 1
                self._runtime.last_email = result.get("email", self._runtime.last_email)
                self._runtime.last_worker_started_at = worker.started_at
                if exit_code == 0 and result.get("ok"):
                    token = str(result.get("sso", "")).strip()
                    if token:
                        self._runtime.pending_tokens.append(token)
                    self._runtime.completed_count += 1
                    self._runtime.current_phase = "worker_succeeded"
                    self.append_log(f"[worker-{slot}] 成功 email={result.get('email', '-')}")
                else:
                    self._runtime.failed_count += 1
                    self._runtime.current_phase = "worker_failed"
                    error = str(result.get("error", f"worker exited with {exit_code}"))
                    self._runtime.last_error = error
                    self.append_log(f"[worker-{slot}] 失败: {error}")
            finished_slots.append(slot)
            try:
                worker.log_handle.close()
            except Exception:
                pass
        for slot in finished_slots:
            self._workers.pop(slot, None)
        should_flush_pending = False
        with self._lock:
            self._runtime.current_running_workers = len(self._workers)
            self._runtime.pending_token_count = len(self._runtime.pending_tokens)
            if self._runtime.stop_requested and not self._workers:
                should_flush_pending = bool(self._runtime.pending_tokens)
        if should_flush_pending:
            self._flush_pending(force=True)
        with self._lock:
            self._runtime.current_running_workers = len(self._workers)
            self._runtime.pending_token_count = len(self._runtime.pending_tokens)
            if self._runtime.stop_requested and not self._workers:
                self._runtime.status = STATUS_IDLE
                self._runtime.current_phase = "idle"
                self._runtime.stop_requested = False

    def _read_worker_result(self, result_path: Path) -> dict[str, Any]:
        if not result_path.exists():
            return {"ok": False, "error": "worker result not found"}
        try:
            data = json.loads(result_path.read_text(encoding="utf-8"))
        except Exception as exc:
            return {"ok": False, "error": f"worker result invalid: {exc}"}
        return data if isinstance(data, dict) else {"ok": False, "error": "worker result invalid"}

    def _check_remote(self, force: bool) -> None:
        settings = read_settings()
        controller = merged_defaults().get("controller", {})
        interval = int(controller.get("poll_interval_sec", 30))
        now = time.time()
        if not force and now - self._last_poll < interval:
            return
        self._last_poll = now
        api_conf = build_api_conf(settings)
        if not api_conf.get("endpoint") or not api_conf.get("token"):
            with self._lock:
                self._runtime.last_check_at = now_iso()
                self._runtime.last_error = "未配置 api.endpoint 或 api.token，无法检测远端账号数量"
            return
        try:
            remote_count = fetch_remote_sso_count(api_conf)
            with self._lock:
                self._runtime.remote_token_count = remote_count
                self._runtime.last_check_at = now_iso()
                if self._runtime.current_phase not in {"worker_succeeded", "worker_failed", "stopping_workers"}:
                    self._runtime.current_phase = "remote_checked"
        except Exception as exc:
            with self._lock:
                self._runtime.last_check_at = now_iso()
                self._runtime.last_error = str(exc)
            self.append_log(f"[controller] 查询远端账号数失败: {exc}")

    def _reconcile_mode(self) -> None:
        settings = merged_defaults()
        controller = dict(settings.get("controller") or {})
        with self._lock:
            if not self._runtime.controller_enabled:
                self._runtime.desired_running = False
                if self._runtime.stop_requested and self._workers:
                    self._runtime.status = STATUS_STOPPING
                    self._runtime.current_phase = "stopping_workers"
                elif self._workers:
                    self._runtime.status = STATUS_STOPPING
                    self._runtime.current_phase = "stopping_workers"
                else:
                    self._runtime.status = STATUS_IDLE
                    self._runtime.current_phase = "idle"
                return
            if not controller.get("auto_refill_enabled"):
                batch_limit = int(controller.get("single_batch_count", 10))
                if self._runtime.completed_count >= batch_limit:
                    self._runtime.desired_running = False
                    if self._workers:
                        self._runtime.status = STATUS_STOPPING
                        self._runtime.current_phase = "stopping_workers"
                    else:
                        self._runtime.status = STATUS_IDLE
                        self._runtime.current_phase = "idle"
                        self._runtime.controller_enabled = False
                        self.append_log(f"[controller] 单次补号完成 ({self._runtime.completed_count}/{batch_limit})")
                else:
                    self._runtime.desired_running = True
                    self._runtime.status = STATUS_MANUAL_RUNNING
                    self._runtime.current_phase = "manual_running"
                return
            if self._runtime.remote_token_count < int(controller.get("start_threshold", 20)):
                self._runtime.desired_running = True
                self._runtime.status = STATUS_AUTO_RUNNING
                self._runtime.current_phase = "auto_refill_running"
            elif self._runtime.remote_token_count >= int(controller.get("stop_threshold", 50)):
                self._runtime.desired_running = False
                if self._workers:
                    self._runtime.status = STATUS_STOPPING
                    self._runtime.current_phase = "waiting_workers_stop"
                else:
                    self._runtime.status = STATUS_AUTO_IDLE
                    self._runtime.current_phase = "auto_refill_idle"
            elif self._workers:
                self._runtime.status = STATUS_AUTO_RUNNING
                self._runtime.current_phase = "auto_refill_running"
            else:
                self._runtime.status = STATUS_AUTO_IDLE
                self._runtime.current_phase = "auto_refill_waiting"

    def _launch_workers_if_needed(self) -> None:
        settings = read_settings()
        merged = merged_defaults()
        controller = dict(merged.get("controller") or {})
        if not self._runtime.desired_running:
            return
        if self._runtime.stop_requested:
            return
        slots = int(controller.get("concurrency", 1)) - len(self._workers)
        if slots <= 0:
            return
        if not SOURCE_PROJECT.exists():
            raise RuntimeError(f"Source project not found: {SOURCE_PROJECT}")
        if not SOURCE_VENV_PYTHON.exists():
            raise RuntimeError(f"Python not found: {SOURCE_VENV_PYTHON}")
        worker_config = build_worker_config(settings)
        for _ in range(slots):
            self._launch_single_worker(worker_config)

    def _launch_single_worker(self, worker_config: dict[str, Any]) -> None:
        with self._lock:
            self._runtime.worker_seq += 1
            slot = self._runtime.worker_seq
        task_dir = WORKERS_DIR / f"worker_{slot}"
        if task_dir.exists():
            shutil.rmtree(task_dir, ignore_errors=True)
        task_dir.mkdir(parents=True, exist_ok=True)
        for file_name in PROJECT_FILES:
            shutil.copy2(SOURCE_PROJECT / file_name, task_dir / file_name)
        for dir_name in PROJECT_DIRS:
            src = SOURCE_PROJECT / dir_name
            dst = task_dir / dir_name
            if dst.exists():
                shutil.rmtree(dst)
            shutil.copytree(src, dst)
        (task_dir / "logs").mkdir(exist_ok=True)
        (task_dir / "sso").mkdir(exist_ok=True)
        config_path = task_dir / "config.json"
        config_path.write_text(json.dumps(worker_config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        output_path = task_dir / "sso" / f"worker_{slot}.txt"
        result_path = task_dir / "worker_result.json"
        console_path = task_dir / "console.log"
        log_handle = console_path.open("a", encoding="utf-8")
        command = [
            str(SOURCE_VENV_PYTHON),
            str(task_dir / "DrissionPage_example.py"),
            "--count",
            "1",
            "--output",
            str(output_path),
            "--result-json",
            str(result_path),
            "--no-api-push",
            "--worker-name",
            f"worker-{slot}",
        ]
        process = subprocess.Popen(
            command,
            cwd=task_dir,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            text=True,
        )
        self._workers[slot] = WorkerProcess(
            slot=slot,
            process=process,
            task_dir=task_dir,
            console_path=console_path,
            result_path=result_path,
            output_path=output_path,
            started_at=now_iso(),
            log_handle=log_handle,
        )
        with self._lock:
            self._runtime.current_running_workers = len(self._workers)
            self._runtime.current_phase = "worker_started"
            self._runtime.last_worker_started_at = now_iso()
        self.append_log(f"[worker-{slot}] 已启动 pid={process.pid}")

    def _flush_pending(self, force: bool) -> None:
        settings = read_settings()
        merged = merged_defaults()
        controller = dict(merged.get("controller") or {})
        batch_size = int(controller.get("push_batch_size", 10))
        with self._lock:
            pending = list(self._runtime.pending_tokens)
        if not pending:
            return
        if not force and len(pending) < batch_size:
            return
        api_conf = build_api_conf(settings)
        if not api_conf.get("endpoint") or not api_conf.get("token"):
            return
        with self._push_lock:
            with self._lock:
                tokens = list(self._runtime.pending_tokens)
            if not tokens:
                return
            if not force and len(tokens) < batch_size:
                return
            if not force:
                tokens = tokens[:batch_size]
            try:
                existing_count, final_count = push_sso_batch(api_conf, tokens)
            except Exception as exc:
                with self._lock:
                    self._runtime.last_push_at = now_iso()
                    self._runtime.last_push_result = f"失败: {exc}"
                    self._runtime.last_error = str(exc)
                self.append_log(f"[controller] 批量推送失败: {exc}")
                return
            with self._lock:
                remaining = self._runtime.pending_tokens[len(tokens):]
                self._runtime.pending_tokens = remaining
                self._runtime.pending_token_count = len(remaining)
                self._runtime.last_push_at = now_iso()
                self._runtime.last_push_result = f"成功推送 {len(tokens)} 个，本次合并后远端共 {final_count} 个"
                self._runtime.remote_token_count = final_count if api_conf.get("append", True) else len(tokens)
                self._runtime.total_pushed_count += len(tokens)
            self.append_log(f"[controller] 批量推送成功: 新推 {len(tokens)} 个，原有 {existing_count} 个，推送后 {final_count} 个")


supervisor = SingleControllerSupervisor()


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    supervisor.start()
    try:
        yield
    finally:
        supervisor.stop()


app = FastAPI(title="Grok Register Console", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(APP_DIR / "static")), name="static")


def _page_context(request: Request, active_page: str) -> dict[str, Any]:
    return {
        "request": request,
        "defaults": json.dumps(merged_defaults(), ensure_ascii=False),
        "max_concurrent_tasks": MAX_CONCURRENT_TASKS,
        "source_project": str(SOURCE_PROJECT),
        "active_page": active_page,
    }


@app.get("/", response_class=HTMLResponse)
def index(request: Request) -> HTMLResponse:
    return TEMPLATES.TemplateResponse(request, "index.html", _page_context(request, "controller"))


@app.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request) -> HTMLResponse:
    return TEMPLATES.TemplateResponse(request, "settings.html", _page_context(request, "settings"))


@app.get("/api/meta")
def api_meta() -> dict[str, Any]:
    return {
        "defaults": merged_defaults(),
        "settings": read_settings(),
        "source_project": str(SOURCE_PROJECT),
        "python_path": str(SOURCE_VENV_PYTHON),
        "max_concurrent_tasks": MAX_CONCURRENT_TASKS,
    }


@app.get("/api/health")
def api_health() -> dict[str, Any]:
    return run_health_checks()


@app.get("/api/settings")
def get_settings() -> dict[str, Any]:
    return {"settings": read_settings(), "defaults": merged_defaults()}


@app.post("/api/settings")
def save_settings(payload: SystemSettings) -> dict[str, Any]:
    saved = write_settings(payload)
    return {"settings": saved, "defaults": merged_defaults()}


@app.post("/api/controller/settings")
def save_controller_settings(payload: ControllerSettingsPayload) -> dict[str, Any]:
    if payload.concurrency > MAX_CONCURRENT_TASKS:
        raise HTTPException(status_code=400, detail=f"并发数不能超过控制台上限 {MAX_CONCURRENT_TASKS}")
    saved = write_controller_settings(payload)
    return {"settings": saved, "defaults": merged_defaults(), "runtime": supervisor.get_runtime()}


@app.get("/api/controller")
def get_controller() -> dict[str, Any]:
    return {
        "settings": read_settings(),
        "defaults": merged_defaults(),
        "runtime": supervisor.get_runtime(),
    }


@app.get("/api/controller/logs")
def get_controller_logs(limit: int = Query(300, ge=20, le=2000)) -> dict[str, Any]:
    return {"lines": supervisor.read_logs(limit=limit)}


@app.post("/api/controller/start")
def start_controller() -> dict[str, Any]:
    supervisor.request_start()
    return {"ok": True, "runtime": supervisor.get_runtime()}


@app.post("/api/controller/stop")
def stop_controller() -> dict[str, Any]:
    supervisor.request_stop(flush_pending=True)
    return {"ok": True, "runtime": supervisor.get_runtime()}


@app.post("/api/controller/check")
def check_controller() -> dict[str, Any]:
    runtime = supervisor.trigger_remote_check()
    return {"ok": True, "runtime": runtime}


if __name__ == "__main__":
    import uvicorn

    host = os.getenv("GROK_REGISTER_CONSOLE_HOST", "127.0.0.1")
    port = int(os.getenv("GROK_REGISTER_CONSOLE_PORT", "18600"))
    uvicorn.run("app:app", host=host, port=port, reload=False)
