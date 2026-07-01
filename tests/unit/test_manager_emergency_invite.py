from autoteam import manager


class _FakeChatGPT:
    browser = True

    def __init__(self):
        self.started = False
        self.stopped = False
        self.allow_team_invites = False
        self.seat_calls = []
        self.invite_calls = []

    def start(self):
        self.started = True
        self.browser = True

    def stop(self):
        self.stopped = True
        self.browser = None

    def update_member_seat_type(self, user_id, seat_type):
        self.seat_calls.append((user_id, seat_type))
        return {"status": 200, "body": "{}"}

    def invite_member(self, email, seat_type="usage_based"):
        self.invite_calls.append((email, seat_type))
        return 200, {"ok": True}


class _FakeCfMail:
    provider_name = "cloudflare_temp_email"
    service_id = "cf-1"

    def __init__(self):
        self.create_calls = []

    def login(self):
        return "ok"

    def _resolve_account_id_for_email(self, email):
        if email == "pending@example.com":
            return "addr-1"
        if email == "jackylai@latte-fitness.com":
            return "addr-forward"
        if email == "alex1234@rand.example.com":
            return "addr-new"
        return None

    def create_temp_email(self, prefix=None, *, domain=None, enable_random_subdomain=None):
        self.create_calls.append(
            {
                "prefix": prefix,
                "domain": domain,
                "enable_random_subdomain": enable_random_subdomain,
            }
        )
        return "addr-new", "alex1234@rand.example.com"

    def search_emails_by_recipient(self, to_email, size=10, account_id=None):
        if to_email in {"pending@example.com", "alex1234@rand.example.com"}:
            return [{"sendEmail": "noreply@openai.com", "subject": "Invite", "body": "invite"}]
        return []

    def wait_for_email(self, **_kwargs):
        return {"sendEmail": "noreply@openai.com", "subject": "Invite", "body": "invite"}

    def extract_invite_link(self, _email_data):
        return "https://invite.example/abc"


class _RecordingMail(_FakeCfMail):
    def __init__(self):
        super().__init__()
        self.search_calls = []
        self.wait_calls = []

    def search_emails_by_recipient(self, to_email, size=10, account_id=None):
        self.search_calls.append((to_email, size, account_id))
        return [
            {
                "sendEmail": "noreply@openai.com",
                "subject": "Invite",
                "body": "invite for sips.bonier.5d@icloud.com",
            }
        ]

    def wait_for_email(self, to_email, timeout=None, sender_keyword=None):
        self.wait_calls.append((to_email, timeout, sender_keyword))
        return {"sendEmail": "noreply@openai.com", "subject": "Invite", "body": "invite"}


class _IcloudMail(_RecordingMail):
    service_id = "icloud-hme"

    def __init__(self):
        super().__init__()
        self.login_calls = 0

    def login(self):
        self.login_calls += 1
        return "ok"


class _RetryCfMail(_FakeCfMail):
    def _resolve_account_id_for_email(self, email):
        if email == "retry@example.com":
            return "addr-retry"
        return super()._resolve_account_id_for_email(email)

    def search_emails_by_recipient(self, to_email, size=10, account_id=None):
        if to_email == "retry@example.com":
            return [{"sendEmail": "noreply@openai.com", "subject": "Invite", "body": "invite"}]
        return super().search_emails_by_recipient(to_email, size=size, account_id=account_id)


def test_parse_pending_invite_forward_map_accepts_text_and_json(monkeypatch):
    assert manager._parse_pending_invite_forward_map("icloud.com=jackylai@latte-fitness.com") == {
        "icloud.com": "jackylai@latte-fitness.com"
    }
    assert manager._parse_pending_invite_forward_map('{"@icloud.com":"jackylai@latte-fitness.com"}') == {
        "icloud.com": "jackylai@latte-fitness.com"
    }
    assert manager._parse_pending_invite_forward_map(
        '[{"from":"sips.bonier.5d@icloud.com","to":"jackylai@latte-fitness.com"}]'
    ) == {"sips.bonier.5d@icloud.com": "jackylai@latte-fitness.com"}

    monkeypatch.setenv("PENDING_INVITE_FORWARD_MAP", "*.icloud.com=>jackylai@latte-fitness.com")
    assert manager._pending_invite_forward_to("artist.armadas_5n@icloud.com") == "jackylai@latte-fitness.com"


def test_forwarded_recipient_mail_client_reads_from_delivery_mailbox(monkeypatch):
    monkeypatch.setenv("PENDING_INVITE_FORWARD_MAP", "icloud.com=jackylai@latte-fitness.com")
    base = _RecordingMail()

    wrapped = manager._with_pending_invite_forwarding(base)
    emails = wrapped.search_emails_by_recipient("sips.bonier.5d@icloud.com", size=7)
    email_item = wrapped.wait_for_email("sips.bonier.5d@icloud.com", timeout=12, sender_keyword="openai")

    assert emails
    assert email_item["subject"] == "Invite"
    assert base.search_calls == [
        ("jackylai@latte-fitness.com", 50, "addr-forward"),
        ("jackylai@latte-fitness.com", 50, "addr-forward"),
    ]
    assert base.wait_calls == []


def test_forwarded_recipient_mail_client_filters_to_original_recipient(monkeypatch):
    monkeypatch.setenv("PENDING_INVITE_FORWARD_MAP", "icloud.com=jackylai@latte-fitness.com")

    class _ForwardInboxMail(_RecordingMail):
        def search_emails_by_recipient(self, to_email, size=10, account_id=None):
            self.search_calls.append((to_email, size, account_id))
            return [
                {
                    "sendEmail": "noreply@openai.com",
                    "subject": "plus alias code",
                    "raw": (
                        "for <46.answer-gasses+2@icloud.com>\n"
                        "X-ICLOUD-HME: p=46.answer-gasses@icloud.com; f=jackylai@latte-fitness.com"
                    ),
                },
                {
                    "sendEmail": "noreply@openai.com",
                    "subject": "base code",
                    "raw": "for <46.answer-gasses@icloud.com>",
                },
                {
                    "sendEmail": "noreply@openai.com",
                    "subject": "other code",
                    "body": "Enter the code sent to other@icloud.com",
                },
            ]

    wrapped = manager._with_pending_invite_forwarding(_ForwardInboxMail())

    base_emails = wrapped.search_emails_by_recipient("46.answer-gasses@icloud.com")
    plus_emails = wrapped.search_emails_by_recipient("46.answer-gasses+2@icloud.com")

    assert [item["subject"] for item in base_emails] == ["base code"]
    assert [item["subject"] for item in plus_emails] == ["plus alias code"]


