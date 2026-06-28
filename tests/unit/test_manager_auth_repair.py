import json
import logging

from autoteam import manager


class _FakeMailClient:
    provider_name = "cloudmail"

    def login(self):
        return None


def test_record_auth_repair_failure_schedules_add_phone_retry_when_enabled(monkeypatch):
    updates = []
    monkeypatch.setattr(
        manager,
        "load_accounts",
        lambda: [
            {
                "email": "user@example.com",
                "status": "auth_pending",
                "auth_retry_count": 5,
                "auth_last_error": "auth_code_missing",
            }
        ],
    )
    monkeypatch.setattr(manager, "update_account", lambda email, **kwargs: updates.append((email, kwargs)))
    monkeypatch.setattr(manager.time, "time", lambda: 1_700_000_000)
    monkeypatch.setattr(manager, "_auth_repair_retry_add_phone_enabled", lambda: True)
    monkeypatch.setattr(manager, "_auth_repair_add_phone_max_retries", lambda: 3)
    monkeypatch.setattr(manager, "_auth_repair_add_phone_retry_delays", lambda max_retries=None: (300, 600, 1_200))
    monkeypatch.setattr(manager, "_is_email_in_team", lambda _email: True)
    monkeypatch.setattr(
        manager,
        "_release_auth_repair_team_seat",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("should not release team seat before retries exhaust")
        ),
    )

    state = manager._record_auth_repair_failure("user@example.com", "add_phone", "需要手机号验证")

    assert state["auth_retry_count"] == 1
    assert state["auth_retry_paused"] is False
    assert state["auth_retry_after"] == 1_700_000_300
    assert state["status"] == "auth_pending"
    assert updates == [
        (
            "user@example.com",
            {
                "auth_retry_count": 1,
                "auth_last_error": "add_phone",
                "auth_last_error_detail": "需要手机号验证",
                "auth_last_failed_at": 1_700_000_000,
                "auth_retry_after": 1_700_000_300,
                "auth_retry_paused": False,
            },
        ),
        ("user@example.com", {"status": "auth_pending"}),
    ]


def test_record_auth_repair_failure_uses_auto_check_interval_backoff(monkeypatch):
    updates = []
    monkeypatch.setattr(
        manager,
        "load_accounts",
        lambda: [{"email": "user@example.com", "status": "auth_pending", "auth_retry_count": 0}],
    )
    monkeypatch.setattr(manager, "update_account", lambda email, **kwargs: updates.append((email, kwargs)))
    monkeypatch.setattr(manager.time, "time", lambda: 1_700_000_000)
    monkeypatch.setattr(manager, "_auth_repair_retry_delays", lambda: (600, 1200, 1800))
    monkeypatch.setattr(manager, "_is_email_in_team", lambda _email: True)

    state = manager._record_auth_repair_failure("user@example.com", "auth_code_missing", "未获取到 auth code")

    assert state["auth_retry_count"] == 1
    assert state["auth_retry_after"] == 1_700_000_600
    assert updates == [
        (
            "user@example.com",
            {
                "auth_retry_count": 1,
                "auth_last_error": "auth_code_missing",
                "auth_last_error_detail": "未获取到 auth code",
                "auth_last_failed_at": 1_700_000_000,
                "auth_retry_after": 1_700_000_600,
                "auth_retry_paused": False,
            },
        ),
        ("user@example.com", {"status": "auth_pending"}),
    ]


def test_record_auth_repair_failure_pauses_on_add_phone_when_retry_disabled(monkeypatch):
    updates = []
    monkeypatch.setattr(
        manager,
        "load_accounts",
        lambda: [{"email": "user@example.com", "status": "auth_pending", "auth_retry_count": 1}],
    )
    monkeypatch.setattr(manager, "update_account", lambda email, **kwargs: updates.append((email, kwargs)))
    monkeypatch.setattr(manager.time, "time", lambda: 1_700_000_000)
    monkeypatch.setattr(manager, "_auth_repair_retry_add_phone_enabled", lambda: False)
    monkeypatch.setattr(manager, "_is_email_in_team", lambda _email: True)
    monkeypatch.setattr(
        manager,
        "_release_auth_repair_team_seat",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("should not release team seat when retry is disabled")
        ),
    )

    state = manager._record_auth_repair_failure("user@example.com", "add_phone", "需要手机号验证")

    assert state["auth_retry_paused"] is True
    assert state["auth_retry_after"] is None
    assert state["status"] == "auth_pending"
    assert updates[-1] == ("user@example.com", {"status": "auth_pending"})


