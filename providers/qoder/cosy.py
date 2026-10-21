"""Qoder COSY headers. Reuses the desktop RSA envelope with Qoder's version."""

from __future__ import annotations

import hashlib
import time

from providers.qoder.constants import (
    CLIENT_IP,
    CLIENT_TYPE,
    COSY_VERSION,
    DATA_POLICY,
    LOGIN_VERSION,
    MACHINE_TYPE,
)
from providers.qwenwork.cosy import encrypt_user_info, generate_auth_token


def auth_headers(
    *,
    url: str,
    body: bytes,
    uid: str,
    name: str,
    email: str,
    access_token: str,
    machine_id: str,
) -> dict[str, str]:
    material = encrypt_user_info(
        uid=uid, name=name, email=email, access_token=access_token
    )
    signed = generate_auth_token(
        material,
        url=url,
        body=body.decode("ascii"),
        timestamp=int(time.time()),
        cosy_version=COSY_VERSION,
        ide_version="",
    )
    headers = {
        "Authorization": signed["Authorization"],
        "Cosy-Key": signed["Cosy-Key"],
        "Cosy-User": uid,
        "Cosy-Date": signed["Cosy-Date"],
        "Cosy-Version": COSY_VERSION,
        "Cosy-Machineid": machine_id or "buddy2api",
        "Cosy-Machinetoken": machine_id or "buddy2api",
        "Cosy-Machinetype": MACHINE_TYPE,
        "Cosy-Machineos": "x86_64_win32",
        "Cosy-Clienttype": CLIENT_TYPE,
        "Cosy-Clientip": CLIENT_IP,
        "Cosy-Bodyhash": hashlib.md5(body).hexdigest(),
        "Cosy-Bodylength": str(len(body)),
        "Cosy-Sigpath": signed["sign_str"].rsplit("\n", 1)[-1],
        "Cosy-Data-Policy": DATA_POLICY,
        "Login-Version": LOGIN_VERSION,
        "X-Request-Id": signed["request_id"],
        "Accept": "text/event-stream",
        "Content-Type": "application/json",
        "Cache-Control": "no-cache",
        "User-Agent": "Qoder",
    }
    return headers
