import time

from autoteam import cpa_sync
from autoteam.swap_seat import (
    SwapSeatCooldownError,
    build_swap_plan,
    cmd_swap_seats,
    force_existing_members_to_codex,
    get_autoteam_managed_member_emails,
    get_swap_cooldown_status,
    quota_available,
    record_quota_result,
    reserve_swap_cooldown_slot,
)
from autoteam.team_context import TeamContext


def _patch_managed_member_emails(monkeypatch, *emails):
    monkeypatch.setattr("autoteam.swap_seat.get_autoteam_managed_member_emails", lambda: {email.lower() for email in emails})


def test_get_autoteam_managed_member_emails_does_not_trust_password_mail_provider_only():
    emails = get_autoteam_managed_member_emails(
        [
            {
                "email": "manual@example.com",
                "password": "secret",
                "mail_provider": "cloudflare_temp_email",
            },
            {
                "email": "false-flag@example.com",
                "managed_by_autoteam": "false",
                "mail_account_id": "addr-should-not-matter",
            },
            {
                "email": "created@example.com",
                "password": "secret",
                "mail_provider": "cloudflare_temp_email",
                "mail_account_id": "addr-1",
            },
            {
                "email": "explicit@example.com",
                "managed_by_autoteam": True,
            },
        ]
    )

    assert emails == {"created@example.com", "explicit@example.com"}


def test_build_swap_plan_selects_top_two_and_disables_rest():
    team_members = [
        {"email": "a@example.com", "id": "u-a", "seat_type": "usage_based"},
        {"email": "b@example.com", "id": "u-b", "seat_type": "default"},
        {"email": "c@example.com", "id": "u-c", "seat_type": "usage_based"},
    ]
    cpa_auths = [
        {"name": "a.json", "auth_index": "idx-a", "provider": "codex", "email": "a@example.com", "status": "disabled", "disabled": True},
        {"name": "b.json", "auth_index": "idx-b", "provider": "codex", "email": "b@example.com", "status": "active", "disabled": False},
        {"name": "c.json", "auth_index": "idx-c", "provider": "codex", "email": "c@example.com", "status": "disabled", "disabled": True},
        {"name": "outside.json", "auth_index": "idx-o", "provider": "codex", "email": "outside@example.com", "status": "active", "disabled": False},
    ]
    quota_results = {
        "a.json": ("ok", {"primary_pct": 10, "weekly_pct": 20, "primary_resets_at": 1, "weekly_resets_at": 2}),
        "b.json": ("exhausted", {"quota_info": {"primary_pct": 100, "weekly_pct": 10}}),
        "c.json": ("ok", {"primary_pct": 5, "weekly_pct": 5, "primary_resets_at": 1, "weekly_resets_at": 2}),
    }

    plan = build_swap_plan(team_members, cpa_auths, quota_results, max_chatgpt_active=2)

    assert plan["selected_emails"] == ["a@example.com", "c@example.com"]
    seat_by_email = {item["email"]: item for item in plan["seat_actions"]}
    assert seat_by_email["a@example.com"]["desired_seat"] == "chatgpt"
    assert seat_by_email["b@example.com"]["desired_seat"] == "codex"
    assert seat_by_email["c@example.com"]["desired_seat"] == "chatgpt"

    oauth_by_name = {item["name"]: item for item in plan["oauth_actions"]}
    assert oauth_by_name["a.json"]["desired_disabled"] is False
    assert oauth_by_name["b.json"]["desired_disabled"] is True
    assert oauth_by_name["c.json"]["desired_disabled"] is False
    assert oauth_by_name["outside.json"]["desired_disabled"] is True


def test_build_swap_plan_excludes_unmanaged_cpa_auths_from_oauth_actions():
    team_members = [
        {"email": "managed@example.com", "id": "u-managed", "seat_type": "usage_based"},
        {"email": "external@example.com", "id": "u-external", "seat_type": "usage_based"},
    ]
    cpa_auths = [
        {
            "name": "managed.json",
            "auth_index": "idx-managed",
            "provider": "codex",
            "email": "managed@example.com",
            "status": "disabled",
            "disabled": True,
        },
        {
            "name": "external.json",
            "auth_index": "idx-external",
            "provider": "codex",
            "email": "external@example.com",
            "status": "active",
            "disabled": False,
        },
    ]
    quota_results = {
        "managed.json": ("ok", {"primary_pct": 10, "weekly_pct": 10}),
    }

    plan = build_swap_plan(
        team_members,
        cpa_auths,
        quota_results,
        max_chatgpt_active=1,
        managed_auth_names={"managed.json"},
    )

    assert [item["name"] for item in plan["oauth_actions"]] == ["managed.json"]
    assert plan["summary"]["managed_cpa_codex_oauth"] == 1
    assert plan["summary"]["unmanaged_cpa_codex_oauth"] == 1
    assert plan["summary"]["unmanaged_active_oauth"] == 1


def test_build_swap_plan_protects_unmanaged_team_members_from_seat_actions():
    team_members = [
        {"email": "managed@example.com", "id": "u-managed", "seat_type": "usage_based"},
        {"email": "external@example.com", "id": "u-external", "seat_type": "default"},
    ]
    cpa_auths = [
        {
            "name": "managed.json",
            "auth_index": "idx-managed",
            "provider": "codex",
            "email": "managed@example.com",
            "status": "disabled",
            "disabled": True,
        }
    ]
    quota_results = {"managed.json": ("ok", {"primary_pct": 10, "weekly_pct": 10})}

    plan = build_swap_plan(
        team_members,
        cpa_auths,
        quota_results,
        max_chatgpt_active=2,
        managed_auth_names={"managed.json"},
        managed_emails={"managed@example.com"},
    )

    assert [item["email"] for item in plan["seat_actions"]] == ["managed@example.com"]
    assert plan["summary"]["protected_team_members"] == 1
    assert plan["summary"]["protected_chatgpt_seats"] == 1
    assert plan["summary"]["managed_active_capacity"] == 1


def test_build_swap_plan_does_not_touch_unmanaged_member_without_managed_email_scope():
    team_members = [
        {"email": "managed@example.com", "id": "u-managed", "seat_type": "usage_based"},
        {"email": "external@example.com", "id": "u-external", "seat_type": "default"},
    ]
    cpa_auths = [
        {
            "name": "managed.json",
            "auth_index": "idx-managed",
            "provider": "codex",
            "email": "managed@example.com",
            "status": "disabled",
            "disabled": True,
        }
    ]
    quota_results = {"managed.json": ("ok", {"primary_pct": 10, "weekly_pct": 10})}

    plan = build_swap_plan(
        team_members,
        cpa_auths,
        quota_results,
        max_chatgpt_active=1,
        managed_auth_names={"managed.json"},
    )

    assert [item["email"] for item in plan["seat_actions"]] == ["managed@example.com"]
    assert plan["summary"]["protected_team_members"] == 1


