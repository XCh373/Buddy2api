"""Account-service HMAC-SHA1. Chat requests do not use this signature."""

from __future__ import annotations

import base64
import hashlib
import hmac
import uuid
from email.utils import formatdate
from typing import Mapping, Optional
from urllib.parse import quote

_EXTRA_ESCAPES = {"!": "%21", "'": "%27", "(": "%28", ")": "%29", "*": "%2A"}


def content_md5(body: str) -> str:
    if not body:
        return ""
    return base64.b64encode(hashlib.md5(body.encode("utf-8")).digest()).decode()


def _escape(value: str) -> str:
    out = quote(str(value), safe="-_.~")
    for raw, enc in _EXTRA_ESCAPES.items():
        out = out.replace(raw, enc)
    return out


def _escaped_path(path: str) -> str:
    text = path if path.startswith("/") else "/" + path
    if len(text) > 1 and text.endswith("/"):
        text = text[:-1]
    return "/".join(_escape(seg) if seg else "" for seg in text.split("/"))


def _escaped_query(params: Mapping[str, object] | None) -> str:
    if not params:
        return ""
    return "&".join(
        f"{_escape(key)}={_escape('' if value is None else value)}"
        for key, value in params.items()
    )


def _canonicalized(headers: Mapping[str, object] | None, prefix: str = "x-") -> tuple[str, str]:
    picked = {
        key.lower(): str(value).strip()
        for key, value in (headers or {}).items()
        if key.lower().startswith(prefix)
    }
    if not picked:
        return "", ""
    keys = sorted(picked)
    canonical = "".join(f"{key}:{picked[key]}\n" for key in keys)
    return ";".join(keys), canonical.rstrip("\n")


def string_to_sign(
    *,
    method: str,
    path: str,
    query: Optional[Mapping] = None,
    body: str = "",
    content_type: str = "application/json",
    date: str,
    nonce: str,
    custom_headers: Optional[Mapping] = None,
) -> str:
    signed_headers, canonical = _canonicalized(custom_headers)
    return "\n".join(
        [
            method.upper(),
            _escaped_path(path),
            _escaped_query(query),
            content_md5(body),
            content_type,
            date,
            nonce,
            signed_headers,
            canonical,
        ]
    )


def sign(secret: str, **kwargs) -> str:
    material = string_to_sign(**kwargs)
    digest = hmac.new(secret.encode("utf-8"), material.encode("utf-8"), hashlib.sha1).digest()
    return base64.b64encode(digest).decode()


def build_headers(access_key_id: str, access_key_secret: str, *, method: str, path: str, body: str) -> dict[str, str]:
    date = formatdate(usegmt=True)
    nonce = str(uuid.uuid4())
    signature = sign(
        access_key_secret,
        method=method,
        path=path,
        body=body,
        date=date,
        nonce=nonce,
    )
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Date": date,
        "Nonce": nonce,
        "Authorization": f"account {access_key_id}:{signature}",
    }
    digest = content_md5(body)
    if digest:
        headers["Content-MD5"] = digest
    return headers