def test_direct_mail_service_takes_precedence_over_pending_forward_map(monkeypatch):
    monkeypatch.setenv("PENDING_INVITE_FORWARD_MAP", "icloud.com=jackylai@latte-fitness.com")
    monkeypatch.setenv(
        "MAIL_SERVICES_JSON",
        '[{"id":"icloud-hme","type":"cloudflare_temp_email","base_url":"https://icloud.example","admin_password":"secret","domain":"icloud.com"}]',
    )
    base = _RecordingMail()

    wrapped = manager._with_pending_invite_forwarding(base)
    wrapped.search_emails_by_recipient("sips.bonier.5d@icloud.com", size=7)

    assert manager._pending_invite_forward_to("sips.bonier.5d@icloud.com") == ""
    assert base.search_calls == [("sips.bonier.5d@icloud.com", 7, None)]


def test_pending_invite_mail_client_uses_domain_matched_service(monkeypatch):
    selected = _IcloudMail()
    monkeypatch.setenv(
        "MAIL_SERVICES_JSON",
        '[{"id":"default-cf","type":"cloudflare_temp_email","base_url":"https://cf.example","admin_password":"secret","domain":"example.com"},'
        '{"id":"icloud-hme","type":"cloudflare_temp_email","base_url":"https://icloud.example","admin_password":"secret","domain":"icloud.com"}]',
    )
    monkeypatch.setattr(manager, "get_mail_client_for_account", lambda _acc: selected)

    client = manager._mail_client_for_pending_invite(_FakeCfMail(), "sips.bonier.5d@icloud.com")

    assert getattr(client, "service_id", "") == "icloud-hme"
    assert selected.login_calls == 1


def test_pending_invite_candidates_allow_mapped_non_cfmail_domain(monkeypatch):
    fake_chatgpt = _FakeChatGPT()
    fake_mail = _FakeCfMail()
    invites = [{"id": "inv-icloud", "email_address": "user@icloud.com", "seat_type": "usage_based"}]

    monkeypatch.setattr(manager, "_fetch_team_state_for_account", lambda _chatgpt, account_id=None: ([], invites))
    monkeypatch.setattr(
        manager,
        "infer_mail_provider_from_email",
        lambda email: "cloudmail" if str(email).endswith("@icloud.com") else "",
    )

    assert manager._pending_invite_candidates(fake_chatgpt, fake_mail) == []

    monkeypatch.setenv("PENDING_INVITE_FORWARD_MAP", "icloud.com=jackylai@latte-fitness.com")
    candidates = manager._pending_invite_candidates(fake_chatgpt, fake_mail)

    assert candidates == [
        {
            "email": "user@icloud.com",
            "invite_id": "inv-icloud",
            "role": "",
            "seat_type": "usage_based",
            "mail_forward_to": "jackylai@latte-fitness.com",
        }
    ]


def test_ensure_local_pending_invite_account_uses_forwarded_mailbox_but_keeps_account_email(monkeypatch):
    fake_mail = _FakeCfMail()
    added = []
    updates = []

    monkeypatch.setenv("PENDING_INVITE_FORWARD_MAP", "icloud.com=jackylai@latte-fitness.com")
    monkeypatch.setattr(manager, "load_accounts", lambda: [])
    monkeypatch.setattr(
        manager,
        "add_account",
        lambda email, password, **kwargs: added.append((email, password, kwargs)),
    )
    monkeypatch.setattr(manager, "update_account", lambda email, **kwargs: updates.append((email, kwargs)))

    account_id = manager._ensure_local_pending_invite_account(
        "sips.bonier.5d@icloud.com",
        "new-password",
        fake_mail,
        team_account_id="acc-team",
    )

    assert account_id == "addr-forward"
    assert added[0][0] == "sips.bonier.5d@icloud.com"
    assert added[0][2]["mail_provider"] == "cloudflare_temp_email"
    assert added[0][2]["mail_account_id"] == "addr-forward"
    assert updates == [
        (
            "sips.bonier.5d@icloud.com",
            {
                "managed_by_autoteam": True,
                "disabled": False,
                "chatgpt_account_id": "acc-team",
                "mail_forward_to": "jackylai@latte-fitness.com",
            },
        )
    ]


def test_cmd_add_consumes_pending_invite_and_activates_new_account(monkeypatch):
    fake_chatgpt = _FakeChatGPT()
    created = []

    monkeypatch.setattr(manager, "ChatGPTTeamAPI", lambda: fake_chatgpt)
    monkeypatch.setattr(manager, "get_mail_client", lambda **kwargs: _FakeCfMail())
    monkeypatch.setattr(
        manager,
        "create_new_account",
        lambda chatgpt, mail_client, **_kwargs: (
            created.append((chatgpt.allow_team_invites, mail_client.provider_name)) or "new@example.com"
        ),
    )
    monkeypatch.setattr(
        manager, "_activate_registered_account", lambda chatgpt, email, **_kwargs: {"ok": True, "email": email}
    )
    monkeypatch.setattr(manager, "_post_registration_seat_rebalance", lambda chatgpt, **_kwargs: {"mode": "swap_seat"})

    result = manager.cmd_add()

    assert result["mode"] == "consume_pending_invite"
    assert result["invited"] is True
    assert result["email"] == "new@example.com"
    assert result["activation"] == {"ok": True, "email": "new@example.com"}
    assert result["rebalance"] == {"mode": "swap_seat"}
    assert created == [(False, "cloudflare_temp_email")]
    assert fake_chatgpt.allow_team_invites is False