def test_build_swap_plan_ignores_unmanaged_active_oauth_outside_team_capacity():
    team_members = [
        {"email": "managed@example.com", "id": "u-managed", "seat_type": "usage_based"},
    ]
    cpa_auths = [
        {
            "name": "managed.json",
            "auth_index": "idx-managed",
            "provider": "codex",
            "email": "managed@example.com",
            "status": "disabled",
            "disabled": True,
        },
        {
            "name": "outside.json",
            "auth_index": "idx-outside",
            "provider": "codex",
            "email": "outside@example.com",
            "status": "active",
            "disabled": False,
        },
    ]
    quota_results = {"managed.json": ("ok", {"primary_pct": 10, "weekly_pct": 10})}

    plan = build_swap_plan(
        team_members,
        cpa_auths,
        quota_results,
        max_chatgpt_active=1,
        managed_auth_names={"managed.json"},
        managed_emails={"managed@example.com"},
    )

    assert plan["selected_emails"] == ["managed@example.com"]
    assert plan["summary"]["unmanaged_active_oauth"] == 1
    assert plan["summary"]["protected_active_oauth"] == 0
    assert plan["summary"]["managed_active_capacity"] == 1


def test_cmd_swap_seats_does_not_check_or_disable_unmanaged_cpa_auth(monkeypatch, tmp_path):
    class FakeChatGPT:
        browser = True

        def __init__(self):
            self.seat_calls = []

        def update_member_seat_type(self, user_id, seat_type):
            self.seat_calls.append((user_id, seat_type))
            return {"status": 200, "body": "{}"}

    auth = {
        "name": "external.json",
        "auth_index": "idx-external",
        "provider": "codex",
        "email": "external@example.com",
        "status": "active",
        "disabled": False,
    }
    checked = []
    cpa_updates = []

    _patch_managed_member_emails(monkeypatch)
    monkeypatch.setattr("autoteam.swap_seat.SWAP_COOLDOWN_FILE", tmp_path / "swap_cooldown.json")
    monkeypatch.setattr("autoteam.swap_seat.SWAP_QUOTA_STATE_FILE", tmp_path / "swap_quota_state.json")
    monkeypatch.setattr("autoteam.swap_seat.get_admin_email", lambda: "")
    monkeypatch.setattr(
        "autoteam.swap_seat.fetch_team_members",
        lambda _chatgpt: [{"email": "external@example.com", "id": "u-external", "seat_type": "usage_based"}],
    )
    monkeypatch.setattr("autoteam.swap_seat.list_cpa_files", lambda: [auth])
    monkeypatch.setattr("autoteam.swap_seat.get_managed_cpa_auth_names", lambda: set())
    monkeypatch.setattr(
        "autoteam.swap_seat.check_cpa_codex_quota",
        lambda auth, account_id=None: checked.append(auth["name"]) or ("ok", {"primary_pct": 0, "weekly_pct": 0}),
    )
    monkeypatch.setattr(
        "autoteam.swap_seat.set_cpa_auth_disabled",
        lambda name, disabled: cpa_updates.append((name, disabled)) or {"status": "ok"},
    )

    result = cmd_swap_seats(max_chatgpt_active=1, chatgpt_api=FakeChatGPT())

    assert result["skipped"] is True
    assert result["reason"] == "no_quota_available"
    assert checked == []
    assert cpa_updates == []
    assert result["summary"]["unmanaged_cpa_codex_oauth"] == 1


def test_cmd_swap_seats_does_not_patch_unmanaged_team_member(monkeypatch, tmp_path):
    class FakeChatGPT:
        browser = True

        def __init__(self):
            self.seat_calls = []

        def update_member_seat_type(self, user_id, seat_type):
            self.seat_calls.append((user_id, seat_type))
            return {"status": 200, "body": "{}"}

    fake_chatgpt = FakeChatGPT()
    team_members = [
        {"email": "external@example.com", "id": "u-external", "seat_type": "default"},
        {"email": "managed@example.com", "id": "u-managed", "seat_type": "usage_based"},
    ]
    managed_auth = {
        "name": "managed.json",
        "auth_index": "idx-managed",
        "provider": "codex",
        "email": "managed@example.com",
        "status": "disabled",
        "disabled": True,
    }
    cpa_updates = []

    _patch_managed_member_emails(monkeypatch, "managed@example.com")
    monkeypatch.setattr("autoteam.swap_seat.SWAP_COOLDOWN_FILE", tmp_path / "swap_cooldown.json")
    monkeypatch.setattr("autoteam.swap_seat.SWAP_QUOTA_STATE_FILE", tmp_path / "swap_quota_state.json")
    monkeypatch.setattr("autoteam.swap_seat.get_admin_email", lambda: "")
    monkeypatch.setattr("autoteam.swap_seat.fetch_team_members", lambda _chatgpt: team_members)
    monkeypatch.setattr("autoteam.swap_seat.list_cpa_files", lambda: [managed_auth])
    monkeypatch.setattr("autoteam.swap_seat.get_managed_cpa_auth_names", lambda: {"managed.json"})
    monkeypatch.setattr(
        "autoteam.swap_seat.check_cpa_codex_quota",
        lambda auth, account_id=None: ("ok", {"primary_pct": 5, "weekly_pct": 5}),
    )
    monkeypatch.setattr(
        "autoteam.swap_seat.set_cpa_auth_disabled",
        lambda name, disabled: cpa_updates.append((name, disabled)) or {"status": "ok"},
    )

    result = cmd_swap_seats(max_chatgpt_active=2, chatgpt_api=fake_chatgpt)

    assert fake_chatgpt.seat_calls == [("u-managed", "default")]
    assert cpa_updates == [("managed.json", False)]
    assert result["summary"]["protected_team_members"] == 1
    assert result["summary"]["protected_chatgpt_seats"] == 1
    assert result["summary"]["managed_active_capacity"] == 1


