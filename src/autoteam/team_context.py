"""多 Team 工作区上下文。

配置入口：
- `TEAM_WORKSPACES_JSON`：可选 JSON 数组，每个元素描述一个 Team；
- 若未配置，则自动回退到当前 `state.json` 里的单 Team。

示例：
[
  {"id":"team-a","account_id":"...","workspace_name":"Team A","enabled":true,"max_chatgpt_active":2},
  {"id":"team-b","account_id":"...","workspace_name":"Team B","session_token":"...","email":"admin@b.com","max_chatgpt_active":1}
]
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from typing import Any

from autoteam.admin_state import load_admin_state

SWAP_ACTIVE_MIN = 1
SWAP_ACTIVE_MAX = 5


def _normalized_email(value: str | None) -> str:
    return (value or "").strip().lower()


def normalize_active_limit(value: Any = 2, *, default: int = 2) -> int:
    try:
        count = int(value)
    except Exception:
        count = int(default)
    return max(SWAP_ACTIVE_MIN, min(SWAP_ACTIVE_MAX, count))


def _truthy(value: Any, *, default: bool = True) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on", "enabled"}


@dataclass(frozen=True)
class TeamContext:
    id: str
    account_id: str
    workspace_name: str = ""
    email: str = ""
    session_token: str = ""
    enabled: bool = True
    max_chatgpt_active: int = 2
    pending_invite_email: str = ""

    @property
    def label(self) -> str:
        return self.workspace_name or self.id or self.account_id

    @property
    def cooldown_scope(self) -> str:
        return self.account_id or self.id or "default"

    def public_dict(self) -> dict:
        data = asdict(self)
        data["session_present"] = bool(self.session_token)
        data["label"] = self.label
        data.pop("session_token", None)
        return data


def _coerce_team_context(item: dict, defaults: dict, index: int) -> TeamContext | None:
    if not isinstance(item, dict):
        return None
    account_id = str(item.get("account_id") or item.get("accountId") or item.get("id") or "").strip()
    if not account_id:
        return None
    workspace_name = str(item.get("workspace_name") or item.get("workspaceName") or item.get("name") or "").strip()
    team_id = str(item.get("team_id") or item.get("teamId") or item.get("key") or "").strip()
    if not team_id:
        team_id = account_id
    return TeamContext(
        id=team_id,
        account_id=account_id,
        workspace_name=workspace_name,
        email=_normalized_email(item.get("email") or defaults.get("email")),
        session_token=str(item.get("session_token") or item.get("sessionToken") or defaults.get("session_token") or "").strip(),
        enabled=_truthy(item.get("enabled"), default=True),
        max_chatgpt_active=normalize_active_limit(
            item.get("max_chatgpt_active", item.get("target_seats", defaults.get("max_chatgpt_active", 2)))
        ),
        pending_invite_email=_normalized_email(item.get("pending_invite_email") or item.get("pendingInviteEmail")),
    )


def parse_team_contexts(raw: str | None, *, defaults: dict | None = None) -> list[TeamContext]:
    defaults = dict(defaults or {})
    text = str(raw or "").strip()
    if not text:
        return []
    try:
        data = json.loads(text)
    except Exception as exc:
        raise ValueError(f"TEAM_WORKSPACES_JSON 不是有效 JSON: {exc}") from exc
    if isinstance(data, dict):
        data = data.get("teams") or data.get("workspaces") or data.get("items") or []
    if not isinstance(data, list):
        raise ValueError("TEAM_WORKSPACES_JSON 必须是数组，或包含 teams/workspaces/items 数组的对象")

    contexts = []
    seen = set()
    for index, item in enumerate(data):
        ctx = _coerce_team_context(item, defaults, index)
        if not ctx:
            continue
        if ctx.account_id in seen:
            continue
        seen.add(ctx.account_id)
        contexts.append(ctx)
    return contexts


def current_admin_team_context(*, max_chatgpt_active: int = 2) -> TeamContext | None:
    state = load_admin_state()
    account_id = str(state.get("account_id") or os.environ.get("CHATGPT_ACCOUNT_ID", "") or "").strip()
    session_token = str(state.get("session_token") or "").strip()
    if not account_id:
        return None
    return TeamContext(
        id=account_id,
        account_id=account_id,
        workspace_name=str(state.get("workspace_name") or "").strip(),
        email=_normalized_email(state.get("email")),
        session_token=session_token,
        enabled=True,
        max_chatgpt_active=normalize_active_limit(max_chatgpt_active),
    )


def get_team_contexts(*, include_disabled: bool = False, default_max_chatgpt_active: int = 2) -> list[TeamContext]:
    """读取可管理 Team 列表；未配置多 Team 时回退当前管理员 Team。"""
    admin = load_admin_state()
    defaults = {
        "email": admin.get("email", ""),
        "session_token": admin.get("session_token", ""),
        "max_chatgpt_active": default_max_chatgpt_active,
    }
    contexts = parse_team_contexts(os.environ.get("TEAM_WORKSPACES_JSON", ""), defaults=defaults)
    if not contexts:
        current = current_admin_team_context(max_chatgpt_active=default_max_chatgpt_active)
        contexts = [current] if current else []
    if include_disabled:
        return contexts
    return [ctx for ctx in contexts if ctx.enabled]


def get_team_context(account_id: str | None = None, *, default_max_chatgpt_active: int = 2) -> TeamContext | None:
    target = str(account_id or "").strip()
    contexts = get_team_contexts(include_disabled=True, default_max_chatgpt_active=default_max_chatgpt_active)
    if target:
        for ctx in contexts:
            if ctx.account_id == target or ctx.id == target:
                return ctx
        return None
    for ctx in contexts:
        if ctx.enabled:
            return ctx
    return contexts[0] if contexts else None


def team_contexts_public(default_max_chatgpt_active: int = 2) -> list[dict]:
    return [
        ctx.public_dict()
        for ctx in get_team_contexts(include_disabled=True, default_max_chatgpt_active=default_max_chatgpt_active)
    ]
