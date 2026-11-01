import asyncio
from pathlib import Path

import pytest
from fastapi import HTTPException

import aliases
import credential_crypto
import database as db
import providers
import router
import server
from providers.protocol import UnknownModel


@pytest.fixture()
def isolated_db(tmp_path, monkeypatch):
    path = tmp_path / "gateway.db"
    monkeypatch.setattr(db, "DB_PATH", path)
    monkeypatch.setenv("CB_GATEWAY_MASTER_KEY", "pytest-master-key")
    credential_crypto.reset_cache()
    db.init_db()
    yield path
    credential_crypto.reset_cache()


@pytest.fixture()
def all_channels(monkeypatch):
    monkeypatch.setenv("CB_GATEWAY_PROVIDERS", "workbuddy,qclaw,qwenwork,traework")
    yield
    monkeypatch.delenv("CB_GATEWAY_PROVIDERS", raising=False)


def test_legacy_flat_aliases_belong_to_workbuddy(isolated_db, all_channels):
    db.set_setting("model_aliases", {"custom-wb": "glm-5.2"})
    assert aliases.user_aliases_for("workbuddy") == {"custom-wb": "glm-5.2"}
    assert aliases.user_aliases_for("traework") == {}
    assert aliases.resolve("workbuddy", "custom-wb") == "glm-5.2"
    assert aliases.resolve("traework", "custom-wb") == "custom-wb"


def test_save_user_aliases_is_per_channel_and_skips_builtin(isolated_db, all_channels):
    aliases.save_user_aliases(
        {
            "workbuddy": {"gpt-4o": "hy4-preview", "my-wb": "glm-5.2"},
            "traework": {"gpt-4o": "qwen-3.7-plus", "auto": "glm-5.3"},
        }
    )
    assert "gpt-4o" not in aliases.user_aliases_for("workbuddy")
    assert aliases.user_aliases_for("workbuddy")["my-wb"] == "glm-5.2"
    assert aliases.merged_map("workbuddy")["gpt-4o"] == "glm-5.2"
    assert aliases.user_aliases_for("traework")["gpt-4o"] == "qwen-3.7-plus"
    assert "auto" not in aliases.user_aliases_for("traework")
    assert aliases.merged_map("traework")["auto"] == "qwen-3.7-plus"


def test_save_rejects_unknown_channel_and_slash_alias(isolated_db, all_channels):
    with pytest.raises(aliases.AliasError, match="unknown channel"):
        aliases.save_user_aliases({"not-a-channel": {"a": "b"}})
    with pytest.raises(aliases.AliasError, match="must not contain"):
        aliases.save_user_aliases({"traework": {"traework/gpt-4o": "qwen-3.7-plus"}})


def test_user_alias_takes_effect_on_matching_channel_key(isolated_db, all_channels):
    aliases.save_user_aliases(
        {
            "traework": {"gpt-4o": "qwen-3.7-plus"},
            "qwenwork": {"gpt-4o": "pro"},
            "workbuddy": {"my-wb": "glm-5.2"},
        }
    )
    traework = providers.get_provider("traework")
    qwenwork = providers.get_provider("qwenwork")
    workbuddy = providers.get_provider("workbuddy")

    assert traework.accepts_model("gpt-4o")
    assert traework.translate_model("gpt-4o") == "qwen-3.7-plus"
    assert qwenwork.accepts_model("gpt-4o")
    assert qwenwork.translate_model("gpt-4o") == "pro"
    assert workbuddy.translate_model("gpt-4o") == "glm-5.2"
    assert workbuddy.accepts_model("my-wb")
    assert workbuddy.translate_model("my-wb") == "glm-5.2"

    bound = router.bind({"model": "gpt-4o"}, {"default_channel": "traework"})
    assert bound.channel == "traework"
    assert bound.inner == "gpt-4o"
    assert traework.translate_model(bound.inner) == "qwen-3.7-plus"

    bound = router.bind({"model": "gpt-4o"}, {"default_channel": "qwenwork"})
    assert bound.channel == "qwenwork"
    assert qwenwork.translate_model(bound.inner) == "pro"

    bound = router.bind({"model": "gpt-4o"}, {"default_channel": "workbuddy"})
    assert bound.channel == "workbuddy"
    assert workbuddy.translate_model(bound.inner) == "glm-5.2"

    bound = router.bind({"model": "traework/gpt-4o"}, {"default_channel": "traework"})
    assert bound.inner == "gpt-4o"

    with pytest.raises(UnknownModel):
        router.bind({"model": "my-wb"}, {"default_channel": "traework"})


def test_key_allowed_models_accepts_channel_alias_translation(isolated_db, all_channels):
    aliases.save_user_aliases({"traework": {"gpt-4o": "qwen-3.7-plus"}})
    server._check_model_access(
        {"allowed_models": ["qwen-3.7-plus"]},
        "gpt-4o",
        "gpt-4o",
        "traework",
    )
    server._check_model_access(
        {"allowed_models": ["traework/qwen-3.7-plus"]},
        "gpt-4o",
        "gpt-4o",
        "traework",
    )
    with pytest.raises(HTTPException) as err:
        server._check_model_access(
            {"allowed_models": ["glm-5.2"]},
            "gpt-4o",
            "gpt-4o",
            "traework",
        )
    assert err.value.status_code == 403


