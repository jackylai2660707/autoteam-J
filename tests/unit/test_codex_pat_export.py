from autoteam import codex_pat_export


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
        return self._responder(call)

    def close(self):
        self.closed = True


def test_session_backed_pat_export_uses_chatgpt_session_protocol(monkeypatch):
    def responder(call):
        if call["path"] == "/api/auth/session":
            return {
                "status": 200,
                "body": '{"accessToken":"session-access","user":{"email":"member@example.com"},"account":{"id":"acc-1"}}',
            }
        if call["path"] == "/backend-api/wham/auth-credentials/available-scopes":
            return {
                "status": 200,
                "body": '{"scopes":["chatgpt.workspace.feature.allow-codex-local-access.access"]}',
            }
        if call["path"] == "/backend-api/wham/auth-credentials":
            assert call["method"] == "POST"
            return {
                "status": 200,
                "body": (
                    '{"access_token":"pat-token","credential_id":"cred-1",'
                    '"scopes":["chatgpt.workspace.feature.allow-codex-local-access.access"]}'
                ),
            }
        raise AssertionError(call["path"])

    transport = _FakeTransport(responder)
    monkeypatch.setattr(codex_pat_export, "build_chatgpt_transport", lambda **_kwargs: transport)
    monkeypatch.setattr(codex_pat_export, "_hydrate_metadata", lambda _token, fallback: fallback)

    api = codex_pat_export.SessionBackedChatGPTAPI(session_token="session-token")
    try:
        api.start()
        result = codex_pat_export.create_codex_auth_json_from_chatgpt_api(
            api,
            ttl_days=1,
            token_name="AutoTeam Test",
        )
    finally:
        api.stop()

    assert transport.closed is True
    assert result["payload"]["email"] == "member@example.com"
    assert result["payload"]["account_id"] == "acc-1"
    assert result["payload"]["headers"]["authorization"] == "Bearer pat-token"
    assert [call["path"] for call in transport.calls] == [
        "/api/auth/session",
        "/backend-api/wham/auth-credentials/available-scopes",
        "/backend-api/wham/auth-credentials",
    ]
    wham_call = transport.calls[1]
    assert wham_call["headers"]["Authorization"] == "Bearer session-access"
    assert wham_call["headers"]["ChatGPT-Account-Id"] == "acc-1"


def test_capture_session_from_page_returns_to_chatgpt_origin(monkeypatch):
    monkeypatch.setattr(codex_pat_export.time, "sleep", lambda _seconds: None)

    class _Context:
        def cookies(self, _url):
            return [{"name": "__Secure-next-auth.session-token", "value": "member-session"}]

    class _Page:
        url = "https://auth.openai.com/login"
        context = _Context()

        def __init__(self):
            self.goto_calls = []

        def goto(self, url, **_kwargs):
            self.goto_calls.append(url)
            self.url = url

        def evaluate(self, _script):
            return {
                "status": 200,
                "body": '{"accessToken":"session-access","user":{"email":"member@example.com"},"account":{"id":"acc-1"}}',
            }

    page = _Page()

    session = codex_pat_export.capture_chatgpt_session_from_page(page)

    assert page.goto_calls == ["https://chatgpt.com/"]
    assert session["chatgpt_session_token"] == "member-session"
    assert session["chatgpt_account_id"] == "acc-1"
    assert session["chatgpt_session_email"] == "member@example.com"
