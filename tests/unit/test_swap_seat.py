import time

from autoteam import cpa_sync
from autoteam.team_context import TeamContext
from autoteam.swap_seat import (
    SwapSeatCooldownError,
    build_swap_plan,
    cmd_swap_seats,
    get_swap_cooldown_status,
    record_quota_result,
    reserve_swap_cooldown_slot,
)


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

    monkeypatch.setattr("autoteam.swap_seat.get_chatgpt_account_id", lambda: "acc-1")
    monkeypatch.setattr("autoteam.swap_seat.SWAP_COOLDOWN_FILE", tmp_path / "swap_cooldown.json")
    monkeypatch.setattr("autoteam.swap_seat.SWAP_QUOTA_STATE_FILE", tmp_path / "swap_quota_state.json")
    monkeypatch.setattr("autoteam.swap_seat.fetch_team_members", lambda _chatgpt: team_members)
    monkeypatch.setattr("autoteam.swap_seat.list_cpa_files", lambda: cpa_auths)
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


def test_cmd_swap_seats_no_available_accounts_does_not_swap_or_consume_cooldown(monkeypatch, tmp_path):
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

    monkeypatch.setattr("autoteam.swap_seat.SWAP_COOLDOWN_FILE", tmp_path / "swap_cooldown.json")
    monkeypatch.setattr("autoteam.swap_seat.SWAP_QUOTA_STATE_FILE", tmp_path / "swap_quota_state.json")
    monkeypatch.setattr("autoteam.swap_seat.fetch_team_members", lambda _chatgpt: team_members)
    monkeypatch.setattr("autoteam.swap_seat.list_cpa_files", lambda: cpa_auths)
    monkeypatch.setattr(
        "autoteam.swap_seat.check_cpa_codex_quota",
        lambda auth, account_id=None: ("exhausted", {"window": "primary", "resets_at": 2_000_000_000, "quota_info": {"primary_pct": 100, "weekly_pct": 10, "primary_resets_at": 2_000_000_000}}),
    )
    monkeypatch.setattr(
        "autoteam.swap_seat.set_cpa_auth_disabled",
        lambda name, disabled: cpa_updates.append((name, disabled)) or {"status": "ok"},
    )

    result = cmd_swap_seats(chatgpt_api=fake_chatgpt)

    assert result["skipped"] is True
    assert result["reason"] == "no_quota_available"
    assert fake_chatgpt.seat_calls == []
    assert cpa_updates == []
    assert not (tmp_path / "swap_cooldown.json").exists()


def test_cmd_swap_seats_uses_cached_exhausted_until_reset_and_skips_cpa(monkeypatch, tmp_path):
    class FakeChatGPT:
        browser = True

        def update_member_seat_type(self, user_id, seat_type):
            raise AssertionError("seat should not be updated")

    quota_state_file = tmp_path / "swap_quota_state.json"
    monkeypatch.setattr("autoteam.swap_seat.SWAP_COOLDOWN_FILE", tmp_path / "swap_cooldown.json")
    monkeypatch.setattr("autoteam.swap_seat.SWAP_QUOTA_STATE_FILE", quota_state_file)

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
    monkeypatch.setattr("autoteam.swap_seat.list_cpa_files", lambda: [auth])
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
    record_quota_result(auth, "ok", {"primary_pct": 10, "weekly_pct": 20})

    monkeypatch.setattr(
        "autoteam.swap_seat.fetch_team_members",
        lambda _chatgpt: [{"email": "a@example.com", "id": "u-a", "seat_type": "default"}],
    )
    monkeypatch.setattr("autoteam.swap_seat.list_cpa_files", lambda: [auth])
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

    monkeypatch.setattr("autoteam.swap_seat.SWAP_COOLDOWN_FILE", tmp_path / "swap_cooldown.json")
    monkeypatch.setattr("autoteam.swap_seat.SWAP_QUOTA_STATE_FILE", tmp_path / "swap_quota_state.json")
    monkeypatch.setattr("autoteam.swap_seat.get_admin_email", lambda: "")
    monkeypatch.setattr(
        "autoteam.swap_seat.fetch_team_members",
        lambda _chatgpt: [{"email": "new@example.com", "id": "u-new", "seat_type": "default"}],
    )
    monkeypatch.setattr("autoteam.swap_seat.list_cpa_files", lambda: [auth])
    monkeypatch.setattr(
        "autoteam.swap_seat.check_cpa_codex_quota",
        lambda auth, account_id=None: calls.append(auth["name"]) or ("ok", {"primary_pct": 15, "weekly_pct": 25}),
    )

    result = cmd_swap_seats(max_chatgpt_active=1, chatgpt_api=FakeChatGPT())

    assert calls == ["new.json"]
    assert result["skipped"] is True
    assert result["reason"] == "no_changes_needed"
    assert result["selected_emails"] == ["new@example.com"]


