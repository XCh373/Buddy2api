"""
auth_manager.py — 多账号凭据管理

功能：
  - 从本机 auth 文件扫描导入账号
  - 手动添加账号（粘贴 auth JSON）
  - Token 自动刷新（提前 60s 判定过期）
  - 账号粘性路由（优先级优先，同级尽量固定账号）
  - 凭据缓存与线程安全
"""

import asyncio
import json
import math
import os
import sqlite3
import sys
import threading
import time
import weakref
from datetime import datetime
from pathlib import Path
from typing import Optional

import httpx

import database as db
import fingerprint
import account_routing

try:
    import wb_atrest
except ImportError:  # pragma: no cover - plaintext snapshots remain supported
    wb_atrest = None

BACKEND = "https://copilot.tencent.com"
DEFAULT_DOMAIN = "www.codebuddy.cn"
_INTL_HOST_SUFFIX = ".workbuddy.ai"
_INTL_CANONICAL_HOST = "www.workbuddy.ai"

# 官方 Work Buddy / CodeBuddy CLI 客户端指纹（见 fingerprint.py）。
# 兼容保留 CB_GATEWAY_USER_AGENT 覆盖；如上游不接受新 UA，
# 设 CB_GATEWAY_USER_AGENT=codebuddy2openai/2.0 可回退到历史 UA。
USER_AGENT = fingerprint.user_agent()

_lock = threading.Lock()
_token_locks: dict[int, asyncio.Lock] = {}
_token_locks_guard = threading.Lock()
_route_lock = threading.Lock()
_sticky_account_id: dict[str, int] = {}
_failure_lock = threading.Lock()
_account_failures: dict[int, tuple[int, float]] = {}
_resource_locks = weakref.WeakKeyDictionary()


def _get_token_lock(aid: int) -> asyncio.Lock:
    with _token_locks_guard:
        if aid not in _token_locks:
            _token_locks[aid] = asyncio.Lock()
        return _token_locks[aid]


def _host_from(value: str) -> str:
    text = str(value or "").strip().lower().rstrip("/")
    if "://" in text:
        text = text.split("://", 1)[1]
    return text.split("/")[0].split(":")[0]


def _is_intl_workbuddy_host(host: str) -> bool:
    return host == "workbuddy.ai" or host.endswith(_INTL_HOST_SUFFIX)


def backend_url(account: Optional[dict] = None) -> str:
    """无账号时走设置项；国际版账号（*.workbuddy.ai）改打自己的站。"""
    value = str(db.get_setting("backend_url", BACKEND) or BACKEND).strip().rstrip("/")
    if not value.startswith("https://"):
        value = BACKEND
    if not account:
        return value
    host = _host_from(account.get("domain") or "")
    if not _is_intl_workbuddy_host(host):
        return value
    if host == "workbuddy.ai":
        host = _INTL_CANONICAL_HOST
    return f"https://{host}"


def request_timeout(default: int) -> int:
    try:
        return max(5, min(600, int(db.get_setting("timeout", default))))
    except (TypeError, ValueError):
        return default


def mark_account_success(aid: int):
    with _failure_lock:
        _account_failures.pop(aid, None)


def mark_account_failure(aid: int, status_code: int = 0):
    with _failure_lock:
        count, _ = _account_failures.get(aid, (0, 0.0))
        count += 1
        base = 30 if status_code in {401, 403, 429} else 5
        cooldown = min(300, base * (2 ** min(count - 1, 4)))
        _account_failures[aid] = (count, time.monotonic() + cooldown)
    if status_code in {401, 403}:
        db.update_account(aid, {"status": "expired"})


def account_is_cooling_down(aid: int) -> bool:
    with _failure_lock:
        failure = _account_failures.get(aid)
        if not failure:
            return False
        if failure[1] <= time.monotonic():
            _account_failures.pop(aid, None)
            return False
        return True


# ============================================================
# Auth 文件扫描
# ============================================================

def _expand_auth_path(path: Optional[str]) -> Optional[Path]:
    if not path:
        return None
    value = str(path).strip().strip('"')
    if not value:
        return None
    return Path(os.path.expandvars(value)).expanduser()


def _running_in_container() -> bool:
    value = os.environ.get("CB_DOCKER", "").strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    return Path("/.dockerenv").exists()


def _dedupe_paths(paths: list[Path]) -> list[Path]:
    result = []
    seen = set()
    for p in paths:
        key = str(p.resolve(strict=False))
        if os.name == "nt":
            key = key.lower()
        if key in seen:
            continue
        seen.add(key)
        result.append(p)
    return result


def _mask_value(value: str, left: int = 6, right: int = 4) -> str:
    value = value or ""
    if not value:
        return ""
    if len(value) <= left + right:
        return value[:2] + "..." if len(value) > 2 else "***"
    return f"{value[:left]}...{value[-right:]}"


def candidate_auth_dirs(auth_dir: Optional[str] = None) -> list[Path]:
    """返回会被扫描的 auth 目录候选项，包括不存在的路径。"""
    custom = _expand_auth_path(auth_dir)
    if custom:
        return [custom.parent if custom.suffix.lower() == ".info" else custom]

    explicit = _expand_auth_path(os.environ.get("CB_AUTH_DIR"))
    if explicit:
        return [explicit.parent if explicit.suffix.lower() == ".info" else explicit]

    home = Path.home()
    plat = sys.platform
    dirs = []
    if _running_in_container():
        dirs.append(Path(os.environ.get("CB_CONTAINER_AUTH_DIR", "/auth")))
    if plat == "darwin":
        dirs.append(home / "Library" / "Application Support" / "CodeBuddyExtension" / "Data" / "Public" / "auth")
    if plat == "win32":
        local = Path(os.environ.get("LOCALAPPDATA", home / "AppData" / "Local"))
        dirs.append(local / "CodeBuddyExtension" / "Data" / "Public" / "auth")
    xdg = Path(os.environ.get("XDG_DATA_HOME", home / ".local" / "share"))
    dirs.append(xdg / "CodeBuddyExtension" / "Data" / "Public" / "auth")
    return _dedupe_paths(dirs)


def scan_auth_dirs(auth_dir: Optional[str] = None) -> list[Path]:
    """返回所有存在的 auth 目录路径。"""
    return [d for d in candidate_auth_dirs(auth_dir) if d.is_dir()]


