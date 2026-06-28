import json

from autoteam.session_input import parse_chatgpt_session_token


def test_parse_raw_session_token():
    assert parse_chatgpt_session_token(" raw-session-token ") == "raw-session-token"


def test_parse_cookie_header_direct_session_token():
    pasted = "Cookie: other=1; __Secure-next-auth.session-token=direct-token; theme=dark"

    assert parse_chatgpt_session_token(pasted) == "direct-token"


def test_parse_chunked_cookie_header():
    pasted = (
        "Cookie: __Secure-next-auth.session-token.1=part-b; "
        "__Secure-next-auth.session-token.0=part-a; other=1"
    )

    assert parse_chatgpt_session_token(pasted) == "part-apart-b"


def test_parse_devtools_cookie_json_array():
    pasted = json.dumps(
        [
            {"name": "other", "value": "1"},
            {"name": "__Secure-next-auth.session-token", "value": "json-token"},
        ]
    )

    assert parse_chatgpt_session_token(pasted) == "json-token"


def test_parse_state_json_session_token_key():
    pasted = json.dumps({"email": "owner@example.com", "session_token": "state-token"})

    assert parse_chatgpt_session_token(pasted) == "state-token"


def test_parse_nested_cookie_json():
    pasted = json.dumps(
        {
            "cookies": [
                {"name": "__Secure-next-auth.session-token.0", "value": "chunk-0"},
                {"name": "__Secure-next-auth.session-token.1", "value": "chunk-1"},
            ]
        }
    )

    assert parse_chatgpt_session_token(pasted) == "chunk-0chunk-1"


def test_parse_netscape_cookie_export():
    pasted = ".chatgpt.com\tTRUE\t/\tTRUE\t1893456000\t__Secure-next-auth.session-token\tnetscape-token"

    assert parse_chatgpt_session_token(pasted) == "netscape-token"


def test_parse_name_value_lines():
    pasted = "Name: __Secure-next-auth.session-token\nValue: named-token\nDomain: chatgpt.com"

    assert parse_chatgpt_session_token(pasted) == "named-token"


def test_structured_input_without_session_token_returns_empty():
    assert parse_chatgpt_session_token('{"accessToken":"not-session"}') == ""