def test_cmd_swap_seats_uses_managed_cpa_auth_as_member_fallback(monkeypatch, tmp_path):
    class FakeChatGPT:
        browser = True

        def __init__(self):
            self.seat_calls = []

        def update_member_seat_type(self, user_id, seat_type):
            self.seat_calls.append((user_id, seat_type))
            return {"status": 200, "body": "{}"}

    fake_chatgpt = FakeChatGPT()
    managed_auth = {
        "name": "managed.json",
        "auth_index": "idx-managed",
        "provider": "codex",
        "email": "managed@example.com",
        "status": "disabled",
        "disabled": True,
    }
    checked = []
    cpa_updates = []

    _patch_managed_member_emails(monkeypatch)
    monkeypatch.setattr("autoteam.swap_seat.SWAP_COOLDOWN_FILE", tmp_path / "swap_cooldown.json")
    monkeypatch.setattr("autoteam.swap_seat.SWAP_QUOTA_STATE_FILE", tmp_path / "swap_quota_state.json")
    monkeypatch.setattr("autoteam.swap_seat.get_admin_email", lambda: "")
    monkeypatch.setattr("autoteam.swap_seat.get_chatgpt_account_id", lambda: "")
    monkeypatch.setattr(
        "autoteam.swap_seat.fetch_team_members",
        lambda _chatgpt: [{"email": "managed@example.com", "id": "u-managed", "seat_type": "usage_based"}],
    )
    monkeypatch.setattr("autoteam.swap_seat.list_cpa_files", lambda: [managed_auth])
    monkeypatch.setattr("autoteam.swap_seat.get_managed_cpa_auth_names", lambda: {"managed.json"})
    monkeypatch.setattr(
        "autoteam.swap_seat.check_cpa_codex_quota",
        lambda auth, account_id=None: checked.append(auth["name"]) or ("ok", {"primary_pct": 5, "weekly_pct": 5}),
    )
    monkeypatch.setattr(
        "autoteam.swap_seat.set_cpa_auth_disabled",
        lambda name, disabled: cpa_updates.append((name, disabled)) or {"status": "ok"},
    )

    result = cmd_swap_seats(max_chatgpt_active=1, chatgpt_api=fake_chatgpt)

    assert checked == ["managed.json"]
    assert fake_chatgpt.seat_calls == [("u-managed", "default")]
    assert cpa_updates == [("managed.json", False)]
    assert result["selected_emails"] == ["managed@example.com"]
    assert result["summary"]["managed_team_members"] == 1
    assert result["summary"]["protected_team_members"] == 0


def test_cmd_swap_seats_does_not_disable_other_team_managed_auth(monkeypatch, tmp_path):
    class FakeChatGPT:
        browser = True

        def __init__(self):
            self.seat_calls = []

        def update_member_seat_type(self, user_id, seat_type):
            self.seat_calls.append((user_id, seat_type))
            return {"status": 200, "body": "{}"}

    fake_chatgpt = FakeChatGPT()
    team_members = [{"email": "a@example.com", "id": "u-a", "seat_type": "usage_based"}]
    cpa_auths = [
        {
            "name": "a.json",
            "auth_index": "idx-a",
            "provider": "codex",
            "email": "a@example.com",
            "status": "disabled",
            "disabled": True,
        },
        {
            "name": "b.json",
            "auth_index": "idx-b",
            "provider": "codex",
            "email": "b@example.com",
            "status": "active",
            "disabled": False,
        },
    ]
    checked = []
    cpa_updates = []

    _patch_managed_member_emails(monkeypatch, "a@example.com", "b@example.com")
    monkeypatch.setattr("autoteam.swap_seat.SWAP_COOLDOWN_FILE", tmp_path / "swap_cooldown.json")
    monkeypatch.setattr("autoteam.swap_seat.SWAP_QUOTA_STATE_FILE", tmp_path / "swap_quota_state.json")
    monkeypatch.setattr("autoteam.swap_seat.get_admin_email", lambda: "")
    monkeypatch.setattr("autoteam.swap_seat.fetch_team_members", lambda _chatgpt: team_members)
    monkeypatch.setattr("autoteam.swap_seat.list_cpa_files", lambda: cpa_auths)
    monkeypatch.setattr("autoteam.swap_seat.get_managed_cpa_auth_names", lambda: {"a.json", "b.json"})
    monkeypatch.setattr(
        "autoteam.swap_seat.check_cpa_codex_quota",
        lambda auth, account_id=None: checked.append(auth["name"]) or ("ok", {"primary_pct": 5, "weekly_pct": 5}),
    )
    monkeypatch.setattr(
        "autoteam.swap_seat.set_cpa_auth_disabled",
        lambda name, disabled: cpa_updates.append((name, disabled)) or {"status": "ok"},
    )

    result = cmd_swap_seats(max_chatgpt_active=1, chatgpt_api=fake_chatgpt)

    assert checked == ["a.json"]
    assert fake_chatgpt.seat_calls == [("u-a", "default")]
    assert cpa_updates == [("a.json", False)]
    assert result["summary"]["unmanaged_cpa_codex_oauth"] == 1


def test_force_existing_members_to_codex_only_touches_managed_emails(monkeypatch):
    class FakeChatGPT:
        def __init__(self):
            self.seat_calls = []

        def update_member_seat_type(self, user_id, seat_type):
            self.seat_calls.append((user_id, seat_type))
            return {"status": 200, "body": "{}"}

    fake_chatgpt = FakeChatGPT()

    def fake_fetch(_chatgpt, account_id=None):
        return [
            {"email": "external@example.com", "id": "u-external", "seat_type": "default"},
            {"email": "managed@example.com", "id": "u-managed", "seat_type": "default"},
        ]

    monkeypatch.setattr("autoteam.swap_seat._fetch_team_members_for_account", fake_fetch)
    result = force_existing_members_to_codex(fake_chatgpt, managed_emails={"managed@example.com"})

    assert fake_chatgpt.seat_calls == [("u-managed", "usage_based")]
    by_email = {item["email"]: item for item in result["seat_results"]}
    assert by_email["external@example.com"]["result"] == "protected_unmanaged"
    assert by_email["managed@example.com"]["result"] == "updated"


