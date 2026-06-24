import threading

import pytest

from autoteam import api


def _setup_team_member_api(monkeypatch):
    monkeypatch.setattr(api, "_playwright_lock", threading.Lock())
    monkeypatch.setattr(api._pw_executor, "run", lambda func, *args, **kwargs: func(*args, **kwargs))
    monkeypatch.setattr("autoteam.admin_state.get_admin_session_token", lambda: "session")
    monkeypatch.setattr("autoteam.admin_state.get_chatgpt_account_id", lambda: "acc-1")


def test_get_team_members_includes_seat_type_fields(monkeypatch):
    _setup_team_member_api(monkeypatch)

    monkeypatch.setattr(api, "_run_with_chatgpt_session", lambda callback: callback(object()))
    monkeypatch.setattr(
        "autoteam.account_ops.fetch_team_state",
        lambda _chatgpt: (
            [
                {
                    "email": "member@example.com",
                    "role": "standard-user",
                    "id": "user-1",
                    "seat_type": "default",
                }
            ],
            [
                {
                    "email_address": "invite@example.com",
                    "role": "standard-user",
                    "id": "invite-1",
                    "seat_type": "usage_based",
                }
            ],
        ),
    )
    monkeypatch.setattr("autoteam.accounts.load_accounts", lambda: [{"email": "member@example.com"}])

    result = api.get_team_members()

    member = next(item for item in result["members"] if item["type"] == "member")
    invite = next(item for item in result["members"] if item["type"] == "invite")

    assert member["seat_type"] == "chatgpt"
    assert member["seat_type_raw"] == "default"
    assert member["seat_type_label"] == "ChatGPT"
    assert member["is_local"] is True

    assert invite["seat_type"] == "codex"
    assert invite["seat_type_raw"] == "usage_based"
    assert invite["seat_type_label"] == "Codex"


def test_post_team_member_remove_rejects_member_and_invite_removal(monkeypatch):
    _setup_team_member_api(monkeypatch)

    with pytest.raises(api.HTTPException) as exc:
        api.post_team_member_remove(
            api.TeamMemberRemoveParams(
                email="member@example.com",
                user_id="user-1",
                type="member",
            )
        )

    assert exc.value.status_code == 410
    assert "swap_seat" in str(exc.value.detail)

    with pytest.raises(api.HTTPException) as exc:
        api.post_team_member_remove(
            api.TeamMemberRemoveParams(
                email="invite@example.com",
                user_id="invite-1",
                type="invite",
            )
        )

    assert exc.value.status_code == 410
    assert "invite" in str(exc.value.detail)


def test_post_team_member_seat_is_disabled(monkeypatch):
    _setup_team_member_api(monkeypatch)

    class FakeChatGPT:
        def __init__(self):
            self.calls = []

        def update_member_seat_type(self, user_id, seat_type):
            self.calls.append((user_id, seat_type))
            return {"status": 200, "body": "{}"}

    fake_chatgpt = FakeChatGPT()

    monkeypatch.setattr(api, "_run_with_chatgpt_session", lambda callback: callback(fake_chatgpt))
    monkeypatch.setattr("autoteam.accounts.load_accounts", lambda: [])

    with pytest.raises(api.HTTPException) as exc:
        api.post_team_member_seat(
            api.TeamMemberSeatParams(
                email="member@example.com",
                user_id="user-1",
                type="member",
                seat_type="codex",
            )
        )

    assert exc.value.status_code == 410
    assert fake_chatgpt.calls == []
