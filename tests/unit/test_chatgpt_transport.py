import pytest

from autoteam import chatgpt_api


class _FakeTransport:
    name = "curl_cffi"

    def __init__(self, responder):
        self._responder = responder
        self.calls = []
        self.closed = False

    def request(self, method, path, *, headers=None, body=None):
        call = {
            "method": method,
            "path": path,
            "headers": headers or {},
            "body": body,
        }
        self.calls.append(call)
        return self._responder(call, len(self.calls))

    def close(self):
        self.closed = True


def test_start_with_session_prefers_curl_cffi_transport(monkeypatch):
    transport = _FakeTransport(
        lambda call, _idx: (
            {"status": 200, "body": '{"accessToken":"tok-1"}'}
            if call["path"] == "/api/auth/session"
            else {"status": 200, "body": '{"workspace_name":"Idapro"}'}
        )
    )
    updates = []

    monkeypatch.setattr(chatgpt_api, "build_chatgpt_transport", lambda **kwargs: transport)
    monkeypatch.setattr(chatgpt_api, "update_admin_state", lambda **kwargs: updates.append(kwargs))

    client = chatgpt_api.ChatGPTTeamAPI()
    monkeypatch.setattr(
        client, "_start_browser_session", lambda _session_token: (_ for _ in ()).throw(AssertionError())
    )

    client.start_with_session("session-1", "acc-1")

    assert client.http_transport is transport
    assert client.browser is None
    assert client.access_token == "tok-1"
    assert client.workspace_name == "Idapro"
    assert updates[-1]["workspace_name"] == "Idapro"


def test_start_with_session_detects_workspace_from_har_settings_shape(monkeypatch):
    transport = _FakeTransport(
        lambda call, _idx: (
            {"status": 200, "body": '{"accessToken":"tok-1"}'}
            if call["path"] == "/api/auth/session"
            else {"status": 200, "body": '{"public_display_name":"Idapro HAR"}'}
        )
    )
    updates = []

    monkeypatch.setattr(chatgpt_api, "build_chatgpt_transport", lambda **kwargs: transport)
    monkeypatch.setattr(chatgpt_api, "update_admin_state", lambda **kwargs: updates.append(kwargs))

    client = chatgpt_api.ChatGPTTeamAPI()
    monkeypatch.setattr(
        client, "_start_browser_session", lambda _session_token: (_ for _ in ()).throw(AssertionError())
    )

    client.start_with_session("session-1", "acc-1")

    assert client.workspace_name == "Idapro HAR"
    assert updates[-1]["workspace_name"] == "Idapro HAR"


def test_start_with_session_detects_workspace_from_optimized_check_fallback(monkeypatch):
    def responder(call, _idx):
        if call["path"] == "/api/auth/session":
            return {"status": 200, "body": '{"accessToken":"tok-1"}'}
        if call["path"] == "/backend-api/accounts/acc-1/settings":
            return {"status": 200, "body": '{"workspace_id":"ws-1"}'}
        if call["path"] == "/backend-api/accounts/optimized/check":
            return {"status": 200, "body": '{"account":{"name":"Idapro Optimized","workspace_type":"team"}}'}
        raise AssertionError(call["path"])

    transport = _FakeTransport(responder)
    monkeypatch.setattr(chatgpt_api, "build_chatgpt_transport", lambda **kwargs: transport)
    monkeypatch.setattr(chatgpt_api, "update_admin_state", lambda **kwargs: None)

    client = chatgpt_api.ChatGPTTeamAPI()
    monkeypatch.setattr(
        client, "_start_browser_session", lambda _session_token: (_ for _ in ()).throw(AssertionError())
    )

    client.start_with_session("session-1", "acc-1")

    assert client.workspace_name == "Idapro Optimized"
    assert [call["path"] for call in transport.calls] == [
        "/api/auth/session",
        "/backend-api/accounts/acc-1/settings",
        "/backend-api/accounts/optimized/check",
    ]


