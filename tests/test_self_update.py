import asyncio
import base64
import json
from pathlib import Path

import pytest

import self_update
import server
from version import VERSION


@pytest.fixture(autouse=True)
def _reset_update_cache(monkeypatch):
    monkeypatch.delenv("CB_GATEWAY_FAKE_LATEST", raising=False)
    self_update.reset_cache()
    yield
    self_update.reset_cache()


def test_parse_and_compare_versions():
    assert self_update.parse_version_file('VERSION = "2.1.14"\n') == "2.1.14"
    assert self_update.parse_release_tag(json.dumps({"tag_name": "v2.1.14"})) == "2.1.14"
    assert self_update.is_newer("2.1.14", "2.1.13")
    assert self_update.is_newer("2.1.13", "2.1.9")
    assert not self_update.is_newer("2.1.13", "2.1.13")
    assert not self_update.is_newer("2.1.12", "2.1.13")


def test_check_uses_first_reachable_source(monkeypatch):
    async def fake_get(url):
        if "cdn.jsdelivr.net" in url:
            return 200, 'VERSION = "9.9.9"\n'
        raise AssertionError(url)

    monkeypatch.setattr(self_update, "_get", fake_get)
    monkeypatch.setattr(self_update, "install_kind", lambda: "git")
    payload = asyncio.run(self_update.check())
    assert payload["checked"] is True
    assert payload["latest"] == "9.9.9"
    assert payload["update_available"] is True
    assert payload["can_apply"] is True
    assert payload["source"] == "jsdelivr"
    assert payload["current"] == VERSION


def test_forced_check_prefers_github_main_over_stale_cdn(monkeypatch):
    requested = []
    github_file = base64.b64encode(b'VERSION = "2.2.5"\n').decode("ascii")
    github_file = github_file[:12] + "\n" + github_file[12:]

    async def fake_get(url):
        requested.append(url)
        if "cdn.jsdelivr.net" in url:
            return 200, 'VERSION = "2.1.18"\n'
        if "api.github.com/repos/XCh373/Buddy2api/contents/version.py?ref=main" in url:
            return 200, json.dumps({"encoding": "base64", "content": github_file})
        raise AssertionError(url)

    monkeypatch.setattr(self_update, "_get", fake_get)
    monkeypatch.setattr(self_update, "install_kind", lambda: "git")
    monkeypatch.setattr(self_update, "VERSION", "2.1.18")
    automatic = asyncio.run(self_update.check())
    assert automatic["latest"] == "2.1.18"
    assert automatic["source"] == "jsdelivr"
    payload = asyncio.run(self_update.check(force=True))

    assert payload["latest"] == "2.2.5"
    assert payload["update_available"] is True
    assert payload["can_apply"] is True
    assert payload["source"] == "github-api"
    assert requested == [
        "https://cdn.jsdelivr.net/gh/XCh373/Buddy2api@main/version.py",
        "https://api.github.com/repos/XCh373/Buddy2api/contents/version.py?ref=main",
    ]


def test_forced_check_same_github_version_is_checked_latest(monkeypatch):
    content = base64.b64encode(f'VERSION = "{VERSION}"\n'.encode()).decode()

    async def fake_get(url):
        assert "https://api.github.com/repos/XCh373/Buddy2api/contents/version.py?ref=main" == url
        return 200, json.dumps({"encoding": "base64", "content": content})

    monkeypatch.setattr(self_update, "_get", fake_get)
    payload = asyncio.run(self_update.check(force=True))

    assert payload["checked"] is True
    assert payload["latest"] == VERSION
    assert payload["update_available"] is False
    assert payload["can_apply"] is False
    assert payload["source"] == "github-api"


@pytest.mark.parametrize(
    "payload",
    [
        "not-json",
        "[]",
        json.dumps({"encoding": "utf-8", "content": 'VERSION = "9.9.9"'}),
        json.dumps({"encoding": "base64", "content": "%%%"}),
        json.dumps({"encoding": "base64", "content": base64.b64encode(b"\xff").decode()}),
        json.dumps({"encoding": "base64", "content": base64.b64encode(b"not a version").decode()}),
    ],
)
def test_github_contents_parser_rejects_malformed_payloads(payload):
    assert self_update.parse_github_content_version(payload) is None


