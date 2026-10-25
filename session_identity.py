"""Opt-in client identity contracts. No upstream fields or message inference."""
from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import re

import database as db

SETTING = "session_affinity"
SECRET_SETTING = "_session_affinity_secret"
CHANNELS = {"workbuddy", "workbuddy-i"}
NATIVE = "pi-ai-responses-session-v1"
CODEX_NATIVE = "codex-responses-session-v1"
DEFAULTS = dict(enabled=False, idle_ttl_seconds=86400, capacity=10000,
                per_caller_capacity=1000, touch_interval_seconds=60,
                busy_timeout_ms=25, profiles={})
_VALUE = re.compile(r"[A-Za-z0-9._~-]+", re.ASCII)


class IdentityError(ValueError):
    def __init__(self):
        super().__init__("Invalid session identity")


class StateUnavailable(RuntimeError):
    def __init__(self):
        super().__init__("Session affinity state unavailable")


@dataclass(frozen=True)
class Identity:
    caller: int
    channel: str
    digest: str = field(repr=False)


def normalize_config(value):
    if value is None:
        return {**DEFAULTS, "profiles": {}}
    if not isinstance(value, dict) or set(value) - set(DEFAULTS):
        raise ValueError("Invalid session_affinity configuration")
    cfg = {**DEFAULTS, **value}
    if type(cfg["enabled"]) is not bool:
        raise ValueError("enabled must be boolean")
    for name, low, high in (("idle_ttl_seconds", 60, 2592000), ("capacity", 1, 100000),
                            ("per_caller_capacity", 1, 100000),
                            ("touch_interval_seconds", 1, 3600), ("busy_timeout_ms", 1, 100)):
        if type(cfg[name]) is not int or not low <= cfg[name] <= high:
            raise ValueError(f"Invalid {name}")
    if cfg["per_caller_capacity"] > cfg["capacity"] or cfg["touch_interval_seconds"] >= cfg["idle_ttl_seconds"]:
        raise ValueError("Invalid affinity capacity or touch interval")
    profiles = cfg["profiles"]
    if not isinstance(profiles, dict):
        raise ValueError("profiles must be an object")
    for key, profile in profiles.items():
        if not str(key).isdigit() or not isinstance(profile, dict) or set(profile) - {"namespace", "namespaces", "native"}:
            raise ValueError("Invalid affinity profile")
        if profile.get("native") not in (None, NATIVE, CODEX_NATIVE):
            raise ValueError("Unsupported native identity adapter")
        namespace = profile.get("namespace", "default")
        namespaces = profile.get("namespaces", [namespace])
        if not valid(namespace, 64) or not isinstance(namespaces, list) or not namespaces or namespace not in namespaces or not all(valid(n, 64) for n in namespaces):
            raise ValueError("Invalid affinity namespace")
    return cfg


def valid(value, limit=256):
    return isinstance(value, str) and 1 <= len(value) <= limit and _VALUE.fullmatch(value) is not None


def load_config():
    try:
        return normalize_config(db.get_setting(SETTING))
    except Exception:
        raise StateUnavailable() from None


def parse(headers, key_info, channel, protocol, config):
    """headers is the raw ASGI list: duplicate values must not be collapsed."""
    if not config["enabled"] or not key_info or channel not in CHANNELS:
        return None
    profile = config["profiles"].get(str(key_info["id"]), {})
    values = {}
    for name, value in headers:
        name = name.decode("latin-1").lower()
        if name in {"x-buddy2api-session-id", "x-buddy2api-session-namespace", "session_id", "session-id", "thread-id"}:
            values.setdefault(name, []).append(value.decode("latin-1"))
    explicit = values.get("x-buddy2api-session-id")
    ns_values = values.get("x-buddy2api-session-namespace")
    namespace = profile.get("namespace", "default")
    if ns_values:
        if not explicit or len(ns_values) != 1 or not valid(ns_values[0], 64) or ns_values[0] not in profile.get("namespaces", [namespace]):
            raise IdentityError()
        namespace = ns_values[0]
    if explicit is not None:
        if len(explicit) != 1 or not valid(explicit[0]):
            raise IdentityError()
        value, domain, scope = explicit[0], "explicit-v1", "conversation-v1"
    elif protocol == "responses" and profile.get("native") == NATIVE:
        native = values.get("session_id", [])
        if len(native) != 1 or not valid(native[0]):
            return None
        value, domain, scope = native[0], NATIVE, "responses-v1"
    elif protocol == "responses" and profile.get("native") == CODEX_NATIVE:
        native, thread = values.get("session-id", []), values.get("thread-id")
        if len(native) != 1 or not valid(native[0]):
            return None
        # Verified CLI sends the same logical thread in both fields. A conflicting
        # mirror is unknown identity, never an invitation to choose one at random.
        if thread is not None and thread != native:
            return None
        value, domain, scope = native[0], CODEX_NATIVE, "responses-v1"
    else:
        return None
    canonical = json.dumps([1, key_info["id"], namespace, domain, scope, channel, value], separators=(",", ":"))
    return Identity(int(key_info["id"]), channel, hashlib.sha256(canonical.encode()).hexdigest())


def from_request(request, key_info, channel, protocol):
    if not key_info or channel not in CHANNELS:
        return None
    return parse(request.scope["headers"], key_info, channel, protocol, load_config())


def state_error():
    return ("error", (503, {"error": {"type": "server_error", "code": "affinity_state_unavailable",
                                    "message": "Session affinity state unavailable"}}))