def test_start_with_session_require_browser_is_unsupported(monkeypatch):
    monkeypatch.setattr(
        chatgpt_api,
        "build_chatgpt_transport",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("should not build curl_cffi transport")),
    )

    client = chatgpt_api.ChatGPTTeamAPI()
    monkeypatch.setattr(
        client, "_start_browser_session", lambda _session_token: (_ for _ in ()).throw(AssertionError())
    )

    with pytest.raises(RuntimeError, match="API-only"):
        client.start_with_session("session-2", "acc-2", require_browser=True)


def test_api_fetch_raises_when_curl_cffi_returns_html(monkeypatch):
    transport = _FakeTransport(lambda _call, _idx: {"status": 200, "body": "<!doctype html><html>challenge</html>"})

    client = chatgpt_api.ChatGPTTeamAPI()
    client.session_token = "session-3"
    client.account_id = "acc-3"
    client.http_transport = transport
    monkeypatch.setattr(
        client, "_ensure_browser_session", lambda: (_ for _ in ()).throw(AssertionError("should not start browser"))
    )

    with pytest.raises(RuntimeError, match="API-only"):
        client._api_fetch("GET", "/backend-api/accounts/acc-3/users")


def test_api_fetch_without_transport_never_starts_browser(monkeypatch):
    client = chatgpt_api.ChatGPTTeamAPI()
    client.session_token = "session-3"
    client.account_id = "acc-3"
    monkeypatch.setattr(
        client, "_ensure_browser_session", lambda: (_ for _ in ()).throw(AssertionError("should not start browser"))
    )

    with pytest.raises(RuntimeError, match="transport 未初始化"):
        client._api_fetch("GET", "/backend-api/accounts/acc-3/users")


def test_direct_api_fetch_refreshes_access_token_before_retry(monkeypatch):
    def responder(call, idx):
        if idx == 1:
            return {"status": 401, "body": '{"detail":{"message":"Unauthorized - Access token is missing"}}'}
        if call["path"] == "/api/auth/session":
            return {"status": 200, "body": '{"accessToken":"tok-2"}'}
        return {"status": 200, "body": '{"items":[]}'}

    transport = _FakeTransport(responder)

    client = chatgpt_api.ChatGPTTeamAPI()
    client.account_id = "acc-4"
    client.session_token = "session-4"
    client.http_transport = transport
    monkeypatch.setattr(client, "_ensure_browser_session", lambda: (_ for _ in ()).throw(AssertionError()))

    result = client._api_fetch("GET", "/backend-api/accounts/acc-4/users")

    assert client.access_token == "tok-2"
    assert result == {"status": 200, "body": '{"items":[]}'}
    assert [call["path"] for call in transport.calls] == [
        "/backend-api/accounts/acc-4/users",
        "/api/auth/session",
        "/backend-api/accounts/acc-4/users",
    ]


def test_guess_account_info_reads_accounts_check_shape():
    account_id = "11111111-2222-3333-4444-555555555555"

    class _FakePage:
        def evaluate(self, _script, _access_token):
            return {
                "/backend-api/accounts/check/v4-2023-04-27?timezone_offset_min=0": {
                    "status": 200,
                    "data": {
                        "accounts": {
                            account_id: {
                                "account": {
                                    "account_id": account_id,
                                    "name": "Idapro Check",
                                    "workspace_type": "team",
                                }
                            }
                        },
                        "account_ordering": [account_id],
                    },
                }
            }

    client = chatgpt_api.ChatGPTTeamAPI()
    client.page = _FakePage()

    detected_account_id, workspace_name = client._guess_account_info(allow_dom_fallback=False)

    assert detected_account_id == account_id
    assert workspace_name == "Idapro Check"