def test_forced_check_rate_limit_falls_back_to_overridden_repo_raw(monkeypatch):
    monkeypatch.setattr(self_update, "VERSION", "2.2.5")
    monkeypatch.setenv("CB_GATEWAY_UPDATE_REPO", "example/fork")
    requested = []

    async def fake_get(url):
        requested.append(url)
        if "api.github.com" in url:
            return 403, '{"message":"API rate limit exceeded"}'
        if url == "https://raw.githubusercontent.com/example/fork/main/version.py":
            return 200, 'VERSION = "2.2.6"\n'
        raise AssertionError(url)

    monkeypatch.setattr(self_update, "_get", fake_get)
    payload = asyncio.run(self_update.check(force=True))

    assert payload["checked"] is True
    assert payload["latest"] == "2.2.6"
    assert payload["update_available"] is True
    assert payload["source"] == "github-raw-main"
    assert requested == [
        "https://api.github.com/repos/example/fork/contents/version.py?ref=main",
        "https://raw.githubusercontent.com/example/fork/main/version.py",
    ]


def test_automatic_check_uses_repo_override_for_cdn(monkeypatch):
    monkeypatch.setenv("CB_GATEWAY_UPDATE_REPO", "example/fork")
    requested = []

    async def fake_get(url):
        requested.append(url)
        if url == "https://cdn.jsdelivr.net/gh/example/fork@main/version.py":
            return 200, 'VERSION = "2.2.5"\n'
        raise AssertionError(url)

    monkeypatch.setattr(self_update, "_get", fake_get)
    payload = asyncio.run(self_update.check())

    assert payload["checked"] is True
    assert payload["source"] == "jsdelivr"
    assert requested == ["https://cdn.jsdelivr.net/gh/example/fork@main/version.py"]


@pytest.mark.parametrize(
    "api_status,api_text,raw_status,raw_text",
    [
        (403, '{"message":"rate limit"}', 404, "missing"),
        (200, "{malformed json", 200, 'VERSION = "not-a-version"\n'),
        (200, json.dumps({"encoding": "utf-8", "content": "VERSION = \\\"9.9.9\\\""}), 200, ""),
        (200, json.dumps({"encoding": "base64", "content": "%%%"}), 200, "VERSION = \"\xff\""),
    ],
)
def test_forced_github_sources_failure_stays_unchecked(
    monkeypatch, api_status, api_text, raw_status, raw_text
):
    async def fake_get(url):
        if "api.github.com" in url:
            return api_status, api_text
        if "raw.githubusercontent.com" in url:
            return raw_status, raw_text
        raise AssertionError(url)

    monkeypatch.setattr(self_update, "_get", fake_get)
    payload = asyncio.run(self_update.check(force=True))

    assert payload["checked"] is False
    assert payload["latest"] is None
    assert payload["update_available"] is False
    assert payload["can_apply"] is False
    assert payload["source"] is None


def test_get_keeps_four_second_timeout(monkeypatch):
    captured = {}

    class Response:
        status_code = 404
        text = ""

    class FakeClient:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def get(self, _url):
            return Response()

    monkeypatch.setattr(self_update.httpx, "AsyncClient", FakeClient)
    assert asyncio.run(self_update._get("https://github.com/example/repo")) == (404, "")
    assert captured["timeout"] == 4.0


def test_forced_version_check_does_not_run_git_or_restart(monkeypatch):
    monkeypatch.setattr(self_update, "VERSION", "2.2.5")
    content = base64.b64encode(b'VERSION = "2.2.6"\n').decode()

    async def fake_get(url):
        return 200, json.dumps({"encoding": "base64", "content": content})

    def forbidden_run(*args, **kwargs):
        raise AssertionError("version discovery must not execute local update commands")

    def forbidden_restart(*args, **kwargs):
        raise AssertionError("version discovery must not schedule a restart")

    monkeypatch.setattr(self_update, "_get", fake_get)
    monkeypatch.setattr(self_update, "_run", forbidden_run)
    monkeypatch.setattr(self_update, "schedule_restart", forbidden_restart)
    monkeypatch.setattr(self_update, "restart_now", forbidden_restart)
    payload = asyncio.run(self_update.check(force=True))

    assert payload["update_available"] is True
    assert payload["source"] == "github-api"