def test_alias_snapshot_lists_enabled_channels(isolated_db, all_channels):
    snap = aliases.snapshot()
    channels = [item["channel"] for item in snap["sources"]]
    assert channels[0] == "workbuddy"
    assert "traework" in channels
    wb = next(item for item in snap["sources"] if item["channel"] == "workbuddy")
    assert "gpt-4o" in wb["builtin_keys"]
    assert wb["aliases"]["gpt-4o"] == "glm-5.2"


def test_admin_aliases_round_trip(isolated_db, all_channels, monkeypatch):
    monkeypatch.setattr(server, "ALLOW_NO_ADMIN_AUTH", True)
    monkeypatch.setattr(server, "LOCAL_MODE", False)

    class _Req:
        async def stream(self):
            yield b'{"traework":{"gpt-4o":"qwen-3.7-plus"}}'

    asyncio.run(server.admin_update_aliases(_Req()))
    snap = asyncio.run(server.admin_get_aliases())
    tw = next(item for item in snap["sources"] if item["channel"] == "traework")
    assert tw["aliases"]["gpt-4o"] == "qwen-3.7-plus"
    assert "gpt-4o" not in tw["builtin_keys"]


def test_models_page_has_per_channel_alias_controls():
    html = (Path(__file__).resolve().parents[1] / "web" / "index.html").read_text(encoding="utf-8")
    assert "aliasChannel" in html
    assert "按通道保存，只对该通道的 API Key 生效" in html
    assert "WorkBuddy 别名映射" not in html


def test_accounts_is_first_nav_and_default_page():
    html = (Path(__file__).resolve().parents[1] / "web" / "index.html").read_text(encoding="utf-8")
    assert html.find("{k:'accounts'") < html.find("{k:'dashboard'")
    assert "savedPage)?savedPage:'accounts'" in html
    assert "savedPage)?savedPage:'dashboard'" not in html


def test_create_key_form_defaults_to_auto_channel():
    html = (Path(__file__).resolve().parents[1] / "web" / "index.html").read_text(encoding="utf-8")
    assert "channel:'*'" in html
    assert "f.channel='*'" in html
    assert "channel:'workbuddy'}),res" not in html
    assert "f.channel='workbuddy'" not in html
    assert "modal key-create" in html
    assert "channel-hint" in html
    assert "keys-channel" in html
    assert 'style="max-width:560px"' not in html


@pytest.mark.parametrize("model", ["gpt-4o", "auto"])
def test_any_channel_uses_highest_account_priority(isolated_db, all_channels, model):
    aliases.save_user_aliases({"traework": {"gpt-4o": "qwen-3.7-plus"}})
    wb = db.add_account({"name": "wb", "uid": "wb", "provider": "workbuddy", "priority": 1})
    tw = db.add_account({"name": "tw", "uid": "tw", "provider": "traework", "priority": 10})
    db.add_account({"name": "disabled", "uid": "disabled", "provider": "workbuddy", "priority": 100, "status": "inactive"})
    assert router.bind({"model": model}, {"default_channel": "*"}).channel == "traework"
    # Explicit prefixes and channel-bound keys keep their binding.
    assert router.bind({"model": "workbuddy/" + model}, {"default_channel": "*"}).channel == "workbuddy"
    assert router.bind({"model": model}, {"default_channel": "workbuddy"}).channel == "workbuddy"
    db.update_account(tw, {"priority": 1})
    assert router.bind({"model": model}, {"default_channel": "*"}).channel == "workbuddy"
    db.update_account(wb, {"priority": -2})
    db.update_account(tw, {"priority": -1})
    assert router.bind({"model": model}, {"default_channel": "*"}).channel == "traework"


@pytest.mark.parametrize("responses_api", [False, True])
def test_dispatch_logs_alias_and_resolved_model(isolated_db, all_channels, monkeypatch, responses_api):
    from providers.traework import chat
    import time

    aliases.save_user_aliases({"traework": {"test-alias": "qwen-3.7-plus"}})
    account_id = db.add_account({"name": "tw", "uid": "tw", "provider": "traework"})
    key_id = db.add_api_key("sk-test-issue127", "test", default_channel="traework")
    key = {"id": key_id, "name": "test", "default_channel": "traework"}

    async def send(payload, info):
        assert payload["model"] == "qwen-3.7-plus"
        chat._log(info, db.get_account(account_id), payload["model"], False, "stop", 200, "", time.time())
        return ("json", {"id": "test", "object": "chat.completion", "model": payload["model"],
                         "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}]})

    monkeypatch.setattr(providers.get_provider("traework"), "chat_completions", send)
    bound = router.bind({"model": "traework/test-alias"}, key)
    handler = router.responses_after_bind if responses_api else router.chat_after_bind
    payload = {"model": bound.original, "input": "ping"} if responses_api else {"model": bound.original, "messages": [{"role": "user", "content": "ping"}]}
    result = asyncio.run(handler(bound, payload, key))
    assert result[1]["model"] == "traework/test-alias"
    row = db.search_logs({"q": "qwen-3.7-plus"})["items"][0]
    assert row["model"] == "traework/test-alias"
    assert row["resolved_model"] == "qwen-3.7-plus"
    assert row["provider"] == "traework"
    recent = db.get_stats()["recent_logs"][0]
    assert recent["resolved_model"] == "qwen-3.7-plus"
    assert recent["provider"] == "traework"
    db.init_db()  # The schema migration is idempotent and keeps the new field.
    assert db.search_logs({"q": "test-alias"})["items"][0]["resolved_model"] == "qwen-3.7-plus"