def test_record_auth_repair_failure_disables_managed_auth_on_hard_failure(monkeypatch):
    disabled = []
    monkeypatch.setattr(
        manager,
        "load_accounts",
        lambda: [
            {
                "email": "user@example.com",
                "status": "auth_pending",
                "auth_file": "/tmp/codex-user@example.com-pat.json",
            }
        ],
    )
    monkeypatch.setattr(manager, "update_account", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(manager, "is_managed_cpa_auth", lambda name: name == "codex-user@example.com-pat.json")
    monkeypatch.setattr(manager, "set_cpa_auth_disabled", lambda name, disabled_flag: disabled.append((name, disabled_flag)))
    monkeypatch.setattr(manager, "_is_email_in_team", lambda _email: True)

    state = manager._record_auth_repair_failure("user@example.com", "account_deactivated", "账号已删除或停用")

    assert state["auth_retry_paused"] is True
    assert state["disabled_auth_status"] == "disabled"
    assert disabled == [("codex-user@example.com-pat.json", True)]


def test_record_auth_repair_failure_releases_team_seat_after_add_phone_retries_exhausted(monkeypatch):
    updates = []
    monkeypatch.setattr(
        manager,
        "load_accounts",
        lambda: [
            {
                "email": "user@example.com",
                "status": "auth_pending",
                "auth_retry_count": 3,
                "auth_last_error": "add_phone",
            }
        ],
    )
    monkeypatch.setattr(manager, "update_account", lambda email, **kwargs: updates.append((email, kwargs)))
    monkeypatch.setattr(manager.time, "time", lambda: 1_700_000_000)
    monkeypatch.setattr(manager, "_auth_repair_retry_add_phone_enabled", lambda: True)
    monkeypatch.setattr(manager, "_auth_repair_add_phone_max_retries", lambda: 3)
    monkeypatch.setattr(manager, "_is_email_in_team", lambda _email: True)
    monkeypatch.setattr(manager, "_release_auth_repair_team_seat", lambda *_args, **_kwargs: "removed")

    state = manager._record_auth_repair_failure("user@example.com", "add_phone", "需要手机号验证")

    assert state["auth_retry_count"] == 4
    assert state["auth_retry_paused"] is True
    assert state["auth_retry_after"] is None
    assert state["status"] == "standby"
    assert state["seat_released"] is True
    assert updates == [
        (
            "user@example.com",
            {
                "auth_retry_count": 4,
                "auth_last_error": "add_phone",
                "auth_last_error_detail": "需要手机号验证",
                "auth_last_failed_at": 1_700_000_000,
                "auth_retry_after": None,
                "auth_retry_paused": True,
            },
        ),
        ("user@example.com", {"status": "standby"}),
    ]


def test_record_auth_repair_failure_can_force_release_team_seat_for_rejoin_failures(monkeypatch):
    updates = []
    monkeypatch.setattr(
        manager,
        "load_accounts",
        lambda: [{"email": "user@example.com", "status": "standby", "auth_retry_count": 0}],
    )
    monkeypatch.setattr(manager, "update_account", lambda email, **kwargs: updates.append((email, kwargs)))
    monkeypatch.setattr(manager.time, "time", lambda: 1_700_000_000)
    monkeypatch.setattr(manager, "_auth_repair_retry_delays", lambda: (600, 1200, 1800))
    monkeypatch.setattr(manager, "_is_email_in_team", lambda _email: True)
    monkeypatch.setattr(manager, "_release_auth_repair_team_seat", lambda *_args, **_kwargs: "removed")

    state = manager._record_auth_repair_failure(
        "user@example.com",
        "auth_code_missing",
        "未获取到 auth code",
        release_team_seat=True,
    )

    assert state["auth_retry_count"] == 1
    assert state["status"] == "standby"
    assert state["seat_released"] is True
    assert updates == [
        (
            "user@example.com",
            {
                "auth_retry_count": 1,
                "auth_last_error": "auth_code_missing",
                "auth_last_error_detail": "未获取到 auth code",
                "auth_last_failed_at": 1_700_000_000,
                "auth_retry_after": 1_700_000_600,
                "auth_retry_paused": False,
            },
        ),
        ("user@example.com", {"status": "standby"}),
    ]


def test_login_codex_with_result_retries_retryable_failures_within_same_round(monkeypatch):
    attempts = {"count": 0}

    def fake_login(email, password, mail_client=None, return_result=False):
        assert return_result is True
        attempts["count"] += 1
        if attempts["count"] < 3:
            return {
                "ok": False,
                "bundle": None,
                "error_type": "auth_code_missing",
                "error_detail": "未获取到 auth code",
                "retryable": True,
            }
        return {
            "ok": True,
            "bundle": {"email": email, "plan_type": "team"},
            "error_type": None,
            "error_detail": None,
            "retryable": False,
        }

    monkeypatch.setattr(manager, "login_codex_via_browser", fake_login)

    result = manager._login_codex_with_result("user@example.com", "", max_attempts=3)

    assert attempts["count"] == 3
    assert result["ok"] is True
    assert result["bundle"]["plan_type"] == "team"
    assert result["attempts"] == 3


def test_cmd_repair_pat_auths_regenerates_auth_for_pending_account(monkeypatch):
    updates = []

    monkeypatch.setattr(
        manager,
        "load_accounts",
        lambda: [
            {
                "email": "repair@example.com",
                "password": "secret",
                "status": "auth_pending",
                "mail_provider": "cloudflare_temp_email",
                "mail_account_id": "addr-1",
                "managed_by_autoteam": True,
                "chatgpt_session_token": "member-session",
                "chatgpt_account_id": "acc-1",
                "disabled": False,
            }
        ],
    )
    monkeypatch.setattr(manager, "update_account", lambda email, **kwargs: updates.append((email, kwargs)))

    class _Mail:
        provider_name = "cloudflare_temp_email"

        def login(self):
            return None

    monkeypatch.setattr(manager, "_get_account_mail_client", lambda _acc: _Mail())
    monkeypatch.setattr(manager, "generate_signup_profile", lambda: None)
    monkeypatch.setattr(
        manager,
        "_create_pat_auth_with_session",
        lambda acc, **_kwargs: {
            "ok": True,
            "auth": {
                "auth_file": "/tmp/codex-repair@example.com-pat-123.json",
                "filename": "codex-repair@example.com-pat-123.json",
            },
        },
    )
    monkeypatch.setattr(
        manager,
        "_create_pat_auth_with_login",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("should use saved member session")),
    )

    class _ChatGPT:
        browser = True

        def __init__(self):
            self.seat_calls = []

        def update_member_seat_type(self, user_id, seat_type):
            self.seat_calls.append((user_id, seat_type))
            return {"status": 200}

    fake_chatgpt = _ChatGPT()
    monkeypatch.setattr(manager, "ChatGPTTeamAPI", lambda: fake_chatgpt)
    monkeypatch.setattr(manager, "_start_chatgpt_for_team", lambda _chatgpt, _team_context=None: _chatgpt)
    monkeypatch.setattr(
        manager,
        "_fetch_team_members_for_account",
        lambda _chatgpt, _account_id=None: [
            {"email": "repair@example.com", "id": "u-repair", "seat_type": "usage_based"}
        ],
    )
    monkeypatch.setattr(manager, "_team_account_id", lambda _team_context=None: "acc-1")

    result = manager.cmd_repair_pat_auths(allow_promote_for_use=True)

    assert result["summary"]["repaired"] == 1
    assert fake_chatgpt.seat_calls == [("u-repair", "default")]
    assert len(updates) == 1
    assert updates[0][0] == "repair@example.com"
    assert updates[0][1]["status"] == "active"
    assert updates[0][1]["auth_file"] == "/tmp/codex-repair@example.com-pat-123.json"
    assert updates[0][1]["auth_last_error"] is None
    assert updates[0][1]["managed_by_autoteam"] is True


