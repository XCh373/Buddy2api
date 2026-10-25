# Frozen account-selection reference from d291cbdb7817b9cd0ed6e26a58da986fcf60eef2.
# Executed by tests with auth_manager helper globals; no production imports here.

def pick_account(
    exclude_ids: set[int] = None,
    provider: str = "workbuddy",
) -> Optional[dict]:
    """Pick an available account. Higher priority first; stick to the current one on ties."""
    exclude_ids = exclude_ids or set()
    accounts = db.get_active_accounts(provider)
    candidates = [
        a for a in accounts
        if a["id"] not in exclude_ids and not account_is_cooling_down(a["id"])
    ]
    if not candidates:
        return None

    config = account_routing.load_config() if provider == "workbuddy" else dict(account_routing.DEFAULTS)
    snapshots = account_routing.read_snapshots(candidates) if config["mode"] == "expiry_first" else {}
    top_candidates, _ = account_routing.best_group(candidates, config, snapshots)
    with _route_lock:
        sticky_id = _sticky_account_id.get(provider)
        if sticky_id is not None:
            sticky = next((a for a in top_candidates if a["id"] == sticky_id), None)
            if sticky:
                return sticky

        chosen = sorted(top_candidates, key=_route_sort_key)[0]
        _sticky_account_id[provider] = chosen["id"]
        return chosen


async def pick_account_with_fallback(
    exclude_ids: set[int] = None,
    provider: str = "workbuddy",
) -> Optional[dict]:
    """Pick an account; refresh expired ones if needed. Only refreshes the same provider."""
    account = pick_account(exclude_ids, provider=provider)
    if account:
        return account
    expired_accounts = sorted(
        (
            account
            for account in db.list_accounts(provider=provider)
            if account.get("status") == "expired"
        ),
        key=_route_sort_key,
    )
    for a in expired_accounts:
        if a["id"] in (exclude_ids or set()):
            continue
        if await refresh_token(a):
            fresh = db.get_account(a["id"])
            if fresh:
                _set_sticky_account(fresh["id"], provider)
                return fresh
    return None
