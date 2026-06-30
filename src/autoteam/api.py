"""AutoTeam HTTP API - 将 CLI 功能暴露为 HTTP 接口"""

import json
import logging
import os
import re
import signal
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from autoteam.config import API_KEY
from autoteam.session_input import parse_chatgpt_session_token
from autoteam.textio import parse_env_line, read_text, write_text

logger = logging.getLogger(__name__)

app = FastAPI(
    title="AutoTeam API",
    description="swap_seat-only Team quota / seat / CPA OAuth 调度 API",
    version="0.1.0",
)

# ---------------------------------------------------------------------------
# API Key 鉴权中间件
# ---------------------------------------------------------------------------

_AUTH_SKIP_PATHS = {"/api/auth/check", "/api/setup/status", "/api/setup/save"}


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    try:
        _maybe_reload_runtime_config_from_env_file()
    except Exception as exc:
        logger.warning("[配置] 自动热加载失败: %s", exc)

    path = request.url.path
    # 不鉴权的路径：非 /api 路径、auth/check 端点
    if not path.startswith("/api/") or path in _AUTH_SKIP_PATHS:
        return await call_next(request)
    # 未配置 API_KEY 则跳过鉴权
    if not API_KEY:
        return await call_next(request)
    # 从 header 或 query param 获取 key
    auth_header = request.headers.get("authorization", "")
    if auth_header.startswith("Bearer "):
        token = auth_header[7:]
    else:
        token = request.query_params.get("key", "")
    if token != API_KEY:
        return JSONResponse(status_code=401, content={"detail": "未授权，请提供有效的 API Key"})
    return await call_next(request)


@app.get("/api/auth/check")
def check_auth(request: Request):
    """验证 API Key 是否有效。未配置 API_KEY 时始终返回成功。"""
    if not API_KEY:
        return {"authenticated": True, "auth_required": False}
    auth_header = request.headers.get("authorization", "")
    if auth_header.startswith("Bearer ") and auth_header[7:] == API_KEY:
        return {"authenticated": True, "auth_required": True}
    return JSONResponse(status_code=401, content={"authenticated": False, "auth_required": True})


# ---------------------------------------------------------------------------
# 初始配置 API（无需鉴权）
# ---------------------------------------------------------------------------


class SetupConfig(BaseModel):
    MAIL_PROVIDER: str = "cloudmail"
    MAIL_SERVICES_JSON: str = ""
    MAIL_SERVICE_DEFAULT: str = ""
    mail_services: list[dict] = []
    mail_service_default: str = ""
    CLOUDMAIL_BASE_URL: str = ""
    CLOUDMAIL_EMAIL: str = ""
    CLOUDMAIL_PASSWORD: str = ""
    CLOUDMAIL_DOMAIN: str = ""
    CF_TEMP_EMAIL_BASE_URL: str = ""
    CF_TEMP_EMAIL_ADMIN_PASSWORD: str = ""
    CF_TEMP_EMAIL_DOMAIN: str = ""
    PENDING_INVITE_FORWARD_MAP: str = ""
    SYNC_TARGET_CPA: str | bool = ""
    CPA_URL: str = "http://127.0.0.1:8317"
    CPA_KEY: str = ""
    SYNC_TARGET_SUB2API: str | bool = ""
    SUB2API_URL: str = ""
    SUB2API_EMAIL: str = ""
    SUB2API_PASSWORD: str = ""
    SUB2API_GROUP: str = ""
    SUB2API_PROXY: str | int = ""
    SUB2API_CONCURRENCY: str | int = "10"
    SUB2API_PRIORITY: str | int = "1"
    SUB2API_RATE_MULTIPLIER: str | int | float = "1"
    SUB2API_AUTO_PAUSE_ON_EXPIRED: str | bool = "true"
    SUB2API_MODEL_WHITELIST: str = ""
    SUB2API_OPENAI_WS_MODE: str = "off"
    SUB2API_OPENAI_PASSTHROUGH: str | bool = "false"
    SUB2API_OVERWRITE_ACCOUNT_SETTINGS: str | bool = "false"
    SWAP_SEAT_WHITELIST_EMAILS: str = ""
    PLAYWRIGHT_PROXY_URL: str = ""
    PLAYWRIGHT_PROXY_BYPASS: str = ""
    TEAM_WORKSPACES_JSON: str = ""
    AUTO_CHECK_REPLACE_MODE: str = "pending_invite"
    API_KEY: str = ""


class SourceConfig(BaseModel):
    content: str = ""


class AutoCheckConfigParams(BaseModel):
    interval: int | None = None
    target_seats: int | None = None
    replace_with_pending_invite: bool | None = None
    replace_mode: str | None = None
    threshold: int | None = None
    min_low: int | None = None
    retry_add_phone: bool | None = None
    add_phone_max_retries: int | None = None


# 兼容旧单元测试/内部调用名称；HTTP 路由统一使用同一模型。
AutoCheckConfig = AutoCheckConfigParams


_RUNTIME_CONFIG_CLEARABLE_FIELDS = {
    "MAIL_PROVIDER",
    "MAIL_SERVICES_JSON",
    "MAIL_SERVICE_DEFAULT",
    "CLOUDMAIL_BASE_URL",
    "CLOUDMAIL_EMAIL",
    "CLOUDMAIL_PASSWORD",
    "CLOUDMAIL_DOMAIN",
    "CF_TEMP_EMAIL_BASE_URL",
    "CF_TEMP_EMAIL_ADMIN_PASSWORD",
    "CF_TEMP_EMAIL_DOMAIN",
    "PENDING_INVITE_FORWARD_MAP",
    "SUB2API_GROUP",
    "SUB2API_PROXY",
    "SUB2API_MODEL_WHITELIST",
    "SWAP_SEAT_WHITELIST_EMAILS",
    "PLAYWRIGHT_PROXY_URL",
    "PLAYWRIGHT_PROXY_BYPASS",
    "TEAM_WORKSPACES_JSON",
    "AUTO_CHECK_REPLACE_MODE",
}

_CLOUDMAIL_REQUIRED_KEYS = ("CLOUDMAIL_BASE_URL", "CLOUDMAIL_EMAIL", "CLOUDMAIL_PASSWORD", "CLOUDMAIL_DOMAIN")
_CF_TEMP_EMAIL_REQUIRED_KEYS = (
    "CF_TEMP_EMAIL_BASE_URL",
    "CF_TEMP_EMAIL_ADMIN_PASSWORD",
    "CF_TEMP_EMAIL_DOMAIN",
)
_CPA_REQUIRED_KEYS = ("CPA_URL", "CPA_KEY")
_SUB2API_REQUIRED_KEYS = ("SUB2API_URL", "SUB2API_EMAIL", "SUB2API_PASSWORD")
_SYNC_TARGET_TOGGLE_KEYS = ("SYNC_TARGET_CPA", "SYNC_TARGET_SUB2API")
SWAP_ACTIVE_MIN = 1
SWAP_ACTIVE_MAX = 5

_ALL_RUNTIME_ENV_KEYS = [
    "MAIL_PROVIDER",
    "MAIL_SERVICES_JSON",
    "MAIL_SERVICE_DEFAULT",
    "CLOUDMAIL_BASE_URL",
    "CLOUDMAIL_EMAIL",
    "CLOUDMAIL_PASSWORD",
    "CLOUDMAIL_DOMAIN",
    "CF_TEMP_EMAIL_BASE_URL",
    "CF_TEMP_EMAIL_ADMIN_PASSWORD",
    "CF_TEMP_EMAIL_DOMAIN",
    "PENDING_INVITE_FORWARD_MAP",
    "CHATGPT_ACCOUNT_ID",
    "SYNC_TARGET_CPA",
    "CPA_URL",
    "CPA_KEY",
    "SYNC_TARGET_SUB2API",
    "SUB2API_URL",
    "SUB2API_EMAIL",
    "SUB2API_PASSWORD",
    "SUB2API_GROUP",
    "SUB2API_PROXY",
    "SUB2API_CONCURRENCY",
    "SUB2API_PRIORITY",
    "SUB2API_RATE_MULTIPLIER",
    "SUB2API_AUTO_PAUSE_ON_EXPIRED",
    "SUB2API_MODEL_WHITELIST",
    "SUB2API_OPENAI_WS_MODE",
    "SUB2API_OPENAI_PASSTHROUGH",
    "SUB2API_OVERWRITE_ACCOUNT_SETTINGS",
    "SWAP_SEAT_WHITELIST_EMAILS",
    "TEAM_WORKSPACES_JSON",
    "EMAIL_POLL_INTERVAL",
    "EMAIL_POLL_TIMEOUT",
    "API_KEY",
    "AUTO_CHECK_INTERVAL",
    "AUTO_CHECK_TARGET_SEATS",
    "AUTO_CHECK_REPLACE_WITH_PENDING_INVITE",
    "AUTO_CHECK_REPLACE_MODE",
    "AUTO_CHECK_THRESHOLD",
    "AUTO_CHECK_MIN_LOW",
    "AUTO_CHECK_RETRY_ADD_PHONE",
    "AUTO_CHECK_ADD_PHONE_MAX_RETRIES",
    "PLAYWRIGHT_PROXY_URL",
    "PLAYWRIGHT_PROXY_SERVER",
    "PLAYWRIGHT_PROXY_USERNAME",
    "PLAYWRIGHT_PROXY_PASSWORD",
    "PLAYWRIGHT_PROXY_BYPASS",
]
_RUNTIME_ENV_BASE = {key: os.environ.get(key) for key in _ALL_RUNTIME_ENV_KEYS}
_runtime_env_reload_lock = threading.Lock()
_runtime_env_reload_state = {"signature": None}


def _runtime_config_prompt_map():
    from autoteam.setup_wizard import REQUIRED_CONFIGS

    return {key: prompt for key, prompt, _default, _optional in REQUIRED_CONFIGS}


def _current_runtime_env():
    from autoteam.setup_wizard import _read_env

    env = _read_env()
    merged = {key: value for key, value in os.environ.items()}
    merged.update({key: value for key, value in env.items() if value is not None})
    return merged


def _missing_runtime_configs(keys: tuple[str, ...] | list[str], *, env: dict[str, str] | None = None):
    env_values = env or _current_runtime_env()
    prompt_map = _runtime_config_prompt_map()
    missing = []
    for key in keys:
        value = (env_values.get(key, "") or "").strip()
        if not value:
            missing.append((key, prompt_map.get(key, key)))
    return missing


def _format_missing_runtime_configs(missing: list[tuple[str, str]]) -> str:
    return "、".join(f"{key}（{prompt}）" for key, prompt in missing)


def _normalize_swap_active_limit(value: object = 2, *, default: int = 2) -> int:
    try:
        count = int(value)
    except Exception:
        count = int(default)
    return max(SWAP_ACTIVE_MIN, min(SWAP_ACTIVE_MAX, count))


def _normalize_auto_check_replace_mode(value: object = "pending_invite") -> str:
    mode = str(value or "pending_invite").strip().lower().replace("-", "_")
    if mode in {"pending", "pending_invite"}:
        return "pending_invite"
    if mode in {"create", "create_invite", "invite_add"}:
        return "create_invite"
    raise ValueError("AUTO_CHECK_REPLACE_MODE 必须是 pending_invite 或 create_invite")


def _resolve_auto_check_replace_mode(value: object = None) -> str:
    try:
        return _normalize_auto_check_replace_mode(value or _auto_check_config.get("replace_mode", "pending_invite"))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _team_active_limit_or_default(team_context=None, default_value: object = 2) -> int:
    """Team 手动操作默认使用该 Team 自己的 ChatGPT/OAuth active 保留数。"""
    default_limit = _normalize_swap_active_limit(default_value)
    if team_context is None:
        return default_limit
    return _normalize_swap_active_limit(getattr(team_context, "max_chatgpt_active", default_limit), default=default_limit)


def _effective_sync_target_states(env: dict[str, str] | None = None):
    from autoteam.sync_targets import get_sync_target_states

    return get_sync_target_states(env or _current_runtime_env())


def _runtime_required_keys(env: dict[str, str] | None = None) -> set[str]:
    from autoteam.mail_provider import get_mail_provider_name, get_mail_provider_required_keys

    states = _effective_sync_target_states(env)
    provider = get_mail_provider_name(env)
    required = set(get_mail_provider_required_keys(provider))
    required.add("API_KEY")
    if states.get("cpa"):
        required.update(_CPA_REQUIRED_KEYS)
    if states.get("sub2api"):
        required.update(_SUB2API_REQUIRED_KEYS)
    return required


def _format_mail_service_missing_fields(missing: list[str]) -> str:
    prompt_map = {
        "base_url": "base_url",
        "email": "email",
        "password": "password",
        "admin_password": "admin_password",
        "domain": "domain",
    }
    return "、".join(prompt_map.get(field, field) for field in missing)


def _require_runtime_configs(
    keys: tuple[str, ...] | list[str], action_label: str, *, env: dict[str, str] | None = None
):
    missing = _missing_runtime_configs(keys, env=env)
    if not missing:
        return

    detail = _format_missing_runtime_configs(missing)
    raise HTTPException(status_code=400, detail=f"{action_label} 前请先在配置面板填写：{detail}")


def _require_mail_provider_configs(
    action_label: str, *, provider: str | None = None, env: dict[str, str] | None = None
):
    from autoteam.mail_provider import (
        get_default_mail_service,
        get_mail_provider_name,
        get_mail_provider_prompt,
        get_mail_provider_required_keys,
        get_mail_service_display_name,
        get_mail_service_missing_fields,
        get_mail_services,
        normalize_mail_provider,
    )

    env_values = env or _current_runtime_env()
    target_provider = normalize_mail_provider(provider, default="") if provider else ""
    default_service = get_default_mail_service(env_values)
    resolved_service = None
    if default_service and (not target_provider or default_service.get("type") == target_provider):
        resolved_service = default_service
    elif target_provider:
        matches = [item for item in get_mail_services(env_values) if item.get("type") == target_provider]
        if len(matches) == 1:
            resolved_service = matches[0]

    if resolved_service:
        missing_fields = get_mail_service_missing_fields(resolved_service)
        if missing_fields:
            label = get_mail_service_display_name(resolved_service)
            detail = _format_mail_service_missing_fields(missing_fields)
            raise HTTPException(
                status_code=400,
                detail=f"{action_label} 前请先在配置面板补全邮箱服务（{label}）配置：{detail}",
            )
        return

    resolved_provider = target_provider or get_mail_provider_name(env_values)
    missing = _missing_runtime_configs(get_mail_provider_required_keys(resolved_provider), env=env_values)
    if not missing:
        return

    detail = _format_missing_runtime_configs(missing)
    provider_label = get_mail_provider_prompt(resolved_provider)
    raise HTTPException(
        status_code=400,
        detail=f"{action_label} 前请先在配置面板填写当前邮箱服务（{provider_label}）配置：{detail}",
    )


def _require_pool_operation_configs(action_label: str):
    from autoteam.sync_targets import get_enabled_sync_targets

    env = _current_runtime_env()
    _require_mail_provider_configs(action_label, env=env)

    enabled_targets = get_enabled_sync_targets(env)
    if not enabled_targets:
        raise HTTPException(
            status_code=400, detail=f"{action_label} 前请先在配置面板启用至少一个远端同步目标（CPA 或 Sub2API）"
        )

    missing = _missing_runtime_configs(
        [
            key
            for target in enabled_targets
            for key in (
                _CPA_REQUIRED_KEYS if target == "cpa" else _SUB2API_REQUIRED_KEYS if target == "sub2api" else ()
            )
        ],
        env=env,
    )
    if missing:
        detail = _format_missing_runtime_configs(missing)
        raise HTTPException(status_code=400, detail=f"{action_label} 前请先在配置面板填写：{detail}")


