import pytest

from autoteam import api


def test_account_login_and_legacy_tasks_are_disabled():
    with pytest.raises(api.HTTPException) as exc:
        api.post_account_login(api.LoginAccountParams(email="user@example.com"))
    assert exc.value.status_code == 410
    assert "swap_seat-only" in str(exc.value.detail)

    for call in (
        lambda: api.post_fill(api.TaskParams(target=5)),
        lambda: api.post_cleanup(api.CleanupParams(max_seats=5)),
        api.post_reset_quota,
        api.post_sync,
        api.post_sync_from_cpa,
        api.post_sync_accounts,
    ):
        with pytest.raises(api.HTTPException) as exc:
            call()
        assert exc.value.status_code == 410
        assert "swap_seat-only" in str(exc.value.detail)


def test_post_add_starts_consume_pending_invite_task(monkeypatch):
    monkeypatch.setattr(api, "_require_cpa_configs", lambda _label: None)
    monkeypatch.setattr(api, "_require_mail_provider_configs", lambda _label, provider=None, env=None: None)
    monkeypatch.setattr(api, "_auto_check_config", {"target_seats": 3})
    started = []

    def fake_start_task(command, func, params, *args, **kwargs):
        started.append((command, params, args, kwargs))
        return {"task_id": command}

    monkeypatch.setattr(api, "_start_task", fake_start_task)

    result = api.post_add()

    assert result == {"task_id": "consume-pending-invite"}
    assert started[0][0] == "consume-pending-invite"
    assert started[0][1] == {"email": "", "account_id": "", "max_chatgpt_active": 3}
    assert started[0][2] == (None, 3, None)


def test_post_add_allows_manual_active_limit_override(monkeypatch):
    monkeypatch.setattr(api, "_require_cpa_configs", lambda _label: None)
    monkeypatch.setattr(api, "_require_mail_provider_configs", lambda _label, provider=None, env=None: None)
    monkeypatch.setattr(api, "_auto_check_config", {"target_seats": 2})
    started = []

    def fake_start_task(command, func, params, *args, **kwargs):
        started.append((command, params, args, kwargs))
        return {"task_id": command}

    monkeypatch.setattr(api, "_start_task", fake_start_task)

    result = api.post_add(api.PendingInviteConsumeParams(email="pending@example.com", max_chatgpt_active=4))

    assert result == {"task_id": "consume-pending-invite"}
    assert started[0][1] == {"email": "pending@example.com", "account_id": "", "max_chatgpt_active": 4}
    assert started[0][2] == ("pending@example.com", 4, None)


def test_post_invite_add_requires_explicit_force(monkeypatch):
    monkeypatch.setattr(api, "_require_cpa_configs", lambda _label: None)
    monkeypatch.setattr(api, "_require_mail_provider_configs", lambda _label, provider=None, env=None: None)

    with pytest.raises(api.HTTPException) as exc:
        api.post_invite_add()

    assert exc.value.status_code == 400
    assert "force_create_invite" in str(exc.value.detail)


def test_post_invite_add_starts_create_invite_task_when_forced(monkeypatch):
    monkeypatch.setattr(api, "_require_cpa_configs", lambda _label: None)
    monkeypatch.setattr(api, "_require_mail_provider_configs", lambda _label, provider=None, env=None: None)
    monkeypatch.setattr(api, "_auto_check_config", {"target_seats": 3})
    started = []

    def fake_start_task(command, func, params, *args, **kwargs):
        started.append((command, params, args, kwargs))
        return {"task_id": command}

    monkeypatch.setattr(api, "_start_task", fake_start_task)

    result = api.post_invite_add(api.InviteAddParams(force_create_invite=True))

    assert result == {"task_id": "create-invite"}
    assert started[0][0] == "create-invite"
    assert started[0][1] == {"account_id": "", "max_chatgpt_active": 3, "force_create_invite": True}
    assert started[0][2] == (3, None)
    assert started[0][3] == {"force_create_invite": True}


def test_post_invite_add_passes_invite_domains_when_provided(monkeypatch):
    monkeypatch.setattr(api, "_require_cpa_configs", lambda _label: None)
    monkeypatch.setattr(api, "_require_mail_provider_configs", lambda _label, provider=None, env=None: None)
    monkeypatch.setattr(api, "_auto_check_config", {"target_seats": 3})
    started = []

    def fake_start_task(command, func, params, *args, **kwargs):
        started.append((command, params, args, kwargs))
        return {"task_id": command}

    monkeypatch.setattr(api, "_start_task", fake_start_task)

    result = api.post_invite_add(
        api.InviteAddParams(
            force_create_invite=True,
            invite_domains="pool-a.example.com; *.pool-b.example.com",
        )
    )

    assert result == {"task_id": "create-invite"}
    assert started[0][0] == "create-invite"
    assert started[0][1] == {
        "account_id": "",
        "max_chatgpt_active": 3,
        "force_create_invite": True,
        "invite_domains": "pool-a.example.com; *.pool-b.example.com",
    }
    assert started[0][2] == (3, None)
    assert started[0][3] == {
        "force_create_invite": True,
        "invite_domains": "pool-a.example.com; *.pool-b.example.com",
    }


