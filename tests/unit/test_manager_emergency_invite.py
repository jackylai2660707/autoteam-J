from autoteam import manager


class _FakeChatGPT:
    browser = True

    def __init__(self):
        self.started = False
        self.stopped = False
        self.allow_team_invites = False
        self.seat_calls = []

    def start(self):
        self.started = True
        self.browser = True

    def stop(self):
        self.stopped = True
        self.browser = None

    def update_member_seat_type(self, user_id, seat_type):
        self.seat_calls.append((user_id, seat_type))
        return {"status": 200, "body": "{}"}


class _FakeCfMail:
    provider_name = "cloudflare_temp_email"
    service_id = "cf-1"

    def login(self):
        return "ok"

    def _resolve_account_id_for_email(self, email):
        if email == "pending@example.com":
            return "addr-1"
        return None

    def search_emails_by_recipient(self, to_email, size=10, account_id=None):
        if to_email == "pending@example.com":
            return [{"sendEmail": "noreply@openai.com", "subject": "Invite", "body": "invite"}]
        return []

    def wait_for_email(self, **_kwargs):
        return {"sendEmail": "noreply@openai.com", "subject": "Invite", "body": "invite"}

    def extract_invite_link(self, _email_data):
        return "https://invite.example/abc"


def test_cmd_add_consumes_pending_invite_and_activates_new_account(monkeypatch):
    fake_chatgpt = _FakeChatGPT()
    created = []

    monkeypatch.setattr(manager, "ChatGPTTeamAPI", lambda: fake_chatgpt)
    monkeypatch.setattr(manager, "get_mail_client", lambda **kwargs: _FakeCfMail())
    monkeypatch.setattr(
        manager,
        "create_new_account",
        lambda chatgpt, mail_client, **_kwargs: created.append((chatgpt.allow_team_invites, mail_client.provider_name))
        or "new@example.com",
    )
    monkeypatch.setattr(manager, "_activate_registered_account", lambda chatgpt, email, **_kwargs: {"ok": True, "email": email})

    result = manager.cmd_add()

    assert result["mode"] == "consume_pending_invite"
    assert result["invited"] is True
    assert result["email"] == "new@example.com"
    assert result["activation"] == {"ok": True, "email": "new@example.com"}
    assert created == [(False, "cloudflare_temp_email")]
    assert fake_chatgpt.allow_team_invites is False


def test_create_new_account_consumes_existing_pending_invite_and_pre_sweeps_old_members(monkeypatch):
    fake_chatgpt = _FakeChatGPT()
    fake_mail = _FakeCfMail()
    team_members = [
        {"email": "old@example.com", "id": "u-old", "seat_type": "default"},
        {"email": "skip@example.com", "id": "u-skip", "seat_type": "default"},
        {"email": "codex@example.com", "id": "u-codex", "seat_type": "usage_based"},
    ]
    invites = [{"id": "inv-1", "email_address": "pending@example.com", "seat_type": "usage_based"}]
    added = []

    monkeypatch.setenv("SWAP_SEAT_WHITELIST_EMAILS", "skip@example.com")
    monkeypatch.setattr("autoteam.swap_seat.fetch_team_members", lambda _chatgpt: team_members)
    monkeypatch.setattr(manager, "fetch_team_state", lambda _chatgpt: (team_members, invites))
    monkeypatch.setattr(manager, "load_accounts", lambda: [])
    monkeypatch.setattr(
        manager,
        "add_account",
        lambda email, password, **kwargs: added.append((email, password, kwargs)),
    )
    monkeypatch.setattr(manager, "update_account", lambda *args, **kwargs: None)
    monkeypatch.setattr(manager, "_complete_registration", lambda email, password, invite_link, mail_client: email)

    result = manager.create_new_account(fake_chatgpt, fake_mail)

    assert result == "pending@example.com"
    assert fake_chatgpt.seat_calls == [("u-old", "usage_based")]
    assert added and added[0][0] == "pending@example.com"
    assert fake_chatgpt.stopped is True


