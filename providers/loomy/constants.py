"""Loomy (iFlytek desktop assistant) protocol constants.

Chat is OpenAI-compatible on the model gateway. Login uses the account
service envelope; there is no refresh token. Daily points reset upstream.
"""

from __future__ import annotations

CHANNEL_ID = "loomy"
DISPLAY_NAME = "Loomy"

MODEL_BASE = "https://loomyad.xunfei.cn/api/v1"
ACCOUNT_BASE = "https://account.xfinfr.com"
ACCOUNT_APPID = "GM3LOOMY"
ACCESS_KEY_ID = "2thryby66wxi53sk"
ACCESS_KEY_SECRET = "zsak6eadrbawz683wf5r3m2snrwj868r"

LOOMY_VERSION = "0.9.38"
SESSION_SECONDS = 14 * 24 * 3600
RENEW_WITHIN_SECONDS = 36 * 3600

DEVICE_ID = "web"
WEB_MODEL_ID = "Web"
CLIENT_VERSION = "1.0.0"
CLIENT_UA = "Loomy|Desktop|Electron|macOS"

STATIC_MODELS = (
    "spark-x",
    "GLM-5.3-Flash",
    "qwen3.8-flash",
    "deepseek-v4-flash-0731",
    "mimo-v2.5",
    "MiniMax-M3",
    "Kimi-k2.6",
)

ALIASES = {
    "auto": "deepseek-v4-flash-0731",
}

SESSION_FILENAME = "auth-session.json"