def find_auth_files(auth_dir: Optional[str] = None) -> list[Path]:
    """扫描所有 auth 目录下的 *.info 文件。"""
    custom = _expand_auth_path(auth_dir)
    if custom and custom.is_file():
        return [custom] if custom.suffix.lower() == ".info" else []

    files = []
    for d in scan_auth_dirs(auth_dir):
        try:
            files.extend(sorted(d.glob("*.info")))
        except OSError:
            continue
    return _dedupe_paths(files)


def _safe_auth_file_meta(path: Path, existing_uids: set[str]) -> dict:
    meta = {
        "name": path.name,
        "path": str(path),
        "dir": str(path.parent),
        "size": 0,
        "mtime": None,
        "valid": False,
        "reason": "",
        "account_name": "",
        "uid_masked": "",
        "domain": "",
        "expires_at": 0,
        "already_imported": False,
    }
    try:
        st = path.stat()
        meta["size"] = st.st_size
        meta["mtime"] = int(st.st_mtime)
    except OSError as e:
        meta["reason"] = f"无法读取文件: {e}"
        return meta

    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except json.JSONDecodeError:
        meta["reason"] = "不是有效 JSON"
        return meta
    except OSError as e:
        meta["reason"] = f"无法读取文件: {e}"
        return meta

    account = data.get("account", {}) if isinstance(data, dict) else {}
    auth = data.get("auth", {}) if isinstance(data, dict) else {}
    token = _decrypt_field(auth.get("accessToken"))
    if not token:
        meta["reason"] = "未发现 accessToken 或解密失败"
        return meta

    uid = _decrypt_field(account.get("uid"))
    nickname = _decrypt_field(account.get("nickname"))
    meta.update({
        "valid": True,
        "reason": "ok",
        "account_name": nickname or path.stem,
        "uid_masked": _mask_value(uid),
        "domain": _decrypt_field(auth.get("domain")) or DEFAULT_DOMAIN,
        "expires_at": auth.get("expiresAt", 0) if isinstance(auth.get("expiresAt"), (int, float)) else 0,
        "already_imported": bool(uid and uid in existing_uids),
    })
    return meta


def _decrypt_field(value) -> str:
    """Return plaintext WorkBuddy values, decrypting 5.6+ envelope fields."""
    if isinstance(value, str):
        return value
    if wb_atrest is not None and wb_atrest.is_encrypted_field(value):
        try:
            return wb_atrest.decrypt_auth_field(value)
        except Exception:
            return ""
    return ""


def discover_auth_files(auth_dir: Optional[str] = None) -> dict:
    """返回本机 auth 文件的安全元信息，不返回任何 token 内容。"""
    candidates = candidate_auth_dirs(auth_dir)
    existing_dirs = [d for d in candidates if d.is_dir()]
    visible_dirs = existing_dirs or candidates
    in_container = _running_in_container()
    auth_mount = Path(os.environ.get("CB_CONTAINER_AUTH_DIR", "/auth"))

    dirs = []
    for d in visible_dirs:
        info_files = []
        exists = d.is_dir()
        if exists:
            try:
                info_files = sorted(d.glob("*.info"))
            except OSError:
                info_files = []
        dirs.append({
            "path": str(d),
            "exists": exists,
            "file_count": len(info_files),
        })

    existing_uids = {a.get("uid", "") for a in db.list_accounts() if a.get("uid")}
    files = [_safe_auth_file_meta(f, existing_uids) for f in find_auth_files(auth_dir)]
    return {
        "dirs": dirs,
        "files": files,
        "file_count": len(files),
        "valid_count": sum(1 for f in files if f.get("valid")),
        "importable_count": sum(
            1 for f in files if f.get("valid") and not f.get("already_imported")
        ),
        "runtime": {
            "container": in_container,
            "auth_mount": str(auth_mount),
            "auth_mount_exists": auth_mount.is_dir() if in_container else False,
        },
    }


def parse_auth_file(path: Path) -> Optional[dict]:
    """解析 auth 文件，返回结构化凭据。"""
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return None

    account = data.get("account", {})
    auth = data.get("auth", {})
    token = _decrypt_field(auth.get("accessToken"))
    if not token:
        return None

    return {
        "name": _decrypt_field(account.get("nickname")) or path.stem,
        "uid": _decrypt_field(account.get("uid")),
        "nickname": _decrypt_field(account.get("nickname")),
        "phone": _decrypt_field(account.get("phoneNumber")),
        "account_type": account.get("type", "personal"),
        "access_token": token,
        "refresh_token": _decrypt_field(auth.get("refreshToken")),
        "expires_at": auth.get("expiresAt", 0),
        "refresh_expires_at": auth.get("refreshExpiresAt", 0),
        "domain": _decrypt_field(auth.get("domain")) or DEFAULT_DOMAIN,
        "enterprise_id": _decrypt_field(account.get("enterpriseId")),
        "session_state": _decrypt_field(auth.get("sessionState")),
    }


def parse_account_payload(data: dict, name: str = "") -> Optional[dict]:
    """Parse nested WorkBuddy auth JSON or its flattened UI representation."""
    if not isinstance(data, dict):
        return None
    auth = data.get("auth") if isinstance(data.get("auth"), dict) else {}
    account = data.get("account") if isinstance(data.get("account"), dict) else {}

    def pick(nested: dict, nested_key: str, flat_key: str) -> str:
        return _decrypt_field(data.get(flat_key)) or _decrypt_field(nested.get(nested_key))

    def number(nested: dict, nested_key: str, flat_key: str):
        for source, key in ((data, flat_key), (nested, nested_key)):
            value = source.get(key)
            if isinstance(value, (int, float)):
                return value
        return 0

    access_token = pick(auth, "accessToken", "access_token")
    if not access_token:
        return None
    nickname = pick(account, "nickname", "nickname")
    account_type = account.get("type") or data.get("account_type") or "personal"
    if not isinstance(account_type, str):
        account_type = "personal"
    return {
        "name": (name or data.get("name") or "").strip() or nickname,
        "uid": pick(account, "uid", "uid"),
        "nickname": nickname,
        "phone": pick(account, "phoneNumber", "phone"),
        "account_type": account_type,
        "access_token": access_token,
        "refresh_token": pick(auth, "refreshToken", "refresh_token"),
        "expires_at": number(auth, "expiresAt", "expires_at"),
        "refresh_expires_at": number(auth, "refreshExpiresAt", "refresh_expires_at"),
        "domain": pick(auth, "domain", "domain") or DEFAULT_DOMAIN,
        "enterprise_id": pick(account, "enterpriseId", "enterprise_id"),
        "session_state": pick(auth, "sessionState", "session_state"),
    }


