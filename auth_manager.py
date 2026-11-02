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
# Cooldown deadline per account (monotonic seconds). Only says "when may this
# account be used again" -- never carries the failure streak.
_account_failures: dict[int, float] = {}
# Consecutive-failure streak per account: (count, streak_expires_at_monotonic).
# Kept in its OWN store so that an expiring cooldown can never erase it. This is
# what makes the 30 -> 60 -> 120 -> 240 -> 300s ladder actually escalate instead
# of being pinned at 30s forever.
_failure_counts: dict[int, tuple[int, float]] = {}
_resource_locks = weakref.WeakKeyDictionary()

# Cooldown tuning (see docs/investigation/09 & 10).
FAILURE_COUNT_TTL_SECONDS = 6 * 3600     # streak survives this long without success
MAX_COOLDOWN_SECONDS = 21 * 3600         # longest real reset window observed: 20.4h


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


# ---------------------------------------------------------------------------
# Account-expiry policy (deliberately minimal).
#
# Scope: a single edge-layer 401/403 (e.g. openresty/APISIX HTML body) must not
# permanently expire an account, while a genuine, repeated authentication
# failure still should. That is all this code claims to do.
#
# KNOWN LIMITATIONS (not solved here, by decision):
#   * A credential renewal (browser login, token refresh, import/scan,
#     control-plane import) and an administrator status change do not participate
#     in the lock below. An older in-flight failure or refresh result can still
#     race with them and, in principle, overwrite a newer status. Reproduced but
#     left as a known limitation rather than fixed with an architecture change.
#   * The tally is in-process; a restart clears it (may delay recognising a
#     genuinely dead account).
# Fixing all of the above would require a unified status/credential write path
# across every provider and admin route, which is out of scope for this defect.
# ---------------------------------------------------------------------------

EXPIRE_CONFIRMATIONS = 2
AUTH_CONFIRMATION_TTL_SEC = 6 * 3600

_auth_confirm_lock = threading.Lock()
_auth_confirmations: dict[int, tuple[int, float]] = {}

# Serializes concurrent failures for the same account so two parallel failures
# cannot both decide to expire from a stale read. Renewals/admin writes do not
# take this lock (see known limitations).
_account_state_locks: dict[int, threading.RLock] = {}
_account_state_locks_guard = threading.Lock()


def _account_state_lock(aid: int) -> threading.RLock:
    with _account_state_locks_guard:
        lock = _account_state_locks.get(int(aid))
        if lock is None:
            lock = threading.RLock()
            _account_state_locks[int(aid)] = lock
        return lock


def reset_auth_confirmations() -> None:
    with _auth_confirm_lock:
        _auth_confirmations.clear()


def reset_account_state_locks() -> None:
    with _account_state_locks_guard:
        _account_state_locks.clear()


def auth_confirmation_count(aid: int) -> int:
    with _auth_confirm_lock:
        count, deadline = _auth_confirmations.get(int(aid), (0, 0.0))
        if deadline and deadline < time.monotonic():
            _auth_confirmations.pop(int(aid), None)
            return 0
        return count


def clear_auth_confirmations(aid: int) -> None:
    with _auth_confirm_lock:
        _auth_confirmations.pop(int(aid), None)


def _note_structural_auth(aid: int) -> int:
    """Count one *structural* auth failure; return the running count.

    Kept in its own store with its own TTL so that cooldown bookkeeping expiring
    can never erase it (an earlier version shared one record and lost the tally).
    """
    deadline = time.monotonic() + AUTH_CONFIRMATION_TTL_SEC
    with _auth_confirm_lock:
        count, previous_deadline = _auth_confirmations.get(int(aid), (0, 0.0))
        if previous_deadline and previous_deadline < time.monotonic():
            count = 0
        count += 1
        _auth_confirmations[int(aid)] = (count, deadline)
        return count


def mark_account_success(aid: int):
    """A real success is the ONLY place the failure streak resets.

    The streak (``_failure_counts``) and the cooldown deadline
    (``_account_failures``) are stored separately on purpose. Sharing one record
    and letting the cooldown expiry remove it is what used to pin every retry at
    30s: the counter was wiped the moment the cooldown lapsed, so the exponential
    step never advanced. The same pattern is already used by
    ``_auth_confirmations`` above ("an earlier version shared one record and lost
    the tally").
    """
    with _failure_lock:
        _account_failures.pop(aid, None)
        _failure_counts.pop(aid, None)
    _persist_cooldown(aid, 0, 0.0, 0)
    clear_auth_confirmations(aid)


