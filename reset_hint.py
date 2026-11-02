"""解析上游错误体里的「明确重置时间」，供账号冷却使用。

背景（实测，见 docs/investigation/09-bud005-rootcause-20261102.md）：

  * 上游 6004 = 单账号 × 单模型 × 每 24h 翻滚周期累计 prompt tokens > 2 亿。
    错误体里带绝对重置时刻，例如：
        {"code":6004,"msg":"您的使用量已超出频率限制，将在 2026-11-02 21:08:23
         UTC+8 重置，您也可以切换其他模型继续使用。"}
    该时刻经 7 个真实窗口验证是硬约束（窗口内 200 成功率 1/642）。

  * 14003 = 模型级饱和（msg="too many requests"），带结构化字段：
        {"code":14003,...,"retry_policy":{"retry":true,"retry_after":30}}

本模块只做一件事：把这两种「上游已经告诉我们的等待时长」解析成
`(reset_at_epoch_seconds, source)`。解析失败返回 `(None, None)`，
调用方退化为纯指数退避 —— **绝不抛异常**。

⚠️ 时区陷阱（实测）：`dateutil 2.9.0` 会把 `"2026-10-27 09:43:39 UTC+8"`
解析成 **-08:00**（偏移错 16 小时），导致 5 小时窗口被算成 11 小时。
因此这里**不用 dateutil**，一律正则 + 自建 `timezone(timedelta(...))`。
"""

from __future__ import annotations

import json
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

# 中文文案：「将在 2026-11-02 21:08:23 UTC+8 重置」
# 时区部分允许 UTC/GMT 前缀、+8 / +08 / +08:00 / +8:30 等写法，也允许缺省。
_CN_RESET = re.compile(
    r"将在\s*(?P<date>\d{4}-\d{2}-\d{2})\s+(?P<clock>\d{2}:\d{2}:\d{2})"
    r"(?:\s*(?P<zone>UTC|GMT)\s*(?P<sign>[+-])?\s*(?P<hours>\d{1,2})(?::(?P<minutes>\d{2}))?)?"
    r"\s*重置",
    re.IGNORECASE,
)

# 中文备用文案（部分客户端/变体）：「请于 2026-09-28 14:30:00 后重试」
_CN_RETRY_AFTER = re.compile(
    r"(?:请于|将于)\s*(?P<date>\d{4}-\d{2}-\d{2})\s+(?P<clock>\d{2}:\d{2}:\d{2})"
    r"(?:\s*(?P<zone>UTC|GMT)\s*(?P<sign>[+-])?\s*(?P<hours>\d{1,2})(?::(?P<minutes>\d{2}))?)?"
    r"\s*后",
    re.IGNORECASE,
)

# 英文：Please try again in 20s / retry in 2 minutes
_EN_DELAY = re.compile(
    r"(?:try again|retry)\s+in\s+(?P<n>\d+(?:\.\d+)?)\s*"
    r"(?P<unit>ms|milliseconds?|s|secs?|seconds?|m|mins?|minutes?|h|hours?)\b",
    re.IGNORECASE,
)

_UNIT_SECONDS = {
    "ms": 0.001, "millisecond": 0.001, "milliseconds": 0.001,
    "s": 1.0, "sec": 1.0, "secs": 1.0, "second": 1.0, "seconds": 1.0,
    "m": 60.0, "min": 60.0, "mins": 60.0, "minute": 60.0, "minutes": 60.0,
    "h": 3600.0, "hour": 3600.0, "hours": 3600.0,
}

# 上游未声明时区时，CodeBuddy 服务在中国，按 UTC+8 解释（与 10router 一致）。
_DEFAULT_TZ_OFFSET_MINUTES = 8 * 60

# 合理性边界：超过这个跨度的「重置时间」视为异常（上游错乱/文案过期），
# 回退退避。实测最长窗口 20.4 小时，留 21 小时余量。
MAX_RESET_HORIZON_SEC = 21 * 3600


def _tz_from_match(match: "re.Match[str]") -> timezone:
    """从正则匹配里构造时区；缺省按 UTC+8。"""
    hours = match.group("hours")
    if not hours:
        return timezone(timedelta(minutes=_DEFAULT_TZ_OFFSET_MINUTES))
    total = int(hours) * 60 + int(match.group("minutes") or 0)
    if (match.group("sign") or "+") == "-":
        total = -total
    return timezone(timedelta(minutes=total))