def test_cmd_repair_pat_auths_skips_non_chatgpt_seat_by_default(monkeypatch):
    monkeypatch.setattr(
        manager,
        "load_accounts",
        lambda: [
            {
                "email": "repair@example.com",
                "password": "secret",
                "status": "auth_pending",
                "mail_provider": "cloudflare_temp_email",
                "mail_account_id": "addr-1",
                "managed_by_autoteam": True,
                "chatgpt_session_token": "member-session",
                "chatgpt_account_id": "acc-1",
                "disabled": False,
            }
        ],
    )

    class _ChatGPT:
        browser = True

        def update_member_seat_type(self, *_args):
            raise AssertionError("non-GPT seat should not be promoted by default")

    monkeypatch.setattr(manager, "ChatGPTTeamAPI", lambda: _ChatGPT())
    monkeypatch.setattr(manager, "_start_chatgpt_for_team", lambda _chatgpt, _team_context=None: _chatgpt)
    monkeypatch.setattr(
        manager,
        "_fetch_team_members_for_account",
        lambda _chatgpt, _account_id=None: [
            {"email": "repair@example.com", "id": "u-repair", "seat_type": "usage_based"}
        ],
    )
    monkeypatch.setattr(manager, "_team_account_id", lambda _team_context=None: "acc-1")
    monkeypatch.setattr(
        manager,
        "_create_pat_auth_with_session",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("non-GPT seat should not repair PAT")),
    )

    result = manager.cmd_repair_pat_auths()

    assert result["summary"]["repaired"] == 0
    assert result["summary"]["skipped"] == 1
    assert result["skipped"][0]["reason"] == "not_chatgpt_seat"


def test_cmd_repair_pat_auths_does_not_demote_original_chatgpt_on_failure(monkeypatch):
    monkeypatch.setenv("AUTO_CHECK_DISABLE_MEMBER_EMAIL_LOGIN_FOR_PAT", "true")
    monkeypatch.setattr(
        manager,
        "load_accounts",
        lambda: [
            {
                "email": "repair@example.com",
                "password": "secret",
                "status": "auth_pending",
                "mail_provider": "cloudflare_temp_email",
                "mail_account_id": "addr-1",
                "managed_by_autoteam": True,
                "chatgpt_session_token": "member-session",
                "chatgpt_account_id": "acc-1",
                "disabled": False,
            }
        ],
    )

    class _ChatGPT:
        browser = True

        def __init__(self):
            self.seat_calls = []

        def update_member_seat_type(self, user_id, seat_type):
            self.seat_calls.append((user_id, seat_type))
            return {"status": 200}

    fake_chatgpt = _ChatGPT()
    monkeypatch.setattr(manager, "ChatGPTTeamAPI", lambda: fake_chatgpt)
    monkeypatch.setattr(manager, "_start_chatgpt_for_team", lambda _chatgpt, _team_context=None: _chatgpt)
    monkeypatch.setattr(
        manager,
        "_fetch_team_members_for_account",
        lambda _chatgpt, _account_id=None: [
            {"email": "repair@example.com", "id": "u-repair", "seat_type": "default"}
        ],
    )
    monkeypatch.setattr(manager, "_team_account_id", lambda _team_context=None: "acc-1")
    monkeypatch.setattr(
        manager,
        "_create_pat_auth_with_session",
        lambda *_args, **_kwargs: {
            "ok": False,
            "error_type": "token_exchange_failed",
            "error_detail": "token exchange failed",
        },
    )
    monkeypatch.setattr(
        manager,
        "_record_auth_repair_failure",
        lambda email, error_type=None, error_detail=None, **_kwargs: {
            "status": "auth_pending",
            "error_type": error_type,
            "error_detail": error_detail,
        },
    )

    result = manager.cmd_repair_pat_auths(allow_promote_for_use=True)

    assert result["summary"]["failed"] == 1
    assert fake_chatgpt.seat_calls == []


