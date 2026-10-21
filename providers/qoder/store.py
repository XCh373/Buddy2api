"""Read the official Qoder CN desktop login file. Browser login stays on WorkBuddy."""

from __future__ import annotations

import base64
import json
import os
from datetime import datetime, timezone
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

import database as db
from credential_crypto import CredentialCryptoError, _dpapi_decrypt
from providers.qoder.constants import region


def user_data_dir(channel: str = "qoder") -> Path:
    env = "CB_QODER_I_USER_DATA_DIR" if channel == "qoder-i" else "CB_QODER_USER_DATA_DIR"
    override = os.environ.get(env, "").strip()
    if override:
        return Path(override).expanduser()
    appdata = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
    name = "com.qoder.app.stable" if channel == "qoder-i" else "com.qodercn.app.stable"
    return Path(appdata) / name


def auth_file(channel: str = "qoder") -> Path:
    return user_data_dir(channel) / "auth.v1.dat"


def machine_id_file(channel: str = "qoder") -> Path:
    return user_data_dir(channel) / "auth.machine-id"


def _os_crypt_key(folder: Path) -> bytes:
    state_path = folder / "Local State"
    if not state_path.is_file():
        raise CredentialCryptoError("Qoder Local State is missing")
    local_state = json.loads(state_path.read_text(encoding="utf-8"))
    raw = base64.b64decode(((local_state.get("os_crypt") or {}).get("encrypted_key")) or "")
    if not raw.startswith(b"DPAPI"):
        raise CredentialCryptoError("Qoder Local State encrypted_key is not DPAPI")
    return _dpapi_decrypt(raw[5:])


def decrypt_auth_blob(blob: bytes, aes_key: bytes) -> bytes:
    if not blob.startswith(b"v10"):
        raise CredentialCryptoError("Qoder auth.v1.dat is not a v10 blob")
    return AESGCM(aes_key).decrypt(blob[3:15], blob[15:], None)


def encrypt_auth_blob(plain: bytes, aes_key: bytes) -> bytes:
    nonce = os.urandom(12)
    return b"v10" + nonce + AESGCM(aes_key).encrypt(nonce, plain, None)


def iso_to_ms(value) -> int:
    if value in (None, ""):
        return 0
    if isinstance(value, (int, float)):
        number = int(value)
        return number if number > 10_000_000_000 else number * 1000
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return 0
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return int(parsed.timestamp() * 1000)


def read_official_session(channel: str = "qoder") -> dict:
    path = auth_file(channel)
    if not path.is_file():
        raise CredentialCryptoError("Qoder auth.v1.dat is missing")
    key = _os_crypt_key(path.parent)
    payload = json.loads(decrypt_auth_blob(path.read_bytes(), key))
    if not isinstance(payload, dict):
        raise CredentialCryptoError("Qoder auth.v1.dat is not an object")
    user = payload.get("user") if isinstance(payload.get("user"), dict) else {}
    machine = ""
    machine_path = machine_id_file(channel)
    if machine_path.is_file():
        machine = machine_path.read_text(encoding="utf-8").strip()
    token = str(payload.get("token") or payload.get("accessToken") or "").strip()
    refresh = str(payload.get("refreshToken") or "").strip()
    if not token:
        raise CredentialCryptoError("Qoder auth.v1.dat has no token")
    return {
        "access_token": token,
        "refresh_token": refresh,
        "expires_at": iso_to_ms(payload.get("expiresAt")),
        "refresh_expires_at": iso_to_ms(payload.get("refreshTokenExpiresAt")),
        "uid": str(user.get("id") or ""),
        "name": str(user.get("name") or ""),
        "email": str(user.get("email") or ""),
        "phone": str(user.get("phone") or ""),
        "machine_id": machine,
        "raw": payload,
    }


