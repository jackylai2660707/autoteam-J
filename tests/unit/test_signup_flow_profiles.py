import os

os.environ.setdefault("DISPLAY", ":99")

from autoteam import codex_auth, invite, manager
from autoteam.signup_profile import SignupProfile


def test_verification_email_marker_skips_invites_and_old_codes():
    class _Mail:
        def search_emails_by_recipient(self, _email, size=20):
            return [
                {
                    "emailId": "12",
                    "sendEmail": "noreply@openai.com",
                    "subject": "Your temporary ChatGPT verification code",
                },
                {"emailId": "13", "sendEmail": "noreply@openai.com", "subject": "Kemmy has invited you to use Codex"},
                {
                    "emailId": "9",
                    "sendEmail": "noreply@openai.com",
                    "subject": "Your temporary ChatGPT verification code",
                },
            ]

    baseline = invite._latest_verification_email_marker(_Mail(), "user@example.com")

    assert baseline == 12
    assert invite._is_new_verification_email({"emailId": "12"}, baseline) is False
    assert invite._is_new_verification_email({"emailId": "14"}, baseline) is True


def test_verification_email_marker_uses_created_at_without_numeric_id():
    class _Mail:
        def search_emails_by_recipient(self, _email, size=20):
            return [
                {
                    "created_at": "2026-06-30 16:22:38",
                    "sendEmail": "noreply@openai.com",
                    "subject": "Your temporary ChatGPT verification code",
                }
            ]

    baseline = invite._latest_verification_email_marker(_Mail(), "user@example.com")

    assert baseline > 0
    assert invite._is_new_verification_email({"created_at": "2026-06-30 16:22:38"}, baseline) is False
    assert invite._is_new_verification_email({"created_at": "2026-06-30 16:22:39"}, baseline) is True


class _NullElement:
    def __init__(self):
        self.clicked = False
        self.filled = None
        self.typed = []

    def is_visible(self, timeout=0):
        return False

    def is_editable(self, timeout=0):
        return False

    def fill(self, value):
        self.filled = value

    def click(self, timeout=0, force=False):
        self.clicked = True


class _FakeElement(_NullElement):
    def __init__(self, page=None, *, visible=True, editable=True):
        super().__init__()
        self.page = page
        self.visible = visible
        self.editable = editable

    def is_visible(self, timeout=0):
        return self.visible

    def is_editable(self, timeout=0):
        return self.editable

    def click(self, timeout=0, force=False):
        self.clicked = True
        if self.page is not None:
            self.page.active_element = self


class _FakeLocatorGroup:
    def __init__(self, items=None, text=None):
        self._items = list(items or [])
        self._text = text

    @property
    def first(self):
        if self._items:
            return self._items[0]
        return _NullElement()

    def all(self):
        return list(self._items)

    def nth(self, index):
        return self._items[index]

    def click(self, timeout=0, force=False):
        return self.first.click(timeout=timeout, force=force)

    def inner_text(self, timeout=0):
        if self._text is None:
            raise AssertionError("unexpected inner_text call")
        return self._text


class _FakeKeyboard:
    def __init__(self, page):
        self.page = page

    def press(self, key):
        if key == "ControlOrMeta+A" and self.page.active_element is not None:
            self.page.active_element.typed.clear()

    def type(self, value, delay=0):
        if self.page.active_element is not None:
            self.page.active_element.typed.append(value)


class _FakePage:
    def __init__(
        self, *, url="https://auth.openai.com/about-you", name_input=None, age_input=None, spinbuttons=None, meta=None
    ):
        self.url = url
        self.active_element = None
        self.keyboard = _FakeKeyboard(self)
        self.name_input = name_input
        self.age_input = age_input
        self.spinbuttons = list(spinbuttons or [])
        self.meta = list(meta or [])
        self.submit_button = _FakeElement(self, visible=True, editable=False)
        self.date_label = _FakeElement(self, visible=True, editable=False)

        for element in self.spinbuttons:
            element.page = self
        if self.name_input is not None:
            self.name_input.page = self
        if self.age_input is not None:
            self.age_input.page = self

    def locator(self, selector):
        if selector == '[role="spinbutton"]':
            return _FakeLocatorGroup(self.spinbuttons)
        if selector in {"text=生日日期", "text=Date of birth"}:
            return _FakeLocatorGroup([self.date_label])
        if "button" in selector:
            return _FakeLocatorGroup([self.submit_button])
        if 'input[name="name"]' in selector or 'placeholder*="name"' in selector or 'placeholder*="全名"' in selector:
            return _FakeLocatorGroup([self.name_input] if self.name_input is not None else [])
        if 'input[name="age"]' in selector or 'placeholder*="年龄"' in selector or 'placeholder*="Age"' in selector:
            return _FakeLocatorGroup([self.age_input] if self.age_input is not None else [])
        return _FakeLocatorGroup([])

    def evaluate(self, _script):
        return list(self.meta)


