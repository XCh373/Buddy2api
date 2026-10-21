import asyncio
import json
from pathlib import Path

import pytest

import credential_crypto
import database as db
import providers
import router
from providers.protocol import UnknownModel
from providers.traework.chat import (
    _stream_once,
    conversation_prompt,
    extract_assistant_text,
    extract_assistant_turn,
    translate_model,
)
from providers.traework.crypto import decrypt_tc_b64, encrypt_tc_b64
from providers.traework.store import parse_credentials, traework_auth_dirs


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
def traework_enabled(monkeypatch):
    monkeypatch.setenv("CB_GATEWAY_PROVIDERS", "workbuddy,traework")
    yield
    monkeypatch.delenv("CB_GATEWAY_PROVIDERS", raising=False)


def test_traework_in_default_registry(monkeypatch):
    monkeypatch.delenv("CB_GATEWAY_PROVIDERS", raising=False)
    assert providers.enabled_provider_ids() == [
        "workbuddy",
        "workbuddy-i",
        "qwenwork",
        "qoder",
        "qoder-i",
        "traework",
        "loomy",
        "xiaohuanxiong",
    ]
    assert providers.get_provider("traework") is not None
    assert "traework" in providers._LOADED


def test_parse_credentials_official_shape():
    parsed = parse_credentials(
        {
            "token": "jwt-access",
            "refreshToken": "rt-1",
            "userId": "3577",
            "expiredAt": "2026-09-09T08:55:19.325Z",
            "host": "https://api.trae.cn",
            "account": {"username": "书虫"},
            "device_id": "3446",
        }
    )
    assert parsed["provider"] == "traework"
    assert parsed["access_token"] == "jwt-access"
    assert parsed["refresh_token"] == "rt-1"
    assert parsed["uid"] == "3577"
    assert parsed["extra"]["device_id"] == "3446"
    assert parsed["expires_at"] > 10_000_000_000


def test_parse_credentials_requires_token():
    with pytest.raises(ValueError):
        parse_credentials({"account": {"username": "x"}})


def test_bind_traework_when_enabled(isolated_db, traework_enabled):
    bound = router.bind({"model": "auto"}, {"default_channel": "traework"})
    assert bound.channel == "traework"
    assert bound.inner == "auto"
    bound = router.bind({"model": "traework/qwen-3.7-plus"}, {"default_channel": "traework"})
    assert bound.inner == "qwen-3.7-plus"
    with pytest.raises(UnknownModel):
        router.bind({"model": "glm-5.2"}, {"default_channel": "traework"})


def test_parse_supplier_models_official_grouped_list():
    from providers.traework.models import parse_supplier_models

    parsed = parse_supplier_models(
        {
            "code": 0,
            "message": "success",
            "data": {
                "list": [
                    {
                        "function": "solo_coder",
                        "models": [
                            {
                                "name": "Doubao-Seed-2.0-Code",
                                "display_name": "Doubao-Seed-2.0-Code",
                                "is_default": False,
                            },
                            {
                                "name": "Doubao-Seed-Code",
                                "display_name": "Doubao-Seed-Code",
                            },
                            {
                                "name": "qwen-3.6-plus",
                                "display_name": "qwen-3.6-plus",
                            },
                        ],
                    }
                ]
            },
        }
    )
    ids = [item["id"] for item in parsed]
    assert ids == ["Doubao-Seed-2.0-Code", "Doubao-Seed-Code", "qwen-3.6-plus"]
    assert "function" not in ids


def test_translate_auto():
    assert translate_model("auto") == "qwen-3.7-plus"


def test_llm_body_renames_tool_calls_and_stringifies_parameters():
    from providers.traework.llm_chat import build_llm_body

    body = build_llm_body(
        {
            "model": "traework/glm-5.3-flash",
            "messages": [
                {"role": "developer", "content": "be brief"},
                {"role": "user", "content": "read a.txt"},
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [{"id": "call-1", "function": {"name": "read_file", "arguments": "{\"path\":\"a.txt\"}"}}],
                },
                {"role": "tool", "tool_call_id": "call-1", "content": "hello"},
                {"role": "tool", "tool_call_id": "missing", "content": "drop me"},
            ],
            "tools": [{"type": "function", "function": {"name": "read_file", "parameters": {"type": "object"}}}],
            "tool_choice": {"type": "auto"},
        },
        "glm-5.3-flash",
    )
    assert body["function"] == "solo_work_lite"
    assert body["config_name"] == "glm-5.3-flash"
    assert body["tool_choice"] == "auto"
    assert body["tools"][0]["function"]["parameters"] == '{"type":"object"}'
    roles = [item["role"] for item in body["messages"]]
    assert roles == ["system", "user", "assistant", "tool"]
    assert "function_call" in body["messages"][2]["tool_calls"][0]
    assert "function" not in body["messages"][2]["tool_calls"][0]
    assert body["messages"][3]["content"] == [{"type": "text", "text": "hello"}]


