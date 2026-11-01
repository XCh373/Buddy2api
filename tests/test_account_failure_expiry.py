"""BUD-009 (minimal scope): an edge-layer 401/403 must not expire an account.

The decision was to fix exactly two things and to record everything else as a
known limitation, rather than pursue an architecture-wide refactor:

  FIXED
    * a bare edge-layer 401/403 (HTML body, or a rate-limit / non-auth business
      code) only cools down, it never writes status='expired';
    * the authentication-confirmation tally is independent of cooldown state,
      so two real auth failures separated by a cooldown still expire;
    * a non-auth failure (429 / edge) neither counts toward nor resets the tally,
      and an operator-set `inactive` account is never overwritten to expired.

  KNOWN LIMITATIONS (asserted as limitations below, NOT as guarantees)
    * renewal (login / refresh / import) and admin status writes are not
      coordinated with the failure path; an older in-flight failure can still
      race them. Documented by test_known_limitation_*.
    * the tally is in-process and clears on restart.
"""

import pytest

import auth_manager
import credential_crypto
import database as db
import proxy
import workbuddy_outbound


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    path = tmp_path / "gateway.db"
    monkeypatch.setattr(db, "DB_PATH", path)
    monkeypatch.setenv("CB_GATEWAY_MASTER_KEY", "pytest-master-key")
    credential_crypto.reset_cache()
    _reset()
    db.init_db()
    yield path
    _reset()
    credential_crypto.reset_cache()


def _reset():
    auth_manager._account_failures.clear()
    auth_manager.reset_auth_confirmations()
    auth_manager.reset_account_state_locks()
    auth_manager.reset_session_dead_counts()


def _add(status="active", **kw):
    base = {
        "name": "acc", "uid": "uid-1", "access_token": "token",
        "refresh_token": "refresh", "expires_at": 9999999999999,
        "domain": "www.codebuddy.cn", "provider": "workbuddy", "status": status,
    }
    base.update(kw)
    return db.add_account(base)


# ------------------------------------------------------- the fix that matters

def test_edge_html_401_never_expires(isolated_db):
    aid = _add()
    for _ in range(6):
        auth_manager.mark_account_failure(aid, 401, structural_auth=False)
    assert db.get_account(aid)["status"] == "active"


def test_non_auth_business_code_never_expires(isolated_db):
    """A 403 with a rate-limit code is not an auth verdict."""
    aid = _add()
    for _ in range(6):
        verdict = workbuddy_outbound.is_auth_failure(403, {"code": 6004, "msg": "rate limit"})
        auth_manager.mark_account_failure(aid, 403, structural_auth=verdict)
    assert db.get_account(aid)["status"] == "active"


def test_two_structural_failures_expire(isolated_db):
    aid = _add()
    auth_manager.mark_account_failure(aid, 401, structural_auth=True)
    assert db.get_account(aid)["status"] == "active"
    auth_manager.mark_account_failure(aid, 401, structural_auth=True)
    assert db.get_account(aid)["status"] == "expired"


def test_tally_survives_cooldown(isolated_db, monkeypatch):
    """The auth tally must not be erased when cooldown bookkeeping expires."""
    aid = _add()
    clock = [1000.0]
    monkeypatch.setattr(auth_manager.time, "monotonic", lambda: clock[0])

    auth_manager.mark_account_failure(aid, 401, structural_auth=True)
    clock[0] += 400
    auth_manager.account_is_cooling_down(aid)          # purges the cooldown record
    assert aid not in auth_manager._account_failures
    assert auth_manager.auth_confirmation_count(aid) == 1

    auth_manager.mark_account_failure(aid, 401, structural_auth=True)
    assert db.get_account(aid)["status"] == "expired"


def test_rate_limit_does_not_count_toward_tally(isolated_db):
    aid = _add()
    auth_manager.mark_account_failure(aid, 429, structural_auth=False)
    auth_manager.mark_account_failure(aid, 401, structural_auth=True)
    assert db.get_account(aid)["status"] == "active"
    auth_manager.mark_account_failure(aid, 401, structural_auth=True)
    assert db.get_account(aid)["status"] == "expired"