def _require_account_mail_configs(account: dict, action_label: str):
    from autoteam.mail_provider import (
        get_account_mail_service,
        get_mail_service_display_name,
        get_mail_service_missing_fields,
    )

    env = _current_runtime_env()
    service = get_account_mail_service(account, env=env)
    if not service:
        email = str((account or {}).get("email") or "").strip() or "<unknown>"
        raise HTTPException(
            status_code=400,
            detail=(
                f"{action_label} 前无法唯一确定账号 {email} 对应的邮箱服务；"
                "请检查 mail_service_id，或确保该邮箱域名只匹配一个已配置邮箱服务"
            ),
        )

    missing_fields = get_mail_service_missing_fields(service)
    if not missing_fields:
        return

    label = get_mail_service_display_name(service)
    detail = _format_mail_service_missing_fields(missing_fields)
    raise HTTPException(status_code=400, detail=f"{action_label} 前请先补全邮箱服务（{label}）配置：{detail}")


def _require_cpa_configs(action_label: str):
    _require_runtime_configs(_CPA_REQUIRED_KEYS, action_label)


def _require_sync_target_configs(action_label: str):
    from autoteam.sync_targets import get_enabled_sync_targets

    env = _current_runtime_env()
    enabled_targets = get_enabled_sync_targets(env)
    if not enabled_targets:
        raise HTTPException(
            status_code=400, detail=f"{action_label} 前请先在配置面板启用至少一个远端同步目标（CPA 或 Sub2API）"
        )

    missing = _missing_runtime_configs(
        [
            key
            for target in enabled_targets
            for key in (
                _CPA_REQUIRED_KEYS if target == "cpa" else _SUB2API_REQUIRED_KEYS if target == "sub2api" else ()
            )
        ],
        env=env,
    )
    if missing:
        detail = _format_missing_runtime_configs(missing)
        raise HTTPException(status_code=400, detail=f"{action_label} 前请先在配置面板填写：{detail}")


def _collect_config_fields(*, include_values: bool = False, configs=None):
    from autoteam.mail_provider import get_default_mail_service_id, get_mail_provider_name, get_mail_services
    from autoteam.setup_wizard import REQUIRED_CONFIGS, _read_env

    env = _read_env()
    merged_env = dict(os.environ)
    merged_env.update(env)
    target_states = _effective_sync_target_states(merged_env)
    runtime_required_keys = _runtime_required_keys(merged_env)
    mail_provider = get_mail_provider_name(merged_env)
    mail_services = get_mail_services(merged_env)
    mail_service_default = get_default_mail_service_id(merged_env, services=mail_services)
    config_items = configs or REQUIRED_CONFIGS
    fields = []
    all_ok = True
    for key, prompt, default, optional in config_items:
        raw_value = env.get(key, "") or os.environ.get(key, "")
        if key == "SYNC_TARGET_CPA":
            raw_value = "true" if target_states.get("cpa") else "false"
            configured = True
        elif key == "SYNC_TARGET_SUB2API":
            raw_value = "true" if target_states.get("sub2api") else "false"
            configured = True
        elif key == "MAIL_PROVIDER":
            raw_value = mail_provider
            configured = True
        else:
            configured = bool(raw_value)
        if not configured and (key in runtime_required_keys or not optional):
            all_ok = False

        field = {
            "key": key,
            "prompt": prompt,
            "default": default,
            "optional": optional,
            "configured": configured,
        }
        if include_values:
            field["value"] = raw_value if raw_value != "" else default
            field["runtime_required"] = key in runtime_required_keys
        fields.append(field)
    return {
        "configured": all_ok,
        "fields": fields,
        "mail_services": mail_services,
        "mail_service_default": mail_service_default,
    }


def _reload_runtime_config_modules():
    import importlib

    import autoteam.config

    modules = [autoteam.config]
    for module_name in (
        "autoteam.cloudmail",
        "autoteam.cloudflare_temp_email",
        "autoteam.mail_provider",
        "autoteam.cpa_sync",
        "autoteam.sub2api_sync",
    ):
        try:
            module = importlib.import_module(module_name)
        except Exception:
            continue
        modules.append(module)

    for module in modules:
        importlib.reload(module)


def _restore_runtime_env(previous_env: dict[str, str | None]):
    for key, previous in previous_env.items():
        if previous is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = previous


def _runtime_env_file_signature():
    from autoteam.setup_wizard import ENV_FILE

    if not ENV_FILE.exists():
        return None
    stat = ENV_FILE.stat()
    return (stat.st_mtime_ns, stat.st_size)


def _read_runtime_env_file_text():
    from autoteam.setup_wizard import ENV_FILE

    if not ENV_FILE.exists():
        return ""
    return read_text(ENV_FILE)


def _read_runtime_source_text():
    from autoteam.setup_wizard import ENV_EXAMPLE, ENV_FILE

    if ENV_FILE.exists():
        return read_text(ENV_FILE), str(ENV_FILE)
    if ENV_EXAMPLE.exists():
        return read_text(ENV_EXAMPLE), str(ENV_FILE)
    return "", str(ENV_FILE)


def _write_runtime_source_text(content: str):
    from autoteam.setup_wizard import ENV_FILE

    write_text(ENV_FILE, content)


def _restore_runtime_source_text(previous_exists: bool, previous_content: str):
    from autoteam.setup_wizard import ENV_FILE

    if previous_exists:
        write_text(ENV_FILE, previous_content)
        return
    if ENV_FILE.exists():
        ENV_FILE.unlink()


def _load_env_values_from_source(content: str, env_keys: list[str]):
    values = {key: "" for key in env_keys}
    for line in content.splitlines():
        parsed = parse_env_line(line)
        if not parsed:
            continue
        key, value = parsed
        if key in values:
            values[key] = value
    return values


def _load_present_env_values_from_source(content: str, env_keys: list[str]):
    allowed = set(env_keys)
    values = {}
    for line in content.splitlines():
        parsed = parse_env_line(line)
        if not parsed:
            continue
        key, value = parsed
        if key in allowed:
            values[key] = value
    return values


def _validate_runtime_required_values(values: dict[str, str]):
    from autoteam.setup_wizard import STARTUP_REQUIRED_CONFIGS

    return [
        f"{key} ({prompt})"
        for key, prompt, _default, optional in STARTUP_REQUIRED_CONFIGS
        if not optional and not values.get(key)
    ]


def _parse_bool_text(value: object, *, default: bool | None = None) -> bool | None:
    if value is None:
        return default
    text = str(value).strip()
    if not text:
        return default
    lowered = text.lower()
    if lowered in {"1", "true", "yes", "on", "enabled"}:
        return True
    if lowered in {"0", "false", "no", "off", "disabled"}:
        return False
    raise ValueError(f"无效布尔值: {value}")


def _normalize_pending_invite_forward_map(value: object) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""

    items: list[tuple[object, object]] = []
    try:
        parsed = json.loads(raw)
    except Exception:
        parsed = None

    if isinstance(parsed, dict):
        items = list(parsed.items())
    elif isinstance(parsed, list):
        for index, item in enumerate(parsed, 1):
            if not isinstance(item, dict):
                raise ValueError(f"PENDING_INVITE_FORWARD_MAP 第 {index} 项必须是对象")
            source = item.get("from") or item.get("source") or item.get("domain") or item.get("email")
            target = item.get("to") or item.get("target") or item.get("mailbox") or item.get("recipient")
            items.append((source, target))
    else:
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
                raise ValueError("PENDING_INVITE_FORWARD_MAP 请使用 icloud.com=收件邮箱@example.com 的格式")
            items.append((source, target))

    email_pattern = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    domain_pattern = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+$")
    normalized: list[str] = []
    seen: set[str] = set()
    for index, (source, target) in enumerate(items, 1):
        source_key = str(source or "").strip().lower().lstrip("@")
        if source_key.startswith("*."):
            source_key = source_key[2:]
        target_email = str(target or "").strip().lower()
        if not source_key:
            raise ValueError(f"PENDING_INVITE_FORWARD_MAP 第 {index} 项缺少来源域名或邮箱")
        if "@" in source_key:
            if not email_pattern.match(source_key):
                raise ValueError(f"PENDING_INVITE_FORWARD_MAP 第 {index} 项来源邮箱无效: {source_key}")
        elif not domain_pattern.match(source_key):
            raise ValueError(f"PENDING_INVITE_FORWARD_MAP 第 {index} 项来源域名无效: {source_key}")
        if not email_pattern.match(target_email):
            raise ValueError(f"PENDING_INVITE_FORWARD_MAP 第 {index} 项收件邮箱无效: {target_email or '<empty>'}")
        if source_key in seen:
            continue
        seen.add(source_key)
        normalized.append(f"{source_key}={target_email}")
    if not normalized:
        raise ValueError("PENDING_INVITE_FORWARD_MAP 没有可用映射，请填写 icloud.com=收件邮箱@example.com")
    return ";".join(normalized)


def _validate_runtime_optional_values(values: dict[str, str]):
    from autoteam.cpa_config import normalize_cpa_url

    normalized = dict(values)

    if "CPA_URL" in normalized:
        normalized["CPA_URL"] = normalize_cpa_url(normalized.get("CPA_URL", ""))
    if "PENDING_INVITE_FORWARD_MAP" in normalized:
        normalized["PENDING_INVITE_FORWARD_MAP"] = _normalize_pending_invite_forward_map(
            normalized.get("PENDING_INVITE_FORWARD_MAP", "")
        )

    def _normalize_positive_int(key: str):
        raw = str(normalized.get(key, "") or "").strip()
        if not raw:
            return
        try:
            value = int(raw)
        except ValueError as exc:
            raise ValueError(f"{key} 必须是正整数") from exc
        if value <= 0:
            raise ValueError(f"{key} 必须是正整数")
        normalized[key] = str(value)

    def _normalize_int(key: str):
        raw = str(normalized.get(key, "") or "").strip()
        if not raw:
            return
        try:
            value = int(raw)
        except ValueError as exc:
            raise ValueError(f"{key} 必须是整数") from exc
        normalized[key] = str(value)

    def _normalize_positive_float(key: str):
        raw = str(normalized.get(key, "") or "").strip()
        if not raw:
            return
        try:
            value = float(raw)
        except ValueError as exc:
            raise ValueError(f"{key} 必须是大于 0 的数字") from exc
        if value <= 0:
            raise ValueError(f"{key} 必须是大于 0 的数字")
        normalized[key] = format(value, "g")

    def _normalize_bool(key: str):
        try:
            value = _parse_bool_text(normalized.get(key, ""), default=None)
        except ValueError as exc:
            raise ValueError(f"{key} 必须是 true 或 false") from exc
        if value is None:
            return
        normalized[key] = "true" if value else "false"

    def _normalize_sub2api_proxy(key: str):
        raw = str(normalized.get(key, "") or "").strip()
        if not raw:
            normalized[key] = ""
            return
        if raw.lstrip("+-").isdigit():
            try:
                value = int(raw)
            except ValueError as exc:
                raise ValueError(f"{key} 必须是 Sub2API 代理 ID（正整数）或代理名称") from exc
            if value <= 0:
                raise ValueError(f"{key} 必须是 Sub2API 代理 ID（正整数）或代理名称")
            normalized[key] = str(value)
            return
        normalized[key] = raw

    _normalize_sub2api_proxy("SUB2API_PROXY")
    _normalize_positive_int("SUB2API_CONCURRENCY")
    _normalize_int("SUB2API_PRIORITY")
    _normalize_positive_float("SUB2API_RATE_MULTIPLIER")
    _normalize_bool("SUB2API_AUTO_PAUSE_ON_EXPIRED")
    _normalize_bool("SUB2API_OPENAI_PASSTHROUGH")
    _normalize_bool("SUB2API_OVERWRITE_ACCOUNT_SETTINGS")

    ws_mode = str(normalized.get("SUB2API_OPENAI_WS_MODE", "") or "").strip().lower()
    if ws_mode:
        if ws_mode not in {"off", "ctx_pool", "passthrough"}:
            raise ValueError("SUB2API_OPENAI_WS_MODE 必须是 off、ctx_pool 或 passthrough")
        normalized["SUB2API_OPENAI_WS_MODE"] = ws_mode

    whitelist = str(normalized.get("SUB2API_MODEL_WHITELIST", "") or "").strip()
    if whitelist:
        normalized["SUB2API_MODEL_WHITELIST"] = ",".join(part.strip() for part in whitelist.split(",") if part.strip())
    else:
        normalized["SUB2API_MODEL_WHITELIST"] = ""

    swap_whitelist = str(normalized.get("SWAP_SEAT_WHITELIST_EMAILS", "") or "").strip()
    if swap_whitelist:
        parts = [part.strip().lower() for part in re.split(r"[,;\s]+", swap_whitelist) if part.strip()]
        normalized["SWAP_SEAT_WHITELIST_EMAILS"] = ",".join(dict.fromkeys(parts))
    else:
        normalized["SWAP_SEAT_WHITELIST_EMAILS"] = ""

    teams_json = str(normalized.get("TEAM_WORKSPACES_JSON", "") or "").strip()
    if teams_json:
        try:
            parsed = json.loads(teams_json)
        except Exception as exc:
            raise ValueError(f"TEAM_WORKSPACES_JSON 必须是有效 JSON: {exc}") from exc
        if isinstance(parsed, dict):
            teams = parsed.get("teams") or parsed.get("workspaces") or parsed.get("items")
        else:
            teams = parsed
        if not isinstance(teams, list):
            raise ValueError("TEAM_WORKSPACES_JSON 必须是数组，或包含 teams/workspaces/items 数组")
        for index, item in enumerate(teams):
            if not isinstance(item, dict):
                raise ValueError(f"TEAM_WORKSPACES_JSON 第 {index + 1} 项必须是对象")
            if not str(item.get("account_id") or item.get("accountId") or item.get("id") or "").strip():
                raise ValueError(f"TEAM_WORKSPACES_JSON 第 {index + 1} 项缺少 account_id")
        normalized["TEAM_WORKSPACES_JSON"] = json.dumps(parsed, ensure_ascii=False, separators=(",", ":"))
    else:
        normalized["TEAM_WORKSPACES_JSON"] = ""

    replace_mode = str(normalized.get("AUTO_CHECK_REPLACE_MODE", "") or "").strip()
    if replace_mode:
        normalized["AUTO_CHECK_REPLACE_MODE"] = _normalize_auto_check_replace_mode(replace_mode)
    else:
        normalized["AUTO_CHECK_REPLACE_MODE"] = "pending_invite"

    return normalized


