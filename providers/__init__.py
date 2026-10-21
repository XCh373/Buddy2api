"""Channel registry. WorkBuddy, QwenWork, Qoder, TraeWork, Loomy, and Xiaohuanxiong are enabled by default."""

from __future__ import annotations

import os

from providers.protocol import (
    KNOWN_CHANNEL_IDS,
    KNOWN_CHANNEL_SET,
    RETIRED_CHANNEL_IDS,
    RETIRED_CHANNEL_MESSAGE,
    ChannelId,
    Provider,
)
from providers.loomy import PROVIDER as LOOMY_PROVIDER
from providers.qoder import CN as QODER_PROVIDER
from providers.qoder import INTL as QODER_I_PROVIDER
from providers.qwenwork import PROVIDER as QWENWORK_PROVIDER
from providers.traework import PROVIDER as TRAEWORK_PROVIDER
from providers.workbuddy import INTL as WORKBUDDY_I_PROVIDER
from providers.workbuddy import PROVIDER as WORKBUDDY_PROVIDER
from providers.xiaohuanxiong import PROVIDER as XIAOHUANXIONG_PROVIDER

DEFAULT_PROVIDER_IDS: tuple[str, ...] = (
    "workbuddy",
    "workbuddy-i",
    "qwenwork",
    "qoder",
    "qoder-i",
    "traework",
    "loomy",
    "xiaohuanxiong",
)

_LOADED: dict[str, Provider] = {
    "workbuddy": WORKBUDDY_PROVIDER,
    "workbuddy-i": WORKBUDDY_I_PROVIDER,
    "qwenwork": QWENWORK_PROVIDER,
    "qoder": QODER_PROVIDER,
    "qoder-i": QODER_I_PROVIDER,
    "traework": TRAEWORK_PROVIDER,
    "loomy": LOOMY_PROVIDER,
    "xiaohuanxiong": XIAOHUANXIONG_PROVIDER,
}


def _parse_enabled() -> list[str]:
    raw = (os.environ.get("CB_GATEWAY_PROVIDERS") or "").strip()
    if not raw:
        return list(DEFAULT_PROVIDER_IDS)
    parts = [item.strip() for item in raw.split(",") if item.strip()]
    if not parts:
        return list(DEFAULT_PROVIDER_IDS)
    parts = [item for item in parts if item not in RETIRED_CHANNEL_IDS]
    if not parts:
        return list(DEFAULT_PROVIDER_IDS)
    unknown = [item for item in parts if item not in KNOWN_CHANNEL_SET]
    if unknown:
        raise RuntimeError(
            f"Unknown CB_GATEWAY_PROVIDERS id(s): {', '.join(unknown)}. "
            f"Known: {', '.join(KNOWN_CHANNEL_IDS)}"
        )
    if "workbuddy" not in parts:
        parts = ["workbuddy", *parts]
    # workbuddy first, then remaining in written order
    ordered = ["workbuddy"]
    for item in parts:
        if item != "workbuddy" and item not in ordered:
            ordered.append(item)
    return ordered


def enabled_provider_ids() -> list[str]:
    return _parse_enabled()


def is_known_channel(channel: str) -> bool:
    return channel in KNOWN_CHANNEL_SET


def is_retired_channel(channel: str) -> bool:
    return channel in RETIRED_CHANNEL_IDS


def retired_message(channel: str = "qclaw") -> str:
    if channel == "qclaw":
        return RETIRED_CHANNEL_MESSAGE
    return f"Channel '{channel}' is retired."


def ignored_retired_ids() -> list[str]:
    raw = (os.environ.get("CB_GATEWAY_PROVIDERS") or "").strip()
    if not raw:
        return []
    return [item.strip() for item in raw.split(",") if item.strip() in RETIRED_CHANNEL_IDS]


def is_channel_enabled(channel: str) -> bool:
    return channel in enabled_provider_ids()


def get_provider(channel: str) -> Provider | None:
    if channel not in enabled_provider_ids():
        return None
    return _LOADED.get(channel)


def register_provider(provider: Provider) -> None:
    """Test helper to install a stub channel."""
    _LOADED[provider.id] = provider