def test_create_new_account_consumes_existing_pending_invite_without_pre_sweeping_old_members(monkeypatch):
    fake_chatgpt = _FakeChatGPT()
    fake_mail = _FakeCfMail()
    team_members = [
        {"email": "old@example.com", "id": "u-old", "seat_type": "default"},
        {"email": "skip@example.com", "id": "u-skip", "seat_type": "usage_based"},
        {"email": "codex@example.com", "id": "u-codex", "seat_type": "usage_based"},
    ]
    invites = [{"id": "inv-1", "email_address": "pending@example.com", "seat_type": "usage_based"}]
    added = []

    monkeypatch.setenv("SWAP_SEAT_WHITELIST_EMAILS", "skip@example.com")
    monkeypatch.setattr(manager, "_fetch_team_members_for_account", lambda _chatgpt, account_id=None: team_members)
    monkeypatch.setattr(manager, "fetch_team_state", lambda _chatgpt: (team_members, invites))
    monkeypatch.setattr(manager, "load_accounts", lambda: [])
    monkeypatch.setattr(
        manager,
        "add_account",
        lambda email, password, **kwargs: added.append((email, password, kwargs)),
    )
    monkeypatch.setattr(manager, "update_account", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        manager, "_complete_registration", lambda email, password, invite_link, mail_client, **_kwargs: email
    )

    result = manager.create_new_account(fake_chatgpt, fake_mail)

    assert result == "pending@example.com"
    assert fake_chatgpt.seat_calls == []
    assert added and added[0][0] == "pending@example.com"
    assert fake_chatgpt.stopped is True


def test_create_new_account_limits_and_rotates_failed_pending_invites(monkeypatch):
    fake_chatgpt = _FakeChatGPT()
    fake_mail = _FakeCfMail()
    team_members = [{"email": "old@example.com", "id": "u-old", "seat_type": "usage_based"}]
    invites = [
        {"id": "inv-recent", "email_address": "recent@example.com", "seat_type": "usage_based"},
        {"id": "inv-first", "email_address": "first@example.com", "seat_type": "usage_based"},
        {"id": "inv-second", "email_address": "second@example.com", "seat_type": "usage_based"},
    ]
    attempted = []
    updates = []

    monkeypatch.setenv("PENDING_INVITE_MAX_REGISTRATION_ATTEMPTS", "2")
    monkeypatch.setattr(manager.time, "time", lambda: 1000.0)
    monkeypatch.setattr(manager, "_fetch_team_members_for_account", lambda _chatgpt, account_id=None: team_members)
    monkeypatch.setattr(manager, "fetch_team_state", lambda _chatgpt: (team_members, invites))
    monkeypatch.setattr(
        manager,
        "load_accounts",
        lambda: [
            {
                "email": "recent@example.com",
                "status": "pending",
                "pending_invite_attempts": 1,
                "pending_invite_last_attempt_at": 999.0,
            }
        ],
    )
    monkeypatch.setattr(manager, "_extract_pending_invite_link", lambda *_args, **_kwargs: "https://invite.example")
    monkeypatch.setattr(
        manager,
        "_complete_registration",
        lambda email, *_args, **_kwargs: attempted.append(email) or None,
    )
    monkeypatch.setattr(manager, "add_account", lambda *args, **kwargs: None)
    monkeypatch.setattr(manager, "update_account", lambda email, **kwargs: updates.append((email, kwargs)))

    result = manager.create_new_account(fake_chatgpt, fake_mail)

    assert result is None
    assert attempted == ["first@example.com", "second@example.com"]
    assert "recent@example.com" not in attempted
    assert [
        (email, fields.get("pending_invite_last_attempt_error"))
        for email, fields in updates
        if "pending_invite_last_attempt_error" in fields
    ] == [
        ("first@example.com", None),
        ("first@example.com", "registration_failed"),
        ("second@example.com", None),
        ("second@example.com", "registration_failed"),
    ]


def test_ensure_local_pending_invite_account_clears_old_disabled_flag(monkeypatch):
    fake_mail = _FakeCfMail()
    updates = []

    monkeypatch.setattr(
        manager,
        "load_accounts",
        lambda: [
            {
                "email": "pending@example.com",
                "password": "old",
                "status": "standby",
                "disabled": True,
                "mail_provider": "cloudflare_temp_email",
            }
        ],
    )
    monkeypatch.setattr(manager, "update_account", lambda email, **kwargs: updates.append((email, kwargs)))

    account_id = manager._ensure_local_pending_invite_account(
        "pending@example.com",
        "new-password",
        fake_mail,
        team_account_id="acc-team",
    )

    assert account_id == "addr-1"
    assert updates == [
        (
            "pending@example.com",
            {
                "password": "new-password",
                "mail_provider": "cloudflare_temp_email",
                "mail_service_id": "cf-1",
                "status": "pending",
                "disabled": False,
                "auth_last_error": None,
                "managed_by_autoteam": True,
                "chatgpt_account_id": "acc-team",
                "mail_account_id": "addr-1",
                "cloudmail_account_id": None,
            },
        )
    ]


def test_create_new_invited_account_creates_cfmail_invite_and_registers(monkeypatch):
    fake_chatgpt = _FakeChatGPT()
    fake_mail = _FakeCfMail()
    team_members = [
        {"email": "old@example.com", "id": "u-old", "seat_type": "default"},
    ]
    added = []

    monkeypatch.delenv("SWAP_SEAT_WHITELIST_EMAILS", raising=False)
    monkeypatch.setattr(manager, "_fetch_team_members_for_account", lambda _chatgpt, account_id=None: team_members)
    monkeypatch.setattr(manager, "_fetch_team_state_for_account", lambda _chatgpt, account_id=None: (team_members, []))
    monkeypatch.setattr(manager, "load_accounts", lambda: [])
    monkeypatch.setattr(
        manager,
        "add_account",
        lambda email, password, **kwargs: added.append((email, password, kwargs)),
    )
    monkeypatch.setattr(manager, "update_account", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        manager, "_complete_registration", lambda email, password, invite_link, mail_client, **_kwargs: email
    )

    result = manager.create_new_invited_account(fake_chatgpt, fake_mail)

    assert result == "alex1234@rand.example.com"
    assert fake_chatgpt.seat_calls == []
    assert fake_chatgpt.invite_calls == [("alex1234@rand.example.com", "usage_based")]
    assert fake_mail.create_calls[0]["enable_random_subdomain"] is True
    assert fake_mail.create_calls[0]["prefix"][-4:].isdigit()
    assert added and added[0][0] == "alex1234@rand.example.com"
    assert added[0][2]["mail_account_id"] == "addr-new"
    assert fake_chatgpt.stopped is True