def _sync_runtime_globals():
    global API_KEY

    API_KEY = os.environ.get("API_KEY", "")

    auto_check_config = globals().get("_auto_check_config")
    auto_check_restart = globals().get("_auto_check_restart")
    if auto_check_config is None:
        return

    try:
        from autoteam.config import (
            AUTO_CHECK_ADD_PHONE_MAX_RETRIES,
            AUTO_CHECK_INTERVAL,
            AUTO_CHECK_MIN_LOW,
            AUTO_CHECK_REPLACE_MODE,
            AUTO_CHECK_REPLACE_WITH_PENDING_INVITE,
            AUTO_CHECK_RETRY_ADD_PHONE,
            AUTO_CHECK_TARGET_SEATS,
            AUTO_CHECK_THRESHOLD,
        )

        auto_check_config["interval"] = AUTO_CHECK_INTERVAL
        auto_check_config["target_seats"] = AUTO_CHECK_TARGET_SEATS
        auto_check_config["replace_with_pending_invite"] = AUTO_CHECK_REPLACE_WITH_PENDING_INVITE
        auto_check_config["replace_mode"] = AUTO_CHECK_REPLACE_MODE
        auto_check_config["threshold"] = AUTO_CHECK_THRESHOLD
        auto_check_config["min_low"] = AUTO_CHECK_MIN_LOW
        auto_check_config["retry_add_phone"] = AUTO_CHECK_RETRY_ADD_PHONE
        auto_check_config["add_phone_max_retries"] = AUTO_CHECK_ADD_PHONE_MAX_RETRIES
        if auto_check_restart is not None:
            auto_check_restart.set()
    except Exception:
        pass


def _apply_runtime_env_file_values(values: dict[str, str]):
    for key in _ALL_RUNTIME_ENV_KEYS:
        if key in values:
            value = values[key]
            if value:
                os.environ[key] = value
            else:
                os.environ.pop(key, None)
            continue

        base_value = _RUNTIME_ENV_BASE.get(key)
        if base_value:
            os.environ[key] = base_value
        else:
            os.environ.pop(key, None)


def _sync_runtime_env_reload_state():
    with _runtime_env_reload_lock:
        _runtime_env_reload_state["signature"] = _runtime_env_file_signature()


def _maybe_reload_runtime_config_from_env_file(*, force: bool = False):
    signature = _runtime_env_file_signature()

    with _runtime_env_reload_lock:
        previous_signature = _runtime_env_reload_state.get("signature")
        if not force and signature == previous_signature:
            return False

        previous_env = {key: os.environ.get(key) for key in _ALL_RUNTIME_ENV_KEYS}
        try:
            content = _read_runtime_env_file_text()
            values = _load_present_env_values_from_source(content, _ALL_RUNTIME_ENV_KEYS)
            _apply_runtime_env_file_values(values)
            _reload_runtime_config_modules()
            _sync_runtime_globals()
            _runtime_env_reload_state["signature"] = signature
        except Exception:
            _restore_runtime_env(previous_env)
            _reload_runtime_config_modules()
            _sync_runtime_globals()
            raise

    if previous_signature is not None and signature != previous_signature:
        logger.info("[配置] 检测到 .env 变更，已自动热加载")
    return True


def _verify_runtime_integrations(
    previous_env: dict[str, str | None] | None = None, *, env: dict[str, object] | None = None
):
    from autoteam.mail_provider import (
        get_default_mail_service,
        get_mail_provider_name,
        get_mail_provider_prompt,
        get_mail_service_display_name,
        get_mail_service_missing_fields,
        get_mail_services,
        normalize_mail_services,
    )
    from autoteam.setup_wizard import _verify_cpa, _verify_mail_provider, _verify_mail_service, _verify_sub2api

    errors = []
    runtime_env = dict(env or os.environ)
    previous = previous_env or {}
    mail_keys = (
        "MAIL_PROVIDER",
        "MAIL_SERVICES_JSON",
        "MAIL_SERVICE_DEFAULT",
        "CLOUDMAIL_BASE_URL",
        "CLOUDMAIL_EMAIL",
        "CLOUDMAIL_PASSWORD",
        "CLOUDMAIL_DOMAIN",
        "CF_TEMP_EMAIL_BASE_URL",
        "CF_TEMP_EMAIL_ADMIN_PASSWORD",
        "CF_TEMP_EMAIL_DOMAIN",
    )
    cpa_keys = ("SYNC_TARGET_CPA", "CPA_URL", "CPA_KEY")
    sub2api_keys = ("SYNC_TARGET_SUB2API", "SUB2API_URL", "SUB2API_EMAIL", "SUB2API_PASSWORD")

    def _changed(keys: tuple[str, ...]) -> bool:
        if previous_env is None:
            return True
        for key in keys:
            before = "" if previous.get(key) is None else str(previous.get(key))
            after = "" if runtime_env.get(key) is None else str(runtime_env.get(key))
            if before != after:
                return True
        return False

    def _verification_error(label: str, verifier, *args) -> str | None:
        try:
            try:
                ok = verifier(*args, raise_errors=True)
            except TypeError as exc:
                if "raise_errors" not in str(exc):
                    raise
                ok = verifier(*args)
        except Exception as exc:
            return f"{label}: {exc}"
        if not ok:
            return f"{label}: 连接失败"
        return None

    cpa_values = [runtime_env.get(key, "") for key in cpa_keys[1:]]
    sub2api_values = [runtime_env.get(key, "") for key in sub2api_keys[1:]]
    sync_states = _effective_sync_target_states(runtime_env)

    if _changed(mail_keys):
        raw_structured_services = normalize_mail_services(runtime_env.get("MAIL_SERVICES_JSON"))
        if raw_structured_services:
            default_service = get_default_mail_service(runtime_env, services=raw_structured_services)
            if not default_service:
                errors.append("邮箱服务缺少默认服务")
            for service in raw_structured_services:
                missing_fields = get_mail_service_missing_fields(service)
                label = get_mail_service_display_name(service)
                if missing_fields:
                    errors.append(f"{label} 缺少配置: {_format_mail_service_missing_fields(missing_fields)}")
                    continue
                error = _verification_error(label, _verify_mail_service, service)
                if error:
                    errors.append(error)
        else:
            mail_services = get_mail_services(runtime_env)
            if mail_services:
                provider = get_mail_provider_name(runtime_env)
                label = get_mail_provider_prompt(provider)
                error = _verification_error(label, _verify_mail_provider, provider)
                if error:
                    errors.append(error)

    if _changed(cpa_keys) and sync_states.get("cpa") and all(cpa_values):
        error = _verification_error("CPA", _verify_cpa)
        if error:
            errors.append(error)
    if _changed(sub2api_keys) and sync_states.get("sub2api") and all(sub2api_values):
        error = _verification_error("Sub2API", _verify_sub2api)
        if error:
            errors.append(error)
    if errors:
        return JSONResponse(status_code=400, content={"message": "、".join(errors)})
    return None


def _save_runtime_config(data: dict[str, str]):
    """保存运行时配置到 .env，并在当前进程立即生效。"""
    import secrets as _secrets

    from autoteam.mail_provider import (
        get_default_mail_service,
        get_mail_service_legacy_env_values,
        normalize_mail_provider,
        normalize_mail_services,
        serialize_mail_services,
    )
    from autoteam.setup_wizard import REQUIRED_CONFIGS, _write_env

    env_keys = [key for key, _prompt, _default, _optional in REQUIRED_CONFIGS]
    existing = {key: os.environ.get(key, "") for key in env_keys}
    merged = {key: data.get(key, existing.get(key, "")) for key in env_keys}
    use_structured_mail_services = "mail_services" in data or "mail_service_default" in data
    normalized_services = normalize_mail_services(
        data.get("mail_services") if use_structured_mail_services else merged.get("MAIL_SERVICES_JSON")
    )

    default_service_id = ""
    if normalized_services:
        requested_default = str(
            data.get("mail_service_default")
            if use_structured_mail_services
            else merged.get("MAIL_SERVICE_DEFAULT") or existing.get("MAIL_SERVICE_DEFAULT") or ""
        ).strip()
        service_ids = {str(item.get("id") or "") for item in normalized_services}
        default_service_id = (
            requested_default if requested_default in service_ids else str(normalized_services[0].get("id") or "")
        )

    if use_structured_mail_services or normalized_services:
        merged["MAIL_SERVICES_JSON"] = serialize_mail_services(normalized_services)
        merged["MAIL_SERVICE_DEFAULT"] = default_service_id

        default_service = get_default_mail_service(
            {
                **existing,
                **merged,
                "MAIL_SERVICES_JSON": merged["MAIL_SERVICES_JSON"],
                "MAIL_SERVICE_DEFAULT": default_service_id,
            },
            services=normalized_services,
        )
        if default_service:
            merged.update(get_mail_service_legacy_env_values(default_service))
        else:
            merged["MAIL_PROVIDER"] = ""
            merged["CLOUDMAIL_BASE_URL"] = ""
            merged["CLOUDMAIL_EMAIL"] = ""
            merged["CLOUDMAIL_PASSWORD"] = ""
            merged["CLOUDMAIL_DOMAIN"] = ""
            merged["CF_TEMP_EMAIL_BASE_URL"] = ""
            merged["CF_TEMP_EMAIL_ADMIN_PASSWORD"] = ""
            merged["CF_TEMP_EMAIL_DOMAIN"] = ""
    else:
        merged["MAIL_PROVIDER"] = normalize_mail_provider(merged.get("MAIL_PROVIDER") or existing.get("MAIL_PROVIDER"))

    if not merged.get("API_KEY"):
        merged["API_KEY"] = _secrets.token_urlsafe(24)

    missing = _validate_runtime_required_values(merged)
    if missing:
        return JSONResponse(
            status_code=400,
            content={"message": "缺少必填项: " + "、".join(missing)},
        )

    try:
        merged = _validate_runtime_optional_values(merged)
    except ValueError as exc:
        return JSONResponse(status_code=400, content={"message": str(exc)})

    previous_env = {key: os.environ.get(key) for key in env_keys}
    try:
        for key, value in merged.items():
            os.environ[key] = value
        _reload_runtime_config_modules()

        verify_result = _verify_runtime_integrations(previous_env, env=merged)
        if verify_result:
            _restore_runtime_env(previous_env)
            _reload_runtime_config_modules()
            return verify_result

        for key, value in merged.items():
            if value or key in _RUNTIME_CONFIG_CLEARABLE_FIELDS:
                _write_env(key, value)

        _sync_runtime_env_reload_state()
        _sync_runtime_globals()
        return {"message": "配置保存成功", "api_key": API_KEY, "configured": True}
    except Exception:
        _restore_runtime_env(previous_env)
        _reload_runtime_config_modules()
        raise


@app.get("/api/setup/status")
def get_setup_status():
    """检查配置是否完整"""
    from autoteam.setup_wizard import STARTUP_REQUIRED_CONFIGS

    return _collect_config_fields(configs=STARTUP_REQUIRED_CONFIGS)


@app.post("/api/setup/save")
def post_setup_save(config: SetupConfig):
    """保存配置到 .env 并验证连通性"""
    try:
        return _save_runtime_config(config.model_dump(exclude_unset=True))
    except Exception as exc:
        logger.exception("[配置] 初始配置保存失败")
        return JSONResponse(status_code=500, content={"message": f"配置保存失败: {exc}"})


@app.get("/api/config/runtime")
def get_runtime_config():
    """获取当前运行时配置，供登录后的设置面板编辑。"""
    return _collect_config_fields(include_values=True)


@app.get("/api/config/source")
def get_runtime_config_source():
    """获取 .env 源文件内容。"""
    content, path = _read_runtime_source_text()
    return {"path": path, "content": content}


@app.put("/api/config/runtime")
def put_runtime_config(config: SetupConfig):
    """登录后修改 CloudMail / CPA / Sub2API / 代理等运行时配置。"""
    try:
        return _save_runtime_config(config.model_dump(exclude_unset=True))
    except Exception as exc:
        logger.exception("[配置] 运行时配置保存失败")
        return JSONResponse(status_code=500, content={"message": f"配置保存失败: {exc}"})


@app.put("/api/config/source")
def put_runtime_config_source(config: SourceConfig):
    """保存 .env 源文件内容，并立即应用到运行时。"""
    env_keys = list(_ALL_RUNTIME_ENV_KEYS)
    previous_env = {key: os.environ.get(key) for key in env_keys}
    source_path = None
    previous_exists = False
    previous_content = ""

    try:
        current_content, source_path = _read_runtime_source_text()
        previous_content = current_content
        from autoteam.setup_wizard import ENV_FILE

        previous_exists = ENV_FILE.exists()

        _write_runtime_source_text(config.content)

        loaded_values = _load_env_values_from_source(config.content, env_keys)
        missing = _validate_runtime_required_values(loaded_values)
        if missing:
            _restore_runtime_source_text(previous_exists, previous_content)
            _restore_runtime_env(previous_env)
            _reload_runtime_config_modules()
            return JSONResponse(status_code=400, content={"message": "缺少必填项: " + "、".join(missing)})

        try:
            loaded_values = _validate_runtime_optional_values(loaded_values)
        except ValueError as exc:
            _restore_runtime_source_text(previous_exists, previous_content)
            _restore_runtime_env(previous_env)
            _reload_runtime_config_modules()
            return JSONResponse(status_code=400, content={"message": str(exc)})

        for key in env_keys:
            if loaded_values.get(key):
                os.environ[key] = loaded_values[key]
            else:
                os.environ.pop(key, None)

        _reload_runtime_config_modules()
        verify_result = _verify_runtime_integrations(previous_env, env=loaded_values)
        if verify_result:
            _restore_runtime_source_text(previous_exists, previous_content)
            _restore_runtime_env(previous_env)
            _reload_runtime_config_modules()
            return verify_result

        _sync_runtime_env_reload_state()
        _sync_runtime_globals()
        return {
            "message": "源文件保存成功",
            "api_key": API_KEY,
            "configured": True,
            "path": source_path,
        }
    except Exception:
        _restore_runtime_source_text(previous_exists, previous_content)
        _restore_runtime_env(previous_env)
        _reload_runtime_config_modules()
        logger.exception("[配置] 源文件保存失败")
        return JSONResponse(status_code=500, content={"message": "源文件保存失败，请检查 .env 内容"})


def _safe_auto_check_config_response():
    cfg = dict(globals().get("_auto_check_config", {}))
    # swap_seat-only：target_seats 表示 ChatGPT seat / CPA OAuth active 保留数量，允许 1~5。
    active_limit = _normalize_swap_active_limit(cfg.get("target_seats", 2))
    cfg["target_seats"] = active_limit
    cfg["max_chatgpt_active"] = active_limit
    cfg["replace_mode"] = _normalize_auto_check_replace_mode(cfg.get("replace_mode", "pending_invite"))
    cfg["min_chatgpt_active"] = SWAP_ACTIVE_MIN
    cfg["max_allowed_chatgpt_active"] = SWAP_ACTIVE_MAX
    return cfg


@app.get("/api/config/auto-check")
def get_auto_check_config():
    """获取自动巡检配置；不会触发 Team API 或 CPA API。"""
    return _safe_auto_check_config_response()