def _failure_streak(aid: int, now: float) -> int:
    """Current consecutive-failure count, dropping it once its TTL has passed."""
    entry = _failure_counts.get(aid)
    if not entry:
        return 0
    count, expires_at = entry
    if expires_at and expires_at <= now:
        _failure_counts.pop(aid, None)
        return 0
    return count


def mark_account_failure(aid: int, status_code: int = 0, *, structural_auth: bool = False,
                         reset_at: Optional[float] = None):
    """Record one failed attempt.

    Cooldown bookkeeping is recorded for every failure. ``status='expired'`` is
    written only when ``structural_auth`` is True (a well-formed authentication
    failure, never a bare edge 401 or a rate-limit code) AND the consecutive
    structural-auth tally reaches ``EXPIRE_CONFIRMATIONS`` AND the account is
    currently ``active`` (an operator-set ``inactive`` is never overwritten).

    ``reset_at`` (epoch seconds) is the upstream-declared reset time when the
    error body carried one (6004 carries an absolute timestamp hours out). When
    present the cooldown is pinned to it instead of the short backoff step, so a
    drained account stays out of the pool until it can actually serve again.
    """
    now = time.monotonic()
    with _account_state_lock(aid):
        with _failure_lock:
            count = _failure_streak(aid, now) + 1
            _failure_counts[aid] = (count, now + FAILURE_COUNT_TTL_SECONDS)
            base = 30 if status_code in {401, 403, 429} else 5
            step = min(300, base * (2 ** min(count - 1, 4)))
            cooldown = step
            if reset_at is not None:
                remaining = reset_at - time.time()
                # Pin to the upstream reset point, but never exceed the horizon so
                # a garbled timestamp cannot lock an account out indefinitely.
                if 0 < remaining <= MAX_COOLDOWN_SECONDS:
                    cooldown = max(cooldown, remaining)
            deadline = now + cooldown
            _account_failures[aid] = deadline

        _persist_cooldown(aid, count, time.time() + cooldown, status_code)

        if status_code not in {401, 403}:
            return
        if not structural_auth:
            # Edge-layer rejection: cool down, but never push toward expiry.
            return
        if _note_structural_auth(aid) < EXPIRE_CONFIRMATIONS:
            return
        current = db.get_account(aid)
        if current is None or current.get("status") != "active":
            return
        db.update_account(aid, {"status": "expired"})


def account_is_cooling_down(aid: int) -> bool:
    """Pure read: never pops, never touches the failure streak."""
    with _failure_lock:
        deadline = _account_failures.get(aid)
        if not deadline:
            return False
        return deadline > time.monotonic()


def cooldown_remaining(aid: int) -> float:
    """Seconds until this account may be used again (0 when not cooling)."""
    with _failure_lock:
        deadline = _account_failures.get(aid)
        if not deadline:
            return 0.0
        remaining = deadline - time.monotonic()
        return remaining if remaining > 0 else 0.0


def earliest_cooldown_remaining(account_ids) -> float:
    """Smallest non-zero cooldown across the given accounts (0 if none cooling)."""
    best = None
    for aid in account_ids:
        remaining = cooldown_remaining(aid)
        if remaining > 0 and (best is None or remaining < best):
            best = remaining
    return best or 0.0


def _persist_cooldown(aid: int, count: int, until_epoch: float, status_code: int) -> None:
    """Write-through to SQLite so cooldowns survive a restart. Best-effort."""
    try:
        db.save_account_cooldown(int(aid), int(count), float(until_epoch), int(status_code))
    except Exception:  # noqa: BLE001 - persistence must never break dispatch
        pass


def load_cooldowns_from_db() -> int:
    """Restore cooldown deadlines after a restart. Returns how many were loaded."""
    try:
        rows = db.list_account_cooldowns()
    except Exception:  # noqa: BLE001
        return 0
    now_mono = time.monotonic()
    now_epoch = time.time()
    loaded = 0
    with _failure_lock:
        for row in rows:
            aid = int(row.get("account_id") or 0)
            until_epoch = float(row.get("cooldown_until") or 0.0)
            count = int(row.get("fail_count") or 0)
            if count > 0:
                _failure_counts[aid] = (count, now_mono + FAILURE_COUNT_TTL_SECONDS)
            remaining = until_epoch - now_epoch
            if aid and remaining > 0:
                _account_failures[aid] = now_mono + remaining
                loaded += 1
    return loaded