def test_create_new_invited_account_records_team_account_id(monkeypatch):
    fake_chatgpt = _FakeChatGPT()
    fake_mail = _FakeCfMail()
    added = []
    updates = []
    registration_contexts = []

    class _Team:
        account_id = "acc-team-b"
        workspace_name = "Team B"
        label = "Team B"
        invite_domains = "pool-a.example.com; *.pool-b.example.com"

    team = _Team()

    monkeypatch.setattr(
        manager,
        "_fetch_team_members_for_account",
        lambda _chatgpt, account_id=None: [{"email": "old@example.com", "id": "u-old", "seat_type": "usage_based"}],
    )
    monkeypatch.setattr(
        manager,
        "_fetch_team_state_for_account",
        lambda _chatgpt, account_id=None: (
            [{"email": "old@example.com", "id": "u-old", "seat_type": "usage_based"}],
            [],
        ),
    )
    monkeypatch.setattr(manager, "load_accounts", lambda: [])
    monkeypatch.setattr(
        manager,
        "add_account",
        lambda email, password, **kwargs: added.append((email, password, kwargs)),
    )
    monkeypatch.setattr(manager, "update_account", lambda email, **kwargs: updates.append((email, kwargs)))

    def fake_complete_registration(email, password, invite_link, mail_client, **kwargs):
        registration_contexts.append(kwargs.get("team_context"))
        return email

    monkeypatch.setattr(manager, "_complete_registration", fake_complete_registration)

    result = manager.create_new_invited_account(fake_chatgpt, fake_mail, team_context=team)

    assert result == "alex1234@rand.example.com"
    assert added and added[0][0] == "alex1234@rand.example.com"
    assert updates[0][0] == "alex1234@rand.example.com"
    assert updates[0][1]["chatgpt_account_id"] == "acc-team-b"
    assert fake_mail.create_calls[0]["domain"] == "pool-a.example.com; *.pool-b.example.com"
    assert fake_mail.create_calls[0]["enable_random_subdomain"] is None
    assert registration_contexts == [team]


def test_create_new_invited_account_retries_self_managed_pending_before_new_invite(monkeypatch):
    fake_chatgpt = _FakeChatGPT()
    fake_mail = _RetryCfMail()
    updates = []
    team_members = [{"email": "old@example.com", "id": "u-old", "seat_type": "usage_based"}]
    invites = [{"id": "inv-retry", "email_address": "retry@example.com", "seat_type": "usage_based"}]

    monkeypatch.setattr(manager, "_fetch_team_members_for_account", lambda _chatgpt, account_id=None: team_members)
    monkeypatch.setattr(
        manager, "_fetch_team_state_for_account", lambda _chatgpt, account_id=None: (team_members, invites)
    )
    monkeypatch.setattr(
        manager,
        "load_accounts",
        lambda: [
            {
                "email": "retry@example.com",
                "password": "old-password",
                "status": "auth_pending",
                "auth_file": None,
                "auth_last_error": "google_signin_redirect",
                "mail_provider": "cloudflare_temp_email",
                "mail_account_id": "addr-retry",
                "managed_by_autoteam": True,
            }
        ],
    )
    monkeypatch.setattr(manager, "update_account", lambda email, **kwargs: updates.append((email, kwargs)))
    monkeypatch.setattr(
        manager, "_complete_registration", lambda email, password, invite_link, mail_client, **_kwargs: email
    )

    result = manager.create_new_invited_account(fake_chatgpt, fake_mail)

    assert result == "retry@example.com"
    assert fake_chatgpt.invite_calls == []
    assert fake_mail.create_calls == []
    assert updates[0][0] == "retry@example.com"
    assert updates[0][1]["status"] == "pending"
    assert updates[0][1]["auth_last_error"] is None


def test_create_new_invited_account_keeps_failed_registration_pending(monkeypatch):
    fake_chatgpt = _FakeChatGPT()
    fake_mail = _FakeCfMail()
    updates = []
    team_members = [{"email": "old@example.com", "id": "u-old", "seat_type": "usage_based"}]

    monkeypatch.setattr(manager, "_fetch_team_members_for_account", lambda _chatgpt, account_id=None: team_members)
    monkeypatch.setattr(manager, "_fetch_team_state_for_account", lambda _chatgpt, account_id=None: (team_members, []))
    monkeypatch.setattr(manager, "load_accounts", lambda: [])
    monkeypatch.setattr(manager, "add_account", lambda *args, **kwargs: None)
    monkeypatch.setattr(manager, "update_account", lambda email, **kwargs: updates.append((email, kwargs)))
    monkeypatch.setattr(manager, "_complete_registration", lambda *args, **kwargs: None)

    result = manager.create_new_invited_account(fake_chatgpt, fake_mail)

    assert result is None
    assert fake_chatgpt.invite_calls == [("alex1234@rand.example.com", "usage_based")]
    assert updates[-1][0] == "alex1234@rand.example.com"
    assert updates[-1][1]["status"] == "pending"
    assert updates[-1][1]["auth_last_error"] == "registration_failed"


def test_create_new_invited_account_skips_when_chatgpt_target_already_met(monkeypatch):
    fake_chatgpt = _FakeChatGPT()
    fake_mail = _FakeCfMail()
    team_members = [
        {"email": "one@example.com", "id": "u-one", "seat_type": "default"},
        {"email": "two@example.com", "id": "u-two", "seat_type": "default"},
        {"email": "codex@example.com", "id": "u-codex", "seat_type": "usage_based"},
    ]

    monkeypatch.setattr(manager, "_fetch_team_members_for_account", lambda _chatgpt, account_id=None: team_members)

    result = manager.create_new_invited_account(fake_chatgpt, fake_mail, max_chatgpt_active=2)

    assert result is None
    assert fake_chatgpt.seat_calls == []
    assert fake_chatgpt.invite_calls == []
    assert fake_mail.create_calls == []


def test_cmd_invite_add_requires_explicit_force():
    try:
        manager.cmd_invite_add()
        raise AssertionError("expected force guard")
    except RuntimeError as exc:
        assert "force" in str(exc)


