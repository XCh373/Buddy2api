"""BUD-005 冷却重构的回归测试。

核心断言：**冷却到期不得清空失败计数**。旧实现把计数和冷却塞在同一条记录里，
冷却到期 `pop()` 时把计数一起带走，导致 30→60→120→240→300 的阶梯永远停在 30s。
"""

import pathlib
import time

import pytest

import auth_manager
import credential_crypto
import database as db


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "cooldown.db")
    monkeypatch.setenv("CB_GATEWAY_MASTER_KEY", "pytest-cooldown-key")
    credential_crypto.reset_cache()
    auth_manager.reset_cooldown_state()
    auth_manager.reset_auth_confirmations()
    auth_manager.reset_account_state_locks()
    db.init_db()
    yield
    auth_manager.reset_cooldown_state()
    credential_crypto.reset_cache()


def _add(**kw):
    base = {
        "name": "acc", "uid": "uid-1", "access_token": "token",
        "refresh_token": "refresh", "expires_at": 9999999999999,
        "domain": "www.codebuddy.cn", "provider": "workbuddy", "status": "active",
    }
    base.update(kw)
    return db.add_account(base)


def test_ladder_survives_cooldown_expiry(isolated_db, monkeypatch):
    """核心回归：到期后计数仍在，阶梯继续升档（30→60→120→240→300）。"""
    aid = _add()
    clock = [1000.0]
    monkeypatch.setattr(auth_manager.time, "monotonic", lambda: clock[0])

    expected = [30, 60, 120, 240, 300]
    for step in expected:
        auth_manager.mark_account_failure(aid, 429)
        assert auth_manager.cooldown_remaining(aid) == pytest.approx(step, abs=0.5)
        # 模拟冷却自然到期，随后有人查询状态（旧实现正是在这里清空计数）
        clock[0] += step + 1
        assert auth_manager.account_is_cooling_down(aid) is False
        assert aid in auth_manager._failure_counts, "冷却到期不应删除失败计数"


def test_is_cooling_down_is_pure_read(isolated_db, monkeypatch):
    """account_is_cooling_down 不得产生副作用。"""
    aid = _add()
    clock = [1000.0]
    monkeypatch.setattr(auth_manager.time, "monotonic", lambda: clock[0])
    auth_manager.mark_account_failure(aid, 429)
    clock[0] += 31
    for _ in range(3):
        assert auth_manager.account_is_cooling_down(aid) is False
    assert aid in auth_manager._failure_counts


def test_success_is_the_only_reset(isolated_db, monkeypatch):
    aid = _add()
    clock = [1000.0]
    monkeypatch.setattr(auth_manager.time, "monotonic", lambda: clock[0])
    auth_manager.mark_account_failure(aid, 429)
    auth_manager.mark_account_failure(aid, 429)
    assert auth_manager._failure_counts[aid][0] == 2
    auth_manager.mark_account_success(aid)
    assert aid not in auth_manager._failure_counts
    assert aid not in auth_manager._account_failures


def test_reset_at_pins_cooldown(isolated_db):
    """上游给了绝对重置时刻时，冷却被 pin 到那一刻而不是 30s。"""
    aid = _add()
    future = time.time() + 3600
    auth_manager.mark_account_failure(aid, 429, reset_at=future)
    remaining = auth_manager.cooldown_remaining(aid)
    assert 3590 < remaining <= 3601


def test_reset_at_beyond_horizon_is_ignored(isolated_db):
    """超过 MAX_COOLDOWN_SECONDS 的 reset_at 不采信，回落到退避阶梯。"""
    aid = _add()
    auth_manager.mark_account_failure(aid, 429, reset_at=time.time() + 30 * 3600)
    assert auth_manager.cooldown_remaining(aid) <= 31


def test_streak_expires_after_ttl(isolated_db, monkeypatch):
    """长时间无失败后，计数 TTL 到期归零（防止永久抬高退避）。"""
    aid = _add()
    clock = [1000.0]
    monkeypatch.setattr(auth_manager.time, "monotonic", lambda: clock[0])
    auth_manager.mark_account_failure(aid, 429)
    assert auth_manager._failure_counts[aid][0] == 1
    clock[0] += auth_manager.FAILURE_COUNT_TTL_SECONDS + 1
    auth_manager.mark_account_failure(aid, 429)
    # TTL 已过，重新从 1 起算
    assert auth_manager._failure_counts[aid][0] == 1


def test_earliest_cooldown_remaining(isolated_db):
    aid1 = _add(uid="u-a")
    aid2 = _add(uid="u-b")
    auth_manager.reset_cooldown_state()
    auth_manager.mark_account_failure(aid1, 429, reset_at=time.time() + 600)
    auth_manager.mark_account_failure(aid2, 429, reset_at=time.time() + 120)
    earliest = auth_manager.earliest_cooldown_remaining([aid1, aid2])
    assert 110 < earliest <= 121


def test_persisted_and_reloaded(isolated_db):
    """冷却状态写穿到 SQLite，重启后可恢复。"""
    aid = _add()
    auth_manager.mark_account_failure(aid, 429, reset_at=time.time() + 900)
    rows = {r["account_id"]: r for r in db.list_account_cooldowns()}
    assert aid in rows and rows[aid]["fail_count"] == 1

    auth_manager.reset_cooldown_state()
    assert auth_manager.cooldown_remaining(aid) == 0
    loaded = auth_manager.load_cooldowns_from_db()
    assert loaded >= 1
    assert auth_manager.cooldown_remaining(aid) > 800
