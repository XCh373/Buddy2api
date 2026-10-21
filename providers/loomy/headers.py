"""Model-gateway request headers. Device identity stays in the login envelope."""

from __future__ import annotations

import os
import uuid

from providers.loomy.constants import LOOMY_VERSION


def loomy_version() -> str:
    return (os.environ.get("CB_LOOMY_VERSION") or "").strip() or LOOMY_VERSION


def model_headers(session: str, *, purpose: str = "chat.message") -> dict[str, str]:
    token = (session or "").strip()
    trace = uuid.uuid4().hex
    span = uuid.uuid4().hex[:16]
    return {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Authorization": f"Bearer {token}",
        "token": token,
        "traceparent": f"00-{trace}-{span}-01",
        "loomy-version": loomy_version(),
        "X-Loomy-Request-Purpose": purpose,
    }