def test_post_auto_detect_replace_starts_task(monkeypatch):
    monkeypatch.setattr(api, "_require_cpa_configs", lambda _label: None)
    monkeypatch.setattr(api, "_require_mail_provider_configs", lambda _label, provider=None, env=None: None)
    monkeypatch.setattr(api, "_auto_check_config", {"target_seats": 4})
    started = []

    def fake_start_task(command, func, params, *args, **kwargs):
        started.append((command, params, args, kwargs))
        return {"task_id": command}

    monkeypatch.setattr(api, "_start_task", fake_start_task)

    result = api.post_auto_detect_replace(api.PendingInviteConsumeParams(email="pending@example.com"))

    assert result == {"task_id": "auto-detect-replace"}
    assert started[0][0] == "auto-detect-replace"
    assert started[0][1] == {
        "max_chatgpt_active": 4,
        "email": "pending@example.com",
        "account_id": "",
        "replace_mode": "pending_invite",
    }
    assert started[0][2] == (4, "pending@example.com", None)
    assert started[0][3] == {"replace_mode": "pending_invite"}


def test_post_auto_detect_replace_rejects_invalid_replace_mode(monkeypatch):
    monkeypatch.setattr(api, "_require_cpa_configs", lambda _label: None)
    monkeypatch.setattr(api, "_require_mail_provider_configs", lambda _label, provider=None, env=None: None)

    with pytest.raises(api.HTTPException) as exc:
        api.post_auto_detect_replace(api.PendingInviteConsumeParams(replace_mode="surprise"))

    assert exc.value.status_code == 400
    assert "pending_invite" in str(exc.value.detail)
    assert "create_invite" in str(exc.value.detail)


def test_pending_invite_tasks_use_target_team_active_limit(monkeypatch):
    class _Team:
        account_id = "acc-one-seat"
        max_chatgpt_active = 1

    monkeypatch.setattr(api, "_require_cpa_configs", lambda _label: None)
    monkeypatch.setattr(api, "_require_mail_provider_configs", lambda _label, provider=None, env=None: None)
    monkeypatch.setattr(api, "_auto_check_config", {"target_seats": 4})
    monkeypatch.setattr(
        "autoteam.team_context.get_team_context",
        lambda account_id, default_max_chatgpt_active=2: _Team() if account_id == "acc-one-seat" else None,
    )
    started = []

    def fake_start_task(command, func, params, *args, **kwargs):
        started.append((command, params, args, kwargs))
        return {"task_id": command}

    monkeypatch.setattr(api, "_start_task", fake_start_task)

    api.post_auto_detect_replace(api.PendingInviteConsumeParams(email="pending@example.com", account_id="acc-one-seat"))
    api.post_add(api.PendingInviteConsumeParams(email="pending@example.com", account_id="acc-one-seat"))
    api.post_invite_add(api.InviteAddParams(account_id="acc-one-seat", force_create_invite=True))

    assert started[0][0] == "auto-detect-replace"
    assert started[0][1] == {
        "max_chatgpt_active": 1,
        "email": "pending@example.com",
        "account_id": "acc-one-seat",
        "replace_mode": "pending_invite",
    }
    assert started[0][2][0] == 1
    assert started[0][2][1] == "pending@example.com"
    assert started[0][2][2].account_id == "acc-one-seat"

    assert started[1][0] == "consume-pending-invite"
    assert started[1][1] == {"email": "pending@example.com", "account_id": "acc-one-seat", "max_chatgpt_active": 1}
    assert started[1][2][0] == "pending@example.com"
    assert started[1][2][1] == 1
    assert started[1][2][2].account_id == "acc-one-seat"

    assert started[2][0] == "create-invite"
    assert started[2][1] == {"account_id": "acc-one-seat", "max_chatgpt_active": 1, "force_create_invite": True}
    assert started[2][2][0] == 1
    assert started[2][2][1].account_id == "acc-one-seat"
    assert started[2][3] == {"force_create_invite": True}


