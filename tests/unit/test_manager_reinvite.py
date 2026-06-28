import pytest

from autoteam import manager


def test_reinvite_account_is_disabled_without_browser_login(monkeypatch):
    monkeypatch.setattr(
        manager,
        "login_codex_via_browser",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("reinvite must not login via browser")),
    )
    monkeypatch.setattr(
        manager,
        "_record_auth_repair_failure",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("reinvite must not mutate account state")),
    )

    with pytest.raises(RuntimeError, match="swap_seat-only"):
        manager.reinvite_account(None, None, {"email": "tmp-user@example.com", "password": "secret"})