def test_check_failure_is_silent(monkeypatch):
    async def fake_get(url):
        raise OSError("blocked")

    monkeypatch.setattr(self_update, "_get", fake_get)
    monkeypatch.setattr(self_update, "install_kind", lambda: "git")
    payload = asyncio.run(self_update.check(force=True))
    assert payload["checked"] is False
    assert payload["latest"] is None
    assert payload["update_available"] is False
    assert payload["can_apply"] is False
    assert payload["message"] == ""


def test_fake_latest_skips_network(monkeypatch):
    async def fake_get(url):
        raise AssertionError(url)

    monkeypatch.setenv("CB_GATEWAY_FAKE_LATEST", "9.9.9")
    monkeypatch.setattr(self_update, "_get", fake_get)
    monkeypatch.setattr(self_update, "install_kind", lambda: "git")
    payload = asyncio.run(self_update.check(force=True))
    assert payload["source"] == "fake"
    assert payload["latest"] == "9.9.9"
    assert payload["update_available"] is True
    assert payload["can_apply"] is True


def test_fake_latest_apply_does_not_pull(monkeypatch):
    monkeypatch.setenv("CB_GATEWAY_FAKE_LATEST", "9.9.9")
    monkeypatch.setattr(self_update, "install_kind", lambda: "git")

    def fake_run(args, timeout):
        raise AssertionError(args)

    monkeypatch.setattr(self_update, "_run", fake_run)
    result = self_update.apply()
    assert result["ok"] is True
    assert result["restart"] is False
    assert "测试模式" in result["message"]


def test_docker_install_cannot_apply(monkeypatch):
    async def fake_get(url):
        return 200, 'VERSION = "9.9.9"\n'

    monkeypatch.setattr(self_update, "_get", fake_get)
    monkeypatch.setattr(self_update, "install_kind", lambda: "docker")
    payload = asyncio.run(self_update.check(force=True))
    assert payload["update_available"] is True
    assert payload["can_apply"] is False
    assert payload["message"] == "Docker 部署请在宿主机更新本 fork 源码后运行 docker compose up -d --build"


def test_apply_pulls_installs_and_asks_restart(monkeypatch, tmp_path):
    monkeypatch.setattr(self_update, "ROOT", tmp_path)
    (tmp_path / ".git").mkdir()
    (tmp_path / "requirements.txt").write_text("fastapi==0.141.1\n", encoding="utf-8")
    (tmp_path / "version.py").write_text('VERSION = "9.9.9"\n', encoding="utf-8")

    async def fake_check(*, force=False):
        return {
            "current": "2.1.13",
            "latest": "9.9.9",
            "update_available": True,
            "can_apply": True,
            "install": "git",
            "checked": True,
            "source": "jsdelivr",
            "message": "",
        }

    calls = []

    def fake_run(args, timeout):
        calls.append(list(args))
        class Result:
            returncode = 0
            stdout = "true\n" if "rev-parse" in args and "--is-inside-work-tree" in args else "main\n"
            stderr = ""
            if "status" in args:
                stdout = ""
            if "pull" in args:
                stdout = "Updating abc..def\n"
        return Result()

    monkeypatch.setattr(self_update, "check", fake_check)
    monkeypatch.setattr(self_update, "_run", fake_run)
    result = self_update.apply()
    assert result["ok"] is True
    assert result["restart"] is True
    assert result["latest"] == "9.9.9"
    assert ["git", "pull", "--ff-only"] in calls
    assert any(args[:3] == [__import__("sys").executable, "-m", "pip"] for args in calls)