@app.put("/api/config/auto-check")
def set_auto_check_config(config: AutoCheckConfigParams):
    """保存自动巡检配置；swap_seat-only 模式下 ChatGPT/OAuth active 保留数量允许 1~5。"""
    data = config.model_dump(exclude_unset=True)
    updates = {}

    if "interval" in data and data["interval"] is not None:
        interval = int(data["interval"])
        if interval <= 0:
            raise HTTPException(status_code=400, detail="AUTO_CHECK_INTERVAL 必须是正整数秒")
        updates["AUTO_CHECK_INTERVAL"] = str(interval)

    if "target_seats" in data and data["target_seats"] is not None:
        updates["AUTO_CHECK_TARGET_SEATS"] = str(_normalize_swap_active_limit(data["target_seats"]))
    if "replace_with_pending_invite" in data and data["replace_with_pending_invite"] is not None:
        updates["AUTO_CHECK_REPLACE_WITH_PENDING_INVITE"] = (
            "true" if data["replace_with_pending_invite"] else "false"
        )
    if "replace_mode" in data and data["replace_mode"] is not None:
        try:
            updates["AUTO_CHECK_REPLACE_MODE"] = _normalize_auto_check_replace_mode(data["replace_mode"])
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    if "threshold" in data and data["threshold"] is not None:
        updates["AUTO_CHECK_THRESHOLD"] = str(int(data["threshold"]))
    if "min_low" in data and data["min_low"] is not None:
        updates["AUTO_CHECK_MIN_LOW"] = str(int(data["min_low"]))
    if "retry_add_phone" in data and data["retry_add_phone"] is not None:
        updates["AUTO_CHECK_RETRY_ADD_PHONE"] = "true" if data["retry_add_phone"] else "false"
    if "add_phone_max_retries" in data and data["add_phone_max_retries"] is not None:
        retries = int(data["add_phone_max_retries"])
        if retries < 0:
            raise HTTPException(status_code=400, detail="AUTO_CHECK_ADD_PHONE_MAX_RETRIES 不能小于 0")
        updates["AUTO_CHECK_ADD_PHONE_MAX_RETRIES"] = str(retries)

    try:
        from autoteam.setup_wizard import _write_env

        for key, value in updates.items():
            os.environ[key] = value
            _write_env(key, value)
        _reload_runtime_config_modules()
        _sync_runtime_globals()
        _sync_runtime_env_reload_state()
    except Exception:
        raise

    if globals().get("_auto_check_restart") is not None:
        _auto_check_restart.set()
    return _safe_auto_check_config_response()


# ---------------------------------------------------------------------------
# 后台任务管理
# ---------------------------------------------------------------------------

_tasks: dict[str, dict] = {}
_playwright_lock = threading.Lock()
_playwright_lock_owner: str | None = None
_playwright_lock_owner_lock_id: int | None = None
_current_task_id: str | None = None
_admin_login_api = None
_admin_login_step: str | None = None
_main_codex_flow = None
_main_codex_step: str | None = None
_main_codex_action: str | None = None
_manual_account_flow = None
MAX_TASK_HISTORY = 50
_TASK_TERMINAL_STATUSES = {"completed", "failed", "cancelled"}


class TaskCancelledError(RuntimeError):
    """后台任务被用户请求终止。"""


def _get_task_cancel_message(task: dict | None) -> str:
    if not task:
        return "任务已终止"
    return task.get("cancel_message") or "任务已终止"


def is_task_cancel_requested(task_id: str | None = None) -> bool:
    task = _tasks.get(task_id or _current_task_id or "")
    return bool(task and task.get("cancel_requested"))


def ensure_current_task_not_cancelled(task_id: str | None = None):
    task = _tasks.get(task_id or _current_task_id or "")
    if task and task.get("cancel_requested"):
        raise TaskCancelledError(_get_task_cancel_message(task))


def _current_playwright_lock_owner() -> str | None:
    """Return the tracked owner only if it belongs to the current lock object."""
    if _playwright_lock_owner_lock_id != id(_playwright_lock):
        return None
    return _playwright_lock_owner


def _mark_playwright_lock_owner(owner: str) -> None:
    global _playwright_lock_owner, _playwright_lock_owner_lock_id
    _playwright_lock_owner = owner
    _playwright_lock_owner_lock_id = id(_playwright_lock)


def _acquire_playwright_lock(owner: str, *, blocking: bool = True) -> bool:
    acquired = _playwright_lock.acquire(blocking=blocking)
    if acquired:
        _mark_playwright_lock_owner(owner)
    return acquired


def _release_playwright_lock(owner: str | None = None) -> bool:
    """Release the shared browser/API lock without freeing another flow's lock."""
    global _playwright_lock_owner, _playwright_lock_owner_lock_id
    current_owner = _current_playwright_lock_owner()
    if owner is not None and current_owner not in (None, owner):
        logger.debug("[API] 跳过释放 Playwright 锁: owner=%s current=%s", owner, current_owner)
        return False
    if not _playwright_lock.locked():
        if owner is None or current_owner in (None, owner):
            _playwright_lock_owner = None
            _playwright_lock_owner_lock_id = None
        return False
    _playwright_lock.release()
    _playwright_lock_owner = None
    _playwright_lock_owner_lock_id = None
    return True


# ---------------------------------------------------------------------------
# Playwright 专用线程执行器（解决跨线程调用问题）
# ---------------------------------------------------------------------------

import queue as _queue


class _PlaywrightExecutor:
    """将 Playwright 操作派发到专用线程执行，避免跨线程错误"""

    def __init__(self):
        self._queue: _queue.Queue = _queue.Queue()
        self._thread: threading.Thread | None = None
        self._broken_reason: str | None = None

    def _worker(self):
        while True:
            item = self._queue.get()
            if item is None:
                break
            func, args, kwargs, result_event, result_holder = item
            try:
                result_holder["result"] = func(*args, **kwargs)
            except Exception as e:
                result_holder["error"] = e
            finally:
                result_event.set()

    def ensure_started(self):
        if self._broken_reason:
            raise RuntimeError(self._broken_reason)
        if self._thread is None or not self._thread.is_alive():
            self._thread = threading.Thread(target=self._worker, daemon=True)
            self._thread.start()

    def run(self, func, *args, timeout_seconds=300, **kwargs):
        """在专用线程中执行函数，阻塞等待结果"""
        self.ensure_started()
        result_event = threading.Event()
        result_holder: dict = {}
        self._queue.put((func, args, kwargs, result_event, result_holder))
        if not result_event.wait(timeout=max(1, timeout_seconds)):
            func_name = getattr(func, "__name__", repr(func))
            self._broken_reason = (
                f"Playwright 专用线程执行超时（>{timeout_seconds}s）: {func_name}；"
                "为避免浏览器进程继续堆积，已拒绝后续专用线程任务，请重启服务"
            )
            logger.error("[API] %s", self._broken_reason)
            raise TimeoutError(self._broken_reason)
        if "error" in result_holder:
            raise result_holder["error"]
        return result_holder.get("result")

    def stop(self):
        if self._thread and self._thread.is_alive():
            self._queue.put(None)
            self._thread.join(timeout=5)
            if self._thread.is_alive():
                logger.warning("[API] Playwright 专用线程在停止时仍未退出")
            else:
                self._thread = None
                self._queue = _queue.Queue()
                self._broken_reason = None


_pw_executor = _PlaywrightExecutor()


def _stop_playwright_resource(resource):
    if not resource:
        return

    stop = getattr(resource, "stop", None)
    if not callable(stop):
        return

    try:
        stop()
    except Exception:
        pass


def _run_playwright_start(factory, starter, *args, **kwargs):
    resource = factory()
    try:
        result = starter(resource, *args, **kwargs)
        return resource, result
    except Exception:
        _stop_playwright_resource(resource)
        raise


def _run_with_chatgpt_session(callback, team_context=None):
    from autoteam.chatgpt_api import ChatGPTTeamAPI

    chatgpt = ChatGPTTeamAPI()
    try:
        if team_context:
            chatgpt.start_with_session(
                getattr(team_context, "session_token", "") or "",
                getattr(team_context, "account_id", "") or "",
                getattr(team_context, "workspace_name", "") or "",
            )
        else:
            chatgpt.start()
        return callback(chatgpt)
    finally:
        chatgpt.stop()


def _current_busy_detail(default_message: str):
    if _admin_login_api:
        return {
            "message": default_message,
            "running_task": {
                "task_id": "admin-login",
                "command": "admin-login",
                "started_at": None,
            },
        }

    if _main_codex_flow:
        return {
            "message": default_message,
            "running_task": {
                "task_id": "main-codex-sync",
                "command": "main-codex-sync",
                "started_at": None,
            },
        }

    running = _tasks.get(_current_task_id, {})
    return {
        "message": default_message,
        "running_task": {
            "task_id": _current_task_id,
            "command": running.get("command", "unknown"),
            "started_at": running.get("started_at"),
        },
    }


def _prune_tasks():
    """保留最近 MAX_TASK_HISTORY 个任务"""
    if len(_tasks) <= MAX_TASK_HISTORY:
        return
    sorted_ids = sorted(_tasks, key=lambda k: _tasks[k]["created_at"])
    for tid in sorted_ids[: len(_tasks) - MAX_TASK_HISTORY]:
        if _tasks[tid]["status"] in _TASK_TERMINAL_STATUSES:
            del _tasks[tid]


def _run_task(task_id: str, func, *args, _lock_reserved: bool = False, **kwargs):
    """在后台线程中执行任务"""
    global _current_task_id
    task = _tasks[task_id]
    owner = f"task:{task_id}"

    if not _lock_reserved:
        _acquire_playwright_lock(owner, blocking=True)
    else:
        _mark_playwright_lock_owner(owner)
    _current_task_id = task_id
    task["started_at"] = time.time()
    task["status"] = "cancelling" if task.get("cancel_requested") else "running"

    try:
        ensure_current_task_not_cancelled(task_id)
        result = func(*args, **kwargs)
        task["status"] = "completed"
        task["result"] = result
    except TaskCancelledError as e:
        task["status"] = "cancelled"
        task["error"] = str(e)
        logger.warning("[API] 任务 %s 已终止: %s", task_id[:8], e)
    except Exception as e:
        task["status"] = "failed"
        task["error"] = str(e)
        logger.error("[API] 任务 %s 失败: %s", task_id[:8], e)
    finally:
        task["finished_at"] = time.time()
        _current_task_id = None
        _release_playwright_lock(owner)


def _start_task(command: str, func, params: dict, *args, **kwargs) -> dict:
    """创建并启动后台任务，返回任务信息"""
    task_id = uuid.uuid4().hex[:12]
    owner = f"task:{task_id}"
    if not _acquire_playwright_lock(owner, blocking=False):
        raise HTTPException(status_code=409, detail=_current_busy_detail("有任务正在执行，请等待完成后再试"))

    task = {
        "task_id": task_id,
        "command": command,
        "params": params,
        "status": "pending",
        "created_at": time.time(),
        "started_at": None,
        "finished_at": None,
        "result": None,
        "error": None,
        "cancel_requested": False,
        "cancel_requested_at": None,
        "cancel_message": "任务已终止",
    }
    try:
        _tasks[task_id] = task
        _prune_tasks()

        task_kwargs = {**kwargs, "_lock_reserved": True}
        thread = threading.Thread(target=_run_task, args=(task_id, func, *args), kwargs=task_kwargs, daemon=True)
        thread.start()
    except Exception:
        _tasks.pop(task_id, None)
        _release_playwright_lock(owner)
        raise

    return task


# ---------------------------------------------------------------------------
# 响应模型
# ---------------------------------------------------------------------------


class TaskParams(BaseModel):
    target: int = 5


class SwapSeatParams(BaseModel):
    max_chatgpt_active: int | None = None
    account_id: str | None = None


class PendingInviteConsumeParams(BaseModel):
    email: str | None = None
    account_id: str | None = None
    replace_mode: str | None = None
    max_chatgpt_active: int | None = None


class InviteAddParams(BaseModel):
    account_id: str | None = None
    max_chatgpt_active: int | None = None
    force_create_invite: bool = False
    invite_domains: str | None = None


class ClearPendingInvitesParams(BaseModel):
    account_id: str | None = None
    confirm: bool = False
    concurrency: int | None = None


class BulkInviteParams(BaseModel):
    account_id: str | None = None
    emails: str | list[str] = ""
    confirm: bool = False
    seat_type: str = "usage_based"
    concurrency: int | None = None
    batch_size: int | None = None
    resend_emails: bool = True


class ManageTeamsParams(BaseModel):
    max_chatgpt_active: int | None = None
    replace_with_pending_invite: bool | None = None
    replace_mode: str | None = None


class CleanupParams(BaseModel):
    max_seats: int | None = None


class AdminEmailParams(BaseModel):
    email: str


class AdminSessionParams(BaseModel):
    email: str
    session_token: str


class AdminPasswordParams(BaseModel):
    password: str


class AdminCodeParams(BaseModel):
    code: str


class AdminWorkspaceParams(BaseModel):
    option_id: str


class ManualAccountCallbackParams(BaseModel):
    redirect_url: str


class TeamMemberRemoveParams(BaseModel):
    email: str
    user_id: str
    type: str


class TeamMemberSeatParams(BaseModel):
    email: str
    user_id: str
    type: str
    seat_type: str


class CpaAuthStatusParams(BaseModel):
    name: str
    disabled: bool


class CpaAuthForgetParams(BaseModel):
    name: str


def _swap_seat_disabled(feature: str):
    raise HTTPException(
        status_code=410,
        detail=f"swap_seat-only 模式已禁用 {feature}；AutoTeam 只检查 CPA quota、切换 seat、启停 CPA OAuth，绝不 kick/remove Team member",
    )


def _normalized_email(value: str | None) -> str:
    return (value or "").strip().lower()


def _is_main_account_email(email: str | None) -> bool:
    from autoteam.admin_state import get_admin_email

    return bool(_normalized_email(email)) and _normalized_email(email) == _normalized_email(get_admin_email())


def _normalize_team_seat_type(value: str | None) -> str:
    """统一 seat type 命名。

    当前项目里邀请逻辑使用 `default` / `usage_based`，这里推断它们分别对应
    `chatgpt` / `codex`，方便前后端统一用更直观的标签。
    """
    raw = str(value or "").strip().lower()
    if raw in ("default", "chatgpt"):
        return "chatgpt"
    if raw in ("usage_based", "codex"):
        return "codex"
    return raw


def _team_seat_label(value: str | None) -> str:
    normalized = _normalize_team_seat_type(value)
    if normalized == "chatgpt":
        return "ChatGPT"
    if normalized == "codex":
        return "Codex"
    return normalized or "-"


def _team_seat_backend_value(value: str | None) -> str:
    normalized = _normalize_team_seat_type(value)
    if normalized == "chatgpt":
        return "default"
    if normalized == "codex":
        return "usage_based"
    raise HTTPException(status_code=400, detail=f"不支持的 seat_type: {value}")


def _quota_snapshot_status(quota_info: dict | None) -> str:
    if not isinstance(quota_info, dict):
        return ""

    values = []
    for key in ("primary_pct", "weekly_pct"):
        value = quota_info.get(key)
        if isinstance(value, (int, float)):
            values.append(value)

    if not values:
        return ""
    return "exhausted" if any(value >= 100 for value in values) else "active"


def _resolve_status_auth_file(acc: dict) -> str:
    auth_file = (acc.get("auth_file") or "").strip()
    if auth_file and Path(auth_file).exists():
        return auth_file

    if _is_main_account_email(acc.get("email")):
        from autoteam.codex_auth import get_saved_main_auth_file

        saved_auth_file = get_saved_main_auth_file()
        if saved_auth_file and Path(saved_auth_file).exists():
            return saved_auth_file

    return ""


def _display_account_status(acc: dict, quota_snapshot: dict | None = None) -> str:
    from autoteam.accounts import is_account_disabled

    status = acc.get("status", "")
    if not _is_main_account_email(acc.get("email")):
        if is_account_disabled(acc):
            return "disabled"
        return status

    quota_status = _quota_snapshot_status(quota_snapshot) or _quota_snapshot_status(acc.get("last_quota"))
    if quota_status:
        return quota_status

    return "active" if _resolve_status_auth_file(acc) else status


