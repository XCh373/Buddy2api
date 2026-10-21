"""Request headers sent to the Xiaohuanxiong web API."""

from __future__ import annotations

import os

from providers.xiaohuanxiong.constants import (
    CLIENT_PLATFORM,
    CLIENT_VERSION,
    ORIGIN,
    USER_AGENT,
)


def user_agent() -> str:
    return (os.environ.get("CB_XHX_USER_AGENT") or "").strip() or USER_AGENT


def client_headers(token: str = "", *, json_body: bool = False) -> dict[str, str]:
    headers = {
        "Accept": "application/json",
        "Origin": ORIGIN,
        "Referer": ORIGIN + "/",
        "User-Agent": user_agent(),
        "X-Client-Platform": CLIENT_PLATFORM,
        "X-Client-Version": CLIENT_VERSION,
        "X-Raccoon-Language": "zh",
    }
    if json_body:
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers
