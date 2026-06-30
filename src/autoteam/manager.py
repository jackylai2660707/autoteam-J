#!/usr/bin/env python3
import autoteam.display  # noqa: F401 — 自动设置虚拟显示器

"""
swap_seat-only 管理器。

当前项目只允许：
- 通过 CPA 检查 Team 成员 Codex quota（5h + weekly）；
- 可配置保留 1~5 个 ChatGPT seat + CPA OAuth active；
- 只对 AutoTeam 自己创建的成员执行 seat/OAuth 收敛，外部/主号成员默认保护；
- 非 swap 功能默认归档；只消费已有 pending invite，用 CF Temp Email 对应邮箱完成注册，注册前只做只读检查；
- 永远禁止 Team member kick/remove。
"""

import getpass
import json
import logging
import os
import re
import secrets
import string
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from autoteam.account_ops import fetch_team_members, fetch_team_state
from autoteam.accounts import (
    STATUS_ACTIVE,
    STATUS_AUTH_PENDING,
    STATUS_EXHAUSTED,
    STATUS_PENDING,
    STATUS_STANDBY,
    add_account,
    find_account,
    is_account_disabled,
    load_accounts,
    save_accounts,
    update_account,
)
from autoteam.admin_state import get_admin_email, get_admin_state_summary, get_chatgpt_account_id
from autoteam.browser_backend import new_browser_session
from autoteam.chatgpt_api import ChatGPTTeamAPI
from autoteam.codex_auth import (
    MainCodexSyncFlow,
    _click_primary_auth_button,
    _is_google_redirect,
    check_codex_quota,
    get_quota_exhausted_info,
    get_saved_main_auth_file,
    login_codex_via_browser,
    quota_result_quota_info,
    quota_result_resets_at,
    refresh_access_token,
)
from autoteam.cpa_sync import (
    check_cpa_codex_quota,
    cpa_auth_is_active,
    cpa_auth_is_disabled,
    get_managed_cpa_auth_names,
    is_cpa_codex_oauth,
    is_managed_cpa_auth,
    list_cpa_files,
    set_cpa_auth_disabled,
    sync_from_cpa,
)
from autoteam.mail_provider import (
    MAIL_PROVIDER_CLOUDFLARE_TEMP_EMAIL,
    get_account_mail_provider,
    get_account_mail_service_id,
    get_mail_client_for_account,
    get_mail_services,
    infer_mail_provider_from_email,
    infer_mail_service_from_email,
)
from autoteam.mail_provider import (
    get_mail_client as get_mail_client,
)
from autoteam.signup_profile import SignupProfile, generate_signup_profile
from autoteam.sync_targets import (
    sync_main_codex_to_configured_targets as sync_main_codex_to_cpa,
)
from autoteam.sync_targets import (
    sync_to_configured_targets as sync_to_cpa,
)
from autoteam.textio import read_text, write_text


def _team_account_id(team_context=None) -> str:
    return str(getattr(team_context, "account_id", "") or get_chatgpt_account_id() or "").strip()


def _team_label(team_context=None) -> str:
    return str(
        getattr(team_context, "label", "")
        or getattr(team_context, "workspace_name", "")
        or getattr(team_context, "account_id", "")
        or "default"
    ).strip()


def _start_chatgpt_for_team(chatgpt_api, team_context=None):
    account_id = _team_account_id(team_context)
    session_token = str(getattr(team_context, "session_token", "") or "").strip()
    workspace_name = str(getattr(team_context, "workspace_name", "") or "").strip()
    if team_context and session_token and account_id:
        chatgpt_api.start_with_session(session_token, account_id, workspace_name)
    else:
        chatgpt_api.start()
        # 同一个管理员 session 可能管理多个 workspace。start() 会加载默认
        # account_id，但多 Team 调度必须继续使用当前 Team 的 account_id，
        # 否则后续 PATCH /users/{id} 可能打到错误 workspace。
        if account_id:
            chatgpt_api.account_id = account_id
            if workspace_name:
                chatgpt_api.workspace_name = workspace_name
    return chatgpt_api


def _fetch_team_state_for_account(chatgpt_api, account_id: str | None = None):
    if account_id:
        try:
            return fetch_team_state(chatgpt_api, account_id=account_id)
        except TypeError:
            return fetch_team_state(chatgpt_api)
    return fetch_team_state(chatgpt_api)


def _fetch_team_members_for_account(chatgpt_api, account_id: str | None = None):
    if account_id:
        try:
            return fetch_team_members(chatgpt_api, account_id=account_id)
        except TypeError:
            return fetch_team_members(chatgpt_api)
    return fetch_team_members(chatgpt_api)


logger = logging.getLogger(__name__)

MAIL_TIMEOUT = int(os.environ.get("MAIL_TIMEOUT", "180"))
REUSE_RESET_GRACE_SECONDS = int(os.environ.get("REUSE_RESET_GRACE_SECONDS", "300"))

# 兼容旧调用名：现在返回“默认邮箱服务”的客户端，不再只指向 CloudMail。
CloudMailClient = get_mail_client


def _chatgpt_session_ready(chatgpt_api) -> bool:
    if not chatgpt_api:
        return False
    is_started = getattr(chatgpt_api, "is_started", None)
    if callable(is_started):
        try:
            return bool(is_started())
        except Exception:
            pass
    return bool(getattr(chatgpt_api, "browser", None))


def _abort_if_cancel_requested():
    try:
        from autoteam.api import ensure_current_task_not_cancelled
    except ImportError:
        return

    ensure_current_task_not_cancelled()


AUTH_REPAIR_HARD_FAILURE_TYPES = {
    "account_deactivated",
    "human_verification",
    "member_session_invalid",
    "member_session_missing",
}
AUTH_REPAIR_SINGLE_ATTEMPT_FAILURE_TYPES = {"add_phone", "human_verification"}


def _normalized_email(value: str | None) -> str:
    return (value or "").strip().lower()


def _email_domain(value: str | None) -> str:
    email = _normalized_email(value)
    if "@" not in email:
        return ""
    return email.rsplit("@", 1)[-1]


def _parse_pending_invite_forward_map(value: str | None = None) -> dict[str, str]:
    """Parse pending invite mail forwarding rules.

    Supported forms:
    - {"icloud.com":"inbox@example.com"}
    - [{"from":"icloud.com","to":"inbox@example.com"}]
    - icloud.com=inbox@example.com; user@a.com=inbox@example.com
    """
    raw = str(value if value is not None else os.environ.get("PENDING_INVITE_FORWARD_MAP", "") or "").strip()
    if not raw:
        return {}

    items = []
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, dict):
            items = list(parsed.items())
        elif isinstance(parsed, list):
            for item in parsed:
                if not isinstance(item, dict):
                    continue
                source = item.get("from") or item.get("source") or item.get("domain") or item.get("email")
                target = item.get("to") or item.get("target") or item.get("mailbox") or item.get("recipient")
                items.append((source, target))
    except Exception:
        for part in re.split(r"[;\n,]+", raw):
            text = part.strip()
            if not text:
                continue
            if "=>" in text:
                source, target = text.split("=>", 1)
            elif "=" in text:
                source, target = text.split("=", 1)
            elif ":" in text:
                source, target = text.split(":", 1)
            else:
                continue
            items.append((source, target))

    mapping = {}
    for source, target in items:
        source_key = str(source or "").strip().lower().lstrip("@")
        target_email = _normalized_email(target)
        if not source_key or "@" not in target_email:
            continue
        if source_key.startswith("*."):
            source_key = source_key[2:]
        mapping[source_key] = target_email
    return mapping


def _pending_invite_forward_to(email: str | None, acc: dict | None = None) -> str:
    acc = acc or {}
    explicit = _normalized_email(acc.get("mail_forward_to") or acc.get("forward_to") or acc.get("delivery_email"))
    if explicit:
        return explicit

    target = _normalized_email(email)
    if not target:
        return ""
    mapping = _parse_pending_invite_forward_map()
    domain = _email_domain(target)
    return mapping.get(target) or mapping.get(domain) or ""


def _mail_delivery_email_for(email: str | None, acc: dict | None = None) -> str:
    return _pending_invite_forward_to(email, acc=acc) or _normalized_email(email)


class _ForwardedRecipientMailClient:
    """Use a forwarding mailbox for reads while keeping the account email unchanged."""

    def __init__(self, base_client, mapping: dict[str, str] | None = None):
        self._base_client = base_client
        self._mapping = mapping or _parse_pending_invite_forward_map()

    def __getattr__(self, name):
        return getattr(self._base_client, name)

    @property
    def provider_name(self):
        return getattr(self._base_client, "provider_name", "")

    @property
    def service_id(self):
        return getattr(self._base_client, "service_id", None)

    def _forward_to(self, to_email, acc: dict | None = None):
        target = _normalized_email(to_email)
        explicit = _normalized_email((acc or {}).get("mail_forward_to") or (acc or {}).get("forward_to"))
        if explicit:
            return explicit
        domain = _email_domain(target)
        return self._mapping.get(target) or self._mapping.get(domain) or target

    def _resolve_account_id_for_email(self, to_email):
        mapped = self._forward_to(to_email)
        resolver = getattr(self._base_client, "_resolve_account_id_for_email", None)
        if callable(resolver):
            return resolver(mapped)
        return None

    def search_emails_by_recipient(self, to_email, size=10, account_id=None):
        mapped = self._forward_to(to_email)
        mapped_account_id = account_id
        if mapped != _normalized_email(to_email):
            mapped_account_id = self._resolve_account_id_for_email(mapped)
            logger.info("[邮件转发] %s 的邮件将从 %s 读取", _normalized_email(to_email), mapped)
        try:
            return self._base_client.search_emails_by_recipient(mapped, size=size, account_id=mapped_account_id)
        except TypeError:
            return self._base_client.search_emails_by_recipient(mapped, size=size)

    def wait_for_email(self, to_email, timeout=None, sender_keyword=None):
        mapped = self._forward_to(to_email)
        return self._base_client.wait_for_email(mapped, timeout=timeout, sender_keyword=sender_keyword)

    def delete_emails_for(self, to_email):
        mapped = self._forward_to(to_email)
        deleter = getattr(self._base_client, "delete_emails_for", None)
        if callable(deleter):
            return deleter(mapped)
        return 0


def _with_pending_invite_forwarding(mail_client, acc: dict | None = None):
    mapping = _parse_pending_invite_forward_map()
    if acc:
        account_email = _normalized_email(acc.get("email"))
        forward_to = _pending_invite_forward_to(account_email, acc=acc)
        if account_email and forward_to and forward_to != account_email:
            mapping[account_email] = forward_to
    if not mapping:
        return mail_client
    if isinstance(mail_client, _ForwardedRecipientMailClient):
        return mail_client
    return _ForwardedRecipientMailClient(mail_client, mapping=mapping)


def _parse_email_list(value) -> tuple[list[str], list[str]]:
    if value is None:
        raw_parts = []
    elif isinstance(value, (list, tuple, set)):
        raw_parts = [str(item or "") for item in value]
    else:
        raw_parts = re.split(r"[,;\s]+", str(value or ""))

    emails = []
    invalid = []
    seen = set()
    for part in raw_parts:
        email = _normalized_email(part)
        if not email:
            continue
        if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email):
            invalid.append(email)
            continue
        if email in seen:
            continue
        seen.add(email)
        emails.append(email)
    return emails, invalid


def _invite_email(invite: dict | None) -> str:
    invite = invite or {}
    return _normalized_email(invite.get("email_address") or invite.get("email"))


def _is_pending_invite(invite: dict | None) -> bool:
    invite = invite or {}
    raw_status = invite.get("status")
    if raw_status is None or raw_status == "":
        return True
    status = str(raw_status).strip().lower()
    # ChatGPT currently returns numeric status=2 for pending invites.
    return status in {"2", "pending", "invited", "sent"}


def _normalize_invite_concurrency(value, *, default=3, maximum=8) -> int:
    try:
        count = int(value or default)
    except Exception:
        count = int(default)
    return max(1, min(int(maximum), count))


def _normalize_invite_batch_size(value, *, default=20, maximum=50) -> int:
    try:
        count = int(value or default)
    except Exception:
        count = int(default)
    return max(1, min(int(maximum), count))


def _chunked(items: list, size: int):
    size = max(1, int(size or 1))
    for index in range(0, len(items), size):
        yield items[index : index + size]


def _truthy_env(value: str | None) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on", "enabled"}


def _explicit_managed_flag(acc: dict | None) -> bool | None:
    acc = acc or {}
    if "managed_by_autoteam" not in acc:
        return None
    raw = acc.get("managed_by_autoteam")
    if raw is None or str(raw).strip() == "":
        return None
    return _truthy_env(raw)


def _allow_member_email_login_for_pat() -> bool:
    """是否允许 PAT 修复在缺少/失效 session 时重新捕获 member session。

    历史版本默认关闭接码登录；现在按业务规则改为：未耗尽额度的 member
    不应因为缺 session 被跳过。紧急禁用请设置
    AUTO_CHECK_DISABLE_MEMBER_EMAIL_LOGIN_FOR_PAT=true。
    """
    if _truthy_env(os.environ.get("AUTO_CHECK_DISABLE_MEMBER_EMAIL_LOGIN_FOR_PAT")):
        return False
    return True


def _account_chatgpt_session_token(acc: dict | None) -> str:
    acc = acc or {}
    return str(
        acc.get("chatgpt_session_token")
        or acc.get("member_session_token")
        or acc.get("session_token")
        or ""
    ).strip()


def _account_chatgpt_account_id(acc: dict | None, team_context=None) -> str:
    acc = acc or {}
    return str(
        acc.get("chatgpt_account_id")
        or acc.get("workspace_account_id")
        or _team_account_id(team_context)
        or get_chatgpt_account_id()
        or ""
    ).strip()


def _account_record_team_account_id(acc: dict | None) -> str:
    """Return only the account_id explicitly saved on the local account record."""
    acc = acc or {}
    return str(acc.get("chatgpt_account_id") or acc.get("workspace_account_id") or acc.get("team_account_id") or "").strip()


def _account_matches_team_context(acc: dict | None, team_context=None) -> bool:
    """Keep explicit multi-Team repair scoped to accounts tied to that Team."""
    target_account_id = _team_account_id(team_context) if team_context else ""
    if not target_account_id:
        return True
    record_account_id = _account_record_team_account_id(acc)
    return bool(record_account_id and record_account_id == target_account_id)


def _team_or_session_account_id(session_info: dict | None = None, team_context=None) -> str:
    """Prefer an explicit Team context over whatever workspace the browser last opened."""
    session_info = session_info or {}
    explicit_team_account_id = _team_account_id(team_context) if team_context is not None else ""
    return str(explicit_team_account_id or session_info.get("chatgpt_account_id") or _team_account_id(None) or "").strip()


def _chatgpt_session_update_fields(session_info: dict | None) -> dict:
    session_info = session_info or {}
    fields = {}
    mapping = {
        "chatgpt_session_token": "chatgpt_session_token",
        "chatgpt_account_id": "chatgpt_account_id",
        "chatgpt_session_email": "chatgpt_session_email",
        "chatgpt_session_expires": "chatgpt_session_expires",
        "chatgpt_session_captured_at": "chatgpt_session_captured_at",
    }
    for source, dest in mapping.items():
        value = session_info.get(source)
        if value:
            fields[dest] = value
    if fields:
        fields["chatgpt_session_last_validated_at"] = time.time()
    return fields


def _is_main_account_email(email: str | None) -> bool:
    return bool(_normalized_email(email)) and _normalized_email(email) == _normalized_email(get_admin_email())


def _account_looks_autoteam_created(acc: dict | None) -> bool:
    acc = acc or {}
    explicit_managed = _explicit_managed_flag(acc)
    if explicit_managed is not None:
        return explicit_managed
    if str(acc.get("created_by") or "").lower() == "autoteam":
        return True
    return acc.get("mail_account_id") is not None or acc.get("cloudmail_account_id") is not None


_GOOGLE_AUTO_REUSE_DOMAINS = {"gmail.com", "googlemail.com"}


def _get_account_login_provider(acc: dict | None) -> str:
    acc = acc or {}
    for key in ("login_provider", "auth_provider", "oauth_provider"):
        provider = (acc.get(key) or "").strip().lower()
        if provider:
            return provider

    email = _normalized_email(acc.get("email"))
    if "@" in email and email.rsplit("@", 1)[-1] in _GOOGLE_AUTO_REUSE_DOMAINS:
        return "google"

    return ""


def _auto_reuse_skip_reason(acc: dict | None) -> str | None:
    provider = _get_account_login_provider(acc)
    if provider == "google":
        return "Google 登录账号暂不支持自动复用"
    return None


def _get_account_mail_client(acc: dict | None):
    acc = acc or {}
    has_explicit_mail_binding = (
        bool(acc.get("mail_service_id")) or bool(acc.get("mail_provider")) or acc.get("mail_account_id") is not None
    )
    has_legacy_cloudmail_binding = acc.get("cloudmail_account_id") is not None
    if has_explicit_mail_binding or has_legacy_cloudmail_binding or infer_mail_service_from_email(acc.get("email")):
        return _with_pending_invite_forwarding(get_mail_client_for_account(acc), acc=acc)

    services = get_mail_services()
    if len(services) == 1:
        return _with_pending_invite_forwarding(get_mail_client(service=services[0]), acc=acc)
    if not services:
        return _with_pending_invite_forwarding(CloudMailClient(), acc=acc)

    email = _normalized_email(acc.get("email"))
    raise ValueError(
        f"无法唯一确定账号 {email or '<unknown>'} 对应的邮箱服务，请补充 mail_service_id 或确保邮箱域名只匹配一个服务"
    )


def _account_mail_cache_key(acc: dict | None) -> str:
    acc = acc or {}
    service_id = get_account_mail_service_id(acc)
    if service_id:
        return f"service:{service_id}"
    provider = get_account_mail_provider(acc, default_provider="")
    if provider:
        return f"provider:{provider}"
    return "default"


def _can_attempt_auth_repair(acc: dict | None, mail_domain_suffix: str = "") -> bool:
    acc = acc or {}
    if (
        bool(acc.get("mail_service_id"))
        or bool(acc.get("mail_provider"))
        or acc.get("mail_account_id") is not None
        or acc.get("cloudmail_account_id") is not None
    ):
        return True
    if infer_mail_service_from_email(acc.get("email")):
        return True
    email = _normalized_email(acc.get("email"))
    return bool(mail_domain_suffix and mail_domain_suffix in email)


def _has_auth_file(acc: dict | None) -> bool:
    acc = acc or {}
    auth_file = (acc.get("auth_file") or "").strip()
    return bool(auth_file) and Path(auth_file).exists()


def _pool_active_target(team_target: int) -> int:
    return max(0, int(team_target) - 1)


def _count_pool_active_accounts(accounts: list[dict] | None = None, *, require_auth: bool = False) -> int:
    accounts = accounts if accounts is not None else load_accounts()
    count = 0
    for acc in accounts:
        if _is_main_account_email(acc.get("email")) or is_account_disabled(acc) or acc.get("status") != STATUS_ACTIVE:
            continue
        if require_auth and not _has_auth_file(acc):
            continue
        count += 1
    return count


def _count_local_team_seat_accounts(accounts: list[dict] | None = None) -> int:
    accounts = accounts if accounts is not None else load_accounts()
    seat_statuses = {STATUS_ACTIVE, STATUS_EXHAUSTED, STATUS_AUTH_PENDING}
    return sum(
        1 for acc in accounts if not _is_main_account_email(acc.get("email")) and acc.get("status") in seat_statuses
    )


def _estimate_local_team_member_count(team_target: int, accounts: list[dict] | None = None) -> int:
    accounts = accounts if accounts is not None else load_accounts()
    reserved_main = 1 if int(team_target) > 0 else 0
    return _count_local_team_seat_accounts(accounts) + reserved_main


def _set_auth_pending_or_standby(email: str) -> str:
    if _is_email_in_team(email):
        update_account(email, status=STATUS_AUTH_PENDING)
        return STATUS_AUTH_PENDING
    update_account(email, status=STATUS_STANDBY, **_auth_repair_reset_fields())
    return STATUS_STANDBY


def _auth_repair_reset_fields() -> dict:
    return {
        "auth_retry_count": 0,
        "auth_last_error": None,
        "auth_last_error_detail": None,
        "auth_last_failed_at": None,
        "auth_retry_after": None,
        "auth_retry_paused": False,
    }


def _auth_repair_retry_delays() -> tuple[int, int, int]:
    from autoteam.config import AUTO_CHECK_INTERVAL

    interval = AUTO_CHECK_INTERVAL
    try:
        from autoteam.api import _auto_check_config

        interval = int(_auto_check_config.get("interval", interval) or interval)
    except Exception:
        pass

    interval = max(60, int(interval))
    return (interval * 2, interval * 4, interval * 6)


def _auth_repair_retry_add_phone_enabled() -> bool:
    from autoteam.config import AUTO_CHECK_RETRY_ADD_PHONE

    enabled = AUTO_CHECK_RETRY_ADD_PHONE
    try:
        from autoteam.api import _auto_check_config

        enabled = bool(_auto_check_config.get("retry_add_phone", enabled))
    except Exception:
        pass

    return bool(enabled)


def _auth_repair_add_phone_max_retries() -> int:
    from autoteam.config import AUTO_CHECK_ADD_PHONE_MAX_RETRIES

    retries = AUTO_CHECK_ADD_PHONE_MAX_RETRIES
    try:
        from autoteam.api import _auto_check_config

        retries = int(_auto_check_config.get("add_phone_max_retries", retries) or retries)
    except Exception:
        pass

    return max(1, int(retries))


def _auth_repair_add_phone_retry_delays(max_retries: int | None = None) -> tuple[int, ...]:
    from autoteam.config import AUTO_CHECK_INTERVAL

    interval = AUTO_CHECK_INTERVAL
    try:
        from autoteam.api import _auto_check_config

        interval = int(_auto_check_config.get("interval", interval) or interval)
    except Exception:
        pass

    retries = _auth_repair_add_phone_max_retries() if max_retries is None else max_retries
    interval = max(60, int(interval))
    retries = max(1, int(retries))
    return tuple(interval * (2**idx) for idx in range(retries))


def _auth_repair_error_label(error_type: str | None) -> str:
    mapping = {
        "add_phone": "手机号验证",
        "account_deactivated": "账号已删除或停用",
        "choose_account_selection": "账号选择未完成",
        "human_verification": "人机验证",
        "email_verification": "邮箱验证码页卡住",
        "workspace_selection": "workspace 选择未完成",
        "login_state_lost": "登录态丢失",
        "site_unavailable": "站点不可用/代理异常",
        "token_exchange_failed": "token 交换失败",
        "non_team_plan": "未进入 Team workspace",
        "auth_code_missing": "未获取到 auth code",
        "login_failed": "登录失败",
        "exception": "登录异常",
        "member_session_missing": "缺少 member session_token",
        "member_session_invalid": "member session_token 已失效",
        "pat_export_failed": "PAT 导出失败",
        "google_signin_redirect": "误入 Google 登录页",
        "email_submit_failed": "邮箱提交失败",
        "verification_mail_missing": "未收到验证码邮件",
        "verification_input_missing": "验证码输入框缺失",
        "workspace_join_incomplete": "加入 workspace 未完成",
        "registration_failed": "注册失败",
    }
    return mapping.get(error_type or "", error_type or "未知错误")


def _auth_repair_state_suffix(state: dict | None) -> str:
    state = state or {}
    if state.get("auth_retry_paused"):
        return "，已暂停自动修复"
    retry_after = state.get("auth_retry_after")
    if retry_after:
        mins = max(1, int((retry_after - time.time() + 59) // 60))
        return f"，约 {mins} 分钟后重试"
    return ""


def _auth_repair_reset(email: str):
    update_account(email, **_auth_repair_reset_fields())


def _release_auth_repair_team_seat(email: str, *, chatgpt_api=None) -> str:
    logger.warning("[认证修复] swap_seat-only 模式禁止释放/移出 Team 席位，跳过: %s", email)
    return "disabled"


def _auth_repair_result_suffix(result: dict | None) -> str:
    result = result or {}
    suffix = _auth_repair_state_suffix(result)
    if result.get("seat_released"):
        return f"{suffix}，已释放 Team 席位"
    if result.get("release_attempted") and result.get("remove_status") == "failed":
        return f"{suffix}，释放 Team 席位失败"
    return suffix


def _auth_repair_skip_reason(acc: dict | None, *, force: bool = False, now: float | None = None) -> str | None:
    if force or not acc:
        return None

    if acc.get("auth_retry_paused"):
        label = _auth_repair_error_label(acc.get("auth_last_error"))
        return f"已暂停自动修复（{label}）"

    retry_after = acc.get("auth_retry_after")
    now = time.time() if now is None else now
    if retry_after and retry_after > now:
        remain_secs = max(0, int(retry_after - now))
        remain_mins = max(1, (remain_secs + 59) // 60)
        label = _auth_repair_error_label(acc.get("auth_last_error"))
        return f"自动修复冷却中（{label}，约 {remain_mins} 分钟后重试）"
    return None


def _auth_repair_quota_skip_reason(acc: dict | None, *, now: float | None = None) -> str | None:
    """只在账号仍处于未重置的耗尽窗口时跳过修复。"""
    acc = acc or {}
    now = time.time() if now is None else now

    hold = _standby_reuse_hold_info(acc, now=now)
    if hold:
        window = _quota_window_label(hold.get("window") or "")
        return f"{window}额度已耗尽，等待重置后再修复"

    if acc.get("status") == STATUS_EXHAUSTED:
        # 没有可用重置时间时保守跳过，避免把明确耗尽的账号反复拉起。
        return "账号已标记为额度耗尽"

    exhausted_info = _pending_historical_exhausted_info(acc.get("last_quota"), now=now)
    if exhausted_info:
        window = _quota_window_label(exhausted_info.get("window") or "")
        return f"{window}额度已耗尽，等待重置后再修复"

    return None


def _should_recapture_member_session_for_pat(acc: dict | None, result: dict | None) -> bool:
    """判断是否应打开 CloakBrowser 重新登录 member 并捕获 session。"""
    acc = acc or {}
    result = result or {}
    if _is_main_account_email(acc.get("email")) or is_account_disabled(acc):
        return False
    if not _allow_member_email_login_for_pat():
        return False
    if not str(acc.get("password") or "").strip():
        return False
    if not _can_attempt_auth_repair(acc):
        return False
    if _auth_repair_quota_skip_reason(acc):
        return False
    error_type = str(result.get("error_type") or "")
    return error_type in {"member_session_missing", "member_session_invalid", "pat_export_failed"}


def _cpa_auth_name_candidates(auth_entry: dict | None) -> set[str]:
    auth_entry = auth_entry or {}
    names = set()
    for key in ("name", "id", "path"):
        value = str(auth_entry.get(key) or "").strip()
        if value:
            names.add(Path(value).name)
    return names


def _active_account_cpa_auth_error(acc: dict | None, cpa_files: list[dict] | None = None, *, team_context=None) -> bool:
    """主动验证 active 账号的自管 CPA auth 是否已经 token_revoked/401。"""
    acc = acc or {}
    if acc.get("status") != STATUS_ACTIVE or not _has_auth_file(acc):
        return False
    auth_name = Path(str(acc.get("auth_file") or "")).name
    if not auth_name:
        return False
    cpa_files = cpa_files if cpa_files is not None else list_cpa_files()
    auth = next((item for item in cpa_files if auth_name in _cpa_auth_name_candidates(item)), None)
    if not auth or not is_managed_cpa_auth(auth):
        return False

    status, info = check_cpa_codex_quota(auth, account_id=_account_chatgpt_account_id(acc, team_context=team_context))
    if status != "auth_error":
        return False

    detail = ""
    if isinstance(info, dict):
        detail = str(info.get("error") or info.get("body") or info.get("status_code") or "")
    update_account(
        _normalized_email(acc.get("email")),
        status=STATUS_AUTH_PENDING,
        auth_last_error="cpa_auth_error",
        auth_last_error_detail=detail[:300] if detail else "CPA quota 检查鉴权失败",
        auth_last_failed_at=time.time(),
        managed_by_autoteam=True,
    )
    return True


def _disable_account_managed_cpa_auth(acc_or_email) -> str:
    """停用账号当前 auth_file 对应的 AutoTeam 自管 CPA auth。"""
    if isinstance(acc_or_email, dict):
        acc = acc_or_email
    else:
        acc = find_account(load_accounts(), _normalized_email(str(acc_or_email))) or {}
    auth_name = Path(str(acc.get("auth_file") or "")).name
    if not auth_name:
        return "missing_auth_file"
    if not is_managed_cpa_auth(auth_name):
        return "unmanaged_auth"
    set_cpa_auth_disabled(auth_name, True)
    return "disabled"


def _restore_pat_repair_promoted_seat(chatgpt_api, user_id: str, original_seat_type, email: str) -> str:
    """PAT 修复失败时只恢复本轮临时升 GPT 前的明确 Codex seat。"""
    if not chatgpt_api or not user_id:
        return "missing_context"
    from autoteam.swap_seat import normalize_team_seat_type, team_seat_backend_value

    original_seat = normalize_team_seat_type(original_seat_type)
    if original_seat != "codex":
        logger.info("[PAT修复] 不回切 seat: %s 原 seat=%s", email, original_seat or "unknown")
        return "not_restored_original_not_codex"
    try:
        response = chatgpt_api.update_member_seat_type(user_id, team_seat_backend_value(original_seat))
        if int(response.get("status") or 0) in (200, 204):
            logger.info("[PAT修复] 已恢复临时 GPT seat -> Codex: %s", email)
            return "restored"
        logger.warning("[PAT修复] 恢复临时 GPT seat 失败 %s: HTTP %s", email, response.get("status"))
        return "failed"
    except Exception as exc:
        logger.warning("[PAT修复] 恢复临时 GPT seat 异常 %s: %s", email, exc)
        return "failed"


def _record_auth_repair_failure(
    email: str,
    error_type: str | None = None,
    error_detail: str | None = None,
    *,
    chatgpt_api=None,
    team_context=None,
    release_team_seat: bool = False,
) -> dict:
    now = time.time()
    acc = find_account(load_accounts(), email) or {"email": email}
    error_type = error_type or "login_failed"
    error_detail = error_detail or _auth_repair_error_label(error_type)
    retry_delays = _auth_repair_retry_delays()
    should_release_team_seat = bool(release_team_seat)

    if error_type == "add_phone" and _auth_repair_retry_add_phone_enabled():
        prev_count = int(acc.get("auth_retry_count") or 0) if acc.get("auth_last_error") == "add_phone" else 0
        next_count = prev_count + 1
        max_retries = _auth_repair_add_phone_max_retries()
        add_phone_delays = _auth_repair_add_phone_retry_delays(max_retries)

        if next_count > max_retries:
            state = {
                "auth_retry_count": next_count,
                "auth_last_error": error_type,
                "auth_last_error_detail": error_detail,
                "auth_last_failed_at": now,
                "auth_retry_after": None,
                "auth_retry_paused": True,
            }
            should_release_team_seat = True
        else:
            state = {
                "auth_retry_count": next_count,
                "auth_last_error": error_type,
                "auth_last_error_detail": error_detail,
                "auth_last_failed_at": now,
                "auth_retry_after": now + add_phone_delays[next_count - 1],
                "auth_retry_paused": False,
            }
    elif error_type in AUTH_REPAIR_HARD_FAILURE_TYPES or error_type == "add_phone":
        retry_count = max(int(acc.get("auth_retry_count") or 0), len(retry_delays))
        state = {
            "auth_retry_count": retry_count,
            "auth_last_error": error_type,
            "auth_last_error_detail": error_detail,
            "auth_last_failed_at": now,
            "auth_retry_after": None,
            "auth_retry_paused": True,
        }
    else:
        prev_count = int(acc.get("auth_retry_count") or 0)
        next_count = min(prev_count + 1, len(retry_delays))
        delay = retry_delays[max(0, next_count - 1)]
        retry_after = now + delay
        state = {
            "auth_retry_count": next_count,
            "auth_last_error": error_type,
            "auth_last_error_detail": error_detail,
            "auth_last_failed_at": now,
            "auth_retry_after": retry_after,
            "auth_retry_paused": False,
        }

    update_account(email, **state)

    disabled_auth_status = None
    if error_type in AUTH_REPAIR_HARD_FAILURE_TYPES:
        try:
            disabled_auth_status = _disable_account_managed_cpa_auth(acc)
            if disabled_auth_status == "disabled":
                logger.info("[认证修复] 已停用硬失败账号的自管 CPA auth: %s", email)
        except Exception as exc:
            disabled_auth_status = "failed"
            logger.warning("[认证修复] 停用硬失败账号的自管 CPA auth 失败 %s: %s", email, exc)

    is_team_member = _is_email_in_team(email) if team_context is None else _is_email_in_team(email, team_context=team_context)
    if not is_team_member and acc.get("status") in (STATUS_ACTIVE, STATUS_EXHAUSTED, STATUS_AUTH_PENDING):
        is_team_member = True

    release_attempted = False
    remove_status = None
    seat_released = False
    if should_release_team_seat and is_team_member:
        release_attempted = True
        remove_status = _release_auth_repair_team_seat(email, chatgpt_api=chatgpt_api)
        seat_released = remove_status in ("removed", "already_absent")

    final_status = STATUS_STANDBY if seat_released or not is_team_member else STATUS_AUTH_PENDING
    update_account(email, status=final_status)

    return {
        **state,
        "status": final_status,
        "seat_released": seat_released,
        "release_attempted": release_attempted,
        "remove_status": remove_status,
        "disabled_auth_status": disabled_auth_status,
    }


def _login_codex_with_result(
    email: str,
    password: str,
    *,
    mail_client=None,
    max_attempts: int = 3,
    signup_profile: SignupProfile | None = None,
) -> dict:
    max_attempts = max(1, int(max_attempts))

    def _single_attempt() -> dict:
        def _reject_non_team(bundle: dict | None) -> dict | None:
            if not isinstance(bundle, dict) or not bundle:
                return None
            plan_type = str(bundle.get("plan_type") or "").lower()
            if plan_type == "team":
                return None
            return {
                "ok": False,
                "bundle": None,
                "error_type": "non_team_plan",
                "error_detail": f"登录后 plan={plan_type or 'unknown'}，未进入 Team workspace",
                "retryable": True,
            }

        try:
            result = login_codex_via_browser(
                email,
                password,
                mail_client=mail_client,
                return_result=True,
                signup_profile=signup_profile,
            )
        except TypeError:
            try:
                result = login_codex_via_browser(email, password, mail_client=mail_client, return_result=True)
            except TypeError:
                bundle = login_codex_via_browser(email, password, mail_client=mail_client)
                non_team = _reject_non_team(bundle)
                if non_team:
                    return non_team
                return {
                    "ok": bool(bundle),
                    "bundle": bundle,
                    "error_type": None if bundle else "login_failed",
                    "error_detail": None if bundle else "登录失败",
                    "retryable": False if bundle else True,
                }
        except Exception as exc:
            return {
                "ok": False,
                "bundle": None,
                "error_type": "exception",
                "error_detail": str(exc),
                "retryable": True,
            }

        if isinstance(result, dict) and "ok" in result:
            non_team = _reject_non_team(result.get("bundle"))
            if result.get("ok") and non_team:
                return non_team
            return result

        non_team = _reject_non_team(result if isinstance(result, dict) else None)
        if non_team:
            return non_team

        return {
            "ok": bool(result),
            "bundle": result if result else None,
            "error_type": None if result else "login_failed",
            "error_detail": None if result else "登录失败",
            "retryable": False if result else True,
        }

    last_result = None
    for attempt in range(1, max_attempts + 1):
        result = _single_attempt()
        result["attempts"] = attempt
        if result.get("ok"):
            return result

        last_result = result
        error_type = result.get("error_type")
        retryable = bool(result.get("retryable"))
        if attempt >= max_attempts or not retryable or error_type in AUTH_REPAIR_SINGLE_ATTEMPT_FAILURE_TYPES:
            return result

        logger.warning(
            "[Codex] %s 登录未完成（%s），准备在本轮重试第 %d/%d 次",
            email,
            _auth_repair_error_label(error_type),
            attempt + 1,
            max_attempts,
        )

    return last_result or {
        "ok": False,
        "bundle": None,
        "error_type": "login_failed",
        "error_detail": "登录失败",
        "retryable": True,
        "attempts": max_attempts,
    }


def _create_pat_auth_with_login(
    email: str,
    password: str,
    *,
    mail_client=None,
    signup_profile: SignupProfile | None = None,
    team_context=None,
) -> dict:
    """登录成员账号后重新创建 Codex PAT，并上传 CPA。"""
    from autoteam.codex_pat_export import capture_chatgpt_session_from_page, create_save_upload_codex_auth_from_session

    captured_session_info = {}

    def _export(page):
        nonlocal captured_session_info
        session_info = capture_chatgpt_session_from_page(page)
        captured_session_info = session_info
        auth = create_save_upload_codex_auth_from_session(
            session_info["chatgpt_session_token"],
            account_id=_team_or_session_account_id(session_info, team_context),
            email=email,
            access_token=session_info.get("access_token"),
            ttl_days=30,
            upload=True,
            main=False,
        )
        return auth

    result = login_codex_via_browser(
        email,
        password,
        mail_client=mail_client,
        return_result=True,
        signup_profile=signup_profile,
        pat_export_callback=_export,
        pat_only=True,
    )
    if not isinstance(result, dict) or not result.get("ok"):
        return {
            "ok": False,
            "error_type": (result or {}).get("error_type") if isinstance(result, dict) else "pat_export_failed",
            "error_detail": (result or {}).get("error_detail") if isinstance(result, dict) else "PAT 导出失败",
            "retryable": True,
        }
    bundle = result.get("bundle") if isinstance(result.get("bundle"), dict) else {}
    pat_auth = bundle.get("pat_auth") if isinstance(bundle.get("pat_auth"), dict) else {}
    if not pat_auth.get("auth_file"):
        return {"ok": False, "error_type": "pat_export_failed", "error_detail": "PAT 导出成功但缺少 auth_file", "retryable": True}
    return {"ok": True, "auth": pat_auth, "session": captured_session_info}


def _create_pat_auth_with_session(acc: dict, *, team_context=None) -> dict:
    """用账号池里保存的 member session_token 纯 API 重建 PAT。"""
    from autoteam.codex_pat_export import create_save_upload_codex_auth_from_session

    email = _normalized_email(acc.get("email"))
    session_token = _account_chatgpt_session_token(acc)
    if not session_token:
        return {
            "ok": False,
            "error_type": "member_session_missing",
            "error_detail": "缺少 member session_token；已按配置跳过接码登录",
            "retryable": False,
        }
    try:
        auth = create_save_upload_codex_auth_from_session(
            session_token,
            account_id=_account_chatgpt_account_id(acc, team_context=team_context),
            email=email,
            ttl_days=30,
            upload=True,
            main=False,
        )
        return {"ok": True, "auth": auth}
    except Exception as exc:
        detail = str(exc)
        lower = detail.lower()
        error_type = "member_session_invalid" if "401" in lower or "unauthorized" in lower or "session_token" in lower else "pat_export_failed"
        return {
            "ok": False,
            "error_type": error_type,
            "error_detail": detail,
            "retryable": error_type != "member_session_invalid",
        }


def cmd_repair_pat_auths(
    *,
    force: bool = False,
    limit: int = 3,
    target_email: str | None = None,
    team_context=None,
    allow_promote_for_use: bool = False,
) -> dict:
    """巡检修复：对 auth_pending / CPA auth_error 的 AutoTeam 账号重新生成 PAT。"""
    _abort_if_cancel_requested()
    target_email = _normalized_email(target_email)
    accounts = load_accounts()
    candidates = []
    skipped = []
    now = time.time()
    cpa_files_cache = None
    for acc in accounts:
        email = _normalized_email(acc.get("email"))
        if not email or _is_main_account_email(email) or is_account_disabled(acc):
            continue
        if target_email and email != target_email:
            continue
        if not _account_matches_team_context(acc, team_context):
            if target_email:
                skipped.append({"email": email, "reason": "team_context_mismatch"})
            continue
        if not _account_looks_autoteam_created(acc):
            continue
        status = str(acc.get("status") or "")
        last_error = str(acc.get("auth_last_error") or "")
        active_cpa_auth_error = False
        if status == STATUS_ACTIVE and _has_auth_file(acc):
            try:
                if cpa_files_cache is None:
                    cpa_files_cache = list_cpa_files()
                active_cpa_auth_error = _active_account_cpa_auth_error(acc, cpa_files_cache, team_context=team_context)
                if active_cpa_auth_error:
                    logger.info("[PAT修复] %s active CPA auth 已失效，纳入 PAT 重建", email)
            except Exception as exc:
                logger.warning("[PAT修复] 主动验证 active CPA auth 失败 %s: %s", email, exc)
        if status != STATUS_AUTH_PENDING and not last_error.startswith("cpa_auth_error") and not active_cpa_auth_error:
            continue
        quota_skip_reason = _auth_repair_quota_skip_reason(acc, now=now)
        if quota_skip_reason:
            logger.info("[PAT修复] 跳过 %s: %s", email, quota_skip_reason)
            skipped.append({"email": email, "reason": "quota_exhausted"})
            continue
        # 未耗尽账号要尽量重新登录继续使用，不因旧冷却/暂停状态跳过；
        # account_deactivated 这类硬失败仍保守跳过。
        bypass_backoff = str(acc.get("auth_last_error") or "") not in {"account_deactivated"}
        skip_reason = _auth_repair_skip_reason(acc, force=force or bypass_backoff, now=now)
        if skip_reason:
            logger.info("[PAT修复] 跳过 %s: %s", email, skip_reason)
            skipped.append({"email": email, "reason": skip_reason})
            continue
        has_session = bool(_account_chatgpt_session_token(acc))
        if not has_session and not _allow_member_email_login_for_pat():
            reason = "缺少 member session_token；已通过 AUTO_CHECK_DISABLE_MEMBER_EMAIL_LOGIN_FOR_PAT 禁用重新捕获"
            logger.info("[PAT修复] 跳过 %s: %s", email, reason)
            skipped.append({"email": email, "reason": "member_session_missing"})
            continue
        if not has_session and not str(acc.get("password") or "").strip():
            reason = "缺少 member session_token 和账号密码，无法重建 PAT"
            logger.info("[PAT修复] 跳过 %s: %s", email, reason)
            skipped.append({"email": email, "reason": "missing_session_and_password"})
            continue
        if not has_session and not _can_attempt_auth_repair(acc):
            reason = "缺少邮箱服务绑定，无法读取验证码"
            logger.info("[PAT修复] 跳过 %s: %s", email, reason)
            skipped.append({"email": email, "reason": "mail_binding_missing"})
            continue
        candidates.append(acc)

    if not candidates:
        return {
            "mode": "repair_pat_auths",
            "scanned": len(accounts),
            "candidates": 0,
            "repaired": [],
            "failed": [],
            "skipped": skipped,
            "summary": {
                "repaired": 0,
                "failed": 0,
                "skipped": len(skipped),
                "remaining_candidates": 0,
            },
        }

    repair_chatgpt = None
    member_by_email = {}
    try:
        repair_chatgpt = ChatGPTTeamAPI()
        _start_chatgpt_for_team(repair_chatgpt, team_context)
        members = _fetch_team_members_for_account(
            repair_chatgpt,
            _team_account_id(team_context) or getattr(repair_chatgpt, "account_id", ""),
        )
        member_by_email = {_normalized_email(item.get("email")): item for item in members if _normalized_email(item.get("email"))}
    except Exception as exc:
        logger.warning("[PAT修复] 无法读取 Team seat 状态，本轮不修复，避免给非 GPT seat 创建 PAT: %s", exc)
        repair_chatgpt = None

    if not member_by_email:
        for acc in candidates:
            skipped.append({"email": _normalized_email(acc.get("email")), "reason": "team_member_missing_or_unreadable"})
        candidates = []
    else:
        filtered_candidates = []
        for acc in candidates:
            email = _normalized_email(acc.get("email"))
            member = member_by_email.get(email) or {}
            if not member:
                skipped.append({"email": email, "reason": "team_member_missing"})
                continue
            try:
                from autoteam.swap_seat import normalize_team_seat_type

                current_seat = normalize_team_seat_type(member.get("seat_type"))
            except Exception:
                current_seat = str(member.get("seat_type") or "").strip().lower()
            if current_seat == "chatgpt" or allow_promote_for_use:
                filtered_candidates.append(acc)
                continue
            logger.info("[PAT修复] 跳过 %s: 当前不是 GPT seat，等到需要启用时再修复 PAT", email)
            skipped.append({"email": email, "reason": "not_chatgpt_seat"})
        candidates = filtered_candidates

    if not candidates:
        if _chatgpt_session_ready(repair_chatgpt):
            try:
                repair_chatgpt.stop()
            except Exception:
                pass
        return {
            "mode": "repair_pat_auths",
            "scanned": len(accounts),
            "candidates": 0,
            "repaired": [],
            "failed": [],
            "skipped": skipped,
            "summary": {
                "repaired": 0,
                "failed": 0,
                "skipped": len(skipped),
                "remaining_candidates": 0,
            },
        }

    repaired = []
    failed = []
    for acc in candidates[: max(1, int(limit or 1))]:
        _abort_if_cancel_requested()
        email = _normalized_email(acc.get("email"))
        previous_auth_name = Path(str(acc.get("auth_file") or "")).name
        seat_promoted = False
        member = member_by_email.get(email) or {}
        user_id = str(member.get("user_id") or member.get("id") or "").strip()
        original_seat_type = member.get("seat_type")
        promote_required = False
        if repair_chatgpt and user_id:
            try:
                from autoteam.swap_seat import normalize_team_seat_type

                promote_required = normalize_team_seat_type(original_seat_type) != "chatgpt"
                if promote_required:
                    response = repair_chatgpt.update_member_seat_type(user_id, "default")
                    seat_promoted = int(response.get("status") or 0) in (200, 204)
                    if seat_promoted:
                        logger.info("[PAT修复] 已临时切到 GPT seat 以创建 PAT: %s", email)
                        time.sleep(2)
            except Exception as exc:
                logger.warning("[PAT修复] 临时切 GPT seat 失败 %s: %s", email, exc)
        elif repair_chatgpt:
            try:
                from autoteam.swap_seat import normalize_team_seat_type

                promote_required = normalize_team_seat_type(original_seat_type) != "chatgpt"
            except Exception:
                promote_required = str(original_seat_type or "").strip().lower() != "default"
        if promote_required and not seat_promoted:
            state = _record_auth_repair_failure(
                email,
                "seat_promote_failed",
                "无法先切换到 GPT seat，已跳过 PAT 修复",
                team_context=team_context,
            )
            failed.append({"email": email, "error": "seat_promote_failed", "state": state})
            continue
        try:
            if _account_chatgpt_session_token(acc):
                result = _create_pat_auth_with_session(acc, team_context=team_context)
                if not result.get("ok") and _should_recapture_member_session_for_pat(acc, result):
                    logger.info("[PAT修复] %s 保存的 session 不可用，改用 CloakBrowser 重新捕获 session", email)
                    mail_client = _get_account_mail_client(acc)
                    login = getattr(mail_client, "login", None)
                    if callable(login):
                        login()
                    result = _create_pat_auth_with_login(
                        email,
                        str(acc.get("password") or ""),
                        mail_client=mail_client,
                        signup_profile=generate_signup_profile(),
                        team_context=team_context,
                    )
            else:
                mail_client = _get_account_mail_client(acc)
                login = getattr(mail_client, "login", None)
                if callable(login):
                    login()
                result = _create_pat_auth_with_login(
                    email,
                    str(acc.get("password") or ""),
                    mail_client=mail_client,
                    signup_profile=generate_signup_profile(),
                    team_context=team_context,
                )
            if not result.get("ok"):
                if seat_promoted and repair_chatgpt and user_id:
                    _restore_pat_repair_promoted_seat(repair_chatgpt, user_id, original_seat_type, email)
                state = _record_auth_repair_failure(
                    email,
                    result.get("error_type") or "pat_export_failed",
                    result.get("error_detail") or "PAT 导出失败",
                    team_context=team_context,
                )
                failed.append({"email": email, "error": result.get("error_detail"), "state": state})
                continue
            auth_result = result["auth"]
            session_updates = _chatgpt_session_update_fields(result.get("session"))
            if _team_account_id(team_context):
                session_updates["chatgpt_account_id"] = _team_account_id(team_context)
            new_auth_name = Path(str(auth_result.get("auth_file") or auth_result.get("filename") or "")).name
            if previous_auth_name and new_auth_name and previous_auth_name != new_auth_name:
                try:
                    if is_managed_cpa_auth(previous_auth_name):
                        set_cpa_auth_disabled(previous_auth_name, True)
                        logger.info("[PAT修复] 已停用旧自管 CPA auth: %s", previous_auth_name)
                except Exception as exc:
                    logger.warning("[PAT修复] 停用旧自管 CPA auth 失败 %s: %s", previous_auth_name, exc)
            update_account(
                email,
                status=STATUS_ACTIVE,
                auth_file=auth_result.get("auth_file"),
                auth_last_error=None,
                auth_last_error_detail=None,
                auth_last_failed_at=None,
                auth_retry_after=None,
                auth_retry_paused=False,
                auth_retry_count=0,
                managed_by_autoteam=True,
                last_active_at=time.time(),
                **session_updates,
            )
            repaired.append({"email": email, "filename": auth_result.get("filename")})
            logger.info("[PAT修复] %s 已重新生成并上传 PAT: %s", email, auth_result.get("filename"))
        except Exception as exc:
            if seat_promoted and repair_chatgpt and user_id:
                _restore_pat_repair_promoted_seat(repair_chatgpt, user_id, original_seat_type, email)
            state = _record_auth_repair_failure(email, "pat_export_failed", str(exc), team_context=team_context)
            failed.append({"email": email, "error": str(exc), "state": state})

    if _chatgpt_session_ready(repair_chatgpt):
        try:
            repair_chatgpt.stop()
        except Exception:
            pass

    return {
        "mode": "repair_pat_auths",
        "scanned": len(accounts),
        "candidates": len(candidates),
        "repaired": repaired,
        "failed": failed,
        "skipped": skipped,
        "summary": {
            "repaired": len(repaired),
            "failed": len(failed),
            "skipped": len(skipped),
            "remaining_candidates": max(0, len(candidates) - len(candidates[: max(1, int(limit or 1))])),
        },
    }


def sync_account_states(chatgpt_api=None):
    """根据 Team 实际成员列表同步本地账号状态"""
    account_id = get_chatgpt_account_id()
    if not account_id:
        return
    accounts = load_accounts()
    team_emails = set()
    member_by_email = {}

    # 获取 Team 实际成员
    need_stop = False
    if not _chatgpt_session_ready(chatgpt_api):
        try:
            chatgpt_api = ChatGPTTeamAPI()
            chatgpt_api.start()
            need_stop = True
        except Exception:
            # Playwright 不可用（event loop 冲突等），跳过同步
            return

    try:
        members = _fetch_team_members_for_account(chatgpt_api, account_id)
        for member in members:
            email = (member.get("email") or "").lower()
            if not email:
                continue
            team_emails.add(email)
            member_by_email[email] = member
    finally:
        if need_stop:
            chatgpt_api.stop()

    # 对照更新状态
    changed = False
    local_email_set = {a["email"].lower() for a in accounts}

    for acc in accounts:
        email = acc["email"].lower()
        live_member = member_by_email.get(email) or {}
        live_seat_type = live_member.get("seat_type")
        inferred_service_id = infer_mail_service_from_email(email)
        inferred_provider = infer_mail_provider_from_email(email)
        if inferred_service_id and not acc.get("mail_service_id"):
            acc["mail_service_id"] = inferred_service_id
            changed = True
        if (
            inferred_provider
            and not acc.get("mail_provider")
            and acc.get("mail_account_id") is None
            and acc.get("cloudmail_account_id") is None
        ):
            acc["mail_provider"] = inferred_provider
            changed = True
        if live_seat_type != acc.get("seat_type"):
            acc["seat_type"] = live_seat_type or None
            changed = True
        in_team = email in team_emails

        if in_team:
            if acc["status"] == STATUS_EXHAUSTED:
                continue
            desired_status = STATUS_ACTIVE if _has_auth_file(acc) else STATUS_AUTH_PENDING
            if is_account_disabled(acc):
                if acc["status"] in (STATUS_ACTIVE, STATUS_AUTH_PENDING):
                    continue
                if acc["status"] != desired_status:
                    acc["status"] = desired_status
                    changed = True
                continue
            if acc["status"] == STATUS_AUTH_PENDING:
                continue

            if acc["status"] != desired_status:
                acc["status"] = desired_status
                changed = True
        elif acc["status"] in (STATUS_ACTIVE, STATUS_EXHAUSTED, STATUS_AUTH_PENDING):
            acc["status"] = STATUS_STANDBY
            acc["seat_type"] = None
            acc.update(_auth_repair_reset_fields())
            changed = True

    # Team 中有我们域名但本地无记录的成员 → 自动添加
    for email in team_emails:
        if _is_main_account_email(email) or email in local_email_set:
            continue
        inferred_service_id = infer_mail_service_from_email(email)
        inferred_provider = infer_mail_provider_from_email(email)
        if not inferred_provider:
            continue
        accounts.append(
            {
                "email": email,
                "password": "",
                "mail_service_id": inferred_service_id or None,
                "mail_provider": inferred_provider,
                "mail_account_id": None,
                "cloudmail_account_id": None,
                "status": STATUS_AUTH_PENDING,
                "seat_type": (member_by_email.get(email) or {}).get("seat_type") or None,
                "auth_file": None,
                "quota_exhausted_at": None,
                "quota_resets_at": None,
                "created_at": time.time(),
                "last_active_at": None,
                **_auth_repair_reset_fields(),
            }
        )
        changed = True
        logger.info("[同步] 发现 Team 中新成员: %s（已添加到本地，状态=auth_pending）", email)

    # auths 目录中有认证文件但本地无记录的 → 自动添加为 standby
    from autoteam.codex_auth import AUTH_DIR

    local_email_set = {a["email"].lower() for a in accounts}  # 刷新一下
    if AUTH_DIR.exists():
        for auth_file in AUTH_DIR.glob("codex-*.json"):
            try:
                auth_data = json.loads(read_text(auth_file))
                email = auth_data.get("email", "").lower()
                if not email or email in local_email_set or _is_main_account_email(email):
                    continue
                # 判断是否在 Team 中
                in_team = email in team_emails
                status = STATUS_ACTIVE if in_team else STATUS_STANDBY
                inferred_service_id = infer_mail_service_from_email(email)
                accounts.append(
                    {
                        "email": email,
                        "password": "",
                        "mail_service_id": inferred_service_id or None,
                        "mail_provider": infer_mail_provider_from_email(email),
                        "mail_account_id": None,
                        "cloudmail_account_id": None,
                        "status": status,
                        "seat_type": (member_by_email.get(email) or {}).get("seat_type") if in_team else None,
                        "auth_file": str(auth_file),
                        "quota_exhausted_at": None,
                        "quota_resets_at": None,
                        "created_at": time.time(),
                        "last_active_at": None,
                        **_auth_repair_reset_fields(),
                    }
                )
                local_email_set.add(email)
                changed = True
                logger.info("[同步] 从 auths 目录恢复账号: %s（%s）", email, status)
            except Exception:
                continue

    if changed:
        save_accounts(accounts)


def _print_status_table(accounts, quota_cache=None):
    """打印账号状态表格（使用 rich）"""
    from rich.console import Console
    from rich.table import Table
    from rich.text import Text

    if quota_cache is None:
        quota_cache = {}

    console = Console(width=120)

    table = Table(
        title="AutoTeam 账号状态",
        show_header=True,
        header_style="bold cyan",
        border_style="dim",
        title_style="bold white",
        padding=(0, 1),
        expand=True,
    )

    table.add_column("#", style="dim", width=3, justify="right")
    table.add_column("邮箱", style="white", no_wrap=True)
    table.add_column("状态", justify="center", width=10)
    table.add_column("5h 剩余", justify="right", width=8)
    table.add_column("周 剩余", justify="right", width=8)
    table.add_column("5h 重置", justify="center", width=12)
    table.add_column("周 重置", justify="center", width=12)

    STATUS_STYLE = {
        STATUS_ACTIVE: ("bold green", "● active"),
        STATUS_AUTH_PENDING: ("bold cyan", "◐ auth pending"),
        STATUS_EXHAUSTED: ("bold red", "✗ used up"),
        STATUS_STANDBY: ("yellow", "○ standby"),
        STATUS_PENDING: ("dim", "… pending"),
        "disabled": ("bold magenta", "◌ disabled"),
    }

    for idx, acc in enumerate(accounts, 1):
        email = acc["email"]
        qi = quota_cache.get(email) or acc.get("last_quota")
        status = "disabled" if not _is_main_account_email(email) and is_account_disabled(acc) else acc["status"]

        style, status_label = STATUS_STYLE.get(status, ("dim", status))
        status_text = Text(status_label, style=style)

        if qi:
            p_val = 100 - qi.get("primary_pct", 0)
            w_val = 100 - qi.get("weekly_pct", 0)
            p_pct = Text(f"{p_val}%", style="green" if p_val > 30 else "yellow" if p_val > 0 else "red")
            w_pct = Text(f"{w_val}%", style="green" if w_val > 30 else "yellow" if w_val > 0 else "red")
            p_reset = (
                time.strftime("%m-%d %H:%M", time.localtime(qi["primary_resets_at"]))
                if qi.get("primary_resets_at")
                else "-"
            )
            w_reset = (
                time.strftime("%m-%d %H:%M", time.localtime(qi["weekly_resets_at"]))
                if qi.get("weekly_resets_at")
                else "-"
            )
        else:
            p_pct = Text("-", style="dim")
            w_pct = Text("-", style="dim")
            p_reset = "-"
            w_reset = "-"

        table.add_row(
            str(idx),
            email,
            status_text,
            p_pct,
            w_pct,
            Text(p_reset, style="dim"),
            Text(w_reset, style="dim"),
        )

    console.print()
    console.print(table)

    # 统计摘要
    active = sum(1 for a in accounts if not is_account_disabled(a) and a["status"] == STATUS_ACTIVE)
    auth_pending = sum(1 for a in accounts if not is_account_disabled(a) and a["status"] == STATUS_AUTH_PENDING)
    standby = sum(1 for a in accounts if not is_account_disabled(a) and a["status"] == STATUS_STANDBY)
    exhausted = sum(1 for a in accounts if not is_account_disabled(a) and a["status"] == STATUS_EXHAUSTED)
    disabled = sum(1 for a in accounts if not _is_main_account_email(a.get("email")) and is_account_disabled(a))
    console.print(
        f"  [green]● 活跃 {active}[/]  "
        f"[cyan]◐ 认证待修复 {auth_pending}[/]  "
        f"[yellow]○ 待命 {standby}[/]  "
        f"[red]✗ 用完 {exhausted}[/]  "
        f"[magenta]◌ 禁用 {disabled}[/]  "
        f"[dim]总计 {len(accounts)}[/]",
    )


def cmd_status():
    """显示所有账号状态（先同步 Team 实际状态，active 账号实时查询额度）"""
    logger.info("[状态] 同步 Team 实际状态...")
    sync_account_states()

    accounts = load_accounts()
    if not accounts:
        logger.info("[状态] 暂无账号")
        return

    # active 账号实时查询额度
    quota_cache = {}
    active_count = sum(
        1 for a in accounts if a["status"] == STATUS_ACTIVE and a.get("auth_file") and Path(a["auth_file"]).exists()
    )
    if active_count:
        logger.info("[状态] 查询 %d 个 active 账号额度...", active_count)
    for acc in accounts:
        if acc["status"] == STATUS_ACTIVE and acc.get("auth_file") and Path(acc["auth_file"]).exists():
            auth_data = json.loads(read_text(Path(acc["auth_file"])))
            access_token = auth_data.get("access_token")
            if access_token:
                status, info = check_codex_quota(access_token)
                if status == "ok" and isinstance(info, dict):
                    quota_cache[acc["email"]] = info
                elif status == "exhausted":
                    quota_info = quota_result_quota_info(info)
                    if quota_info:
                        quota_cache[acc["email"]] = quota_info

    _print_status_table(accounts, quota_cache)


def _check_and_refresh(acc):
    """检查单个账号额度，401 时自动刷新 token。返回 (status_str, info)
    info: exhausted 时为 exhausted_info，ok 时为 quota_info dict
    """
    email = acc["email"]
    auth_file = acc.get("auth_file")

    if not auth_file or not Path(auth_file).exists():
        return "no_auth", None

    auth_data = json.loads(read_text(Path(auth_file)))
    access_token = auth_data.get("access_token")
    rt = auth_data.get("refresh_token")

    if not access_token:
        return "no_auth", None

    status, info = check_codex_quota(access_token)

    # token 过期，尝试刷新
    if status == "auth_error" and rt:
        logger.info("[%s] token 过期，尝试刷新...", email)
        new_tokens = refresh_access_token(rt)
        if new_tokens:
            auth_data["access_token"] = new_tokens["access_token"]
            auth_data["refresh_token"] = new_tokens.get("refresh_token", rt)
            auth_data["last_refresh"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            write_text(Path(auth_file), json.dumps(auth_data, indent=2))
            logger.info("[%s] token 已刷新，重新检查额度...", email)
            status, info = check_codex_quota(new_tokens["access_token"])
        else:
            logger.error("[%s] token 刷新失败", email)

    return status, info


def cmd_check(force_auth_repair=False, preserve_low_active=False, preserved_low_accounts=None):
    """swap_seat-only：通过 CPA 检查 quota 并收敛 seat/OAuth。"""
    logger.info("[检查] swap_seat-only 模式：通过 CPA 检查 quota 并收敛 seat/OAuth")
    from autoteam.config import AUTO_CHECK_TARGET_SEATS
    from autoteam.swap_seat import normalize_active_limit

    return cmd_swap_seats(max_chatgpt_active=normalize_active_limit(AUTO_CHECK_TARGET_SEATS))


def remove_from_team(chatgpt_api, email, *, return_status=False):
    """swap_seat-only：禁止将账号从 Team 中移除。"""
    raise RuntimeError("swap_seat-only 模式已禁用 Team member kick/remove；请只修改 seat_type")


def invite_to_team(chatgpt_api, email, seat_type="default"):
    """swap_seat-only：不再创建新 invite，只消费已有 pending invite。"""
    raise RuntimeError("swap_seat-only 模式禁用创建新 Team invite；请只消费已有 pending invite")


def _complete_registration(email, password, invite_link, mail_client, *, team_context=None):
    """仅供注册新号流程接受邀请；OAuth/auth 仍由 CPA 后续管理。"""
    from autoteam.invite import get_registration_error, register_with_invite

    signup_profile = generate_signup_profile()

    logger.info("[注册新号] 开始接受邀请并注册 %s...", email)
    registration_error = None
    with new_browser_session() as browser_session:
        page = browser_session.page
        result, password = register_with_invite(
            page,
            invite_link,
            email,
            mail_client,
            password=password,
            signup_profile=signup_profile,
        )
        registration_error = get_registration_error(page)
        auth_result = None
        session_info = None
        if result:
            try:
                from autoteam.codex_pat_export import (
                    capture_chatgpt_session_from_page,
                    create_save_upload_codex_auth_from_session,
                )

                session_info = capture_chatgpt_session_from_page(page)
                session_updates = _chatgpt_session_update_fields(session_info)
                team_account_id = _team_account_id(team_context)
                if team_account_id:
                    session_updates["chatgpt_account_id"] = team_account_id
                update_account(email, **session_updates)
                auth_result = create_save_upload_codex_auth_from_session(
                    session_info["chatgpt_session_token"],
                    account_id=_team_or_session_account_id(session_info, team_context),
                    email=email,
                    access_token=session_info.get("access_token"),
                    ttl_days=30,
                    upload=True,
                    main=False,
                )
                update_account(
                    email,
                    status=STATUS_STANDBY,
                    auth_file=auth_result.get("auth_file"),
                    auth_last_error=None,
                    chatgpt_account_id=_team_or_session_account_id(session_info, team_context) or None,
                    last_active_at=time.time(),
                )
                logger.info("[注册新号] %s Codex PAT 已生成并上传 CPA: %s", email, auth_result.get("filename"))
            except Exception as exc:
                updates = {
                    "status": STATUS_AUTH_PENDING,
                    "auth_last_error": f"codex_pat_export_failed: {exc}",
                    "last_active_at": time.time(),
                }
                updates.update(_chatgpt_session_update_fields(session_info))
                if _team_account_id(team_context):
                    updates["chatgpt_account_id"] = _team_account_id(team_context)
                update_account(
                    email,
                    **updates,
                )
                logger.warning("[注册新号] %s 已加入 Team，但 Codex PAT 自动导出/上传失败: %s", email, exc)

    if result:
        if auth_result:
            logger.info("[注册新号] %s 已加入 Team；auth 已交由 CPA 管理", email)
        else:
            logger.info("[注册新号] %s 已加入 Team；OAuth/auth 等待 CPA/PAT 补齐", email)
        return email
    error_type = str((registration_error or {}).get("type") or "registration_failed")
    error_detail = str((registration_error or {}).get("detail") or "")
    update_account(
        email,
        status=STATUS_PENDING,
        auth_last_error=error_type,
        auth_last_error_detail=(error_detail or "接受邀请/注册失败")[:300],
        auth_last_failed_at=time.time(),
        managed_by_autoteam=True,
    )
    logger.error("[注册新号] %s 接受邀请/注册失败: %s", email, error_type)
    return None


def _check_pending_invites(chatgpt_api, mail_client):
    """swap_seat-only：禁止 pending invite 处理。"""
    raise RuntimeError("swap_seat-only 模式已禁用 pending invite 处理")


def _is_email_in_team(email, *, team_context=None):
    """检查邮箱是否已实际进入 Team。"""
    chatgpt = None
    try:
        chatgpt = ChatGPTTeamAPI()
        _start_chatgpt_for_team(chatgpt, team_context)
        members, _ = _fetch_team_state_for_account(chatgpt, _team_account_id(team_context) or getattr(chatgpt, "account_id", ""))
        return any((m.get("email", "") or "").lower() == email.lower() for m in members)
    except Exception as exc:
        logger.warning("[直接注册] 检查 Team 成员失败: %s", exc)
        return False
    finally:
        if _chatgpt_session_ready(chatgpt):
            chatgpt.stop()


_DIRECT_EMAIL_SELECTORS = (
    'input[name="email"], input[type="email"], input[id="email"], '
    'input[autocomplete="email"], input[autocomplete="username"], '
    'input[placeholder*="email" i], input[placeholder*="Email" i]'
)
_DIRECT_PASSWORD_SELECTORS = 'input[name="password"], input[type="password"]'
_DIRECT_CODE_SELECTORS = 'input[name="code"], input[placeholder*="验证码"], input[placeholder*="code" i]'


def _safe_invite_screenshot(page, name):
    from autoteam.invite import screenshot

    try:
        screenshot(page, name)
    except Exception as exc:
        logger.debug("[直接注册] 截图失败 %s: %s", name, exc)


def _page_excerpt(page, limit=240):
    try:
        return page.locator("body").inner_text(timeout=1500)[:limit].replace("\n", " ")
    except Exception:
        return ""


def _quota_window_label(window: str | None) -> str:
    if window == "monthly":
        return "月"
    if window == "weekly":
        return "周"
    if window == "combined":
        return "多窗口"
    if window == "primary":
        return "5h"
    return "额度"


def _pending_historical_exhausted_info(quota_info, now=None):
    """仅当历史额度快照对应的耗尽窗口尚未重置时，才返回耗尽详情。"""
    exhausted_info = get_quota_exhausted_info(quota_info)
    if not exhausted_info:
        return None

    current_ts = time.time() if now is None else now
    resets_at = quota_result_resets_at(exhausted_info)
    if resets_at and current_ts >= resets_at:
        return None

    return exhausted_info


def _standby_reuse_hold_info(acc, now=None, grace_seconds=None):
    """返回 standby 账号在何时之前都不应复用。

    优先使用账号被标记 exhausted 时保存下来的 quota_resets_at，
    避免仅凭 last_quota.primary_resets_at 误判 5h 已恢复。
    """
    current_ts = time.time() if now is None else now
    grace = REUSE_RESET_GRACE_SECONDS if grace_seconds is None else grace_seconds

    saved_resets_at = 0
    try:
        saved_resets_at = int(acc.get("quota_resets_at") or 0)
    except Exception:
        saved_resets_at = 0

    if saved_resets_at:
        hold_until = saved_resets_at + grace
        if current_ts < hold_until:
            window = (acc.get("quota_window") or "").strip()
            if not window:
                exhausted_info = get_quota_exhausted_info(acc.get("last_quota"))
                if exhausted_info:
                    window = exhausted_info.get("window") or ""
            return {
                "resets_at": saved_resets_at,
                "hold_until": hold_until,
                "window": window,
                "source": "saved",
            }

    exhausted_info = get_quota_exhausted_info(acc.get("last_quota"))
    if exhausted_info:
        resets_at = quota_result_resets_at(exhausted_info)
        hold_until = resets_at + grace if resets_at else 0
        if hold_until and current_ts < hold_until:
            return {
                "resets_at": resets_at,
                "hold_until": hold_until,
                "window": exhausted_info.get("window") or "",
                "source": "history",
            }

    return None


def _first_visible_editable_locator(page, selectors, timeout=800):
    try:
        locator = page.locator(selectors).first
        if not locator.is_visible(timeout=timeout):
            return None
        if locator.is_editable(timeout=timeout):
            return locator
    except Exception:
        return None
    return None


def _collect_date_spinbutton_meta(page):
    try:
        return page.evaluate(
            """() => {
                const byIdsText = (rawIds) => {
                    return (rawIds || '')
                        .split(/\\s+/)
                        .filter(Boolean)
                        .map(id => {
                            const el = document.getElementById(id);
                            return el ? (el.textContent || '').trim() : '';
                        })
                        .filter(Boolean)
                        .join(' ');
                };

                return Array.from(document.querySelectorAll('[role="spinbutton"]')).map((el, index) => ({
                    index,
                    text: (el.textContent || '').trim(),
                    ariaLabel: el.getAttribute('aria-label') || '',
                    ariaValueText: el.getAttribute('aria-valuetext') || '',
                    ariaValueMin: el.getAttribute('aria-valuemin') || '',
                    ariaValueMax: el.getAttribute('aria-valuemax') || '',
                    placeholder: el.getAttribute('placeholder') || '',
                    dataType: el.getAttribute('data-type') || el.dataset?.type || '',
                    labelledText: byIdsText(el.getAttribute('aria-labelledby')),
                    describedText: byIdsText(el.getAttribute('aria-describedby')),
                }));
            }"""
        )
    except Exception:
        return []


def _infer_date_spinbutton_kind(meta):
    text_parts = [
        meta.get("text", ""),
        meta.get("ariaLabel", ""),
        meta.get("ariaValueText", ""),
        meta.get("placeholder", ""),
        meta.get("dataType", ""),
        meta.get("labelledText", ""),
        meta.get("describedText", ""),
    ]
    lowered = " ".join(part for part in text_parts if part).lower()

    def _to_int(value):
        try:
            return int(str(value).strip())
        except Exception:
            return None

    max_val = _to_int(meta.get("ariaValueMax"))

    if any(token in lowered for token in ("year", "yyyy", "yy", "年")):
        return "year"
    if any(token in lowered for token in ("month", "mm", "月")):
        return "month"
    if any(token in lowered for token in ("day", "dd", "日")):
        return "day"

    if max_val is not None:
        if max_val > 31:
            return "year"
        if max_val == 12:
            return "month"
        if max_val <= 31:
            return "day"

    return None


def _fill_about_you_birthday_by_meta(page, signup_profile: SignupProfile):
    metas = _collect_date_spinbutton_meta(page)
    if len(metas) < 3:
        return False

    desired = {
        "year": signup_profile.birth_year_text,
        "month": signup_profile.birth_month_text,
        "day": signup_profile.birth_day_text,
    }
    kind_to_meta = {}

    for meta in metas:
        kind = _infer_date_spinbutton_kind(meta)
        if kind and kind not in kind_to_meta:
            kind_to_meta[kind] = meta

    if not all(kind in kind_to_meta for kind in desired):
        logger.info("[直接注册] 无法可靠识别生日字段顺序，降级为位置猜测")
        return False

    try:
        for kind in ("year", "month", "day"):
            meta = kind_to_meta[kind]
            sb = page.locator('[role="spinbutton"]').nth(meta["index"])
            sb.click(force=True)
            time.sleep(0.2)
            try:
                page.keyboard.press("ControlOrMeta+A")
                time.sleep(0.1)
            except Exception:
                pass
            page.keyboard.type(desired[kind], delay=80)
            time.sleep(0.3)

        logger.info(
            "[直接注册] 已填入随机生日: year=%s month=%s day=%s | order=%s",
            desired["year"],
            desired["month"],
            desired["day"],
            {kind: kind_to_meta[kind]["index"] for kind in ("year", "month", "day")},
        )
        return True
    except Exception as exc:
        logger.warning("[直接注册] 按字段填写生日失败，降级为位置猜测: %s", exc)
        return False


def _detect_direct_register_step(page):
    url = (page.url or "").lower()
    if _is_google_redirect(page):
        return "google"
    if "/api/auth/error" in url or url.endswith("/auth/error"):
        return "error"

    if "email-verification" in url:
        return "code"
    if "about-you" in url:
        return "profile"
    if "create-account/password" in url or url.endswith("/password"):
        return "password"
    if "chatgpt.com" in url and "auth" not in url:
        return "completed"

    try:
        if _first_visible_editable_locator(page, _DIRECT_PASSWORD_SELECTORS, timeout=300):
            return "password"
    except Exception:
        pass

    try:
        if _first_visible_editable_locator(page, _DIRECT_CODE_SELECTORS, timeout=300):
            return "code"
    except Exception:
        pass

    try:
        if page.locator('input[name="name"], [role="spinbutton"]').first.is_visible(timeout=300):
            return "profile"
    except Exception:
        pass

    try:
        if _first_visible_editable_locator(page, _DIRECT_EMAIL_SELECTORS, timeout=300):
            return "email"
    except Exception:
        pass

    if "log-in-or-create-account" in url or url.endswith("/auth/login"):
        return "email"
    if "create-account" in url or "password" in url:
        return "password"
    return "unknown"


def _wait_for_direct_register_step(page, allowed_steps, timeout=15):
    deadline = time.time() + timeout
    while time.time() < deadline:
        step = _detect_direct_register_step(page)
        if step == "error":
            return step
        if step in allowed_steps:
            return step
        time.sleep(0.5)
    return _detect_direct_register_step(page)


def _wait_for_direct_step_change(page, current_step, timeout=15):
    deadline = time.time() + timeout
    while time.time() < deadline:
        step = _detect_direct_register_step(page)
        if step != current_step:
            return step
        time.sleep(0.5)
    return _detect_direct_register_step(page)


def _complete_direct_about_you(page, signup_profile: SignupProfile | None = None):
    """尽量完成 about-you 页面，兼容不同生日字段顺序。"""
    if "about-you" not in (page.url or "").lower():
        return True

    signup_profile = signup_profile or generate_signup_profile()
    birthday_orders = signup_profile.positional_birthday_orders()

    for attempt, values in enumerate(birthday_orders, 1):
        if "about-you" not in (page.url or "").lower():
            return True

        try:
            name_input = page.locator('input[name="name"]').first
            if name_input.is_visible(timeout=2000):
                try:
                    if name_input.is_editable(timeout=500):
                        name_input.fill(signup_profile.full_name)
                        time.sleep(0.3)
                        logger.info("[直接注册] 已填入随机姓名: %s", signup_profile.full_name)
                except Exception:
                    pass
        except Exception:
            name_input = None

        spinbuttons = []
        try:
            spinbuttons = page.locator('[role="spinbutton"]').all()
        except Exception:
            spinbuttons = []

        if len(spinbuttons) >= 3:
            filled = _fill_about_you_birthday_by_meta(page, signup_profile)
            if not filled:
                for label_sel in ("text=生日日期", "text=Date of birth"):
                    try:
                        page.locator(label_sel).first.click(timeout=1000)
                        time.sleep(0.3)
                        break
                    except Exception:
                        continue

                try:
                    for sb, val in zip(spinbuttons[:3], values):
                        sb.click(force=True)
                        time.sleep(0.2)
                        try:
                            page.keyboard.press("ControlOrMeta+A")
                            time.sleep(0.1)
                        except Exception:
                            pass
                        page.keyboard.type(val, delay=80)
                        time.sleep(0.3)
                    logger.info("[直接注册] 尝试按位置填入随机生日（第 %d 次）: %s/%s/%s", attempt, *values)
                except Exception as exc:
                    logger.warning("[直接注册] 生日字段填写失败（第 %d 次）: %s", attempt, exc)
        else:
            try:
                age_input = page.locator(
                    'input[name="age"], input[placeholder*="年龄"], input[placeholder*="Age"]'
                ).first
                if age_input.is_visible(timeout=2000) and age_input.is_editable(timeout=500):
                    age_input.fill(signup_profile.age_text)
                    logger.info("[直接注册] 已填入随机年龄: %s", signup_profile.age_text)
            except Exception:
                pass

        submitted = False
        for btn_selector in (
            'button:has-text("完成帐户创建")',
            'button:has-text("Create account")',
            'button:has-text("Continue")',
            'button:has-text("继续")',
            'button[type="submit"]',
        ):
            try:
                btn = page.locator(btn_selector).first
                if btn.is_visible(timeout=1000):
                    btn.click()
                    submitted = True
                    break
            except Exception:
                continue

        if not submitted:
            try:
                page.keyboard.press("Enter")
            except Exception:
                pass

        next_step = _wait_for_direct_register_step(
            page,
            {"profile", "completed", "code", "password", "email", "google"},
            timeout=12,
        )
        logger.info("[直接注册] 提交资料后状态: %s | URL: %s", next_step, page.url)
        if next_step != "profile":
            return True

    logger.warning("[直接注册] about-you 页面仍未完成 | URL: %s | body=%s", page.url, _page_excerpt(page))
    return False


def _register_direct_once(
    mail_client, email, password, mail_account_id=None, signup_profile: SignupProfile | None = None
):
    """执行一次直接注册，返回是否完成注册并进入 Team。"""
    signup_profile = signup_profile or generate_signup_profile()

    logger.info("[直接注册] %s", email)
    signup_url = "https://chatgpt.com/auth/login"

    with new_browser_session() as browser_session:
        page = browser_session.page

        page.goto(signup_url, wait_until="domcontentloaded", timeout=60000)
        time.sleep(5)

        for i in range(12):
            html = page.content()[:2000].lower()
            if "verify you are human" not in html and "challenge" not in page.url:
                break
            logger.info("[直接注册] 等待 Cloudflare... (%ds)", i * 5)
            time.sleep(5)

        _safe_invite_screenshot(page, "direct_01_login_page.png")

        # OpenAI 首页有多种 A/B 测试变体，需要逐步找到邮箱输入框
        try:
            email_visible = page.locator(_DIRECT_EMAIL_SELECTORS).first.is_visible(timeout=3000)
            if not email_visible:
                # 尝试按优先级点击各种按钮来展开/跳转到邮箱输入
                for sel, desc in [
                    ('button:has-text("More options")', "More options"),
                    ('button:has-text("更多选项")', "更多选项"),
                    ('a:has-text("Sign up for free")', "Sign up for free"),
                    ('button:has-text("Sign up for free")', "Sign up for free"),
                    ('a:has-text("Sign up")', "Sign up"),
                    ('button:has-text("Sign up")', "Sign up"),
                    ('a:has-text("注册")', "注册"),
                    ('button:has-text("注册")', "注册"),
                    ('a:has-text("Log in")', "Log in"),
                    ('button:has-text("Log in")', "Log in"),
                ]:
                    try:
                        btn = page.locator(sel).first
                        if btn.is_visible(timeout=1000):
                            logger.info("[直接注册] 点击: %s", desc)
                            btn.click()
                            time.sleep(2)
                            # 检查邮箱输入框是否出现了
                            step = _wait_for_direct_register_step(
                                page,
                                {"email", "password", "code", "profile", "completed", "google"},
                                timeout=10,
                            )
                            if step != "unknown":
                                break
                    except Exception:
                        continue
        except Exception:
            pass

        _safe_invite_screenshot(page, "direct_02_signup.png")

        logger.info("[直接注册] 输入邮箱: %s", email)
        email_step = _wait_for_direct_register_step(
            page,
            {"email", "password", "code", "profile", "completed", "google"},
            timeout=15,
        )
        logger.info("[直接注册] 邮箱步骤初始状态: %s | URL: %s", email_step, page.url)

        if email_step == "google":
            logger.warning("[直接注册] 邮箱步骤误跳转到 Google 登录页")
            browser_session.close()
            return False
        if email_step == "unknown":
            logger.warning("[直接注册] 未识别到邮箱步骤 | URL: %s | body=%s", page.url, _page_excerpt(page))
            browser_session.close()
            return False

        try:
            for attempt in range(3):
                step = _detect_direct_register_step(page)
                if step != "email":
                    break

                email_input = _first_visible_editable_locator(page, _DIRECT_EMAIL_SELECTORS, timeout=1500)
                if not email_input:
                    logger.info("[直接注册] 邮箱输入框不可编辑，等待页面继续跳转...")
                    next_step = _wait_for_direct_step_change(page, "email", timeout=10)
                    if next_step != "email":
                        break
                    logger.warning("[直接注册] 邮箱输入框仍不可编辑，继续重试 | URL: %s", page.url)
                    continue

                email_input.fill(email)
                time.sleep(0.5)
                logger.info("[直接注册] 邮箱已填入，点击 Continue... (attempt %d)", attempt + 1)
                _safe_invite_screenshot(page, f"direct_02b_email_filled_{attempt}.png")
                _click_primary_auth_button(page, email_input, ["Continue", "继续"])

                next_step = _wait_for_direct_step_change(page, "email", timeout=15)
                logger.info("[直接注册] 点击 Continue 后状态: %s | URL: %s", next_step, page.url)
                _safe_invite_screenshot(page, f"direct_02c_after_continue_{attempt}.png")

                if next_step == "google":
                    _safe_invite_screenshot(page, f"direct_03_google_redirect_attempt{attempt + 1}.png")
                    logger.warning("[直接注册] 邮箱步骤误跳转到 Google 登录，返回重试... (attempt %d)", attempt + 1)
                    page.go_back(wait_until="domcontentloaded", timeout=30000)
                    time.sleep(2)
                    continue
                if next_step != "email":
                    break

                email_input = _first_visible_editable_locator(page, _DIRECT_EMAIL_SELECTORS, timeout=600)
                if not email_input:
                    logger.info("[直接注册] 邮箱框已只读/跳转中，额外等待页面推进...")
                    next_step = _wait_for_direct_step_change(page, "email", timeout=10)
                    logger.info("[直接注册] 额外等待后状态: %s | URL: %s", next_step, page.url)
                    if next_step != "email":
                        break

                logger.warning(
                    "[直接注册] 点击 Continue 后仍停留在邮箱步骤，准备重试... | URL: %s | body=%s",
                    page.url,
                    _page_excerpt(page),
                )
        except Exception as exc:
            logger.warning("[直接注册] 邮箱步骤异常: %s | URL: %s", exc, page.url)

        _safe_invite_screenshot(page, "direct_03_after_email.png")
        current_step = _detect_direct_register_step(page)
        logger.info("[直接注册] 邮箱步骤结束状态: %s | URL: %s", current_step, page.url)
        if current_step == "google":
            logger.warning("[直接注册] 邮箱步骤仍停留在 Google 登录页")
            browser_session.close()
            return False
        if current_step == "error":
            logger.warning("[直接注册] 邮箱步骤进入认证错误页 | URL: %s | body=%s", page.url, _page_excerpt(page))
            browser_session.close()
            return False
        if current_step == "unknown":
            logger.warning("[直接注册] 邮箱步骤进入未知状态 | URL: %s | body=%s", page.url, _page_excerpt(page))
            browser_session.close()
            return False
        if current_step == "email":
            logger.warning("[直接注册] 邮箱步骤未推进 | URL: %s | body=%s", page.url, _page_excerpt(page))
            browser_session.close()
            return False

        # 等待页面跳转完成（可能跳到 create-account/password）
        password_step = _wait_for_direct_register_step(
            page,
            {"password", "code", "profile", "completed", "google", "email"},
            timeout=15,
        )
        logger.info("[直接注册] 密码页检测状态: %s | URL: %s", password_step, page.url)
        _safe_invite_screenshot(page, "direct_03b_before_password.png")
        if password_step == "error":
            logger.warning("[直接注册] 密码步骤进入认证错误页 | URL: %s | body=%s", page.url, _page_excerpt(page))
            browser_session.close()
            return False
        if password_step == "unknown":
            logger.warning("[直接注册] 无法识别密码/验证码步骤 | URL: %s | body=%s", page.url, _page_excerpt(page))
            browser_session.close()
            return False

        try:
            for attempt in range(2):
                if _detect_direct_register_step(page) != "password":
                    logger.info("[直接注册] 未检测到密码输入框，跳过")
                    break

                pwd_input = _first_visible_editable_locator(page, _DIRECT_PASSWORD_SELECTORS, timeout=1500)
                if not pwd_input:
                    logger.info("[直接注册] 密码输入框不可编辑，等待页面继续跳转...")
                    next_step = _wait_for_direct_step_change(page, "password", timeout=10)
                    if next_step != "password":
                        break
                    logger.warning("[直接注册] 密码输入框仍不可编辑，继续重试 | URL: %s", page.url)
                    continue

                logger.info("[直接注册] 设置密码")
                pwd_input.fill(password)
                time.sleep(0.5)
                _click_primary_auth_button(page, pwd_input, ["Continue", "继续", "Log in"])
                next_step = _wait_for_direct_step_change(page, "password", timeout=15)
                logger.info("[直接注册] 提交密码后状态: %s | URL: %s", next_step, page.url)

                if next_step == "google":
                    _safe_invite_screenshot(page, f"direct_04_google_redirect_attempt{attempt + 1}.png")
                    logger.warning("[直接注册] 密码步骤误跳转到 Google 登录，返回重试... (attempt %d)", attempt + 1)
                    page.go_back(wait_until="domcontentloaded", timeout=30000)
                    time.sleep(2)
                    continue
                if next_step != "password":
                    break

                pwd_input = _first_visible_editable_locator(page, _DIRECT_PASSWORD_SELECTORS, timeout=600)
                if not pwd_input:
                    logger.info("[直接注册] 密码框已只读/跳转中，额外等待页面推进...")
                    next_step = _wait_for_direct_step_change(page, "password", timeout=10)
                    logger.info("[直接注册] 额外等待后状态: %s | URL: %s", next_step, page.url)
                    if next_step != "password":
                        break
        except Exception as exc:
            logger.warning("[直接注册] 密码步骤异常: %s | URL: %s", exc, page.url)

        _safe_invite_screenshot(page, "direct_04_after_password.png")
        current_step = _detect_direct_register_step(page)
        if current_step == "google":
            logger.warning("[直接注册] 密码步骤仍停留在 Google 登录页")
            browser_session.close()
            return False
        if current_step == "error":
            logger.warning("[直接注册] 密码步骤进入认证错误页 | URL: %s | body=%s", page.url, _page_excerpt(page))
            browser_session.close()
            return False
        if current_step == "unknown":
            logger.warning("[直接注册] 密码步骤进入未知状态 | URL: %s | body=%s", page.url, _page_excerpt(page))
            browser_session.close()
            return False
        if current_step == "email":
            logger.warning("[直接注册] 提交密码前流程回退到邮箱页 | URL: %s | body=%s", page.url, _page_excerpt(page))
            browser_session.close()
            return False

        code_input = None
        try:
            code_input = page.locator(_DIRECT_CODE_SELECTORS).first
            if not code_input.is_visible(timeout=5000):
                code_input = None
        except Exception:
            code_input = None

        if code_input:
            logger.info("[直接注册] 等待验证码...")
            verification_code = None
            start_t = time.time()
            while time.time() - start_t < MAIL_TIMEOUT:
                emails = mail_client.search_emails_by_recipient(email, size=10, account_id=mail_account_id)
                for em in emails:
                    verification_code = mail_client.extract_verification_code(em)
                    if verification_code:
                        break
                if verification_code:
                    break
                elapsed = int(time.time() - start_t)
                print(f"\r  等待验证码... ({elapsed}s)", end="", flush=True)
                time.sleep(3)

            if verification_code:
                logger.info("[直接注册] 输入验证码: [redacted]")
                code_input.fill(verification_code)
                time.sleep(0.5)
                _click_primary_auth_button(page, code_input, ["Continue", "继续"])
                time.sleep(8)
            else:
                logger.error("[直接注册] 未收到验证码")
                browser_session.close()
                return False

        _safe_invite_screenshot(page, "direct_05_after_code.png")
        logger.info("[直接注册] 当前 URL: %s", page.url)

        try:
            _complete_direct_about_you(page, signup_profile)
        except Exception as exc:
            logger.warning("[直接注册] about-you 步骤异常: %s | URL: %s", exc, page.url)

        _safe_invite_screenshot(page, "direct_06_after_profile.png")
        logger.info("[直接注册] 当前 URL: %s", page.url)

        try:
            join_btn = page.locator('button:has-text("Accept"), button:has-text("Join"), button:has-text("加入")').first
            if join_btn.is_visible(timeout=5000):
                join_btn.click()
                time.sleep(5)
        except Exception:
            pass

        _safe_invite_screenshot(page, "direct_07_final.png")

        current_url = page.url
        success = "chatgpt.com" in current_url and "auth" not in current_url and not _is_google_redirect(page)
        if success:
            logger.info("[直接注册] 注册成功并已加入 workspace!")
        else:
            logger.warning("[直接注册] 注册可能未完成，URL: %s", current_url)

        browser_session.close()
        return success


def create_account_direct(mail_client):
    """swap_seat-only：禁止直接注册账号。"""
    raise RuntimeError("swap_seat-only 模式已禁用直接注册账号")


def _mail_account_id_for_email(mail_client, email):
    resolver = getattr(mail_client, "_resolve_account_id_for_email", None)
    if callable(resolver):
        try:
            return resolver(email)
        except Exception:
            logger.debug("[注册新号] 解析邮箱 account_id 失败: %s", email, exc_info=True)
    return None


_CFMAIL_LOCAL_NAMES = (
    "alex",
    "blake",
    "casey",
    "drew",
    "ellis",
    "finley",
    "harper",
    "jordan",
    "logan",
    "morgan",
    "parker",
    "riley",
    "taylor",
)


def _random_cfmail_prefix():
    name = secrets.choice(_CFMAIL_LOCAL_NAMES)
    suffix = "".join(secrets.choice(string.ascii_lowercase) for _ in range(3))
    digits = "".join(secrets.choice(string.digits) for _ in range(4))
    return f"{name}{suffix}{digits}"


def _create_random_cfmail_address(mail_client, *, attempts=5, invite_domains: str | None = None):
    creator = getattr(mail_client, "create_temp_email", None)
    if not callable(creator):
        raise RuntimeError("当前邮箱服务不支持创建 CFMail 地址")

    last_error = None
    random_subdomain_override = None if str(invite_domains or "").strip() else True
    for _attempt in range(1, max(1, int(attempts or 1)) + 1):
        prefix = _random_cfmail_prefix()
        try:
            account_id, email = creator(
                prefix=prefix,
                domain=invite_domains,
                enable_random_subdomain=random_subdomain_override,
            )
        except TypeError:
            try:
                account_id, email = creator(prefix=prefix, domain=invite_domains)
            except TypeError:
                account_id, email = creator(prefix=prefix)
        except Exception as exc:
            last_error = str(exc)
            continue

        email = _normalized_email(email)
        if email and "@" in email:
            return account_id, email, prefix
        last_error = f"CFMail 创建地址返回异常: {email or '<empty>'}"

    raise RuntimeError(last_error or "CFMail 创建地址失败")


def _ensure_local_pending_invite_account(email, password, mail_client, account_id=None, *, team_context=None, team_account_id=None):
    """把 pending invite 对应账号落到本地账号池，转发邮箱只作为收信通道。"""
    email = _normalized_email(email)
    if not email:
        return None

    accounts = load_accounts()
    acc = find_account(accounts, email)
    delivery_email = _mail_delivery_email_for(email, acc=acc)
    account_id = account_id if account_id is not None else _mail_account_id_for_email(mail_client, delivery_email)
    team_account_id = str(team_account_id or _team_account_id(team_context) or "").strip()
    provider = getattr(mail_client, "provider_name", "") or infer_mail_provider_from_email(email)
    service_id = getattr(mail_client, "service_id", None) or infer_mail_service_from_email(delivery_email) or None

    if acc:
        updates = {
            "password": password,
            "mail_provider": provider or acc.get("mail_provider"),
            "mail_service_id": service_id or acc.get("mail_service_id"),
            "status": STATUS_PENDING,
            "disabled": False,
            "auth_last_error": None,
            "managed_by_autoteam": True,
        }
        if team_account_id:
            updates["chatgpt_account_id"] = team_account_id
        if account_id is not None:
            updates["mail_account_id"] = account_id
            updates["cloudmail_account_id"] = None
        if delivery_email and delivery_email != email:
            updates["mail_forward_to"] = delivery_email
        update_account(email, **updates)
        return account_id

    add_account(
        email,
        password,
        cloudmail_account_id=None,
        mail_provider=provider,
        mail_account_id=account_id,
        mail_service_id=service_id,
    )
    updates = {"managed_by_autoteam": True, "disabled": False}
    if team_account_id:
        updates["chatgpt_account_id"] = team_account_id
    if delivery_email and delivery_email != email:
        updates["mail_forward_to"] = delivery_email
    update_account(email, **updates)
    return account_id


def _pending_invite_candidates(chatgpt_api, mail_client, *, account_id: str | None = None):
    members, invites = _fetch_team_state_for_account(chatgpt_api, account_id)
    joined = {_normalized_email(item.get("email")) for item in members if _normalized_email(item.get("email"))}
    candidates = []
    for inv in invites:
        email = _normalized_email(inv.get("email_address") or inv.get("email"))
        if not email or email in joined:
            continue
        forward_to = _pending_invite_forward_to(email)
        provider = infer_mail_provider_from_email(email) or getattr(mail_client, "provider_name", "")
        if provider and provider != MAIL_PROVIDER_CLOUDFLARE_TEMP_EMAIL and not forward_to:
            continue
        candidate = {
            "email": email,
            "invite_id": str(inv.get("id") or "").strip(),
            "role": inv.get("role", ""),
            "seat_type": inv.get("seat_type", ""),
        }
        if forward_to and forward_to != email:
            candidate["mail_forward_to"] = forward_to
        candidates.append(candidate)
    return candidates


def _is_retryable_pending_registration_account(acc: dict | None, *, account_id: str | None = None) -> bool:
    acc = acc or {}
    if not _account_looks_autoteam_created(acc) or is_account_disabled(acc):
        return False
    if _has_auth_file(acc):
        return False
    status = str(acc.get("status") or "")
    if status not in {STATUS_PENDING, STATUS_AUTH_PENDING}:
        return False
    record_account_id = _account_record_team_account_id(acc)
    if account_id and record_account_id and record_account_id != account_id:
        return False
    last_error = str(acc.get("auth_last_error") or "")
    if last_error.startswith("codex_pat_export_failed") or last_error.startswith("cpa_auth"):
        return False
    return True


def _autoteam_pending_invite_retry_candidates(chatgpt_api, mail_client, *, account_id: str | None = None):
    """Return self-managed pending invite accounts that should be retried before creating a new invite."""
    candidates = _pending_invite_candidates(chatgpt_api, mail_client, account_id=account_id)
    accounts_by_email = {_normalized_email(acc.get("email")): acc for acc in load_accounts()}
    retry = []
    for candidate in candidates:
        email = _normalized_email(candidate.get("email"))
        acc = accounts_by_email.get(email)
        if not _is_retryable_pending_registration_account(acc, account_id=account_id):
            continue
        retry.append({**candidate, "account": acc})
    retry.sort(key=lambda item: float((item.get("account") or {}).get("created_at") or 0))
    return retry


def _extract_pending_invite_link(mail_client, email, *, timeout=MAIL_TIMEOUT):
    account_id = _mail_account_id_for_email(mail_client, email)
    try:
        emails = mail_client.search_emails_by_recipient(email, size=20, account_id=account_id)
    except TypeError:
        emails = mail_client.search_emails_by_recipient(email, size=20)
    except Exception as exc:
        logger.warning("[注册新号] 读取 %s 历史邀请邮件失败: %s", email, exc)
        emails = []

    for email_item in emails:
        sender = str(email_item.get("sendEmail") or email_item.get("source") or "").lower()
        subject = str(email_item.get("subject") or "").lower()
        if "openai" not in sender and "chatgpt" not in sender and "openai" not in subject:
            continue
        link = mail_client.extract_invite_link(email_item)
        if link:
            return link

    email_item = mail_client.wait_for_email(to_email=email, timeout=timeout, sender_keyword="openai")
    return mail_client.extract_invite_link(email_item)


def _wait_for_team_member(chatgpt_api, email, *, timeout=45, account_id: str | None = None):
    deadline = time.time() + max(5, int(timeout))
    target = _normalized_email(email)
    while time.time() < deadline:
        members, _invites = _fetch_team_state_for_account(chatgpt_api, account_id)
        for member in members:
            if _normalized_email(member.get("email")) == target:
                return member
        time.sleep(2)
    return None


def _wait_for_cpa_auth(email, *, timeout=45, managed_auth_names: set[str] | None = None):
    deadline = time.time() + max(5, int(timeout))
    target = _normalized_email(email)
    latest = None
    while time.time() < deadline:
        auths = [
            auth
            for auth in list_cpa_files()
            if is_cpa_codex_oauth(auth) and _normalized_email(auth.get("email") or auth.get("account")) == target
        ]
        if managed_auth_names is not None:
            auths = [auth for auth in auths if is_managed_cpa_auth(auth, managed_auth_names)]
        if auths:
            def _score(auth):
                status = str(auth.get("status") or "").strip().lower()
                ts = str(auth.get("updated_at") or auth.get("last_refresh") or auth.get("modtime") or "")
                return (
                    1 if not cpa_auth_is_disabled(auth) else 0,
                    1 if status in {"active", "enabled", "ok"} else 0,
                    len(str(auth.get("headers", {}).get("authorization") or "")),
                    ts,
                )

            latest = max(auths, key=_score)
            break
        time.sleep(2)
    return latest


def _activate_registered_account(chatgpt_api, email, max_chatgpt_active=2, team_context=None):
    """注册完成后：旧号保持 Codex，新号升为 ChatGPT 并启用其 CPA auth。"""
    from autoteam.swap_seat import get_swap_seat_whitelist_emails, normalize_active_limit, normalize_team_seat_type

    target = _normalized_email(email)
    whitelist = get_swap_seat_whitelist_emails()
    active_limit = normalize_active_limit(max_chatgpt_active)
    account_id = _team_account_id(team_context) or str(getattr(chatgpt_api, "account_id", "") or "").strip()
    member = _wait_for_team_member(chatgpt_api, target, account_id=account_id)
    if not member:
        return {"ok": False, "reason": "team_member_not_found", "email": target, "team": _team_label(team_context)}

    members, _invites = _fetch_team_state_for_account(chatgpt_api, account_id)
    whitelisted_chatgpt = sum(
        1
        for item in members
        if _normalized_email(item.get("email")) != target
        and _normalized_email(item.get("email")) in whitelist
        and normalize_team_seat_type(item.get("seat_type")) == "chatgpt"
    )
    if whitelisted_chatgpt >= active_limit:
        logger.warning(
            "[注册新号] 白名单 GPT seat 已占满 %d/%d，跳过新号激活但不降级当前 seat: %s",
            whitelisted_chatgpt,
            active_limit,
            target,
        )
        return {
            "ok": False,
            "reason": "whitelist_chatgpt_limit",
            "email": target,
            "count": whitelisted_chatgpt,
            "max_chatgpt_active": active_limit,
            "team": _team_label(team_context),
        }

    current_seat = normalize_team_seat_type(member.get("seat_type"))
    managed_auth_names = get_managed_cpa_auth_names()
    target_auth = _wait_for_cpa_auth(target, managed_auth_names=managed_auth_names)
    if not target_auth:
        update_account(
            target,
            status=STATUS_AUTH_PENDING,
            seat_type=current_seat if current_seat in {"chatgpt", "codex"} else None,
            disabled=False,
            auth_last_error="cpa_auth_missing_after_registration",
            last_active_at=time.time(),
        )
        logger.warning("[注册新号] CPA auth 尚未出现，保留当前 seat 等待 PAT 修复: %s seat=%s", target, current_seat)
        return {"ok": False, "reason": "cpa_auth_missing", "email": target, "team": _team_label(team_context)}

    target_auth_id = str(target_auth.get("name") or target_auth.get("id") or "").strip()
    if not target_auth_id:
        update_account(
            target,
            status=STATUS_AUTH_PENDING,
            seat_type=current_seat if current_seat in {"chatgpt", "codex"} else None,
            disabled=False,
            auth_last_error="cpa_auth_missing_identifier_after_registration",
            last_active_at=time.time(),
        )
        logger.warning("[注册新号] CPA auth 缺少 name/id，无法启用，保留当前 seat: %s seat=%s", target, current_seat)
        return {"ok": False, "reason": "cpa_auth_missing_identifier", "email": target, "team": _team_label(team_context)}

    all_auths = [auth for auth in list_cpa_files() if is_cpa_codex_oauth(auth)]
    auths = [auth for auth in all_auths if is_managed_cpa_auth(auth, managed_auth_names)]
    if not any(str(auth.get("name") or auth.get("id") or "").strip() == target_auth_id for auth in auths):
        # _wait_for_cpa_auth already observed this managed auth. If the next list response is stale,
        # keep the observed target in the activation set so we do not mark the account active without
        # attempting to enable its CPA OAuth.
        auths.append(target_auth)
        if not any(str(auth.get("name") or auth.get("id") or "").strip() == target_auth_id for auth in all_auths):
            all_auths.append(target_auth)
    whitelisted_active_oauth = sum(
        1
        for auth in auths
        if _normalized_email(auth.get("email") or auth.get("account")) != target
        and _normalized_email(auth.get("email") or auth.get("account")) in whitelist
        and cpa_auth_is_active(auth)
    )
    if whitelisted_active_oauth >= active_limit:
        return {
            "ok": False,
            "reason": "whitelist_oauth_limit",
            "email": target,
            "count": whitelisted_active_oauth,
            "max_chatgpt_active": active_limit,
            "team": _team_label(team_context),
        }

    user_id = str(member.get("user_id") or member.get("id") or "").strip()
    seat_result = {
        "email": target,
        "current_seat": current_seat,
        "desired_seat": "chatgpt",
        "result": "unchanged" if current_seat == "chatgpt" else "pending",
    }
    if current_seat != "chatgpt":
        if not user_id:
            seat_result["result"] = "skipped"
            seat_result["error"] = "missing user_id"
        else:
            response = chatgpt_api.update_member_seat_type(user_id, "default")
            if int(response.get("status") or 0) in (200, 204):
                seat_result["result"] = "updated"
            else:
                seat_result["result"] = "failed"
                seat_result["error"] = f"HTTP {response.get('status')}: {str(response.get('body') or '')[:200]}"

    seat_ok = seat_result.get("result") in {"updated", "unchanged"}
    oauth_results = []
    if seat_ok:
        for auth in auths:
            auth_email = _normalized_email(auth.get("email") or auth.get("account"))
            if not auth_email or auth_email in whitelist:
                continue
            auth_id = str(auth.get("name") or auth.get("id") or "").strip()
            if auth_email != target or not auth_id or auth_id != target_auth_id:
                continue
            desired_disabled = False
            current_disabled = cpa_auth_is_disabled(auth)
            current_status = str(auth.get("status") or "").strip().lower()
            try:
                if current_disabled or (current_status and current_status not in {"active", "enabled", "ok"}):
                    set_cpa_auth_disabled(auth, desired_disabled)
                    result = "updated"
                else:
                    result = "unchanged"
                oauth_results.append(
                    {
                        "email": auth_email,
                        "name": auth_id,
                        "disabled": desired_disabled,
                        "result": result,
                    }
                )
            except Exception as exc:
                oauth_results.append(
                    {
                        "email": auth_email,
                        "name": auth_id,
                        "disabled": desired_disabled,
                        "result": "failed",
                        "error": str(exc),
                    }
                )
    else:
        try:
            if not cpa_auth_is_disabled(target_auth):
                set_cpa_auth_disabled(target_auth, True)
                oauth_result = "updated"
            else:
                oauth_result = "unchanged"
            oauth_results.append(
                {
                    "email": target,
                    "name": target_auth_id,
                    "disabled": True,
                    "result": oauth_result,
                    "reason": "team_seat_not_chatgpt",
                }
            )
        except Exception as exc:
            oauth_results.append(
                {
                    "email": target,
                    "name": target_auth_id,
                    "disabled": True,
                    "result": "failed",
                    "reason": "team_seat_not_chatgpt",
                    "error": str(exc),
                }
            )
    ok = seat_ok and not any(item.get("result") == "failed" for item in oauth_results)
    final_seat_type = "chatgpt" if seat_ok else (current_seat if current_seat in {"chatgpt", "codex"} else None)
    update_account(
        target,
        status=STATUS_ACTIVE if ok else STATUS_AUTH_PENDING,
        seat_type=final_seat_type,
        disabled=False,
        auth_last_error=None if ok else "post_registration_activation_failed",
        last_active_at=time.time(),
    )
    return {
        "ok": ok,
        "email": target,
        "team": _team_label(team_context),
        "account_id": account_id,
        "max_chatgpt_active": active_limit,
        "seat_result": seat_result,
        "oauth_results": oauth_results,
        "summary": {
            "managed_cpa_codex_oauth": len(auths),
            "unmanaged_cpa_codex_oauth": len(all_auths) - len(auths),
            "oauth_updated": sum(1 for item in oauth_results if item.get("result") == "updated"),
            "oauth_failed": sum(1 for item in oauth_results if item.get("result") == "failed"),
        },
        "reason": "activated" if ok else "partial_failure",
    }


def _post_registration_seat_rebalance(chatgpt_api, max_chatgpt_active=2, team_context=None):
    """注册/PAT 成功后复用 swap_seat，补齐目标数量的 GPT seat + CPA active。"""
    try:
        from autoteam.swap_seat import cmd_swap_seats as _cmd_swap_seats

        return _cmd_swap_seats(
            max_chatgpt_active=max_chatgpt_active,
            chatgpt_api=chatgpt_api,
            team_context=team_context,
        )
    except Exception as exc:
        logger.warning("[注册新号] 注册后 swap_seat 复核失败: %s", exc)
        return {"ok": False, "error": str(exc)}


def _pre_sweep_registration_team(chatgpt_api, max_chatgpt_active=2, team_context=None):
    """注册/邀请前只做只读检查；不要无依据把旧成员切回 Codex。"""
    from autoteam.swap_seat import (
        normalize_active_limit,
        normalize_team_seat_type,
    )

    active_limit = normalize_active_limit(max_chatgpt_active)
    account_id = _team_account_id(team_context) or str(getattr(chatgpt_api, "account_id", "") or "").strip()
    members = _fetch_team_members_for_account(chatgpt_api, account_id)
    chatgpt_members = [
        item
        for item in members
        if _normalized_email(item.get("email")) and normalize_team_seat_type(item.get("seat_type")) == "chatgpt"
    ]
    precheck = {
        "mode": "registration_team_precheck",
        "team": _team_label(team_context),
        "account_id": account_id,
        "max_chatgpt_active": active_limit,
        "summary": {
            "team_members": len([item for item in members if _normalized_email(item.get("email"))]),
            "current_chatgpt_seats": len(chatgpt_members),
            "target_met": len(chatgpt_members) >= active_limit,
        },
    }
    logger.info(
        "[注册新号] 注册前只读检查: GPT seat=%d/%d；不会预切旧成员为 Codex | team=%s",
        len(chatgpt_members),
        active_limit,
        _team_label(team_context),
    )
    return active_limit, account_id, precheck


def create_new_account(chatgpt_api, mail_client, pending_invite_email: str | None = None, max_chatgpt_active=2, team_context=None):
    """只消费已有 pending invite：用对应 CF 邮箱完成注册，不再发送新 invite。"""
    import uuid

    if getattr(mail_client, "provider_name", "") != MAIL_PROVIDER_CLOUDFLARE_TEMP_EMAIL:
        raise RuntimeError("注册新号只允许使用 Cloudflare Temp Email")
    mail_client = _with_pending_invite_forwarding(mail_client)

    _, team_account_id, precheck = _pre_sweep_registration_team(
        chatgpt_api,
        max_chatgpt_active=max_chatgpt_active,
        team_context=team_context,
    )
    if precheck.get("summary", {}).get("target_met"):
        logger.info("[注册新号] 当前 GPT seat 已达到目标，跳过消费 pending invite")
        return None

    pending_invite_email = _normalized_email(pending_invite_email)
    candidates = _pending_invite_candidates(chatgpt_api, mail_client, account_id=team_account_id)
    if pending_invite_email:
        candidates = [item for item in candidates if _normalized_email(item.get("email")) == pending_invite_email]
    if not candidates:
        if pending_invite_email:
            logger.warning("[注册新号] 未找到指定 pending invite: %s", pending_invite_email)
        else:
            logger.warning("[注册新号] 当前没有可消费的 pending invite")
        return None

    last_error = None
    for candidate in candidates:
        _abort_if_cancel_requested()
        email = candidate["email"]
        password = f"Tmp_{uuid.uuid4().hex[:12]}!"
        mail_account_id = _ensure_local_pending_invite_account(
            email,
            password,
            mail_client,
            team_context=team_context,
            team_account_id=team_account_id,
        )
        logger.info("[注册新号] 尝试消费 pending invite: %s (invite_id=%s, addressId=%s)", email, candidate.get("invite_id"), mail_account_id)

        try:
            invite_link = _extract_pending_invite_link(mail_client, email)
        except TimeoutError:
            update_account(email, status=STATUS_PENDING, auth_last_error="invite_mail_missing")
            logger.warning("[注册新号] pending invite 邮件缺失/超时: %s", email)
            last_error = f"{email}: invite_mail_missing"
            continue
        except Exception as exc:
            update_account(email, status=STATUS_PENDING, auth_last_error=f"invite_mail_error: {exc}")
            logger.warning("[注册新号] 提取 pending invite 邮件失败 %s: %s", email, exc)
            last_error = f"{email}: {exc}"
            continue

        if not invite_link:
            update_account(email, status=STATUS_PENDING, auth_last_error="invite_link_missing")
            last_error = f"{email}: invite_link_missing"
            continue

        # 避免 ChatGPT Team API 浏览器和注册浏览器互相干扰。
        if _chatgpt_session_ready(chatgpt_api):
            chatgpt_api.stop()
        result = _complete_registration(email, password, invite_link, mail_client, team_context=team_context)
        if result:
            return result
        last_error = f"{email}: registration_failed"

    if last_error:
        logger.warning("[注册新号] 所有 pending invite 都未能完成注册: %s", last_error)
    return None


def create_new_invited_account(chatgpt_api, mail_client, max_chatgpt_active=2, team_context=None, invite_domains: str | None = None):
    """创建一个新 CFMail 地址，发送 Team invite，再完成注册和 PAT 上传。"""
    import uuid

    if getattr(mail_client, "provider_name", "") != MAIL_PROVIDER_CLOUDFLARE_TEMP_EMAIL:
        raise RuntimeError("新增 invite 注册只允许使用 Cloudflare Temp Email")
    mail_client = _with_pending_invite_forwarding(mail_client)

    _, team_account_id, precheck = _pre_sweep_registration_team(
        chatgpt_api,
        max_chatgpt_active=max_chatgpt_active,
        team_context=team_context,
    )
    if precheck.get("summary", {}).get("target_met"):
        logger.info("[新增 invite] 当前 GPT seat 已达到目标，跳过创建新 invite")
        return None

    retry_candidates = _autoteam_pending_invite_retry_candidates(chatgpt_api, mail_client, account_id=team_account_id)
    if retry_candidates:
        candidate = retry_candidates[0]
        acc = candidate.get("account") or {}
        email = candidate["email"]
        password = str(acc.get("password") or "") or f"Tmp_{uuid.uuid4().hex[:12]}!"
        mail_account_id = _ensure_local_pending_invite_account(
            email,
            password,
            mail_client,
            account_id=acc.get("mail_account_id") or None,
            team_context=team_context,
            team_account_id=team_account_id,
        )
        logger.info(
            "[新增 invite] 先重试已有自管 pending invite: %s (invite_id=%s, addressId=%s, team=%s)",
            email,
            candidate.get("invite_id"),
            mail_account_id,
            _team_label(team_context),
        )
        try:
            invite_link = _extract_pending_invite_link(mail_client, email)
        except TimeoutError:
            update_account(email, status=STATUS_PENDING, auth_last_error="invite_mail_missing")
            logger.warning("[新增 invite] 已有 pending invite 邮件缺失/超时: %s", email)
            return None
        except Exception as exc:
            update_account(email, status=STATUS_PENDING, auth_last_error=f"invite_mail_error: {exc}")
            logger.warning("[新增 invite] 提取已有 pending invite 邮件失败 %s: %s", email, exc)
            return None
        if not invite_link:
            update_account(email, status=STATUS_PENDING, auth_last_error="invite_link_missing")
            logger.warning("[新增 invite] 已有 pending invite 链接缺失: %s", email)
            return None

        if _chatgpt_session_ready(chatgpt_api):
            chatgpt_api.stop()
        return _complete_registration(email, password, invite_link, mail_client, team_context=team_context)

    effective_invite_domains = str(
        invite_domains or getattr(team_context, "invite_domains", "") or ""
    ).strip()
    mail_account_id, email, prefix = _create_random_cfmail_address(
        mail_client,
        invite_domains=effective_invite_domains or None,
    )
    password = f"Tmp_{uuid.uuid4().hex[:12]}!"
    _ensure_local_pending_invite_account(
        email,
        password,
        mail_client,
        account_id=mail_account_id,
        team_context=team_context,
        team_account_id=team_account_id,
    )
    logger.info(
        "[新增 invite] 已创建 CFMail 地址: %s (prefix=%s, addressId=%s, domains=%s, team=%s)",
        email,
        prefix,
        mail_account_id,
        effective_invite_domains or "<default>",
        _team_label(team_context),
    )

    status, data = chatgpt_api.invite_member(email, seat_type="usage_based")
    if status not in (200, 201, 202, 204):
        update_account(email, status=STATUS_PENDING, auth_last_error=f"invite_create_failed_http_{status}")
        raise RuntimeError(f"创建 Team invite 失败: HTTP {status} {str(data)[:200]}")

    logger.info("[新增 invite] Team invite 已发送: %s (team_account_id=%s)", email, team_account_id)
    try:
        invite_link = _extract_pending_invite_link(mail_client, email)
    except TimeoutError:
        update_account(email, status=STATUS_PENDING, auth_last_error="invite_mail_missing")
        logger.warning("[新增 invite] 邀请邮件缺失/超时: %s", email)
        return None
    except Exception as exc:
        update_account(email, status=STATUS_PENDING, auth_last_error=f"invite_mail_error: {exc}")
        logger.warning("[新增 invite] 提取邀请邮件失败 %s: %s", email, exc)
        return None

    if not invite_link:
        update_account(email, status=STATUS_PENDING, auth_last_error="invite_link_missing")
        logger.warning("[新增 invite] 邀请链接缺失: %s", email)
        return None

    # 避免 Team API HTTP session 与注册浏览器同时持有状态；注册完成后由调用方重启 API client。
    if _chatgpt_session_ready(chatgpt_api):
        chatgpt_api.stop()
    result = _complete_registration(email, password, invite_link, mail_client, team_context=team_context)
    if result:
        return result

    acc = find_account(load_accounts(), email) or {}
    if acc.get("auth_last_error"):
        update_account(email, status=STATUS_PENDING)
    else:
        update_account(email, status=STATUS_PENDING, auth_last_error="registration_failed")
    return None


def reinvite_account(chatgpt_api, mail_client, acc):
    """swap_seat-only：禁止重新邀请/本地 OAuth 登录。"""
    raise RuntimeError("swap_seat-only 模式已禁用重新邀请/本地 OAuth 登录")


def cmd_swap_seats(max_chatgpt_active=2, team_context=None):
    """CPA-driven swap_seat：只切 seat + CPA OAuth active/disabled，不 kick、不本地管理 auth。"""
    from autoteam.swap_seat import cmd_swap_seats as _cmd_swap_seats

    return _cmd_swap_seats(max_chatgpt_active=max_chatgpt_active, team_context=team_context)


def cmd_rotate(target_seats=5, force_auth_repair=False):
    """swap_seat-only：兼容旧 rotate 命令，实际只执行 swap_seat。"""
    logger.info("[轮转] swap_seat-only：不 kick、不补号，只按 CPA quota 收敛 seat/OAuth")
    return cmd_swap_seats(max_chatgpt_active=target_seats)


def cmd_add(pending_invite_email: str | None = None, max_chatgpt_active=2, team_context=None):
    """消费一个已有 pending invite：注册完成后旧号 Codex，新号 GPT，全程不 kick。"""
    from autoteam.swap_seat import normalize_active_limit

    _abort_if_cancel_requested()
    active_limit = normalize_active_limit(max_chatgpt_active)
    chatgpt = ChatGPTTeamAPI()
    _start_chatgpt_for_team(chatgpt, team_context)
    mail_client = None

    try:
        mail_client = get_mail_client(provider=MAIL_PROVIDER_CLOUDFLARE_TEMP_EMAIL)
        if getattr(mail_client, "provider_name", "") != MAIL_PROVIDER_CLOUDFLARE_TEMP_EMAIL:
            raise RuntimeError("注册新号只允许使用 Cloudflare Temp Email")
        mail_client.login()
        mail_client = _with_pending_invite_forwarding(mail_client)

        result_email = create_new_account(
            chatgpt,
            mail_client,
            pending_invite_email=pending_invite_email,
            max_chatgpt_active=active_limit,
            team_context=team_context,
        )
        if result_email:
            if not _chatgpt_session_ready(chatgpt):
                _start_chatgpt_for_team(chatgpt, team_context)
            activation = _activate_registered_account(
                chatgpt,
                result_email,
                max_chatgpt_active=active_limit,
                team_context=team_context,
            )
            rebalance = _post_registration_seat_rebalance(
                chatgpt,
                max_chatgpt_active=active_limit,
                team_context=team_context,
            )
            return {
                "mode": "consume_pending_invite",
                "invited": True,
                "team": _team_label(team_context),
                "account_id": _team_account_id(team_context),
                "email": result_email,
                "reason": "pending_invite_registered",
                "requested_email": _normalized_email(pending_invite_email),
                "max_chatgpt_active": active_limit,
                "activation": activation,
                "rebalance": rebalance,
            }
        return {
            "mode": "consume_pending_invite",
            "invited": False,
            "team": _team_label(team_context),
            "account_id": _team_account_id(team_context),
            "reason": "no_usable_pending_invite",
            "requested_email": _normalized_email(pending_invite_email),
            "max_chatgpt_active": active_limit,
        }
    finally:
        try:
            chatgpt.allow_team_invites = False
        except Exception:
            pass
        if _chatgpt_session_ready(chatgpt):
            chatgpt.stop()


def cmd_invite_add(
    max_chatgpt_active=2,
    team_context=None,
    *,
    force_create_invite: bool = False,
    invite_domains: str | None = None,
):
    """新增 Team invite：创建随机 CFMail 邮箱，邀请注册，生成 PAT 并上传 CPA。"""
    from autoteam.swap_seat import normalize_active_limit

    _abort_if_cancel_requested()
    if not force_create_invite:
        raise RuntimeError("新增 invite 会真实发送 Team invite；请使用 --force-create-invite 或 API force_create_invite=true 显式确认")
    active_limit = normalize_active_limit(max_chatgpt_active)
    chatgpt = ChatGPTTeamAPI()
    _start_chatgpt_for_team(chatgpt, team_context)

    try:
        mail_client = get_mail_client(provider=MAIL_PROVIDER_CLOUDFLARE_TEMP_EMAIL)
        if getattr(mail_client, "provider_name", "") != MAIL_PROVIDER_CLOUDFLARE_TEMP_EMAIL:
            raise RuntimeError("新增 invite 注册只允许使用 Cloudflare Temp Email")
        mail_client.login()
        mail_client = _with_pending_invite_forwarding(mail_client)

        result_email = create_new_invited_account(
            chatgpt,
            mail_client,
            max_chatgpt_active=active_limit,
            team_context=team_context,
            invite_domains=invite_domains,
        )
        if result_email:
            if not _chatgpt_session_ready(chatgpt):
                _start_chatgpt_for_team(chatgpt, team_context)
            activation = _activate_registered_account(
                chatgpt,
                result_email,
                max_chatgpt_active=active_limit,
                team_context=team_context,
            )
            rebalance = _post_registration_seat_rebalance(
                chatgpt,
                max_chatgpt_active=active_limit,
                team_context=team_context,
            )
            return {
                "mode": "create_invite",
                "invited": True,
                "team": _team_label(team_context),
                "account_id": _team_account_id(team_context),
                "email": result_email,
                "reason": "new_invite_registered",
                "max_chatgpt_active": active_limit,
                "invite_domains": str(invite_domains or getattr(team_context, "invite_domains", "") or "").strip(),
                "activation": activation,
                "rebalance": rebalance,
            }

        return {
            "mode": "create_invite",
            "invited": False,
            "team": _team_label(team_context),
            "account_id": _team_account_id(team_context),
            "reason": "new_invite_registration_failed",
            "max_chatgpt_active": active_limit,
            "invite_domains": str(invite_domains or getattr(team_context, "invite_domains", "") or "").strip(),
        }
    finally:
        try:
            chatgpt.allow_team_invites = False
        except Exception:
            pass
        if _chatgpt_session_ready(chatgpt):
            chatgpt.stop()


def _run_team_invite_worker(team_context, callback):
    chatgpt = ChatGPTTeamAPI()
    try:
        _abort_if_cancel_requested()
        _start_chatgpt_for_team(chatgpt, team_context)
        return callback(chatgpt)
    finally:
        if _chatgpt_session_ready(chatgpt):
            chatgpt.stop()


def cmd_clear_pending_invites(*, team_context=None, concurrency: int = 4) -> dict:
    """显式清空当前 Team 的 pending invites；不删除 member。"""
    _abort_if_cancel_requested()
    concurrency = _normalize_invite_concurrency(concurrency, default=4, maximum=8)
    chatgpt = ChatGPTTeamAPI()
    _start_chatgpt_for_team(chatgpt, team_context)
    try:
        invites = chatgpt.list_invites()
    finally:
        if _chatgpt_session_ready(chatgpt):
            chatgpt.stop()

    emails = []
    for invite in invites or []:
        if not _is_pending_invite(invite):
            continue
        email = _invite_email(invite)
        if email and email not in emails:
            emails.append(email)

    if not emails:
        logger.info("[pending invite 清理] Team=%s 扫描 %d 条，未发现 pending invite", _team_label(team_context), len(invites or []))
        return {
            "mode": "clear_pending_invites",
            "team": _team_label(team_context),
            "account_id": _team_account_id(team_context),
            "scanned": len(invites or []),
            "deleted": [],
            "failed": [],
            "summary": {"scanned": len(invites or []), "deleted": 0, "failed": 0},
        }

    logger.info(
        "[pending invite 清理] Team=%s 扫描 %d 条，准备清理 %d 条，并发=%d",
        _team_label(team_context),
        len(invites or []),
        len(emails),
        concurrency,
    )

    worker_count = min(concurrency, len(emails))
    email_batches = [[] for _ in range(worker_count)]
    for index, email in enumerate(emails):
        email_batches[index % worker_count].append(email)

    def delete_batch(batch: list[str], batch_index: int):
        def _delete(chatgpt_api):
            batch_deleted = []
            batch_failed = []
            for email in batch:
                _abort_if_cancel_requested()
                try:
                    status, data = chatgpt_api.cancel_invite(email)
                    if status not in (200, 201, 202, 204):
                        raise RuntimeError(f"HTTP {status} {str(data)[:200]}")
                    if isinstance(data, dict) and data.get("success") is False:
                        raise RuntimeError(str(data)[:200])
                    batch_deleted.append({"email": email, "status": status})
                except Exception as exc:
                    logger.warning("[pending invite 清理] %s 失败: %s", email, exc)
                    batch_failed.append({"email": email, "error": str(exc)})
            return {"batch": batch_index, "deleted": batch_deleted, "failed": batch_failed}

        return _run_team_invite_worker(team_context, _delete)

    deleted = []
    failed = []
    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        futures = {
            executor.submit(delete_batch, batch, index + 1): batch
            for index, batch in enumerate(email_batches)
            if batch
        }
        for future in as_completed(futures):
            _abort_if_cancel_requested()
            batch = futures[future]
            try:
                result = future.result()
                deleted.extend(result.get("deleted") or [])
                failed.extend(result.get("failed") or [])
            except Exception as exc:
                logger.warning("[pending invite 清理] batch 失败: %s", exc)
                failed.extend({"email": email, "error": str(exc)} for email in batch)

    deleted.sort(key=lambda item: item.get("email") or "")
    failed.sort(key=lambda item: item.get("email") or "")
    logger.info(
        "[pending invite 清理] Team=%s 完成：扫描=%d 清理=%d 失败=%d",
        _team_label(team_context),
        len(invites or []),
        len(deleted),
        len(failed),
    )
    return {
        "mode": "clear_pending_invites",
        "team": _team_label(team_context),
        "account_id": _team_account_id(team_context),
        "scanned": len(invites or []),
        "concurrency": concurrency,
        "deleted": deleted,
        "failed": failed,
        "summary": {"scanned": len(invites or []), "deleted": len(deleted), "failed": len(failed)},
    }


def cmd_bulk_invite(
    emails,
    *,
    team_context=None,
    seat_type: str = "usage_based",
    concurrency: int = 3,
    batch_size: int = 5,
    resend_emails: bool = True,
) -> dict:
    """并发批量发送 Team invite；只发送邀请，不注册账号/不生成 PAT。"""
    _abort_if_cancel_requested()
    email_list, invalid = _parse_email_list(emails)
    if not email_list:
        raise RuntimeError("批量 invite 需要至少 1 个有效邮箱")

    seat_type = str(seat_type or "usage_based").strip()
    if seat_type not in {"usage_based", "default"}:
        raise RuntimeError("seat_type 只允许 usage_based 或 default")

    concurrency = _normalize_invite_concurrency(concurrency, default=3, maximum=8)
    batch_size = _normalize_invite_batch_size(batch_size, default=5, maximum=50)
    batches = list(_chunked(email_list, batch_size))

    def send_batch(batch: list[str], batch_index: int):
        def _send(chatgpt_api):
            status, data = chatgpt_api.invite_members(batch, seat_type=seat_type, resend_emails=resend_emails)
            if status not in (200, 201, 202, 204):
                raise RuntimeError(f"HTTP {status} {str(data)[:200]}")
            errored = []
            accepted = batch
            if isinstance(data, dict):
                raw_errors = data.get("errored_emails") if isinstance(data.get("errored_emails"), list) else []
                errored = [
                    _normalized_email(item.get("email") or item.get("email_address") if isinstance(item, dict) else item)
                    for item in raw_errors
                ]
                errored = [email for email in errored if email]
                if errored:
                    accepted = [email for email in batch if email not in set(errored)]
            return {"batch": batch_index, "status": status, "sent": accepted, "errored": errored}

        return _run_team_invite_worker(team_context, _send)

    sent = []
    failed = []
    batch_results = []
    with ThreadPoolExecutor(max_workers=concurrency) as executor:
        futures = {
            executor.submit(send_batch, batch, index + 1): (index + 1, batch)
            for index, batch in enumerate(batches)
        }
        for future in as_completed(futures):
            _abort_if_cancel_requested()
            batch_index, batch = futures[future]
            try:
                result = future.result()
                batch_results.append(result)
                sent.extend(result.get("sent") or [])
                for email in result.get("errored") or []:
                    failed.append({"email": email, "error": "errored_by_chatgpt"})
            except Exception as exc:
                logger.warning("[批量 invite] batch %s 失败: %s", batch_index, exc)
                failed.extend({"email": email, "error": str(exc)} for email in batch)

    verified_after_error = []
    if failed:
        try:
            def _list_invites(chatgpt_api):
                return chatgpt_api.list_invites()

            remote_invites = _run_team_invite_worker(team_context, _list_invites)
            remote_pending = {
                _invite_email(invite)
                for invite in remote_invites or []
                if _is_pending_invite(invite)
            }
            remaining_failed = []
            for item in failed:
                email = _normalized_email(item.get("email"))
                if email and email in remote_pending:
                    sent.append(email)
                    verified_after_error.append(email)
                else:
                    remaining_failed.append(item)
            if verified_after_error:
                logger.info(
                    "[批量 invite] 复查确认 %d 个超时/失败邮箱已创建 pending invite",
                    len(verified_after_error),
                )
            failed = remaining_failed
        except Exception as exc:
            logger.warning("[批量 invite] 失败后复查 pending invite 失败: %s", exc)

    sent = sorted(dict.fromkeys(sent))
    verified_after_error = sorted(dict.fromkeys(verified_after_error))
    failed.sort(key=lambda item: item.get("email") or "")
    return {
        "mode": "bulk_invite",
        "team": _team_label(team_context),
        "account_id": _team_account_id(team_context),
        "requested": len(email_list),
        "invalid": invalid,
        "seat_type": seat_type,
        "concurrency": concurrency,
        "batch_size": batch_size,
        "batches": sorted(batch_results, key=lambda item: item.get("batch") or 0),
        "sent": sent,
        "failed": failed,
        "verified_after_error": verified_after_error,
        "summary": {
            "requested": len(email_list),
            "sent": len(sent),
            "failed": len(failed),
            "invalid": len(invalid),
            "batches": len(batches),
            "verified_after_error": len(verified_after_error),
        },
    }


def _normalize_auto_replace_mode(value: str | None = "pending_invite") -> str:
    mode = str(value or "pending_invite").strip().lower().replace("-", "_")
    if mode in {"pending", "pending_invite"}:
        return "pending_invite"
    if mode in {"create", "create_invite", "invite_add"}:
        return "create_invite"
    raise ValueError(f"未知自动补位模式: {value}")


def _auto_replace_trigger_from_swap(swap_result: dict, active_limit: int) -> str | None:
    """根据 swap 后可预期的 GPT seat 数判断是否需要补位。"""
    summary = swap_result.get("summary") if isinstance(swap_result.get("summary"), dict) else {}
    active_limit = max(1, int(active_limit or 1))
    selected_chatgpt = int(summary.get("selected_chatgpt") or 0)
    protected_chatgpt = int(summary.get("protected_chatgpt_seats") or 0)
    whitelisted_chatgpt = int(summary.get("whitelisted_chatgpt_seats") or 0)
    effective_chatgpt_after_swap = selected_chatgpt + protected_chatgpt + whitelisted_chatgpt
    if effective_chatgpt_after_swap >= active_limit:
        return None

    quota_available_present = "quota_available" in summary
    quota_available = int(summary.get("quota_available") or 0)
    current_chatgpt_seats = int(summary.get("current_chatgpt_seats") or 0)
    if swap_result.get("skipped") and swap_result.get("reason") == "no_quota_available":
        return "no_quota_available"
    if quota_available_present and quota_available <= 0:
        return "no_quota_available"
    if current_chatgpt_seats <= 0:
        return "no_current_chatgpt_seat"
    if effective_chatgpt_after_swap < active_limit:
        return "below_target_chatgpt_seats"
    return None


def cmd_auto_detect_replace(
    max_chatgpt_active=2,
    pending_invite_email: str | None = None,
    team_context=None,
    *,
    repair_before_replace: bool = True,
    replace_mode: str = "pending_invite",
):
    """自动检测：先执行 swap_seat；若 swap 后 GPT seat 低于目标，则按模式补位。"""
    from autoteam.swap_seat import normalize_active_limit

    active_limit = normalize_active_limit(max_chatgpt_active)
    replace_mode = _normalize_auto_replace_mode(replace_mode)
    swap_result = cmd_swap_seats(max_chatgpt_active=active_limit, team_context=team_context)
    trigger = _auto_replace_trigger_from_swap(swap_result, active_limit)
    repair_result = None

    if trigger and repair_before_replace:
        try:
            repair_result = cmd_repair_pat_auths(limit=2, team_context=team_context, allow_promote_for_use=True)
        except Exception as exc:
            logger.warning("[自动替换] PAT 修复跳过/失败，继续按 swap 结果判断是否补位: %s", exc)
        else:
            repaired = int((repair_result.get("summary") or {}).get("repaired") or 0)
            if repaired:
                logger.info("[自动替换] PAT 修复成功 %d 个，重新执行 swap_seat 后再判断是否需要补位", repaired)
                try:
                    swap_result = cmd_swap_seats(max_chatgpt_active=active_limit, team_context=team_context)
                except Exception as exc:
                    logger.warning("[自动替换] PAT 修复后 swap_seat 复核失败，本轮不补位，等待下轮巡检: %s", exc)
                    return {
                        "mode": "auto_detect_replace",
                        "team": _team_label(team_context),
                        "account_id": _team_account_id(team_context),
                        "replaced": False,
                        "reason": "pat_repair_recheck_failed",
                        "trigger": trigger,
                        "replace_mode": replace_mode,
                        "swap_result": swap_result,
                        "repair_result": repair_result,
                        "error": str(exc),
                    }
                trigger = _auto_replace_trigger_from_swap(swap_result, active_limit)

    if trigger:
        logger.info("[自动替换] 触发补位: %s mode=%s | team=%s", trigger, replace_mode, _team_label(team_context))
        if replace_mode == "create_invite":
            replace_result = cmd_invite_add(
                max_chatgpt_active=active_limit,
                team_context=team_context,
                force_create_invite=True,
            )
        else:
            replace_result = cmd_add(
                pending_invite_email=pending_invite_email,
                max_chatgpt_active=active_limit,
                team_context=team_context,
            )
        return {
            "mode": "auto_detect_replace",
            "team": _team_label(team_context),
            "account_id": _team_account_id(team_context),
            "replaced": bool(replace_result.get("invited")),
            "reason": replace_result.get("reason"),
            "trigger": trigger,
            "replace_mode": replace_mode,
            "swap_result": swap_result,
            "repair_result": repair_result,
            "replace_result": replace_result,
        }
    logger.info("[自动替换] GPT seat 已达到目标或 swap 已完成，本轮不补位")
    return {
        "mode": "auto_detect_replace",
        "team": _team_label(team_context),
        "account_id": _team_account_id(team_context),
        "replaced": False,
        "reason": "target_met_or_swap_done",
        "trigger": None,
        "replace_mode": replace_mode,
        "swap_result": swap_result,
        "repair_result": repair_result,
    }


def cmd_manage_teams(
    max_chatgpt_active=2,
    replace_with_pending_invite=True,
    pat_repair_already_run=False,
    replace_mode: str = "pending_invite",
):
    """多 Team 调度：逐个 Team 确保 GPT seat 达标；低于目标时按模式补位。"""
    from autoteam.team_context import get_team_contexts, normalize_active_limit

    default_limit = normalize_active_limit(max_chatgpt_active)
    replace_mode = _normalize_auto_replace_mode(replace_mode)
    teams = get_team_contexts(default_max_chatgpt_active=default_limit)
    results = []
    for team in teams:
        _abort_if_cancel_requested()
        team_limit = normalize_active_limit(getattr(team, "max_chatgpt_active", default_limit), default=default_limit)
        logger.info("[多 Team] 开始调度: %s (%s), active=%d", team.label, team.account_id, team_limit)
        try:
            if replace_with_pending_invite:
                result = cmd_auto_detect_replace(
                    max_chatgpt_active=team_limit,
                    pending_invite_email=getattr(team, "pending_invite_email", "") or None,
                    team_context=team,
                    repair_before_replace=not bool(pat_repair_already_run),
                    replace_mode=replace_mode,
                )
            else:
                result = cmd_swap_seats(max_chatgpt_active=team_limit, team_context=team)
            results.append({"ok": True, "team": team.public_dict(), "result": result})
        except Exception as exc:
            logger.warning("[多 Team] Team 调度失败 %s: %s", team.label, exc)
            results.append({"ok": False, "team": team.public_dict(), "error": str(exc)})

    return {
        "mode": "multi_team_manage",
        "replace_with_pending_invite": bool(replace_with_pending_invite),
        "replace_mode": replace_mode,
        "teams_total": len(teams),
        "teams_ok": sum(1 for item in results if item.get("ok")),
        "teams_failed": sum(1 for item in results if not item.get("ok")),
        "results": results,
    }


def cmd_manual_add():
    """手动添加账号：优先自动接收 localhost 回调，失败时再手动粘贴回调 URL。"""
    logger.warning("[手动添加] swap_seat-only 模式已禁用；OAuth/auth 请在 CPA 管理")
    return {"mode": "swap_seat", "skipped": True, "reason": "manual_add_disabled"}

    from autoteam.manual_account import ManualAccountFlow

    flow = ManualAccountFlow()
    try:
        result = flow.start()
        logger.info("[手动添加] 打开以下链接完成 OAuth 登录：\n%s", result["auth_url"])
        if result.get("auto_callback_available"):
            logger.info("[手动添加] 已启动本地回调服务 http://localhost:1455/auth/callback，可自动完成认证")
        else:
            logger.warning("[手动添加] 本地自动回调不可用：%s", result.get("auto_callback_error") or "未知错误")

        callback_url = input("登录成功后：若自动完成则直接回车；否则粘贴回调 URL（留空取消）: ").strip()
        if callback_url:
            result = flow.submit_callback(callback_url)
        else:
            result = flow.status()
            if result.get("status") != "completed":
                logger.warning("[手动添加] 未检测到自动回调，已取消")
                return None

        account = result.get("account") or {}
        logger.info(
            "[手动添加] 完成: %s (plan=%s, status=%s)",
            account.get("email") or "?",
            account.get("plan_type") or "?",
            account.get("status") or "?",
        )
        return result
    finally:
        flow.stop()


def cmd_admin_login(email=None):
    """交互式完成管理员登录并保存到 state.json。"""
    email = (email or "").strip()
    if not email:
        email = input("管理员邮箱: ").strip()

    if not email:
        logger.error("[管理员登录] 邮箱不能为空")
        return None

    chatgpt = ChatGPTTeamAPI()

    try:
        logger.info("[管理员登录] 开始: %s", email)
        result = chatgpt.begin_admin_login(email)
        step = result.get("step")

        while True:
            if step == "completed":
                info = chatgpt.complete_admin_login()
                chatgpt.stop()
                logger.info("[管理员登录] 登录完成: %s", info.get("email") or email)
                if info.get("account_id"):
                    logger.info("[管理员登录] Workspace ID: %s", info["account_id"])
                if info.get("workspace_name"):
                    logger.info("[管理员登录] Workspace 名称: %s", info["workspace_name"])
                return info

            if step == "password_required":
                password = getpass.getpass("管理员密码（留空取消）: ")
                if not password:
                    logger.warning("[管理员登录] 已取消")
                    return None
                result = chatgpt.submit_admin_password(password)
                step = result.get("step")
                continue

            if step == "code_required":
                code = input("邮箱验证码（留空取消）: ").strip()
                if not code:
                    logger.warning("[管理员登录] 已取消")
                    return None
                result = chatgpt.submit_admin_code(code)
                step = result.get("step")
                continue

            if step == "workspace_required":
                options = chatgpt.list_workspace_options()
                if not options:
                    raise RuntimeError("当前需要选择组织，但未获取到可选项")

                logger.info("[管理员登录] 请选择要进入的 workspace:")
                for idx, option in enumerate(options, 1):
                    suffix = " [推荐]" if option.get("kind") == "preferred" else ""
                    logger.info("[管理员登录]   %d. %s%s", idx, option["label"], suffix)

                choice = input("选择序号（留空取消）: ").strip()
                if not choice:
                    logger.warning("[管理员登录] 已取消")
                    return None
                if not choice.isdigit():
                    raise RuntimeError(f"无效的序号: {choice}")

                selected_index = int(choice) - 1
                if selected_index < 0 or selected_index >= len(options):
                    raise RuntimeError(f"序号超出范围: {choice}")

                result = chatgpt.select_workspace_option(options[selected_index]["id"])
                step = result.get("step")
                continue

            detail = result.get("detail") or "无法识别管理员登录步骤"
            raise RuntimeError(detail)

    except KeyboardInterrupt:
        logger.warning("[管理员登录] 已中断")
        return None
    finally:
        chatgpt.stop()


def cmd_admin_session(email=None):
    """手动导入管理员 session_token 并保存到 state.json。"""
    email = (email or "").strip()
    if not email:
        email = input("管理员邮箱: ").strip()

    if not email:
        logger.error("[管理员登录] 邮箱不能为空")
        return None

    session_token = getpass.getpass("session_token（留空取消）: ").strip()
    if not session_token:
        logger.warning("[管理员登录] 已取消")
        return None

    chatgpt = ChatGPTTeamAPI()
    try:
        logger.info("[管理员登录] 开始导入 session_token: %s", email)
        info = chatgpt.import_admin_session(email, session_token)
        chatgpt.stop()
        logger.info("[管理员登录] session_token 导入完成: %s", info.get("email") or email)
        if info.get("account_id"):
            logger.info("[管理员登录] Workspace ID: %s", info["account_id"])
        if info.get("workspace_name"):
            logger.info("[管理员登录] Workspace 名称: %s", info["workspace_name"])
        return info
    finally:
        chatgpt.stop()


def cmd_main_codex_sync():
    """交互式同步主号 Codex 认证到已启用远端。"""
    logger.warning("[主号 Codex] swap_seat-only 模式已禁用本地/远端主号 OAuth 同步；母号 seat 会强制保持 Codex")
    return {"mode": "swap_seat", "skipped": True, "reason": "main_codex_sync_disabled"}

    state = get_admin_state_summary()
    if not state.get("session_present") or not state.get("email"):
        logger.error("[主号 Codex] 缺少管理员登录态，请先执行 admin-login")
        return None

    saved_auth_file = get_saved_main_auth_file()
    if saved_auth_file:
        sync_main_codex_to_cpa(saved_auth_file)
        logger.info("[主号 Codex] 已直接同步现有认证文件: %s", saved_auth_file)
        return {"auth_file": saved_auth_file}

    flow = MainCodexSyncFlow()
    try:
        logger.info("[主号 Codex] 开始同步: %s", state.get("email"))
        result = flow.start()
        step = result.get("step")

        while True:
            if step == "completed":
                info = flow.complete()
                logger.info("[主号 Codex] 同步完成: %s", info.get("email") or state.get("email"))
                if info.get("plan_type"):
                    logger.info("[主号 Codex] Plan: %s", info["plan_type"])
                if info.get("auth_file"):
                    logger.info("[主号 Codex] Auth 文件: %s", info["auth_file"])
                return info

            if step == "password_required":
                password = getpass.getpass("主号密码（留空取消）: ")
                if not password:
                    logger.warning("[主号 Codex] 已取消")
                    return None
                result = flow.submit_password(password)
                step = result.get("step")
                continue

            if step == "code_required":
                code = input("主号验证码（留空取消）: ").strip()
                if not code:
                    logger.warning("[主号 Codex] 已取消")
                    return None
                result = flow.submit_code(code)
                step = result.get("step")
                continue

            detail = result.get("detail") or "无法识别主号 Codex 登录步骤"
            raise RuntimeError(detail)
    except KeyboardInterrupt:
        logger.warning("[主号 Codex] 已中断")
        return None
    finally:
        flow.stop()


def get_team_member_count(chatgpt_api):
    """获取当前 Team 成员数"""
    account_id = get_chatgpt_account_id()
    if not account_id:
        logger.error("[Team] account_id 为空，无法查询成员数")
        return -1
    try:
        members = _fetch_team_members_for_account(chatgpt_api, account_id)
    except Exception as exc:
        logger.error("[Team] 获取成员列表失败: %s", exc)
        return -1
    return len(members)


def cmd_fill(target=5):
    """swap_seat-only：兼容旧 fill 命令，实际只执行 swap_seat。"""
    logger.info("[填充] swap_seat-only 模式已禁用补号；改为执行 seat/OAuth 收敛")
    return cmd_swap_seats(max_chatgpt_active=target)


def cmd_cleanup(max_seats=None):
    """swap_seat-only：兼容旧 cleanup 命令，实际只执行 swap_seat。"""
    logger.info("[清理] swap_seat-only 模式已禁用 Team member 移除；改为执行 seat/OAuth 收敛")
    return cmd_swap_seats(max_chatgpt_active=max_seats or 2)


def cmd_reset_quota_recovery():
    """清空自管且未禁用账号的本地额度恢复记录。"""
    _abort_if_cancel_requested()
    accounts = load_accounts()
    if not accounts:
        summary = {
            "total_accounts": 0,
            "updated_accounts": 0,
            "rearmed_exhausted_to_active": 0,
            "rearmed_exhausted_to_auth_pending": 0,
            "skipped_disabled": 0,
            "skipped_unmanaged": 0,
        }
        logger.info("[额度重置] 本地无账号记录")
        return summary

    total_accounts = 0
    updated_accounts = 0
    rearmed_to_active = 0
    rearmed_to_auth_pending = 0
    skipped_disabled = 0
    skipped_unmanaged = 0

    for acc in accounts:
        _abort_if_cancel_requested()
        email = acc.get("email", "")
        if _is_main_account_email(email):
            continue
        if is_account_disabled(acc):
            skipped_disabled += 1
            continue
        if not _account_looks_autoteam_created(acc):
            skipped_unmanaged += 1
            continue

        total_accounts += 1
        changed = False

        if acc.get("last_quota") is not None:
            acc["last_quota"] = None
            changed = True
        if acc.get("quota_resets_at") is not None:
            acc["quota_resets_at"] = None
            changed = True
        if acc.get("quota_exhausted_at") is not None:
            acc["quota_exhausted_at"] = None
            changed = True
        if acc.get("quota_window") is not None:
            acc["quota_window"] = None
            changed = True

        if acc.get("status") == STATUS_EXHAUSTED:
            desired_status = STATUS_ACTIVE if _has_auth_file(acc) else STATUS_AUTH_PENDING
            if acc.get("status") != desired_status:
                acc["status"] = desired_status
                changed = True
            if desired_status == STATUS_ACTIVE:
                rearmed_to_active += 1
            else:
                rearmed_to_auth_pending += 1

        if changed:
            updated_accounts += 1

    if updated_accounts:
        save_accounts(accounts)

    summary = {
        "total_accounts": total_accounts,
        "updated_accounts": updated_accounts,
        "rearmed_exhausted_to_active": rearmed_to_active,
        "rearmed_exhausted_to_auth_pending": rearmed_to_auth_pending,
        "skipped_disabled": skipped_disabled,
        "skipped_unmanaged": skipped_unmanaged,
    }
    logger.info(
        (
            "[额度重置] 完成: 扫描自管未禁用 %d 个账号，更新 %d 个，"
            "恢复 exhausted -> active %d 个，exhausted -> auth_pending %d 个，"
            "跳过 disabled %d 个，跳过非自管 %d 个"
        ),
        total_accounts,
        updated_accounts,
        rearmed_to_active,
        rearmed_to_auth_pending,
        skipped_disabled,
        skipped_unmanaged,
    )
    return summary


def cmd_pull_cpa():
    """从 CPA 反向同步认证文件到本地。"""
    logger.warning("[CPA] swap_seat-only 模式不再把 CPA OAuth 拉回本地；AutoTeam 只读 CPA 并启停 OAuth")
    return {"mode": "swap_seat", "skipped": True, "reason": "pull_cpa_disabled"}

    result = sync_from_cpa()
    logger.info(
        "[CPA] 拉取完成: 新增文件 %d, 更新文件 %d, 新增账号 %d, 更新账号 %d, 跳过 %d",
        result.get("downloaded", 0),
        result.get("updated", 0),
        result.get("accounts_added", 0),
        result.get("accounts_updated", 0),
        result.get("skipped", 0),
    )
    return result


def cmd_sync_disabled():
    """swap_seat-only：禁止旧 sync 命令批量同步/删除远端 auth。"""
    _ = sync_to_cpa  # 保留历史 monkeypatch/导入兼容，但不执行。
    logger.warning("[同步] swap_seat-only 模式已禁用旧 sync；不会上传、删除或覆盖远端 auth")
    return {"mode": "swap_seat", "skipped": True, "reason": "sync_disabled"}


def main():
    import argparse

    parser = argparse.ArgumentParser(
        prog="manager.py",
        description="swap_seat-only Team seat/OAuth 调度器（不 kick/remove/cancel invite）",
    )
    sub = parser.add_subparsers(dest="command", help="可用命令")

    sub.add_parser("status", help="查看所有账号状态")
    sub.add_parser("check", help="检查 CPA quota 并执行 swap_seat 收敛")
    rotate_p = sub.add_parser("rotate", help="兼容旧命令：实际执行 swap_seat，不移出、不邀请")
    rotate_p.add_argument("target", type=int, nargs="?", default=2, help="ChatGPT/OAuth active 保留数 1~5（默认 2）")
    swap_p = sub.add_parser("swap-seats", help="CPA 驱动 seat 切换（保留 1~5 个 ChatGPT/OAuth active，不 kick）")
    swap_p.add_argument("max_chatgpt_active", type=int, nargs="?", default=2, help="ChatGPT/OAuth active 保留数 1~5（默认 2）")
    auto_replace_p = sub.add_parser("auto-detect-replace", help="先 swap_seat；若 GPT seat 低于目标，则按模式补位")
    auto_replace_p.add_argument("max_chatgpt_active", type=int, nargs="?", default=2, help="ChatGPT/OAuth active 保留数 1~5（默认 2）")
    auto_replace_p.add_argument("--email", help="指定要消费的 pending invite 邮箱")
    auto_replace_p.add_argument("--replace-mode", choices=["pending_invite", "create_invite"], default="pending_invite", help="补位模式：消费已有 pending invite 或创建新 CFMail invite")
    multi_team_p = sub.add_parser("manage-teams", help="按 TEAM_WORKSPACES_JSON 逐个 Team 执行 quota 检查与必要替换")
    multi_team_p.add_argument("max_chatgpt_active", type=int, nargs="?", default=2, help="默认 ChatGPT/OAuth active 保留数 1~5")
    multi_team_p.add_argument("--no-replace", action="store_true", help="只执行 swap_seat，不在 GPT seat 低于目标时补位")
    multi_team_p.add_argument("--replace-mode", choices=["pending_invite", "create_invite"], default="pending_invite", help="补位模式：消费已有 pending invite 或创建新 CFMail invite")
    add_p = sub.add_parser("add", help="消费已有 pending invite 注册新号（不会发送新 invite）")
    add_p.add_argument("--email", help="指定要消费的 pending invite 邮箱；不传则自动选择")
    invite_add_p = sub.add_parser("invite-add", help="创建随机 CFMail、发送新 Team invite 并注册上传 PAT")
    invite_add_p.add_argument("max_chatgpt_active", type=int, nargs="?", default=2, help="ChatGPT/OAuth active 保留数 1~5（默认 2）")
    invite_add_p.add_argument("--force-create-invite", action="store_true", help="确认真实创建并发送一个新的 Team invite")
    sub.add_parser("manual-add", help="已停用：OAuth/auth 由 CPA 管理")
    admin_login_p = sub.add_parser("admin-login", help="交互式完成管理员主号登录")
    admin_login_p.add_argument("--email", help="管理员邮箱；不传则运行时交互输入")
    admin_session_p = sub.add_parser("admin-session", help="手动输入 session_token 导入管理员登录态")
    admin_session_p.add_argument("--email", help="管理员邮箱；不传则运行时交互输入")
    sub.add_parser("main-codex-sync", help="交互式同步主号 Codex 到已启用远端")

    fill_p = sub.add_parser("fill", help="已归档兼容命令：实际执行 swap_seat，不补号")
    fill_p.add_argument("target", type=int, nargs="?", default=2, help="ChatGPT/OAuth active 保留数 1~5（默认 2）")

    cleanup_p = sub.add_parser("cleanup", help="已归档兼容命令：实际执行 swap_seat，不移除成员")
    cleanup_p.add_argument("max_seats", type=int, nargs="?", default=None, help="ChatGPT/OAuth active 保留数 1~5")

    sub.add_parser("reset-quota", help="清空本地额度恢复记录，并把 exhausted 账号恢复为可检查状态")

    sub.add_parser("sync", help="已停用：auth 由 CPA 管理")
    sub.add_parser("pull-cpa", help="已停用：AutoTeam 不再拉取 CPA auth 到本地")

    api_p = sub.add_parser("api", help="启动 HTTP API 服务器")
    api_p.add_argument("--host", default="0.0.0.0", help="监听地址（默认 0.0.0.0）")
    api_p.add_argument("--port", type=int, default=8787, help="监听端口（默认 8787）")

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(0)

    # 首次启动检查必填配置（api 命令在 start_server 里单独处理）
    if args.command not in ("api",):
        from autoteam.setup_wizard import check_and_setup

        check_and_setup(interactive=True)

    try:
        from autoteam.auth_storage import ensure_auth_file_permissions

        ensure_auth_file_permissions()
    except Exception:
        pass

    if args.command == "status":
        cmd_status()
    elif args.command == "check":
        cmd_check(force_auth_repair=True)
    elif args.command == "rotate":
        cmd_rotate(args.target, force_auth_repair=True)
    elif args.command == "swap-seats":
        cmd_swap_seats(args.max_chatgpt_active)
    elif args.command == "auto-detect-replace":
        cmd_auto_detect_replace(args.max_chatgpt_active, pending_invite_email=args.email, replace_mode=args.replace_mode)
    elif args.command == "manage-teams":
        cmd_manage_teams(
            args.max_chatgpt_active,
            replace_with_pending_invite=not args.no_replace,
            replace_mode=args.replace_mode,
        )
    elif args.command == "add":
        cmd_add(pending_invite_email=args.email)
    elif args.command == "invite-add":
        cmd_invite_add(args.max_chatgpt_active, force_create_invite=args.force_create_invite)
    elif args.command == "manual-add":
        cmd_manual_add()
    elif args.command == "admin-login":
        cmd_admin_login(args.email)
    elif args.command == "admin-session":
        cmd_admin_session(args.email)
    elif args.command == "main-codex-sync":
        cmd_main_codex_sync()
    elif args.command == "fill":
        cmd_fill(args.target)
    elif args.command == "cleanup":
        cmd_cleanup(args.max_seats)
    elif args.command == "reset-quota":
        cmd_reset_quota_recovery()
    elif args.command == "sync":
        cmd_sync_disabled()
    elif args.command == "pull-cpa":
        cmd_pull_cpa()
    elif args.command == "api":
        from autoteam.api import start_server

        start_server(host=args.host, port=args.port)


if __name__ == "__main__":
    main()