def test_cmd_repair_pat_auths_restores_only_original_codex_seat_on_failure(monkeypatch):
    monkeypatch.setenv("AUTO_CHECK_DISABLE_MEMBER_EMAIL_LOGIN_FOR_PAT", "true")
    monkeypatch.setattr(
        manager,
        "load_accounts",
        lambda: [
            {
                "email": "repair@example.com",
                "password": "secret",
                "status": "auth_pending",
                "mail_provider": "cloudflare_temp_email",
                "mail_account_id": "addr-1",
                "managed_by_autoteam": True,
                "chatgpt_session_token": "member-session",
                "chatgpt_account_id": "acc-1",
                "disabled": False,
            }
        ],
    )

    class _ChatGPT:
        browser = True

        def __init__(self):
            self.seat_calls = []

        def update_member_seat_type(self, user_id, seat_type):
            self.seat_calls.append((user_id, seat_type))
            return {"status": 200}

    fake_chatgpt = _ChatGPT()
    monkeypatch.setattr(manager, "ChatGPTTeamAPI", lambda: fake_chatgpt)
    monkeypatch.setattr(manager, "_start_chatgpt_for_team", lambda _chatgpt, _team_context=None: _chatgpt)
    monkeypatch.setattr(
        manager,
        "_fetch_team_members_for_account",
        lambda _chatgpt, _account_id=None: [
            {"email": "repair@example.com", "id": "u-repair", "seat_type": "usage_based"}
        ],
    )
    monkeypatch.setattr(manager, "_team_account_id", lambda _team_context=None: "acc-1")
    monkeypatch.setattr(
        manager,
        "_create_pat_auth_with_session",
        lambda *_args, **_kwargs: {
            "ok": False,
            "error_type": "token_exchange_failed",
            "error_detail": "token exchange failed",
        },
    )
    monkeypatch.setattr(
        manager,
        "_record_auth_repair_failure",
        lambda email, error_type=None, error_detail=None, **_kwargs: {
            "status": "auth_pending",
            "error_type": error_type,
            "error_detail": error_detail,
        },
    )

    result = manager.cmd_repair_pat_auths(allow_promote_for_use=True)

    assert result["summary"]["failed"] == 1
    assert fake_chatgpt.seat_calls == [("u-repair", "default"), ("u-repair", "usage_based")]


def test_cmd_repair_pat_auths_scopes_repair_to_team_context(monkeypatch):
    updates = []
    start_contexts = []
    fetched_account_ids = []
    session_calls = []

    class _Team:
        account_id = "acc-b"
        session_token = "team-session"
        workspace_name = "Team B"
        label = "Team B"

    team = _Team()

    monkeypatch.setattr(
        manager,
        "load_accounts",
        lambda: [
            {
                "email": "repair-a@example.com",
                "password": "secret",
                "status": "auth_pending",
                "mail_provider": "cloudflare_temp_email",
                "mail_account_id": "addr-a",
                "managed_by_autoteam": True,
                "chatgpt_session_token": "member-session-a",
                "chatgpt_account_id": "acc-a",
                "disabled": False,
            },
            {
                "email": "repair-b@example.com",
                "password": "secret",
                "status": "auth_pending",
                "mail_provider": "cloudflare_temp_email",
                "mail_account_id": "addr-b",
                "managed_by_autoteam": True,
                "chatgpt_session_token": "member-session-b",
                "chatgpt_account_id": "acc-b",
                "disabled": False,
            },
        ],
    )
    monkeypatch.setattr(manager, "update_account", lambda email, **kwargs: updates.append((email, kwargs)))
    monkeypatch.setattr(manager, "ChatGPTTeamAPI", lambda: type("_ChatGPT", (), {"browser": True})())
    monkeypatch.setattr(
        manager,
        "_start_chatgpt_for_team",
        lambda chatgpt, team_context=None: start_contexts.append(team_context) or chatgpt,
    )

    def fake_fetch(_chatgpt, account_id=None):
        fetched_account_ids.append(account_id)
        return [{"email": "repair-b@example.com", "id": "u-b", "seat_type": "default"}]

    monkeypatch.setattr(manager, "_fetch_team_members_for_account", fake_fetch)

    def fake_create_with_session(acc, **kwargs):
        session_calls.append((acc["email"], kwargs.get("team_context")))
        return {
            "ok": True,
            "auth": {
                "auth_file": f"/tmp/codex-{acc['email']}-pat.json",
                "filename": f"codex-{acc['email']}-pat.json",
            },
        }

    monkeypatch.setattr(manager, "_create_pat_auth_with_session", fake_create_with_session)

    result = manager.cmd_repair_pat_auths(team_context=team)

    assert result["candidates"] == 1
    assert result["summary"]["repaired"] == 1
    assert start_contexts == [team]
    assert fetched_account_ids == ["acc-b"]
    assert session_calls == [("repair-b@example.com", team)]
    assert [item[0] for item in updates] == ["repair-b@example.com"]


def test_cmd_repair_pat_auths_skips_password_only_unmanaged_account(monkeypatch):
    monkeypatch.setattr(
        manager,
        "load_accounts",
        lambda: [
            {
                "email": "manual@example.com",
                "password": "secret",
                "status": "auth_pending",
                "mail_provider": "cloudflare_temp_email",
                "mail_account_id": "addr-should-not-matter",
                "managed_by_autoteam": "false",
                "disabled": False,
            }
        ],
    )
    monkeypatch.setattr(
        manager,
        "ChatGPTTeamAPI",
        lambda: (_ for _ in ()).throw(AssertionError("unmanaged account should not start PAT repair client")),
    )
    monkeypatch.setattr(
        manager,
        "_create_pat_auth_with_login",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("unmanaged account should not login")),
    )

    result = manager.cmd_repair_pat_auths()

    assert result["candidates"] == 0
    assert result["summary"]["repaired"] == 0
    assert result["summary"]["failed"] == 0