def test_activate_registered_account_promotes_new_member_and_disables_old_oauth(monkeypatch):
    fake_chatgpt = _FakeChatGPT()
    updates = []
    oauth_updates = []
    members = [
        {"email": "old@example.com", "id": "u-old", "seat_type": "usage_based"},
        {"email": "new@example.com", "id": "u-new", "seat_type": "usage_based"},
    ]
    auths = [
        {"name": "old.json", "provider": "codex", "email": "old@example.com", "status": "active", "disabled": False},
        {"name": "new.json", "provider": "codex", "email": "new@example.com", "status": "disabled", "disabled": True},
    ]

    monkeypatch.delenv("SWAP_SEAT_WHITELIST_EMAILS", raising=False)
    monkeypatch.setattr(manager, "fetch_team_state", lambda _chatgpt: (members, []))
    monkeypatch.setattr(manager, "list_cpa_files", lambda: auths)
    monkeypatch.setattr(
        manager,
        "set_cpa_auth_disabled",
        lambda auth, disabled: oauth_updates.append(((auth.get("name") if isinstance(auth, dict) else auth), disabled)) or {"status": "ok"},
    )
    monkeypatch.setattr(manager, "update_account", lambda email, **kwargs: updates.append((email, kwargs)))

    result = manager._activate_registered_account(fake_chatgpt, "new@example.com")

    assert result["ok"] is True
    assert fake_chatgpt.seat_calls == [("u-new", "default")]
    assert oauth_updates == [("old.json", True), ("new.json", False)]
    assert updates and updates[0][0] == "new@example.com"
    assert updates[0][1]["status"] == "active"
    assert updates[0][1]["seat_type"] == "chatgpt"


def test_cmd_manage_teams_runs_each_configured_team(monkeypatch):
    class _Team:
        def __init__(self, account_id, label, limit):
            self.id = account_id
            self.account_id = account_id
            self.workspace_name = label
            self.label = label
            self.max_chatgpt_active = limit
            self.pending_invite_email = ""

        def public_dict(self):
            return {"account_id": self.account_id, "label": self.label}

    teams = [_Team("acc-a", "Team A", 1), _Team("acc-b", "Team B", 2)]
    calls = []

    monkeypatch.setattr("autoteam.team_context.get_team_contexts", lambda default_max_chatgpt_active=2: teams)
    monkeypatch.setattr(
        manager,
        "cmd_auto_detect_replace",
        lambda max_chatgpt_active=2, pending_invite_email=None, team_context=None: calls.append(
            (team_context.account_id, max_chatgpt_active)
        )
        or {"mode": "auto_detect_replace", "team": team_context.label},
    )

    result = manager.cmd_manage_teams(max_chatgpt_active=2, replace_with_pending_invite=True)

    assert result["mode"] == "multi_team_manage"
    assert result["teams_total"] == 2
    assert result["teams_ok"] == 2
    assert calls == [("acc-a", 1), ("acc-b", 2)]


def test_cmd_manage_teams_consumes_each_team_pending_invite_when_enabled(monkeypatch):
    class _Team:
        def __init__(self, account_id, label, pending_invite_email):
            self.id = account_id
            self.account_id = account_id
            self.workspace_name = label
            self.label = label
            self.max_chatgpt_active = 2
            self.pending_invite_email = pending_invite_email

        def public_dict(self):
            return {"account_id": self.account_id, "label": self.label, "pending_invite_email": self.pending_invite_email}

    teams = [
        _Team("acc-a", "Team A", "pending-a@example.com"),
        _Team("acc-b", "Team B", "pending-b@example.com"),
    ]
    calls = []

    monkeypatch.setattr("autoteam.team_context.get_team_contexts", lambda default_max_chatgpt_active=2: teams)
    monkeypatch.setattr(
        manager,
        "cmd_auto_detect_replace",
        lambda max_chatgpt_active=2, pending_invite_email=None, team_context=None: calls.append(
            (team_context.account_id, pending_invite_email, max_chatgpt_active)
        )
        or {"mode": "auto_detect_replace", "team": team_context.label},
    )

    result = manager.cmd_manage_teams(max_chatgpt_active=2, replace_with_pending_invite=True)

    assert result["teams_ok"] == 2
    assert calls == [
        ("acc-a", "pending-a@example.com", 2),
        ("acc-b", "pending-b@example.com", 2),
    ]


def test_start_chatgpt_for_team_keeps_configured_account_id_with_shared_session():
    class _Team:
        account_id = "acc-team"
        session_token = ""
        workspace_name = "Team B"

    class _SharedSessionChatGPT(_FakeChatGPT):
        def start(self):
            super().start()
            self.account_id = "acc-default"
            self.workspace_name = "Default Team"

    fake_chatgpt = _SharedSessionChatGPT()

    manager._start_chatgpt_for_team(fake_chatgpt, _Team())

    assert fake_chatgpt.account_id == "acc-team"
    assert fake_chatgpt.workspace_name == "Team B"
