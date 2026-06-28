from autoteam import manager


def test_cmd_rotate_delegates_to_swap_seat_without_reuse_or_creation(monkeypatch):
    calls = []

    for name in (
        "sync_account_states",
        "cmd_check",
        "ChatGPTTeamAPI",
        "CloudMailClient",
        "reinvite_account",
        "create_new_account",
        "remove_from_team",
        "sync_to_cpa",
    ):
        monkeypatch.setattr(
            manager,
            name,
            lambda *args, _name=name, **kwargs: (_ for _ in ()).throw(
                AssertionError(f"cmd_rotate must not call {_name}")
            ),
        )

    monkeypatch.setattr(
        manager,
        "cmd_swap_seats",
        lambda max_chatgpt_active: calls.append(max_chatgpt_active) or {"mode": "swap_seat"},
    )

    result = manager.cmd_rotate(target_seats=5)

    assert result == {"mode": "swap_seat"}
    assert calls == [5]


def test_cmd_sync_disabled_does_not_call_legacy_remote_sync(monkeypatch):
    monkeypatch.setattr(
        manager,
        "sync_to_cpa",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("legacy sync must stay disabled")),
    )

    result = manager.cmd_sync_disabled()

    assert result == {"mode": "swap_seat", "skipped": True, "reason": "sync_disabled"}
