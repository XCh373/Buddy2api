import asyncio
import time
from pathlib import Path

import pytest
from fastapi import HTTPException

import server
import self_update


@pytest.fixture(autouse=True)
def _reset_shutdown_state():
    server.reset_shutdown_state()
    server.bind_runtime_server(None)
    yield
    server.reset_shutdown_state()
    server.bind_runtime_server(None)


def test_shutdown_marks_server_for_exit():
    class FakeServer:
        should_exit = False

    server.bind_runtime_server(FakeServer())
    result = server.schedule_shutdown(0)
    assert result["ok"] is True
    assert "start.bat" in result["message"]
    for _ in range(30):
        if server._SERVER.should_exit:
            break
        time.sleep(0.05)
    assert server._SERVER.should_exit is True


def test_shutdown_does_not_exit_pytest_without_server():
    result = server.schedule_shutdown(0)
    assert result["ok"] is True
    time.sleep(0.05)
    assert server._SHUTDOWN_STARTED is True


def test_shutdown_refuses_docker(monkeypatch):
    monkeypatch.setattr(self_update, "in_docker", lambda: True)
    result = server.schedule_shutdown()
    assert result["ok"] is False
    assert "docker compose stop" in result["message"]
    assert server._SHUTDOWN_STARTED is False


def test_admin_shutdown_endpoint(monkeypatch):
    monkeypatch.setattr(server, "ALLOW_NO_ADMIN_AUTH", True)
    monkeypatch.setattr(server, "LOCAL_MODE", False)
    seen = {}

    def fake_schedule(delay=0.4):
        seen["delay"] = delay
        return {"ok": True, "message": "正在关闭"}

    monkeypatch.setattr(server, "schedule_shutdown", fake_schedule)
    payload = asyncio.run(server.admin_shutdown())
    assert payload["ok"] is True
    assert seen["delay"] == 0.4


def test_shutdown_button_is_in_topbar():
    text = Path("web/index.html").read_text(encoding="utf-8")
    assert "/admin/shutdown" in text
    assert "shutdownGateway" in text
    assert "stop-cta" in text
    assert "hardRefresh" not in text
    assert "refresh-cta" not in text
    topbar = text.split('<header class="topbar">', 1)[1].split("</header>", 1)[0]
    assert "check-cta" in topbar
    assert "检查更新" in topbar
    assert "update-cta" in topbar
    assert "stop-cta" in topbar
    assert "更新到 v" in topbar
    assert "shutdownGateway" in topbar
    assert "关闭" in topbar
    settings = text.split('<div class="phead"><h1>设置</h1>', 1)[1]
    assert "shutdownGateway" not in settings