def test_swap_cooldown_enforces_two_hours_and_three_per_day(monkeypatch, tmp_path):
    monkeypatch.setattr("autoteam.swap_seat.SWAP_COOLDOWN_FILE", tmp_path / "swap_cooldown.json")

    day_start = 1_800_000_000.0
    first = reserve_swap_cooldown_slot(day_start)
    assert first["used_today"] == 1

    try:
        reserve_swap_cooldown_slot(day_start + 60)
        assert False, "expected cooldown"
    except SwapSeatCooldownError as exc:
        assert "min_interval" in exc.status["blocked_reasons"]
        assert exc.retry_after > 0

    reserve_swap_cooldown_slot(day_start + 2 * 60 * 60)
    third = reserve_swap_cooldown_slot(day_start + 4 * 60 * 60)
    assert third["used_today"] == 3

    try:
        reserve_swap_cooldown_slot(day_start + 6 * 60 * 60)
        assert False, "expected daily limit"
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

    monkeypatch.setenv("SWAP_SEAT_WHITELIST_EMAILS", "keep@example.com")
    monkeypatch.setattr("autoteam.swap_seat.SWAP_COOLDOWN_FILE", tmp_path / "swap_cooldown.json")
    monkeypatch.setattr("autoteam.swap_seat.SWAP_QUOTA_STATE_FILE", tmp_path / "swap_quota_state.json")
    monkeypatch.setattr("autoteam.swap_seat.get_admin_email", lambda: "")
    monkeypatch.setattr("autoteam.swap_seat.fetch_team_members", lambda _chatgpt: team_members)
    monkeypatch.setattr("autoteam.swap_seat.list_cpa_files", lambda: cpa_auths)
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
    monkeypatch.setattr("autoteam.swap_seat.SWAP_COOLDOWN_FILE", tmp_path / "swap_cooldown.json")
    monkeypatch.setattr("autoteam.swap_seat.SWAP_QUOTA_STATE_FILE", tmp_path / "swap_quota_state.json")
    monkeypatch.setattr("autoteam.swap_seat.get_admin_email", lambda: "")

    def fake_fetch(_chatgpt, account_id=None):
        assert account_id == "acc-team"
        return [{"email": "a@example.com", "id": "u-a", "seat_type": "usage_based"}]

    monkeypatch.setattr("autoteam.swap_seat.fetch_team_members", fake_fetch)
    monkeypatch.setattr("autoteam.swap_seat.list_cpa_files", lambda: [auth])
    monkeypatch.setattr(
        "autoteam.swap_seat.check_cpa_codex_quota",
        lambda auth, account_id=None: ("ok", {"primary_pct": 10, "weekly_pct": 10}),
    )
    monkeypatch.setattr("autoteam.swap_seat.set_cpa_auth_disabled", lambda name, disabled: {"status": "ok"})

    result = cmd_swap_seats(max_chatgpt_active=1, team_context=team_context)

    assert result["team"]["account_id"] == "acc-team"
    assert fake_chatgpt.seat_calls == [("acc-team", "u-a", "default")]
    assert fake_chatgpt.stopped is True
