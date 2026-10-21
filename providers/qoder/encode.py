"""Encode a Qoder chat body before COSY signing.

The gateway restores the bytes with the inverse shuffle. Signing must use
the encoded bytes, not the original JSON.
"""

from __future__ import annotations

import base64

_STD = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/"
_CUSTOM = "_doRTgHZBKcGVjlvpC,@aFSx#DPuNJme&i*MzLOEn)sUrthbf%Y^w.(kIQyXqWA!"
_TABLE = {ord(src): ord(dst) for src, dst in zip(_STD, _CUSTOM)}
_TABLE[ord("=")] = ord("$")


def encode_body(payload: bytes) -> bytes:
    text = base64.b64encode(payload)
    length = len(text)
    third = length // 3
    ordered = text[length - third :] + text[third : length - third] + text[:third]
    return bytes(_TABLE.get(byte, byte) for byte in ordered)
