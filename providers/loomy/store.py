"""Read the official Loomy auth-session.json. Password stays encrypted in extra."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import credential_crypto
import database as db
from providers.loomy.account import new_identity
from providers.loomy.constants import CHANNEL_ID, SESSION_FILENAME, SESSION_SECONDS


def loomy_roots() -> list[Path]:
    roots: list[Path] = []
    explicit = os.environ.get("CB_LOOMY_AUTH_DIR", "").strip()
    if explicit:
        roots.append(Path(explicit).expanduser())
    public = os.environ.get("PUBLIC", "").strip()
    if public:
        roots.append(Path(public) / "Loomy")
    else:
        roots.append(Path(r"C:\Users\Public\Loomy"))
    roots.append(Path.home() / "Loomy")
    seen: set[str] = set()
    out: list[Path] = []
    for item in roots:
        key = str(item)
        if key not in seen:
            seen.add(key)
            out.append(item)
    return out


def _session_files(root: Path) -> list[Path]:
    if root.is_file() and root.name == SESSION_FILENAME:
        return [root]
    if not root.is_dir():
        return []
    direct = root / "userData" / SESSION_FILENAME
    found = [direct] if direct.is_file() else []
    try:
        found.extend(sorted(root.glob(f"*/userData/{SESSION_FILENAME}")))
    except OSError:
        return found
    seen: set[str] = set()
    out: list[Path] = []
    for item in found:
        key = str(item)
        if key not in seen:
            seen.add(key)
            out.append(item)
    return out


def _expires_ms(document: dict) -> int:
    updated = document.get("updatedAt") or document.get("obtainedAt") or 0
    try:
        number = int(updated)
    except (TypeError, ValueError):
        number = 0
    if number and number < 10_000_000_000:
        number *= 1000
    base = number or int(time.time() * 1000)
    return base + SESSION_SECONDS * 1000


def session_to_account(document: dict, *, source: str = "") -> dict:
    if not isinstance(document, dict):
        raise ValueError("Loomy session is not an object")
    session = str(document.get("session") or document.get("access_token") or "").strip()
    userid = str(document.get("userid") or document.get("userId") or document.get("uid") or "").strip()
    phone = str(document.get("phone") or document.get("loginid") or document.get("nickname") or "").strip()
    if not session:
        raise ValueError("Loomy session is missing")
    identity = document.get("identity") if isinstance(document.get("identity"), dict) else new_identity()
    extra = {
        "phone": phone,
        "loginid": str(document.get("loginid") or phone),
        "identity": identity,
        "source": source,
    }
    password = str(document.get("password") or "").strip()
    if password:
        extra["password"] = password
    return {
        "name": phone or userid or "Loomy",
        "nickname": phone or userid or "Loomy",
        "uid": userid or phone,
        "provider": CHANNEL_ID,
        "access_token": session,
        "refresh_token": "",
        "expires_at": _expires_ms(document),
        "status": "active",
        "extra": extra,
    }


def parse_credentials(body: dict) -> dict:
    if not isinstance(body, dict):
        raise ValueError("Loomy credentials must be an object")
    nested = body.get("data") if isinstance(body.get("data"), dict) else {}
    merged = {**nested, **body}
    return session_to_account(merged, source="paste")


def _encrypt_password(parsed: dict, existing: dict | None) -> dict:
    extra = dict(parsed.get("extra") or {})
    password = str(extra.pop("password", "") or "")
    old_extra = existing.get("extra") if isinstance(existing, dict) and isinstance(existing.get("extra"), dict) else {}
    if password:
        extra["password_enc"] = credential_crypto.encrypt_secret(password, db.DB_PATH)
    elif old_extra.get("password_enc"):
        extra["password_enc"] = old_extra["password_enc"]
    parsed = dict(parsed)
    parsed["extra"] = extra
    return parsed


def upsert_account(parsed: dict) -> dict:
    uid = str(parsed.get("uid") or "")
    existing = None
    if uid:
        for row in db.list_accounts(provider=CHANNEL_ID):
            if str(row.get("uid") or "") == uid:
                existing = row
                break
    stored = _encrypt_password(parsed, existing)
    if existing:
        patch = {
            "access_token": stored.get("access_token") or existing.get("access_token"),
            "expires_at": stored.get("expires_at") or existing.get("expires_at"),
            "nickname": stored.get("nickname") or existing.get("nickname"),
            "name": stored.get("name") or existing.get("name"),
            "status": "active",
            "extra": stored.get("extra") or {},
        }
        db.update_account(existing["id"], patch)
        return {"id": existing["id"], "updated": True}
    account_id = db.add_account(stored)
    return {"id": account_id, "updated": False}


def discover() -> dict:
    existing = {str(row.get("uid")) for row in db.list_accounts(provider=CHANNEL_ID) if row.get("uid")}
    dirs = []
    files = []
    for root in loomy_roots():
        exists = root.exists()
        dirs.append({"path": str(root), "exists": exists, "file_count": 0})
        if not exists:
            continue
        found = _session_files(root)
        dirs[-1]["file_count"] = len(found)
        for path in found:
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
        valid = bool(parsed.get("access_token"))
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


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def import_discovered(path: str) -> dict:
    target = Path(path)
    allowed = [item.resolve() for item in loomy_roots() if item.exists()]
    resolved = target.resolve()
    if allowed and not any(_is_relative_to(resolved, root) for root in allowed):
        raise ValueError("path is outside CB_LOOMY_AUTH_DIR / official Loomy data dir")
    payload = json.loads(target.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("Loomy session file is not an object")
    parsed = session_to_account(payload, source=str(resolved))
    extra = parsed.get("extra") if isinstance(parsed.get("extra"), dict) else {}
    extra["auth_path"] = str(resolved)
    parsed["extra"] = extra
    return parsed


def newest_local_session(uid: str) -> dict | None:
    wanted = (uid or "").strip()
    newest = None
    newest_ms = -1
    for root in loomy_roots():
        for path in _session_files(root):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                parsed = session_to_account(payload, source=str(path))
            except Exception:
                continue
            if wanted and str(parsed.get("uid") or "") != wanted:
                continue
            updated = int(parsed.get("expires_at") or 0)
            if updated >= newest_ms:
                newest = parsed
                newest_ms = updated
    return newest