class _FakeContext:
    def __init__(self, page):
        self.page = page

    def new_page(self):
        return self.page


class _FakeBrowser:
    def __init__(self, page):
        self.page = page
        self.closed = 0

    def new_context(self, **kwargs):
        return _FakeContext(self.page)

    def close(self):
        self.closed += 1


class _FakeChromium:
    def __init__(self, page):
        self.page = page

    def launch(self, **kwargs):
        return _FakeBrowser(self.page)


class _FakePlaywright:
    def __init__(self, page):
        self.chromium = _FakeChromium(page)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


class _FakeBrowserSession:
    def __init__(self, page):
        self.page = page
        self.closed = 0

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def close(self):
        self.closed += 1


class _DirectFlowPage:
    def __init__(self):
        self.url = "https://chatgpt.com/auth/login"
        self.active_element = None
        self.keyboard = _FakeKeyboard(self)
        self.email_input = _FakeElement(self, visible=True, editable=True)
        self.submit_button = _FakeElement(self, visible=True, editable=False)
        self._body = "Something went wrong"

    def goto(self, url, wait_until=None, timeout=None):
        self.url = url

    def content(self):
        return "<html></html>"

    def locator(self, selector):
        if selector == "body":
            return _FakeLocatorGroup(text=self._body)
        if selector == manager._DIRECT_EMAIL_SELECTORS:
            return _FakeLocatorGroup([self.email_input])
        if selector in {
            manager._DIRECT_PASSWORD_SELECTORS,
            manager._DIRECT_CODE_SELECTORS,
            'input[name="name"], [role="spinbutton"]',
            '[role="spinbutton"]',
        }:
            return _FakeLocatorGroup([])
        if "button" in selector:
            return _FakeLocatorGroup([self.submit_button])
        return _FakeLocatorGroup([])


class _GoogleSignInPage:
    def __init__(self):
        self.url = "https://chatgpt.com"
        self.keyboard = _FakeKeyboard(self)
        self.active_element = None
        self.screenshots = []
        self.body = "Sign in with Google Email or phone Forgot email? to continue to OpenAI"

    def goto(self, url, wait_until=None, timeout=None):
        self.url = "https://accounts.google.com/signin/oauth"

    def content(self):
        return f"<html><body>{self.body}</body></html>"

    def inner_text(self, selector, timeout=0):
        return self.body

    def screenshot(self, path=None, full_page=False):
        self.screenshots.append(path)

    def locator(self, selector):
        if selector == '#identifierId, input[name="identifier"]':
            return _FakeLocatorGroup([_FakeElement(self, visible=True, editable=True)])
        return _FakeLocatorGroup([])


class _TextButton(_FakeElement):
    def __init__(self, page=None, text="", on_click=None):
        super().__init__(page, visible=True, editable=False)
        self.text = text
        self.on_click = on_click

    def inner_text(self, timeout=0):
        return self.text

    def click(self, timeout=0, force=False):
        super().click(timeout=timeout, force=force)
        if self.on_click:
            self.on_click()


