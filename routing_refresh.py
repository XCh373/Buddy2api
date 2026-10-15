"""Lifespan-owned resource refresh, independent of inference requests."""

from __future__ import annotations

import asyncio
import logging
import weakref

import account_routing
import auth_manager
import database as db
import providers

_log = logging.getLogger(__name__)
_workers = weakref.WeakKeyDictionary()


class RoutingRefresher:
    def __init__(self):
        self.wake = asyncio.Event()
        self.pending: set[int] = set()
        self.task: asyncio.Task | None = None

    def start(self):
        _workers[asyncio.get_running_loop()] = self
        self.task = asyncio.create_task(self.run(), name="workbuddy-routing-refresh")

    async def stop(self):
        _workers.pop(asyncio.get_running_loop(), None)
        if self.task:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass
            self.task = None

    async def refresh_round(self, config: dict, requested: set[int], *, only_requested: bool = False):
        semaphore = asyncio.Semaphore(2)
        accounts = db.get_active_accounts("workbuddy")
        if only_requested:
            accounts = [a for a in accounts if a["id"] in requested]

        async def one(account):
            async with semaphore:
                current = db.get_account(account["id"])
                if (not current or current.get("status") != "active"
                        or account_routing.load_config()["mode"] != "expiry_first"
                        or not providers.is_channel_enabled("workbuddy")):
                    return
                try:
                    await asyncio.wait_for(
                        auth_manager.fetch_account_resources(
                            current, force=account["id"] in requested,
                            max_age_seconds=config["refresh_interval_seconds"] - 1,
                            routing_only=True,
                        ), timeout=25,
                    )
                except Exception as exc:
                    _log.warning("Resource refresh failed for account %s (%s)", account["id"], type(exc).__name__)

        await asyncio.gather(*(one(account) for account in accounts))

    async def run(self):
        next_full = 0.0
        previous_config = None
        while True:
            self.wake.clear()
            requested, self.pending = self.pending, set()
            config = account_routing.load_config()
            if config != previous_config:
                next_full = 0.0
                previous_config = config
            if config["mode"] == "expiry_first" and providers.is_channel_enabled("workbuddy"):
                try:
                    full = asyncio.get_running_loop().time() >= next_full
                    if full or requested:
                        await self.refresh_round(config, requested, only_requested=not full)
                    if full:
                        next_full = asyncio.get_running_loop().time() + config["refresh_interval_seconds"]
                except Exception as exc:
                    _log.warning("Routing refresh round failed (%s)", type(exc).__name__)
                    next_full = asyncio.get_running_loop().time() + config["refresh_interval_seconds"]
            delay = config["refresh_interval_seconds"]
            if config["mode"] == "expiry_first" and next_full:
                delay = max(0.01, next_full - asyncio.get_running_loop().time())
            try:
                await asyncio.wait_for(self.wake.wait(), timeout=delay)
            except asyncio.TimeoutError:
                pass


def notify(account_id: int | None = None) -> bool:
    try:
        worker = _workers.get(asyncio.get_running_loop())
    except RuntimeError:
        return False
    if worker is None:
        return False
    if account_id is not None:
        if account_routing.load_config()["mode"] != "expiry_first":
            return False
        worker.pending.add(account_id)
    worker.wake.set()
    return True