def upsert_account(parsed: dict, provider: str = "workbuddy") -> tuple[int, str]:
    """Update a same-provider account by uid or insert a new one."""
    uid = str(parsed.get("uid") or "").strip()
    if uid:
        for account in db.list_accounts(provider=provider):
            if str(account.get("uid") or "").strip() == uid:
                db.update_account(account["id"], {**parsed, "provider": provider})
                return account["id"], "updated"
    return db.add_account({**parsed, "provider": provider}), "imported"


def import_auth_file(path: Path) -> Optional[int]:
    """扫描并导入 auth 文件到数据库。如果 uid 已存在则更新。"""
    parsed = parse_auth_file(path)
    if not parsed:
        return None

    # 检查是否已存在（按 uid 去重）
    existing = db.list_accounts()
    for acc in existing:
        if acc.get("uid") == parsed["uid"]:
            db.update_account(acc["id"], parsed)
            return acc["id"]

    return db.add_account(parsed)


def auto_scan_and_import(auth_dir: Optional[str] = None) -> dict:
    """自动扫描本机 auth 文件并导入。返回 {imported, updated, skipped}。"""
    result = {"imported": 0, "updated": 0, "skipped": 0, "errors": []}
    existing_by_uid = {
        account.get("uid"): account
        for account in db.list_accounts()
        if account.get("uid")
    }
    for f in find_auth_files(auth_dir):
        parsed = parse_auth_file(f)
        if not parsed:
            result["skipped"] += 1
            continue
        existing = existing_by_uid.get(parsed.get("uid"))
        if existing:
            patch = {
                key: parsed[key]
                for key in (
                    "access_token",
                    "refresh_token",
                    "expires_at",
                    "refresh_expires_at",
                    "session_state",
                    "nickname",
                    "name",
                    "phone",
                )
                if key in parsed
            }
            db.update_account(existing["id"], patch)
            result["updated"] += 1
        else:
            aid = db.add_account(parsed)
            if parsed.get("uid"):
                existing_by_uid[parsed["uid"]] = {**parsed, "id": aid}
            result["imported"] += 1
    return result


# ============================================================
# Token 刷新 / 保活
# ============================================================

SESSION_DEAD_THRESHOLD = 3
_session_dead_lock = threading.Lock()
_session_dead_counts: dict[int, int] = {}


def reset_session_dead_counts() -> None:
    with _session_dead_lock:
        _session_dead_counts.clear()


def session_dead_count(aid: int) -> int:
    with _session_dead_lock:
        return _session_dead_counts.get(int(aid), 0)


def _note_session_dead(aid: int) -> int:
    with _session_dead_lock:
        count = _session_dead_counts.get(int(aid), 0) + 1
        _session_dead_counts[int(aid)] = count
        return count


def clear_session_dead(aid: int) -> None:
    with _session_dead_lock:
        _session_dead_counts.pop(int(aid), None)


def epoch_ms(value, *, now_ms: int | None = None, seconds=None) -> int:
    """Normalize upstream expiry to milliseconds. Seconds-since-epoch values are scaled."""
    moment = int(time.time() * 1000) if now_ms is None else int(now_ms)
    try:
        number = int(value or 0)
    except (TypeError, ValueError):
        number = 0
    if number > 0:
        return number * 1000 if number < 10**12 else number
    try:
        extra = int(seconds or 0)
    except (TypeError, ValueError):
        extra = 0
    if extra > 0:
        return moment + extra * 1000
    return 0


def _safe_refresh_message(text: str, fallback: str) -> str:
    value = " ".join(str(text or "").split())
    lowered = value.lower()
    if not value or "accesstoken" in lowered or "refreshtoken" in lowered or "bearer " in lowered:
        return fallback
    return value[:180]


def _is_session_dead(status_code: int, payload, message: str) -> bool:
    parts = [str(message or "")]
    if isinstance(payload, dict):
        parts.append(str(payload.get("code") or ""))
        parts.append(str(payload.get("msg") or payload.get("message") or ""))
        data = payload.get("data")
        if isinstance(data, dict):
            parts.append(str(data.get("code") or ""))
            parts.append(str(data.get("msg") or ""))
    text = " ".join(parts)
    return "12153" in text or "offline user session not found" in text.lower()


def _refresh_view(account: dict, *, ok: bool, reason: str, message: str, status_code: int = 0) -> dict:
    fresh = db.get_account(account["id"]) or account
    return {
        "ok": ok,
        "account_id": account.get("id"),
        "account_name": account.get("nickname") or account.get("name") or str(account.get("id")),
        "provider": account.get("provider") or "workbuddy",
        "reason": reason,
        "message": message,
        "status": fresh.get("status") or account.get("status") or "",
        "status_code": status_code,
        "session_dead_count": session_dead_count(account["id"]),
        "skipped": reason in {"inactive", "missing_refresh"},
    }


