import asyncio
import json

import pytest

import router
import session_identity as identity
from providers.protocol import BindResult


def headers(value="conversation-A", namespace=None):
    result = [(b"x-buddy2api-session-id", value.encode())]
    if namespace is not None:
        result.append((b"x-buddy2api-session-namespace", namespace.encode()))
    return result


def parse(h=None, key=1, channel="workbuddy", protocol="chat", **config):
    return identity.parse(headers() if h is None else h, {"id": key} if key else None,
                          channel, protocol, identity.normalize_config({"enabled": True, **config}))


@pytest.mark.parametrize("value", ["", "a,b", "a b", "x" * 257, "\n", "非ASCII"])
def test_invalid_explicit(value):
    with pytest.raises(identity.IdentityError):
        parse([(b"x-buddy2api-session-id", value.encode())])


@pytest.mark.parametrize("h", [headers() * 2, headers(namespace="other"),
                                  [(b"x-buddy2api-session-namespace", b"default")]])
def test_duplicate_or_invalid_namespace(h):
    with pytest.raises(identity.IdentityError):
        parse(h)


def test_isolation_and_scope():
    a = parse()
    assert a == parse(protocol="responses")
    assert a != parse(key=2)
    assert a != parse(channel="workbuddy-i")
    assert a != parse(headers("conversation-B"))
    assert a != parse(headers("Conversation-A"))
    assert "conversation-A" not in repr(a)
    assert a != parse(headers(namespace="other"), profiles={"1": {"namespace": "default", "namespaces": ["default", "other"]}})


def test_legacy_and_unverified_fields():
    assert parse([], enabled=False) is None
    assert parse(headers() * 2, enabled=False) is None
    assert parse(key=0) is None
    assert parse(channel="qoder") is None
    assert parse([(b"session-id", b"A"), (b"x-client-request-id", b"A")]) is None
    assert parse([(b"session_id", b"A")]) is None


def test_native_contract_is_scoped_and_explicit_wins():
    cfg = {"profiles": {"1": {"native": identity.NATIVE}}}
    h = [(b"session_id", b"native-A")]
    assert parse(h, protocol="responses", **cfg) is not None
    assert parse(h, protocol="chat", **cfg) is None
    assert parse(h * 2, protocol="responses", **cfg) is None
    assert parse([], protocol="responses", **cfg) is None
    assert parse(headers() + h, protocol="responses", **cfg) == parse()


def test_verified_codex_contract_conflicts_and_isolation():
    cfg = {"profiles": {"1": {"native": identity.CODEX_NATIVE}}}
    h = [(b"session-id", b"thread-A"), (b"thread-id", b"thread-A")]
    native = parse(h, protocol="responses", **cfg)
    assert native is not None
    assert parse(h, protocol="chat", **cfg) is None
    assert parse(h, protocol="responses") is None
    assert parse(h * 2, protocol="responses", **cfg) is None
    assert parse(h + [(b"thread-id", b"thread-A")], protocol="responses", **cfg) is None
    assert parse([h[0], (b"thread-id", b"other")], protocol="responses", **cfg) is None
    assert parse([(b"session-id", b"bad,value")], protocol="responses", **cfg) is None
    assert parse([(b"thread-id", b"thread-A")], protocol="responses", **cfg) is None
    assert parse(headers() + h, protocol="responses", **cfg) == parse()
    pi = {"profiles": {"1": {"native": identity.NATIVE}}}
    assert native != parse([(b"session_id", b"thread-A")], protocol="responses", **pi)


@pytest.mark.parametrize("value", [{"enabled": 1}, {"unknown": True}, {"profiles": {"1": {"native": "codex"}}},
                                    {"capacity": 1}, {"idle_ttl_seconds": 60}])
def test_bad_configuration(value):
    with pytest.raises(ValueError):
        identity.normalize_config(value)


@pytest.mark.parametrize("protocol", ["chat", "responses"])
@pytest.mark.parametrize("stream", [False, True])
def test_router_private_identity_survives_bridge(monkeypatch, protocol, stream):
    seen = []
    ident = parse()
    class Provider:
        def translate_model(self, model): return model
        def list_models(self): return []
        async def chat_completions(self, body, info):
            seen.append((body, info))
            return ("json", {"choices": [{"message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}]})
    monkeypatch.setattr(router.providers, "get_provider", lambda _: Provider())
    bound = BindResult(channel="workbuddy", inner="auto", original="auto")
    body = {"model": "auto", "stream": stream}
    body.update({"input": "hello"} if protocol == "responses" else {"messages": [{"role": "user", "content": "hello"}]})
    call = router.responses_after_bind if protocol == "responses" else router.chat_after_bind
    asyncio.run(call(bound, body, {"id": 1, "name": "synthetic"}, affinity=ident))
    assert seen[0][1]["_session_affinity"] is ident
    assert "_session_affinity" not in json.dumps(seen[0][0])