def _sanitize_account(acc: dict, quota_snapshot: dict | None = None) -> dict:
    """脱敏账号信息（去掉 password 等敏感字段）"""
    sanitized = {k: v for k, v in acc.items() if k not in ("password", "cloudmail_account_id", "mail_account_id")}
    sanitized["is_main_account"] = _is_main_account_email(acc.get("email"))
    sanitized["raw_status"] = acc.get("status", "")
    sanitized["status"] = _display_account_status(acc, quota_snapshot)
    return sanitized


def _admin_status():
    from autoteam.admin_state import get_admin_state_summary

    status = get_admin_state_summary()
    status["login_step"] = _admin_login_step
    status["login_in_progress"] = _admin_login_api is not None
    if _admin_login_api and _admin_login_step == "workspace_required":
        status["workspace_options"] = getattr(_admin_login_api, "workspace_options_cache", []) or []
    else:
        status["workspace_options"] = []
    return status


def _main_codex_status():
    return {
        "in_progress": _main_codex_flow is not None,
        "step": _main_codex_step,
        "action": _main_codex_action,
    }


def _manual_account_status():
    status = {
        "in_progress": False,
        "status": "idle",
        "state": "",
        "auth_url": "",
        "started_at": None,
        "message": "",
        "error": "",
        "account": None,
        "callback_received": False,
        "callback_source": "",
        "auto_callback_available": False,
        "auto_callback_error": "",
    }
    if _manual_account_flow:
        status.update(_manual_account_flow.status())
    return status


def _finish_admin_login(completed: dict):
    global _admin_login_api, _admin_login_step
    api = _admin_login_api
    info = None
    try:
        info = _pw_executor.run(api.complete_admin_login)
    finally:
        if api:
            try:
                _pw_executor.run(api.stop)
            except Exception:
                pass
        _admin_login_api = None
        _admin_login_step = None
        _release_playwright_lock("admin-login")
    return {"status": "completed", "admin": _admin_status(), "codex": _main_codex_status(), "info": info}


def _set_pending_admin_login(api, step):
    global _admin_login_api, _admin_login_step
    _admin_login_api = api
    _admin_login_step = step
    return {"status": step, "admin": _admin_status()}


def _finish_main_codex_flow():
    global _main_codex_flow, _main_codex_step, _main_codex_action
    flow = _main_codex_flow
    action = _main_codex_action or "sync"
    try:
        info = _pw_executor.run(flow.complete)
    finally:
        if flow:
            try:
                _pw_executor.run(flow.stop)
            except Exception:
                pass
        _main_codex_flow = None
        _main_codex_step = None
        _main_codex_action = None
        _release_playwright_lock("main-codex")

    message = "主号 Codex 已同步到已启用远端" if action == "sync" else "主号 Codex 已登录"
    return {
        "status": "completed",
        "message": message,
        "codex": _main_codex_status(),
        "info": info,
    }


def _set_pending_main_codex_flow(flow, step, action):
    global _main_codex_flow, _main_codex_step, _main_codex_action
    _main_codex_flow = flow
    _main_codex_step = step
    _main_codex_action = action
    return {"status": step, "codex": _main_codex_status()}


def _start_main_codex_flow(action="sync"):
    from autoteam.codex_auth import MainCodexLoginFlow, MainCodexSyncFlow

    flow_cls = MainCodexSyncFlow if action == "sync" else MainCodexLoginFlow

    def _do_start():
        return _run_playwright_start(flow_cls, lambda flow: flow.start())

    flow, result = _pw_executor.run(_do_start)
    step = result["step"]
    if step == "completed":
        _set_pending_main_codex_flow(flow, step, action)
        return step, _finish_main_codex_flow()
    if step in ("password_required", "code_required"):
        return step, _set_pending_main_codex_flow(flow, step, action)

    _pw_executor.run(flow.stop)
    raise RuntimeError(result.get("detail") or "无法识别主号 Codex 登录步骤")


def _finish_manual_account_flow(result: dict):
    return {**result, "manual_account": _manual_account_status()}


def _set_pending_manual_account_flow(flow, result):
    global _manual_account_flow
    _manual_account_flow = flow
    return {**result, "manual_account": _manual_account_status()}


# ---------------------------------------------------------------------------
# 同步端点
# ---------------------------------------------------------------------------


@app.get("/api/admin/status")
def get_admin_status():
    """获取管理员登录状态。"""
    return _admin_status()


@app.get("/api/main-codex/status")
def get_main_codex_status():
    """获取主号 Codex 同步状态。"""
    return _main_codex_status()


@app.get("/api/manual-account/status")
def get_manual_account_status():
    """获取手动添加账号状态。"""
    return _manual_account_status()


@app.post("/api/admin/login/start")
def post_admin_login_start(params: AdminEmailParams):
    """开始管理员登录流程。"""
    global _admin_login_api, _admin_login_step

    if _admin_login_api:
        try:
            _pw_executor.run(_admin_login_api.stop)
        except Exception:
            pass
        _admin_login_api = None
        _admin_login_step = None
        _release_playwright_lock("admin-login")

    if not _acquire_playwright_lock("admin-login", blocking=False):
        raise HTTPException(
            status_code=409, detail=_current_busy_detail("有任务正在执行，请等待完成后再进行管理员登录")
        )

    try:
        from autoteam.chatgpt_api import ChatGPTTeamAPI

        logger.info("[API] 开始管理员登录: %s", params.email.strip())

        def _do_start(email):
            return _run_playwright_start(
                ChatGPTTeamAPI, lambda api, login_email: api.begin_admin_login(login_email), email
            )

        api, result = _pw_executor.run(_do_start, params.email.strip())
        step = result["step"]
        logger.info("[API] 管理员登录 start 返回: step=%s detail=%s", step, result.get("detail"))
        if step == "completed":
            _admin_login_api = api
            return _finish_admin_login(result)
        if step in ("password_required", "code_required", "workspace_required"):
            return _set_pending_admin_login(api, step)
        _pw_executor.run(api.stop)
        _release_playwright_lock("admin-login")
        raise HTTPException(status_code=400, detail=result.get("detail") or "无法识别管理员登录步骤")
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("[API] 管理员登录 start 失败")
        _release_playwright_lock("admin-login")
        raise HTTPException(status_code=400, detail=str(exc))


@app.post("/api/admin/login/session")
def post_admin_login_session(params: AdminSessionParams):
    """手动导入管理员 session_token。"""
    global _admin_login_api, _admin_login_step

    if _admin_login_api:
        post_admin_login_cancel()

    if not _acquire_playwright_lock("admin-session-import", blocking=False):
        raise HTTPException(
            status_code=409,
            detail=_current_busy_detail("有任务正在执行，请等待完成后再导入管理员 session_token"),
        )

    try:
        from autoteam.chatgpt_api import ChatGPTTeamAPI

        logger.info("[API] 导入管理员 session_token: %s", params.email.strip())

        def _do_import(email, session_token):
            api = ChatGPTTeamAPI()
            try:
                return api.import_admin_session(email, session_token)
            finally:
                api.stop()

        session_token = parse_chatgpt_session_token(params.session_token)
        if not session_token:
            raise HTTPException(
                status_code=400,
                detail="未从输入内容解析到 ChatGPT session_token。请粘贴纯 token，或包含 __Secure-next-auth.session-token 的 Cookie/JSON。",
            )

        info = _pw_executor.run(_do_import, params.email.strip(), session_token)
        _admin_login_api = None
        _admin_login_step = None
        return {"status": "completed", "admin": _admin_status(), "codex": _main_codex_status(), "info": info}
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("[API] 导入管理员 session_token 失败")
        raise HTTPException(status_code=400, detail=str(exc))
    finally:
        _release_playwright_lock("admin-session-import")


@app.post("/api/admin/login/password")
def post_admin_login_password(params: AdminPasswordParams):
    """提交管理员密码。"""
    global _admin_login_api, _admin_login_step
    if not _admin_login_api or _admin_login_step != "password_required":
        raise HTTPException(status_code=409, detail="当前没有等待密码的管理员登录流程")

    try:
        logger.info("[API] 提交管理员密码 | current_step=%s", _admin_login_step)
        result = _pw_executor.run(_admin_login_api.submit_admin_password, params.password)
        step = result["step"]
        logger.info("[API] 管理员密码提交返回: step=%s detail=%s", step, result.get("detail"))
        if step == "completed":
            return _finish_admin_login(result)
        if step in ("password_required", "code_required", "workspace_required"):
            _admin_login_step = step
            return {"status": step, "admin": _admin_status()}
        raise HTTPException(status_code=400, detail=result.get("detail") or "管理员密码登录失败")
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("[API] 管理员密码提交失败")
        try:
            _pw_executor.run(_admin_login_api.stop)
        except Exception:
            pass
        _admin_login_api = None
        _admin_login_step = None
        _release_playwright_lock("admin-login")
        raise HTTPException(status_code=400, detail=str(exc))


@app.post("/api/admin/login/code")
def post_admin_login_code(params: AdminCodeParams):
    """提交管理员验证码。"""
    global _admin_login_api, _admin_login_step
    if not _admin_login_api or _admin_login_step != "code_required":
        raise HTTPException(status_code=409, detail="当前没有等待验证码的管理员登录流程")

    try:
        logger.info("[API] 提交管理员验证码 | current_step=%s code_len=%d", _admin_login_step, len(params.code.strip()))
        result = _pw_executor.run(_admin_login_api.submit_admin_code, params.code.strip())
        step = result["step"]
        logger.info("[API] 管理员验证码提交返回: step=%s detail=%s", step, result.get("detail"))
        if step == "completed":
            return _finish_admin_login(result)
        if step in ("password_required", "code_required", "workspace_required"):
            _admin_login_step = step
            return {"status": step, "admin": _admin_status()}
        raise HTTPException(status_code=400, detail=result.get("detail") or "管理员验证码登录失败")
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("[API] 管理员验证码提交失败")
        try:
            _pw_executor.run(_admin_login_api.stop)
        except Exception:
            pass
        _admin_login_api = None
        _admin_login_step = None
        _release_playwright_lock("admin-login")
        raise HTTPException(status_code=400, detail=str(exc))


@app.post("/api/admin/login/workspace")
def post_admin_login_workspace(params: AdminWorkspaceParams):
    """提交管理员 workspace 选择。"""
    global _admin_login_api, _admin_login_step
    if not _admin_login_api or _admin_login_step != "workspace_required":
        raise HTTPException(status_code=409, detail="当前没有等待组织选择的管理员登录流程")

    try:
        logger.info("[API] 提交管理员 workspace 选择 | option_id=%s", params.option_id)
        result = _pw_executor.run(_admin_login_api.select_workspace_option, params.option_id)
        step = result["step"]
        logger.info("[API] 管理员 workspace 选择返回: step=%s detail=%s", step, result.get("detail"))
        if step == "completed":
            return _finish_admin_login(result)
        if step in ("password_required", "code_required", "workspace_required"):
            _admin_login_step = step
            return {"status": step, "admin": _admin_status()}
        raise HTTPException(status_code=400, detail=result.get("detail") or "管理员组织选择失败")
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("[API] 管理员 workspace 选择失败")
        try:
            _pw_executor.run(_admin_login_api.stop)
        except Exception:
            pass
        _admin_login_api = None
        _admin_login_step = None
        _release_playwright_lock("admin-login")
        raise HTTPException(status_code=400, detail=str(exc))


@app.post("/api/admin/login/cancel")
def post_admin_login_cancel():
    """取消管理员登录流程。"""
    global _admin_login_api, _admin_login_step
    if _admin_login_api:
        try:
            _pw_executor.run(_admin_login_api.stop)
        except Exception:
            pass
        _admin_login_api = None
        _admin_login_step = None
        _release_playwright_lock("admin-login")
    return {"message": "管理员登录已取消", "admin": _admin_status()}


@app.post("/api/admin/logout")
def post_admin_logout():
    """清除已保存的管理员登录态。"""
    from autoteam.admin_state import clear_admin_state

    if _admin_login_api:
        post_admin_login_cancel()
    clear_admin_state()
    return {"message": "管理员登录态已清除", "admin": _admin_status()}


@app.post("/api/main-codex/start")
def post_main_codex_start():
    """swap_seat-only：主号 OAuth 由 CPA 管理，AutoTeam 不再同步。"""
    _swap_seat_disabled("主号 Codex 同步")


@app.post("/api/main-codex/login")
def post_main_codex_login():
    """swap_seat-only：主号 OAuth 由 CPA 管理，AutoTeam 不再登录保存。"""
    _swap_seat_disabled("主号 Codex 登录")


@app.post("/api/main-codex/password")
def post_main_codex_password(params: AdminPasswordParams):
    """swap_seat-only：主号 OAuth 由 CPA 管理。"""
    _swap_seat_disabled("主号 Codex 密码提交")


@app.post("/api/main-codex/code")
def post_main_codex_code(params: AdminCodeParams):
    """swap_seat-only：主号 OAuth 由 CPA 管理。"""
    _swap_seat_disabled("主号 Codex 验证码提交")


@app.post("/api/main-codex/cancel")
def post_main_codex_cancel():
    """取消历史主号 Codex 登录流程（只做本地清理，不新建登录）。"""
    global _main_codex_flow, _main_codex_step, _main_codex_action
    if _main_codex_flow:
        try:
            _pw_executor.run(_main_codex_flow.stop)
        except Exception:
            pass
        _main_codex_flow = None
        _main_codex_step = None
        _main_codex_action = None
        _release_playwright_lock("main-codex")
    return {"message": "主号 Codex 登录已取消", "codex": _main_codex_status()}


def _delete_main_codex_from_enabled_targets():
    from autoteam.sync_targets import (
        delete_main_codex_from_configured_targets,
        describe_sync_targets,
        get_enabled_sync_targets,
    )

    _require_sync_target_configs("删除主号 Codex 远端文件")

    env = _current_runtime_env()
    enabled_targets = get_enabled_sync_targets(env)
    results = delete_main_codex_from_configured_targets()
    deleted: list[str] = []
    total_count = 0
    failed_targets: list[str] = []

    for target in enabled_targets:
        target_result = results.get(target) or {}
        target_deleted = [str(item) for item in (target_result.get("deleted") or [])]
        deleted.extend(target_deleted)
        if str(target_result.get("error") or "").strip():
            failed_targets.append(target)

        try:
            target_count = int(target_result.get("count", len(target_deleted)) or 0)
        except (TypeError, ValueError):
            target_count = len(target_deleted)
        total_count += target_count

    target_label = describe_sync_targets(enabled_targets)
    message = f"已从 {target_label} 删除 {total_count} 个主号认证文件"
    if failed_targets:
        message += f"（{describe_sync_targets(failed_targets)} 清理失败，详情见 results）"
    return {
        "message": message,
        "deleted": deleted,
        "results": results,
    }


@app.post("/api/main-codex/delete-remote-files")
def post_main_codex_delete_remote_files():
    """swap_seat-only：不再删除 CPA/远端 OAuth 文件，禁用请用 CPA status。"""
    _swap_seat_disabled("删除主号远端 OAuth 文件")


@app.post("/api/main-codex/delete-cpa")
def post_main_codex_delete_cpa():
    """swap_seat-only：不再删除 CPA OAuth 文件，禁用请用 CPA status。"""
    _swap_seat_disabled("删除主号 CPA OAuth 文件")


