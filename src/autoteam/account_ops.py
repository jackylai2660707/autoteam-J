"""账号资源清理与远端对账操作。"""

import json
import logging
from pathlib import Path

from autoteam.accounts import find_account, load_accounts, save_accounts
from autoteam.admin_state import get_chatgpt_account_id
from autoteam.mail_provider import (
    get_account_mail_account_id,
    get_account_mail_provider,
    get_account_mail_service_id,
    get_mail_client_for_account,
)
from autoteam.sync_targets import delete_account_from_configured_targets
from autoteam.sync_targets import sync_to_configured_targets as sync_to_cpa

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).parent.parent.parent
AUTH_DIR = PROJECT_ROOT / "auths"


def _response_excerpt(body, limit=240):
    text = str(body or "").strip().replace("\n", " ")
    if len(text) > limit:
        text = text[:limit] + "..."
    return text


def _parse_team_api_json(response, label):
    status = int(response.get("status") or 0)
    body = response.get("body", "")

    if status in (401, 403):
        raise RuntimeError(f"{label}接口鉴权失败 (HTTP {status})，请重新完成管理员登录")
    if status != 200:
        raise RuntimeError(f"{label}接口请求失败 (HTTP {status}): {_response_excerpt(body)}")

    try:
        return json.loads(body)
    except Exception as exc:
        lower_body = str(body or "").lower()
        if "<html" in lower_body or "<!doctype" in lower_body:
            raise RuntimeError(f"{label}接口返回了非 JSON 内容（疑似登录页或错误页），请重新完成管理员登录") from exc
        raise RuntimeError(f"{label}接口返回了非 JSON 内容: {_response_excerpt(body)}") from exc


def _extract_remote_cleanup_errors(results):
    errors = {}
    for target, target_result in (results or {}).items():
        if not isinstance(target_result, dict):
            continue
        error = str(target_result.get("error") or "").strip()
        if error:
            errors[str(target)] = error
    return errors


def _resolve_account_id(chatgpt_api=None, account_id=None):
    resolved = str(account_id or getattr(chatgpt_api, "account_id", "") or "").strip()
    resolved = resolved or get_chatgpt_account_id()
    if resolved and chatgpt_api is not None and hasattr(chatgpt_api, "account_id"):
        try:
            chatgpt_api.account_id = resolved
        except Exception:
            pass
    return resolved


def fetch_team_members(chatgpt_api, account_id=None):
    """只读 Team 成员列表；不读取/处理 invite。"""
    account_id = _resolve_account_id(chatgpt_api, account_id)
    users_resp = chatgpt_api._api_fetch("GET", f"/backend-api/accounts/{account_id}/users")
    data = _parse_team_api_json(users_resp, "Team 成员")
    return data.get("items", data.get("users", data.get("members", [])))


def fetch_team_state(chatgpt_api, account_id=None):
    """读取 Team 成员和邀请状态（只读；保留给展示页兼容）。"""
    account_id = _resolve_account_id(chatgpt_api, account_id)
    members = fetch_team_members(chatgpt_api, account_id=account_id)

    invites_resp = chatgpt_api._api_fetch("GET", f"/backend-api/accounts/{account_id}/invites")
    data = _parse_team_api_json(invites_resp, "Team 邀请")
    invites = data if isinstance(data, list) else data.get("invites", data.get("account_invites", []))

    return members, invites


def delete_managed_account(
    email,
    *,
    remove_remote=True,
    remove_cloudmail=True,
    sync_cpa_after=True,
    chatgpt_api=None,
    mail_client=None,
    remote_state=None,
):
    """swap_seat-only 模式下禁用账号删除/Team member 移除。"""
    raise RuntimeError("swap_seat-only 模式已禁用账号删除/Team member 移除；请只执行 swap_seat")