def test_cmd_swap_seats_marks_managed_auth_error_for_pat_repair(monkeypatch, tmp_path):
    class FakeChatGPT:
        browser = True

        def update_member_seat_type(self, user_id, seat_type):
            raise AssertionError("seat should not be updated")

    auth = {
        "name": "broken.json",
        "auth_index": "idx-broken",
        "provider": "codex",
        "email": "broken@example.com",
        "status": "disabled",
        "disabled": True,
    }
    updates = []

    _patch_managed_member_emails(monkeypatch, "broken@example.com")
    monkeypatch.setattr("autoteam.swap_seat.SWAP_COOLDOWN_FILE", tmp_path / "swap_cooldown.json")
    monkeypatch.setattr("autoteam.swap_seat.SWAP_QUOTA_STATE_FILE", tmp_path / "swap_quota_state.json")
    monkeypatch.setattr("autoteam.swap_seat.get_admin_email", lambda: "")
    monkeypatch.setattr(
        "autoteam.swap_seat.fetch_team_members",
        lambda _chatgpt: [{"email": "broken@example.com", "id": "u-broken", "seat_type": "usage_based"}],
    )
    monkeypatch.setattr("autoteam.swap_seat.list_cpa_files", lambda: [auth])
    monkeypatch.setattr("autoteam.swap_seat.get_managed_cpa_auth_names", lambda: {"broken.json"})
    monkeypatch.setattr(
        "autoteam.swap_seat.check_cpa_codex_quota",
        lambda auth, account_id=None: ("auth_error", {"error": "expired token"}),
    )
    monkeypatch.setattr("autoteam.accounts.update_account", lambda email, **kwargs: updates.append((email, kwargs)))

    result = cmd_swap_seats(max_chatgpt_active=1, chatgpt_api=FakeChatGPT())

    assert result["skipped"] is True
    assert updates[0][0] == "broken@example.com"
    assert updates[0][1]["status"] == "auth_pending"
    assert updates[0][1]["auth_last_error"] == "cpa_auth_error"


def test_parse_codex_quota_usage_considers_weekly_exhaustion():
    status, info = cpa_sync.parse_codex_quota_usage(
        {
            "rate_limit": {
                "primary_window": {"used_percent": 20, "reset_at": 100},
                "secondary_window": {"used_percent": 100, "reset_at": 200},
            }
        }
    )

    assert status == "exhausted"
    assert info["window"] == "weekly"
    assert info["resets_at"] == 200


def test_parse_codex_quota_usage_handles_har_null_secondary_window():
    status, info = cpa_sync.parse_codex_quota_usage(
        {
            "rate_limit": {
                "allowed": False,
                "limit_reached": True,
                "primary_window": {"used_percent": 100, "reset_at": 1784851280},
                "secondary_window": None,
            },
            "additional_rate_limits": None,
        }
    )

    assert status == "exhausted"
    assert info["window"] == "primary"
    assert info["resets_at"] == 1784851280
    assert info["quota_info"]["weekly_pct"] == 0


def test_parse_codex_quota_usage_considers_monthly_exhaustion():
    status, info = cpa_sync.parse_codex_quota_usage(
        {
            "rate_limit": {
                "primary_window": {"used_percent": 20, "reset_at": 100},
                "monthly_window": {"used_percent": 100, "reset_at": 300},
            }
        }
    )

    assert status == "exhausted"
    assert info["window"] == "monthly"
    assert info["resets_at"] == 300


def test_cmd_swap_seats_updates_seat_and_cpa_status(monkeypatch, tmp_path):
    class FakeChatGPT:
        browser = True

        def __init__(self):
            self.seat_calls = []

        def update_member_seat_type(self, user_id, seat_type):
            self.seat_calls.append((user_id, seat_type))
            return {"status": 200, "body": "{}"}

    fake_chatgpt = FakeChatGPT()
    team_members = [
        {"email": "a@example.com", "id": "u-a", "seat_type": "usage_based"},
        {"email": "b@example.com", "id": "u-b", "seat_type": "default"},
        {"email": "c@example.com", "id": "u-c", "seat_type": "usage_based"},
    ]
    cpa_auths = [
        {"name": "a.json", "auth_index": "idx-a", "provider": "codex", "email": "a@example.com", "status": "disabled", "disabled": True},
        {"name": "b.json", "auth_index": "idx-b", "provider": "codex", "email": "b@example.com", "status": "active", "disabled": False},
        {"name": "c.json", "auth_index": "idx-c", "provider": "codex", "email": "c@example.com", "status": "disabled", "disabled": True},
    ]
    quota_by_name = {
        "a.json": ("ok", {"primary_pct": 30, "weekly_pct": 30}),
        "b.json": ("exhausted", {"quota_info": {"primary_pct": 100, "weekly_pct": 0}}),
        "c.json": ("ok", {"primary_pct": 10, "weekly_pct": 20}),
    }

    _patch_managed_member_emails(monkeypatch, "a@example.com", "b@example.com", "c@example.com")
    monkeypatch.setattr("autoteam.swap_seat.get_chatgpt_account_id", lambda: "acc-1")
    monkeypatch.setattr("autoteam.swap_seat.SWAP_COOLDOWN_FILE", tmp_path / "swap_cooldown.json")
    monkeypatch.setattr("autoteam.swap_seat.SWAP_QUOTA_STATE_FILE", tmp_path / "swap_quota_state.json")
    monkeypatch.setattr("autoteam.swap_seat.fetch_team_members", lambda _chatgpt: team_members)
    monkeypatch.setattr("autoteam.swap_seat.list_cpa_files", lambda: cpa_auths)
    monkeypatch.setattr("autoteam.swap_seat.get_managed_cpa_auth_names", lambda: {"a.json", "b.json", "c.json"})
    monkeypatch.setattr(
        "autoteam.swap_seat.check_cpa_codex_quota",
        lambda auth, account_id=None: quota_by_name[auth["name"]],
    )
    cpa_updates = []
    monkeypatch.setattr(
        "autoteam.swap_seat.set_cpa_auth_disabled",
        lambda name, disabled: cpa_updates.append((name, disabled)) or {"status": "ok"},
    )

    result = cmd_swap_seats(chatgpt_api=fake_chatgpt)

    assert result["selected_emails"] == ["a@example.com", "c@example.com"]
    assert fake_chatgpt.seat_calls == [("u-b", "usage_based"), ("u-a", "default"), ("u-c", "default")]
    assert cpa_updates == [("b.json", True), ("a.json", False), ("c.json", False)]


