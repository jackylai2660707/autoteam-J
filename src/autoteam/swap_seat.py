"""CPA 驱动的 Team seat 调度。

目标：
- 不 kick / 不移除任何 Team member；
- 只从 CPA 读取 OAuth 与 quota；
- 可配置保留 1~5 个 ChatGPT seat + CPA OAuth active；
- 其余 Team member 切到 Codex seat，CPA OAuth disabled 作为 standby；
- 白名单账号完全跳过 quota 检查、seat 切换和 CPA OAuth 启停。
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from collections import defaultdict
from pathlib import Path
from typing import Any

from autoteam.account_ops import fetch_team_members
from autoteam.admin_state import get_admin_email, get_chatgpt_account_id
from autoteam.chatgpt_api import ChatGPTTeamAPI
from autoteam.cpa_sync import (
    check_cpa_codex_quota,
    cpa_auth_is_active,
    cpa_auth_is_disabled,
    get_managed_cpa_auth_names,
    is_cpa_codex_oauth,
    is_managed_cpa_auth,
    list_cpa_files,
    set_cpa_auth_disabled,
)
from autoteam.textio import read_text, write_text

logger = logging.getLogger(__name__)

CHATGPT_SEAT = "default"
CODEX_SEAT = "usage_based"
PROJECT_ROOT = Path(__file__).parent.parent.parent
SWAP_COOLDOWN_FILE = PROJECT_ROOT / "swap_seat_cooldown.json"
SWAP_QUOTA_STATE_FILE = PROJECT_ROOT / "swap_seat_quota_state.json"
SWAP_COOLDOWN_MIN_INTERVAL_SECONDS = 2 * 60 * 60
SWAP_COOLDOWN_DAILY_LIMIT = 3
SWAP_ACTIVE_MIN = 1
SWAP_ACTIVE_MAX = 5
_SWAP_COOLDOWN_LOCK = threading.RLock()
_SWAP_QUOTA_STATE_LOCK = threading.RLock()
_QUOTA_WINDOWS = (
    ("primary", "primary_pct", "primary_resets_at"),
    ("weekly", "weekly_pct", "weekly_resets_at"),
    ("monthly", "monthly_pct", "monthly_resets_at"),
)
_QUOTA_WINDOW_NAMES = tuple(window for window, _pct_key, _reset_key in _QUOTA_WINDOWS)


def _fetch_team_members_for_account(chatgpt_api, account_id: str | None = None):
    """兼容旧测试 monkeypatch；真实实现支持显式 account_id。"""
    if account_id:
        try:
            return fetch_team_members(chatgpt_api, account_id=account_id)
        except TypeError:
            return fetch_team_members(chatgpt_api)
    return fetch_team_members(chatgpt_api)


def _quota_check_min_interval_seconds() -> int:
    try:
        return max(60, int(os.environ.get("SWAP_QUOTA_CHECK_MIN_INTERVAL_SECONDS", "900") or "900"))
    except Exception:
        return 900


SWAP_QUOTA_CHECK_MIN_INTERVAL_SECONDS = _quota_check_min_interval_seconds()


class SwapSeatCooldownError(RuntimeError):
    """swap_seat 冷却限制命中。"""

    def __init__(self, message: str, *, retry_after: int | None = None, status: dict | None = None):
        super().__init__(message)
        self.retry_after = retry_after
        self.status = status or {}


def _current_day_key(now: float | None = None) -> str:
    return time.strftime("%Y-%m-%d", time.localtime(time.time() if now is None else now))


def _load_swap_cooldown_state() -> dict:
    try:
        raw = read_text(SWAP_COOLDOWN_FILE).strip()
    except FileNotFoundError:
        return {}
    except Exception:
        logger.warning("[swap_seat] 冷却状态读取失败，将按空状态处理", exc_info=True)
        return {}
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except Exception:
        logger.warning("[swap_seat] 冷却状态 JSON 无效，将按空状态处理")
        return {}
    return data if isinstance(data, dict) else {}


def _save_swap_cooldown_state(state: dict) -> None:
    SWAP_COOLDOWN_FILE.parent.mkdir(parents=True, exist_ok=True)
    write_text(SWAP_COOLDOWN_FILE, json.dumps(state, ensure_ascii=False, indent=2))


def _load_swap_quota_state() -> dict:
    try:
        raw = read_text(SWAP_QUOTA_STATE_FILE).strip()
    except FileNotFoundError:
        return {"auths": {}}
    except Exception:
        logger.warning("[swap_seat] quota 状态读取失败，将按空状态处理", exc_info=True)
        return {"auths": {}}
    if not raw:
        return {"auths": {}}
    try:
        data = json.loads(raw)
    except Exception:
        logger.warning("[swap_seat] quota 状态 JSON 无效，将按空状态处理")
        return {"auths": {}}
    if not isinstance(data, dict):
        return {"auths": {}}
    if not isinstance(data.get("auths"), dict):
        data["auths"] = {}
    return data


def _save_swap_quota_state(state: dict) -> None:
    SWAP_QUOTA_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    write_text(SWAP_QUOTA_STATE_FILE, json.dumps(state, ensure_ascii=False, indent=2))


def _normalize_swap_cooldown_state(state: dict, now: float | None = None) -> dict:
    now = time.time() if now is None else float(now)
    today = _current_day_key(now)
    raw_events = state.get("events")
    if not isinstance(raw_events, list):
        raw_events = state.get("started_at") if isinstance(state.get("started_at"), list) else []

    all_events = []
    events = []
    for value in raw_events:
        try:
            ts = float(value)
        except Exception:
            continue
        all_events.append(ts)
        # 自然日限制：只保留本机当前日期内的 swap 记录。
        if _current_day_key(ts) == today:
            events.append(ts)

    try:
        last_started_at = float(state.get("last_started_at"))
        all_events.append(last_started_at)
    except Exception:
        pass

    events = sorted(events)
    all_events = sorted(ts for ts in all_events if ts <= now + 5)
    return {
        "date": today,
        "events": events,
        # 每日次数按自然日重置，但“每次至少间隔 2 小时”必须跨天继续生效。
        "last_started_at": all_events[-1] if all_events else None,
        "daily_limit": SWAP_COOLDOWN_DAILY_LIMIT,
        "min_interval_seconds": SWAP_COOLDOWN_MIN_INTERVAL_SECONDS,
    }


def _cooldown_scope_key(scope: str | None = None) -> str:
    return str(scope or "default").strip() or "default"


def _load_swap_cooldown_scope_state(scope: str | None = None) -> dict:
    scope_key = _cooldown_scope_key(scope)
    state = _load_swap_cooldown_state()
    if scope_key == "default" and "scopes" not in state:
        return state
    scopes = state.get("scopes") if isinstance(state.get("scopes"), dict) else {}
    return scopes.get(scope_key, {})


def _save_swap_cooldown_scope_state(scope: str | None, scope_state: dict) -> None:
    scope_key = _cooldown_scope_key(scope)
    state = _load_swap_cooldown_state()
    if scope_key == "default" and "scopes" not in state:
        _save_swap_cooldown_state(scope_state)
        return
    scopes = state.get("scopes") if isinstance(state.get("scopes"), dict) else {}
    scopes[scope_key] = scope_state
    state["scopes"] = scopes
    _save_swap_cooldown_state(state)


def get_swap_cooldown_status(now: float | None = None, *, scope: str | None = None) -> dict:
    """返回当前 swap_seat 冷却状态；只读，不占用次数。"""
    now = time.time() if now is None else float(now)
    with _SWAP_COOLDOWN_LOCK:
        state = _normalize_swap_cooldown_state(_load_swap_cooldown_scope_state(scope), now)

    events = state["events"]
    last_started_at = state.get("last_started_at")
    next_by_interval = 0
    if last_started_at:
        next_by_interval = int(last_started_at + SWAP_COOLDOWN_MIN_INTERVAL_SECONDS)

    blocked_reasons = []
    retry_after = 0
    if last_started_at and now < next_by_interval:
        blocked_reasons.append("min_interval")
        retry_after = max(retry_after, next_by_interval - now)
    if len(events) >= SWAP_COOLDOWN_DAILY_LIMIT:
        tomorrow = time.mktime(time.strptime(state["date"], "%Y-%m-%d")) + 24 * 60 * 60
        blocked_reasons.append("daily_limit")
        retry_after = max(retry_after, tomorrow - now)

    return {
        "allowed": not blocked_reasons,
        "blocked_reasons": blocked_reasons,
        "date": state["date"],
        "used_today": len(events),
        "daily_limit": SWAP_COOLDOWN_DAILY_LIMIT,
        "remaining_today": max(0, SWAP_COOLDOWN_DAILY_LIMIT - len(events)),
        "last_started_at": last_started_at,
        "next_allowed_at": int(now + retry_after) if retry_after > 0 else int(now),
        "retry_after_seconds": int(max(0, retry_after)),
        "min_interval_seconds": SWAP_COOLDOWN_MIN_INTERVAL_SECONDS,
        "scope": _cooldown_scope_key(scope),
    }


def reserve_swap_cooldown_slot(now: float | None = None, *, scope: str | None = None) -> dict:
    """在执行任何 seat/OAuth 副作用前占用一次 swap 额度。"""
    now = time.time() if now is None else float(now)
    with _SWAP_COOLDOWN_LOCK:
        state = _normalize_swap_cooldown_state(_load_swap_cooldown_scope_state(scope), now)
        status = get_swap_cooldown_status(now, scope=scope)
        if not status["allowed"]:
            retry_minutes = max(1, int((status["retry_after_seconds"] + 59) // 60))
            reasons = "、".join(status["blocked_reasons"])
            raise SwapSeatCooldownError(
                (
                    "swap_seat 冷却中：一天最多 3 次且每次至少间隔 2 小时；"
                    f"当前原因={reasons}，约 {retry_minutes} 分钟后可再次执行"
                ),
                retry_after=status["retry_after_seconds"],
                status=status,
            )

        state["events"].append(now)
        state["last_started_at"] = now
        _save_swap_cooldown_scope_state(scope, state)
        return get_swap_cooldown_status(now, scope=scope)


def normalized_email(value: str | None) -> str:
    return (value or "").strip().lower()


def normalize_active_limit(value: Any = 2, *, default: int = 2) -> int:
    """ChatGPT seat / CPA OAuth active 保留数量，允许 1~5。"""
    try:
        count = int(value)
    except Exception:
        count = int(default)
    return max(SWAP_ACTIVE_MIN, min(SWAP_ACTIVE_MAX, count))


def parse_email_set(value: Any) -> set[str]:
    """解析逗号/分号/换行分隔的邮箱列表。"""
    if value is None:
        return set()
    if isinstance(value, (set, list, tuple)):
        parts = [str(item or "") for item in value]
    else:
        parts = re.split(r"[,;\s]+", str(value or ""))
    return {normalized_email(part) for part in parts if normalized_email(part)}


def get_swap_seat_whitelist_emails() -> set[str]:
    """额外白名单：不检查 quota、不切 seat、不启停 CPA OAuth。"""
    return parse_email_set(os.environ.get("SWAP_SEAT_WHITELIST_EMAILS", ""))


def _truthy_flag(value) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on", "enabled"}


def _explicit_managed_flag(acc: dict | None) -> bool | None:
    acc = acc or {}
    if "managed_by_autoteam" not in acc:
        return None
    raw = acc.get("managed_by_autoteam")
    if raw is None or str(raw).strip() == "":
        return None
    return _truthy_flag(raw)


def _account_looks_autoteam_managed(acc: dict | None) -> bool:
    """保守识别 AutoTeam 创建的 member，避免误操作外部/主号成员。"""
    acc = acc or {}
    explicit_managed = _explicit_managed_flag(acc)
    if explicit_managed is not None:
        return explicit_managed
    if str(acc.get("created_by") or "").lower() == "autoteam":
        return True
    if acc.get("mail_account_id") is not None or acc.get("cloudmail_account_id") is not None:
        return True
    return False


def get_autoteam_managed_member_emails(accounts: list[dict] | None = None) -> set[str]:
    """返回允许 AutoTeam 修改 seat 的 member 邮箱白名单。"""
    if accounts is None:
        try:
            from autoteam.accounts import load_accounts

            accounts = load_accounts()
        except Exception:
            logger.warning("[swap_seat] 读取本地账号失败，本轮将不主动 seat-swap 任何非显式受管成员", exc_info=True)
            return set()
    return {
        normalized_email(acc.get("email"))
        for acc in accounts or []
        if isinstance(acc, dict) and _account_looks_autoteam_managed(acc) and normalized_email(acc.get("email"))
    }


def normalize_team_seat_type(value: str | None) -> str:
    raw = str(value or "").strip().lower()
    if raw in {"chatgpt", "default", "standard", "standard-user", "chat"}:
        return "chatgpt"
    if raw in {"codex", "usage_based", "usage-based", "usagebased", "usage"}:
        return "codex"
    return raw or "unknown"


def team_seat_backend_value(value: str | None) -> str:
    normalized = normalize_team_seat_type(value)
    if normalized == "chatgpt":
        return CHATGPT_SEAT
    if normalized == "codex":
        return CODEX_SEAT
    raise ValueError(f"未知 seat 类型: {value}")


def cpa_auth_identifier(auth: dict | None) -> str:
    auth = auth or {}
    for key in ("name", "id"):
        value = str(auth.get(key) or "").strip()
        if value:
            return value
    return ""


def cpa_auth_email(auth: dict | None) -> str:
    auth = auth or {}
    return normalized_email(auth.get("email") or auth.get("account"))


def quota_state_key(auth: dict | None, *, account_id: str | None = None) -> str:
    """quota 缓存 key 必须按 Team 隔离；同一个 OAuth 在不同 workspace 的 quota 可能不同。"""
    auth_id = cpa_auth_identifier(auth)
    if not auth_id:
        return ""
    account = str(account_id or "").strip()
    return f"{account}:{auth_id}" if account else auth_id


def cpa_auth_index(auth: dict | None) -> str:
    auth = auth or {}
    for key in ("auth_index", "authIndex", "AuthIndex"):
        value = str(auth.get(key) or "").strip()
        if value:
            return value
    return ""


def quota_info_from_result(status: str, info: dict | None) -> dict:
    if status == "ok" and isinstance(info, dict):
        return info
    if isinstance(info, dict) and isinstance(info.get("quota_info"), dict):
        return info["quota_info"]
    return {}


def quota_applicable_windows(status: str, info: dict | None) -> set[str]:
    """返回该账号真实适用的 quota 窗口；monthly-only 不应被 5h/weekly 限制影响。"""
    quota = quota_info_from_result(status, info)
    raw_windows = quota.get("quota_windows")
    if isinstance(raw_windows, (list, tuple, set)):
        windows = {str(window or "").strip().lower() for window in raw_windows}
        return {window for window in windows if window in _QUOTA_WINDOW_NAMES}

    applicable = set()
    explicit_flags = False
    for window, _pct_key, _reset_key in _QUOTA_WINDOWS:
        flag_key = f"{window}_applicable"
        if flag_key not in quota:
            continue
        explicit_flags = True
        if bool(quota.get(flag_key)):
            applicable.add(window)
    if explicit_flags:
        return applicable

    # Backward compatibility：旧 cache / 旧测试没有 applicability 字段，按旧逻辑三个窗口都适用。
    return set(_QUOTA_WINDOW_NAMES)


def quota_remaining_by_window(status: str, info: dict | None) -> dict[str, int]:
    quota = quota_info_from_result(status, info)
    remaining = {}
    for window, pct_key, _reset_key in _QUOTA_WINDOWS:
        try:
            remaining[window] = max(0, 100 - int(float(quota.get(pct_key, 0) or 0)))
        except Exception:
            remaining[window] = 0
    return remaining


def quota_remaining_pair(status: str, info: dict | None) -> tuple[int, int]:
    remaining = quota_remaining_by_window(status, info)
    return remaining["primary"], remaining["weekly"]


def quota_remaining_log_text(status: str, info: dict | None) -> str:
    """Human-readable quota windows for logs, using N/A for windows that do not apply."""
    remaining = quota_remaining_by_window(status, info)
    applicable = quota_applicable_windows(status, info)
    labels = []
    for window, label in (("primary", "5h"), ("weekly", "weekly"), ("monthly", "monthly")):
        value = f"{remaining[window]}%" if window in applicable else "N/A"
        labels.append(f"{label}={value}")
    return " ".join(labels)


def quota_available(status: str, info: dict | None) -> bool:
    """任一适用窗口耗尽都不可用；不适用窗口（如 monthly-only 的 5h）不参与判断。"""
    if status != "ok":
        return False
    applicable = quota_applicable_windows(status, info)
    if not applicable:
        return False
    remaining = quota_remaining_by_window(status, info)
    return all(remaining[window] > 0 for window in applicable)


def _int_ts(value: Any, default: int = 0) -> int:
    try:
        return int(float(value or 0))
    except Exception:
        return default


def _quota_resets(status: str, info: dict | None) -> dict:
    quota = quota_info_from_result(status, info)
    primary_reset = _int_ts(quota.get("primary_resets_at"))
    weekly_reset = _int_ts(quota.get("weekly_resets_at"))
    monthly_reset = _int_ts(quota.get("monthly_resets_at"))
    applicable = quota_applicable_windows(status, info)
    exhausted_until = 0
    window = ""
    if isinstance(info, dict):
        exhausted_until = _int_ts(info.get("resets_at"))
        window = str(info.get("window") or "")
    if status == "exhausted":
        if not exhausted_until:
            candidates = []
            if "primary" in applicable and _int_ts(quota.get("primary_pct")) >= 100 and primary_reset:
                candidates.append(primary_reset)
            if "weekly" in applicable and _int_ts(quota.get("weekly_pct")) >= 100 and weekly_reset:
                candidates.append(weekly_reset)
            if "monthly" in applicable and _int_ts(quota.get("monthly_pct")) >= 100 and monthly_reset:
                candidates.append(monthly_reset)
            exhausted_until = max(candidates) if candidates else int(time.time() + 5 * 60 * 60)
        if not window:
            primary_exhausted = "primary" in applicable and _int_ts(quota.get("primary_pct")) >= 100
            weekly_exhausted = "weekly" in applicable and _int_ts(quota.get("weekly_pct")) >= 100
            monthly_exhausted = "monthly" in applicable and _int_ts(quota.get("monthly_pct")) >= 100
            exhausted_count = sum(1 for flag in (primary_exhausted, weekly_exhausted, monthly_exhausted) if flag)
            if exhausted_count > 1:
                window = "combined"
            elif monthly_exhausted:
                window = "monthly"
            elif weekly_exhausted:
                window = "weekly"
            elif primary_exhausted:
                window = "primary"
            else:
                window = "limit"
    return {
        "quota_info": quota,
        "primary_resets_at": primary_reset,
        "weekly_resets_at": weekly_reset,
        "monthly_resets_at": monthly_reset,
        "exhausted_until": exhausted_until,
        "window": window,
    }


def record_quota_result(auth: dict, status: str, info: dict | None, *, now: float | None = None, account_id: str | None = None) -> dict:
    """记录每个账号/OAuth 的 quota 快照和 5h/weekly/monthly reset 时间。"""
    now = time.time() if now is None else float(now)
    auth_id = cpa_auth_identifier(auth)
    state_key = quota_state_key(auth, account_id=account_id)
    if not auth_id or not state_key:
        return {}
    email = normalized_email(auth.get("email") or auth.get("account"))
    resets = _quota_resets(status, info)
    remaining = quota_remaining_by_window(status, info)
    applicable = quota_applicable_windows(status, info)
    entry = {
        "auth_id": auth_id,
        "account_id": str(account_id or "").strip(),
        "email": email,
        "status": status,
        "quota_available": quota_available(status, info),
        "primary_remaining": remaining["primary"],
        "weekly_remaining": remaining["weekly"],
        "monthly_remaining": remaining["monthly"],
        "primary_applicable": "primary" in applicable,
        "weekly_applicable": "weekly" in applicable,
        "monthly_applicable": "monthly" in applicable,
        "quota_windows": sorted(applicable),
        "primary_resets_at": resets["primary_resets_at"],
        "weekly_resets_at": resets["weekly_resets_at"],
        "monthly_resets_at": resets["monthly_resets_at"],
        "exhausted_until": resets["exhausted_until"] if status == "exhausted" else 0,
        "window": resets["window"],
        "quota_info": resets["quota_info"],
        "updated_at": int(now),
    }
    with _SWAP_QUOTA_STATE_LOCK:
        state = _load_swap_quota_state()
        state.setdefault("auths", {})[state_key] = entry
        state["updated_at"] = int(now)
        _save_swap_quota_state(state)
    return entry


def forget_quota_cache_entries(*, auth_id: str | None = None, email: str | None = None) -> dict:
    """删除指定受管 auth/email 的本地 quota 历史记录。"""
    auth_id = str(auth_id or "").strip()
    email = normalized_email(email)
    removed = []
    with _SWAP_QUOTA_STATE_LOCK:
        state = _load_swap_quota_state()
        auths = state.get("auths") if isinstance(state.get("auths"), dict) else {}
        for key, entry in list(auths.items()):
            if not isinstance(entry, dict):
                continue
            entry_auth_id = str(entry.get("auth_id") or "").strip()
            entry_email = normalized_email(entry.get("email"))
            if (auth_id and entry_auth_id == auth_id) or (email and entry_email == email):
                removed.append(key)
                auths.pop(key, None)
        if removed:
            state["updated_at"] = int(time.time())
            _save_swap_quota_state(state)
    return {"removed": removed, "count": len(removed)}


def cached_exhausted_quota_result(auth: dict, *, now: float | None = None, account_id: str | None = None) -> tuple[str, dict] | None:
    """未到已记录 reset 时间前，直接视为 exhausted，避免无意义检查/切换。"""
    now = time.time() if now is None else float(now)
    state_key = quota_state_key(auth, account_id=account_id)
    if not state_key:
        return None
    with _SWAP_QUOTA_STATE_LOCK:
        entry = _load_swap_quota_state().get("auths", {}).get(state_key)
    if not isinstance(entry, dict) or entry.get("status") != "exhausted":
        return None
    exhausted_until = _int_ts(entry.get("exhausted_until"))
    if not exhausted_until or exhausted_until <= now:
        return None
    quota_info = entry.get("quota_info") if isinstance(entry.get("quota_info"), dict) else {}
    return "exhausted", {
        "window": entry.get("window") or "cached",
        "resets_at": exhausted_until,
        "quota_info": quota_info,
        "source": "swap_seat_quota_state",
    }


def cached_recent_quota_result(auth: dict, *, now: float | None = None, account_id: str | None = None) -> tuple[str, dict] | None:
    """近期已查过且未耗尽的账号直接复用快照，避免频繁打 CPA quota。

    没有历史记录的新账号会返回 None，从而强制实时检查一次 quota。
    耗尽账号不走这里，必须由 cached_exhausted_quota_result 按 reset 时间判断。
    """
    now = time.time() if now is None else float(now)
    state_key = quota_state_key(auth, account_id=account_id)
    if not state_key:
        return None
    with _SWAP_QUOTA_STATE_LOCK:
        entry = _load_swap_quota_state().get("auths", {}).get(state_key)
    if not isinstance(entry, dict) or entry.get("status") != "ok":
        return None
    updated_at = _int_ts(entry.get("updated_at"))
    if not updated_at or updated_at > now or now - updated_at >= SWAP_QUOTA_CHECK_MIN_INTERVAL_SECONDS:
        return None
    quota_info = entry.get("quota_info") if isinstance(entry.get("quota_info"), dict) else {}
    if not quota_info:
        return None
    return "ok", {
        **quota_info,
        "source": "swap_seat_quota_state",
        "cached_at": updated_at,
        "cache_ttl_seconds": SWAP_QUOTA_CHECK_MIN_INTERVAL_SECONDS,
    }


def mark_auth_error_for_pat_repair(auth: dict, info: dict | None = None) -> None:
    """CPA quota 鉴权失败时，把本地账号标成 PAT 待修复。"""
    email = normalized_email((auth or {}).get("email") or (auth or {}).get("account"))
    if not email:
        return
    try:
        from autoteam.accounts import STATUS_AUTH_PENDING, update_account

        detail = ""
        if isinstance(info, dict):
            detail = str(info.get("error") or info.get("body") or info.get("status_code") or "")
        update_account(
            email,
            status=STATUS_AUTH_PENDING,
            auth_last_error="cpa_auth_error",
            auth_last_error_detail=detail[:300] if detail else "CPA quota 检查鉴权失败",
            auth_last_failed_at=time.time(),
            managed_by_autoteam=True,
        )
    except Exception:
        logger.debug("[swap_seat] 标记 PAT 待修复失败: %s", email, exc_info=True)


def quota_cache_runtime_status(*, now: float | None = None) -> dict:
    """只读返回 swap_seat quota 缓存状态，供 WebUI 展示。

    不触发 CPA / Team API；仅读取本地 `swap_seat_quota_state.json`。
    """
    now = time.time() if now is None else float(now)
    with _SWAP_QUOTA_STATE_LOCK:
        state = _load_swap_quota_state()
    raw_auths = state.get("auths") if isinstance(state.get("auths"), dict) else {}
    entries = []
    for key, entry in raw_auths.items():
        if not isinstance(entry, dict):
            continue
        status = str(entry.get("status") or "")
        applicable = {
            window
            for window in _QUOTA_WINDOW_NAMES
            if bool(entry.get(f"{window}_applicable", True))
        }
        updated_at = _int_ts(entry.get("updated_at"))
        exhausted_until = _int_ts(entry.get("exhausted_until"))
        next_check_at = 0
        cache_state = "unknown"
        if status == "exhausted" and exhausted_until > now:
            cache_state = "blocked_until_reset"
            next_check_at = exhausted_until
        elif status == "ok" and updated_at and updated_at + SWAP_QUOTA_CHECK_MIN_INTERVAL_SECONDS > now:
            cache_state = "recent_ok"
            next_check_at = updated_at + SWAP_QUOTA_CHECK_MIN_INTERVAL_SECONDS
        elif updated_at:
            cache_state = "stale"
        entries.append(
            {
                "key": key,
                "auth_id": entry.get("auth_id") or "",
                "account_id": entry.get("account_id") or "",
                "email": entry.get("email") or "",
                "status": status,
                "quota_available": bool(entry.get("quota_available")),
                "primary_remaining": _int_ts(entry.get("primary_remaining")),
                "weekly_remaining": _int_ts(entry.get("weekly_remaining")),
                "monthly_remaining": _int_ts(entry.get("monthly_remaining")),
                "primary_applicable": "primary" in applicable,
                "weekly_applicable": "weekly" in applicable,
                "monthly_applicable": "monthly" in applicable,
                "quota_windows": sorted(applicable),
                "primary_resets_at": _int_ts(entry.get("primary_resets_at")),
                "weekly_resets_at": _int_ts(entry.get("weekly_resets_at")),
                "monthly_resets_at": _int_ts(entry.get("monthly_resets_at")),
                "exhausted_until": exhausted_until,
                "window": entry.get("window") or "",
                "updated_at": updated_at,
                "next_check_at": int(next_check_at),
                "cache_state": cache_state,
            }
        )
    entries.sort(key=lambda item: item.get("updated_at") or 0, reverse=True)
    return {
        "updated_at": _int_ts(state.get("updated_at")),
        "check_min_interval_seconds": SWAP_QUOTA_CHECK_MIN_INTERVAL_SECONDS,
        "entries": entries,
        "summary": {
            "total": len(entries),
            "available": sum(1 for item in entries if item.get("quota_available")),
            "exhausted_cached": sum(1 for item in entries if item.get("cache_state") == "blocked_until_reset"),
            "recent_ok": sum(1 for item in entries if item.get("cache_state") == "recent_ok"),
            "stale": sum(1 for item in entries if item.get("cache_state") == "stale"),
        },
    }


def _parse_time_score(value: Any) -> float:
    if isinstance(value, (int, float)):
        return float(value)
    if not value:
        return 0.0
    text = str(value).strip()
    if not text:
        return 0.0
    try:
        # Python 3.11 支持 RFC3339 的 +00:00；兼容 Z。
        from datetime import datetime

        return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except Exception:
        return 0.0


def _auth_sort_key(auth: dict) -> tuple:
    """同一邮箱多个 CPA OAuth 时，优先选择当前可用且较新的。"""
    return (
        1 if cpa_auth_is_active(auth) else 0,
        0 if cpa_auth_is_disabled(auth) else 1,
        0 if str(auth.get("status") or "").lower() in {"error", "unknown"} else 1,
        _parse_time_score(auth.get("last_refresh") or auth.get("updated_at") or auth.get("modtime")),
        1 if cpa_auth_index(auth) else 0,
    )


def _best_auth(auths: list[dict]) -> dict | None:
    if not auths:
        return None
    return max(auths, key=_auth_sort_key)


def _best_auth_for_quota(auths: list[dict], quota_results: dict[str, tuple[str, dict | None]]) -> dict | None:
    """同一邮箱多个 OAuth 时，优先选 quota 可用且剩余更多的那个。"""
    if not auths:
        return None

    def _key(auth: dict) -> tuple:
        auth_id = cpa_auth_identifier(auth)
        status, info = quota_results.get(auth_id, ("auth_error", None))
        remaining = quota_remaining_by_window(status, info)
        applicable = quota_applicable_windows(status, info)
        applicable_remaining = [remaining[window] for window in applicable] or [0]
        return (
            1 if quota_available(status, info) else 0,
            min(applicable_remaining),
            sum(applicable_remaining),
            *_auth_sort_key(auth),
        )

    return max(auths, key=_key)


def build_swap_plan(
    team_members: list[dict],
    cpa_auths: list[dict],
    quota_results: dict[str, tuple[str, dict | None]],
    *,
    max_chatgpt_active: int = 2,
    forced_codex_emails: set[str] | None = None,
    whitelist_emails: set[str] | None = None,
    managed_auth_names: set[str] | None = None,
    managed_emails: set[str] | None = None,
) -> dict:
    """根据 Team member + CPA quota 生成无副作用切换计划。"""
    # 硬上限：无论 API/CLI/前端传什么，ChatGPT seat / CPA OAuth active 只能保留 1~5 个。
    max_chatgpt_active = normalize_active_limit(max_chatgpt_active)
    forced_codex_emails = {normalized_email(email) for email in (forced_codex_emails or set()) if normalized_email(email)}
    whitelist_emails = {normalized_email(email) for email in (whitelist_emails or set()) if normalized_email(email)}
    managed_email_scope = None
    if managed_emails is not None:
        managed_email_scope = {normalized_email(email) for email in managed_emails if normalized_email(email)}

    auths_by_email: dict[str, list[dict]] = defaultdict(list)
    all_codex_auths = [auth for auth in cpa_auths if is_cpa_codex_oauth(auth)]
    if managed_auth_names is None:
        name_managed_codex_auths = all_codex_auths
    else:
        name_managed_codex_auths = [auth for auth in all_codex_auths if is_managed_cpa_auth(auth, managed_auth_names)]
    if managed_email_scope is None:
        codex_auths = name_managed_codex_auths
    else:
        codex_auths = [auth for auth in name_managed_codex_auths if cpa_auth_email(auth) in managed_email_scope]
    eligible_auth_object_ids = {id(auth) for auth in codex_auths}
    unmanaged_codex_auths = [auth for auth in all_codex_auths if id(auth) not in eligible_auth_object_ids]
    for auth in codex_auths:
        email = cpa_auth_email(auth)
        if email:
            auths_by_email[email].append(auth)

    member_states = []
    seen_team_emails = set()
    whitelisted_chatgpt_seats = 0
    protected_team_emails = set()
    protected_chatgpt_seats = 0
    current_team_chatgpt_seats = 0
    for member in team_members:
        email = normalized_email(member.get("email"))
        if not email:
            continue
        seen_team_emails.add(email)
        current_seat = normalize_team_seat_type(member.get("seat_type"))
        if current_seat == "chatgpt":
            current_team_chatgpt_seats += 1
        if email in whitelist_emails:
            if current_seat == "chatgpt":
                whitelisted_chatgpt_seats += 1
            continue
        email_auths = auths_by_email.get(email, [])
        if managed_email_scope is not None:
            managed_member = email in managed_email_scope or bool(email_auths)
        elif managed_auth_names is not None:
            managed_member = bool(email_auths)
        else:
            managed_member = True
        if not managed_member:
            protected_team_emails.add(email)
            if current_seat == "chatgpt":
                protected_chatgpt_seats += 1
            continue
        auth = _best_auth_for_quota(email_auths, quota_results)
        auth_id = cpa_auth_identifier(auth)
        quota_status, quota_info = quota_results.get(auth_id, ("auth_error", {"error": "missing_quota"}))
        remaining = quota_remaining_by_window(quota_status, quota_info)
        applicable = quota_applicable_windows(quota_status, quota_info)
        applicable_remaining = [remaining[window] for window in applicable] or [0]
        available = bool(auth) and quota_available(quota_status, quota_info)
        force_codex = email in forced_codex_emails
        if force_codex:
            available = False
        current_oauth_active = cpa_auth_is_active(auth) if auth else False
        member_states.append(
            {
                "email": email,
                "member": member,
                "auth": auth,
                "auth_id": auth_id,
                "quota_status": quota_status,
                "quota_info": quota_info,
                "quota_available": available,
                "force_codex": force_codex,
                "primary_remaining": remaining["primary"],
                "weekly_remaining": remaining["weekly"],
                "monthly_remaining": remaining["monthly"],
                "primary_applicable": "primary" in applicable,
                "weekly_applicable": "weekly" in applicable,
                "monthly_applicable": "monthly" in applicable,
                "quota_windows": sorted(applicable),
                "score": min(applicable_remaining),
                "current_seat": current_seat,
                "current_oauth_active": current_oauth_active,
            }
        )

    whitelisted_active_oauths = sum(
        1
        for auth in codex_auths
        if cpa_auth_email(auth) in whitelist_emails and cpa_auth_is_active(auth)
    )
    unmanaged_active_oauths = sum(1 for auth in unmanaged_codex_auths if cpa_auth_is_active(auth))
    protected_active_oauths = sum(
        1
        for auth in unmanaged_codex_auths
        if cpa_auth_is_active(auth) and cpa_auth_email(auth) in seen_team_emails
    )

    # 选择最多 max_chatgpt_active 个 quota 可用成员。优先保证各 quota 窗口的最小剩余量最高，平分时减少 seat/OAuth 抖动。
    eligible = [state for state in member_states if state["quota_available"]]
    eligible.sort(
        key=lambda state: (
            state["score"],
            state["primary_remaining"] + state["weekly_remaining"] + state["monthly_remaining"],
            1 if state["current_seat"] == "chatgpt" else 0,
            1 if state["current_oauth_active"] else 0,
            state["email"],
        ),
        reverse=True,
    )
    managed_active_limit = max(
        0,
        min(
            max_chatgpt_active - whitelisted_chatgpt_seats - protected_chatgpt_seats,
            max_chatgpt_active - whitelisted_active_oauths - protected_active_oauths,
        ),
    )
    selected_emails = {state["email"] for state in eligible[:managed_active_limit]}
    selected_auth_ids = {state["auth_id"] for state in eligible[:managed_active_limit] if state.get("auth_id")}

    seat_actions = []
    desired_members = []
    for state in member_states:
        desired_seat = "chatgpt" if state["email"] in selected_emails else "codex"
        desired_backend = team_seat_backend_value(desired_seat)
        user_id = str(state["member"].get("user_id") or state["member"].get("id") or "").strip()
        needs_update = state["current_seat"] != desired_seat
        action = {
            "email": state["email"],
            "user_id": user_id,
            "current_seat": state["current_seat"],
            "desired_seat": desired_seat,
            "desired_seat_raw": desired_backend,
            "needs_update": needs_update,
            "quota_status": state["quota_status"],
            "quota_available": state["quota_available"],
            "force_codex": state.get("force_codex", False),
            "primary_remaining": state["primary_remaining"],
            "weekly_remaining": state["weekly_remaining"],
            "monthly_remaining": state["monthly_remaining"],
            "primary_applicable": state["primary_applicable"],
            "weekly_applicable": state["weekly_applicable"],
            "monthly_applicable": state["monthly_applicable"],
            "quota_windows": state["quota_windows"],
            "auth_id": state["auth_id"],
        }
        seat_actions.append(action)
        desired_members.append(action.copy())

    oauth_actions = []
    for auth in codex_auths:
        auth_id = cpa_auth_identifier(auth)
        email = normalized_email(auth.get("email") or auth.get("account"))
        if email in whitelist_emails:
            continue
        desired_disabled = auth_id not in selected_auth_ids
        current_disabled = cpa_auth_is_disabled(auth)
        status = str(auth.get("status") or "").strip().lower()
        # 有些 CPA 列表不返回 status；只在 status 明确存在且不是 active/enabled 时才强制刷新，
        # 避免 disabled=false 但 status 为空的条目被每轮误判为需要 enable。
        needs_update = current_disabled != desired_disabled or (
            not desired_disabled and bool(status) and status not in {"active", "enabled", "ok"}
        )
        oauth_actions.append(
            {
                "email": email,
                "auth_id": auth_id,
                "name": auth.get("name") or auth.get("id") or auth_id,
                "status": status,
                "current_disabled": current_disabled,
                "desired_disabled": desired_disabled,
                "desired_status": "disabled" if desired_disabled else "active",
                "needs_update": needs_update,
                "in_team": email in seen_team_emails,
            }
        )

    return {
        "max_chatgpt_active": max_chatgpt_active,
        "selected_emails": sorted(selected_emails),
        "selected_auth_ids": sorted(selected_auth_ids),
        "member_states": member_states,
        "seat_actions": seat_actions,
        "oauth_actions": oauth_actions,
        "summary": {
            "team_members": len(seen_team_emails),
            "managed_team_members": len(member_states),
            "whitelisted_team_members": len(seen_team_emails & whitelist_emails),
            "whitelisted_chatgpt_seats": whitelisted_chatgpt_seats,
            "protected_team_members": len(protected_team_emails),
            "protected_chatgpt_seats": protected_chatgpt_seats,
            "current_chatgpt_seats": current_team_chatgpt_seats,
            "managed_current_chatgpt_seats": sum(1 for state in member_states if state["current_seat"] == "chatgpt"),
            "cpa_codex_oauth": len(codex_auths),
            "managed_cpa_codex_oauth": len(codex_auths),
            "unmanaged_cpa_codex_oauth": len(unmanaged_codex_auths),
            "unmanaged_active_oauth": unmanaged_active_oauths,
            "protected_active_oauth": protected_active_oauths,
            "whitelisted_cpa_oauth": sum(1 for auth in codex_auths if cpa_auth_email(auth) in whitelist_emails),
            "whitelisted_active_oauth": whitelisted_active_oauths,
            "current_active_oauth": sum(1 for state in member_states if state["current_oauth_active"]),
            "managed_active_capacity": managed_active_limit,
            "quota_available": len(eligible),
            "selected_chatgpt": len(selected_emails),
            "to_chatgpt": sum(1 for a in seat_actions if a["desired_seat"] == "chatgpt" and a["needs_update"]),
            "to_codex": sum(1 for a in seat_actions if a["desired_seat"] == "codex" and a["needs_update"]),
            "oauth_enable": sum(1 for a in oauth_actions if not a["desired_disabled"] and a["needs_update"]),
            "oauth_disable": sum(1 for a in oauth_actions if a["desired_disabled"] and a["needs_update"]),
        },
    }


def _chatgpt_session_ready(chatgpt_api) -> bool:
    if not chatgpt_api:
        return False
    is_started = getattr(chatgpt_api, "is_started", None)
    if callable(is_started):
        try:
            return bool(is_started())
        except Exception:
            pass
    return bool(getattr(chatgpt_api, "browser", None) or getattr(chatgpt_api, "http_transport", None))


def _abort_if_cancel_requested():
    try:
        from autoteam.api import ensure_current_task_not_cancelled
    except ImportError:
        return
    ensure_current_task_not_cancelled()


def _team_result_context(team_context=None, account_id: str | None = None) -> dict:
    return {
        "id": str(getattr(team_context, "id", "") or account_id or "").strip(),
        "account_id": str(getattr(team_context, "account_id", "") or account_id or "").strip(),
        "workspace_name": str(getattr(team_context, "workspace_name", "") or "").strip(),
        "label": str(getattr(team_context, "label", "") or getattr(team_context, "workspace_name", "") or account_id or "").strip(),
    }


def force_existing_members_to_codex(
    chatgpt_api,
    *,
    whitelist_emails: set[str] | None = None,
    managed_emails: set[str] | None = None,
    account_id: str | None = None,
) -> dict:
    """遗留兜底：只允许把 AutoTeam 自管成员切成 Codex seat。

    只调用 PATCH /users/{id} 修改 seat_type；不读取/取消 invite，不 kick/remove。
    白名单与非自管成员完全跳过，不检查额度也不切换 seat。
    """
    whitelist_emails = {normalized_email(email) for email in (whitelist_emails or set()) if normalized_email(email)}
    if managed_emails is None:
        managed_emails = get_autoteam_managed_member_emails()
    managed_emails = {normalized_email(email) for email in (managed_emails or set()) if normalized_email(email)}
    members = _fetch_team_members_for_account(chatgpt_api, account_id)
    results = []
    for member in members:
        _abort_if_cancel_requested()
        email = normalized_email(member.get("email"))
        if not email:
            continue
        current_seat = normalize_team_seat_type(member.get("seat_type"))
        action = {
            "email": email,
            "user_id": str(member.get("user_id") or member.get("id") or "").strip(),
            "current_seat": current_seat,
            "desired_seat": "codex",
            "desired_seat_raw": CODEX_SEAT,
            "whitelisted": email in whitelist_emails,
        }
        if email in whitelist_emails:
            action["result"] = "whitelisted"
            results.append(action)
            continue
        if email not in managed_emails:
            action["result"] = "protected_unmanaged"
            results.append(action)
            continue
        if current_seat == "codex":
            action["result"] = "unchanged"
            results.append(action)
            continue
        if not action["user_id"]:
            action["result"] = "skipped"
            action["error"] = "missing user_id"
            results.append(action)
            logger.warning("[兜底邀请] 预切 Codex 跳过，缺少 user_id: %s", email)
            continue
        result = chatgpt_api.update_member_seat_type(action["user_id"], CODEX_SEAT)
        if int(result.get("status") or 0) in (200, 204):
            action["result"] = "updated"
            logger.info("[兜底邀请] 预切 Codex: %s", email)
        else:
            action["result"] = "failed"
            action["error"] = f"HTTP {result.get('status')}: {str(result.get('body') or '')[:200]}"
            logger.warning("[兜底邀请] 预切 Codex 失败 %s: %s", email, action["error"])
        results.append(action)
    return {
        "mode": "pre_invite_codex_sweep",
        "whitelist_emails": sorted(whitelist_emails),
        "managed_emails": sorted(managed_emails),
        "summary": {
            "team_members": len([item for item in results if item.get("email")]),
            "updated": sum(1 for item in results if item.get("result") == "updated"),
            "failed": sum(1 for item in results if item.get("result") == "failed"),
            "whitelisted": sum(1 for item in results if item.get("result") == "whitelisted"),
            "protected_unmanaged": sum(1 for item in results if item.get("result") == "protected_unmanaged"),
            "remaining_chatgpt": sum(
                1
                for item in results
                if item.get("current_seat") == "chatgpt"
                and (item.get("result") in {"whitelisted", "protected_unmanaged", "failed", "skipped"})
            ),
        },
        "seat_results": results,
    }


def cmd_swap_seats(max_chatgpt_active: int = 2, *, chatgpt_api=None, team_context=None) -> dict:
    """执行一次 CPA-driven swap_seat 收敛。"""
    _abort_if_cancel_requested()
    max_chatgpt_active = normalize_active_limit(max_chatgpt_active)
    account_id = str(getattr(team_context, "account_id", "") or get_chatgpt_account_id()).strip()
    cooldown_scope = str(getattr(team_context, "cooldown_scope", "") or account_id or "default")
    cooldown_status = get_swap_cooldown_status(scope=cooldown_scope)
    whitelist_emails = get_swap_seat_whitelist_emails()
    admin_email = normalized_email(getattr(team_context, "email", "") or get_admin_email())
    forced_codex_emails = {admin_email} - whitelist_emails if admin_email else set()

    managed_chatgpt = chatgpt_api
    started_here = False
    if managed_chatgpt is None:
        managed_chatgpt = ChatGPTTeamAPI()
    if not _chatgpt_session_ready(managed_chatgpt):
        session_token = str(getattr(team_context, "session_token", "") or "").strip()
        if session_token and account_id:
            managed_chatgpt.start_with_session(
                session_token,
                account_id,
                str(getattr(team_context, "workspace_name", "") or ""),
            )
        else:
            managed_chatgpt.start()
            if account_id:
                # 多 Team 共用同一个管理员 session 时，start() 可能会把客户端
                # 设回默认 workspace；这里必须显式切回当前 Team 的 account_id，
                # 确保只对目标 Team 执行 PATCH /users/{id} seat 更新。
                managed_chatgpt.account_id = account_id
            else:
                account_id = str(getattr(managed_chatgpt, "account_id", "") or "").strip()
        started_here = True
    elif account_id:
        managed_chatgpt.account_id = account_id

    try:
        logger.info("[swap_seat] 读取 Team 成员... team=%s", str(getattr(team_context, "label", "") or account_id or "default"))
        members = _fetch_team_members_for_account(managed_chatgpt, account_id)
        team_members = [m for m in members if normalized_email(m.get("email"))]
        logger.info("[swap_seat] Team 成员 %d 个（只读成员列表；不会读取/取消 invite，不会移除 member）", len(team_members))

        logger.info("[swap_seat] 从 CPA 读取 OAuth/auth-files...")
        cpa_auths = list_cpa_files()
        team_emails = {normalized_email(m.get("email")) for m in team_members if normalized_email(m.get("email"))}
        all_codex_auths = [auth for auth in cpa_auths if is_cpa_codex_oauth(auth)]
        managed_auth_names = get_managed_cpa_auth_names()
        name_managed_codex_auths = [auth for auth in all_codex_auths if is_managed_cpa_auth(auth, managed_auth_names)]
        local_managed_member_emails = get_autoteam_managed_member_emails()
        managed_auth_emails = {
            cpa_auth_email(auth)
            for auth in name_managed_codex_auths
            if cpa_auth_email(auth)
        }
        all_managed_member_emails = local_managed_member_emails | managed_auth_emails
        # 多 Team 共用同一 CPA 时，当前 Team 的 swap 不应 disable 其他 Team 的自管 OAuth。
        #
        # 正常情况下受管 member 来自 accounts.json；如果运行态账号池损坏/为空，
        # 受管 CPA auth registry 仍能作为保守兜底：只信任本系统登记过的 auth，
        # 并且只在该邮箱确实属于当前 Team 时才参与 seat/OAuth 调度。
        managed_member_emails = all_managed_member_emails & team_emails
        codex_auths = [auth for auth in name_managed_codex_auths if cpa_auth_email(auth) in managed_member_emails]
        unmanaged_codex_auths = len(all_codex_auths) - len(codex_auths)
        if unmanaged_codex_auths:
            logger.info(
                "[swap_seat] CPA Codex OAuth: self-managed=%d protected/unmanaged=%d；非自管 auth 只读忽略，不查 quota、不启停",
                len(codex_auths),
                unmanaged_codex_auths,
            )
        logger.info(
            "[swap_seat] 当前 Team 自管 member %d/%d 个；非当前 Team/非自管 member 只读保护，不做 seat/OAuth 修改",
            len(managed_member_emails),
            len(all_managed_member_emails),
        )
        quota_results: dict[str, tuple[str, dict | None]] = {}
        for auth in codex_auths:
            _abort_if_cancel_requested()
            email = normalized_email(auth.get("email") or auth.get("account"))
            auth_id = cpa_auth_identifier(auth)
            if not auth_id:
                continue
            if email in whitelist_emails:
                quota_results[auth_id] = ("skipped", {"reason": "whitelisted"})
                logger.info("[swap_seat] quota %s: whitelisted，跳过检查与 seat/OAuth 切换", email or auth_id)
                continue
            # 非 Team OAuth 不需要测 quota；最终会统一 disabled standby。
            if email not in team_emails:
                quota_results[auth_id] = ("auth_error", {"error": "not_in_team"})
                continue
            cached = cached_exhausted_quota_result(auth, account_id=account_id)
            if cached:
                quota_results[auth_id] = cached
                _status, _info = cached
                resets_at = _int_ts((_info or {}).get("resets_at"))
                logger.info(
                    "[swap_seat] quota %s: cached exhausted until %s，未到 reset 前不参与 swap",
                    email or auth_id,
                    resets_at,
                )
                continue
            cached = cached_recent_quota_result(auth, account_id=account_id)
            if cached:
                quota_results[auth_id] = cached
                status, info = cached
                logger.info(
                    "[swap_seat] quota %s: cached recent ok %s，避免频繁检查 CPA quota",
                    email or auth_id,
                    quota_remaining_log_text(status, info),
                )
                continue
            status, info = check_cpa_codex_quota(auth, account_id=account_id)
            quota_results[auth_id] = (status, info)
            record_quota_result(auth, status, info, account_id=account_id)
            if status == "auth_error":
                mark_auth_error_for_pat_repair(auth, info)
            logger.info(
                "[swap_seat] quota %s: status=%s %s",
                email or auth_id,
                status,
                quota_remaining_log_text(status, info),
            )

        plan = build_swap_plan(
            team_members,
            cpa_auths,
            quota_results,
            max_chatgpt_active=max_chatgpt_active,
            forced_codex_emails=forced_codex_emails,
            whitelist_emails=whitelist_emails,
            managed_auth_names=managed_auth_names,
            managed_emails=managed_member_emails,
        )
        summary = plan["summary"]
        no_quota_available = summary["quota_available"] <= 0

        has_pending_updates = any(action["needs_update"] for action in plan["seat_actions"]) or any(
            action["needs_update"] for action in plan["oauth_actions"]
        )
        if no_quota_available and not has_pending_updates:
            logger.warning("[swap_seat] 没有任何 quota 可用账号，且 seat/OAuth 已全部处于 Codex/disabled 状态")
            return {
                "mode": "swap_seat",
                "skipped": True,
                "reason": "no_quota_available",
                "team": _team_result_context(team_context, account_id),
                "max_chatgpt_active": max_chatgpt_active,
                "whitelist_emails": sorted(whitelist_emails),
                "cooldown": cooldown_status,
                "selected_emails": [],
                "summary": {**summary, "finished_at": int(time.time())},
                "seat_results": [],
                "oauth_results": [],
            }
        if no_quota_available:
            logger.warning("[swap_seat] 没有任何 quota 可用账号，仅执行 Codex seat / CPA OAuth disabled 安全收敛")

        logger.info(
            "[swap_seat] 计划: selected=%d/%d, to_chatgpt=%d, to_codex=%d, oauth_enable=%d, oauth_disable=%d",
            summary["selected_chatgpt"],
            max_chatgpt_active,
            summary["to_chatgpt"],
            summary["to_codex"],
            summary["oauth_enable"],
            summary["oauth_disable"],
        )

        if not any(action["needs_update"] for action in plan["seat_actions"]) and not any(
            action["needs_update"] for action in plan["oauth_actions"]
        ):
            logger.info("[swap_seat] 当前 seat/OAuth 已是目标状态，本轮不做无用 swap")
            return {
                "mode": "swap_seat",
                "skipped": True,
                "reason": "no_changes_needed",
                "team": _team_result_context(team_context, account_id),
                "max_chatgpt_active": max_chatgpt_active,
                "whitelist_emails": sorted(whitelist_emails),
                "cooldown": cooldown_status,
                "selected_emails": plan["selected_emails"],
                "summary": {**summary, "finished_at": int(time.time())},
                "seat_results": [],
                "oauth_results": [],
            }

        cooldown_status = reserve_swap_cooldown_slot(scope=cooldown_scope)
        logger.info(
            "[swap_seat] 冷却额度已占用: today=%d/%d, next_allowed_at=%s",
            cooldown_status["used_today"],
            cooldown_status["daily_limit"],
            cooldown_status["next_allowed_at"],
        )

        seat_results = []
        # 先把不该占 ChatGPT 的 seat 切到 Codex，再启用目标 ChatGPT seat。
        seat_actions = sorted(plan["seat_actions"], key=lambda a: 0 if a["desired_seat"] == "codex" else 1)
        for action in seat_actions:
            _abort_if_cancel_requested()
            if not action["needs_update"]:
                action["result"] = "unchanged"
                seat_results.append(action)
                continue
            if not action.get("user_id"):
                action["result"] = "skipped"
                action["error"] = "missing user_id"
                seat_results.append(action)
                logger.warning("[swap_seat] 跳过 seat 切换，缺少 user_id: %s", action["email"])
                continue

            result = managed_chatgpt.update_member_seat_type(action["user_id"], action["desired_seat_raw"])
            if result.get("status") in (200, 204):
                action["result"] = "updated"
                logger.info("[swap_seat] %s seat -> %s", action["email"], action["desired_seat"])
            else:
                action["result"] = "failed"
                action["error"] = f"HTTP {result.get('status')}: {str(result.get('body') or '')[:200]}"
                logger.warning("[swap_seat] %s seat 切换失败: %s", action["email"], action["error"])
            seat_results.append(action)

        chatgpt_ready_emails = {
            normalized_email(action.get("email"))
            for action in seat_results
            if action.get("desired_seat") == "chatgpt" and action.get("result") in {"updated", "unchanged"}
        }
        for action in plan["oauth_actions"]:
            if action.get("desired_disabled") or normalized_email(action.get("email")) in chatgpt_ready_emails:
                continue
            action["desired_disabled"] = True
            action["desired_status"] = "disabled"
            action["needs_update"] = not cpa_auth_is_disabled(
                {
                    "disabled": action.get("current_disabled"),
                    "status": action.get("status"),
                }
            )
            action["blocked_enable_reason"] = "team_seat_not_chatgpt"
        summary["oauth_enable"] = sum(1 for action in plan["oauth_actions"] if not action["desired_disabled"] and action["needs_update"])
        summary["oauth_disable"] = sum(1 for action in plan["oauth_actions"] if action["desired_disabled"] and action["needs_update"])

        oauth_results = []
        # 先 disable 其他 OAuth，最后 enable 选中的目标账号，避免中间态 active 超过保留数。
        oauth_actions = sorted(plan["oauth_actions"], key=lambda a: 0 if a["desired_disabled"] else 1)
        for action in oauth_actions:
            _abort_if_cancel_requested()
            if not action["needs_update"]:
                action["result"] = "unchanged"
                oauth_results.append(action)
                continue
            if not action.get("name"):
                action["result"] = "skipped"
                action["error"] = "missing name/id"
                oauth_results.append(action)
                continue
            try:
                set_cpa_auth_disabled(action["name"], action["desired_disabled"])
                action["result"] = "updated"
                logger.info(
                    "[swap_seat] CPA OAuth %s -> %s",
                    action["email"] or action["name"],
                    "disabled/standby" if action["desired_disabled"] else "active",
                )
            except Exception as exc:
                action["result"] = "failed"
                action["error"] = str(exc)
                logger.warning("[swap_seat] CPA OAuth %s 更新失败: %s", action["email"] or action["name"], exc)
            oauth_results.append(action)

        result = {
            "mode": "swap_seat",
            "team": _team_result_context(team_context, account_id),
            "max_chatgpt_active": max_chatgpt_active,
            "whitelist_emails": sorted(whitelist_emails),
            "cooldown": cooldown_status,
            "selected_emails": plan["selected_emails"],
            "summary": {
                **summary,
                "seat_updated": sum(1 for item in seat_results if item.get("result") == "updated"),
                "seat_failed": sum(1 for item in seat_results if item.get("result") == "failed"),
                "oauth_updated": sum(1 for item in oauth_results if item.get("result") == "updated"),
                "oauth_failed": sum(1 for item in oauth_results if item.get("result") == "failed"),
                "finished_at": int(time.time()),
            },
            "seat_results": seat_results,
            "oauth_results": oauth_results,
        }
        if no_quota_available:
            result["reason"] = "no_quota_available_cleanup"
        return result
    finally:
        if started_here and _chatgpt_session_ready(managed_chatgpt):
            managed_chatgpt.stop()


def cmd_swap_seats_json(max_chatgpt_active: int = 2) -> str:
    """CLI 友好的 JSON 输出包装。"""
    return json.dumps(cmd_swap_seats(max_chatgpt_active), ensure_ascii=False, indent=2)
