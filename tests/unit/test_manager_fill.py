from autoteam import manager


def test_cmd_fill_delegates_to_swap_seat_without_reuse_or_creation(monkeypatch):
    calls = []

    monkeypatch.setattr(
        manager,
        "ChatGPTTeamAPI",
        lambda: (_ for _ in ()).throw(AssertionError("cmd_fill must not open a browser")),
    )
    monkeypatch.setattr(
        manager,
        "CloudMailClient",
        lambda: (_ for _ in ()).throw(AssertionError("cmd_fill must not create a mail client")),
    )
    monkeypatch.setattr(
        manager,
        "reinvite_account",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("cmd_fill must not reinvite accounts")),
    )
    monkeypatch.setattr(
        manager,
        "create_new_account",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("cmd_fill must not create accounts")),
    )
    monkeypatch.setattr(
        manager,
        "cmd_swap_seats",
        lambda max_chatgpt_active: calls.append(max_chatgpt_active) or {"mode": "swap_seat"},
    )

    result = manager.cmd_fill(target=5)

    assert result == {"mode": "swap_seat"}
    assert calls == [5]


def test_auto_reuse_skip_reason_detects_google_provider_and_gmail():
    assert manager._auto_reuse_skip_reason({"email": "bubblehuntr@gmail.com"}) == "Google 登录账号暂不支持自动复用"
    assert (
        manager._auto_reuse_skip_reason({"email": "user@example.com", "login_provider": "google"})
        == "Google 登录账号暂不支持自动复用"
    )
    assert manager._auto_reuse_skip_reason({"email": "user@example.com"}) is None