def test_llm_output_event_restores_function_field():
    from providers.traework.llm_chat import parse_event

    parsed = parse_event(
        "output",
        json.dumps({"response": "查一下", "tool_calls": [{"index": 0, "id": "call-1", "function_call": {"name": "read_file", "arguments": "{}", "namespace": "solo"}}]}),
    )
    assert parsed["text"] == "查一下"
    assert parsed["tool_calls"][0]["function"]["name"] == "read_file"
    assert "namespace" not in parsed["tool_calls"][0]["function"]


def test_conversation_prompt_keeps_a_single_user_turn():
    assert conversation_prompt({"messages": [{"role": "user", "content": "hi"}]}) == "hi"


def test_conversation_prompt_includes_prior_turns_and_tool_results():
    prompt = conversation_prompt({
        "messages": [
            {"role": "system", "content": "你在 E:\\test-project 工作"},
            {"role": "user", "content": "测试写入"},
            {
                "role": "assistant",
                "content": [{"type": "text", "text": "我先写一个测试文件。"}],
                "tool_calls": [{"function": {"name": "write_file", "arguments": "{\"path\":\"a.txt\"}"}}],
            },
            {"role": "tool", "name": "write_file", "content": "created=true"},
            {"role": "user", "content": "继续"},
        ]
    })
    assert "E:\\test-project" in prompt
    assert "测试写入" in prompt
    assert "write_file" in prompt
    assert "created=true" in prompt
    assert prompt.rstrip().endswith("继续")


def test_extract_assistant_text_from_task():
    items = [
        {"role": "user", "content": "[]"},
        {
            "role": "assistant",
            "message_type": "task",
            "content": json.dumps(
                {
                    "task_id": "t1",
                    "messages": [
                        {"type": "text", "text_content": "pong"},
                    ],
                },
                ensure_ascii=False,
            ),
        },
    ]
    assert extract_assistant_text(items) == "pong"


def _finish_task_item(*, summary: str, reasoning: str = "", usage: dict | None = None) -> dict:
    item = {
        "role": "assistant",
        "message_type": "task",
        "content": json.dumps(
            {
                "task_id": "t1",
                "messages": [
                    {
                        "type": "plan_item",
                        "plan_item": {
                            "thought": "",
                            "reasoning_content": reasoning,
                            "tool_call_info": {
                                "name": "finish",
                                "params": {"summary": summary},
                                "result": {"data": {"summary": ""}, "status": "success"},
                            },
                        },
                    }
                ],
            },
            ensure_ascii=False,
        ),
    }
    if usage is not None:
        item["token_usage"] = json.dumps(usage, ensure_ascii=False)
    return item


def test_extract_finish_summary_not_reasoning():
    items = [
        {"role": "user", "content": "[]"},
        _finish_task_item(
            summary="pong",
            reasoning='The user wants me to reply with exactly "pong".',
            usage={"prompt_tokens": 26490, "completion_tokens": 16, "total_tokens": 26506, "reasoning_tokens": 11},
        ),
    ]
    turn = extract_assistant_turn(items)
    assert turn["text"] == "pong"
    assert "user wants me to reply" in turn["reasoning"]
    assert turn["usage"]["prompt_tokens"] == 26490
    assert turn["usage"]["completion_tokens"] == 16
    assert turn["usage"]["total_tokens"] == 26506


def test_extract_deepseek_finish_without_reasoning():
    items = [_finish_task_item(summary="pong", reasoning="")]
    turn = extract_assistant_turn(items)
    assert turn["text"] == "pong"
    assert turn["reasoning"] == ""


def test_parse_credit_usage_from_entitlement_packs():
    from providers.traework.quota import parse_credit_usage

    remaining, used, limit, unlimited = parse_credit_usage(
        {
            "code": 0,
            "user_entitlement_pack_list": [
                {
                    "entitlement_base_info": {"quota": {"credits_limit": 100}},
                    "usage": {"credits_amount": 40},
                },
                {
                    "entitlement_base_info": {"quota": {"credits_limit": 30}},
                    "usage": {"credits_amount": 10},
                },
            ],
            "usage_summary": {},
        }
    )
    assert remaining == 80
    assert used == 50
    assert limit == 130
    assert unlimited is False