def test_cmd_swap_seats_no_available_accounts_cleans_up_codex_and_disabled(monkeypatch, tmp_path):
    class FakeChatGPT:
        browser = True

        def __init__(self):
            self.seat_calls = []

        def update_member_seat_type(self, user_id, seat_type):
            self.seat_calls.append((user_id, seat_type))
            return {"status": 200, "body": "{}"}

    fake_chatgpt = FakeChatGPT()
    cpa_updates = []
    team_members = [
        {"email": "a@example.com", "id": "u-a", "seat_type": "default"},
        {"email": "b@example.com", "id": "u-b", "seat_type": "usage_based"},
    ]
    cpa_auths = [
        {"name": "a.json", "auth_index": "idx-a", "provider": "codex", "email": "a@example.com", "status": "active", "disabled": False},
        {"name": "b.json", "auth_index": "idx-b", "provider": "codex", "email": "b@example.com", "status": "disabled", "disabled": True},
    ]

    _patch_managed_member_emails(monkeypatch, "a@example.com", "b@example.com")
    monkeypatch.setattr("autoteam.swap_seat.SWAP_COOLDOWN_FILE", tmp_path / "swap_cooldown.json")
    monkeypatch.setattr("autoteam.swap_seat.SWAP_QUOTA_STATE_FILE", tmp_path / "swap_quota_state.json")
    monkeypatch.setattr("autoteam.swap_seat.fetch_team_members", lambda _chatgpt: team_members)
    monkeypatch.setattr("autoteam.swap_seat.list_cpa_files", lambda: cpa_auths)
    monkeypatch.setattr("autoteam.swap_seat.get_managed_cpa_auth_names", lambda: {"a.json", "b.json"})
    monkeypatch.setattr(
        "autoteam.swap_seat.check_cpa_codex_quota",
        lambda auth, account_id=None: ("exhausted", {"window": "primary", "resets_at": 2_000_000_000, "quota_info": {"primary_pct": 100, "weekly_pct": 10, "primary_resets_at": 2_000_000_000}}),
    )
    monkeypatch.setattr(
        "autoteam.swap_seat.set_cpa_auth_disabled",
        lambda name, disabled: cpa_updates.append((name, disabled)) or {"status": "ok"},
    )

    result = cmd_swap_seats(chatgpt_api=fake_chatgpt)

    assert result["reason"] == "no_quota_available_cleanup"
    assert fake_chatgpt.seat_calls == [("u-a", "usage_based")]
    assert cpa_updates == [("a.json", True)]
    assert (tmp_path / "swap_cooldown.json").exists()


def test_cmd_swap_seats_uses_cached_exhausted_until_reset_and_skips_cpa(monkeypatch, tmp_path):
    class FakeChatGPT:
        browser = True

        def update_member_seat_type(self, user_id, seat_type):
            raise AssertionError("seat should not be updated")

    quota_state_file = tmp_path / "swap_quota_state.json"
    monkeypatch.setattr("autoteam.swap_seat.SWAP_COOLDOWN_FILE", tmp_path / "swap_cooldown.json")
    monkeypatch.setattr("autoteam.swap_seat.SWAP_QUOTA_STATE_FILE", quota_state_file)
    monkeypatch.setattr("autoteam.swap_seat.get_chatgpt_account_id", lambda: "")

    auth = {"name": "a.json", "auth_index": "idx-a", "provider": "codex", "email": "a@example.com", "status": "active", "disabled": False}
    record_quota_result(
        auth,
        "exhausted",
        {
            "window": "weekly",
            "resets_at": time.time() + 3600,
            "quota_info": {"primary_pct": 10, "weekly_pct": 100, "weekly_resets_at": time.time() + 3600},
        },
    )

    monkeypatch.setattr("autoteam.swap_seat.fetch_team_members", lambda _chatgpt: [{"email": "a@example.com", "id": "u-a", "seat_type": "usage_based"}])
    auth["status"] = "disabled"
    auth["disabled"] = True
    _patch_managed_member_emails(monkeypatch, "a@example.com")
    monkeypatch.setattr("autoteam.swap_seat.list_cpa_files", lambda: [auth])
    monkeypatch.setattr("autoteam.swap_seat.get_managed_cpa_auth_names", lambda: {"a.json"})
    monkeypatch.setattr(
        "autoteam.swap_seat.check_cpa_codex_quota",
        lambda auth, account_id=None: (_ for _ in ()).throw(AssertionError("CPA quota should be skipped before reset")),
    )

    result = cmd_swap_seats(chatgpt_api=FakeChatGPT())

    assert result["skipped"] is True
    assert result["reason"] == "no_quota_available"
    assert result["summary"]["quota_available"] == 0


def test_cmd_swap_seats_uses_recent_ok_cache_to_avoid_frequent_quota_checks(monkeypatch, tmp_path):
    class FakeChatGPT:
        browser = True

        def update_member_seat_type(self, user_id, seat_type):
            raise AssertionError("seat should not be updated")

    auth = {
        "name": "a.json",
        "auth_index": "idx-a",
        "provider": "codex",
        "email": "a@example.com",
        "status": "active",
        "disabled": False,
    }

    monkeypatch.setattr("autoteam.swap_seat.SWAP_COOLDOWN_FILE", tmp_path / "swap_cooldown.json")
    monkeypatch.setattr("autoteam.swap_seat.SWAP_QUOTA_STATE_FILE", tmp_path / "swap_quota_state.json")
    monkeypatch.setattr("autoteam.swap_seat.SWAP_QUOTA_CHECK_MIN_INTERVAL_SECONDS", 3600)
    monkeypatch.setattr("autoteam.swap_seat.get_admin_email", lambda: "")
    monkeypatch.setattr("autoteam.swap_seat.get_chatgpt_account_id", lambda: "")
    record_quota_result(auth, "ok", {"primary_pct": 10, "weekly_pct": 20})

    _patch_managed_member_emails(monkeypatch, "a@example.com")
    monkeypatch.setattr(
        "autoteam.swap_seat.fetch_team_members",
        lambda _chatgpt: [{"email": "a@example.com", "id": "u-a", "seat_type": "default"}],
    )
    monkeypatch.setattr("autoteam.swap_seat.list_cpa_files", lambda: [auth])
    monkeypatch.setattr("autoteam.swap_seat.get_managed_cpa_auth_names", lambda: {"a.json"})
    monkeypatch.setattr(
        "autoteam.swap_seat.check_cpa_codex_quota",
        lambda auth, account_id=None: (_ for _ in ()).throw(AssertionError("recent quota should be cached")),
    )

    result = cmd_swap_seats(max_chatgpt_active=1, chatgpt_api=FakeChatGPT())

    assert result["skipped"] is True
    assert result["reason"] == "no_changes_needed"
    assert result["selected_emails"] == ["a@example.com"]
    assert not (tmp_path / "swap_cooldown.json").exists()


