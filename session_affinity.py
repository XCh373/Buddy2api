"""Single-process, durable account affinity. Never stores client identifiers.

All SQLite work is synchronous and called off the event loop by the dispatcher.
No network or credential refresh is permitted inside this module's locks.
"""
from __future__ import annotations

from collections import OrderedDict
from contextlib import contextmanager
import hashlib
import hmac
import logging
import secrets
import sqlite3
import threading
import time

import credential_crypto
import database as db
from session_identity import SECRET_SETTING, StateUnavailable

_guard = threading.Lock()
_stores = {}
_log = logging.getLogger(__name__)


class Store:
    def __init__(self, path, config):
        self.path = path
        self.lock = threading.RLock()
        self.cache = OrderedDict()
        self.active = {}
        self.active_since = {}
        self.writes = 0
        with self.connection(config) as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT value FROM settings WHERE key=?", (SECRET_SETTING,)).fetchone()
            if row:
                self.secret = credential_crypto.decrypt_secret(row[0], path).encode()
            else:
                self.secret = secrets.token_hex(32).encode()
                conn.execute("INSERT INTO settings(key,value) VALUES (?,?)", (
                    SECRET_SETTING, credential_crypto.encrypt_secret(self.secret.decode(), path)))
                self.writes += 1
            conn.commit()
            rows = conn.execute("SELECT * FROM session_bindings WHERE expires_at>? ORDER BY last_seen_at DESC LIMIT ?",
                                (time.time(), config["capacity"])).fetchall()
            for row in reversed(rows):
                self.cache[row["binding_key"]] = dict(row)

    @contextmanager
    def connection(self, config):
        conn = sqlite3.connect(str(self.path), timeout=config["busy_timeout_ms"] / 1000)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA foreign_keys=ON")
            yield conn
        finally:
            conn.close()

    def key(self, identity):
        return hmac.new(self.secret, identity.digest.encode(), hashlib.sha256).hexdigest()

    def pin(self, identity):
        key = self.key(identity)
        with self.lock:
            if key not in self.active:
                self.active_since[key] = time.time()
            self.active[key] = self.active.get(key, 0) + 1
        return Lease(self, key)

    def invalidate(self, caller=None, account=None):
        with self.lock:
            for key, row in list(self.cache.items()):
                if row["api_key_id"] == caller or row["account_id"] == account:
                    self.cache.pop(key, None)

    def _room(self, conn, key, caller, config, now):
        # Indexed, bounded expiry cleanup; cold admission only, never an SSE event.
        expired = conn.execute("SELECT binding_key FROM session_bindings WHERE expires_at<=? ORDER BY expires_at LIMIT 64", (now,)).fetchall()
        for row in expired:
            other = row["binding_key"]
            if other != key and other not in self.active:
                conn.execute("DELETE FROM session_bindings WHERE binding_key=?", (other,))
                self.cache.pop(other, None)
        if conn.execute("SELECT 1 FROM session_bindings WHERE binding_key=?", (key,)).fetchone():
            return True  # migration reuses its slot, including after capacity is lowered
        total = conn.execute("SELECT count(*) FROM session_bindings").fetchone()[0]
        own = conn.execute("SELECT count(*) FROM session_bindings WHERE api_key_id=?", (caller,)).fetchone()[0]
        if total < config["capacity"] and own < config["per_caller_capacity"]:
            return True
        if own >= config["per_caller_capacity"]:
            cursor = conn.execute("SELECT binding_key,api_key_id FROM session_bindings WHERE api_key_id=? ORDER BY last_seen_at", (caller,))
        else:
            cursor = conn.execute("SELECT binding_key,api_key_id FROM session_bindings ORDER BY last_seen_at")
        evicted = 0
        for row in cursor:
            other = row["binding_key"]
            if other in self.active:
                continue
            conn.execute("DELETE FROM session_bindings WHERE binding_key=?", (other,))
            self.cache.pop(other, None)
            total -= 1
            own -= int(row["api_key_id"] == caller)
            evicted += 1
            if total < config["capacity"] and own < config["per_caller_capacity"]:
                return True
            if evicted == 64:
                break
        return False

    def select(self, identity, eligible, excluded, choose, config):
        """eligible is global availability, before request-local exclusions.

        choose(rows) reuses existing expiry/sticky/order rules without mutation.
        No success/failure callback reassigns bindings: late results cannot roll back.
        """
        if not self.lock.acquire(timeout=config["busy_timeout_ms"] / 1000):
            raise StateUnavailable()
        try:
            key, now = self.key(identity), time.time()
            eligible = eligible() if callable(eligible) else eligible
            if len(self.cache) > config["capacity"]:
                for cached in list(self.cache):
                    if len(self.cache) <= config["capacity"]:
                        break
                    if cached not in self.active:
                        self.cache.pop(cached, None)
            candidates = [a for a in eligible if a["id"] not in excluded]
            if not candidates:
                return None
            highest = max(a.get("priority") or 0 for a in eligible)
            allowed = {a["id"]: a for a in eligible if (a.get("priority") or 0) == highest}

            def retained(row):
                if row is None:
                    return False
                if row["api_key_id"] != identity.caller or row["provider_channel"] != identity.channel or row["generation"] < 1:
                    raise StateUnavailable()
                expiry = min(row["expires_at"], row["last_seen_at"] + config["idle_ttl_seconds"])
                return (expiry > now or self.active_since.get(key, now) < expiry) and row["account_id"] in allowed

            row = self.cache.get(key)
            if retained(row):
                if row["account_id"] in excluded:
                    return choose(candidates)  # request-local bypass, never persist
                self.cache.move_to_end(key)
                if now - row["last_seen_at"] >= config["touch_interval_seconds"]:
                    try:
                        with self.connection(config) as conn:
                            changed = conn.execute("UPDATE session_bindings SET last_seen_at=?, expires_at=? WHERE binding_key=? AND generation=?",
                                                   (now, now + config["idle_ttl_seconds"], key, row["generation"])).rowcount
                            conn.commit()
                        if changed:
                            row.update(last_seen_at=now, expires_at=now + config["idle_ttl_seconds"])
                            self.writes += 1
                    except sqlite3.Error:
                        pass  # existing valid in-memory allocation may continue
                return allowed[row["account_id"]]
            with self.connection(config) as conn:
                conn.execute("BEGIN IMMEDIATE")
                current = conn.execute("SELECT * FROM session_bindings WHERE binding_key=?", (key,)).fetchone()
                current = dict(current) if current else None
                if retained(current):
                    self.cache[key] = current
                    if current["account_id"] in excluded:
                        return choose(candidates)
                    return allowed[current["account_id"]]
                chosen = choose(candidates)
                if not self._room(conn, key, identity.caller, config, now):
                    conn.commit()  # bounded cleanup remains useful after a capacity reduction
                    _log.debug("session_affinity: capacity_bypass")
                    return chosen  # no false binding
                generation = current["generation"] + 1 if current else 1
                row = dict(binding_key=key, api_key_id=identity.caller, provider_channel=identity.channel,
                           account_id=chosen["id"], generation=generation,
                           created_at=current["created_at"] if current else now,
                           last_seen_at=now, expires_at=now + config["idle_ttl_seconds"])
                if current:
                    changed = conn.execute("UPDATE session_bindings SET account_id=?, generation=?, last_seen_at=?, expires_at=? WHERE binding_key=? AND generation=?",
                                           (chosen["id"], generation, now, row["expires_at"], key, current["generation"])).rowcount
                    if changed != 1:
                        raise StateUnavailable()
                else:
                    conn.execute("INSERT INTO session_bindings VALUES (:binding_key,:api_key_id,:provider_channel,:account_id,:generation,:created_at,:last_seen_at,:expires_at)", row)
                conn.commit()
                self.writes += 1
                _log.debug("session_affinity: %s", "migrated" if current else "bound")
                self.cache[key] = row
                while len(self.cache) > config["capacity"]:
                    self.cache.popitem(last=False)
                return chosen
        except (sqlite3.Error, credential_crypto.CredentialCryptoError):
            raise StateUnavailable() from None
        finally:
            self.lock.release()


class Lease:
    def __init__(self, store, key):
        self.store, self.key, self.closed = store, key, False

    def close(self):
        with self.store.lock:
            if not self.closed:
                count = self.store.active.get(self.key, 0)
                if count <= 1:
                    self.store.active.pop(self.key, None)
                    self.store.active_since.pop(self.key, None)
                else:
                    self.store.active[self.key] = count - 1
                self.closed = True

    def __del__(self):
        self.close()


def get_store(config):
    path = db.DB_PATH.resolve()
    if not _guard.acquire(timeout=config["busy_timeout_ms"] / 1000):
        raise StateUnavailable()
    try:
        if path not in _stores:
            try:
                _stores[path] = Store(path, config)
            except Exception:
                raise StateUnavailable() from None
        return _stores[path]
    finally:
        _guard.release()


def invalidate(*, caller=None, account=None):
    with _guard:
        store = _stores.get(db.DB_PATH.resolve())
    if store:
        store.invalidate(caller, account)


def reset_runtime():
    """Drop runtime index; next request restores durable allocations. Not a DB reset."""
    with _guard:
        _stores.clear()