def test_parse_credit_usage_falls_back_to_usage_summary():
    from providers.traework.quota import parse_credit_usage

    remaining, used, limit, unlimited = parse_credit_usage(
        {"usage_summary": {"total_amount": 12, "consumed_amount": 5}}
    )
    assert (remaining, used, limit, unlimited) == (7, 5, 12, False)


class _JsonResponse:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        return self._payload


def test_fetch_checkin_refreshes_expired_token_before_status(monkeypatch):
    from providers.traework import quota

    calls = []

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def post(self, url, headers=None, json=None):
            calls.append((url, headers.get("Authorization")))
            return _JsonResponse(200, {"code": 0, "enable": True, "checked_in": False, "credits": 15})

    async def refresh(account):
        return {**account, "access_token": "fresh-token", "expires_at": 9_999_999_999_999}

    monkeypatch.setattr(quota.httpx, "AsyncClient", FakeClient)
    monkeypatch.setattr(quota, "refresh_account", refresh)
    account = {
        "id": 7,
        "name": "tw",
        "access_token": "stale-token",
        "expires_at": 1,
        "extra": {"host": "https://api.trae.cn"},
    }
    row = asyncio.run(quota.fetch_checkin(account, force=True))
    assert row["ok"] is True
    assert row["already_claimed"] is False
    assert row["credit"] == 15
    assert calls == [
        ("https://api.trae.cn/trae/api/v2/ug/checkin_credits/status", "Cloud-IDE-JWT fresh-token")
    ]


def test_fetch_quota_retries_auth_failure_and_reads_packs(monkeypatch):
    from providers.traework import quota

    calls = []
    refreshed = {"n": 0}

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def post(self, url, headers=None, json=None):
            token = headers.get("Authorization")
            calls.append(token)
            if token.endswith("stale-token"):
                return _JsonResponse(
                    200,
                    {
                        "code": 1001,
                        "message": "We're sorry, but we are not able to authenticate you.",
                        "usage_summary": {},
                        "user_entitlement_pack_list": [],
                    },
                )
            return _JsonResponse(
                200,
                {
                    "code": 0,
                    "user_entitlement_pack_list": [
                        {
                            "entitlement_base_info": {"quota": {"credits_limit": 200}},
                            "usage": {"credits_amount": 25},
                        }
                    ],
                },
            )

    async def refresh(account):
        refreshed["n"] += 1
        return {**account, "access_token": "fresh-token", "expires_at": 9_999_999_999_999}

    monkeypatch.setattr(quota.httpx, "AsyncClient", FakeClient)
    monkeypatch.setattr(quota, "refresh_account", refresh)
    snapshot = asyncio.run(
        quota.fetch_quota(
            {
                "id": 8,
                "access_token": "stale-token",
                "expires_at": 9_999_999_999_999,
                "extra": {"host": "https://api.trae.cn"},
            }
        )
    )
    assert refreshed["n"] == 1
    assert calls == ["Cloud-IDE-JWT stale-token", "Cloud-IDE-JWT fresh-token"]
    assert snapshot.ok is True
    assert snapshot.remaining == 175
    assert snapshot.unsupported is False


def test_exchange_401_says_relogin(isolated_db, monkeypatch):
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    from providers.traework import token

    key = ec.generate_private_key(ec.SECP256R1())
    private_pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode("ascii")
    public_pem = key.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode("ascii")
    account_id = db.add_account(
        {
            "name": "tw",
            "uid": "tw-1",
            "provider": "traework",
            "access_token": "old-access",
            "refresh_token": "old-refresh",
            "expires_at": 9_999_999_999_999,
            "extra": {
                "host": "https://api.trae.cn",
                "device_id": "device",
                "machine_id": "machine",
                "public_key_pem": public_pem,
                "private_key_pem": private_pem,
            },
        }
    )

    class FakeResponse:
        status_code = 401

        def json(self):
            return {"ResponseMetadata": {"Error": {"Code": "20101", "Message": "refresh token is invalid."}}}

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def post(self, url, headers=None, json=None):
            assert json["RefreshToken"] == "old-refresh"
            assert "x-cloudide-token" in headers
            return FakeResponse()

    monkeypatch.setattr(token.httpx, "AsyncClient", FakeClient)
    with pytest.raises(token.TraeWorkAuthError) as caught:
        asyncio.run(token.refresh_account(db.get_account(account_id)))
    assert "HTTP 401" in str(caught.value)
    assert "重新登录" in str(caught.value)
    saved = db.get_account(account_id)
    assert saved["refresh_token"] == "old-refresh"