def test_cmd_repair_pat_auths_recaptures_missing_session_for_usable_account(monkeypatch):
    updates = []
    mail_logins = []

    monkeypatch.delenv("AUTO_CHECK_ALLOW_MEMBER_EMAIL_LOGIN_FOR_PAT", raising=False)
    monkeypatch.delenv("AUTO_CHECK_DISABLE_MEMBER_EMAIL_LOGIN_FOR_PAT", raising=False)
    monkeypatch.setattr(
        manager,
        "load_accounts",
        lambda: [
            {
                "email": "repair@example.com",
                "password": "secret",
                "status": "auth_pending",
                "mail_provider": "cloudflare_temp_email",
                "mail_account_id": "addr-1",
                "managed_by_autoteam": True,
                "disabled": False,
            }
        ],
    )
    monkeypatch.setattr(manager, "update_account", lambda email, **kwargs: updates.append((email, kwargs)))

    class _Mail:
        provider_name = "cloudflare_temp_email"

        def login(self):
            mail_logins.append(True)

    class _ChatGPT:
        browser = True

    monkeypatch.setattr(manager, "_get_account_mail_client", lambda _acc: _Mail())
    monkeypatch.setattr(manager, "ChatGPTTeamAPI", lambda: _ChatGPT())
    monkeypatch.setattr(manager, "_start_chatgpt_for_team", lambda _chatgpt, _team_context=None: _chatgpt)
    monkeypatch.setattr(
        manager,
        "_fetch_team_members_for_account",
        lambda _chatgpt, _account_id=None: [
            {"email": "repair@example.com", "id": "u-repair", "seat_type": "default"}
        ],
    )
    monkeypatch.setattr(manager, "_team_account_id", lambda _team_context=None: "acc-1")
    monkeypatch.setattr(
        manager,
        "_create_pat_auth_with_session",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("missing session should recapture first")),
    )
    monkeypatch.setattr(
        manager,
        "_create_pat_auth_with_login",
        lambda email, password, **_kwargs: {
            "ok": True,
            "auth": {
                "auth_file": f"/tmp/codex-{email}-pat.json",
                "filename": f"codex-{email}-pat.json",
            },
            "session": {
                "chatgpt_session_token": "new-member-session",
                "chatgpt_account_id": "acc-1",
                "chatgpt_session_email": email,
            },
        },
    )

    result = manager.cmd_repair_pat_auths()

    assert result["summary"]["repaired"] == 1
    assert result["summary"]["skipped"] == 0
    assert mail_logins == [True]
    assert updates[0][0] == "repair@example.com"
    assert updates[0][1]["status"] == "active"
    assert updates[0][1]["chatgpt_session_token"] == "new-member-session"


def test_cmd_repair_pat_auths_recaptures_when_saved_session_is_invalid(monkeypatch):
    updates = []
    mail_logins = []

    monkeypatch.setattr(
        manager,
        "load_accounts",
        lambda: [
            {
                "email": "repair@example.com",
                "password": "secret",
                "status": "auth_pending",
                "mail_provider": "cloudflare_temp_email",
                "mail_account_id": "addr-1",
                "managed_by_autoteam": True,
                "chatgpt_session_token": "old-session",
                "chatgpt_account_id": "acc-1",
                "disabled": False,
            }
        ],
    )
    monkeypatch.setattr(manager, "update_account", lambda email, **kwargs: updates.append((email, kwargs)))

    class _Mail:
        provider_name = "cloudflare_temp_email"

        def login(self):
            mail_logins.append(True)

    class _ChatGPT:
        browser = True

    monkeypatch.setattr(manager, "_get_account_mail_client", lambda _acc: _Mail())
    monkeypatch.setattr(manager, "ChatGPTTeamAPI", lambda: _ChatGPT())
    monkeypatch.setattr(manager, "_start_chatgpt_for_team", lambda _chatgpt, _team_context=None: _chatgpt)
    monkeypatch.setattr(
        manager,
        "_fetch_team_members_for_account",
        lambda _chatgpt, _account_id=None: [
            {"email": "repair@example.com", "id": "u-repair", "seat_type": "default"}
        ],
    )
    monkeypatch.setattr(manager, "_team_account_id", lambda _team_context=None: "acc-1")
    monkeypatch.setattr(
        manager,
        "_create_pat_auth_with_session",
        lambda *_args, **_kwargs: {
            "ok": False,
            "error_type": "member_session_invalid",
            "error_detail": "HTTP 401",
            "retryable": False,
        },
    )
    monkeypatch.setattr(
        manager,
        "_create_pat_auth_with_login",
        lambda email, password, **_kwargs: {
            "ok": True,
            "auth": {
                "auth_file": f"/tmp/codex-{email}-pat.json",
                "filename": f"codex-{email}-pat.json",
            },
            "session": {
                "chatgpt_session_token": "fresh-session",
                "chatgpt_account_id": "acc-1",
                "chatgpt_session_email": email,
            },
        },
    )

    result = manager.cmd_repair_pat_auths()

    assert result["summary"]["repaired"] == 1
    assert mail_logins == [True]
    assert updates[0][1]["chatgpt_session_token"] == "fresh-session"