async def refresh_token_result(account: dict) -> dict:
    """Refresh one WorkBuddy login. Transient failures keep the current status."""
    aid = int(account["id"])
    if not str(account.get("refresh_token") or "").strip():
        return _refresh_view(account, ok=False, reason="missing_refresh", message="没有 refresh token，需要重新登录")

    lock = _get_token_lock(aid)
    async with lock:
        current = db.get_account(aid) or account
        headers = build_refresh_headers(current)
        url = f"{backend_url(current)}/v2/plugin/auth/token/refresh"
        try:
            async with httpx.AsyncClient(timeout=request_timeout(15)) as client:
                response = await client.post(url, headers=headers, json={})
            data = response.json()
            status_code = response.status_code
        except (httpx.HTTPError, ValueError) as exc:
            print(f"[auth_manager] 刷新 token 网络失败 (account={aid}): {type(exc).__name__}", file=sys.stderr)
            return _refresh_view(current, ok=False, reason="transient", message="网络或响应异常，账号状态未改")

        message = ""
        if isinstance(data, dict):
            message = str(data.get("msg") or data.get("message") or "")
        new_auth = data.get("data") if isinstance(data, dict) and isinstance(data.get("data"), dict) else None
        code = data.get("code") if isinstance(data, dict) else None
        access_token = str((new_auth or {}).get("accessToken") or "").strip()
        failed = (
            not isinstance(data, dict)
            or status_code >= 300
            or code not in {0, "0"}
            or not access_token
        )
        if failed:
            safe = _safe_refresh_message(message, "上游拒绝刷新")
            if _is_session_dead(status_code, data, message):
                count = _note_session_dead(aid)
                if count >= SESSION_DEAD_THRESHOLD:
                    db.update_account(aid, {"status": "expired"})
                    text = f"登录态已失效（连续 {count} 次），请重新浏览器登录"
                else:
                    text = f"登录态暂时失效（{count}/{SESSION_DEAD_THRESHOLD}），未禁用"
                print(f"[auth_manager] 刷新 token session dead (account={aid}): {count}", file=sys.stderr)
                return _refresh_view(current, ok=False, reason="session_dead", message=text, status_code=status_code)
            if status_code >= 500 or status_code == 429 or status_code == 0:
                reason = "transient"
                text = "上游暂时失败，账号状态未改"
            else:
                reason = "rejected"
                text = safe
            print(f"[auth_manager] 刷新 token 失败 (account={aid}): {reason}", file=sys.stderr)
            return _refresh_view(current, ok=False, reason=reason, message=text, status_code=status_code)

        now_ms = int(time.time() * 1000)
        refresh_token = str(new_auth.get("refreshToken") or "").strip() or str(current.get("refresh_token") or "")
        expires_at = epoch_ms(new_auth.get("expiresAt"), now_ms=now_ms, seconds=new_auth.get("expiresIn"))
        refresh_expires_at = epoch_ms(
            new_auth.get("refreshExpiresAt"), now_ms=now_ms, seconds=new_auth.get("refreshExpiresIn")
        )
        next_status = "inactive" if current.get("status") == "inactive" else "active"
        update_data = {
            "access_token": access_token,
            "refresh_token": refresh_token,
            "expires_at": expires_at or int(current.get("expires_at") or 0),
            "refresh_expires_at": refresh_expires_at or int(current.get("refresh_expires_at") or 0),
            "domain": str(new_auth.get("domain") or current.get("domain") or DEFAULT_DOMAIN),
            "status": next_status,
        }
        session_state = str(new_auth.get("sessionState") or "").strip()
        if session_state:
            update_data["session_state"] = session_state
        db.update_account(aid, update_data)
        clear_session_dead(aid)
        return _refresh_view(current, ok=True, reason="refreshed", message="已续期", status_code=status_code)


async def refresh_token(account: dict) -> bool:
    """调后端刷新 token，写回数据库。返回是否成功。"""
    return bool((await refresh_token_result(account)).get("ok"))


def upsert_login_account(parsed: dict) -> dict:
    """Insert or update a WorkBuddy account from browser login. Keeps weight and priority."""
    uid = str(parsed.get("uid") or "").strip()
    channel = workbuddy_channel_for_domain(str(parsed.get("domain") or ""))
    parsed["provider"] = channel
    existing = next(
        (
            row
            for row in db.list_accounts(provider="workbuddy") + db.list_accounts(provider="workbuddy-i")
            if row.get("uid") == uid
        ),
        None,
    )
    if existing:
        patch = {
            "access_token": parsed.get("access_token") or existing.get("access_token") or "",
            "refresh_token": parsed.get("refresh_token") or existing.get("refresh_token") or "",
            "expires_at": parsed.get("expires_at") or existing.get("expires_at") or 0,
            "refresh_expires_at": parsed.get("refresh_expires_at") or existing.get("refresh_expires_at") or 0,
            "domain": parsed.get("domain") or existing.get("domain") or DEFAULT_DOMAIN,
            "provider": channel,
            "status": "active",
        }
        if parsed.get("nickname"):
            patch["nickname"] = parsed["nickname"]
            patch["name"] = parsed.get("name") or parsed["nickname"]
        if parsed.get("enterprise_id"):
            patch["enterprise_id"] = parsed["enterprise_id"]
        if parsed.get("phone"):
            patch["phone"] = parsed["phone"]
        if parsed.get("session_state"):
            patch["session_state"] = parsed["session_state"]
        db.update_account(existing["id"], patch)
        clear_session_dead(existing["id"])
        return {"id": existing["id"], "updated": True}
    aid = db.add_account({**parsed, "provider": "workbuddy", "status": "active"})
    return {"id": aid, "updated": False}


def is_token_expired(account: dict) -> bool:
    expires_at = account.get("expires_at", 0)
    if not expires_at:
        return True
    return time.time() * 1000 >= (expires_at - 60_000)


async def ensure_token_valid(account: dict) -> bool:
    """如果 token 快过期则刷新。返回是否有效。"""
    if not is_token_expired(account):
        return True
    return await refresh_token(account)


# ============================================================
# Header 构造
# ============================================================

def _fingerprint_account(account: dict) -> dict:
    """为指纹构造补齐默认 domain（不修改原 dict）。"""
    if (account.get("domain") or "").strip():
        return account
    domain = str(db.get_setting("default_domain", DEFAULT_DOMAIN) or DEFAULT_DOMAIN)
    return {**account, "domain": domain}


def build_headers(account: dict) -> dict:
    """Chat 请求头：官方 CLI 完整指纹（通用 + 账号 + IDE/CLI + SDK）。"""
    return fingerprint.chat_headers(_fingerprint_account(account))


def build_billing_headers(account: dict) -> dict:
    """Billing 接口（余额/积分）请求头指纹。"""
    return fingerprint.billing_headers(_fingerprint_account(account))


def build_refresh_headers(account: dict) -> dict:
    """Token 刷新接口请求头指纹（X-Refresh-Token 只出现在这里）。"""
    return fingerprint.refresh_headers(_fingerprint_account(account))


async def get_valid_headers(account: dict) -> Optional[dict]:
    """确保 token 有效后返回 chat 指纹 header。失败返回 None。"""
    if not await ensure_token_valid(account):
        return None
    # 重新从数据库读取最新凭据
    fresh = db.get_account(account["id"])
    if not fresh:
        return None
    return build_headers(fresh)


async def get_billing_headers(account: dict) -> Optional[dict]:
    """确保 token 有效后返回 billing 指纹 header。失败返回 None。"""
    if not await ensure_token_valid(account):
        return None
    # 重新从数据库读取最新凭据
    fresh = db.get_account(account["id"])
    if not fresh:
        return None
    return build_billing_headers(fresh)


