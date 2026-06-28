import threading

import pytest
from fastapi import HTTPException

from autoteam import api


def test_finish_admin_login_does_not_trigger_main_codex_sync(monkeypatch):
    events = []

    class FakeAdminLoginAPI:
        def complete_admin_login(self):
            events.append("admin_complete")
            return {
                "email": "owner@example.com",
                "session_token": "session-token",
                "account_id": "acc-1",
                "workspace_name": "Idapro",
            }

        def stop(self):
            events.append("admin_stop")

    lock = threading.Lock()
    assert lock.acquire(blocking=False) is True

    monkeypatch.setattr(api, "_playwright_lock", lock)
    monkeypatch.setattr(api, "_admin_login_api", FakeAdminLoginAPI())
    monkeypatch.setattr(api, "_admin_login_step", "workspace_required")
    monkeypatch.setattr(api, "_main_codex_flow", None)
    monkeypatch.setattr(api, "_main_codex_step", None)
    monkeypatch.setattr(api, "_main_codex_action", None)
    monkeypatch.setattr(api._pw_executor, "run", lambda func, *args, **kwargs: func(*args, **kwargs))
    monkeypatch.setattr(
        "autoteam.admin_state.get_admin_state_summary",
        lambda: {
            "configured": False,
            "email": "",
            "password_saved": False,
            "session_present": False,
            "account_id": "",
            "workspace_name": "",
        },
    )

    result = api._finish_admin_login({"step": "completed"})

    assert result == {
        "status": "completed",
        "admin": {
            "configured": False,
            "email": "",
            "password_saved": False,
            "session_present": False,
            "account_id": "",
            "workspace_name": "",
            "login_step": None,
            "login_in_progress": False,
            "workspace_options": [],
        },
        "codex": {"in_progress": False, "step": None, "action": None},
        "info": {
            "email": "owner@example.com",
            "session_token": "session-token",
            "account_id": "acc-1",
            "workspace_name": "Idapro",
        },
    }
    assert api._main_codex_flow is None
    assert api._main_codex_step is None
    assert api._main_codex_action is None
    assert api._admin_login_api is None
    assert api._admin_login_step is None
    assert lock.locked() is False
    assert events == ["admin_complete", "admin_stop"]


def test_finish_main_codex_flow_returns_login_message(monkeypatch):
    events = []

    class FakeMainCodexFlow:
        def complete(self):
            events.append("complete")
            return {
                "email": "owner@example.com",
                "auth_file": "/tmp/codex-main-acc-1.json",
                "plan_type": "team",
            }

        def stop(self):
            events.append("stop")

    lock = threading.Lock()
    assert lock.acquire(blocking=False) is True

    monkeypatch.setattr(api, "_playwright_lock", lock)
    monkeypatch.setattr(api, "_main_codex_flow", FakeMainCodexFlow())
    monkeypatch.setattr(api, "_main_codex_step", "code_required")
    monkeypatch.setattr(api, "_main_codex_action", "login")
    monkeypatch.setattr(api._pw_executor, "run", lambda func, *args, **kwargs: func(*args, **kwargs))

    result = api._finish_main_codex_flow()

    assert result == {
        "status": "completed",
        "message": "主号 Codex 已登录",
        "codex": {"in_progress": False, "step": None, "action": None},
        "info": {
            "email": "owner@example.com",
            "auth_file": "/tmp/codex-main-acc-1.json",
            "plan_type": "team",
        },
    }
    assert api._main_codex_flow is None
    assert api._main_codex_step is None
    assert api._main_codex_action is None
    assert lock.locked() is False
    assert events == ["complete", "stop"]


def test_main_codex_mutation_endpoints_are_disabled():
    for call in (
        api.post_main_codex_start,
        api.post_main_codex_login,
        lambda: api.post_main_codex_password(api.AdminPasswordParams(password="secret")),
        lambda: api.post_main_codex_code(api.AdminCodeParams(code="123456")),
        api.post_main_codex_delete_cpa,
        api.post_main_codex_delete_remote_files,
    ):
        with pytest.raises(HTTPException) as exc_info:
            call()
        assert exc_info.value.status_code == 410
        assert "swap_seat-only" in str(exc_info.value.detail)