@app.post("/api/manual-account/start")
def post_manual_account_start():
    """swap_seat-only：OAuth 添加由 CPA 管理，AutoTeam 不再生成登录链接。"""
    _swap_seat_disabled("手动 OAuth 添加账号")


@app.post("/api/manual-account/callback")
def post_manual_account_callback(params: ManualAccountCallbackParams):
    """swap_seat-only：OAuth 添加由 CPA 管理。"""
    _swap_seat_disabled("手动 OAuth 回调")


@app.post("/api/manual-account/cancel")
def post_manual_account_cancel():
    """取消手动添加账号流程。"""
    global _manual_account_flow
    if _manual_account_flow:
        try:
            _manual_account_flow.stop()
        except Exception:
            pass
        _manual_account_flow = None
    return {"message": "手动添加账号流程已取消", "manual_account": _manual_account_status()}


@app.get("/api/accounts")
def get_accounts():
    """获取所有账号列表"""
    from autoteam.accounts import load_accounts

    accounts = load_accounts()
    return [_sanitize_account(a) for a in accounts]


@app.get("/api/accounts/{email}/codex-auth")
def get_codex_auth(email: str):
    """swap_seat-only：本地 Codex auth 导出已禁用，OAuth/auth 由 CPA 管理。"""
    _swap_seat_disabled("本地 Codex auth 导出")


@app.get("/api/accounts/active")
def get_active():
    """获取活跃账号"""
    from autoteam.accounts import get_active_accounts

    return [_sanitize_account(a) for a in get_active_accounts()]


@app.get("/api/accounts/standby")
def get_standby():
    """获取待命账号"""
    from autoteam.accounts import get_standby_accounts

    accounts = get_standby_accounts()
    return [_sanitize_account(a) for a in accounts]


@app.delete("/api/accounts/{email}")
def delete_account(email: str):
    """swap_seat-only：禁止删除账号/移除 Team member。"""
    raise HTTPException(status_code=410, detail="swap_seat-only 模式已禁用账号删除；不会 kick/remove Team member")


def _toggle_account_disabled(email: str, disabled: bool):
    from autoteam.accounts import find_account, load_accounts, update_account

    email = email.strip().lower()
    if _is_main_account_email(email):
        raise HTTPException(status_code=400, detail="主号不允许禁用或启用")

    accounts = load_accounts()
    acc = find_account(accounts, email)
    if not acc:
        raise HTTPException(status_code=404, detail="账号不存在")

    update_account(email, disabled=disabled)
    refreshed = find_account(load_accounts(), email)
    return {
        "message": f"已{'禁用' if disabled else '启用'} {email}",
        "email": email,
        "disabled": bool(disabled),
        "account": _sanitize_account(refreshed or {**acc, "disabled": disabled}),
    }


def _toggle_accounts_disabled(emails: list[str], disabled: bool):
    from autoteam.accounts import is_account_disabled, load_accounts, save_accounts

    normalized_emails = []
    seen = set()
    for value in emails or []:
        email = _normalized_email(value)
        if not email or email in seen:
            continue
        seen.add(email)
        normalized_emails.append(email)

    if not normalized_emails:
        raise HTTPException(status_code=400, detail="请至少提供一个有效邮箱")

    accounts = load_accounts()
    by_email = {(_normalized_email(acc.get("email"))): acc for acc in accounts if acc.get("email")}

    updated = []
    unchanged = []
    missing = []
    skipped_main = []

    for email in normalized_emails:
        if _is_main_account_email(email):
            skipped_main.append(email)
            continue
        acc = by_email.get(email)
        if not acc:
            missing.append(email)
            continue
        if is_account_disabled(acc) == bool(disabled):
            unchanged.append(email)
            continue
        acc["disabled"] = bool(disabled)
        updated.append(email)

    if updated:
        save_accounts(accounts)
        accounts = load_accounts()
        by_email = {(_normalized_email(acc.get("email"))): acc for acc in accounts if acc.get("email")}

    action = "禁用" if disabled else "启用"
    parts = [f"已{action} {len(updated)} 个账号"]
    if unchanged:
        parts.append(f"{len(unchanged)} 个已是目标状态")
    if skipped_main:
        parts.append(f"跳过主号 {len(skipped_main)} 个")
    if missing:
        parts.append(f"未找到 {len(missing)} 个")

    return {
        "message": "，".join(parts),
        "disabled": bool(disabled),
        "updated_count": len(updated),
        "updated_emails": updated,
        "unchanged_emails": unchanged,
        "skipped_main_accounts": skipped_main,
        "missing_emails": missing,
        "accounts": [_sanitize_account(by_email[email]) for email in updated if email in by_email],
    }


class BulkAccountDisableParams(BaseModel):
    emails: list[str]


@app.post("/api/accounts/bulk/disable")
def post_bulk_disable_accounts(params: BulkAccountDisableParams):
    """swap_seat-only：本地账号池禁用已废弃，OAuth 启停由 CPA 管理。"""
    _swap_seat_disabled("本地账号批量禁用")


@app.post("/api/accounts/bulk/enable")
def post_bulk_enable_accounts(params: BulkAccountDisableParams):
    """swap_seat-only：本地账号池启用已废弃，OAuth 启停由 CPA 管理。"""
    _swap_seat_disabled("本地账号批量启用")


@app.post("/api/accounts/{email}/disable")
def post_disable_account(email: str):
    """swap_seat-only：本地账号池禁用已废弃，OAuth 启停由 CPA 管理。"""
    _swap_seat_disabled("本地账号禁用")


@app.post("/api/accounts/{email}/enable")
def post_enable_account(email: str):
    """swap_seat-only：本地账号池启用已废弃，OAuth 启停由 CPA 管理。"""
    _swap_seat_disabled("本地账号启用")


@app.post("/api/accounts/{email}/kick")
def post_kick_account(email: str):
    """swap_seat 模式下禁止 kick。"""
    raise HTTPException(status_code=410, detail="swap_seat-only 模式已禁用 kick/remove；请运行 swap_seat 统一收敛 seat")


class LoginAccountParams(BaseModel):
    email: str


@app.post("/api/accounts/login", status_code=202)
def post_account_login(params: LoginAccountParams):
    """swap_seat-only：OAuth/auth 由 CPA 管理，AutoTeam 不再登录账号。"""
    _swap_seat_disabled("账号 OAuth 登录")


@app.get("/api/status")
def get_status():
    """只读本地状态摘要；实时 quota 由 swap_seat 通过 CPA 检查。"""
    from autoteam.accounts import (
        STATUS_ACTIVE,
        STATUS_AUTH_PENDING,
        STATUS_EXHAUSTED,
        STATUS_PENDING,
        STATUS_STANDBY,
        load_accounts,
    )

    accounts = load_accounts()
    quota_cache = {}
    sanitized_accounts = [_sanitize_account(a) for a in accounts]

    summary = {
        "active": sum(1 for a in sanitized_accounts if a["status"] == STATUS_ACTIVE),
        "auth_pending": sum(1 for a in sanitized_accounts if a["status"] == STATUS_AUTH_PENDING),
        "standby": sum(1 for a in sanitized_accounts if a["status"] == STATUS_STANDBY),
        "exhausted": sum(1 for a in sanitized_accounts if a["status"] == STATUS_EXHAUSTED),
        "pending": sum(1 for a in sanitized_accounts if a["status"] == STATUS_PENDING),
        "disabled": sum(1 for a in sanitized_accounts if a["status"] == "disabled"),
        "total": len(sanitized_accounts),
    }

    return {
        "accounts": sanitized_accounts,
        "summary": summary,
        "quota_cache": quota_cache,
        "note": "swap_seat-only：实时 5h/weekly quota 只在 swap_seat 任务中通过 CPA /api-call 检查",
    }


@app.post("/api/sync")
def post_sync():
    """swap_seat-only：不再把本地 auth 同步到远端。"""
    _swap_seat_disabled("本地 auth 同步")


@app.post("/api/sync/from-cpa")
def post_sync_from_cpa():
    """swap_seat-only：不再把 CPA OAuth 拉回本地。"""
    _swap_seat_disabled("CPA 反向同步到本地")


@app.post("/api/sync/accounts")
def post_sync_accounts():
    """swap_seat-only：不再维护本地账号池。"""
    _swap_seat_disabled("本地账号池同步")


@app.get("/api/team/members")
def get_team_members(account_id: str | None = None):
    """获取 Team 全部成员（包括手动添加的外部成员）"""
    from autoteam.admin_state import get_admin_session_token, get_chatgpt_account_id
    from autoteam.team_context import get_team_context

    active_limit = _normalize_swap_active_limit(_auto_check_config.get("target_seats", 2))
    team_context = get_team_context(account_id, default_max_chatgpt_active=active_limit)
    if account_id and not team_context:
        raise HTTPException(status_code=404, detail=f"未找到 Team 配置: {account_id}")
    session_present = bool(getattr(team_context, "session_token", "") if team_context else get_admin_session_token())
    resolved_account_id = str(getattr(team_context, "account_id", "") if team_context else get_chatgpt_account_id()).strip()

    if not session_present or not resolved_account_id:
        raise HTTPException(status_code=400, detail="请先完成管理员登录")

    if not _acquire_playwright_lock("team-members", blocking=False):
        raise HTTPException(status_code=409, detail=_current_busy_detail("有任务正在执行，请等待完成后再查询"))

    try:

        def _fetch_team_members():
            from autoteam.account_ops import fetch_team_state
            from autoteam.accounts import load_accounts

            def _collect(chatgpt):
                try:
                    members, invites = fetch_team_state(chatgpt, account_id=resolved_account_id)
                except TypeError:
                    members, invites = fetch_team_state(chatgpt)
                local_emails = {a["email"].lower() for a in load_accounts()}
                managed_emails = set(local_emails)
                try:
                    from autoteam.cpa_sync import (
                        get_managed_cpa_auth_names,
                        is_cpa_codex_oauth,
                        is_managed_cpa_auth,
                        list_cpa_files,
                    )

                    managed_names = get_managed_cpa_auth_names()
                    for auth in list_cpa_files():
                        if not is_cpa_codex_oauth(auth) or not is_managed_cpa_auth(auth, managed_names):
                            continue
                        email = (auth.get("email") or auth.get("account") or "").strip().lower()
                        if email:
                            managed_emails.add(email)
                except Exception:
                    logger.debug("[API] 读取受管 CPA auth 邮箱失败，Team 成员仅按本地账号池标记", exc_info=True)

                result = []
                for m in members:
                    email = (m.get("email") or "").lower()
                    raw_seat_type = m.get("seat_type", "")
                    result.append(
                        {
                            "email": m.get("email", ""),
                            "role": m.get("role", ""),
                            "user_id": m.get("user_id") or m.get("id", ""),
                            "is_local": email in managed_emails,
                            "type": "member",
                            "seat_type": _normalize_team_seat_type(raw_seat_type),
                            "seat_type_raw": raw_seat_type,
                            "seat_type_label": _team_seat_label(raw_seat_type),
                        }
                    )
                for inv in invites:
                    email = (inv.get("email_address") or inv.get("email") or "").lower()
                    raw_seat_type = inv.get("seat_type", "")
                    result.append(
                        {
                            "email": email,
                            "role": inv.get("role", ""),
                            "user_id": inv.get("id", ""),
                            "is_local": email in managed_emails,
                            "type": "invite",
                            "seat_type": _normalize_team_seat_type(raw_seat_type),
                            "seat_type_raw": raw_seat_type,
                            "seat_type_label": _team_seat_label(raw_seat_type),
                        }
                    )
                return {
                    "members": result,
                    "total": len(members),
                    "invites": len(invites),
                    "team": team_context.public_dict() if team_context else {"account_id": resolved_account_id},
                }

            try:
                return _run_with_chatgpt_session(_collect, team_context=team_context)
            except TypeError:
                return _run_with_chatgpt_session(_collect)

        try:
            return _pw_executor.run(_fetch_team_members)
        except HTTPException:
            raise
        except Exception as exc:
            logger.exception("[API] 获取 Team 成员失败")
            raise HTTPException(status_code=502, detail=str(exc))
    finally:
        _release_playwright_lock("team-members")


@app.post("/api/team/members/remove")
def post_team_member_remove(params: TeamMemberRemoveParams):
    """swap_seat-only：禁止移出 Team 成员，也禁止取消邀请。"""
    _swap_seat_disabled("Team member remove / invite cancel")


@app.post("/api/team/members/seat")
def post_team_member_seat(params: TeamMemberSeatParams):
    """swap_seat-only：禁止手动任意切 seat，避免绕过 2 个 ChatGPT/母号 Codex 策略。"""
    _swap_seat_disabled("手动 Team member seat 切换；请运行 /api/tasks/swap-seats")


# ---------------------------------------------------------------------------
# 日志收集
# ---------------------------------------------------------------------------

_log_buffer: list[dict] = []
_LOG_BUFFER_MAX = 500


class _LogCollector(logging.Handler):
    """收集日志到内存 buffer，供前端查询"""

    def emit(self, record):
        entry = {
            "time": record.created,
            "level": record.levelname,
            "message": self.format(record),
        }
        _log_buffer.append(entry)
        if len(_log_buffer) > _LOG_BUFFER_MAX:
            del _log_buffer[: len(_log_buffer) - _LOG_BUFFER_MAX]


_log_collector = _LogCollector()
_log_collector.setFormatter(logging.Formatter("%(message)s"))
logging.getLogger().addHandler(_log_collector)


@app.get("/api/logs")
def get_logs(limit: int = 100, since: float = 0):
    """获取最近的日志"""
    if since > 0:
        entries = [e for e in _log_buffer if e["time"] > since]
    else:
        entries = _log_buffer[-limit:]
    return {"logs": entries, "total": len(_log_buffer)}


@app.post("/api/sync/main-codex")
def post_sync_main_codex():
    """swap_seat-only：主号 OAuth 由 CPA 管理。"""
    _swap_seat_disabled("主号 Codex 同步")


@app.get("/api/cpa/files")
def get_cpa_files():
    """获取 AutoTeam 受管 CPA Codex auth 列表；未受管 auth 只统计不暴露为可操作资源。"""
    _require_cpa_configs("查看 CPA 文件")

    from autoteam.cpa_sync import get_managed_cpa_auth_names, is_cpa_codex_oauth, is_managed_cpa_auth, list_cpa_files

    files = list_cpa_files()
    managed_names = get_managed_cpa_auth_names()
    codex_files = [item for item in files if is_cpa_codex_oauth(item)]
    managed_files = [
        {
            **item,
            "autoteam_managed": True,
        }
        for item in codex_files
        if is_managed_cpa_auth(item, managed_names)
    ]

    return {
        "files": managed_files,
        "summary": {
            "total_cpa_files": len(files),
            "codex_total": len(codex_files),
            "managed": len(managed_files),
            "protected_unmanaged": max(0, len(codex_files) - len(managed_files)),
        },
    }


@app.patch("/api/cpa/auth/status")
def patch_cpa_auth_status(params: CpaAuthStatusParams):
    """手动禁用 CPA OAuth；启用必须由 swap_seat 按 quota/seat 保留数统一决策。"""
    _require_cpa_configs("启停 CPA OAuth")

    from autoteam.cpa_sync import is_managed_cpa_auth, set_cpa_auth_disabled

    if params.disabled is False:
        raise HTTPException(
            status_code=410,
            detail="swap_seat-only 模式禁止手动 enable CPA OAuth；请运行 swap_seat，由 CPA quota 和保留数自动选择 active OAuth",
        )
    if not is_managed_cpa_auth(params.name):
        raise HTTPException(
            status_code=403,
            detail="禁止修改未由 AutoTeam 创建/登记的 CPA auth；请只操作本项目生成的 auth 文件",
        )

    result = set_cpa_auth_disabled(params.name, params.disabled)
    return {
        "message": f"CPA OAuth {params.name} 已切到 disabled/standby",
        "name": params.name,
        "disabled": bool(params.disabled),
        "result": result,
    }