# ============================================================
# 每日积分领取
# ============================================================

def _checkin_result(
    account: dict,
    *,
    ok: bool,
    status_code: int = 0,
    message: str = "",
    payload: Optional[dict] = None,
    claimed: bool = False,
    already_claimed: bool = False,
) -> dict:
    payload = payload or {}
    credit = payload.get("credit", payload.get("today_credit", 0)) or 0
    try:
        credit = float(credit)
    except (TypeError, ValueError):
        credit = 0
    return {
        "account_id": account.get("id"),
        "account_name": account.get("nickname") or account.get("name") or str(account.get("id")),
        "ok": ok,
        "claimed": claimed,
        "already_claimed": already_claimed,
        "status_code": status_code,
        "message": message,
        "credit": credit,
        "active": payload.get("active"),
        "today_checked_in": payload.get("today_checked_in"),
        "today_credit": payload.get("today_credit"),
        "streak_days": payload.get("streak_days"),
        "is_streak_day": payload.get("is_streak_day"),
    }


def _unwrap_response(data: object) -> tuple[bool, str, dict]:
    if not isinstance(data, dict):
        return False, "响应不是 JSON 对象", {}
    code = data.get("code")
    msg = str(data.get("msg") or data.get("message") or "")
    payload = data.get("data") if isinstance(data.get("data"), dict) else data
    if code not in (None, 0):
        return False, msg or f"code={code}", payload
    return True, msg or "OK", payload


# ============================================================
# 官方额度资源
# ============================================================

def _to_float(value, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _to_int(value, default: int = 0) -> int:
    try:
        if value is None or value == "":
            return default
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _parse_resource_time(value) -> tuple[int | None, str]:
    """把官方资源时间统一为秒级时间戳和原始可读字符串。"""
    if value in (None, "", 0, "0", "9999-99-99 99:99:99"):
        return None, str(value or "")

    if isinstance(value, (int, float)):
        raw = float(value)
        if raw > 10_000_000_000:
            raw = raw / 1000
        return int(raw), time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(raw))

    text = str(value).strip()
    if not text:
        return None, ""
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(text, fmt)
            return int(dt.timestamp()), text
        except ValueError:
            continue
    return None, text


def _safe_resource_item(item: dict, now_ts: int) -> dict:
    cycle_end_ts, cycle_end = _parse_resource_time(item.get("CycleEndTime"))
    cycle_start_ts, cycle_start = _parse_resource_time(item.get("CycleStartTime"))
    deduction_end_ts, deduction_end = _parse_resource_time(item.get("DeductionEndTime"))
    deduction_start_ts, deduction_start = _parse_resource_time(item.get("DeductionStartTime"))
    expired_ts, expired_time = _parse_resource_time(item.get("ExpiredTime"))

    remain = _to_float(item.get("CapacityRemainPrecise"), _to_float(item.get("CapacityRemain")))
    used = _to_float(item.get("CapacityUsedPrecise"), _to_float(item.get("CapacityUsed")))
    size = _to_float(item.get("CapacitySizePrecise"), _to_float(item.get("CapacitySize")))
    cycle_remain = _to_float(item.get("CycleCapacityRemainPrecise"), _to_float(item.get("CycleCapacityRemain")))
    cycle_used = _to_float(item.get("CycleCapacityUsedPrecise"), _to_float(item.get("CycleCapacityUsed")))
    cycle_size = _to_float(item.get("CycleCapacitySizePrecise"), _to_float(item.get("CycleCapacitySize"), size))
    effective_remain = cycle_remain if cycle_remain > 0 or cycle_used > 0 else remain

    expire_ts = expired_ts or deduction_end_ts or cycle_end_ts
    days_to_expire = None
    if expire_ts:
        days_to_expire = math.ceil((expire_ts - now_ts) / 86400)

    package_name = str(item.get("PackageName") or item.get("DealName") or item.get("ProductName") or "额度包")
    product_name = str(item.get("ProductName") or item.get("SubProductName") or "")
    status = _to_int(item.get("Status"))
    is_expired = bool(expire_ts and expire_ts < now_ts)

    return {
        "package_name": package_name,
        "product_name": product_name,
        "package_type": str(item.get("PackageType") or ""),
        "resource_type": str(item.get("ResourceType") or ""),
        "capacity_unit": str(item.get("CapacityUnit") or item.get("OriginUnit") or "credit"),
        "status": status,
        "remaining": round(remain, 4),
        "remaining_precise": round(effective_remain, 4),
        "used": round(used, 4),
        "size": round(size, 4),
        "cycle_remaining": round(cycle_remain, 4),
        "cycle_used": round(cycle_used, 4),
        "cycle_size": round(cycle_size, 4),
        "cycle_start": cycle_start,
        "cycle_start_ts": cycle_start_ts,
        "cycle_end": cycle_end,
        "cycle_end_ts": cycle_end_ts,
        "deduction_start": deduction_start,
        "deduction_start_ts": deduction_start_ts,
        "deduction_end": deduction_end,
        "deduction_end_ts": deduction_end_ts,
        "expired_time": expired_time,
        "expired_ts": expired_ts,
        "expire_ts": expire_ts,
        "expire_time": expired_time or deduction_end or cycle_end,
        "days_to_expire": days_to_expire,
        "expired": is_expired,
        "auto_renew": bool(_to_int(item.get("AutoRenewFlag"))),
        "remain_cycles": _to_int(item.get("RemainCycles")),
        "total_cycles": _to_int(item.get("TotalCycles")),
    }


def _resource_failure(
    account: dict,
    *,
    message: str,
    status_code: int = 0,
    allow_stale: bool = True,
) -> dict:
    if account.get("id"):
        try:
            db.mark_resource_refresh_failed(account["id"])
        except sqlite3.Error:
            pass
    cached = db.get_account_resource_cache(account.get("id")) if allow_stale and account.get("id") else None
    if cached:
        cached["stale"] = True
        cached["message"] = message
        cached["status_code"] = status_code
        return cached
    return {
        "ok": False,
        "status_code": status_code,
        "message": message,
        "account_id": account.get("id"),
        "account_name": account.get("nickname") or account.get("name") or str(account.get("id")),
        "total_dosage": 0,
        "resource_count": 0,
        "package_count": 0,
        "active_package_count": 0,
        "expired_package_count": 0,
        "expiring_package_count": 0,
        "available_total": 0,
        "expiring_7d_total": 0,
        "expiring_30d_total": 0,
        "next_expire_time": "",
        "next_expire_ts": None,
        "next_expire_amount": 0,
        "next_expire_days": None,
        "updated_at": int(time.time()),
        "cached": False,
        "stale": False,
        "age_seconds": 0,
        "packages": [],
        "expiring_packages": [],
    }


