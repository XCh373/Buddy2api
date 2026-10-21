"""Read the official desktop auth.json. Tokens stay in the encrypted account columns."""

from __future__ import annotations

import json
import os
from pathlib import Path

import database as db
from providers.xiaohuanxiong.constants import AUTH_FILENAME, CHANNEL_ID
from providers.xiaohuanxiong.token import jwt_exp_ms


def auth_candidates() -> list[Path]:
    paths: list[Path] = []
    explicit = (
        os.environ.get("CB_XHX_AUTH_DIR", "").strip()
        or os.environ.get("CB_XIAOHUANXIONG_AUTH_DIR", "").strip()
    )
    if explicit:
        target = Path(explicit).expanduser()
        paths.append(target if target.suffix.lower() == ".json" else target / AUTH_FILENAME)
    paths.append(Path.home() / ".box-agent" / "config" / AUTH_FILENAME)
    local = os.environ.get("LOCALAPPDATA", "").strip()
    if local:
        paths.append(Path(local) / "box-agent" / "config" / AUTH_FILENAME)
        paths.append(Path(local) / ".box-agent" / "config" / AUTH_FILENAME)
    seen: set[str] = set()
    out: list[Path] = []
    for item in paths:
        key = str(item)
        if key not in seen:
            seen.add(key)
            out.append(item)
    return out


def session_to_account(document: dict, *, source: str = "") -> dict:
    if not isinstance(document, dict):
        raise ValueError("Xiaohuanxiong auth is not an object")
    nested = document.get("data") if isinstance(document.get("data"), dict) else {}
    access = str(
        document.get("access_token")
        or document.get("accessToken")
        or nested.get("access_token")
        or ""
    ).strip()
    refresh = str(
        document.get("refresh_token")
        or document.get("refreshToken")
        or nested.get("refresh_token")
        or ""
    ).strip()
    if access.lower().startswith("bearer "):
        access = access[7:].strip()
    uid = str(document.get("user_id") or document.get("uid") or document.get("id") or nested.get("user_id") or "").strip()
    name = str(document.get("name") or document.get("nickname") or document.get("office_org_name") or uid or "小浣熊")
    if not access and not refresh:
        raise ValueError("Xiaohuanxiong auth is missing access_token and refresh_token")
    expires = document.get("expires_at") or document.get("expiresAt") or jwt_exp_ms(access)
    try:
        expires_at = int(expires or 0)
    except (TypeError, ValueError):
        expires_at = 0
    if expires_at and expires_at < 10_000_000_000:
        expires_at *= 1000
    return {
        "name": name,
        "nickname": name,
        "uid": uid or name,
        "provider": CHANNEL_ID,
        "access_token": access,
        "refresh_token": refresh,
        "expires_at": expires_at,
        "status": "active",
        "extra": {"source": source},
    }


def parse_credentials(body: dict) -> dict:
    return session_to_account(body, source="paste")


def upsert_account(parsed: dict) -> dict:
    uid = str(parsed.get("uid") or "")
    existing = None
    if uid:
        for row in db.list_accounts(provider=CHANNEL_ID):
            if str(row.get("uid") or "") == uid:
                existing = row
                break
    if existing:
        patch = {
            "access_token": parsed.get("access_token") or existing.get("access_token"),
            "refresh_token": parsed.get("refresh_token") or existing.get("refresh_token"),
            "expires_at": parsed.get("expires_at") or existing.get("expires_at"),
            "nickname": parsed.get("nickname") or existing.get("nickname"),
            "status": "active",
            "extra": parsed.get("extra") or existing.get("extra") or {},
        }
        db.update_account(existing["id"], patch)
        return {"id": existing["id"], "updated": True}
    return {"id": db.add_account(parsed), "updated": False}


def discover() -> dict:
    existing = {str(row.get("uid")) for row in db.list_accounts(provider=CHANNEL_ID) if row.get("uid")}
    dirs = []
    files = []
    seen_dirs: set[str] = set()
    for path in auth_candidates():
        folder = path.parent
        key = str(folder)
        if key not in seen_dirs:
            seen_dirs.add(key)
            dirs.append({"path": key, "exists": folder.is_dir(), "file_count": 0})
        if not path.is_file():
            continue
        dirs[-1]["file_count"] = int(dirs[-1]["file_count"]) + 1
        files.append(_file_meta(path, existing))
    return {
        "dirs": dirs,
        "files": files,
        "file_count": len(files),
        "valid_count": sum(1 for item in files if item.get("valid")),
        "importable_count": sum(1 for item in files if item.get("valid") and not item.get("already_imported")),
        "channel": CHANNEL_ID,
    }


def _file_meta(path: Path, existing: set[str]) -> dict:
    reason = ""
    valid = False
    uid = ""
    name = path.name
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        parsed = session_to_account(payload, source=str(path))
        valid = bool(parsed.get("access_token") or parsed.get("refresh_token"))
        uid = str(parsed.get("uid") or "")
        name = parsed.get("nickname") or name
    except Exception as exc:
        reason = str(exc)[:160]
    masked = (uid[:6] + "…") if len(uid) > 6 else uid
    return {
        "channel": CHANNEL_ID,
        "path": str(path),
        "valid": valid,
        "reason": reason,
        "account_name": name,
        "uid_masked": masked,
        "already_imported": bool(uid and uid in existing),
    }


def import_discovered(path: str) -> dict:
    target = Path(path)
    allowed = [item.resolve().parent for item in auth_candidates()]
    resolved = target.resolve()
    if not any(_under(resolved, root) for root in allowed):
        raise ValueError("path is outside the Xiaohuanxiong auth dir")
    payload = json.loads(target.read_text(encoding="utf-8"))
    parsed = session_to_account(payload, source=str(resolved))
    extra = parsed.get("extra") if isinstance(parsed.get("extra"), dict) else {}
    extra["auth_path"] = str(resolved)
    parsed["extra"] = extra
    return parsed


def _under(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False