@app.post("/api/cpa/auth/forget")
def post_cpa_auth_forget(params: CpaAuthForgetParams):
    """删除并忘记 AutoTeam 受管 CPA auth；不会移除 Team member。"""
    _require_cpa_configs("清理 CPA OAuth")

    from autoteam.cpa_sync import (
        cpa_auth_name_candidates,
        delete_from_cpa,
        is_managed_cpa_auth,
        list_cpa_files,
        unregister_managed_cpa_auth,
    )
    from autoteam.swap_seat import forget_quota_cache_entries

    name = str(params.name or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="CPA auth name 不能为空")
    if not is_managed_cpa_auth(name):
        raise HTTPException(status_code=403, detail="禁止清理未由 AutoTeam 创建/登记的 CPA auth")

    target = None
    for item in list_cpa_files():
        if name in cpa_auth_name_candidates(item):
            target = item
            break

    email = ""
    if isinstance(target, dict):
        email = str(target.get("email") or target.get("account") or "").strip().lower()

    deleted = delete_from_cpa(name)
    unregistered = unregister_managed_cpa_auth(name)
    quota = forget_quota_cache_entries(auth_id=name, email=email)

    return {
        "message": f"已清理受管 CPA auth: {name}",
        "name": name,
        "email": email,
        "deleted_from_cpa": bool(deleted),
        "unregistered": bool(unregistered),
        "quota_cache": quota,
        "note": "未移除 Team member；外部/未受管 auth 不会被清理",
    }


@app.get("/api/teams")
def get_managed_teams():
    """列出将由多 Team 调度管理的 Team；不触发 Team/CPA API。"""
    from autoteam.team_context import team_contexts_public

    active_limit = _normalize_swap_active_limit(_auto_check_config.get("target_seats", 2))
    teams = team_contexts_public(default_max_chatgpt_active=active_limit)
    return {"teams": teams, "total": len(teams)}


@app.get("/api/swap/runtime-status")
def get_swap_runtime_status():
    """只读返回 swap 冷却与 quota 缓存；不触发 Team/CPA 外部 API。"""
    from autoteam.swap_seat import get_swap_cooldown_status, quota_cache_runtime_status
    from autoteam.team_context import get_team_contexts

    active_limit = _normalize_swap_active_limit(_auto_check_config.get("target_seats", 2))
    teams = get_team_contexts(include_disabled=True, default_max_chatgpt_active=active_limit)
    team_status = []
    for team in teams:
        team_status.append(
            {
                "team": team.public_dict(),
                "cooldown": get_swap_cooldown_status(scope=team.cooldown_scope),
            }
        )
    return {
        "teams": team_status,
        "quota_cache": quota_cache_runtime_status(),
    }


# ---------------------------------------------------------------------------
# 后台任务端点
# ---------------------------------------------------------------------------


@app.post("/api/tasks/check", status_code=202)
def post_check():
    """swap_seat-only：检查 CPA quota 并收敛 seat/OAuth（后台执行）"""
    _require_cpa_configs("检查并切换 seat")

    from autoteam.manager import cmd_swap_seats

    active_limit = _normalize_swap_active_limit(_auto_check_config.get("target_seats", 2))

    def _run():
        return cmd_swap_seats(max_chatgpt_active=active_limit)

    task = _start_task("swap-seats", _run, {"max_chatgpt_active": active_limit})
    return task


@app.post("/api/tasks/rotate", status_code=202)
def post_rotate(params: TaskParams = TaskParams()):
    """兼容旧入口：执行 swap_seat，不再 kick/补号/本地 auth 轮转。"""
    _require_cpa_configs("swap_seat")

    from autoteam.manager import cmd_swap_seats
    active_limit = _normalize_swap_active_limit(params.target)

    task = _start_task(
        "swap-seats",
        lambda _target: cmd_swap_seats(max_chatgpt_active=active_limit),
        {"target": params.target, "max_chatgpt_active": active_limit},
        active_limit,
    )
    return task


@app.post("/api/tasks/swap-seats", status_code=202)
def post_swap_seats(params: SwapSeatParams = SwapSeatParams()):
    """CPA-driven swap_seat：最多保留 N 个 ChatGPT seat + OAuth active。"""
    _require_cpa_configs("swap_seat")

    from autoteam.manager import cmd_swap_seats
    from autoteam.team_context import get_team_context

    default_limit = _normalize_swap_active_limit(_auto_check_config.get("target_seats", 2))
    requested_limit = (
        _normalize_swap_active_limit(params.max_chatgpt_active)
        if params.max_chatgpt_active is not None
        else default_limit
    )
    team_context = get_team_context(params.account_id, default_max_chatgpt_active=requested_limit) if params.account_id else None
    if params.account_id and not team_context:
        raise HTTPException(status_code=404, detail=f"未找到 Team 配置: {params.account_id}")
    max_chatgpt_active = (
        _normalize_swap_active_limit(params.max_chatgpt_active)
        if params.max_chatgpt_active is not None
        else _team_active_limit_or_default(team_context, default_limit)
    )
    task = _start_task(
        "swap-seats",
        cmd_swap_seats,
        {"max_chatgpt_active": max_chatgpt_active, "account_id": params.account_id or ""},
        max_chatgpt_active,
        team_context=team_context,
    )
    return task


@app.post("/api/tasks/auto-detect-replace", status_code=202)
def post_auto_detect_replace(params: PendingInviteConsumeParams = PendingInviteConsumeParams()):
    """手动触发：先检查 quota/swap；若 GPT seat 低于目标，则按模式补位。"""
    _require_cpa_configs("自动检测替换")
    from autoteam.mail_provider import MAIL_PROVIDER_CLOUDFLARE_TEMP_EMAIL
    from autoteam.manager import cmd_auto_detect_replace
    from autoteam.team_context import get_team_context

    _require_mail_provider_configs("自动检测替换", provider=MAIL_PROVIDER_CLOUDFLARE_TEMP_EMAIL)
    default_limit = _normalize_swap_active_limit(_auto_check_config.get("target_seats", 2))
    team_context = get_team_context(params.account_id, default_max_chatgpt_active=default_limit) if params.account_id else None
    if params.account_id and not team_context:
        raise HTTPException(status_code=404, detail=f"未找到 Team 配置: {params.account_id}")
    active_limit = _team_active_limit_or_default(team_context, default_limit)
    replace_mode = _resolve_auto_check_replace_mode(params.replace_mode)
    params_payload = {
        "max_chatgpt_active": active_limit,
        "email": params.email or "",
        "account_id": params.account_id or "",
        "replace_mode": replace_mode,
    }
    task = _start_task(
        "auto-detect-replace",
        cmd_auto_detect_replace,
        params_payload,
        active_limit,
        params.email,
        team_context,
        replace_mode=replace_mode,
    )
    return task


@app.post("/api/tasks/manage-teams", status_code=202)
def post_manage_teams(params: ManageTeamsParams = ManageTeamsParams()):
    """多 Team 巡检：每个 Team 独立 swap；低于目标时按模式补位。"""
    _require_cpa_configs("多 Team 自动调度")
    from autoteam.mail_provider import MAIL_PROVIDER_CLOUDFLARE_TEMP_EMAIL
    from autoteam.manager import cmd_manage_teams

    replace = (
        bool(params.replace_with_pending_invite)
        if params.replace_with_pending_invite is not None
        else bool(_auto_check_config.get("replace_with_pending_invite", False))
    )
    if replace:
        _require_mail_provider_configs("多 Team 自动替换", provider=MAIL_PROVIDER_CLOUDFLARE_TEMP_EMAIL)
    active_limit = _normalize_swap_active_limit(params.max_chatgpt_active or _auto_check_config.get("target_seats", 2))
    replace_mode = _resolve_auto_check_replace_mode(params.replace_mode)
    task = _start_task(
        "manage-teams",
        cmd_manage_teams,
        {"max_chatgpt_active": active_limit, "replace_with_pending_invite": replace, "replace_mode": replace_mode},
        active_limit,
        replace,
        replace_mode=replace_mode,
    )
    return task


@app.post("/api/tasks/add", status_code=202)
def post_add(params: PendingInviteConsumeParams = PendingInviteConsumeParams()):
    """消费 pending invite：只读检查目标 Team 后，用已有 CF 邮箱完成注册。"""
    from autoteam.mail_provider import MAIL_PROVIDER_CLOUDFLARE_TEMP_EMAIL
    from autoteam.manager import cmd_add
    from autoteam.team_context import get_team_context

    _require_cpa_configs("消费 pending invite 替换")
    _require_mail_provider_configs("注册新号", provider=MAIL_PROVIDER_CLOUDFLARE_TEMP_EMAIL)

    default_limit = _normalize_swap_active_limit(_auto_check_config.get("target_seats", 2))
    requested_limit = (
        _normalize_swap_active_limit(params.max_chatgpt_active)
        if params.max_chatgpt_active is not None
        else default_limit
    )
    team_context = get_team_context(params.account_id, default_max_chatgpt_active=requested_limit) if params.account_id else None
    if params.account_id and not team_context:
        raise HTTPException(status_code=404, detail=f"未找到 Team 配置: {params.account_id}")
    active_limit = (
        requested_limit
        if params.max_chatgpt_active is not None
        else _team_active_limit_or_default(team_context, default_limit)
    )
    task = _start_task(
        "consume-pending-invite",
        cmd_add,
        {"email": params.email or "", "account_id": params.account_id or "", "max_chatgpt_active": active_limit},
        params.email,
        active_limit,
        team_context,
    )
    return task


@app.post("/api/tasks/invite-add", status_code=202)
def post_invite_add(params: InviteAddParams = InviteAddParams()):
    """新增 invite：创建随机 CFMail 邮箱，发送 Team invite，注册并上传 PAT。"""
    from autoteam.mail_provider import MAIL_PROVIDER_CLOUDFLARE_TEMP_EMAIL
    from autoteam.manager import cmd_invite_add
    from autoteam.team_context import get_team_context

    _require_cpa_configs("新增 invite 注册")
    _require_mail_provider_configs("新增 invite 注册", provider=MAIL_PROVIDER_CLOUDFLARE_TEMP_EMAIL)
    if not params.force_create_invite:
        raise HTTPException(status_code=400, detail="新增 invite 会真实发送 Team invite；请传 force_create_invite=true 显式确认")

    default_limit = _normalize_swap_active_limit(_auto_check_config.get("target_seats", 2))
    requested_limit = (
        _normalize_swap_active_limit(params.max_chatgpt_active)
        if params.max_chatgpt_active is not None
        else default_limit
    )
    team_context = get_team_context(params.account_id, default_max_chatgpt_active=requested_limit) if params.account_id else None
    if params.account_id and not team_context:
        raise HTTPException(status_code=404, detail=f"未找到 Team 配置: {params.account_id}")
    active_limit = (
        _normalize_swap_active_limit(params.max_chatgpt_active)
        if params.max_chatgpt_active is not None
        else _team_active_limit_or_default(team_context, default_limit)
    )
    invite_domains = str(params.invite_domains or "").strip()
    task_params = {
        "account_id": params.account_id or "",
        "max_chatgpt_active": active_limit,
        "force_create_invite": True,
    }
    if invite_domains:
        task_params["invite_domains"] = invite_domains
    task_kwargs = {"force_create_invite": True}
    if invite_domains:
        task_kwargs["invite_domains"] = invite_domains
    task = _start_task(
        "create-invite",
        cmd_invite_add,
        task_params,
        active_limit,
        team_context,
        **task_kwargs,
    )
    return task


@app.post("/api/tasks/invites/clear", status_code=202)
def post_clear_pending_invites(params: ClearPendingInvitesParams = ClearPendingInvitesParams()):
    """显式清空目标 Team 的 pending invites；不移除任何 Team member。"""
    from autoteam.admin_state import get_admin_session_token, get_chatgpt_account_id
    from autoteam.manager import cmd_clear_pending_invites
    from autoteam.team_context import get_team_context

    if not params.confirm:
        raise HTTPException(status_code=400, detail="清空 pending invite 会取消所有未接受邀请；请传 confirm=true 显式确认")

    default_limit = _normalize_swap_active_limit(_auto_check_config.get("target_seats", 2))
    team_context = get_team_context(params.account_id, default_max_chatgpt_active=default_limit) if params.account_id else None
    if params.account_id and not team_context:
        raise HTTPException(status_code=404, detail=f"未找到 Team 配置: {params.account_id}")
    session_present = bool(getattr(team_context, "session_token", "") if team_context else get_admin_session_token())
    resolved_account_id = str(getattr(team_context, "account_id", "") if team_context else get_chatgpt_account_id()).strip()
    if not session_present or not resolved_account_id:
        raise HTTPException(status_code=400, detail="请先完成管理员登录")

    concurrency = max(1, min(8, int(params.concurrency or 4)))
    task = _start_task(
        "clear-pending-invites",
        cmd_clear_pending_invites,
        {"account_id": params.account_id or "", "concurrency": concurrency, "confirm": True},
        team_context=team_context,
        concurrency=concurrency,
    )
    return task


@app.post("/api/tasks/invites/bulk", status_code=202)
def post_bulk_invite(params: BulkInviteParams):
    """并发批量发送 Team invites；只发送邀请，不注册账号/不上传 PAT。"""
    from autoteam.admin_state import get_admin_session_token, get_chatgpt_account_id
    from autoteam.manager import cmd_bulk_invite
    from autoteam.team_context import get_team_context

    if not params.confirm:
        raise HTTPException(status_code=400, detail="批量 invite 会真实发送 Team 邀请邮件；请传 confirm=true 显式确认")

    default_limit = _normalize_swap_active_limit(_auto_check_config.get("target_seats", 2))
    team_context = get_team_context(params.account_id, default_max_chatgpt_active=default_limit) if params.account_id else None
    if params.account_id and not team_context:
        raise HTTPException(status_code=404, detail=f"未找到 Team 配置: {params.account_id}")
    session_present = bool(getattr(team_context, "session_token", "") if team_context else get_admin_session_token())
    resolved_account_id = str(getattr(team_context, "account_id", "") if team_context else get_chatgpt_account_id()).strip()
    if not session_present or not resolved_account_id:
        raise HTTPException(status_code=400, detail="请先完成管理员登录")

    emails = params.emails
    if isinstance(emails, str) and not emails.strip():
        raise HTTPException(status_code=400, detail="请填写至少 1 个邮箱")
    if isinstance(emails, list) and not [item for item in emails if str(item or "").strip()]:
        raise HTTPException(status_code=400, detail="请填写至少 1 个邮箱")

    concurrency = max(1, min(8, int(params.concurrency or 3)))
    batch_size = max(1, min(50, int(params.batch_size or 5)))
    task = _start_task(
        "bulk-invite",
        cmd_bulk_invite,
        {
            "account_id": params.account_id or "",
            "emails_count": len(emails) if isinstance(emails, list) else len(re.split(r"[,;\s]+", emails.strip())),
            "seat_type": params.seat_type,
            "concurrency": concurrency,
            "batch_size": batch_size,
            "confirm": True,
        },
        emails,
        team_context=team_context,
        seat_type=params.seat_type,
        concurrency=concurrency,
        batch_size=batch_size,
        resend_emails=params.resend_emails,
    )
    return task