async def fetch_account_resources(
    account: dict,
    *,
    force: bool = False,
    max_age_seconds: int = 60,
    allow_stale: bool = True,
    routing_only: bool = False,
) -> dict:
    aid = account.get("id")
    if not aid:
        return await _fetch_account_resources_unlocked(
            account, force=force, max_age_seconds=max_age_seconds, allow_stale=allow_stale
        )
    loop = asyncio.get_running_loop()
    locks = _resource_locks.setdefault(loop, {})
    lock = locks.setdefault((str(db.DB_PATH), aid), asyncio.Lock())
    async with lock:
        fresh = db.get_account(aid) or account
        if routing_only:
            import providers

            if (fresh.get("status") != "active" or not providers.is_channel_enabled("workbuddy")
                    or account_routing.load_config()["mode"] != "expiry_first"):
                return {"ok": False, "message": "routing refresh is disabled", "packages": []}
        if fresh.get("status") == "inactive":
            return _resource_failure(fresh, message="account is inactive", allow_stale=allow_stale)
        try:
            return await _fetch_account_resources_unlocked(
                fresh, force=force, max_age_seconds=max_age_seconds, allow_stale=allow_stale
            )
        except (Exception, asyncio.CancelledError):
            try:
                db.mark_resource_refresh_failed(aid)
            except sqlite3.Error:
                pass
            raise


async def _fetch_account_resources_unlocked(
    account: dict, *, force: bool, max_age_seconds: int, allow_stale: bool,
) -> dict:
    """查询官方额度资源，只返回安全摘要和额度包明细。"""
    if account.get("id") and not force:
        cached = db.get_account_resource_cache(account["id"])
        if (cached and "route_usage_baseline" in cached and cached.get("route_last_attempt_ok") is True
                and cached.get("ok") is True
                and 0 < cached.get("cache_updated_at", 0) <= time.time()
                and int(cached.get("age_seconds") or 0) <= max_age_seconds):
            cached["stale"] = False
            return cached

    headers = await get_billing_headers(account)
    if not headers:
        return _resource_failure(
            account,
            message="token refresh failed or account credentials are invalid",
            allow_stale=allow_stale,
        )

    try:
        async with httpx.AsyncClient(timeout=min(25, request_timeout(25))) as c:
            r = await c.post(f"{backend_url(account)}/v2/billing/meter/get-user-resource", headers=headers, json={})
            data = r.json()
    except (httpx.HTTPError, ValueError) as e:
        return _resource_failure(
            account,
            message=str(e)[:240],
            allow_stale=allow_stale,
        )

    ok, msg, payload = _unwrap_response(data)
    if r.status_code < 200 or r.status_code >= 300:
        ok = False

    response = payload.get("Response") if isinstance(payload.get("Response"), dict) else {}
    raw_data = response.get("Data") if isinstance(response.get("Data"), dict) else {}
    raw_items = raw_data.get("Accounts") if isinstance(raw_data.get("Accounts"), list) else []
    now_ts = int(time.time())
    packages = [_safe_resource_item(x, now_ts) for x in raw_items if isinstance(x, dict)]
    packages.sort(key=lambda x: (
        x.get("expired", False),
        x.get("expire_ts") or 9_999_999_999,
        -float(x.get("remaining_precise") or 0),
    ))

    active_packages = [p for p in packages if not p.get("expired")]
    expiring_packages = [
        p for p in active_packages
        if p.get("expire_ts") and 0 <= (p["expire_ts"] - now_ts) <= 7 * 86400
        and float(p.get("remaining_precise") or 0) > 0
    ]
    expiring_30d_packages = [
        p for p in active_packages
        if p.get("expire_ts") and 0 <= (p["expire_ts"] - now_ts) <= 30 * 86400
        and float(p.get("remaining_precise") or 0) > 0
    ]
    next_expiring = next(
        (
            p for p in active_packages
            if p.get("expire_ts") and float(p.get("remaining_precise") or 0) > 0
        ),
        None,
    )
    expired_packages = [p for p in packages if p.get("expired")]

    result = {
        "ok": ok,
        "status_code": r.status_code,
        "message": msg,
        "account_id": account.get("id"),
        "account_name": account.get("nickname") or account.get("name") or str(account.get("id")),
        "total_dosage": round(_to_float(raw_data.get("TotalDosage")), 4),
        "resource_count": _to_int(raw_data.get("TotalCount"), len(packages)),
        "package_count": len(packages),
        "active_package_count": len(active_packages),
        "expired_package_count": len(expired_packages),
        "expiring_package_count": len(expiring_packages),
        "available_total": round(sum(float(p.get("remaining_precise") or 0) for p in active_packages), 4),
        "expiring_7d_total": round(sum(float(p.get("remaining_precise") or 0) for p in expiring_packages), 4),
        "expiring_30d_total": round(sum(float(p.get("remaining_precise") or 0) for p in expiring_30d_packages), 4),
        "next_expire_time": next_expiring.get("expire_time") if next_expiring else "",
        "next_expire_ts": next_expiring.get("expire_ts") if next_expiring else None,
        "next_expire_amount": round(float(next_expiring.get("remaining_precise") or 0), 4) if next_expiring else 0,
        "next_expire_days": next_expiring.get("days_to_expire") if next_expiring else None,
        "updated_at": now_ts,
        "cached": False,
        "stale": False,
        "age_seconds": 0,
        "packages": packages,
        "expiring_packages": expiring_packages,
        "route_usage_baseline": account.get("total_credits", 0),
        "route_last_attempt_at": now_ts,
        "route_last_attempt_ok": True,
    }
    if ok and account.get("id"):
        db.upsert_account_resource_cache(account["id"], result)
    if not ok:
        return _resource_failure(
            account,
            message=msg,
            status_code=r.status_code,
            allow_stale=allow_stale,
        )
    return result