class _EmailChoicePage:
    def __init__(self):
        self.url = "https://chatgpt.com/auth/login"
        self.keyboard = _FakeKeyboard(self)
        self.active_element = None
        self.email_input = _FakeElement(self, visible=True, editable=True)
        self.password_input = _FakeElement(self, visible=False, editable=True)
        self.google_clicked = False
        self.submit_clicked = False
        self.google_button = _TextButton(self, "Continue with Google", self._click_google)
        self.submit_button = _TextButton(self, "Continue", self._click_submit)

    def _click_google(self):
        self.google_clicked = True
        self.url = "https://accounts.google.com/signin/oauth"

    def _click_submit(self):
        self.submit_clicked = True
        self.email_input.visible = False
        self.password_input.visible = True

    def content(self):
        return "<html><body>Log in to start chatting</body></html>"

    def inner_text(self, selector, timeout=0):
        return "Log in to start chatting"

    def screenshot(self, path=None, full_page=False):
        pass

    def locator(self, selector):
        if selector in {
            'input[name="email"]',
            'input[type="email"]:not([name="identifier"])',
            'input[placeholder*="email" i]',
            'input[id="email"]',
            "#email-input",
            'input[autocomplete="email"]',
            'input[autocomplete="username"]:not([name="identifier"])',
        }:
            return _FakeLocatorGroup([self.email_input] if self.email_input.visible else [])
        if selector in {'input[name="password"]', 'input[type="password"]', 'input[id="password"]'}:
            return _FakeLocatorGroup([self.password_input] if self.password_input.visible else [])
        if selector == 'button:has-text("Continue")':
            return _FakeLocatorGroup([self.google_button, self.submit_button])
        return _FakeLocatorGroup([])


class _NoMail:
    def __init__(self):
        self.search_calls = 0

    def search_emails_by_recipient(self, *args, **kwargs):
        self.search_calls += 1
        raise AssertionError("Google sign-in redirect should fail before waiting for mail")


def test_fill_about_you_birthday_by_meta_uses_profile_values(monkeypatch):
    monkeypatch.setattr(manager.time, "sleep", lambda *_args, **_kwargs: None)
    profile = SignupProfile("Ethan Carter", 1988, 7, 14, 37)
    spinbuttons = [_FakeElement(), _FakeElement(), _FakeElement()]
    page = _FakePage(
        spinbuttons=spinbuttons,
        meta=[
            {"index": 0, "ariaLabel": "Month", "ariaValueMax": "12"},
            {"index": 1, "ariaLabel": "Year", "ariaValueMax": "2100"},
            {"index": 2, "ariaLabel": "Day", "ariaValueMax": "31"},
        ],
    )

    assert manager._fill_about_you_birthday_by_meta(page, profile) is True
    assert spinbuttons[0].typed == ["07"]
    assert spinbuttons[1].typed == ["1988"]
    assert spinbuttons[2].typed == ["14"]