def test_cmd_swap_seats_checks_new_auth_without_quota_cache_once(monkeypatch, tmp_path):
    class FakeChatGPT:
        browser = True

        def update_member_seat_type(self, user_id, seat_type):
            raise AssertionError("seat should not be updated")

    auth = {
        "name": "new.json",
        "auth_index": "idx-new",
        "provider": "codex",
        "email": "new@example.com",
        "status": "active",
        "disabled": False,
    }
    calls = []

    _patch_managed_member_emails(monkeypatch, "new@example.com")
    monkeypatch.setattr("autoteam.swap_seat.SWAP_COOLDOWN_FILE", tmp_path / "swap_cooldown.json")
    monkeypatch.setattr("autoteam.swap_seat.SWAP_QUOTA_STATE_FILE", tmp_path / "swap_quota_state.json")
    monkeypatch.setattr("autoteam.swap_seat.get_admin_email", lambda: "")
    monkeypatch.setattr(
        "autoteam.swap_seat.fetch_team_members",
        lambda _chatgpt: [{"email": "new@example.com", "id": "u-new", "seat_type": "default"}],
    )
    monkeypatch.setattr("autoteam.swap_seat.list_cpa_files", lambda: [auth])
    monkeypatch.setattr("autoteam.swap_seat.get_managed_cpa_auth_names", lambda: {"new.json"})
    monkeypatch.setattr(
        "autoteam.swap_seat.check_cpa_codex_quota",
        lambda auth, account_id=None: calls.append(auth["name"]) or ("ok", {"primary_pct": 15, "weekly_pct": 25}),
    )

    result = cmd_swap_seats(max_chatgpt_active=1, chatgpt_api=FakeChatGPT())

    assert calls == ["new.json"]
    assert result["skipped"] is True
    assert result["reason"] == "no_changes_needed"
    assert result["selected_emails"] == ["new@example.com"]


def test_build_swap_plan_monthly_exhausted_account_is_not_selected():
    team_members = [
        {"email": "a@example.com", "id": "u-a", "seat_type": "usage_based"},
        {"email": "b@example.com", "id": "u-b", "seat_type": "usage_based"},
    ]
    cpa_auths = [
        {"name": "a.json", "auth_index": "idx-a", "provider": "codex", "email": "a@example.com", "status": "disabled", "disabled": True},
        {"name": "b.json", "auth_index": "idx-b", "provider": "codex", "email": "b@example.com", "status": "disabled", "disabled": True},
    ]
    quota_results = {
        "a.json": ("ok", {"primary_pct": 10, "weekly_pct": 10, "monthly_pct": 100, "monthly_resets_at": 300}),
        "b.json": ("ok", {"primary_pct": 10, "weekly_pct": 10, "monthly_pct": 20, "monthly_resets_at": 400}),
    }

    plan = build_swap_plan(team_members, cpa_auths, quota_results, max_chatgpt_active=1)

    assert plan["selected_emails"] == ["b@example.com"]
    seat_by_email = {item["email"]: item for item in plan["seat_actions"]}
    assert seat_by_email["a@example.com"]["desired_seat"] == "codex"
    assert seat_by_email["b@example.com"]["desired_seat"] == "chatgpt"


def test_monthly_only_quota_ignores_primary_and_weekly_windows():
    status, info = cpa_sync.parse_codex_quota_usage(
        {
            "rate_limit": {
                "monthly_window": {"used_percent": 20, "reset_at": 300},
            }
        }
    )

    assert status == "ok"
    assert info["quota_windows"] == ["monthly"]
    assert info["primary_applicable"] is False
    assert info["weekly_applicable"] is False
    assert info["monthly_applicable"] is True
    assert quota_available(status, info) is True


def test_build_swap_plan_selects_monthly_only_account_when_monthly_available():
    team_members = [
        {"email": "monthly@example.com", "id": "u-monthly", "seat_type": "usage_based"},
        {"email": "weekly@example.com", "id": "u-weekly", "seat_type": "usage_based"},
    ]
    cpa_auths = [
        {
            "name": "monthly.json",
            "auth_index": "idx-monthly",
            "provider": "codex",
            "email": "monthly@example.com",
            "status": "disabled",
            "disabled": True,
        },
        {
            "name": "weekly.json",
            "auth_index": "idx-weekly",
            "provider": "codex",
            "email": "weekly@example.com",
            "status": "disabled",
            "disabled": True,
        },
    ]
    quota_results = {
        "monthly.json": (
            "ok",
            {
                "primary_pct": 0,
                "primary_applicable": False,
                "weekly_pct": 0,
                "weekly_applicable": False,
                "monthly_pct": 20,
                "monthly_applicable": True,
                "quota_windows": ["monthly"],
            },
        ),
        "weekly.json": (
            "ok",
            {
                "primary_pct": 95,
                "primary_applicable": True,
                "weekly_pct": 50,
                "weekly_applicable": True,
                "monthly_pct": 0,
                "monthly_applicable": False,
                "quota_windows": ["primary", "weekly"],
            },
        ),
    }

    plan = build_swap_plan(team_members, cpa_auths, quota_results, max_chatgpt_active=1)

    assert plan["selected_emails"] == ["monthly@example.com"]
    monthly_state = next(item for item in plan["member_states"] if item["email"] == "monthly@example.com")
    assert monthly_state["primary_applicable"] is False
    assert monthly_state["weekly_applicable"] is False
    assert monthly_state["monthly_applicable"] is True


def test_swap_cooldown_enforces_two_hours_and_three_per_day(monkeypatch, tmp_path):
    monkeypatch.setattr("autoteam.swap_seat.SWAP_COOLDOWN_FILE", tmp_path / "swap_cooldown.json")

    day_start = 1_800_000_000.0
    first = reserve_swap_cooldown_slot(day_start)
    assert first["used_today"] == 1

    try:
        reserve_swap_cooldown_slot(day_start + 60)
        raise AssertionError("expected cooldown")
    except SwapSeatCooldownError as exc:
        assert "min_interval" in exc.status["blocked_reasons"]
        assert exc.retry_after > 0

    reserve_swap_cooldown_slot(day_start + 2 * 60 * 60)
    third = reserve_swap_cooldown_slot(day_start + 4 * 60 * 60)
    assert third["used_today"] == 3

    try:
        reserve_swap_cooldown_slot(day_start + 6 * 60 * 60)
        raise AssertionError("expected daily limit")
    except SwapSeatCooldownError as exc:
        assert "daily_limit" in exc.status["blocked_reasons"]


def test_swap_cooldown_resets_next_day(monkeypatch, tmp_path):
    monkeypatch.setattr("autoteam.swap_seat.SWAP_COOLDOWN_FILE", tmp_path / "swap_cooldown.json")

    # 2027-01-15 00:00:00 local-ish timestamp is only used for relative same/day checks.
    now = 1_800_000_000.0
    reserve_swap_cooldown_slot(now)
    reserve_swap_cooldown_slot(now + 2 * 60 * 60)
    reserve_swap_cooldown_slot(now + 4 * 60 * 60)

    status = get_swap_cooldown_status(now + 24 * 60 * 60)
    assert status["used_today"] == 0
    assert status["allowed"] is True