def _checkin_failure(
    account: dict,
    *,
    message: str,
    status_code: int = 0,
    allow_stale: bool = True,
) -> dict:
    cached = db.get_account_checkin_cache(account.get("id")) if allow_stale and account.get("id") else None
    if cached:
        cached["stale"] = True
        cached["message"] = message
        cached["status_code"] = status_code
        return cached
    return _checkin_result(account, ok=False, status_code=status_code, message=message)


async def fetch_checkin_status(
    account: dict,
    *,
    force: bool = False,
    max_age_seconds: int = 300,
    allow_stale: bool = True,
) -> dict:
    """查询每日积分领取状态。只返回安全摘要，不返回凭据。"""
    if account.get("id") and not force:
        cached = db.get_account_checkin_cache(account["id"])
        if cached and int(cached.get("age_seconds") or 0) <= max_age_seconds:
            cached["stale"] = False
            return cached

    headers = await get_billing_headers(account)
    if not headers:
        return _checkin_failure(
            account,
            message="token refresh failed or account credentials are invalid",
            allow_stale=allow_stale,
        )

    try:
        async with httpx.AsyncClient(timeout=request_timeout(20)) as c:
            r = await c.post(f"{backend_url(account)}/v2/billing/meter/checkin-activity-status", headers=headers, json={})
            data = r.json()
    except (httpx.HTTPError, ValueError) as e:
        return _checkin_failure(account, status_code=0, message=str(e)[:240], allow_stale=allow_stale)

    ok, msg, payload = _unwrap_response(data)
    if r.status_code < 200 or r.status_code >= 300:
        ok = False
    result = _checkin_result(
        account,
        ok=ok,
        status_code=r.status_code,
        message=msg,
        payload=payload,
        already_claimed=bool(payload.get("today_checked_in")),
    )
    result["updated_at"] = int(time.time())
    result["cached"] = False
    result["stale"] = False
    result["age_seconds"] = 0
    if ok and account.get("id"):
        db.upsert_account_checkin_cache(account["id"], result)
    if not ok:
        return _checkin_failure(account, status_code=r.status_code, message=msg, allow_stale=allow_stale)
    return result


async def claim_daily_checkin(account: dict) -> dict:
    """手动领取单个账号的每日积分。不会绕过验证或做自动定时。"""
    status = await fetch_checkin_status(account, force=True, allow_stale=False)
    if not status.get("ok"):
        return status
    if status.get("active") is False:
        status["ok"] = False
        status["message"] = "活动当前不可用"
        return status
    if status.get("today_checked_in"):
        status["already_claimed"] = True
        status["message"] = "今日已领取"
        return status

    fresh = db.get_account(account["id"])
    if not fresh:
        return _checkin_result(account, ok=False, message="account not found")
    headers = await get_billing_headers(fresh)
    if not headers:
        return _checkin_result(
            account,
            ok=False,
            message="token refresh failed or account credentials are invalid",
        )

    try:
        async with httpx.AsyncClient(timeout=request_timeout(30)) as c:
            r = await c.post(f"{backend_url(account)}/v2/billing/meter/daily-checkin", headers=headers, json={})
            data = r.json()
    except (httpx.HTTPError, ValueError) as e:
        return _checkin_result(account, ok=False, status_code=0, message=str(e)[:240])

    ok, msg, payload = _unwrap_response(data)
    if r.status_code < 200 or r.status_code >= 300:
        ok = False
    credit = payload.get("credit", payload.get("today_credit", 0)) or 0
    try:
        credit = float(credit)
    except (TypeError, ValueError):
        credit = 0
    claimed = bool(ok and credit > 0)
    if claimed and float(fresh.get("credit_limit") or 0) > 0:
        db.update_account(fresh["id"], {"credit_limit": float(fresh.get("credit_limit") or 0) + credit})

    result = _checkin_result(
        account,
        ok=ok,
        status_code=r.status_code,
        message=("领取成功" if claimed else msg),
        payload=payload,
        claimed=claimed,
        already_claimed=bool(payload.get("today_checked_in")) and not claimed,
    )
    result["updated_at"] = int(time.time())
    result["cached"] = False
    result["stale"] = False
    result["age_seconds"] = 0
    if ok and account.get("id"):
        db.upsert_account_checkin_cache(account["id"], result)
    return result


# ============================================================
# 账号路由（同级粘性）
# ============================================================

def _route_int(value, default: int = 0) -> int:
    try:
        if value is None or value == "":
            return default
        return int(value)
    except (TypeError, ValueError):
        return default


def _route_priority(account: dict) -> int:
    return _route_int(account.get("priority"), 0)


def _route_weight(account: dict) -> int:
    return max(1, _route_int(account.get("weight"), 1))


def _route_sort_key(account: dict):
    weight = _route_weight(account)
    total_requests = _route_int(account.get("total_requests"), 0)
    return (
        -_route_priority(account),
        -weight,
        total_requests / weight,
        total_requests,
        _route_int(account.get("id"), 0),
    )


def _set_sticky_account(aid: int, provider: str = "workbuddy"):
    with _route_lock:
        _sticky_account_id[provider] = aid


def workbuddy_channel_for_domain(domain: str) -> str:
    host = _host_from(domain)
    if host == "workbuddy.ai" or host.endswith(".workbuddy.ai"):
        return "workbuddy-i"
    return "workbuddy"


def pick_account(
    exclude_ids: set[int] = None,
    provider: str = "workbuddy",
    *, affinity=None,
) -> Optional[dict]:
    """Pick an available account. Higher priority first; stick to the current one on ties."""
    if affinity is not None:
        import session_identity
        import session_affinity
        config = session_identity.load_config()
        if config["enabled"] and isinstance(affinity, session_identity.Identity) and affinity.channel == provider:
            def eligible():
                # Re-read under the binding lock: an older queued snapshot must not undo a migration.
                return [a for a in db.get_active_accounts(provider) if not account_is_cooling_down(a["id"])]
            routing = account_routing.load_config() if provider == "workbuddy" else dict(account_routing.DEFAULTS)
            def choose(candidates):
                snapshots = account_routing.read_snapshots(candidates) if routing["mode"] == "expiry_first" else {}
                top, _ = account_routing.best_group(candidates, routing, snapshots)
                with _route_lock:
                    sticky_id = _sticky_account_id.get(provider)
                return next((a for a in top if a["id"] == sticky_id), None) or min(top, key=_route_sort_key)
            return session_affinity.get_store(config).select(affinity, eligible, exclude_ids or set(), choose, config)
    exclude_ids = exclude_ids or set()
    accounts = db.get_active_accounts(provider)
    candidates = [
        a for a in accounts
        if a["id"] not in exclude_ids and not account_is_cooling_down(a["id"])
    ]
    if not candidates:
        return None

    config = account_routing.load_config() if provider == "workbuddy" else dict(account_routing.DEFAULTS)
    snapshots = account_routing.read_snapshots(candidates) if config["mode"] == "expiry_first" else {}
    top_candidates, _ = account_routing.best_group(candidates, config, snapshots)
    with _route_lock:
        sticky_id = _sticky_account_id.get(provider)
        if sticky_id is not None:
            sticky = next((a for a in top_candidates if a["id"] == sticky_id), None)
            if sticky:
                return sticky

        chosen = sorted(top_candidates, key=_route_sort_key)[0]
        _sticky_account_id[provider] = chosen["id"]
        return chosen