def test_cmd_invite_add_creates_invite_and_activates_new_account_when_forced(monkeypatch):
    fake_chatgpt = _FakeChatGPT()
    created = []

    monkeypatch.setattr(manager, "ChatGPTTeamAPI", lambda: fake_chatgpt)
    monkeypatch.setattr(manager, "get_mail_client", lambda **kwargs: _FakeCfMail())
    monkeypatch.setattr(
        manager,
        "create_new_invited_account",
        lambda chatgpt, mail_client, **_kwargs: (
            created.append((chatgpt.started, mail_client.provider_name)) or "new@example.com"
        ),
    )
    monkeypatch.setattr(
        manager, "_activate_registered_account", lambda chatgpt, email, **_kwargs: {"ok": True, "email": email}
    )
    monkeypatch.setattr(manager, "_post_registration_seat_rebalance", lambda chatgpt, **_kwargs: {"mode": "swap_seat"})

    result = manager.cmd_invite_add(force_create_invite=True)

    assert result["mode"] == "create_invite"
    assert result["invited"] is True
    assert result["email"] == "new@example.com"
    assert result["activation"] == {"ok": True, "email": "new@example.com"}
    assert result["rebalance"] == {"mode": "swap_seat"}
    assert created == [(True, "cloudflare_temp_email")]
    assert fake_chatgpt.allow_team_invites is False


def test_cmd_invite_add_passes_invite_domains_to_create_new_invited_account(monkeypatch):
    fake_chatgpt = _FakeChatGPT()
    calls = []

    monkeypatch.setattr(manager, "ChatGPTTeamAPI", lambda: fake_chatgpt)
    monkeypatch.setattr(manager, "get_mail_client", lambda **kwargs: _FakeCfMail())
    monkeypatch.setattr(
        manager,
        "create_new_invited_account",
        lambda chatgpt, mail_client, **kwargs: calls.append(kwargs) or None,
    )

    result = manager.cmd_invite_add(
        force_create_invite=True,
        invite_domains="pool-a.example.com; *.pool-b.example.com",
    )

    assert result["mode"] == "create_invite"
    assert result["invited"] is False
    assert result["invite_domains"] == "pool-a.example.com; *.pool-b.example.com"
    assert calls == [
        {
            "max_chatgpt_active": 2,
            "team_context": None,
            "invite_domains": "pool-a.example.com; *.pool-b.example.com",
        }
    ]


def test_cmd_clear_pending_invites_deletes_by_email(monkeypatch):
    class _InviteChatGPT(_FakeChatGPT):
        def __init__(self):
            super().__init__()
            self.delete_calls = []

        def list_invites(self):
            return [
                {"email_address": "one@example.com", "id": "inv-1", "status": 2},
                {"email_address": "two@example.com", "id": "inv-2", "status": "pending"},
                {"email_address": "joined@example.com", "id": "inv-accepted", "status": "accepted"},
                {"email_address": "one@example.com", "id": "inv-dup", "status": 2},
            ]

        def cancel_invite(self, email):
            self.delete_calls.append(email)
            return 200, {"success": True}

    fake_chatgpt = _InviteChatGPT()
    monkeypatch.setattr(manager, "ChatGPTTeamAPI", lambda: fake_chatgpt)

    result = manager.cmd_clear_pending_invites(concurrency=1)

    assert result["mode"] == "clear_pending_invites"
    assert result["summary"] == {"scanned": 4, "deleted": 2, "failed": 0}
    assert fake_chatgpt.delete_calls == ["one@example.com", "two@example.com"]


def test_cmd_bulk_invite_sends_deduped_batches(monkeypatch):
    class _InviteChatGPT(_FakeChatGPT):
        def __init__(self):
            super().__init__()
            self.batch_calls = []

        def invite_members(self, emails, seat_type="usage_based", *, resend_emails=True):
            self.batch_calls.append((list(emails), seat_type, resend_emails))
            return 200, {"account_invites": [{"email_address": email} for email in emails], "errored_emails": []}

    fake_chatgpt = _InviteChatGPT()
    monkeypatch.setattr(manager, "ChatGPTTeamAPI", lambda: fake_chatgpt)

    result = manager.cmd_bulk_invite(
        "one@example.com two@example.com one@example.com",
        concurrency=1,
        batch_size=1,
    )

    assert result["mode"] == "bulk_invite"
    assert result["summary"] == {
        "requested": 2,
        "sent": 2,
        "failed": 0,
        "invalid": 0,
        "batches": 2,
        "verified_after_error": 0,
    }
    assert fake_chatgpt.batch_calls == [
        (["one@example.com"], "usage_based", True),
        (["two@example.com"], "usage_based", True),
    ]


def test_cmd_bulk_invite_reconciles_timeout_when_pending_exists(monkeypatch):
    class _InviteChatGPT(_FakeChatGPT):
        def __init__(self):
            super().__init__()
            self.batch_calls = []

        def invite_members(self, emails, seat_type="usage_based", *, resend_emails=True):
            self.batch_calls.append((list(emails), seat_type, resend_emails))
            raise RuntimeError("timeout")

        def list_invites(self):
            return [
                {"email_address": "one@example.com", "status": 2, "seat_type": "usage_based"},
                {"email_address": "two@example.com", "status": 2, "seat_type": "usage_based"},
            ]

    fake_chatgpt = _InviteChatGPT()
    monkeypatch.setattr(manager, "ChatGPTTeamAPI", lambda: fake_chatgpt)

    result = manager.cmd_bulk_invite("one@example.com two@example.com", concurrency=1, batch_size=2)

    assert result["summary"] == {
        "requested": 2,
        "sent": 2,
        "failed": 0,
        "invalid": 0,
        "batches": 1,
        "verified_after_error": 2,
    }
    assert result["verified_after_error"] == ["one@example.com", "two@example.com"]
    assert result["failed"] == []


def test_activate_registered_account_promotes_new_member_without_disabling_old_oauth(monkeypatch):
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
    monkeypatch.setattr(manager, "get_managed_cpa_auth_names", lambda: {"old.json", "new.json"})
    monkeypatch.setattr(
        manager,
        "set_cpa_auth_disabled",
        lambda auth, disabled: (
            oauth_updates.append(((auth.get("name") if isinstance(auth, dict) else auth), disabled)) or {"status": "ok"}
        ),
    )
    monkeypatch.setattr(manager, "update_account", lambda email, **kwargs: updates.append((email, kwargs)))

    result = manager._activate_registered_account(fake_chatgpt, "new@example.com")

    assert result["ok"] is True
    assert fake_chatgpt.seat_calls == [("u-new", "default")]
    assert oauth_updates == [("new.json", False)]
    assert updates and updates[0][0] == "new@example.com"
    assert updates[0][1]["status"] == "active"
    assert updates[0][1]["seat_type"] == "chatgpt"