def test_post_swap_seats_can_target_configured_team(monkeypatch):
    class _Team:
        account_id = "acc-team"

    monkeypatch.setattr(api, "_require_cpa_configs", lambda _label: None)
    monkeypatch.setattr("autoteam.team_context.get_team_context", lambda account_id, default_max_chatgpt_active=2: _Team() if account_id == "acc-team" else None)
    started = []

    def fake_start_task(command, func, params, *args, **kwargs):
        started.append((command, params, args, kwargs))
        return {"task_id": command}

    monkeypatch.setattr(api, "_start_task", fake_start_task)

    result = api.post_swap_seats(api.SwapSeatParams(max_chatgpt_active=3, account_id="acc-team"))

    assert result == {"task_id": "swap-seats"}
    assert started[0][0] == "swap-seats"
    assert started[0][1] == {"max_chatgpt_active": 3, "account_id": "acc-team"}
    assert started[0][2] == (3,)
    assert started[0][3]["team_context"].account_id == "acc-team"


def test_post_swap_seats_without_explicit_limit_uses_target_team_limit(monkeypatch):
    class _Team:
        account_id = "acc-one-seat"
        max_chatgpt_active = 1

    monkeypatch.setattr(api, "_require_cpa_configs", lambda _label: None)
    monkeypatch.setattr(api, "_auto_check_config", {"target_seats": 4})
    monkeypatch.setattr(
        "autoteam.team_context.get_team_context",
        lambda account_id, default_max_chatgpt_active=2: _Team() if account_id == "acc-one-seat" else None,
    )
    started = []

    def fake_start_task(command, func, params, *args, **kwargs):
        started.append((command, params, args, kwargs))
        return {"task_id": command}

    monkeypatch.setattr(api, "_start_task", fake_start_task)

    result = api.post_swap_seats(api.SwapSeatParams(account_id="acc-one-seat"))

    assert result == {"task_id": "swap-seats"}
    assert started[0][1] == {"max_chatgpt_active": 1, "account_id": "acc-one-seat"}
    assert started[0][2] == (1,)
    assert started[0][3]["team_context"].account_id == "acc-one-seat"


def test_post_manage_teams_starts_multi_team_task(monkeypatch):
    monkeypatch.setattr(api, "_require_cpa_configs", lambda _label: None)
    monkeypatch.setattr(api, "_require_mail_provider_configs", lambda _label, provider=None, env=None: None)
    monkeypatch.setattr(api, "_auto_check_config", {"target_seats": 6, "replace_with_pending_invite": True})
    started = []

    def fake_start_task(command, func, params, *args, **kwargs):
        started.append((command, params, args))
        return {"task_id": command}

    monkeypatch.setattr(api, "_start_task", fake_start_task)

    result = api.post_manage_teams(api.ManageTeamsParams())

    assert result == {"task_id": "manage-teams"}
    assert started[0][0] == "manage-teams"
    assert started[0][1] == {
        "max_chatgpt_active": 5,
        "replace_with_pending_invite": True,
        "replace_mode": "pending_invite",
    }
    assert started[0][2] == (5, True)


def test_get_swap_runtime_status_is_read_only_snapshot(monkeypatch):
    class _Team:
        account_id = "acc-a"
        cooldown_scope = "acc-a"

        def public_dict(self):
            return {"account_id": self.account_id, "label": "Team A"}

    monkeypatch.setattr(api, "_auto_check_config", {"target_seats": 2})
    monkeypatch.setattr("autoteam.team_context.get_team_contexts", lambda include_disabled=True, default_max_chatgpt_active=2: [_Team()])
    monkeypatch.setattr(
        "autoteam.swap_seat.get_swap_cooldown_status",
        lambda scope=None: {"scope": scope, "allowed": True, "used_today": 0},
    )
    monkeypatch.setattr(
        "autoteam.swap_seat.quota_cache_runtime_status",
        lambda: {"summary": {"total": 1}, "entries": [{"email": "a@example.com"}]},
    )

    result = api.get_swap_runtime_status()

    assert result["teams"][0]["team"]["account_id"] == "acc-a"
    assert result["teams"][0]["cooldown"]["scope"] == "acc-a"
    assert result["quota_cache"]["summary"]["total"] == 1


def test_get_cpa_files_returns_only_managed_codex_auths(monkeypatch):
    monkeypatch.setattr(api, "_require_cpa_configs", lambda _label: None)
    monkeypatch.setattr(
        "autoteam.cpa_sync.list_cpa_files",
        lambda: [
            {"name": "managed.json", "email": "managed@example.com", "provider": "codex", "status": "active"},
            {"name": "external.json", "email": "external@example.com", "provider": "codex", "status": "active"},
            {"name": "other.json", "email": "other@example.com", "provider": "openai", "status": "active"},
        ],
    )
    monkeypatch.setattr("autoteam.cpa_sync.get_managed_cpa_auth_names", lambda: {"managed.json"})

    result = api.get_cpa_files()

    assert [item["name"] for item in result["files"]] == ["managed.json"]
    assert result["files"][0]["autoteam_managed"] is True
    assert result["summary"] == {
        "total_cpa_files": 3,
        "codex_total": 2,
        "managed": 1,
        "protected_unmanaged": 1,
    }


