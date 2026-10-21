"""Qoder providers. China and international are separate channels."""

from __future__ import annotations

from typing import Optional

import auth_manager
import database as db
from providers.protocol import ChannelId, QuotaSnapshot
from providers.qoder import chat, checkin, quota, store
from providers.qoder.token import QoderAuthError, is_token_expired, refresh_account
from providers.qoder.constants import REGIONS, model_rows


class QoderProvider:
    def __init__(self, channel: str):
        self.id: ChannelId = channel
        self.display_name = REGIONS[channel]["display_name"]
        self.checkin_supported = True
        self._channel = channel

    def list_models(self) -> list[dict]:
        import catalog

        fallback = [{"id": item_id, "name": item_id} for item_id, *_rest in model_rows(self._channel)]
        return catalog.models_for(self.id, fallback)

    def alias_map(self) -> dict[str, str]:
        import aliases

        return aliases.merged_map(self.id)

    def accepts_model(self, inner: str) -> bool:
        return chat.accepts_model(self._channel, inner)

    def translate_model(self, model: str) -> str:
        return chat.translate_model(self._channel, model)

    def pick_account(self, exclude_ids: set[int] | None = None) -> Optional[dict]:
        return auth_manager.pick_account(exclude_ids, provider=self.id)

    async def pick_account_with_fallback(self, exclude_ids: set[int] | None = None) -> Optional[dict]:
        exclude = exclude_ids or set()
        account = self.pick_account(exclude)
        if account and not is_token_expired(account):
            return account
        candidates = []
        if account:
            candidates.append(account)
        for row in db.list_accounts(provider=self.id):
            if row.get("id") in exclude or not row.get("access_token"):
                continue
            if account and row.get("id") == account.get("id"):
                continue
            candidates.append(row)
        for row in candidates:
            if not is_token_expired(row):
                return row
            try:
                return await refresh_account(row)
            except QoderAuthError:
                continue
        return account

    async def has_usable_account(self) -> bool:
        return await self.pick_account_with_fallback() is not None

    async def chat_completions(self, payload: dict, api_key_info: dict | None) -> tuple:
        return await chat.chat_completions(self._channel, payload, api_key_info)

    async def test_chat(self, account: dict, model: str = "auto", prompt: str = "ping") -> dict:
        return await chat.test_chat(account, model, prompt)

    def parse_credentials(self, body: dict) -> dict:
        return store.parse_credentials(body, self._channel)

    def discover(self) -> dict:
        return store.discover(self._channel)

    def import_path(self, path: str) -> dict:
        return store.import_discovered(self._channel, path)

    def upsert_account(self, parsed: dict) -> dict:
        return store.upsert_account(self._channel, parsed)

    async def fetch_quota(self, account: dict) -> QuotaSnapshot:
        return await quota.fetch_quota(account)

    async def fetch_checkin(self, account: dict, force: bool = False) -> dict:
        return await checkin.fetch_checkin(account, force=force)

    async def claim_checkin(self, account: dict) -> dict:
        return await checkin.claim_checkin(account)

    async def refresh(self, account: dict) -> dict:
        try:
            return await refresh_account(account)
        except QoderAuthError as exc:
            raise RuntimeError(str(exc)) from exc


CN = QoderProvider("qoder")
INTL = QoderProvider("qoder-i")