def write_refreshed_auth(channel: str, access_token: str, refresh_token: str, expires_at: int, refresh_expires_at: int) -> None:
    path = auth_file(channel)
    key = _os_crypt_key(path.parent)
    payload = json.loads(decrypt_auth_blob(path.read_bytes(), key))
    payload["token"] = access_token
    if refresh_token:
        payload["refreshToken"] = refresh_token
    if expires_at:
        payload["expiresAt"] = datetime.fromtimestamp(expires_at / 1000, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    if refresh_expires_at:
        payload["refreshTokenExpiresAt"] = datetime.fromtimestamp(
            refresh_expires_at / 1000, timezone.utc
        ).strftime("%Y-%m-%dT%H:%M:%SZ")
    path.write_bytes(encrypt_auth_blob(json.dumps(payload, ensure_ascii=False).encode("utf-8"), key))


def parse_credentials(body: dict, channel: str) -> dict:
    info = region(channel)
    extra_in = body.get("extra") if isinstance(body.get("extra"), dict) else {}
    token = str(body.get("access_token") or body.get("pat") or body.get("token") or "").strip()
    if not token:
        raise ValueError("Qoder 账号需要 access_token 或 PAT")
    uid = str(body.get("uid") or body.get("user_id") or extra_in.get("uid") or "")
    return {
        "name": str(body.get("name") or body.get("nickname") or info["display_name"]),
        "nickname": str(body.get("nickname") or body.get("name") or ""),
        "uid": uid,
        "phone": str(body.get("phone") or ""),
        "provider": channel,
        "access_token": token,
        "refresh_token": str(body.get("refresh_token") or ""),
        "expires_at": int(body.get("expires_at") or 0),
        "refresh_expires_at": int(body.get("refresh_expires_at") or 0),
        "domain": info["origin"].split("://", 1)[-1],
        "status": "active",
        "extra": {
            "email": str(body.get("email") or extra_in.get("email") or ""),
            "machine_id": str(body.get("machine_id") or extra_in.get("machine_id") or ""),
            "machine_token": str(body.get("machine_token") or extra_in.get("machine_token") or ""),
            "region": "i" if channel == "qoder-i" else "cn",
            "auth_path": str(body.get("auth_path") or extra_in.get("auth_path") or ""),
        },
    }


def session_to_account(channel: str, session: dict) -> dict:
    info = region(channel)
    return parse_credentials(
        {
            "access_token": session.get("access_token"),
            "refresh_token": session.get("refresh_token"),
            "expires_at": session.get("expires_at"),
            "refresh_expires_at": session.get("refresh_expires_at"),
            "uid": session.get("uid"),
            "name": session.get("name"),
            "email": session.get("email"),
            "phone": session.get("phone"),
            "machine_id": session.get("machine_id"),
            "auth_path": str(auth_file(channel)),
        },
        channel,
    )


def upsert_account(channel: str, parsed: dict) -> dict:
    uid = str(parsed.get("uid") or "")
    existing = None
    for row in db.list_accounts(provider=channel):
        if uid and str(row.get("uid") or "") == uid:
            existing = row
            break
    if existing:
        db.update_account(existing["id"], {
            "access_token": parsed.get("access_token"),
            "refresh_token": parsed.get("refresh_token") or existing.get("refresh_token"),
            "expires_at": parsed.get("expires_at") or existing.get("expires_at"),
            "refresh_expires_at": parsed.get("refresh_expires_at") or existing.get("refresh_expires_at"),
            "nickname": parsed.get("nickname") or existing.get("nickname"),
            "domain": parsed.get("domain") or existing.get("domain"),
            "status": "active",
            "extra": parsed.get("extra") or existing.get("extra") or {},
        })
        return {"id": existing["id"], "updated": True, "account": parsed}
    account_id = db.add_account(parsed)
    return {"id": account_id, "updated": False, "account": parsed}


def discover(channel: str) -> dict:
    info = region(channel)
    path = auth_file(channel)
    folder = path.parent
    files = []
    message = ""
    if path.is_file():
        try:
            session = read_official_session(channel)
            uid = session.get("uid") or ""
            imported = any(str(row.get("uid") or "") == uid for row in db.list_accounts(provider=channel))
            files.append({
                "path": str(path),
                "name": path.name,
                "account_name": session.get("name") or info["display_name"],
                "uid_masked": (uid[:4] + "…" + uid[-4:]) if len(uid) > 8 else uid,
                "valid": True,
                "already_imported": imported,
                "domain": info["origin"].split("://", 1)[-1],
                "mtime": int(path.stat().st_mtime),
                "size": path.stat().st_size,
                "dir": str(folder),
            })
        except (CredentialCryptoError, OSError, ValueError) as exc:
            message = str(exc)[:180]
            files.append({
                "path": str(path),
                "name": path.name,
                "valid": False,
                "already_imported": False,
                "domain": info["origin"].split("://", 1)[-1],
                "dir": str(folder),
            })
    return {
        "files": files,
        "dirs": [{"path": str(folder), "exists": folder.is_dir(), "file_count": 1 if path.is_file() else 0}],
        "file_count": len(files),
        "valid_count": sum(1 for item in files if item.get("valid")),
        "importable_count": sum(1 for item in files if item.get("valid") and not item.get("already_imported")),
        "channel": channel,
        "message": message,
    }


def import_discovered(channel: str, path: str) -> dict:
    expected = auth_file(channel)
    if Path(path).resolve() != expected.resolve():
        raise ValueError("Qoder 只导入官方 auth.v1.dat")
    return session_to_account(channel, read_official_session(channel))