def test_activate_registered_account_does_not_demote_chatgpt_member_when_whitelist_limit_blocks(monkeypatch):
    fake_chatgpt = _FakeChatGPT()
    target_member = {"email": "new@example.com", "id": "u-new", "seat_type": "default"}
    members = [
        {"email": "vip@example.com", "id": "u-vip", "seat_type": "default"},
        target_member,
    ]

    monkeypatch.setenv("SWAP_SEAT_WHITELIST_EMAILS", "vip@example.com")
    monkeypatch.setattr(manager, "_wait_for_team_member", lambda *_args, **_kwargs: target_member)
    monkeypatch.setattr(manager, "_fetch_team_state_for_account", lambda *_args, **_kwargs: (members, []))

    result = manager._activate_registered_account(fake_chatgpt, "new@example.com", max_chatgpt_active=1)

    assert result["ok"] is False
    assert result["reason"] == "whitelist_chatgpt_limit"
    assert fake_chatgpt.seat_calls == []


def test_activate_registered_account_marks_auth_pending_without_demoting_when_cpa_auth_missing(monkeypatch):
    fake_chatgpt = _FakeChatGPT()
    updates = []
    target_member = {"email": "new@example.com", "id": "u-new", "seat_type": "default"}

    monkeypatch.delenv("SWAP_SEAT_WHITELIST_EMAILS", raising=False)
    monkeypatch.setattr(manager, "_wait_for_team_member", lambda *_args, **_kwargs: target_member)
    monkeypatch.setattr(manager, "_fetch_team_state_for_account", lambda *_args, **_kwargs: ([target_member], []))
    monkeypatch.setattr(manager, "get_managed_cpa_auth_names", lambda: {"new.json"})
    monkeypatch.setattr(manager, "_wait_for_cpa_auth", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(manager, "update_account", lambda email, **kwargs: updates.append((email, kwargs)))

    result = manager._activate_registered_account(fake_chatgpt, "new@example.com")

    assert result["ok"] is False
    assert result["reason"] == "cpa_auth_missing"
    assert fake_chatgpt.seat_calls == []
    assert updates == [
        (
            "new@example.com",
            {
                "status": "auth_pending",
                "seat_type": "chatgpt",
                "disabled": False,
                "auth_last_error": "cpa_auth_missing_after_registration",
                "last_active_at": updates[0][1]["last_active_at"],
            },
        )
    ]


def test_activate_registered_account_requires_user_id_before_marking_active(monkeypatch):
    fake_chatgpt = _FakeChatGPT()
    updates = []
    oauth_updates = []
    target_member = {"email": "new@example.com", "seat_type": "usage_based"}
    auth = {"name": "new.json", "provider": "codex", "email": "new@example.com", "status": "disabled", "disabled": True}

    monkeypatch.delenv("SWAP_SEAT_WHITELIST_EMAILS", raising=False)
    monkeypatch.setattr(manager, "_wait_for_team_member", lambda *_args, **_kwargs: target_member)
    monkeypatch.setattr(manager, "_fetch_team_state_for_account", lambda *_args, **_kwargs: ([target_member], []))
    monkeypatch.setattr(manager, "get_managed_cpa_auth_names", lambda: {"new.json"})
    monkeypatch.setattr(manager, "_wait_for_cpa_auth", lambda *_args, **_kwargs: auth)
    monkeypatch.setattr(manager, "list_cpa_files", lambda: [auth])
    monkeypatch.setattr(
        manager,
        "set_cpa_auth_disabled",
        lambda auth_entry, disabled: oauth_updates.append((auth_entry["name"], disabled)) or {"status": "ok"},
    )
    monkeypatch.setattr(manager, "update_account", lambda email, **kwargs: updates.append((email, kwargs)))

    result = manager._activate_registered_account(fake_chatgpt, "new@example.com")

    assert result["ok"] is False
    assert result["reason"] == "partial_failure"
    assert result["seat_result"]["result"] == "skipped"
    assert result["seat_result"]["error"] == "missing user_id"
    assert fake_chatgpt.seat_calls == []
    assert oauth_updates == []
    assert updates[0][1]["status"] == "auth_pending"
    assert updates[0][1]["seat_type"] == "codex"
    assert updates[0][1]["auth_last_error"] == "post_registration_activation_failed"


def test_activate_registered_account_rejects_cpa_auth_without_identifier(monkeypatch):
    fake_chatgpt = _FakeChatGPT()
    updates = []
    target_member = {"email": "new@example.com", "id": "u-new", "seat_type": "usage_based"}
    auth = {"provider": "codex", "email": "new@example.com", "status": "disabled", "disabled": True}

    monkeypatch.delenv("SWAP_SEAT_WHITELIST_EMAILS", raising=False)
    monkeypatch.setattr(manager, "_wait_for_team_member", lambda *_args, **_kwargs: target_member)
    monkeypatch.setattr(manager, "_fetch_team_state_for_account", lambda *_args, **_kwargs: ([target_member], []))
    monkeypatch.setattr(manager, "get_managed_cpa_auth_names", lambda: {"new.json"})
    monkeypatch.setattr(manager, "_wait_for_cpa_auth", lambda *_args, **_kwargs: auth)
    monkeypatch.setattr(
        manager,
        "list_cpa_files",
        lambda: (_ for _ in ()).throw(AssertionError("auth without identifier should fail before CPA list refresh")),
    )
    monkeypatch.setattr(
        manager,
        "set_cpa_auth_disabled",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("auth without identifier cannot be enabled")),
    )
    monkeypatch.setattr(manager, "update_account", lambda email, **kwargs: updates.append((email, kwargs)))

    result = manager._activate_registered_account(fake_chatgpt, "new@example.com")

    assert result["ok"] is False
    assert result["reason"] == "cpa_auth_missing_identifier"
    assert fake_chatgpt.seat_calls == []
    assert updates[0][1]["status"] == "auth_pending"
    assert updates[0][1]["seat_type"] == "codex"
    assert updates[0][1]["auth_last_error"] == "cpa_auth_missing_identifier_after_registration"