def test_list_invites_uses_paginated_invites_endpoint():
    def responder(call, _idx):
        responses = {
            "/backend-api/accounts/acc-1/invites?offset=0&limit=25&query=": {
                "status": 200,
                "body": '{"items":[{"email_address":"one@example.com"}],"total":2,"limit":1,"offset":0}',
            },
            "/backend-api/accounts/acc-1/invites?offset=1&limit=25&query=": {
                "status": 200,
                "body": '{"items":[{"email_address":"two@example.com"}],"total":2,"limit":1,"offset":1}',
            },
        }
        return responses[call["path"]]

    transport = _FakeTransport(responder)
    client = chatgpt_api.ChatGPTTeamAPI()
    client.account_id = "acc-1"
    client.http_transport = transport

    assert client.list_invites() == [
        {"email_address": "one@example.com"},
        {"email_address": "two@example.com"},
    ]
    assert [call["path"] for call in transport.calls] == [
        "/backend-api/accounts/acc-1/invites?offset=0&limit=25&query=",
        "/backend-api/accounts/acc-1/invites?offset=1&limit=25&query=",
    ]


def test_check_codex_quota_uses_wham_usage_endpoint():
    transport = _FakeTransport(
        lambda call, _idx: {
            "status": 200,
            "body": (
                '{"rate_limit":{"allowed":false,"limit_reached":true,'
                '"primary_window":{"used_percent":100,"reset_at":1784851280},'
                '"secondary_window":null},"additional_rate_limits":null}'
            ),
        }
    )
    client = chatgpt_api.ChatGPTTeamAPI()
    client.account_id = "acc-1"
    client.http_transport = transport

    status, info = client.check_codex_quota()

    assert status == "exhausted"
    assert info["window"] == "primary"
    assert info["quota_info"]["primary_pct"] == 100
    assert [call["path"] for call in transport.calls] == ["/backend-api/wham/usage"]


def test_fetch_codex_usage_raises_readable_error_for_non_json():
    transport = _FakeTransport(lambda _call, _idx: {"status": 200, "body": "<!doctype html>"})
    client = chatgpt_api.ChatGPTTeamAPI()
    client.account_id = "acc-1"
    client.http_transport = transport

    with pytest.raises(RuntimeError, match="非 JSON"):
        client.fetch_codex_usage()


def test_team_api_guard_blocks_kick_invite_and_allows_seat_patch():
    client = chatgpt_api.ChatGPTTeamAPI()

    with pytest.raises(RuntimeError, match="kick/remove"):
        client._assert_team_api_mutation_allowed("DELETE", "/backend-api/accounts/acc-1/users/user-1")

    with pytest.raises(RuntimeError, match="invite"):
        client._assert_team_api_mutation_allowed("POST", "/backend-api/accounts/acc-1/invites")

    with pytest.raises(RuntimeError, match="invite"):
        client._assert_team_api_mutation_allowed("PATCH", "/backend-api/accounts/acc-1/invites/inv-1")

    client._assert_team_api_mutation_allowed("PATCH", "/backend-api/accounts/acc-1/users/user-1")


def test_team_api_guard_allows_invite_only_when_explicitly_enabled():
    client = chatgpt_api.ChatGPTTeamAPI()

    with pytest.raises(RuntimeError, match="invite 创建默认禁用"):
        client._assert_team_api_mutation_allowed("POST", "/backend-api/accounts/acc-1/invites")

    client.allow_team_invites = True
    client._assert_team_api_mutation_allowed("POST", "/backend-api/accounts/acc-1/invites")

    with pytest.raises(RuntimeError, match="invite"):
        client._assert_team_api_mutation_allowed("PATCH", "/backend-api/accounts/acc-1/invites/inv-1")


def test_invite_member_temporarily_allows_post_invites():
    transport = _FakeTransport(lambda _call, _idx: {"status": 200, "body": '{"ok":true}'})
    client = chatgpt_api.ChatGPTTeamAPI()
    client.account_id = "acc-1"
    client.http_transport = transport
    client.allow_team_invites = False

    status, data = client.invite_member("new@example.com", seat_type="usage_based")

    assert status == 200
    assert data == {"ok": True}
    assert client.allow_team_invites is False
    assert transport.calls == [
        {
            "method": "POST",
            "path": "/backend-api/accounts/acc-1/invites",
            "headers": transport.calls[0]["headers"],
            "body": {
                "email_addresses": ["new@example.com"],
                "role": "standard-user",
                "seat_type": "usage_based",
                "resend_emails": True,
            },
        }
    ]
