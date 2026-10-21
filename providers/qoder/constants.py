"""Qoder CN and international protocol constants.

The two regions are separate channels. Credentials and model catalogs
are not interchangeable, so a domain filter on one pool would hide that.
"""

from __future__ import annotations

CHAT_PATH = "/algo/api/v2/service/pro/sse/agent_chat_generation"
CHAT_QUERY = "FetchKeys=llm_model_result&AgentId=agent_common&Encode=1"
CAMPAIGNS_PATH = "/sash/api/v1/me/campaigns"
CAMPAIGNS_QUERY = "?forceRefresh=true"
USAGE_PATH = "/api/v2/quota/usage"
COSY_VERSION = "1.1.38"
CLIENT_TYPE = "5"
DATA_POLICY = "disagree"
LOGIN_VERSION = "v2"
MACHINE_TYPE = "5"
CLIENT_IP = "127.0.0.1"
MAX_OUTPUT_TOKENS = 32768
SASH_USER_AGENT = "Qoder"
SASH_CLIENT_TYPE = "10"

# id, upstream key, reasoning, vision
_SHARED = (
    ("Qwen3.8-Flash", "qfmodel", True, True),
    ("Qwen3.8-Max", "qmodel_38max", True, True),
    ("Auto", "auto", False, False),
    ("Qwen3.7-Max", "qmodel_latest", True, True),
    ("Qwen3.7-Plus", "qmodel", True, False),
    ("DeepSeek-V4-Pro", "dmodel", True, False),
    ("DeepSeek-Flash", "dfmodel", False, False),
    ("GLM-5.3", "gmodel", True, False),
    ("Kimi-K2.8-Preview", "kmodel", True, True),
    ("MiniMax-M3", "mmodel", False, False),
)
_INTL_ONLY = (
    ("Ultimate", "ultimate", True, True),
    ("Performance", "performance", True, True),
    ("Efficient", "efficient", False, True),
    ("Sonus", "smodel", True, True),
    ("Cantus", "cmodel", True, True),
    ("Kimi-K3", "kmodel_latest", False, True),
    ("GLM-5.3-Flash", "gfmodel", True, True),
)

REGIONS = {
    "qoder": {
        "display_name": "Qoder 中国版",
        "gateway": "https://gateway.qoder.com.cn",
        "openapi": "https://openapi.qoder.com.cn",
        "origin": "https://qoder.com.cn",
        "models": _SHARED,
    },
    "qoder-i": {
        "display_name": "Qoder 国际版",
        "gateway": "https://api3.qoder.sh",
        "job_gateway": "https://api2.qoder.sh",
        "openapi": "https://openapi.qoder.sh",
        "origin": "https://qoder.com",
        "models": _SHARED + _INTL_ONLY,
    },
}


def region(channel: str) -> dict:
    try:
        return REGIONS[channel]
    except KeyError as exc:
        raise ValueError(f"Unknown Qoder channel '{channel}'") from exc


def inference_base(channel: str, access_token: str) -> str:
    info = region(channel)
    if channel == "qoder-i" and str(access_token or "").startswith("jt-"):
        return info["job_gateway"]
    return info["gateway"]


def model_rows(channel: str) -> tuple:
    return region(channel)["models"]


def upstream_key(channel: str, model_id: str) -> str:
    wanted = (model_id or "").strip().lower()
    for item_id, key, _reasoning, _vision in model_rows(channel):
        if item_id.lower() == wanted or key.lower() == wanted:
            return key
    return model_id