def test_cmd_repair_pat_auths_repairs_active_account_when_cpa_auth_is_revoked(tmp_path, monkeypatch):
    auth_file = tmp_path / "codex-repair@example.com-pat-123.json"
    auth_file.write_text("{}", encoding="utf-8")
    updates = []

    monkeypatch.setattr(
        manager,
        "load_accounts",
        lambda: [
            {
                "email": "repair@example.com",
                "password": "secret",
                "status": "active",
                "auth_file": str(auth_file),
                "managed_by_autoteam": True,
                "chatgpt_session_token": "member-session",
                "chatgpt_account_id": "acc-1",
                "disabled": False,
            }
        ],
    )
    monkeypatch.setattr(manager, "update_account", lambda email, **kwargs: updates.append((email, kwargs)))
    monkeypatch.setattr(
        manager,
        "list_cpa_files",
        lambda: [
            {
                "name": auth_file.name,
                "email": "repair@example.com",
                "status": "active",
                "disabled": False,
                "auth_index": "idx-1",
            }
        ],
    )
    monkeypatch.setattr(manager, "is_managed_cpa_auth", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(
        manager,
        "check_cpa_codex_quota",
        lambda *_args, **_kwargs: ("auth_error", {"status_code": 401, "body": "token_revoked"}),
    )
    monkeypatch.setattr(
        manager,
        "_create_pat_auth_with_session",
        lambda acc, **_kwargs: {
            "ok": True,
            "auth": {
                "auth_file": str(auth_file),
                "filename": auth_file.name,
            },
        },
    )

    class _ChatGPT:
        browser = True

    monkeypatch.setattr(manager, "ChatGPTTeamAPI", lambda: _ChatGPT())
    monkeypatch.setattr(manager, "_start_chatgpt_for_team", lambda _chatgpt, _team_context=None: _chatgpt)
    monkeypatch.setattr(
        manager,
        "_fetch_team_members_for_account",
        lambda _chatgpt, _account_id=None: [
            {"email": "repair@example.com", "id": "u-repair", "seat_type": "default"}
        ],
    )
    monkeypatch.setattr(manager, "_team_account_id", lambda _team_context=None: "acc-1")

    result = manager.cmd_repair_pat_auths()

    assert result["summary"]["repaired"] == 1
    assert updates[0][0] == "repair@example.com"
    assert updates[0][1]["status"] == "auth_pending"
    assert updates[-1][0] == "repair@example.com"
    assert updates[-1][1]["status"] == "active"


def test_cmd_repair_pat_auths_skips_only_when_quota_window_not_reset(monkeypatch):
    monkeypatch.setattr(manager.time, "time", lambda: 1_700_000_000)
    monkeypatch.setattr(
        manager,
        "load_accounts",
        lambda: [
            {
                "email": "repair@example.com",
                "password": "secret",
                "status": "auth_pending",
                "mail_provider": "cloudflare_temp_email",
                "managed_by_autoteam": True,
                "quota_resets_at": 1_700_003_600,
                "quota_window": "weekly",
                "disabled": False,
            }
        ],
    )
    monkeypatch.setattr(
        manager,
        "ChatGPTTeamAPI",
        lambda: (_ for _ in ()).throw(AssertionError("quota-exhausted account should not start repair client")),
    )

    result = manager.cmd_repair_pat_auths()

    assert result["candidates"] == 0
    assert result["summary"]["skipped"] == 1
    assert result["skipped"][0]["reason"] == "quota_exhausted"


def test_get_account_mail_client_uses_inferred_provider_for_unbound_account(monkeypatch):
    captured = []

    monkeypatch.setenv("MAIL_PROVIDER", "cloudflare_temp_email")
    monkeypatch.setenv("CLOUDMAIL_DOMAIN", "@52100521.xyz")
    monkeypatch.setenv("CF_TEMP_EMAIL_DOMAIN", "xxmail.idapro.tech")
    monkeypatch.setattr(
        manager,
        "get_mail_client_for_account",
        lambda acc: captured.append(manager.get_account_mail_provider(acc)) or object(),
    )

    manager._get_account_mail_client(
        {
            "email": "tmp-9c0ebe17@52100521.xyz",
            "mail_provider": None,
            "mail_account_id": None,
            "cloudmail_account_id": None,
        }
    )

    assert captured == ["cloudmail"]


def test_get_account_mail_client_falls_back_to_default_client_when_domain_is_unknown(monkeypatch):
    sentinel = object()

    monkeypatch.delenv("CLOUDMAIL_DOMAIN", raising=False)
    monkeypatch.delenv("CF_TEMP_EMAIL_DOMAIN", raising=False)
    monkeypatch.setattr(manager, "CloudMailClient", lambda: sentinel)

    client = manager._get_account_mail_client(
        {
            "email": "old-1@example.com",
            "mail_provider": None,
            "mail_account_id": None,
            "cloudmail_account_id": None,
        }
    )

    assert client is sentinel


def test_sync_account_states_infers_provider_for_existing_and_new_accounts(monkeypatch, tmp_path):
    accounts = [
        {
            "email": "tmp-old@52100521.xyz",
            "password": "",
            "mail_provider": None,
            "mail_account_id": None,
            "cloudmail_account_id": None,
            "status": "standby",
            "auth_file": None,
            "quota_exhausted_at": None,
            "quota_resets_at": None,
            "created_at": 0,
            "last_active_at": None,
        }
    ]
    saved = {}

    class _FakeChatGPT:
        def _api_fetch(self, method, path):
            assert method == "GET"
            assert path == "/backend-api/accounts/acc-1/users?offset=0&limit=25&query="
            return {
                "status": 200,
                "body": (
                    '{"items":[{"email":"tmp-old@52100521.xyz"},'
                    '{"email":"tmp-new@xxmail.idapro.tech"}],"total":2,"limit":25,"offset":0} '
                ),
            }

    monkeypatch.setenv("MAIL_PROVIDER", "cloudflare_temp_email")
    monkeypatch.setenv("CLOUDMAIL_DOMAIN", "@52100521.xyz")
    monkeypatch.setenv("CF_TEMP_EMAIL_DOMAIN", "xxmail.idapro.tech")
    monkeypatch.setattr(manager, "get_chatgpt_account_id", lambda: "acc-1")
    monkeypatch.setattr(manager, "_chatgpt_session_ready", lambda _chatgpt: True)
    monkeypatch.setattr(manager, "load_accounts", lambda: accounts)
    monkeypatch.setattr(manager, "save_accounts", lambda items: saved.setdefault("accounts", [dict(i) for i in items]))
    monkeypatch.setattr("autoteam.codex_auth.AUTH_DIR", tmp_path)

    manager.sync_account_states(chatgpt_api=_FakeChatGPT())

    saved_accounts = saved["accounts"]
    existing = next(acc for acc in saved_accounts if acc["email"] == "tmp-old@52100521.xyz")
    added = next(acc for acc in saved_accounts if acc["email"] == "tmp-new@xxmail.idapro.tech")

    assert existing["mail_provider"] == "cloudmail"
    assert added["mail_provider"] == "cloudflare_temp_email"
    assert added["status"] == "auth_pending"


def test_get_team_member_count_uses_paginated_members_endpoint(monkeypatch):
    class _FakeChatGPT:
        def __init__(self):
            self.paths = []

        def _api_fetch(self, method, path):
            assert method == "GET"
            self.paths.append(path)
            responses = {
                "/backend-api/accounts/acc-1/users?offset=0&limit=25&query=": {
                    "status": 200,
                    "body": '{"items":[{"email":"one@example.com"}],"total":2,"limit":1,"offset":0}',
                },
                "/backend-api/accounts/acc-1/users?offset=1&limit=25&query=": {
                    "status": 200,
                    "body": '{"items":[{"email":"two@example.com"}],"total":2,"limit":1,"offset":1}',
                },
            }
            return responses[path]

    fake = _FakeChatGPT()
    monkeypatch.setattr(manager, "get_chatgpt_account_id", lambda: "acc-1")

    assert manager.get_team_member_count(fake) == 2
    assert fake.paths == [
        "/backend-api/accounts/acc-1/users?offset=0&limit=25&query=",
        "/backend-api/accounts/acc-1/users?offset=1&limit=25&query=",
    ]


def test_login_codex_with_result_stops_immediately_on_hard_failure(monkeypatch):
    attempts = {"count": 0}

    def fake_login(email, password, mail_client=None, return_result=False):
        assert return_result is True
        attempts["count"] += 1
        return {
            "ok": False,
            "bundle": None,
            "error_type": "add_phone",
            "error_detail": "需要手机号验证",
            "retryable": False,
        }

    monkeypatch.setattr(manager, "login_codex_via_browser", fake_login)

    result = manager._login_codex_with_result("user@example.com", "", max_attempts=3)

    assert attempts["count"] == 1
    assert result["ok"] is False
    assert result["error_type"] == "add_phone"
    assert result["attempts"] == 1


def test_cmd_check_skips_disabled_accounts(monkeypatch):
    calls = []
    monkeypatch.setattr(
        manager,
        "load_accounts",
        lambda: [
            {"email": "pending@example.com", "status": "pending", "disabled": True},
            {"email": "active@example.com", "status": "active", "disabled": True, "auth_file": "/tmp/a.json"},
            {"email": "repair@example.com", "status": "auth_pending", "disabled": True, "auth_file": ""},
        ],
    )
    monkeypatch.setattr(
        manager,
        "ChatGPTTeamAPI",
        lambda: (_ for _ in ()).throw(
            AssertionError("disabled accounts should not trigger remote pending reconciliation")
        ),
    )
    monkeypatch.setattr(
        manager,
        "_check_and_refresh",
        lambda _acc: (_ for _ in ()).throw(AssertionError("disabled accounts should not trigger quota checks")),
    )
    monkeypatch.setattr(
        manager,
        "cmd_swap_seats",
        lambda max_chatgpt_active: calls.append(max_chatgpt_active) or {"mode": "swap_seat"},
    )

    assert manager.cmd_check() == {"mode": "swap_seat"}
    assert calls == [2]


def test_login_codex_with_result_rejects_non_team_bundle(monkeypatch):
    def fake_login(email, password, mail_client=None, return_result=False):
        assert return_result is True
        return {
            "ok": True,
            "bundle": {"email": email, "plan_type": "free"},
            "error_type": None,
            "error_detail": None,
            "retryable": False,
        }

    monkeypatch.setattr(manager, "login_codex_via_browser", fake_login)

    result = manager._login_codex_with_result("user@example.com", "", max_attempts=1)

    assert result["ok"] is False
    assert result["bundle"] is None
    assert result["error_type"] == "non_team_plan"
    assert result["attempts"] == 1


def test_create_pat_auth_with_login_prefers_team_context_account_id(monkeypatch):
    captured = {}

    class _Team:
        account_id = "acc-target"
        workspace_name = "Target Team"
        label = "Target Team"

    monkeypatch.setattr(
        "autoteam.codex_pat_export.capture_chatgpt_session_from_page",
        lambda _page: {
            "chatgpt_session_token": "session-token",
            "access_token": "access-token",
            "chatgpt_account_id": "acc-default",
        },
    )

    def fake_create_auth(session_token, **kwargs):
        captured["session_token"] = session_token
        captured["kwargs"] = kwargs
        return {"auth_file": "/tmp/codex-repair@example.com-pat.json", "filename": "codex-repair@example.com-pat.json"}

    def fake_login_codex_via_browser(*_args, **kwargs):
        auth = kwargs["pat_export_callback"](object())
        return {"ok": True, "bundle": {"pat_auth": auth}}

    monkeypatch.setattr("autoteam.codex_pat_export.create_save_upload_codex_auth_from_session", fake_create_auth)
    monkeypatch.setattr(manager, "login_codex_via_browser", fake_login_codex_via_browser)

    result = manager._create_pat_auth_with_login("repair@example.com", "secret", team_context=_Team())

    assert result["ok"] is True
    assert captured["session_token"] == "session-token"
    assert captured["kwargs"]["account_id"] == "acc-target"
    assert captured["kwargs"]["email"] == "repair@example.com"


def test_cmd_check_delegates_to_swap_seat_without_local_auth_repair(monkeypatch, caplog):
    calls = []
    monkeypatch.setattr(
        manager,
        "load_accounts",
        lambda: [
            {
                "email": "pending@example.com",
                "status": "auth_pending",
                "auth_file": None,
                "mail_provider": "cloudmail",
                "auth_retry_count": 1,
                "auth_last_error": "auth_code_missing",
                "auth_retry_after": 1_700_000_600,
                "auth_retry_paused": False,
            }
        ],
    )
    monkeypatch.setattr(manager.time, "time", lambda: 1_700_000_000)
    monkeypatch.setattr(manager, "_is_main_account_email", lambda _email: False)
    monkeypatch.setattr(
        manager,
        "_login_codex_with_result",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("should not attempt login during cooldown")),
    )
    monkeypatch.setattr(
        manager,
        "cmd_swap_seats",
        lambda max_chatgpt_active: calls.append(max_chatgpt_active) or {"mode": "swap_seat"},
    )

    with caplog.at_level(logging.INFO):
        result = manager.cmd_check(force_auth_repair=False)

    assert result == {"mode": "swap_seat"}
    assert calls == [2]
    assert "swap_seat-only" in caplog.text


