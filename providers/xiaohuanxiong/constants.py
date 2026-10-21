"""Xiaohuanxiong (SenseTime office raccoon) protocol constants."""

from __future__ import annotations

CHANNEL_ID = "xiaohuanxiong"
DISPLAY_NAME = "小浣熊"

ORIGIN = "https://xiaohuanxiong.com"
LLM_BASE = "https://xiaohuanxiong.com/api/web/llm/v2"
AUTH_BASE = "https://xiaohuanxiong.com/api/web/auth/v1"
POINTS_BASE = "https://xiaohuanxiong.com/api/web/points/v1"

CLIENT_PLATFORM = "desktop-windows"
CLIENT_VERSION = "v1.0.0"
USER_AGENT = "office-raccoon/1.0.0"
REFRESH_MARGIN_SECONDS = 300

STATIC_MODELS = (
    "sn-glm-5-3-flash",
    "sn-glm-5-3",
    "sn-deepseek-v4-1-flash",
    "sn-deepseek-v4-pro",
    "sn-kimi-k3",
    "sn-SenseNova-6-8-Flash",
)

ALIASES = {
    "auto": "sn-glm-5-3-flash",
}

AUTH_FILENAME = "auth.json"