def test_swap_cooldown_interval_crosses_midnight(monkeypatch, tmp_path):
    monkeypatch.setattr("autoteam.swap_seat.SWAP_COOLDOWN_FILE", tmp_path / "swap_cooldown.json")

    before_midnight = time.mktime((2027, 1, 15, 23, 30, 0, 0, 0, -1))
    reserve_swap_cooldown_slot(before_midnight)

    after_midnight = before_midnight + 60 * 60
    status = get_swap_cooldown_status(after_midnight)
    assert status["used_today"] == 0
    assert status["allowed"] is False
    assert "min_interval" in status["blocked_reasons"]


def test_build_swap_plan_forces_admin_codex_and_caps_five():
    team_members = [
        {"email": "admin@example.com", "id": "u-admin", "seat_type": "default"},
        {"email": "a@example.com", "id": "u-a", "seat_type": "usage_based"},
        {"email": "b@example.com", "id": "u-b", "seat_type": "usage_based"},
        {"email": "c@example.com", "id": "u-c", "seat_type": "usage_based"},
    ]
    cpa_auths = [
        {"name": "admin.json", "auth_index": "idx-admin", "provider": "codex", "email": "admin@example.com", "status": "active", "disabled": False},
        {"name": "a.json", "auth_index": "idx-a", "provider": "codex", "email": "a@example.com", "status": "active", "disabled": False},
        {"name": "b.json", "auth_index": "idx-b", "provider": "codex", "email": "b@example.com", "status": "active", "disabled": False},
        {"name": "c.json", "auth_index": "idx-c", "provider": "codex", "email": "c@example.com", "status": "active", "disabled": False},
    ]
    quota_results = {
        "admin.json": ("ok", {"primary_pct": 0, "weekly_pct": 0}),
        "a.json": ("ok", {"primary_pct": 10, "weekly_pct": 10}),
        "b.json": ("ok", {"primary_pct": 20, "weekly_pct": 20}),
        "c.json": ("ok", {"primary_pct": 30, "weekly_pct": 30}),
    }

    plan = build_swap_plan(
        team_members,
        cpa_auths,
        quota_results,
        max_chatgpt_active=99,
        forced_codex_emails={"admin@example.com"},
    )

    assert len(plan["selected_emails"]) == 3
    assert "admin@example.com" not in plan["selected_emails"]
    seat_by_email = {item["email"]: item for item in plan["seat_actions"]}
    assert seat_by_email["admin@example.com"]["desired_seat"] == "codex"
    assert seat_by_email["admin@example.com"]["force_codex"] is True


def test_build_swap_plan_supports_single_active_limit_and_blank_cpa_status():
    team_members = [
        {"email": "a@example.com", "id": "u-a", "seat_type": "default"},
        {"email": "b@example.com", "id": "u-b", "seat_type": "usage_based"},
    ]
    cpa_auths = [
        {"name": "a.json", "auth_index": "idx-a", "provider": "codex", "email": "a@example.com", "disabled": False},
        {"name": "b.json", "auth_index": "idx-b", "provider": "codex", "email": "b@example.com", "disabled": True},
    ]
    quota_results = {
        "a.json": ("ok", {"primary_pct": 10, "weekly_pct": 10}),
        "b.json": ("ok", {"primary_pct": 20, "weekly_pct": 20}),
    }

    plan = build_swap_plan(team_members, cpa_auths, quota_results, max_chatgpt_active=1)

    assert len(plan["selected_emails"]) == 1
    # a.json 已经 disabled=false，即使 CPA 列表没返回 status，也不应被误判为需要 enable。
    oauth_by_name = {item["name"]: item for item in plan["oauth_actions"]}
    assert oauth_by_name["a.json"]["desired_disabled"] is False
    assert oauth_by_name["a.json"]["needs_update"] is False


def test_build_swap_plan_counts_blank_status_active_oauth_against_capacity():
    team_members = [
        {"email": "owner@example.com", "id": "u-owner", "seat_type": "usage_based"},
        {"email": "managed@example.com", "id": "u-managed", "seat_type": "usage_based"},
    ]
    cpa_auths = [
        {"name": "owner.json", "auth_index": "idx-owner", "provider": "codex", "email": "owner@example.com", "disabled": False},
        {
            "name": "managed.json",
            "auth_index": "idx-managed",
            "provider": "codex",
            "email": "managed@example.com",
            "status": "disabled",
            "disabled": True,
        },
    ]
    quota_results = {
        "managed.json": ("ok", {"primary_pct": 10, "weekly_pct": 10}),
    }

    plan = build_swap_plan(
        team_members,
        cpa_auths,
        quota_results,
        max_chatgpt_active=1,
        whitelist_emails={"owner@example.com"},
    )

    assert plan["summary"]["whitelisted_active_oauth"] == 1
    assert plan["summary"]["managed_active_capacity"] == 0
    assert plan["selected_emails"] == []
    seat_by_email = {item["email"]: item for item in plan["seat_actions"]}
    assert seat_by_email["managed@example.com"]["desired_seat"] == "codex"


def test_build_swap_plan_uses_available_duplicate_auth():
    team_members = [{"email": "dup@example.com", "id": "u-dup", "seat_type": "usage_based"}]
    cpa_auths = [
        {"name": "dup-active-exhausted.json", "auth_index": "idx-old", "provider": "codex", "email": "dup@example.com", "status": "active", "disabled": False},
        {"name": "dup-disabled-ok.json", "auth_index": "idx-new", "provider": "codex", "email": "dup@example.com", "status": "disabled", "disabled": True},
    ]
    quota_results = {
        "dup-active-exhausted.json": ("exhausted", {"quota_info": {"primary_pct": 100, "weekly_pct": 0}}),
        "dup-disabled-ok.json": ("ok", {"primary_pct": 5, "weekly_pct": 10}),
    }

    plan = build_swap_plan(team_members, cpa_auths, quota_results, max_chatgpt_active=2)

    assert plan["selected_emails"] == ["dup@example.com"]
    assert plan["selected_auth_ids"] == ["dup-disabled-ok.json"]
    oauth_by_name = {item["name"]: item for item in plan["oauth_actions"]}
    assert oauth_by_name["dup-active-exhausted.json"]["desired_disabled"] is True
    assert oauth_by_name["dup-disabled-ok.json"]["desired_disabled"] is False