def test_complete_direct_about_you_age_branch_uses_profile_values(monkeypatch):
    monkeypatch.setattr(manager.time, "sleep", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(manager, "_wait_for_direct_register_step", lambda *args, **kwargs: "completed")
    profile = SignupProfile("Noah Bennett", 1992, 11, 3, 33)
    page = _FakePage(name_input=_FakeElement(), age_input=_FakeElement(), spinbuttons=[])

    assert manager._complete_direct_about_you(page, profile) is True
    assert page.name_input.filled == "Noah Bennett"
    assert page.age_input.filled == "33"
    assert page.submit_button.clicked is True


def test_detect_direct_register_step_recognizes_auth_error_page():
    page = _DirectFlowPage()
    page.url = "https://chatgpt.com/api/auth/error"

    assert manager._detect_direct_register_step(page) == "error"


def test_register_direct_once_fails_fast_when_email_step_hits_auth_error(monkeypatch):
    page = _DirectFlowPage()

    monkeypatch.setattr(manager.time, "sleep", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(manager, "_safe_invite_screenshot", lambda *args, **kwargs: None)
    monkeypatch.setattr(manager, "_page_excerpt", lambda *_args, **_kwargs: "Something went wrong")
    monkeypatch.setattr(
        manager,
        "_click_primary_auth_button",
        lambda page, field, labels: setattr(page, "url", "https://chatgpt.com/api/auth/error") or True,
    )
    monkeypatch.setattr(manager, "new_browser_session", lambda: _FakeBrowserSession(page))

    result = manager._register_direct_once(
        object(),
        "user@example.com",
        "pw",
        signup_profile=SignupProfile("Liam Parker", 1991, 9, 8, 34),
    )

    assert result is False


def test_create_account_direct_is_disabled_without_registering(monkeypatch):
    monkeypatch.setattr(
        manager,
        "_register_direct_once",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("direct registration must not run")),
    )

    try:
        manager.create_account_direct(object())
    except RuntimeError as exc:
        assert "swap_seat-only" in str(exc)
    else:
        raise AssertionError("expected create_account_direct to be disabled")


def test_register_with_invite_fails_fast_on_google_signin(monkeypatch):
    page = _GoogleSignInPage()
    mail = _NoMail()

    monkeypatch.setattr(invite.time, "sleep", lambda *_args, **_kwargs: None)

    ok, password = invite.register_with_invite(page, "https://invite.example", "user@example.com", mail, password="pw")

    assert ok is False
    assert password == "pw"
    assert invite.get_registration_error(page)["type"] == "google_signin_redirect"
    assert mail.search_calls == 0


def test_submit_email_step_skips_continue_with_google(monkeypatch):
    page = _EmailChoicePage()

    monkeypatch.setattr(invite.time, "sleep", lambda *_args, **_kwargs: None)

    assert invite._submit_email_step(page, "user@example.com") is True
    assert page.email_input.filled == "user@example.com"
    assert page.google_clicked is False
    assert page.submit_clicked is True
    assert page.url == "https://chatgpt.com/auth/login"


def test_complete_registration_reuses_one_profile_and_exports_pat_from_session(monkeypatch):
    profile = SignupProfile("Owen Reed", 1989, 2, 10, 37)
    fake_page = _FakePage(url="https://chatgpt.com")
    captured = {}
    updates = []

    monkeypatch.setattr(manager, "generate_signup_profile", lambda: profile)
    monkeypatch.setattr(
        invite,
        "register_with_invite",
        lambda page, invite_link, email, mail_client, password=None, signup_profile=None: (
            captured.setdefault("invite_profile", signup_profile),
            (True, password),
        )[1],
    )
    monkeypatch.setattr(
        "autoteam.codex_pat_export.capture_chatgpt_session_from_page",
        lambda page: {
            "chatgpt_session_token": "session-token",
            "access_token": "access-token",
            "chatgpt_account_id": "acc-2",
        },
    )

    def fake_create_auth(session_token, **kwargs):
        captured["pat_session_token"] = session_token
        captured["pat_kwargs"] = kwargs
        return {"auth_file": "/tmp/auth.json", "filename": "auth.json"}

    monkeypatch.setattr("autoteam.codex_pat_export.create_save_upload_codex_auth_from_session", fake_create_auth)
    monkeypatch.setattr(manager, "update_account", lambda email, **kwargs: updates.append((email, kwargs)))
    monkeypatch.setattr(manager, "new_browser_session", lambda: _FakeBrowserSession(fake_page))

    result = manager._complete_registration("user@example.com", "pw", "https://invite", object())

    assert result == "user@example.com"
    assert captured["invite_profile"] is profile
    assert captured["pat_session_token"] == "session-token"
    assert captured["pat_kwargs"]["email"] == "user@example.com"
    assert captured["pat_kwargs"]["account_id"] == "acc-2"
    assert captured["pat_kwargs"]["upload"] is True
    assert updates[-1][0] == "user@example.com"
    assert updates[-1][1]["status"] == "standby"
    assert updates[-1][1]["auth_file"] == "/tmp/auth.json"


def test_complete_registration_prefers_team_context_account_id(monkeypatch):
    fake_page = _FakePage(url="https://chatgpt.com")
    captured = {}
    updates = []

    class _Team:
        account_id = "acc-target"
        workspace_name = "Target Team"
        label = "Target Team"

    monkeypatch.setattr(manager, "generate_signup_profile", lambda: SignupProfile("Owen Reed", 1989, 2, 10, 37))
    monkeypatch.setattr(
        invite,
        "register_with_invite",
        lambda page, invite_link, email, mail_client, password=None, signup_profile=None: (True, password),
    )
    monkeypatch.setattr(
        "autoteam.codex_pat_export.capture_chatgpt_session_from_page",
        lambda page: {
            "chatgpt_session_token": "session-token",
            "access_token": "access-token",
            "chatgpt_account_id": "acc-default",
        },
    )

    def fake_create_auth(session_token, **kwargs):
        captured["pat_session_token"] = session_token
        captured["pat_kwargs"] = kwargs
        return {"auth_file": "/tmp/auth.json", "filename": "auth.json"}

    monkeypatch.setattr("autoteam.codex_pat_export.create_save_upload_codex_auth_from_session", fake_create_auth)
    monkeypatch.setattr(manager, "update_account", lambda email, **kwargs: updates.append((email, kwargs)))
    monkeypatch.setattr(manager, "new_browser_session", lambda: _FakeBrowserSession(fake_page))

    result = manager._complete_registration("user@example.com", "pw", "https://invite", object(), team_context=_Team())

    assert result == "user@example.com"
    assert captured["pat_session_token"] == "session-token"
    assert captured["pat_kwargs"]["account_id"] == "acc-target"
    assert updates[0][1]["chatgpt_account_id"] == "acc-target"
    assert updates[-1][1]["chatgpt_account_id"] == "acc-target"


def test_complete_registration_marks_auth_pending_when_pat_export_fails(monkeypatch):
    fake_page = _FakePage(url="https://chatgpt.com")
    updates = []

    monkeypatch.setattr(manager, "generate_signup_profile", lambda: SignupProfile("Owen Reed", 1989, 2, 10, 37))
    monkeypatch.setattr(
        invite,
        "register_with_invite",
        lambda page, invite_link, email, mail_client, password=None, signup_profile=None: (True, password),
    )
    monkeypatch.setattr(
        "autoteam.codex_pat_export.capture_chatgpt_session_from_page",
        lambda page: {"chatgpt_session_token": "session-token", "access_token": "access-token"},
    )
    monkeypatch.setattr(
        "autoteam.codex_pat_export.create_save_upload_codex_auth_from_session",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("pat failed")),
    )
    monkeypatch.setattr(manager, "update_account", lambda email, **kwargs: updates.append((email, kwargs)))
    monkeypatch.setattr(manager, "new_browser_session", lambda: _FakeBrowserSession(fake_page))

    result = manager._complete_registration("user@example.com", "pw", "https://invite", object())

    assert result == "user@example.com"
    assert updates[-1][0] == "user@example.com"
    assert updates[-1][1]["status"] == "auth_pending"
    assert "codex_pat_export_failed" in updates[-1][1]["auth_last_error"]