def test_cmd_check_force_auth_repair_still_delegates_to_swap_seat(monkeypatch):
    login_calls = []
    swap_calls = []
    monkeypatch.setattr(
        manager,
        "load_accounts",
        lambda: [
            {
                "email": "pending@example.com",
                "status": "auth_pending",
                "password": "",
                "auth_file": None,
                "mail_provider": "cloudmail",
                "auth_retry_count": 2,
                "auth_last_error": "auth_code_missing",
                "auth_retry_after": 1_700_000_600,
                "auth_retry_paused": False,
            }
        ],
    )
    monkeypatch.setattr(manager.time, "time", lambda: 1_700_000_000)
    monkeypatch.setattr(manager, "_is_main_account_email", lambda _email: False)
    monkeypatch.setattr(manager, "_get_account_mail_client", lambda _acc: _FakeMailClient())
    monkeypatch.setattr(
        manager,
        "_login_codex_with_result",
        lambda email, password, mail_client=None: (
            login_calls.append((email, password, mail_client.provider_name))
            or {
                "ok": False,
                "bundle": None,
                "error_type": "auth_code_missing",
                "error_detail": "未获取到 auth code",
                "retryable": True,
            }
        ),
    )
    monkeypatch.setattr(manager, "_is_email_in_team", lambda _email: True)
    monkeypatch.setattr(manager, "update_account", lambda *args, **kwargs: None)
    monkeypatch.setattr(manager, "_record_auth_repair_failure", lambda *args, **kwargs: {})
    monkeypatch.setattr(
        manager,
        "cmd_swap_seats",
        lambda max_chatgpt_active: swap_calls.append(max_chatgpt_active) or {"mode": "swap_seat"},
    )

    result = manager.cmd_check(force_auth_repair=True)

    assert result == {"mode": "swap_seat"}
    assert login_calls == []
    assert swap_calls == [2]