def test_build_swap_plan_whitelist_skips_quota_seat_and_oauth_actions():
    team_members = [
        {"email": "keep@example.com", "id": "u-keep", "seat_type": "default"},
        {"email": "a@example.com", "id": "u-a", "seat_type": "usage_based"},
    ]
    cpa_auths = [
        {"name": "keep.json", "auth_index": "idx-keep", "provider": "codex", "email": "keep@example.com", "status": "active", "disabled": False},
        {"name": "a.json", "auth_index": "idx-a", "provider": "codex", "email": "a@example.com", "status": "disabled", "disabled": True},
    ]
    quota_results = {
        "a.json": ("ok", {"primary_pct": 10, "weekly_pct": 10}),
    }

    plan = build_swap_plan(
        team_members,
        cpa_auths,
        quota_results,
        max_chatgpt_active=2,
        whitelist_emails={"keep@example.com"},
    )

    assert plan["selected_emails"] == ["a@example.com"]
    assert [item["email"] for item in plan["seat_actions"]] == ["a@example.com"]
    assert [item["name"] for item in plan["oauth_actions"]] == ["a.json"]
    assert plan["summary"]["whitelisted_team_members"] == 1
    assert plan["summary"]["whitelisted_cpa_oauth"] == 1


def test_cmd_swap_seats_skips_whitelisted_quota_and_seat(monkeypatch, tmp_path):
    class FakeChatGPT:
        browser = True

        def __init__(self):
            self.seat_calls = []

        def update_member_seat_type(self, user_id, seat_type):
            self.seat_calls.append((user_id, seat_type))
            return {"status": 200, "body": "{}"}

    fake_chatgpt = FakeChatGPT()
    cpa_updates = []
    team_members = [
        {"email": "keep@example.com", "id": "u-keep", "seat_type": "default"},
        {"email": "a@example.com", "id": "u-a", "seat_type": "usage_based"},
    ]
    cpa_auths = [
        {"name": "keep.json", "auth_index": "idx-keep", "provider": "codex", "email": "keep@example.com", "status": "active", "disabled": False},
        {"name": "a.json", "auth_index": "idx-a", "provider": "codex", "email": "a@example.com", "status": "disabled", "disabled": True},
    ]
    checked = []

    _patch_managed_member_emails(monkeypatch, "a@example.com")
    monkeypatch.setenv("SWAP_SEAT_WHITELIST_EMAILS", "keep@example.com")
    monkeypatch.setattr("autoteam.swap_seat.SWAP_COOLDOWN_FILE", tmp_path / "swap_cooldown.json")
    monkeypatch.setattr("autoteam.swap_seat.SWAP_QUOTA_STATE_FILE", tmp_path / "swap_quota_state.json")
    monkeypatch.setattr("autoteam.swap_seat.get_admin_email", lambda: "")
    monkeypatch.setattr("autoteam.swap_seat.fetch_team_members", lambda _chatgpt: team_members)
    monkeypatch.setattr("autoteam.swap_seat.list_cpa_files", lambda: cpa_auths)
    monkeypatch.setattr("autoteam.swap_seat.get_managed_cpa_auth_names", lambda: {"keep.json", "a.json"})
    monkeypatch.setattr(
        "autoteam.swap_seat.check_cpa_codex_quota",
        lambda auth, account_id=None: checked.append(auth["name"]) or ("ok", {"primary_pct": 10, "weekly_pct": 10}),
    )
    monkeypatch.setattr(
        "autoteam.swap_seat.set_cpa_auth_disabled",
        lambda name, disabled: cpa_updates.append((name, disabled)) or {"status": "ok"},
    )

    result = cmd_swap_seats(max_chatgpt_active=2, chatgpt_api=fake_chatgpt)

    assert checked == ["a.json"]
    assert fake_chatgpt.seat_calls == [("u-a", "default")]
    assert cpa_updates == [("a.json", False)]
    assert result["whitelist_emails"] == ["keep@example.com"]


def test_cmd_swap_seats_uses_team_account_id_for_shared_admin_session(monkeypatch, tmp_path):
    class FakeChatGPT:
        browser = None

        def __init__(self):
            self.account_id = ""
            self.seat_calls = []
            self.stopped = False

        def start(self):
            self.browser = True
            self.account_id = "acc-default"

        def stop(self):
            self.browser = None
            self.stopped = True

        def update_member_seat_type(self, user_id, seat_type):
            self.seat_calls.append((self.account_id, user_id, seat_type))
            return {"status": 200, "body": "{}"}

    fake_chatgpt = FakeChatGPT()
    team_context = TeamContext(id="team-b", account_id="acc-team", workspace_name="Team B")
    auth = {
        "name": "a.json",
        "auth_index": "idx-a",
        "provider": "codex",
        "email": "a@example.com",
        "status": "disabled",
        "disabled": True,
    }

    monkeypatch.setattr("autoteam.swap_seat.ChatGPTTeamAPI", lambda: fake_chatgpt)
    _patch_managed_member_emails(monkeypatch, "a@example.com")
    monkeypatch.setattr("autoteam.swap_seat.SWAP_COOLDOWN_FILE", tmp_path / "swap_cooldown.json")
    monkeypatch.setattr("autoteam.swap_seat.SWAP_QUOTA_STATE_FILE", tmp_path / "swap_quota_state.json")
    monkeypatch.setattr("autoteam.swap_seat.get_admin_email", lambda: "")

    def fake_fetch(_chatgpt, account_id=None):
        assert account_id == "acc-team"
        return [{"email": "a@example.com", "id": "u-a", "seat_type": "usage_based"}]

    monkeypatch.setattr("autoteam.swap_seat.fetch_team_members", fake_fetch)
    monkeypatch.setattr("autoteam.swap_seat.list_cpa_files", lambda: [auth])
    monkeypatch.setattr("autoteam.swap_seat.get_managed_cpa_auth_names", lambda: {"a.json"})
    monkeypatch.setattr(
        "autoteam.swap_seat.check_cpa_codex_quota",
        lambda auth, account_id=None: ("ok", {"primary_pct": 10, "weekly_pct": 10}),
    )
    monkeypatch.setattr("autoteam.swap_seat.set_cpa_auth_disabled", lambda name, disabled: {"status": "ok"})

    result = cmd_swap_seats(max_chatgpt_active=1, team_context=team_context)

    assert result["team"]["account_id"] == "acc-team"
    assert fake_chatgpt.seat_calls == [("acc-team", "u-a", "default")]
    assert fake_chatgpt.stopped is True