def test_complete_registration_records_registration_failure_as_pending(monkeypatch):
    fake_page = _FakePage(url="https://accounts.google.com/signin/oauth")
    updates = []

    monkeypatch.setattr(manager, "generate_signup_profile", lambda: SignupProfile("Owen Reed", 1989, 2, 10, 37))

    def fake_register(page, invite_link, email, mail_client, password=None, signup_profile=None):
        invite._set_registration_error(page, "google_signin_redirect", "Google sign-in redirect")
        return False, password

    monkeypatch.setattr(invite, "register_with_invite", fake_register)
    monkeypatch.setattr(manager, "update_account", lambda email, **kwargs: updates.append((email, kwargs)))
    monkeypatch.setattr(manager, "new_browser_session", lambda: _FakeBrowserSession(fake_page))

    result = manager._complete_registration("user@example.com", "pw", "https://invite", object())

    assert result is None
    assert updates[-1][0] == "user@example.com"
    assert updates[-1][1]["status"] == "pending"
    assert updates[-1][1]["auth_last_error"] == "google_signin_redirect"
    assert "Google sign-in redirect" in updates[-1][1]["auth_last_error_detail"]


def test_complete_invite_about_you_uses_profile_values(monkeypatch):
    monkeypatch.setattr(invite.time, "sleep", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(invite, "screenshot", lambda *args, **kwargs: None)
    profile = SignupProfile("Lucas Ward", 1990, 12, 5, 35)
    page = _FakePage(name_input=_FakeElement(), age_input=_FakeElement(), spinbuttons=[])

    assert invite._complete_invite_about_you(page, profile) is True
    assert page.name_input.filled == "Lucas Ward"
    assert page.age_input.filled == "35"
    assert page.submit_button.clicked is True


def test_complete_oauth_about_you_uses_profile_values(monkeypatch):
    monkeypatch.setattr(codex_auth.time, "sleep", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(codex_auth, "_screenshot", lambda *args, **kwargs: None)
    profile = SignupProfile("Henry Foster", 1987, 6, 21, 38)
    spinbuttons = [_FakeElement(), _FakeElement(), _FakeElement()]
    page = _FakePage(name_input=_FakeElement(), spinbuttons=spinbuttons)

    assert codex_auth._complete_oauth_about_you(page, profile) is True
    assert page.name_input.filled == "Henry Foster"
    assert [button.typed for button in spinbuttons] == [["1987"], ["06"], ["21"]]
    assert page.submit_button.clicked is True