def test_cmd_check_preserve_low_active_args_do_not_bypass_swap_seat(tmp_path, monkeypatch):
    auth_file = tmp_path / "active.json"
    auth_file.write_text(json.dumps({"access_token": "token-active"}), encoding="utf-8")

    updates = []
    preserved = []
    swap_calls = []

    monkeypatch.setattr(
        manager,
        "load_accounts",
        lambda: [
            {
                "email": "low@example.com",
                "status": "active",
                "auth_file": str(auth_file),
                "last_quota": None,
            }
        ],
    )
    monkeypatch.setattr(manager, "_is_main_account_email", lambda _email: False)
    monkeypatch.setattr(
        manager,
        "_check_and_refresh",
        lambda _acc: (
            "ok",
            {
                "primary_pct": 93,
                "primary_resets_at": 1_700_001_000,
                "weekly_pct": 1,
                "weekly_resets_at": 0,
            },
        ),
    )
    monkeypatch.setattr(manager, "update_account", lambda email, **kwargs: updates.append((email, kwargs)))
    monkeypatch.setattr(
        manager,
        "cmd_swap_seats",
        lambda max_chatgpt_active: swap_calls.append(max_chatgpt_active) or {"mode": "swap_seat"},
    )

    result = manager.cmd_check(
        force_auth_repair=False,
        preserve_low_active=True,
        preserved_low_accounts=preserved,
    )

    assert result == {"mode": "swap_seat"}
    assert preserved == []
    assert updates == []
    assert swap_calls == [2]
