import json

from autoteam import team_context


def test_parse_team_contexts_inherits_admin_session_and_clamps_limit():
    raw = json.dumps(
        [
            {
                "id": "team-a",
                "account_id": "acc-a",
                "workspace_name": "Team A",
                "max_chatgpt_active": 9,
                "invite_domains": "pool-a.example.com; pool-b.example.com",
            },
            {
                "id": "team-b",
                "account_id": "acc-b",
                "enabled": False,
                "session_token": "team-b-session",
                "max_chatgpt_active": 1,
            },
        ]
    )

    contexts = team_context.parse_team_contexts(
        raw,
        defaults={"email": "owner@example.com", "session_token": "main-session", "max_chatgpt_active": 2},
    )

    assert [ctx.account_id for ctx in contexts] == ["acc-a", "acc-b"]
    assert contexts[0].session_token == "main-session"
    assert contexts[0].max_chatgpt_active == 5
    assert contexts[0].invite_domains == "pool-a.example.com; pool-b.example.com"
    assert contexts[1].session_token == "team-b-session"
    assert contexts[1].enabled is False


def test_get_team_contexts_falls_back_to_current_admin(monkeypatch):
    monkeypatch.delenv("TEAM_WORKSPACES_JSON", raising=False)
    monkeypatch.setattr(
        team_context,
        "load_admin_state",
        lambda: {
            "email": "owner@example.com",
            "session_token": "session",
            "account_id": "acc-main",
            "workspace_name": "Main Team",
        },
    )

    contexts = team_context.get_team_contexts(default_max_chatgpt_active=3)

    assert len(contexts) == 1
    assert contexts[0].account_id == "acc-main"
    assert contexts[0].workspace_name == "Main Team"
    assert contexts[0].max_chatgpt_active == 3