def routing_preview() -> dict:
    config = account_routing.load_config()
    accounts = db.list_accounts(provider="workbuddy")
    candidates = [a for a in accounts if a.get("status") == "active"
                  and not account_is_cooling_down(a["id"])]
    snapshots = account_routing.read_snapshots(candidates) if config["mode"] == "expiry_first" else {}
    top, summaries = account_routing.best_group(candidates, config, snapshots)
    highest = max((_route_priority(a) for a in candidates), default=None)
    with _route_lock:
        sticky_id = _sticky_account_id.get("workbuddy")
    chosen = next((a for a in top if a["id"] == sticky_id), None)
    if chosen is None and top:
        chosen = min(top, key=_route_sort_key)
    rows = []
    for account in accounts:
        summary = summaries.get(account["id"], {})
        reason = summary.get("reason", "legacy" if config["mode"] == "legacy" else "lower_manual_priority")
        if account.get("status") != "active":
            reason = "inactive"
        elif account_is_cooling_down(account["id"]):
            reason = "cooldown"
        elif highest is not None and _route_priority(account) < highest:
            reason = "lower_manual_priority"
        rows.append({"account_id": account["id"], "priority": _route_priority(account),
                     **summary, "reason": reason,
                     "in_best_group": any(a["id"] == account["id"] for a in top)})
    return {"channel": "workbuddy", "config": config, "sticky_account_id": sticky_id,
            "selected_account_id": chosen["id"] if chosen else None, "accounts": rows}


async def pick_account_with_fallback(
    exclude_ids: set[int] = None,
    provider: str = "workbuddy",
    *, affinity=None,
) -> Optional[dict]:
    """Pick an account; refresh expired ones if needed. Only refreshes the same provider."""
    from starlette.concurrency import run_in_threadpool
    if affinity is not None:
        import session_identity
        config = await run_in_threadpool(session_identity.load_config)
        if not config["enabled"]:
            affinity = None
    if affinity is None:
        account = pick_account(exclude_ids, provider=provider)
    else:
        account = await run_in_threadpool(lambda: pick_account(exclude_ids, provider=provider, affinity=affinity))
    if account:
        return account
    expired_accounts = sorted(
        (
            account
            for account in db.list_accounts(provider=provider)
            if account.get("status") == "expired"
        ),
        key=_route_sort_key,
    )
    for a in expired_accounts:
        if a["id"] in (exclude_ids or set()):
            continue
        if await refresh_token(a):
            fresh = db.get_account(a["id"])
            if fresh:
                if affinity is not None:
                    account = await run_in_threadpool(lambda: pick_account(exclude_ids, provider=provider, affinity=affinity))
                    if account:
                        return account
                    continue
                _set_sticky_account(fresh["id"], provider)
                return fresh
    return None


# ============================================================
# 账号状态检查
# ============================================================

def get_account_status(account: dict) -> dict:
    """返回账号状态摘要。"""
    expired = is_token_expired(account)
    now_ms = int(time.time() * 1000)
    remaining_hours = 0
    if account.get("expires_at"):
        remaining_hours = max(0, int((account["expires_at"] - now_ms) / 1000 / 3600))
    refresh_expires_at = int(account.get("refresh_expires_at") or 0)
    refresh_known = refresh_expires_at > 0
    refresh_expired = bool(refresh_known and now_ms >= refresh_expires_at)
    refresh_remaining_hours = (
        max(0, int((refresh_expires_at - now_ms) / 1000 / 3600)) if refresh_known else 0
    )
    credit_snapshot = max(0.0, float(account.get("credit_limit") or 0))
    credit_baseline = max(0.0, float(account.get("credit_baseline") or 0))
    total_credits = round(float(account.get("total_credits") or 0), 4)
    credit_since_snapshot = round(max(0.0, total_credits - credit_baseline), 4)
    credit_remaining = None
    credit_used_pct = 0
    if credit_snapshot > 0:
        credit_remaining = round(max(0.0, credit_snapshot - credit_since_snapshot), 4)
        credit_used_pct = min(100, round(credit_since_snapshot / credit_snapshot * 100, 1))

    return {
        "id": account["id"],
        "name": account.get("name", ""),
        "nickname": account.get("nickname", ""),
        "uid": account.get("uid", ""),
        "status": account.get("status", "unknown"),
        "weight": int(account.get("weight") or 1),
        "priority": int(account.get("priority") or 0),
        "token_expired": expired,
        "remaining_hours": remaining_hours,
        "refresh_known": refresh_known,
        "refresh_expired": refresh_expired,
        "refresh_remaining_hours": refresh_remaining_hours,
        "total_requests": account.get("total_requests", 0),
        "total_tokens": account.get("total_tokens", 0),
        "total_credits": total_credits,
        "credit_limit": round(credit_snapshot, 4),
        "credit_snapshot": round(credit_snapshot, 4),
        "credit_baseline": round(credit_baseline, 4),
        "credit_since_snapshot": credit_since_snapshot,
        "credit_remaining": credit_remaining,
        "credit_used_pct": credit_used_pct,
        "credit_source": "local_snapshot" if credit_snapshot > 0 else "usage_only",
        "last_used_at": account.get("last_used_at"),
    }


def check_all_accounts() -> list[dict]:
    """检查所有账号状态。"""
    accounts = db.list_accounts()
    return [get_account_status(a) for a in accounts]