def test_activate_registered_account_uses_observed_target_auth_when_latest_list_is_stale(monkeypatch):
    fake_chatgpt = _FakeChatGPT()
    updates = []
    oauth_updates = []
    target_member = {"email": "new@example.com", "id": "u-new", "seat_type": "usage_based"}
    auth = {"name": "new.json", "provider": "codex", "email": "new@example.com", "status": "disabled", "disabled": True}

    monkeypatch.delenv("SWAP_SEAT_WHITELIST_EMAILS", raising=False)
    monkeypatch.setattr(manager, "_wait_for_team_member", lambda *_args, **_kwargs: target_member)
    monkeypatch.setattr(manager, "_fetch_team_state_for_account", lambda *_args, **_kwargs: ([target_member], []))
    monkeypatch.setattr(manager, "get_managed_cpa_auth_names", lambda: {"new.json"})
    monkeypatch.setattr(manager, "_wait_for_cpa_auth", lambda *_args, **_kwargs: auth)
    monkeypatch.setattr(manager, "list_cpa_files", lambda: [])
    monkeypatch.setattr(
        manager,
        "set_cpa_auth_disabled",
        lambda auth_entry, disabled: oauth_updates.append((auth_entry["name"], disabled)) or {"status": "ok"},
    )
    monkeypatch.setattr(manager, "update_account", lambda email, **kwargs: updates.append((email, kwargs)))

    result = manager._activate_registered_account(fake_chatgpt, "new@example.com")

    assert result["ok"] is True
    assert result["reason"] == "activated"
    assert fake_chatgpt.seat_calls == [("u-new", "default")]
    assert oauth_updates == [("new.json", False)]
    assert updates[0][1]["status"] == "active"
    assert updates[0][1]["seat_type"] == "chatgpt"


def test_activate_registered_account_preserves_chatgpt_seat_when_oauth_enable_fails(monkeypatch):
    fake_chatgpt = _FakeChatGPT()
    updates = []
    target_member = {"email": "new@example.com", "id": "u-new", "seat_type": "default"}
    auth = {"name": "new.json", "provider": "codex", "email": "new@example.com", "status": "disabled", "disabled": True}

    monkeypatch.delenv("SWAP_SEAT_WHITELIST_EMAILS", raising=False)
    monkeypatch.setattr(manager, "_wait_for_team_member", lambda *_args, **_kwargs: target_member)
    monkeypatch.setattr(manager, "_fetch_team_state_for_account", lambda *_args, **_kwargs: ([target_member], []))
    monkeypatch.setattr(manager, "get_managed_cpa_auth_names", lambda: {"new.json"})
    monkeypatch.setattr(manager, "_wait_for_cpa_auth", lambda *_args, **_kwargs: auth)
    monkeypatch.setattr(manager, "list_cpa_files", lambda: [auth])
    monkeypatch.setattr(
        manager,
        "set_cpa_auth_disabled",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("CPA down")),
    )
    monkeypatch.setattr(manager, "update_account", lambda email, **kwargs: updates.append((email, kwargs)))

    result = manager._activate_registered_account(fake_chatgpt, "new@example.com")

    assert result["ok"] is False
    assert result["reason"] == "partial_failure"
    assert result["seat_result"]["result"] == "unchanged"
    assert fake_chatgpt.seat_calls == []
    assert updates[0][1]["status"] == "auth_pending"
    assert updates[0][1]["seat_type"] == "chatgpt"
    assert updates[0][1]["auth_last_error"] == "post_registration_activation_failed"


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
        lambda max_chatgpt_active=2, pending_invite_email=None, team_context=None, repair_before_replace=True, replace_mode="pending_invite": (
            calls.append((team_context.account_id, max_chatgpt_active, repair_before_replace, replace_mode))
            or {"mode": "auto_detect_replace", "team": team_context.label}
        ),
    )

    result = manager.cmd_manage_teams(max_chatgpt_active=2, replace_with_pending_invite=True)

    assert result["mode"] == "multi_team_manage"
    assert result["teams_total"] == 2
    assert result["teams_ok"] == 2
    assert calls == [("acc-a", 1, True, "pending_invite"), ("acc-b", 2, True, "pending_invite")]


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
            return {
                "account_id": self.account_id,
                "label": self.label,
                "pending_invite_email": self.pending_invite_email,
            }

    teams = [
        _Team("acc-a", "Team A", "pending-a@example.com"),
        _Team("acc-b", "Team B", "pending-b@example.com"),
    ]
    calls = []

    monkeypatch.setattr("autoteam.team_context.get_team_contexts", lambda default_max_chatgpt_active=2: teams)
    monkeypatch.setattr(
        manager,
        "cmd_auto_detect_replace",
        lambda max_chatgpt_active=2, pending_invite_email=None, team_context=None, repair_before_replace=True, replace_mode="pending_invite": (
            calls.append(
                (team_context.account_id, pending_invite_email, max_chatgpt_active, repair_before_replace, replace_mode)
            )
            or {"mode": "auto_detect_replace", "team": team_context.label}
        ),
    )

    result = manager.cmd_manage_teams(max_chatgpt_active=2, replace_with_pending_invite=True)

    assert result["teams_ok"] == 2
    assert calls == [
        ("acc-a", "pending-a@example.com", 2, True, "pending_invite"),
        ("acc-b", "pending-b@example.com", 2, True, "pending_invite"),
    ]


def test_cmd_manage_teams_can_skip_duplicate_pat_repair(monkeypatch):
    class _Team:
        id = "acc-a"
        account_id = "acc-a"
        workspace_name = "Team A"
        label = "Team A"
        max_chatgpt_active = 2
        pending_invite_email = ""

        def public_dict(self):
            return {"account_id": self.account_id, "label": self.label}

    calls = []
    monkeypatch.setattr("autoteam.team_context.get_team_contexts", lambda default_max_chatgpt_active=2: [_Team()])
    monkeypatch.setattr(
        manager,
        "cmd_auto_detect_replace",
        lambda max_chatgpt_active=2, pending_invite_email=None, team_context=None, repair_before_replace=True, replace_mode="pending_invite": (
            calls.append((repair_before_replace, replace_mode)) or {"mode": "auto_detect_replace"}
        ),
    )

    result = manager.cmd_manage_teams(
        max_chatgpt_active=2,
        replace_with_pending_invite=True,
        pat_repair_already_run=True,
        replace_mode="create_invite",
    )

    assert result["teams_ok"] == 1
    assert result["replace_mode"] == "create_invite"
    assert calls == [(False, "create_invite")]