def test_success_clears_tally(isolated_db):
    aid = _add()
    auth_manager.mark_account_failure(aid, 401, structural_auth=True)
    auth_manager.mark_account_success(aid)
    assert auth_manager.auth_confirmation_count(aid) == 0


def test_tally_expires_after_ttl(isolated_db, monkeypatch):
    aid = _add()
    clock = [1000.0]
    monkeypatch.setattr(auth_manager.time, "monotonic", lambda: clock[0])
    auth_manager.mark_account_failure(aid, 401, structural_auth=True)
    clock[0] += auth_manager.AUTH_CONFIRMATION_TTL_SEC + 1
    assert auth_manager.auth_confirmation_count(aid) == 0


@pytest.mark.parametrize("status", ["inactive", "expired"])
def test_non_active_status_not_overwritten(isolated_db, status):
    aid = _add(status=status)
    for _ in range(4):
        auth_manager.mark_account_failure(aid, 401, structural_auth=True)
    assert db.get_account(aid)["status"] == status


# ------------------------------------------------------------------ classifier

@pytest.mark.parametrize("status,detail,expected", [
    (401, {"code": 12153}, True),
    (401, {"code": 14015}, True),
    (401, {"code": 11142}, True),
    (401, {"code": 9999, "msg": "offline user session not found"}, True),
    (401, {"code": 11140, "msg": "request illegal"}, False),
    (429, {"code": 11140, "msg": "rate limiting"}, False),
    (403, {"code": 6004, "msg": "rate limit"}, False),
    (401, {"code": 11115}, False),
    (401, {"error": {"message": "<html>401 Authorization Required</html>"}}, False),
    (401, None, False),
])
def test_is_auth_failure_table(status, detail, expected):
    assert workbuddy_outbound.is_auth_failure(status, detail) is expected


def test_apply_account_rule_11140_does_not_expire(isolated_db):
    aid = _add()
    detail = {"code": 11140, "msg": "request illegal"}
    for _ in range(4):
        proxy._apply_account_rule(aid, 401, detail)
    assert db.get_account(aid)["status"] == "active"


def test_apply_account_rule_auth_code_expires(isolated_db):
    aid = _add()
    detail = {"code": 12153, "msg": "offline user session not found"}
    proxy._apply_account_rule(aid, 401, detail)
    proxy._apply_account_rule(aid, 401, detail)
    assert db.get_account(aid)["status"] == "expired"


# -------------------------------------------------------------- known limits

def test_known_limitation_admin_inactive_is_preserved_when_seen(isolated_db):
    """LOCKED-IN GUARANTEE: a failure that observes `inactive` will not overwrite it.

    KNOWN LIMITATION: an admin write that races the failure's read-then-write is
    not fully prevented (no shared lock / version check across admin writes).
    """
    aid = _add()
    db.update_account(aid, {"status": "inactive"})
    auth_manager.mark_account_failure(aid, 401, structural_auth=True)
    auth_manager.mark_account_failure(aid, 401, structural_auth=True)
    assert db.get_account(aid)["status"] == "inactive"


def test_known_limitation_relogin_clears_tally_sequentially(isolated_db):
    """SUPPORTED: a sequential re-login clears the tally.

    KNOWN LIMITATION: a *concurrent* in-flight failure is not version-checked, so
    it could still land after the login. Deliberately out of scope here.
    """
    aid = _add()
    auth_manager.mark_account_failure(aid, 401, structural_auth=True)
    auth_manager.upsert_login_account({
        "uid": "uid-1", "access_token": "t2", "refresh_token": "r2",
        "expires_at": 9999999999999, "domain": "www.codebuddy.cn", "provider": "workbuddy",
    })
    assert auth_manager.auth_confirmation_count(aid) == 0
