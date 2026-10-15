"""Read-only WorkBuddy routing policy and explanations."""

from __future__ import annotations

import logging
import math
import sqlite3
import time

import database as db

SETTING = "workbuddy_routing"
DEFAULTS = {
    "mode": "legacy",
    "expiry_window_days": 30,
    "cache_max_age_seconds": 300,
    "refresh_interval_seconds": 120,
}
_log = logging.getLogger(__name__)


def normalize_config(value: dict | None) -> dict:
    if value is None:
        return dict(DEFAULTS)
    if not isinstance(value, dict):
        raise ValueError("workbuddy_routing must be an object")
    if set(value) - set(DEFAULTS):
        raise ValueError("Unsupported workbuddy_routing fields")
    result = {**DEFAULTS, **value}
    if result["mode"] not in ("legacy", "expiry_first"):
        raise ValueError("routing mode must be legacy or expiry_first")
    for key, low, high in (
        ("expiry_window_days", 1, 30),
        ("cache_max_age_seconds", 60, 1800),
        ("refresh_interval_seconds", 30, 600),
    ):
        if type(result[key]) is not int or not low <= result[key] <= high:
            raise ValueError(f"{key} must be an integer between {low} and {high}")
    if result["refresh_interval_seconds"] >= result["cache_max_age_seconds"]:
        raise ValueError("refresh_interval_seconds must be less than cache_max_age_seconds")
    return result


def load_config() -> dict:
    try:
        return normalize_config(db.get_setting(SETTING))
    except (ValueError, TypeError, sqlite3.Error):
        _log.warning("Invalid or unavailable WorkBuddy routing settings; using legacy routing")
        return dict(DEFAULTS)


def _number(value) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        return float(value) if math.isfinite(value) else None
    except OverflowError:
        return None


def credit_summary(account: dict, snapshot: dict | None, config: dict, now: float) -> dict:
    result = {"reason": "no_snapshot", "trusted": False, "snapshot_at": None,
              "age_seconds": None, "expire_ts": None, "effective_remaining": 0.0}
    if snapshot is None:
        return result
    if not isinstance(snapshot, dict):
        return {**result, "reason": "invalid_snapshot"}
    stamp = _number(snapshot.get("cache_updated_at", snapshot.get("updated_at")))
    if stamp is None or stamp <= 0 or stamp > now or not isinstance(snapshot.get("packages"), list):
        return {**result, "reason": "invalid_snapshot"}
    result.update(snapshot_at=stamp, age_seconds=now - stamp)
    if snapshot.get("route_last_attempt_ok") is False or snapshot.get("stale"):
        return {**result, "reason": "refresh_failed"}
    if snapshot.get("ok") is not True:
        return {**result, "reason": "invalid_snapshot"}
    if now - stamp > config["cache_max_age_seconds"]:
        return {**result, "reason": "cache_expired"}
    if "route_usage_baseline" not in snapshot or "route_last_attempt_ok" not in snapshot:
        return {**result, "reason": "legacy_snapshot"}
    if snapshot["route_last_attempt_ok"] is not True:
        return {**result, "reason": "invalid_snapshot"}
    baseline = _number(snapshot.get("route_usage_baseline"))
    used = _number(account.get("total_credits"))
    if baseline is None or used is None or baseline < 0 or used < baseline:
        return {**result, "reason": "invalid_snapshot"}
    delta = used - baseline
    result["trusted"] = True
    eligible = []
    had_credit = False
    for package in snapshot["packages"]:
        if not isinstance(package, dict):
            continue
        remaining = _number(package.get("remaining_precise"))
        expiry = _number(package.get("expire_ts"))
        unit = str(package.get("capacity_unit") or "credit").strip().lower()
        if unit not in {"credit", "credits", "point", "points", "积分"}:
            continue
        if remaining is None or remaining <= 0 or expiry is None:
            continue
        if not now < expiry <= now + config["expiry_window_days"] * 86400:
            continue
        inactive_period = False
        for field in ("cycle_start_ts", "deduction_start_ts", "cycle_end_ts", "deduction_end_ts"):
            raw = package.get(field)
            if raw is None:
                continue
            boundary = _number(raw)
            if boundary is None or boundary <= 0:
                inactive_period = True
                break
            if ("start" in field and boundary > now) or ("end" in field and boundary <= now):
                inactive_period = True
                break
        if inactive_period:
            continue
        had_credit = True
        # Actual package deduction is upstream-owned: subtract all local usage conservatively.
        amount = max(0.0, remaining - delta)
        if amount > 0:
            eligible.append((expiry, amount))
    if not eligible:
        return {**result, "reason": "local_usage_exhausted" if had_credit else "no_expiring_credit"}
    earliest = min(expiry for expiry, _ in eligible)
    return {**result, "reason": "expiry_candidate", "expire_ts": earliest,
            "effective_remaining": round(sum(amount for expiry, amount in eligible if expiry == earliest), 4)}


def best_group(accounts: list[dict], config: dict, snapshots: dict[int, dict] | None = None,
               now: float | None = None) -> tuple[list[dict], dict[int, dict]]:
    if not accounts:
        return [], {}
    highest = max(_integer(a.get("priority")) for a in accounts)
    top = [a for a in accounts if _integer(a.get("priority")) == highest]
    if config["mode"] == "legacy":
        return top, {}
    now = time.time() if now is None else now
    summaries = {a["id"]: credit_summary(a, (snapshots or {}).get(a["id"]), config, now) for a in top}
    expiring = [a for a in top if summaries[a["id"]]["expire_ts"] is not None]
    if not expiring:
        return top, summaries
    earliest = min(summaries[a["id"]]["expire_ts"] for a in expiring)
    return [a for a in expiring if summaries[a["id"]]["expire_ts"] == earliest], summaries


def _integer(value, default=0) -> int:
    try:
        return int(value) if value is not None else default
    except (ValueError, TypeError):
        return default


def read_snapshots(accounts: list[dict]) -> dict[int, dict]:
    try:
        return db.get_account_resource_caches([a["id"] for a in accounts])
    except sqlite3.Error:
        _log.warning("Resource cache unavailable; using legacy account order")
        return {}