def test_auth_failure_after_refresh_says_login_expired(monkeypatch):
    from providers.traework import quota

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def post(self, url, headers=None, json=None):
            return _JsonResponse(
                200,
                {"code": 1001, "enable": False, "checked_in": False, "message": "authenticate you"},
            )

    async def refresh(account):
        raise quota.TraeWorkAuthError("ExchangeToken failed: HTTP 401")

    monkeypatch.setattr(quota.httpx, "AsyncClient", FakeClient)
    monkeypatch.setattr(quota, "refresh_account", refresh)
    row = asyncio.run(
        quota.fetch_checkin(
            {
                "id": 9,
                "name": "tw",
                "access_token": "stale-token",
                "expires_at": 9_999_999_999_999,
            }
        )
    )
    assert row["ok"] is False
    assert row["status_code"] == 200
    assert "重新登录" in row["message"]
    assert "authenticate you" not in row["message"]


def test_traework_sources_do_not_touch_workbuddy_stack():
    root = Path(__file__).resolve().parents[1] / "providers" / "traework"
    for path in root.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        assert "copilot.tencent.com" not in text
        assert "import fingerprint" not in text
        assert "from fingerprint" not in text
        assert "X-IDE-Type" not in text


def test_traework_auth_dirs_ignore_workbuddy_cb_auth_dir(monkeypatch, tmp_path):
    tdir = tmp_path / "trae-auth"
    tdir.mkdir()
    wb = tmp_path / "workbuddy-auth"
    wb.mkdir()
    monkeypatch.setenv("CB_TRAEWORK_AUTH_DIR", str(tdir))
    monkeypatch.setenv("CB_AUTH_DIR", str(wb))
    dirs = [path.resolve() for path in traework_auth_dirs()]
    assert tdir.resolve() in dirs
    assert wb.resolve() not in dirs


def test_decrypt_tc_roundtrip_rejects_garbage():
    with pytest.raises(Exception):
        decrypt_tc_b64("not-base64-$$$")


def test_encrypt_tc_roundtrip():
    plain = json.dumps({"token": "access", "refreshToken": "refresh", "userId": "u1"}, separators=(",", ":"))
    assert decrypt_tc_b64(encrypt_tc_b64(plain)) == plain


def _storage_file(folder: Path, document: dict) -> Path:
    path = folder / "storage.json"
    blob = encrypt_tc_b64(json.dumps(document, separators=(",", ":")))
    path.write_text(
        json.dumps({"telemetry.machineId": "m1", "iCubeAuthInfo://icube.cloudide": blob}, indent=4),
        encoding="utf-8",
    )
    return path


def test_refresh_writes_storage_so_reimport_keeps_new_token(isolated_db, monkeypatch, tmp_path):
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    from providers.traework import token
    from providers.traework.store import import_discovered

    key = ec.generate_private_key(ec.SECP256R1())
    private_pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode("ascii")
    folder = tmp_path / "trae-auth"
    folder.mkdir()
    monkeypatch.setenv("CB_TRAEWORK_AUTH_DIR", str(folder))
    auth_path = _storage_file(
        folder,
        {
            "token": "old-access",
            "refreshToken": "old-refresh",
            "userId": "tw-write",
            "expiredAt": "2026-10-01T00:00:00.000Z",
            "host": "https://api.trae.cn",
            "account": {"username": "kept"},
        },
    )
    account_id = db.add_account(
        {
            "name": "tw",
            "uid": "tw-write",
            "provider": "traework",
            "access_token": "old-access",
            "refresh_token": "old-refresh",
            "expires_at": 1_700_000_000_000,
            "extra": {
                "host": "https://api.trae.cn",
                "device_id": "device",
                "private_key_pem": private_pem,
                "auth_path": str(auth_path),
            },
        }
    )

    class FakeResponse:
        status_code = 200

        def json(self):
            return {
                "Result": {
                    "Token": "new-access",
                    "RefreshToken": "new-refresh",
                    "TokenExpireAt": "2026-10-20T12:13:58.000Z",
                    "RefreshExpireAt": "2027-04-04T12:13:58.000Z",
                }
            }

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def post(self, url, headers=None, json=None):
            return FakeResponse()

    monkeypatch.setattr(token.httpx, "AsyncClient", FakeClient)
    fresh = asyncio.run(token.refresh_account(db.get_account(account_id)))
    assert fresh["access_token"] == "new-access"
    assert fresh["refresh_token"] == "new-refresh"
    reimported = import_discovered(str(auth_path))
    assert reimported["access_token"] == "new-access"
    assert reimported["refresh_token"] == "new-refresh"
    assert reimported["nickname"] == "kept"
    backup = auth_path.with_name("storage.json.buddy2api.bak")
    assert backup.is_file()
    assert "old-refresh" in decrypt_tc_b64(json.loads(backup.read_text(encoding="utf-8"))["iCubeAuthInfo://icube.cloudide"])