def reset_cooldown_state() -> None:
    """Test helper: drop all in-memory cooldown bookkeeping."""
    with _failure_lock:
        _account_failures.clear()
        _failure_counts.clear()


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
    if not isinstance(account, dict) or not isinstance(auth, dict):
        meta["reason"] = "account/auth 必须是 JSON 对象"
        return meta
    if not auth.get("accessToken"):
        meta["reason"] = "未发现 accessToken"
        return meta

    uid = account.get("uid", "")
    # 昵称在建号时也可能是 at-rest 信封（国内版），展示前先解出来；解不出来就退回文件名。
    shown, atrest_err = resolve_atrest_fields({
        "nickname": account.get("nickname", ""),
        "phone": account.get("phoneNumber", ""),
        "access_token": auth.get("accessToken", ""),
        "refresh_token": auth.get("refreshToken", ""),
    })
    meta.update({
        "valid": bool(shown.get("access_token")) and not atrest_err,
        "reason": atrest_err or ("ok" if shown.get("access_token") else "未发现可用 accessToken"),
        "account_name": shown.get("nickname") or path.stem,
        "uid_masked": _mask_value(uid),
        "domain": auth.get("domain", DEFAULT_DOMAIN) or DEFAULT_DOMAIN,
        "expires_at": auth.get("expiresAt", 0) if isinstance(auth.get("expiresAt"), (int, float)) else 0,
        "already_imported": bool(uid and uid in existing_uids),
    })
    return meta


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


def _atrest():
    """懒加载 wb_atrest（桌面端 at-rest 信封解密），不可用时返回 None。"""
    try:
        import wb_atrest  # 本机可选模块，仓库里随 wb_switch 一起带
        return wb_atrest
    except Exception:
        return None


def resolve_atrest_fields(values: dict) -> tuple[dict, str]:
    """把桌面端 at-rest 信封（{"$wbEncrypted":1,"envelope":".."}）解成明文。

    非信封字段原样返回；拿到密钥失败时信封字段一律置空，调用方按「无可用凭据」
    处理。★绝不能把信封 dict 交给下游 credential_crypto（会 AttributeError 崩扫描）。
    密钥来源：正在运行的 WorkBuddy 客户端进程内存，回收一次后进程内缓存。
    """
    # 所有出口都只允许字符串进入凭据存储，包括不完整的信封或其他异常 JSON 值。
    plain = {k: v if isinstance(v, str) else "" for k, v in values.items()}
    mod = _atrest()
    if mod is None:
        encrypted = any(isinstance(v, dict) and "$wbEncrypted" in v
                        or isinstance(v, str) and "$wbEncrypted" in v
                        for v in values.values())
        return (
            {k: ("" if isinstance(v, str) and "$wbEncrypted" in v else plain[k])
             for k, v in values.items()},
            "未找到 wb_atrest 模块" if encrypted else "",
        )
    if not any(mod.is_envelope(v) for v in values.values()):
        return plain, ""
    try:
        # wb_atrest 负责唯一的密钥缓存，并在每次使用前校验当前 keyblob。
        key = mod.recover_key()
    except Exception as exc:
        return (
            {k: ("" if mod.is_envelope(v) else plain[k]) for k, v in values.items()},
            f"at-rest 密钥回收失败（客户端没在跑？）: {exc}",
        )
    out = {}
    errors = []
    for name, value in values.items():
        if mod.is_envelope(value):
            try:
                out[name] = mod.decrypt_field(key, value) or ""
            except Exception as exc:
                errors.append(f"{name} 解密失败: {exc}")
                out[name] = ""
        else:
            out[name] = value if isinstance(value, str) else ""
    return out, "; ".join(errors)


