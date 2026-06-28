"""Parse pasted ChatGPT session input without logging sensitive values."""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from urllib.parse import unquote

SESSION_COOKIE_BASE_NAMES = (
    "__Secure-next-auth.session-token",
    "next-auth.session-token",
)
SESSION_TOKEN_KEYS = (
    "session_token",
    "sessionToken",
    "chatgpt_session_token",
    "chatgptSessionToken",
    "admin_session_token",
    "member_session_token",
)

_COOKIE_ASSIGN_RE = re.compile(
    r"(?P<name>(?:__Secure-)?next-auth\.session-token(?:\.\d+)?)\s*=\s*(?P<value>\"[^\"]*\"|'[^']*'|[^;\s,\r\n]+)"
)
_NAME_LINE_RE = re.compile(r"^\s*name\s*[:=]\s*(?P<value>.+?)\s*$", re.I)
_VALUE_LINE_RE = re.compile(r"^\s*value\s*[:=]\s*(?P<value>.+?)\s*$", re.I)


def parse_chatgpt_session_token(raw_input: str | None) -> str:
    """Extract ChatGPT session token from raw token, Cookie header, or cookie JSON.

    Supported paste formats:
    - raw session token
    - Cookie header containing ``__Secure-next-auth.session-token``
    - DevTools / extension cookie JSON objects or arrays with ``name`` and ``value``
    - chunked cookies such as ``__Secure-next-auth.session-token.0`` and ``.1``
    - Netscape-style cookie export lines
    """

    text = str(raw_input or "").strip()
    if not text:
        return ""

    token = _parse_json_input(text)
    if token:
        return token

    token = _parse_text_input(text)
    if token:
        return token

    if _looks_structured(text):
        return ""
    return _clean_value(text)


def _parse_json_input(text: str) -> str:
    try:
        data = json.loads(text)
    except Exception:
        return ""
    return _extract_from_json_value(data)


def _extract_from_json_value(value) -> str:
    if isinstance(value, str):
        nested = _parse_json_input(value)
        if nested:
            return nested
        return _parse_text_input(value)

    if isinstance(value, list):
        token = _token_from_cookie_dicts(value)
        if token:
            return token
        for item in value:
            token = _extract_from_json_value(item)
            if token:
                return token
        return ""

    if not isinstance(value, dict):
        return ""

    for key in SESSION_TOKEN_KEYS:
        if key in value:
            token = _extract_from_json_value(value.get(key))
            if token:
                return token
            return _clean_value(value.get(key))

    token = _token_from_cookie_dicts([value])
    if token:
        return token

    for key in ("cookies", "cookie", "cookieStore", "cookie_store", "jar"):
        token = _extract_from_json_value(value.get(key))
        if token:
            return token

    return ""


def _parse_text_input(text: str) -> str:
    pairs: list[tuple[str, str]] = []
    pairs.extend(_cookie_assignments(text))
    pairs.extend(_netscape_cookie_lines(text))
    pairs.extend(_tabular_cookie_lines(text))
    pairs.extend(_name_value_line_pairs(text))
    return _token_from_pairs(pairs)


def _cookie_assignments(text: str) -> list[tuple[str, str]]:
    return [(match.group("name"), match.group("value")) for match in _COOKIE_ASSIGN_RE.finditer(text)]


def _netscape_cookie_lines(text: str) -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        parts = re.split(r"\s+", stripped)
        if len(parts) < 7:
            continue
        name, value = parts[-2], parts[-1]
        if _is_session_cookie_name(name):
            pairs.append((name, value))
    return pairs


def _tabular_cookie_lines(text: str) -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    for line in text.splitlines():
        parts = [part.strip() for part in line.split("\t") if part.strip()]
        for idx, name in enumerate(parts[:-1]):
            if _is_session_cookie_name(name):
                pairs.append((name, parts[idx + 1]))
    return pairs


def _name_value_line_pairs(text: str) -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    pending_name = ""
    for line in text.splitlines():
        name_match = _NAME_LINE_RE.match(line)
        if name_match:
            pending_name = name_match.group("value")
            continue
        value_match = _VALUE_LINE_RE.match(line)
        if value_match and pending_name:
            pairs.append((pending_name, value_match.group("value")))
            pending_name = ""
    return pairs


def _token_from_cookie_dicts(cookies: Iterable) -> str:
    pairs: list[tuple[str, str]] = []
    for cookie in cookies or []:
        if not isinstance(cookie, dict):
            continue
        name = cookie.get("name") or cookie.get("key")
        value = cookie.get("value")
        if name is None or value is None:
            continue
        pairs.append((str(name), str(value)))
    return _token_from_pairs(pairs)


def _token_from_pairs(pairs: Iterable[tuple[str, str]]) -> str:
    direct = ""
    chunked: dict[tuple[str, int], str] = {}
    for name, value in pairs:
        name = _clean_value(name)
        value = _clean_value(value)
        if not name or not value:
            continue
        if name in SESSION_COOKIE_BASE_NAMES:
            direct = value
            continue
        for base_name in SESSION_COOKIE_BASE_NAMES:
            match = re.fullmatch(re.escape(base_name) + r"\.(\d+)", name)
            if match:
                chunked[(base_name, int(match.group(1)))] = value
                break

    if direct:
        return direct

    for base_name in SESSION_COOKIE_BASE_NAMES:
        parts = {idx: value for (name, idx), value in chunked.items() if name == base_name}
        if parts:
            return "".join(parts[idx] for idx in sorted(parts))
    return ""


def _is_session_cookie_name(name: str) -> bool:
    name = _clean_value(name)
    if name in SESSION_COOKIE_BASE_NAMES:
        return True
    return any(re.fullmatch(re.escape(base_name) + r"\.\d+", name) for base_name in SESSION_COOKIE_BASE_NAMES)


def _clean_value(value) -> str:
    text = str(value or "").strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in ("'", '"'):
        text = text[1:-1].strip()
    return unquote(text)


def _looks_structured(text: str) -> bool:
    stripped = text.lstrip()
    if stripped.startswith(("{", "[")):
        return True
    lowered = stripped.lower()
    return (
        lowered.startswith("cookie:")
        or "name:" in lowered
        or "value:" in lowered
        or any(name in text for name in SESSION_COOKIE_BASE_NAMES)
    )