def test_refresh_does_not_write_outside_auth_dir(monkeypatch, tmp_path):
    from providers.traework.store import write_refreshed_session

    allowed = tmp_path / "allowed"
    allowed.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    monkeypatch.setenv("CB_TRAEWORK_AUTH_DIR", str(allowed))
    target = _storage_file(outside, {"token": "old", "refreshToken": "old", "userId": "u"})
    before = target.read_text(encoding="utf-8")
    write_refreshed_session(
        {"uid": "u", "extra": {"auth_path": str(target)}},
        {"access_token": "new", "refresh_token": "new", "expires_at": 1_800_000_000_000},
    )
    assert target.read_text(encoding="utf-8") == before


def test_refresh_skips_mismatched_uid(monkeypatch, tmp_path):
    from providers.traework.store import write_refreshed_session

    folder = tmp_path / "trae-auth"
    folder.mkdir()
    monkeypatch.setenv("CB_TRAEWORK_AUTH_DIR", str(folder))
    target = _storage_file(folder, {"token": "old", "refreshToken": "old", "userId": "owner"})
    write_refreshed_session(
        {"uid": "other", "extra": {"auth_path": str(target)}},
        {"access_token": "new", "refresh_token": "new", "expires_at": 1_800_000_000_000},
    )
    document = json.loads(decrypt_tc_b64(json.loads(target.read_text(encoding="utf-8"))["iCubeAuthInfo://icube.cloudide"]))
    assert document["token"] == "old"
    assert document["refreshToken"] == "old"


def test_stream_once_yields_bytes():
    async def collect():
        return [chunk async for chunk in _stream_once("pong", "glm-5.3")]

    chunks = asyncio.run(collect())
    assert chunks
    assert all(isinstance(chunk, (bytes, bytearray)) for chunk in chunks)
    assert chunks[-1] == b"data: [DONE]\n\n"
    payload = json.loads(chunks[0].decode("utf-8").split("data:", 1)[1].strip())
    assert payload["choices"][0]["delta"]["content"] == "pong"


def test_responses_bridge_accepts_traework_text_sse(isolated_db, traework_enabled, monkeypatch):
    async def string_stream(payload, api_key_info):
        async def chunks():
            yield (
                'data: {"id":"traework-stub","model":"glm-5.3",'
                '"choices":[{"index":0,"delta":{"role":"assistant","content":"pong"},'
                '"finish_reason":null}]}\n\n'
            )
            yield (
                'data: {"id":"traework-stub","model":"glm-5.3",'
                '"choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}\n\n'
            )
            yield "data: [DONE]\n\n"

        return ("stream", chunks())

    provider = providers.get_provider("traework")
    monkeypatch.setattr(provider, "chat_completions", string_stream)
    original = "traework/qwen-3.7-plus"
    bound = router.bind({"model": original}, {"default_channel": "traework"})

    async def collect_events():
        result = await router.responses_after_bind(
            bound,
            {"model": original, "input": "hi", "stream": True},
            {"id": 1, "name": "dsh-key", "default_channel": "traework"},
        )
        assert result[0] == "stream"
        return [chunk async for chunk in result[1]]

    raw = asyncio.run(collect_events())
    events = [
        json.loads(line[6:])
        for chunk in raw
        for line in (
            chunk.decode() if isinstance(chunk, (bytes, bytearray)) else chunk
        ).splitlines()
        if line.startswith("data: {")
    ]
    failed = [event for event in events if event.get("type") == "response.failed"]
    completed = [event for event in events if event.get("type") == "response.completed"]
    assert not failed
    assert completed
    assert completed[0]["response"]["output"][0]["content"][0]["text"] == "pong"