def test_patch_cpa_auth_status_uses_cpa_management_api(monkeypatch):
    monkeypatch.setattr(api, "_require_cpa_configs", lambda _label: None)
    calls = []
    monkeypatch.setattr(
        "autoteam.cpa_sync.set_cpa_auth_disabled",
        lambda name, disabled: calls.append((name, disabled)) or {"status": "ok"},
    )
    monkeypatch.setattr("autoteam.cpa_sync.is_managed_cpa_auth", lambda name: name == "a.json")

    result = api.patch_cpa_auth_status(api.CpaAuthStatusParams(name="a.json", disabled=True))

    assert calls == [("a.json", True)]
    assert result["disabled"] is True


def test_patch_cpa_auth_status_rejects_manual_enable(monkeypatch):
    monkeypatch.setattr(api, "_require_cpa_configs", lambda _label: None)
    calls = []
    monkeypatch.setattr(
        "autoteam.cpa_sync.set_cpa_auth_disabled",
        lambda name, disabled: calls.append((name, disabled)) or {"status": "ok"},
    )

    with pytest.raises(api.HTTPException) as exc:
        api.patch_cpa_auth_status(api.CpaAuthStatusParams(name="a.json", disabled=False))

    assert exc.value.status_code == 410
    assert "禁止手动 enable" in str(exc.value.detail)
    assert calls == []


def test_patch_cpa_auth_status_rejects_unmanaged_auth(monkeypatch):
    monkeypatch.setattr(api, "_require_cpa_configs", lambda _label: None)
    calls = []
    monkeypatch.setattr(
        "autoteam.cpa_sync.set_cpa_auth_disabled",
        lambda name, disabled: calls.append((name, disabled)) or {"status": "ok"},
    )
    monkeypatch.setattr("autoteam.cpa_sync.is_managed_cpa_auth", lambda _name: False)

    with pytest.raises(api.HTTPException) as exc:
        api.patch_cpa_auth_status(api.CpaAuthStatusParams(name="external.json", disabled=True))

    assert exc.value.status_code == 403
    assert "未由 AutoTeam 创建" in str(exc.value.detail)
    assert calls == []


def test_post_cpa_auth_forget_deletes_managed_auth_and_quota(monkeypatch):
    monkeypatch.setattr(api, "_require_cpa_configs", lambda _label: None)
    calls = {}
    monkeypatch.setattr("autoteam.cpa_sync.is_managed_cpa_auth", lambda name: name == "managed.json")
    monkeypatch.setattr(
        "autoteam.cpa_sync.list_cpa_files",
        lambda: [{"name": "managed.json", "email": "managed@example.com"}],
    )
    monkeypatch.setattr(
        "autoteam.cpa_sync.delete_from_cpa",
        lambda name: calls.setdefault("deleted", []).append(name) or True,
    )
    monkeypatch.setattr(
        "autoteam.cpa_sync.unregister_managed_cpa_auth",
        lambda name: calls.setdefault("unregistered", []).append(name) or True,
    )
    monkeypatch.setattr(
        "autoteam.swap_seat.forget_quota_cache_entries",
        lambda **kwargs: calls.setdefault("quota", []).append(kwargs) or {"count": 1, "removed": ["acc:managed.json"]},
    )

    result = api.post_cpa_auth_forget(api.CpaAuthForgetParams(name="managed.json"))

    assert result["deleted_from_cpa"] is True
    assert result["unregistered"] is True
    assert result["quota_cache"]["count"] == 1
    assert calls == {
        "deleted": ["managed.json"],
        "unregistered": ["managed.json"],
        "quota": [{"auth_id": "managed.json", "email": "managed@example.com"}],
    }


def test_post_cpa_auth_forget_rejects_unmanaged_auth(monkeypatch):
    monkeypatch.setattr(api, "_require_cpa_configs", lambda _label: None)
    monkeypatch.setattr("autoteam.cpa_sync.is_managed_cpa_auth", lambda _name: False)
    monkeypatch.setattr(
        "autoteam.cpa_sync.delete_from_cpa",
        lambda _name: (_ for _ in ()).throw(AssertionError("unmanaged auth must not be deleted")),
    )

    with pytest.raises(api.HTTPException) as exc:
        api.post_cpa_auth_forget(api.CpaAuthForgetParams(name="external.json"))

    assert exc.value.status_code == 403
    assert "未由 AutoTeam 创建" in str(exc.value.detail)
