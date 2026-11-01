"""Refresh Xiaohuanxiong access tokens."""

from __future__ import annotations

import base64
import json
import time

import httpx

import database as db
from providers.xiaohuanxiong.constants import AUTH_BASE, REFRESH_MARGIN_SECONDS
from providers.xiaohuanxiong.headers import client_headers


class XiaohuanxiongAuthError(RuntimeError):
    pass


def jwt_payload(token: str) -> dict:
    try:
        parts = (token or "").split(".")
        if len(parts) < 2:
            return {}
        payload = parts[1] + "=" * (-len(parts[1]) % 4)
        data = json.loads(base64.urlsafe_b64decode(payload.encode("ascii")))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def jwt_exp_ms(token: str) -> int:
    try:
        exp = int(jwt_payload(token).get("exp") or 0)
    except (TypeError, ValueError):
        return 0
    if exp <= 0:
        return 0
    return exp * 1000 if exp < 10_000_000_000 else exp


def jwt_sub(token: str) -> str:
    return str(jwt_payload(token).get("sub") or "").strip()


def is_token_expired(account: dict, skew_seconds: int = REFRESH_MARGIN_SECONDS) -> bool:
    expires_at = int(account.get("expires_at") or 0) or jwt_exp_ms(str(account.get("access_token") or ""))
    if expires_at <= 0:
        return False
    return time.time() * 1000 >= expires_at - skew_seconds * 1000


async def refresh_account(account: dict) -> dict:
    refresh = str(account.get("refresh_token") or "").strip()
    if not refresh:
        if account.get("access_token") and not is_token_expired(account, skew_seconds=0):
            return account
        raise XiaohuanxiongAuthError("小浣熊账号没有 refresh_token，请重新导入 auth.json")
    body = json.dumps({"refresh_token": refresh}, separators=(",", ":"))
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                f"{AUTH_BASE}/refresh",
                headers=client_headers(json_body=True),
                content=body.encode("utf-8"),
            )
    except httpx.HTTPError as exc:
        raise XiaohuanxiongAuthError(f"刷新 token 请求失败: {exc}") from exc
    try:
        payload = response.json()
    except ValueError as exc:
        raise XiaohuanxiongAuthError(f"刷新 token 失败: HTTP {response.status_code}") from exc
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    code = payload.get("code")
    if response.status_code >= 400 or (code not in (None, 0)):
        message = payload.get("message") or payload.get("msg") or response.text[:180]
        raise XiaohuanxiongAuthError(f"刷新 token 失败: HTTP {response.status_code} {message}")
    access = str(data.get("access_token") or "").strip()
    rotated = str(data.get("refresh_token") or "").strip() or refresh
    if not access:
        raise XiaohuanxiongAuthError("刷新 token 失败: 上游未返回 access_token")
    patch = {
        "access_token": access,
        "refresh_token": rotated,
        "expires_at": jwt_exp_ms(access),
        "status": "active",
    }
    db.update_account(int(account["id"]), patch)
    return db.get_account(account["id"]) or {**account, **patch}