@app.post("/api/tasks/fill", status_code=202)
def post_fill(params: TaskParams = TaskParams()):
    """swap_seat-only：禁用补号/邀请任务。"""
    _swap_seat_disabled("补号/邀请任务")


@app.post("/api/tasks/cleanup", status_code=202)
def post_cleanup(params: CleanupParams = CleanupParams()):
    """swap_seat-only：禁用清理/移除 Team 成员。"""
    _swap_seat_disabled("清理/移除 Team 成员任务")


@app.post("/api/tasks/reset-quota", status_code=202)
def post_reset_quota():
    """swap_seat-only：本地额度恢复记录已废弃，quota 只从 CPA 实时读取。"""
    _swap_seat_disabled("本地额度恢复记录重置")


@app.get("/api/tasks")
def get_tasks():
    """查看所有任务"""
    sorted_tasks = sorted(_tasks.values(), key=lambda t: t["created_at"], reverse=True)
    return sorted_tasks


@app.get("/api/tasks/{task_id}")
def get_task(task_id: str):
    """查看任务状态"""
    task = _tasks.get(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")
    return task


@app.post("/api/tasks/{task_id}/cancel")
def cancel_task(task_id: str):
    """请求终止后台任务。"""
    task = _tasks.get(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")

    if task["status"] in _TASK_TERMINAL_STATUSES:
        return {
            "task_id": task_id,
            "status": task["status"],
            "message": "任务已结束，无需终止",
            "task": task,
        }

    if not task.get("cancel_requested"):
        task["cancel_requested"] = True
        task["cancel_requested_at"] = time.time()
        if task["status"] in ("pending", "running"):
            task["status"] = "cancelling"
        task["error"] = "任务终止中"
        logger.warning("[API] 已请求终止任务 %s (%s)", task_id[:8], task.get("command", "unknown"))

    return {
        "task_id": task_id,
        "status": task["status"],
        "message": "已发送终止请求，任务会在安全检查点停止",
        "task": task,
    }


# ---------------------------------------------------------------------------
# 后台自动巡检
# ---------------------------------------------------------------------------

from autoteam.config import (
    AUTO_CHECK_ADD_PHONE_MAX_RETRIES as _DEFAULT_ADD_PHONE_MAX_RETRIES,
)
from autoteam.config import (
    AUTO_CHECK_INTERVAL as _DEFAULT_INTERVAL,
)
from autoteam.config import (
    AUTO_CHECK_MIN_LOW as _DEFAULT_MIN_LOW,
)
from autoteam.config import (
    AUTO_CHECK_REPLACE_MODE as _DEFAULT_REPLACE_MODE,
)
from autoteam.config import (
    AUTO_CHECK_REPLACE_WITH_PENDING_INVITE as _DEFAULT_REPLACE_WITH_PENDING_INVITE,
)
from autoteam.config import (
    AUTO_CHECK_RETRY_ADD_PHONE as _DEFAULT_RETRY_ADD_PHONE,
)
from autoteam.config import (
    AUTO_CHECK_TARGET_SEATS as _DEFAULT_TARGET_SEATS,
)
from autoteam.config import (
    AUTO_CHECK_THRESHOLD as _DEFAULT_THRESHOLD,
)

# 运行时可修改的巡检配置
_auto_check_config = {
    "interval": _DEFAULT_INTERVAL,
    "target_seats": _DEFAULT_TARGET_SEATS,
    "replace_with_pending_invite": _DEFAULT_REPLACE_WITH_PENDING_INVITE,
    "replace_mode": _DEFAULT_REPLACE_MODE,
    "threshold": _DEFAULT_THRESHOLD,
    "min_low": _DEFAULT_MIN_LOW,
    "retry_add_phone": _DEFAULT_RETRY_ADD_PHONE,
    "add_phone_max_retries": _DEFAULT_ADD_PHONE_MAX_RETRIES,
}
_auto_check_stop = threading.Event()
_auto_check_restart = threading.Event()  # 配置变更时通知线程重启


def _playwright_probe_command(*args: str) -> list[str]:
    return [sys.executable, "-m", "autoteam.playwright_probe", *args]


def _kill_subprocess_group(proc: subprocess.Popen):
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def _run_playwright_probe(*args: str, timeout_seconds: float = 30):
    cmd = _playwright_probe_command(*args)
    env = os.environ.copy()
    env["AUTOTEAM_PROBE_MODE"] = "1"
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
        start_new_session=True,
    )
    try:
        stdout, stderr = proc.communicate(timeout=max(1.0, float(timeout_seconds)))
    except subprocess.TimeoutExpired as exc:
        _kill_subprocess_group(proc)
        try:
            proc.communicate(timeout=1)
        except Exception:
            pass
        raise TimeoutError(f"Playwright probe timeout: {' '.join(args)}") from exc

    stdout = (stdout or "").strip()
    stderr = (stderr or "").strip()
    if proc.returncode != 0:
        detail = stderr or stdout or f"exit={proc.returncode}"
        raise RuntimeError(detail)

    if not stdout:
        return {}
    return _parse_playwright_probe_stdout(stdout)


def _parse_playwright_probe_stdout(stdout: str):
    text = (stdout or "").strip()
    if not text:
        return {}

    lines = [line.strip() for line in text.splitlines() if line.strip()]
    for line in reversed(lines):
        if not line.startswith(("{", "[")):
            continue
        try:
            return json.loads(line)
        except json.JSONDecodeError:
            continue

    return json.loads(text)


def _auto_check_team_member_count(timeout_seconds=30, retries=3):
    """查询 Team 实际成员数，供自动巡检的人数兜底判断使用。"""
    for attempt in range(1, max(1, retries) + 1):
        try:
            result = _run_playwright_probe("team-member-count", timeout_seconds=timeout_seconds)
        except TimeoutError:
            if attempt < retries:
                logger.warning(
                    "[巡检] 查询 Team 实际成员数超时（>%ss），准备重试第 %d/%d 次",
                    timeout_seconds,
                    attempt + 1,
                    retries,
                )
                continue
            logger.warning(
                "[巡检] 查询 Team 实际成员数超时（>%ss，已重试 %d 次），跳过本轮人数校验",
                timeout_seconds,
                retries,
            )
            return -1
        except Exception as exc:
            logger.warning("[巡检] 查询 Team 实际成员数失败: %s", exc)
            return -1

        try:
            return int(result.get("count", -1))
        except Exception:
            return -1

    return -1


def _auto_check_wait(interval_seconds, poll_seconds=0.2):
    """等待下一轮巡检，同时允许 stop / restart 尽快生效。"""
    interval = max(0.0, float(interval_seconds))
    poll = max(0.05, float(poll_seconds))
    deadline = time.monotonic() + interval

    while True:
        try:
            _maybe_reload_runtime_config_from_env_file()
        except Exception as exc:
            logger.warning("[配置] 自动热加载失败: %s", exc)

        if _auto_check_restart.is_set():
            _auto_check_restart.clear()
            return "restart"

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            if _auto_check_stop.wait(0):
                return "stop"
            return "timeout"

        step = min(remaining, poll)
        if _auto_check_stop.wait(step):
            return "stop"


def _log_auto_pat_repair(repair_result: dict | None, *, team_label: str | None = None) -> None:
    repair_result = repair_result or {}
    repaired = int((repair_result.get("summary") or {}).get("repaired") or 0)
    failed = int((repair_result.get("summary") or {}).get("failed") or 0)
    if not repaired and not failed:
        return
    if team_label:
        logger.info("[巡检] PAT 修复: team=%s repaired=%d failed=%d", team_label, repaired, failed)
    else:
        logger.info("[巡检] PAT 修复: repaired=%d failed=%d", repaired, failed)


def _run_auto_single_check_task(active_limit: int, replace_with_pending_invite: bool, replace_mode: str = "pending_invite"):
    """Run one auto-check under the task lock, including PAT repair."""
    from autoteam.manager import cmd_auto_detect_replace, cmd_repair_pat_auths, cmd_swap_seats

    try:
        _log_auto_pat_repair(cmd_repair_pat_auths(limit=2))
    except Exception as exc:
        logger.warning("[巡检] PAT 修复跳过/失败: %s", exc)

    if replace_with_pending_invite:
        return cmd_auto_detect_replace(
            max_chatgpt_active=active_limit,
            repair_before_replace=True,
            replace_mode=replace_mode,
        )
    return cmd_swap_seats(max_chatgpt_active=active_limit)


def _run_auto_multi_team_task(
    active_limit: int,
    replace_with_pending_invite: bool,
    replace_mode: str = "pending_invite",
    teams=None,
):
    """Run multi-Team auto-check under the task lock, including scoped PAT repair."""
    from autoteam.manager import cmd_manage_teams, cmd_repair_pat_auths
    from autoteam.team_context import get_team_contexts

    teams = list(teams) if teams is not None else get_team_contexts(default_max_chatgpt_active=active_limit)
    for team in teams:
        try:
            _log_auto_pat_repair(
                cmd_repair_pat_auths(limit=2, team_context=team),
                team_label=getattr(team, "label", "") or getattr(team, "account_id", "") or "default",
            )
        except Exception as exc:
            logger.warning(
                "[巡检] PAT 修复跳过/失败 team=%s: %s",
                getattr(team, "label", "") or getattr(team, "account_id", "") or "default",
                exc,
            )

    return cmd_manage_teams(
        active_limit,
        replace_with_pending_invite,
        replace_mode=replace_mode,
        pat_repair_already_run=False,
    )


def _auto_check_loop():
    """swap_seat-only 后台巡检：定时触发 CPA-driven seat/OAuth 收敛。"""
    while not _auto_check_stop.is_set():
        try:
            _maybe_reload_runtime_config_from_env_file()
        except Exception as exc:
            logger.warning("[配置] 自动热加载失败: %s", exc)

        cfg = _auto_check_config
        active_limit = _normalize_swap_active_limit(cfg.get("target_seats", 2))
        replace_with_pending_invite = bool(cfg.get("replace_with_pending_invite", False))
        replace_mode = _normalize_auto_check_replace_mode(cfg.get("replace_mode", "pending_invite"))
        logger.info(
            "[巡检] 等待 %d 分钟后执行下一轮 swap_seat（ChatGPT/OAuth active 保留 %d 个，低于目标自动补位=%s，模式=%s）",
            cfg["interval"] // 60,
            active_limit,
            "on" if replace_with_pending_invite else "off",
            replace_mode,
        )

        wait_result = _auto_check_wait(cfg["interval"])
        if wait_result == "stop":
            break
        if wait_result == "restart":
            continue

        try:
            _require_cpa_configs("自动 swap_seat")
            if replace_with_pending_invite:
                from autoteam.mail_provider import MAIL_PROVIDER_CLOUDFLARE_TEMP_EMAIL

                action_label = "自动创建 invite 补位" if replace_mode == "create_invite" else "自动 pending invite 补位"
                _require_mail_provider_configs(action_label, provider=MAIL_PROVIDER_CLOUDFLARE_TEMP_EMAIL)
            from autoteam.team_context import get_team_contexts

            teams = get_team_contexts(default_max_chatgpt_active=active_limit)
            multi_team = bool(os.environ.get("TEAM_WORKSPACES_JSON", "").strip()) or len(teams) > 1

            if multi_team:
                _start_task(
                    "manage-teams",
                    _run_auto_multi_team_task,
                    {
                        "max_chatgpt_active": active_limit,
                        "trigger": "auto-check",
                        "replace_with_pending_invite": replace_with_pending_invite,
                        "replace_mode": replace_mode,
                        "pat_repair_already_run": True,
                    },
                    active_limit,
                    replace_with_pending_invite,
                    replace_mode,
                    teams,
                )
                continue

            command = "auto-detect-replace" if replace_with_pending_invite else "auto-swap-seats"

            _start_task(
                command,
                _run_auto_single_check_task,
                {
                    "max_chatgpt_active": active_limit,
                    "trigger": "auto-check",
                    "replace_with_pending_invite": replace_with_pending_invite,
                    "replace_mode": replace_mode,
                    "pat_repair_already_run": True,
                },
                active_limit,
                replace_with_pending_invite,
                replace_mode,
            )
        except HTTPException as exc:
            logger.warning("[巡检] 跳过自动 swap_seat: %s", exc.detail)
        except Exception as exc:
            logger.error("[巡检] 自动 swap_seat 触发失败: %s", exc)


@app.on_event("startup")
def _start_auto_check():
    try:
        from autoteam.auth_storage import ensure_auth_file_permissions

        fixed = ensure_auth_file_permissions()
        if fixed:
            logger.info("[启动] 已修复 %d 个 auths 认证文件权限", fixed)
    except Exception as exc:
        logger.warning("[启动] 修复 auths 认证文件权限失败: %s", exc)

    _sync_runtime_env_reload_state()
    thread = threading.Thread(target=_auto_check_loop, daemon=True)
    thread.start()


@app.on_event("shutdown")
def _stop_auto_check():
    _auto_check_stop.set()
    try:
        _pw_executor.stop()
    except Exception:
        pass


# ---------------------------------------------------------------------------
# 前端静态文件
# ---------------------------------------------------------------------------

DIST_DIR = Path(__file__).parent / "web" / "dist"

if DIST_DIR.exists():
    # Vite 构建的 assets 目录
    assets_dir = DIST_DIR / "assets"
    if assets_dir.exists():
        app.mount("/assets", StaticFiles(directory=str(assets_dir)), name="assets")

    @app.get("/{path:path}")
    def serve_frontend(path: str):
        """兜底路由：serve 前端 SPA"""
        file = DIST_DIR / path
        if file.is_file() and ".." not in path:
            return FileResponse(str(file))
        return FileResponse(str(DIST_DIR / "index.html"))


class _QuietAccessLog(logging.Filter):
    """过滤前端轮询产生的高频访问日志"""

    _quiet_paths = (
        "/api/status",
        "/api/tasks",
        "/api/config/auto-check",
        "/api/config/runtime",
        "/api/admin/status",
        "/api/main-codex/status",
        "/api/manual-account/status",
        "/api/auth/check",
        "/api/setup/status",
    )

    def filter(self, record):
        msg = record.getMessage()
        return not any(p in msg for p in self._quiet_paths)


def start_server(host: str = "0.0.0.0", port: int = 8787):
    """启动 API 服务器"""
    import uvicorn

    # 过滤轮询日志，避免刷屏
    logging.getLogger("uvicorn.access").addFilter(_QuietAccessLog())
    # 首次启动检查配置
    from autoteam.setup_wizard import check_and_setup

    check_and_setup(interactive=True)

    # 重新读取 API_KEY（可能刚刚被向导写入）
    global API_KEY
    from autoteam.config import API_KEY as _fresh_key

    API_KEY = _fresh_key or os.environ.get("API_KEY", "")
    if API_KEY:
        logger.info("[API] API Key 鉴权已启用")
    else:
        logger.warning("[API] 未设置 API_KEY，所有接口无需认证")
    logger.info("[API] 启动 AutoTeam API 服务器 http://%s:%d", host, port)
    if DIST_DIR.exists():
        logger.info("[API] 前端面板 http://%s:%d", host, port)
    logger.info("[API] API 文档 http://%s:%d/docs", host, port)
    uvicorn.run(app, host=host, port=port, log_level="info")
