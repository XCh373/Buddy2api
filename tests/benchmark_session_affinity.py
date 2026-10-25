"""Offline selection benchmark: temporary DB, synthetic accounts, no model calls.

python tests/benchmark_session_affinity.py [samples=200] > results.json
"""
import asyncio
import json
import os
from pathlib import Path
import sqlite3
import statistics
import sys
import tempfile
import time

from offline_runner import guard, ROOT


def percentiles(values):
    ordered = sorted(values)
    return {f"p{p}": round(ordered[min(len(ordered) - 1, int((len(ordered) - 1) * p / 100))], 4) for p in (50, 95, 99)}


async def run(folder, count):
    os.environ["CB_GATEWAY_DB_PATH"] = str(Path(folder) / "benchmark.db")
    os.environ["CB_GATEWAY_MASTER_KEY"] = "synthetic-benchmark-only"
    os.environ["CB_AUTH_DIR"] = str(Path(folder) / "empty-auth")
    sys.addaudithook(guard)
    import auth_manager as auth
    import database as db
    import session_affinity as affinity
    import session_identity as identity
    db.init_db()
    caller = db.add_api_key("synthetic-benchmark", "benchmark")
    aids = [db.add_account({"name": str(i), "uid": str(i), "access_token": "synthetic", "expires_at": 9999999999999}) for i in range(7)]
    cfg = identity.normalize_config({"enabled": True})
    db.set_setting(identity.SETTING, cfg)
    def ident(value):
        return identity.parse([(b"x-buddy2api-session-id", value.encode())], {"id": caller}, "workbuddy", "chat", cfg)
    hot = ident("hot")
    await auth.pick_account_with_fallback(affinity=hot)
    namespace = dict(vars(auth))
    exec(compile((ROOT / "tests/legacy_selection_reference.py").read_text(), "legacy_selection_reference.py", "exec"), namespace)
    old = namespace["pick_account_with_fallback"]
    original_connect = sqlite3.connect
    writes = []
    begin_wait_ms = []
    measuring_contention = False
    class ObservedConnection(sqlite3.Connection):
        def execute(self, sql, *args, **kwargs):
            started = time.perf_counter()
            try:
                return super().execute(sql, *args, **kwargs)
            finally:
                if measuring_contention and sql.strip().upper() == "BEGIN IMMEDIATE":
                    begin_wait_ms.append(round((time.perf_counter() - started) * 1000, 4))
    def trace(sql):
        if sql.lstrip().split(" ", 1)[0].upper() in {"INSERT", "UPDATE", "DELETE", "REPLACE"}:
            writes.append(1)
    def connect(*args, **kwargs):
        kwargs.setdefault("factory", ObservedConnection)
        conn = original_connect(*args, **kwargs)
        conn.set_trace_callback(trace)
        return conn
    sqlite3.connect = connect
    async def measure(name, fn, prepare=None):
        latencies, lag = [], []
        done = False
        async def ticker():
            while not done:
                start = time.perf_counter()
                await asyncio.sleep(.001)
                lag.append(max(0, (time.perf_counter() - start - .001) * 1000))
        ticking = asyncio.create_task(ticker())
        sql_writes = 0
        try:
            for i in range(count):
                if prepare:
                    prepare(i)
                before = len(writes)
                start = time.perf_counter()
                await fn(i)
                latencies.append((time.perf_counter() - start) * 1000)
                sql_writes += len(writes) - before
                await asyncio.sleep(0)
        finally:
            done = True
            await ticking
        return {"name": name, "samples": count, "latency_ms": percentiles(latencies),
                "event_loop_lag_ms": {**percentiles(lag), "max": round(max(lag), 4)},
                "sql_write_statements_in_timed_selection": sql_writes}
    output = []
    output.append(await measure("d291cbd_reference", lambda _: old()))
    db.set_setting(identity.SETTING, {**cfg, "enabled": False})
    output.append(await measure("feature_off", lambda _: auth.pick_account_with_fallback()))
    db.set_setting(identity.SETTING, cfg)
    output.append(await measure("enabled_no_identity", lambda _: auth.pick_account_with_fallback()))
    output.append(await measure("affinity_hot", lambda _: auth.pick_account_with_fallback(affinity=hot)))
    output.append(await measure("affinity_new", lambda i: auth.pick_account_with_fallback(affinity=ident(f"new-{i}"))))
    def migrate(i):
        # Reset through direct SQL outside timed selection to force a different highest priority.
        with db.connection() as conn:
            conn.execute("UPDATE accounts SET priority=0")
            conn.execute("UPDATE accounts SET priority=10 WHERE id=?", (aids[(i + 1) % 7],))
            conn.commit()
    output.append(await measure("affinity_migration", lambda _: auth.pick_account_with_fallback(affinity=hot), migrate))
    start = time.perf_counter()
    blocked = False
    with db.connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        start = time.perf_counter()
        measuring_contention = True
        try:
            await auth.pick_account_with_fallback(affinity=ident("locked-new"))
        except identity.StateUnavailable:
            blocked = True
        finally:
            measuring_contention = False
        lock_ms = (time.perf_counter() - start) * 1000
    sqlite3.connect = original_connect
    return {"baseline": "d291cbdb7817b9cd0ed6e26a58da986fcf60eef2", "accounts": 7,
            "scope": "local selector only; no inference latency or cache-savings claim", "network_calls": 0,
            "measurements": output, "sqlite_contention": {"configured_busy_timeout_ms": cfg["busy_timeout_ms"],
            "begin_immediate_call_ms_including_lock_wait": begin_wait_ms,
            "observed_total_selection_ms": round(lock_ms, 4), "failed_before_upstream": blocked}}


if __name__ == "__main__":
    with tempfile.TemporaryDirectory(prefix="buddy-affinity-benchmark-") as folder:
        print(json.dumps(asyncio.run(run(folder, int(sys.argv[1]) if len(sys.argv) > 1 else 200)), indent=2))