def parse_auth_file(path: Path) -> Optional[dict]:
    """解析 auth 文件，返回结构化凭据。"""
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return None

    if not isinstance(data, dict):
        return None
    account = data.get("account", {})
    auth = data.get("auth", {})
    if not isinstance(account, dict) or not isinstance(auth, dict):
        return None
    if not auth.get("accessToken"):
        return None

    # ★2026-10-26: 桌面端自 2026-09-24 起把 accessToken/refreshToken（国内版还有
    # nickname/phoneNumber）写成 at-rest 信封 → 必须先解密，否则下游 encrypt_secret
    # 收到 dict 抛 AttributeError（/admin/accounts/scan 500），或把信封当 token 存进去
    # 导致每个请求 401。
    fields, _err = resolve_atrest_fields(
        {
            "nickname": account.get("nickname", ""),
            "phone": account.get("phoneNumber", ""),
            "access_token": auth.get("accessToken", ""),
            "refresh_token": auth.get("refreshToken", ""),
        }
    )
    if _err or not fields.get("access_token"):
        return None
    session_state = auth.get("sessionState", "")
    if not isinstance(session_state, str):
        session_state = ""

    return {
        "name": fields.get("nickname") or path.stem,
        "uid": account.get("uid", ""),
        "nickname": fields.get("nickname", ""),
        "phone": fields.get("phone", ""),
        "account_type": account.get("type", "personal"),
        "access_token": fields.get("access_token", ""),
        "refresh_token": fields.get("refresh_token", ""),
        "expires_at": auth.get("expiresAt", 0),
        "refresh_expires_at": auth.get("refreshExpiresAt", 0),
        "domain": auth.get("domain", DEFAULT_DOMAIN),
        "enterprise_id": account.get("enterpriseId", ""),
        "session_state": session_state,
    }


def parse_account_payload(data: dict, name: str = "") -> Optional[dict]:
    """Parse nested WorkBuddy auth JSON or its flattened UI representation.

    Fork addition: accepts both the nested ``{"auth":..,"account":..}`` shape and
    the flattened UI shape, and decrypts desktop at-rest envelopes through
    ``resolve_atrest_fields`` (upstream's single decryption seam).
    """
    if not isinstance(data, dict):
        return None
    auth = data.get("auth") if isinstance(data.get("auth"), dict) else {}
    account = data.get("account") if isinstance(data.get("account"), dict) else {}

    def raw(nested: dict, nested_key: str, flat_key: str):
        value = data.get(flat_key)
        if value is None:
            value = nested.get(nested_key)
        return value if value is not None else ""

    def number(nested: dict, nested_key: str, flat_key: str):
        for source, key in ((data, flat_key), (nested, nested_key)):
            value = source.get(key)
            if isinstance(value, (int, float)):
                return value
        return 0

    fields, err = resolve_atrest_fields({
        "access_token": raw(auth, "accessToken", "access_token"),
        "refresh_token": raw(auth, "refreshToken", "refresh_token"),
        "nickname": raw(account, "nickname", "nickname"),
        "phone": raw(account, "phoneNumber", "phone"),
        "domain": raw(auth, "domain", "domain"),
        "enterprise_id": raw(account, "enterpriseId", "enterprise_id"),
        "session_state": raw(auth, "sessionState", "session_state"),
    })
    if err or not fields.get("access_token"):
        return None
    nickname = fields.get("nickname") or ""
    account_type = account.get("type") or data.get("account_type") or "personal"
    if not isinstance(account_type, str):
        account_type = "personal"
    return {
        "name": (name or data.get("name") or "").strip() or nickname,
        "uid": raw(account, "uid", "uid") or "",
        "nickname": nickname,
        "phone": fields.get("phone") or "",
        "account_type": account_type,
        "access_token": fields["access_token"],
        "refresh_token": fields.get("refresh_token") or "",
        "expires_at": number(auth, "expiresAt", "expires_at"),
        "refresh_expires_at": number(auth, "refreshExpiresAt", "refresh_expires_at"),
        "domain": fields.get("domain") or DEFAULT_DOMAIN,
        "enterprise_id": fields.get("enterprise_id") or "",
        "session_state": fields.get("session_state") or "",
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
        clear_auth_confirmations(aid)
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
        clear_auth_confirmations(existing["id"])
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


def account_attempt_limit(provider: str, *, refresh_retry: bool = False) -> int:
    """Budget for the whole channel pool, including refreshable expired accounts.

    Keep the existing minimum for refresh attempts; never cap a larger pool at three.
    TraeWork may retry each account once after renewing its credentials.
    """
    count = sum(a.get("status") in {"active", "expired"}
                for a in db.list_accounts(provider=provider))
    return max(3, count * (2 if refresh_retry else 1))


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