def test_cmd_auto_detect_replace_consumes_pending_invite_when_team_has_no_chatgpt_seat(monkeypatch):
    add_calls = []

    monkeypatch.setattr(
        manager,
        "cmd_swap_seats",
        lambda max_chatgpt_active=2, team_context=None: {
            "mode": "swap_seat",
            "skipped": True,
            "reason": "no_changes_needed",
            "summary": {
                "current_chatgpt_seats": 0,
                "selected_chatgpt": 0,
            },
        },
    )
    monkeypatch.setattr(
        manager,
        "cmd_add",
        lambda pending_invite_email=None, max_chatgpt_active=2, team_context=None: (
            add_calls.append((pending_invite_email, max_chatgpt_active, team_context))
            or {"mode": "consume_pending_invite", "invited": True, "reason": "pending_invite_registered"}
        ),
    )
    monkeypatch.setattr(
        manager,
        "cmd_repair_pat_auths",
        lambda **_kwargs: {"mode": "repair_pat_auths", "summary": {"repaired": 0, "failed": 0}},
    )

    result = manager.cmd_auto_detect_replace(max_chatgpt_active=2, pending_invite_email="pending@example.com")

    assert result["replaced"] is True
    assert result["trigger"] == "no_current_chatgpt_seat"
    assert add_calls == [("pending@example.com", 2, None)]


def test_cmd_auto_detect_replace_consumes_pending_invite_when_below_target(monkeypatch):
    add_calls = []

    monkeypatch.setattr(
        manager,
        "cmd_swap_seats",
        lambda max_chatgpt_active=2, team_context=None: {
            "mode": "swap_seat",
            "summary": {
                "current_chatgpt_seats": 2,
                "selected_chatgpt": 1,
                "quota_available": 1,
            },
        },
    )
    monkeypatch.setattr(
        manager,
        "cmd_add",
        lambda pending_invite_email=None, max_chatgpt_active=2, team_context=None: (
            add_calls.append((pending_invite_email, max_chatgpt_active, team_context))
            or {"mode": "consume_pending_invite", "invited": True, "reason": "pending_invite_registered"}
        ),
    )
    monkeypatch.setattr(
        manager,
        "cmd_repair_pat_auths",
        lambda **_kwargs: {"mode": "repair_pat_auths", "summary": {"repaired": 0, "failed": 0}},
    )

    result = manager.cmd_auto_detect_replace(max_chatgpt_active=2, pending_invite_email="pending@example.com")

    assert result["replaced"] is True
    assert result["trigger"] == "below_target_chatgpt_seats"
    assert result["replace_mode"] == "pending_invite"
    assert add_calls == [("pending@example.com", 2, None)]


def test_cmd_auto_detect_replace_can_create_invite_when_below_target(monkeypatch):
    invite_calls = []

    monkeypatch.setattr(
        manager,
        "cmd_swap_seats",
        lambda max_chatgpt_active=2, team_context=None: {
            "mode": "swap_seat",
            "summary": {
                "current_chatgpt_seats": 1,
                "selected_chatgpt": 1,
                "quota_available": 1,
            },
        },
    )
    monkeypatch.setattr(
        manager,
        "cmd_invite_add",
        lambda max_chatgpt_active=2, team_context=None, force_create_invite=False: (
            invite_calls.append((max_chatgpt_active, team_context, force_create_invite))
            or {"mode": "create_invite", "invited": True, "reason": "new_invite_registered"}
        ),
    )
    monkeypatch.setattr(
        manager,
        "cmd_add",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("pending invite should not be consumed in create_invite mode")
        ),
    )
    monkeypatch.setattr(
        manager,
        "cmd_repair_pat_auths",
        lambda **_kwargs: {"mode": "repair_pat_auths", "summary": {"repaired": 0, "failed": 0}},
    )

    result = manager.cmd_auto_detect_replace(max_chatgpt_active=2, replace_mode="create_invite")

    assert result["replaced"] is True
    assert result["trigger"] == "below_target_chatgpt_seats"
    assert result["replace_mode"] == "create_invite"
    assert invite_calls == [(2, None, True)]


def test_cmd_auto_detect_replace_repairs_pat_before_consuming_pending_invite(monkeypatch):
    swap_results = [
        {
            "mode": "swap_seat",
            "skipped": True,
            "reason": "no_quota_available",
            "summary": {"current_chatgpt_seats": 0, "selected_chatgpt": 0, "quota_available": 0},
        },
        {
            "mode": "swap_seat",
            "summary": {"current_chatgpt_seats": 2, "selected_chatgpt": 2, "quota_available": 2},
        },
    ]

    monkeypatch.setattr(manager, "cmd_swap_seats", lambda **_kwargs: swap_results.pop(0))
    monkeypatch.setattr(
        manager,
        "cmd_repair_pat_auths",
        lambda **_kwargs: {"mode": "repair_pat_auths", "summary": {"repaired": 1, "failed": 0}},
    )
    monkeypatch.setattr(
        manager,
        "cmd_add",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("pending invite should not be consumed after repair")),
    )

    result = manager.cmd_auto_detect_replace(max_chatgpt_active=2, pending_invite_email="pending@example.com")

    assert result["replaced"] is False
    assert result["trigger"] is None
    assert result["repair_result"]["summary"]["repaired"] == 1
    assert swap_results == []


def test_cmd_auto_detect_replace_defers_replacement_when_recheck_fails_after_pat_repair(monkeypatch):
    swap_results = [
        {
            "mode": "swap_seat",
            "skipped": True,
            "reason": "no_quota_available",
            "summary": {"current_chatgpt_seats": 0, "selected_chatgpt": 0, "quota_available": 0},
        },
        RuntimeError("swap cooldown"),
    ]

    def fake_swap(**_kwargs):
        result = swap_results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(manager, "cmd_swap_seats", fake_swap)
    monkeypatch.setattr(
        manager,
        "cmd_repair_pat_auths",
        lambda **_kwargs: {"mode": "repair_pat_auths", "summary": {"repaired": 1, "failed": 0}},
    )
    monkeypatch.setattr(
        manager,
        "cmd_add",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("pending invite should not be consumed after repaired PAT")
        ),
    )
    monkeypatch.setattr(
        manager,
        "cmd_invite_add",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("new invite should not be created after repaired PAT")),
    )

    result = manager.cmd_auto_detect_replace(max_chatgpt_active=2, pending_invite_email="pending@example.com")

    assert result["replaced"] is False
    assert result["reason"] == "pat_repair_recheck_failed"
    assert result["repair_result"]["summary"]["repaired"] == 1
    assert result["error"] == "swap cooldown"
    assert swap_results == []


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
