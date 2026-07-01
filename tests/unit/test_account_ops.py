import pytest

from autoteam import account_ops


class _FakeChatGPT:
    def __init__(self, responses):
        self._responses = responses

    def _api_fetch(self, method, path):
        return self._responses[path]


def test_fetch_team_state_parses_members_and_invites(monkeypatch):
    monkeypatch.setattr(account_ops, "get_chatgpt_account_id", lambda: "acc-1")
    chatgpt = _FakeChatGPT(
        {
            "/backend-api/accounts/acc-1/users?offset=0&limit=25&query=": {
                "status": 200,
                "body": '{"items":[{"email":"member@example.com"}]}',
            },
            "/backend-api/accounts/acc-1/invites?offset=0&limit=25&query=": {
                "status": 200,
                "body": '{"invites":[{"email":"invite@example.com"}]}',
            },
        }
    )

    members, invites = account_ops.fetch_team_state(chatgpt)

    assert members == [{"email": "member@example.com"}]
    assert invites == [{"email": "invite@example.com"}]


def test_fetch_team_state_parses_har_items_and_paginates_invites(monkeypatch):
    monkeypatch.setattr(account_ops, "get_chatgpt_account_id", lambda: "acc-1")
    chatgpt = _FakeChatGPT(
        {
            "/backend-api/accounts/acc-1/users?offset=0&limit=25&query=": {
                "status": 200,
                "body": '{"items":[{"email":"member@example.com"}],"total":1,"limit":25,"offset":0}',
            },
            "/backend-api/accounts/acc-1/invites?offset=0&limit=25&query=": {
                "status": 200,
                "body": (
                    '{"items":[{"email_address":"first@example.com"}],'
                    '"total":2,"limit":1,"offset":0}'
                ),
            },
            "/backend-api/accounts/acc-1/invites?offset=1&limit=25&query=": {
                "status": 200,
                "body": (
                    '{"items":[{"email_address":"second@example.com"}],'
                    '"total":2,"limit":1,"offset":1}'
                ),
            },
        }
    )

    members, invites = account_ops.fetch_team_state(chatgpt)

    assert members == [{"email": "member@example.com"}]
    assert invites == [
        {"email_address": "first@example.com"},
        {"email_address": "second@example.com"},
    ]


def test_fetch_team_invite_count_reads_only_first_page(monkeypatch):
    monkeypatch.setattr(account_ops, "get_chatgpt_account_id", lambda: "acc-1")
    calls = []

    class _CountChatGPT:
        def _api_fetch(self, method, path):
            calls.append(path)
            return {
                "status": 200,
                "body": '{"items":[{"email_address":"first@example.com"}],"total":888,"limit":1,"offset":0}',
            }

    assert account_ops.fetch_team_invite_count(_CountChatGPT()) == 888
    assert calls == ["/backend-api/accounts/acc-1/invites?offset=0&limit=1&query="]


def test_fetch_team_invites_can_limit_loaded_items(monkeypatch):
    monkeypatch.setattr(account_ops, "get_chatgpt_account_id", lambda: "acc-1")
    chatgpt = _FakeChatGPT(
        {
            "/backend-api/accounts/acc-1/invites?offset=0&limit=25&query=": {
                "status": 200,
                "body": (
                    '{"items":[{"email_address":"first@example.com"}],'
                    '"total":2,"limit":1,"offset":0}'
                ),
            },
        }
    )

    assert account_ops.fetch_team_invites(chatgpt, max_items=1) == [{"email_address": "first@example.com"}]


def test_fetch_team_state_raises_readable_error_when_users_response_is_html(monkeypatch):
    monkeypatch.setattr(account_ops, "get_chatgpt_account_id", lambda: "acc-1")
    chatgpt = _FakeChatGPT(
        {
            "/backend-api/accounts/acc-1/users?offset=0&limit=25&query=": {
                "status": 200,
                "body": "<!doctype html><html><body>login</body></html>",
            },
            "/backend-api/accounts/acc-1/invites?offset=0&limit=25&query=": {
                "status": 200,
                "body": '{"invites":[]}',
            },
        }
    )

    with pytest.raises(RuntimeError, match="Team 成员接口返回了非 JSON 内容"):
        account_ops.fetch_team_state(chatgpt)


def test_fetch_team_state_raises_readable_error_when_users_auth_fails(monkeypatch):
    monkeypatch.setattr(account_ops, "get_chatgpt_account_id", lambda: "acc-1")
    chatgpt = _FakeChatGPT(
        {
            "/backend-api/accounts/acc-1/users?offset=0&limit=25&query=": {
                "status": 403,
                "body": '{"detail":"forbidden"}',
            },
            "/backend-api/accounts/acc-1/invites?offset=0&limit=25&query=": {
                "status": 200,
                "body": '{"invites":[]}',
            },
        }
    )

    with pytest.raises(RuntimeError, match="请重新完成管理员登录"):
        account_ops.fetch_team_state(chatgpt)


def test_delete_managed_account_is_hard_disabled():
    with pytest.raises(RuntimeError, match="swap_seat-only"):
        account_ops.delete_managed_account("user@example.com", remove_remote=False)