def test_apply_refuses_dirty_tree(monkeypatch, tmp_path):
    monkeypatch.setattr(self_update, "ROOT", tmp_path)
    (tmp_path / ".git").mkdir()

    async def fake_check(*, force=False):
        return {
            "current": "2.1.13",
            "latest": "9.9.9",
            "update_available": True,
            "can_apply": True,
            "install": "git",
            "checked": True,
            "source": "jsdelivr",
            "message": "",
        }

    def fake_run(args, timeout):
        class Result:
            returncode = 0
            stdout = "true\n"
            stderr = ""
        if args[:2] == ["git", "rev-parse"] and "--abbrev-ref" in args:
            Result.stdout = "main\n"
        if args[:2] == ["git", "status"]:
            Result.stdout = " M server.py\n"
        return Result()

    monkeypatch.setattr(self_update, "check", fake_check)
    monkeypatch.setattr(self_update, "_run", fake_run)
    monkeypatch.setattr(self_update, "install_kind", lambda: "git")
    result = self_update.apply()
    assert result["ok"] is False
    assert result["restart"] is False
    assert "未提交" in result["message"]


def test_apply_skips_untracked_files(monkeypatch, tmp_path):
    monkeypatch.setattr(self_update, "ROOT", tmp_path)
    (tmp_path / ".git").mkdir()
    (tmp_path / "requirements.txt").write_text("fastapi==0.141.1\n", encoding="utf-8")
    (tmp_path / "version.py").write_text('VERSION = "9.9.9"\n', encoding="utf-8")

    async def fake_check(*, force=False):
        return {
            "current": "2.1.13",
            "latest": "9.9.9",
            "update_available": True,
            "can_apply": True,
            "install": "git",
            "checked": True,
            "source": "jsdelivr",
            "message": "",
        }

    def fake_run(args, timeout):
        class Result:
            returncode = 0
            stdout = "true\n"
            stderr = ""
        if "--abbrev-ref" in args:
            Result.stdout = "main\n"
        elif "status" in args:
            Result.stdout = "?? docker-compose.override.yml\n"
        elif "pull" in args:
            Result.stdout = "Updating\n"
        return Result()

    monkeypatch.setattr(self_update, "check", fake_check)
    monkeypatch.setattr(self_update, "_run", fake_run)
    monkeypatch.setattr(self_update, "install_kind", lambda: "git")
    result = self_update.apply()
    assert result["ok"] is True


def test_restart_disabled_under_pytest():
    assert self_update.restart_allowed() is False


def test_admin_version_endpoint(monkeypatch):
    monkeypatch.setattr(server, "ALLOW_NO_ADMIN_AUTH", True)
    monkeypatch.setattr(server, "LOCAL_MODE", False)

    async def fake_check(*, force=False):
        return {
            "current": VERSION,
            "latest": "9.9.9",
            "update_available": True,
            "can_apply": True,
            "install": "git",
            "checked": True,
            "source": "jsdelivr",
            "message": "",
        }

    monkeypatch.setattr(self_update, "check", fake_check)
    payload = asyncio.run(server.admin_get_version())
    assert payload["latest"] == "9.9.9"
    assert payload["update_available"] is True


def test_admin_update_endpoint_skips_restart_under_pytest(monkeypatch):
    monkeypatch.setattr(server, "ALLOW_NO_ADMIN_AUTH", True)
    monkeypatch.setattr(server, "LOCAL_MODE", False)
    monkeypatch.setattr(
        self_update,
        "apply",
        lambda: {"ok": True, "restart": True, "current": "2.1.13", "latest": "9.9.9", "message": "done"},
    )
    payload = asyncio.run(server.admin_apply_update())
    assert payload["ok"] is True
    assert payload["restart"] is True
    assert self_update.restart_allowed() is False


def test_web_has_update_controls():
    html = (Path(__file__).resolve().parents[1] / "web" / "index.html").read_text(encoding="utf-8")
    assert "/admin/version" in html
    assert "/admin/update" in html
    assert "更新到 v" in html
    assert "一键更新到 v" in html
    assert "检查更新" in html
    assert "暂时查不到远端版本" in html
    assert "测试模式" in html