def _parse_absolute(text: str, now: float) -> Optional[float]:
    for pattern in (_CN_RESET, _CN_RETRY_AFTER):
        match = pattern.search(text or "")
        if not match:
            continue
        try:
            naive = datetime.strptime(
                f"{match.group('date')} {match.group('clock')}", "%Y-%m-%d %H:%M:%S"
            )
        except ValueError:
            continue
        try:
            stamp = naive.replace(tzinfo=_tz_from_match(match)).timestamp()
        except (OverflowError, OSError, ValueError):
            continue
        if _plausible(stamp, now):
            return stamp
    return None


def _parse_structured(text: str, now: float) -> Optional[float]:
    """解析 JSON 里的 retry_after / retryDelay 等结构化等待时长。

    注意：`logs.error_msg` 入库时会被截断到 500 字符，JSON 可能不完整，
    所以这里先尝试整体 `json.loads`，失败后用正则兜底提取。
    """
    raw = text or ""
    start = raw.find("{")
    if start < 0:
        return None
    payload = raw[start:]

    # 1) 完整 JSON（成功时最可靠）
    try:
        data = json.loads(payload)
    except (ValueError, TypeError):
        data = None
    if isinstance(data, dict):
        seconds = _retry_after_seconds(data)
        if seconds is not None:
            return _clamp_future(now + seconds, now)

    # 2) 截断 JSON 兜底：直接找 retry_after / retryDelay 字段
    match = re.search(r'"(retry_after|retryDelay|retry_after_ms)"\s*:\s*"?(\d+(?:\.\d+)?)"?', payload)
    if match:
        value = float(match.group(2))
        seconds = value / 1000.0 if match.group(1) == "retry_after_ms" else value
        return _clamp_future(now + seconds, now)
    return None


def _retry_after_seconds(data: dict) -> Optional[float]:
    policy = data.get("retry_policy")
    if isinstance(policy, dict):
        raw = policy.get("retry_after", policy.get("retry_after_ms"))
        seconds = _as_seconds(raw, key="retry_after_ms" if "retry_after_ms" in policy else "retry_after")
        if seconds is not None:
            return seconds
    for key in ("retry_after", "retry_after_ms", "retryDelay"):
        seconds = _as_seconds(data.get(key), key=key)
        if seconds is not None:
            return seconds
    return None


def _as_seconds(value, *, key: str) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number < 0:
        return None
    return number / 1000.0 if key.endswith("_ms") else number


def _parse_relative(text: str, now: float) -> Optional[float]:
    match = _EN_DELAY.search(text or "")
    if not match:
        return None
    unit = _UNIT_SECONDS.get(match.group("unit").lower())
    if unit is None:
        return None
    try:
        seconds = float(match.group("n")) * unit
    except ValueError:
        return None
    return _clamp_future(now + seconds, now)


def _plausible(stamp: float, now: float) -> bool:
    """绝对时刻必须落在 (now, now + MAX_RESET_HORIZON_SEC] 内。"""
    return now < stamp <= now + MAX_RESET_HORIZON_SEC


def _clamp_future(stamp: float, now: float) -> Optional[float]:
    """相对时长推出来的时刻：只要求在未来且不超过上限。"""
    if stamp <= now:
        return None
    return min(stamp, now + MAX_RESET_HORIZON_SEC)


def parse_reset_at(text: str, *, now: Optional[float] = None) -> tuple[Optional[float], Optional[str]]:
    """把上游错误体解析成 `(reset_at_epoch, source)`。

    解析顺序（命中即返回）：
      1. ``structured``  —— JSON 里的 retry_after / retryDelay（14003 等）
      2. ``absolute``    —— 中文文案里的绝对时刻（6004）
      3. ``relative``    —— 英文 "try again in Ns"

    全部失败返回 ``(None, None)``，调用方应退化为纯指数退避。
    本函数**不会抛异常**。
    """
    if not text or not isinstance(text, str):
        return None, None
    current = time.time() if now is None else float(now)

    for source, parser in (
        ("structured", _parse_structured),
        ("absolute", _parse_absolute),
        ("relative", _parse_relative),
    ):
        try:
            stamp = parser(text, current)
        except Exception:  # noqa: BLE001 - 解析永不让主流程失败
            stamp = None
        if stamp is not None:
            return stamp, source
    return None, None


def describe_delay(reset_at: Optional[float], *, now: Optional[float] = None) -> Optional[int]:
    """把 reset_at 换算成「还需等待多少秒」，用于 Retry-After 头。"""
    if reset_at is None:
        return None
    current = time.time() if now is None else float(now)
    remaining = reset_at - current
    if remaining <= 0:
        return 0
    return int(remaining + 0.999)
