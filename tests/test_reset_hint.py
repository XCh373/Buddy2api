"""reset_hint：上游重置时间解析的边界测试。

断言以真实库里的错误文案为基准（见 docs/investigation/09-bud005-rootcause-20261102.md）。
"""

import time

import pytest

import reset_hint


def _epoch(text: str, tz_hours: float = 8) -> float:
    import datetime

    tz = datetime.timezone(datetime.timedelta(hours=tz_hours))
    return datetime.datetime.strptime(text, "%Y-%m-%d %H:%M:%S").replace(tzinfo=tz).timestamp()


# 真实 6004 文案
CN_6004 = (
    '[upstream_http] {"code":6004,"msg":"您的使用量已超出频率限制，'
    '将在 2026-11-02 21:08:23 UTC+8 重置，您也可以切换其他模型继续使用。",'
    '"requestId":"05f9436a0d1b4543a880aea97de80192"}'
)
# 真实 14003 文案（注意日志里被截断到 500 字符）
CN_14003_TRUNCATED = (
    '[upstream_http] {"code":14003,"msg":"too many requests",'
    '"requestId":"ae05924fb04540b2b0b301bd59c95abb",'
    '"retry_policy":{"retry":true,"retry_after":30},'
    '"displayMsg":{"en":"Model busy. Please switch mode'
)


def test_cn_6004_absolute_reset_time():
    now = _epoch("2026-11-02 09:28:41")
    stamp, source = reset_hint.parse_reset_at(CN_6004, now=now)
    assert source == "absolute"
    assert stamp == _epoch("2026-11-02 21:08:23")
    # 关键：UTC+8 必须被解释为 +08:00 —— dateutil 会把 "UTC+8" 解析成 -08:00，
    # 那会把这 11.66 小时的窗口算成错的值。这里显式断言本地时间一致。
    assert time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(stamp)) == "2026-11-02 21:08:23"


def test_cn_6004_timezone_is_plus_eight_not_minus():
    """回归：绝不能把 UTC+8 当成 -08:00。"""
    now = _epoch("2026-10-27 00:00:00")
    stamp, _ = reset_hint.parse_reset_at(
        "您的使用量已超出频率限制，将在 2026-10-27 09:43:39 UTC+8 重置", now=now
    )
    assert stamp == _epoch("2026-10-27 09:43:39")
    # 若是 -08:00，结果会相差 16 小时
    assert abs(stamp - _epoch("2026-10-27 09:43:39", tz_hours=-8)) > 3600


def test_14003_structured_retry_after():
    now = 1_800_000_000.0
    stamp, source = reset_hint.parse_reset_at(CN_14003_TRUNCATED, now=now)
    assert source == "structured"
    assert stamp == pytest.approx(now + 30, abs=1)


def test_default_timezone_when_absent():
    """没有时区声明时按 UTC+8 解释（与 10router 行为一致）。"""
    now = _epoch("2026-09-28 11:30:00")
    stamp, source = reset_hint.parse_reset_at("请于 2026-09-28 14:30:00 后重试", now=now)
    assert source == "absolute"
    assert stamp == _epoch("2026-09-28 14:30:00")


def test_english_relative_delay():
    now = 1_800_000_000.0
    stamp, source = reset_hint.parse_reset_at("Rate limited, please try again in 2 minutes", now=now)
    assert source == "relative"
    assert stamp == pytest.approx(now + 120, abs=1)


@pytest.mark.parametrize(
    "text",
    ["", None, "random text", "{}", '{"code":6004}', "将在 1999-01-01 00:00:00 UTC+8 重置"],
)
def test_never_raises_and_returns_none(text):
    """解析失败必须安全返回 (None, None)，绝不抛异常。"""
    assert reset_hint.parse_reset_at(text, now=1_800_000_000.0) == (None, None)


def test_rejects_beyond_horizon():
    """超过 21 小时的重置时间视为异常（防止错乱时间锁死账号）。"""
    now = _epoch("2026-11-02 00:00:00")
    assert reset_hint.parse_reset_at("将在 2026-11-20 09:43:39 UTC+8 重置", now=now) == (None, None)


def test_describe_delay():
    now = 1_800_000_000.0
    assert reset_hint.describe_delay(now + 90, now=now) == 90
    assert reset_hint.describe_delay(now - 5, now=now) == 0
    assert reset_hint.describe_delay(None, now=now) is None
