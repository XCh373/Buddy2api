import concurrent.futures
import socket
import threading
import time

import pytest

import credential_crypto
import database as db
import session_affinity as affinity
import session_identity as identity


@pytest.fixture
def state(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "synthetic.db")
    monkeypatch.setenv("CB_GATEWAY_MASTER_KEY", "synthetic-affinity-only")
    def forbidden(*args, **kwargs):
        raise AssertionError("Unregistered network access")
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)
    credential_crypto.reset_cache()
    affinity.reset_runtime()
    db.init_db()
    caller = db.add_api_key("synthetic-caller", "synthetic")
    accounts = [db.add_account({"name": str(i), "uid": str(i), "access_token": "synthetic-token",
                               "expires_at": 9999999999999}) for i in range(7)]
    cfg = identity.normalize_config({"enabled": True})
    yield cfg, caller, accounts
    affinity.reset_runtime()
    credential_crypto.reset_cache()


def ident(caller, value="A"):
    return identity.parse([(b"x-buddy2api-session-id", value.encode())], {"id": caller},
                          "workbuddy", "chat", identity.normalize_config({"enabled": True}))


def rows():
    with db.connection() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM session_bindings")]


def test_atomic_first_bind_and_restart(state):
    cfg, caller, aids = state
    store = affinity.get_store(cfg)
    i = ident(caller)
    barrier = threading.Barrier(12)
    eligible = db.get_active_accounts()
    def select(n):
        barrier.wait()
        return store.select(i, eligible, set(), lambda a: a[n % 7], cfg)["id"]
    with concurrent.futures.ThreadPoolExecutor(max_workers=12) as pool:
        selected = list(pool.map(select, range(12)))
    assert len(set(selected)) == 1
    assert len(rows()) == 1 and rows()[0]["generation"] == 1
    affinity.reset_runtime()
    recovered = affinity.get_store(cfg)
    assert recovered.select(i, eligible, set(), lambda a: a[-1], cfg)["id"] == selected[0]
    assert "synthetic-caller" not in str(rows())


def test_priority_migration_local_exclusion_and_late_result(state):
    cfg, caller, aids = state
    store, i = affinity.get_store(cfg), ident(caller)
    choose = lambda a: a[0]
    assert store.select(i, db.get_active_accounts(), set(), choose, cfg)["id"] == aids[0]
    old = rows()[0].copy()
    assert store.select(i, db.get_active_accounts(), {aids[0]}, choose, cfg)["id"] == aids[1]
    assert rows()[0] == old
    db.update_account(aids[1], {"priority": 10})
    assert store.select(i, db.get_active_accounts(), set(), choose, cfg)["id"] == aids[1]
    assert rows()[0]["generation"] == 2
    with db.connection() as conn:
        changed = conn.execute("UPDATE session_bindings SET account_id=? WHERE binding_key=? AND generation=?",
                               (aids[0], old["binding_key"], old["generation"])).rowcount
        conn.commit()
    assert changed == 0
    assert rows()[0]["account_id"] == aids[1]


def test_ttl_capacity_pinning_and_deletion(state, monkeypatch):
    cfg, caller, aids = state
    cfg.update(capacity=1, per_caller_capacity=1, idle_ttl_seconds=60, touch_interval_seconds=10)
    clock = [2000000000.0]
    monkeypatch.setattr(time, "time", lambda: clock[0])
    store = affinity.get_store(cfg)
    a, b = ident(caller), ident(caller, "B")
    select = lambda i: store.select(i, db.get_active_accounts(), set(), lambda a: a[0], cfg)
    select(a)
    lease = store.pin(a)
    select(b)
    assert len(rows()) == 1 and rows()[0]["binding_key"] == store.key(a)
    lease.close()
    select(b)
    assert len(rows()) == 1 and rows()[0]["binding_key"] == store.key(b)
    clock[0] += 61
    lease = store.pin(b)
    select(b)
    assert rows()[0]["generation"] == 2
    lease.close()
    db.delete_account(aids[0])
    assert not rows() and not store.cache
    select(b)
    db.delete_api_key(caller)
    assert not rows() and not store.cache


def test_bounded_write_lock_and_hot_hits(state):
    cfg, caller, aids = state
    store, i = affinity.get_store(cfg), ident(caller)
    eligible = db.get_active_accounts()
    select = lambda value: store.select(value, eligible, set(), lambda a: a[0], cfg)
    select(i)
    writes = store.writes
    with db.connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        assert select(i)["id"] == aids[0]
        start = time.monotonic()
        with pytest.raises(identity.StateUnavailable):
            select(ident(caller, "B"))
        assert time.monotonic() - start < .5
    assert store.writes == writes


def test_secret_change_and_caller_scope(state):
    cfg, caller, aids = state
    store = affinity.get_store(cfg)
    first = store.key(ident(caller))
    other = db.add_api_key("synthetic-other", "other")
    assert first != store.key(ident(other))
    db.set_setting(identity.SECRET_SETTING, credential_crypto.encrypt_secret("new-synthetic-secret", db.DB_PATH))
    affinity.reset_runtime()
    assert first != affinity.get_store(cfg).key(ident(caller))


def test_shortened_ttl_and_corrupt_restored_scope(state, monkeypatch):
    cfg, caller, aids = state
    clock = [2000000000.0]
    monkeypatch.setattr(time, "time", lambda: clock[0])
    store, i = affinity.get_store(cfg), ident(caller)
    select = lambda: store.select(i, db.get_active_accounts(), set(), lambda a: a[0], cfg)
    select()
    clock[0] += 61
    cfg.update(idle_ttl_seconds=60, touch_interval_seconds=10)
    select()
    assert rows()[0]["generation"] == 2
    with db.connection() as conn:
        conn.execute("UPDATE session_bindings SET provider_channel='qoder'")
        conn.commit()
    affinity.reset_runtime()
    store = affinity.get_store(cfg)
    with pytest.raises(identity.StateUnavailable):
        store.select(i, db.get_active_accounts(), set(), lambda a: a[0], cfg)


def test_old_plaintext_key_migration_keeps_binding_foreign_key(tmp_path, monkeypatch):
    import sqlite3
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "legacy.db")
    monkeypatch.setenv("CB_GATEWAY_MASTER_KEY", "synthetic-legacy")
    credential_crypto.reset_cache()
    with sqlite3.connect(db.DB_PATH) as conn:
        conn.execute("CREATE TABLE api_keys (id INTEGER PRIMARY KEY, key TEXT, name TEXT, status TEXT)")
        conn.execute("INSERT INTO api_keys VALUES (1,'synthetic-legacy','legacy','active')")
    db.init_db()
    with db.connection() as conn:
        targets = {r["table"] for r in conn.execute("PRAGMA foreign_key_list(session_bindings)")}
    assert targets == {"api_keys", "accounts"}
    cfg = identity.normalize_config({"enabled": True})
    db.add_account({"name": "synthetic", "access_token": "synthetic"})
    store = affinity.get_store(cfg)
    assert store.select(ident(1), db.get_active_accounts(), set(), lambda a: a[0], cfg)
    affinity.reset_runtime()
    credential_crypto.reset_cache()
