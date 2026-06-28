"""CPA (CLIProxyAPI) 认证文件同步 - 保持本地 codex 认证文件与 CPA 一致"""

import base64
import json
import logging
import os
import re
import time
from datetime import datetime
from hashlib import md5
from pathlib import Path

import requests

from autoteam.auth_storage import AUTH_DIR, ensure_auth_dir, ensure_auth_file_permissions
from autoteam.config import CPA_KEY, CPA_URL
from autoteam.cpa_config import normalize_cpa_url
from autoteam.textio import read_text, write_text

logger = logging.getLogger(__name__)
MANAGED_CPA_AUTHS_FILE = AUTH_DIR.parent / "managed_cpa_auths.json"


def _headers():
    return {"Authorization": f"Bearer {CPA_KEY}"}


def _cpa_base_url():
    return normalize_cpa_url(CPA_URL)


def _require_cpa_base_url(operation: str) -> str:
    base_url = _cpa_base_url()
    if not base_url:
        raise RuntimeError(f"未配置 CPA_URL，无法{operation}")
    return base_url


def list_cpa_files():
    """获取 CPA 中所有认证文件"""
    base_url = _cpa_base_url()
    if not base_url:
        logger.warning("[CPA] 未配置 CPA_URL，跳过文件列表读取")
        return []
    resp = requests.get(f"{base_url}/v0/management/auth-files", headers=_headers(), timeout=10)
    if resp.status_code != 200:
        logger.error("[CPA] 获取文件列表失败: %d", resp.status_code)
        return []
    data = resp.json()
    return data.get("files", [])


def _cpa_auth_identifier(auth_entry: dict | None) -> str:
    """返回 CPA management API 可接受的 auth 标识，优先用 name。"""
    auth_entry = auth_entry or {}
    for key in ("name", "id"):
        value = str(auth_entry.get(key) or "").strip()
        if value:
            return value
    return ""


def _auth_name_basename(value) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    return Path(text).name


def _parse_auth_name_set(value) -> set[str]:
    if value is None:
        return set()
    if isinstance(value, (set, list, tuple)):
        parts = [str(item or "") for item in value]
    else:
        parts = re.split(r"[,;\s]+", str(value or ""))
    return {_auth_name_basename(part) for part in parts if _auth_name_basename(part)}


def _load_managed_cpa_auth_registry() -> dict:
    try:
        raw = read_text(MANAGED_CPA_AUTHS_FILE).strip()
    except FileNotFoundError:
        return {"auths": {}}
    except Exception:
        logger.warning("[CPA] 读取受管 auth registry 失败，将按空 registry 处理", exc_info=True)
        return {"auths": {}}
    if not raw:
        return {"auths": {}}
    try:
        data = json.loads(raw)
    except Exception:
        logger.warning("[CPA] 受管 auth registry JSON 无效，将按空 registry 处理")
        return {"auths": {}}
    if not isinstance(data, dict):
        return {"auths": {}}
    if not isinstance(data.get("auths"), dict):
        data["auths"] = {}
    return data


def _save_managed_cpa_auth_registry(data: dict) -> None:
    MANAGED_CPA_AUTHS_FILE.parent.mkdir(parents=True, exist_ok=True)
    write_text(MANAGED_CPA_AUTHS_FILE, json.dumps(data, ensure_ascii=False, indent=2))


def register_managed_cpa_auth(auth_name_or_path, *, source: str = "autoteam") -> str:
    """登记 AutoTeam 自己创建/上传的 CPA auth，后续启停/删除只认这些名字。"""
    name = _auth_name_basename(auth_name_or_path)
    if not name:
        return ""
    registry = _load_managed_cpa_auth_registry()
    auths = registry.setdefault("auths", {})
    previous = auths.get(name) if isinstance(auths.get(name), dict) else {}
    auths[name] = {
        **previous,
        "name": name,
        "source": source,
        "registered_at": int(previous.get("registered_at") or time.time()),
        "updated_at": int(time.time()),
    }
    _save_managed_cpa_auth_registry(registry)
    return name


def unregister_managed_cpa_auth(auth_name_or_path) -> bool:
    """从 AutoTeam 受管 registry 移除 auth；不会影响 CPA 文件本身。"""
    name = _auth_name_basename(auth_name_or_path)
    if not name:
        return False
    registry = _load_managed_cpa_auth_registry()
    auths = registry.setdefault("auths", {})
    removed = False
    for key in list(auths.keys()):
        entry = auths.get(key)
        entry_name = _auth_name_basename(entry.get("name") if isinstance(entry, dict) else key)
        if key == name or entry_name == name:
            auths.pop(key, None)
            removed = True
    if removed:
        _save_managed_cpa_auth_registry(registry)
    return removed


def _extract_upload_response_auth_names(data) -> set[str]:
    """CPA 可能按自己的规则重命名 auth，登记响应里的真实名字。"""
    names = set()
    if isinstance(data, dict):
        for key in ("name", "filename", "file_name", "path", "id"):
            name = _auth_name_basename(data.get(key))
            if name:
                names.add(name)
        for key in ("file", "auth", "auth_file", "uploaded", "data", "result"):
            names.update(_extract_upload_response_auth_names(data.get(key)))
        for key in ("files", "auths", "items"):
            names.update(_extract_upload_response_auth_names(data.get(key)))
    elif isinstance(data, list):
        for item in data:
            names.update(_extract_upload_response_auth_names(item))
    elif isinstance(data, str):
        name = _auth_name_basename(data)
        if name:
            names.add(name)
    return names


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


def _account_looks_autoteam_created(acc: dict | None) -> bool:
    """保守识别 AutoTeam 创建的本地账号，避免把 CPA 反向同步账号当成可操作对象。"""
    acc = acc or {}
    explicit_managed = _explicit_managed_flag(acc)
    if explicit_managed is not None:
        return explicit_managed
    if str(acc.get("created_by") or "").lower() == "autoteam":
        return True
    if acc.get("mail_account_id") is not None or acc.get("cloudmail_account_id") is not None:
        return True
    return False


def get_managed_cpa_auth_names(accounts: list[dict] | None = None) -> set[str]:
    """返回允许 AutoTeam 在 CPA 中启停/删除的 auth 文件名集合。

    来源只包含：
    - 本项目上传成功后写入的 registry；
    - 可选显式环境变量 CPA_MANAGED_AUTH_NAMES（需 CPA_ALLOW_MANUAL_MANAGED_AUTH_NAMES=true）；
    - 本地账号池里看起来由 AutoTeam 注册流程创建的账号 auth_file。
    """
    names = set()
    manual_names = os.environ.get("CPA_MANAGED_AUTH_NAMES", "")
    if _truthy_env(os.environ.get("CPA_ALLOW_MANUAL_MANAGED_AUTH_NAMES")):
        names.update(_parse_auth_name_set(manual_names))
    elif str(manual_names or "").strip():
        logger.warning(
            "[CPA] 已忽略 CPA_MANAGED_AUTH_NAMES；如确需手动托管 CPA auth，请显式设置 CPA_ALLOW_MANUAL_MANAGED_AUTH_NAMES=true"
        )

    registry = _load_managed_cpa_auth_registry()
    for key, entry in registry.get("auths", {}).items():
        if isinstance(entry, dict):
            names.update(_parse_auth_name_set([entry.get("name") or key]))
        else:
            names.update(_parse_auth_name_set([key]))

    if accounts is None:
        try:
            from autoteam.accounts import load_accounts

            accounts = load_accounts()
        except Exception:
            accounts = []
    explicit_unmanaged_names = set()
    for acc in accounts or []:
        if not isinstance(acc, dict):
            continue
        name = _auth_name_basename(acc.get("auth_file"))
        if _explicit_managed_flag(acc) is False:
            if name:
                explicit_unmanaged_names.add(name)
            continue
        if not _account_looks_autoteam_created(acc):
            continue
        if name:
            names.add(name)
    return names - explicit_unmanaged_names


def cpa_auth_name_candidates(auth_entry_or_name) -> set[str]:
    if isinstance(auth_entry_or_name, dict):
        candidates = set()
        for key in ("name", "id", "path"):
            candidates.update(_parse_auth_name_set([auth_entry_or_name.get(key)]))
        return candidates
    return _parse_auth_name_set([auth_entry_or_name])


def is_managed_cpa_auth(auth_entry_or_name, managed_names: set[str] | None = None) -> bool:
    managed_names = get_managed_cpa_auth_names() if managed_names is None else {_auth_name_basename(name) for name in managed_names}
    candidates = cpa_auth_name_candidates(auth_entry_or_name)
    return bool(candidates and candidates & managed_names)


def _cpa_auth_index(auth_entry: dict | None) -> str:
    """返回 /api-call 所需的 auth_index。"""
    auth_entry = auth_entry or {}
    for key in ("auth_index", "authIndex", "AuthIndex"):
        value = str(auth_entry.get(key) or "").strip()
        if value:
            return value
    return ""


def is_cpa_codex_oauth(auth_entry: dict | None) -> bool:
    """判断 CPA auth-files 条目是否是 Codex OAuth 账号。"""
    auth_entry = auth_entry or {}
    provider = str(auth_entry.get("provider") or auth_entry.get("type") or "").strip().lower()
    if provider and provider != "codex":
        return False
    email = str(auth_entry.get("email") or auth_entry.get("account") or "").strip()
    return bool(email)


def cpa_auth_is_disabled(auth_entry: dict | None) -> bool:
    """兼容 CPA disabled 字段的布尔/字符串返回值。"""
    if not auth_entry:
        return False
    value = auth_entry.get("disabled", False)
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value or "").strip().lower()
    if text in {"1", "true", "yes", "on", "disabled"}:
        return True
    if text in {"", "0", "false", "no", "off", "active", "enabled", "ok", "none", "null"}:
        return False
    return bool(text)


def _coerce_bool(value, *, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if value is None:
        return default
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on", "enabled"}:
        return True
    if text in {"", "0", "false", "no", "off", "none", "null"}:
        return False
    return bool(text)


def cpa_auth_is_active(auth_entry: dict | None) -> bool:
    """CPA 侧 OAuth 是否处于可调度 active 状态。"""
    if not auth_entry or cpa_auth_is_disabled(auth_entry):
        return False
    status = str(auth_entry.get("status") or "").strip().lower()
    # CPA 有些列表响应只返回 disabled=false 而不返回 status；这类条目已经可调度，
    # 否则会在容量统计里被漏算，导致下一轮误选超过目标数量的 active OAuth。
    return not status or status in {"active", "enabled", "ok"}


def set_cpa_auth_disabled(auth_entry_or_name, disabled: bool, *, force: bool = False):
    """通过 CPA management API 启用/禁用单个 OAuth/auth-file；默认只允许自管 auth。"""
    if isinstance(auth_entry_or_name, dict):
        name = _cpa_auth_identifier(auth_entry_or_name)
    else:
        name = str(auth_entry_or_name or "").strip()
    if not name:
        raise ValueError("CPA auth name/id 为空")
    if not force and not is_managed_cpa_auth(auth_entry_or_name):
        raise PermissionError(f"禁止修改未由 AutoTeam 创建/登记的 CPA auth: {_auth_name_basename(name) or name}")

    base_url = _require_cpa_base_url("更新 CPA auth 状态")
    resp = requests.patch(
        f"{base_url}/v0/management/auth-files/status",
        headers={**_headers(), "Content-Type": "application/json"},
        json={"name": name, "disabled": bool(disabled)},
        timeout=10,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"CPA auth 状态更新失败: HTTP {resp.status_code} {resp.text[:200]}")
    try:
        return resp.json()
    except Exception:
        return {"status": "ok", "disabled": bool(disabled)}


def cpa_api_call(auth_entry_or_index, method: str, url: str, *, headers: dict | None = None, data: str = ""):
    """调用 CPA /v0/management/api-call，用 CPA 内的 OAuth 代发请求。"""
    if isinstance(auth_entry_or_index, dict):
        auth_index = _cpa_auth_index(auth_entry_or_index)
    else:
        auth_index = str(auth_entry_or_index or "").strip()
    if not auth_index:
        raise ValueError("CPA auth_index 为空，无法通过 api-call 检查 quota")

    base_url = _require_cpa_base_url("调用 CPA api-call")
    payload = {
        "auth_index": auth_index,
        "method": method,
        "url": url,
        "header": headers or {},
    }
    if data:
        payload["data"] = data

    resp = requests.post(
        f"{base_url}/v0/management/api-call",
        headers={**_headers(), "Content-Type": "application/json"},
        json=payload,
        timeout=70,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"CPA api-call 失败: HTTP {resp.status_code} {resp.text[:200]}")
    return resp.json()


def parse_codex_quota_usage(data: dict | str):
    """
    解析 ChatGPT /backend-api/wham/usage 的 rate_limit。

    返回值与 autoteam.codex_auth.check_codex_quota 保持一致：
    ("ok", quota_info) | ("exhausted", exhausted_info) | ("auth_error", None)
    quota_info 同时包含 5h(primary)、weekly(secondary) 和可选 monthly 窗口。
    """
    from autoteam.codex_auth import parse_codex_usage_payload

    return parse_codex_usage_payload(data)


def check_cpa_codex_quota(auth_entry: dict, account_id: str | None = None):
    """只通过 CPA API 检查某个 Codex OAuth 的 5h/weekly/monthly quota。"""
    request_headers = {
        "Authorization": "Bearer $TOKEN$",
        "Content-Type": "application/json",
    }
    if account_id:
        request_headers["Chatgpt-Account-Id"] = account_id

    try:
        result = cpa_api_call(
            auth_entry,
            "GET",
            "https://chatgpt.com/backend-api/wham/usage",
            headers=request_headers,
        )
    except Exception as exc:
        logger.warning("[CPA] quota 检查失败: %s (%s)", _cpa_auth_identifier(auth_entry), exc)
        return "auth_error", {"error": str(exc)}

    status_code = int(result.get("status_code") or result.get("statusCode") or 0)
    body = result.get("body") or ""
    if status_code in (401, 403):
        return "auth_error", {"status_code": status_code, "body": body[:200]}
    if status_code != 200:
        logger.warning("[CPA] wham/usage 异常: HTTP %d %s", status_code, str(body)[:200])
        return "auth_error", {"status_code": status_code, "body": str(body)[:200]}

    return parse_codex_quota_usage(body)


def upload_to_cpa(filepath):
    """上传认证文件到 CPA"""
    filepath = Path(filepath)
    if not filepath.exists():
        logger.warning("[CPA] 文件不存在: %s", filepath)
        return False

    try:
        base_url = _require_cpa_base_url("上传认证文件")
    except RuntimeError as exc:
        logger.warning("[CPA] %s", exc)
        return False

    with open(filepath, "rb") as f:
        resp = requests.post(
            f"{base_url}/v0/management/auth-files",
            headers=_headers(),
            files={"file": (filepath.name, f, "application/json")},
            timeout=10,
        )

    if resp.status_code == 200:
        uploaded_names = {filepath.name}
        try:
            uploaded_names.update(_extract_upload_response_auth_names(resp.json()))
        except Exception:
            pass
        for name in uploaded_names:
            register_managed_cpa_auth(name, source="upload_to_cpa")
        logger.info("[CPA] 已上传: %s", filepath.name)
        return True
    else:
        logger.error("[CPA] 上传失败: %d %s", resp.status_code, resp.text[:200])
        return False


def delete_from_cpa(name, *, force: bool = False):
    """从 CPA 删除认证文件；默认只允许删除 AutoTeam 已登记的 auth。"""
    name = _auth_name_basename(name)
    if not name:
        logger.warning("[CPA] 删除跳过：auth name 为空")
        return False
    if not force and not is_managed_cpa_auth(name):
        logger.warning("[CPA] 删除跳过：%s 不是 AutoTeam 自管 auth", name)
        return False
    try:
        base_url = _require_cpa_base_url("删除认证文件")
    except RuntimeError as exc:
        logger.warning("[CPA] %s", exc)
        return False

    resp = requests.delete(
        f"{base_url}/v0/management/auth-files",
        headers=_headers(),
        params={"name": name},
        timeout=10,
    )
    if resp.status_code == 200:
        logger.info("[CPA] 已删除: %s", name)
        return True
    else:
        logger.error("[CPA] 删除失败: %d %s", resp.status_code, resp.text[:200])
        return False


def download_from_cpa(name):
    """从 CPA 下载认证文件内容。"""
    try:
        base_url = _require_cpa_base_url("下载认证文件")
    except RuntimeError as exc:
        logger.warning("[CPA] %s", exc)
        return None

    resp = requests.get(
        f"{base_url}/v0/management/auth-files/download",
        headers=_headers(),
        params={"name": name},
        timeout=10,
    )
    if resp.status_code == 200:
        return resp.text
    logger.error("[CPA] 下载失败: %s -> %d %s", name, resp.status_code, resp.text[:200])
    return None


def _parse_expired_timestamp(value):
    if isinstance(value, (int, float)):
        return float(value)
    if not value:
        return time.time() + 3600
    text = str(value).strip()
    try:
        if text.endswith("Z"):
            return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
        return datetime.fromisoformat(text).timestamp()
    except Exception:
        return time.time() + 3600


def _parse_optional_timestamp(value):
    if isinstance(value, (int, float)):
        return float(value)
    if not value:
        return 0.0
    text = str(value).strip()
    try:
        if text.endswith("Z"):
            return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
        return datetime.fromisoformat(text).timestamp()
    except Exception:
        return 0.0


def _parse_jwt_payload(token):
    parts = token.split(".")
    if len(parts) < 2:
        return {}
    payload = parts[1]
    payload += "=" * (-len(payload) % 4)
    try:
        return json.loads(base64.urlsafe_b64decode(payload))
    except Exception:
        return {}


def _bundle_from_auth_data(auth_data, fallback_name=""):
    id_token = auth_data.get("id_token") or ""
    claims = _parse_jwt_payload(id_token) if id_token else {}
    auth_claims = claims.get("https://api.openai.com/auth", {}) if isinstance(claims, dict) else {}

    plan_type = auth_claims.get("chatgpt_plan_type", "")
    if not plan_type and "-team" in fallback_name:
        plan_type = "team"
    if not plan_type and "-plus" in fallback_name:
        plan_type = "plus"
    if not plan_type and "-free" in fallback_name:
        plan_type = "free"
    if not plan_type:
        plan_type = "unknown"

    return {
        "id_token": id_token,
        "access_token": auth_data.get("access_token", ""),
        "refresh_token": auth_data.get("refresh_token", ""),
        "account_id": auth_data.get("account_id", ""),
        "email": auth_data.get("email", ""),
        "plan_type": plan_type,
        "expired": _parse_expired_timestamp(auth_data.get("expired") or auth_data.get("expires_at")),
        "last_refresh_ts": _parse_optional_timestamp(auth_data.get("last_refresh")),
        # 新版 userscript 导出的 Codex Access Token 在 headers.authorization 里；
        # 归一化/反向同步时必须原样保留，否则会退回旧 OAuth access_token 格式导致 CPA 不可用。
        "headers": auth_data.get("headers") if isinstance(auth_data.get("headers"), dict) else {},
        "disabled": cpa_auth_is_disabled(auth_data),
        "websockets": _coerce_bool(auth_data.get("websockets"), default=True),
    }


def _normalized_auth_path(bundle, main=False):
    email = bundle.get("email", "")
    account_id = bundle.get("account_id", "")
    if main:
        suffix = account_id or md5(email.encode()).hexdigest()[:8]
        return AUTH_DIR / f"codex-main-{suffix}.json"
    plan_type = bundle.get("plan_type", "unknown")
    hash_id = md5(account_id.encode()).hexdigest()[:8] if account_id else "unknown"
    return AUTH_DIR / f"codex-{email}-{plan_type}-{hash_id}.json"


def _auth_identity(bundle, main=False):
    if main:
        return ("main", bundle.get("account_id") or bundle.get("email") or "")
    return ("codex", (bundle.get("email") or "").lower(), bundle.get("account_id") or "")


def _candidate_score(auth_data, bundle, name, main=False):
    canonical_name = _normalized_auth_path(bundle, main=main).name
    headers = auth_data.get("headers") if isinstance(auth_data.get("headers"), dict) else {}
    header_authorization = str(headers.get("authorization") or headers.get("Authorization") or "")
    return (
        1 if name == canonical_name else 0,
        bundle.get("last_refresh_ts", _parse_optional_timestamp(auth_data.get("last_refresh"))),
        _parse_expired_timestamp(auth_data.get("expired")),
        len(header_authorization),
        len(auth_data.get("refresh_token") or ""),
    )


def _write_auth_file(filepath, bundle):
    ensure_auth_dir()
    auth_data = {
        "type": "codex",
        "id_token": bundle.get("id_token") or None,
        "access_token": bundle.get("access_token", ""),
        "refresh_token": bundle.get("refresh_token") or None,
        "account_id": bundle.get("account_id", ""),
        "email": bundle.get("email", ""),
        "expired": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(bundle.get("expired", 0))),
        "last_refresh": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(bundle.get("last_refresh_ts", time.time()))),
    }
    headers = bundle.get("headers") if isinstance(bundle.get("headers"), dict) else {}
    if headers:
        auth_data["headers"] = headers
    auth_data["disabled"] = cpa_auth_is_disabled(bundle)
    auth_data["websockets"] = _coerce_bool(bundle.get("websockets"), default=True)
    write_text(filepath, json.dumps(auth_data, indent=2))
    ensure_auth_file_permissions(filepath)
    return filepath


def _save_normalized_auth_file(bundle, main=False):
    filepath = _normalized_auth_path(bundle, main=main)

    if main:
        for old in AUTH_DIR.glob("codex-main-*.json"):
            if old != filepath and old.exists():
                old.unlink()
    else:
        email = bundle.get("email", "")
        for old in AUTH_DIR.glob(f"codex-{email}-*.json"):
            if old != filepath and old.exists():
                old.unlink()

    return _write_auth_file(filepath, bundle)


def _load_local_best_candidate(identity_key):
    """读取本地同 identity 的最佳候选认证文件。"""
    best = None
    for path in AUTH_DIR.glob("codex-*.json"):
        if not path.is_file():
            continue
        try:
            auth_data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if auth_data.get("type") != "codex":
            continue
        main = path.name.startswith("codex-main-")
        bundle = _bundle_from_auth_data(auth_data, fallback_name=path.name)
        if _auth_identity(bundle, main=main) != identity_key:
            continue
        candidate = {
            "path": path,
            "auth_data": auth_data,
            "bundle": bundle,
            "main": main,
        }
        if best is None or _candidate_score(
            candidate["auth_data"], candidate["bundle"], candidate["path"].name, candidate["main"]
        ) > _candidate_score(best["auth_data"], best["bundle"], best["path"].name, best["main"]):
            best = candidate
    return best


def _cleanup_local_duplicates(accounts=None):
    """清理本地同账号重复认证文件，只保留一个规范文件。"""
    grouped = {}
    for path in AUTH_DIR.glob("codex-*.json"):
        if not path.is_file():
            continue
        try:
            auth_data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if auth_data.get("type") != "codex":
            continue
        main = path.name.startswith("codex-main-")
        bundle = _bundle_from_auth_data(auth_data, fallback_name=path.name)
        key = _auth_identity(bundle, main=main)
        grouped.setdefault(key, []).append(
            {
                "path": path,
                "auth_data": auth_data,
                "bundle": bundle,
                "main": main,
            }
        )

    canonical_map = {}
    removed = 0
    for items in grouped.values():
        if not items:
            continue
        winner = max(
            items, key=lambda item: _candidate_score(item["auth_data"], item["bundle"], item["path"].name, item["main"])
        )
        canonical_path = Path(_save_normalized_auth_file(winner["bundle"], main=winner["main"]))
        canonical_map[_auth_identity(winner["bundle"], main=winner["main"])] = canonical_path
        for item in items:
            if item["path"] != canonical_path and item["path"].exists():
                item["path"].unlink()
                removed += 1

    if accounts is not None:
        changed = False
        for acc in accounts:
            auth_path = acc.get("auth_file")
            if not auth_path:
                continue
            try:
                path = Path(auth_path)
                if not path.exists():
                    continue
                auth_data = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                continue
            bundle = _bundle_from_auth_data(auth_data, fallback_name=path.name)
            canonical_path = canonical_map.get(_auth_identity(bundle, main=False))
            if canonical_path and acc.get("auth_file") != str(canonical_path.resolve()):
                acc["auth_file"] = str(canonical_path.resolve())
                changed = True
        return removed, changed

    return removed, False


def sync_from_cpa():
    """
    从 CPA 反向同步认证文件到本地。

    规则：
    - 下载 CPA 中所有 codex 认证文件到本地 auths/
    - 非主号文件会导入/修复到 accounts.json，默认状态为 standby（保守导入）
    - 不删除本地账号记录，仅补充/更新 auth_file
    """
    from autoteam.accounts import STATUS_STANDBY, find_account, load_accounts, save_accounts
    from autoteam.mail_provider import infer_mail_provider_from_email, infer_mail_service_from_email

    AUTH_DIR.mkdir(exist_ok=True)

    accounts = load_accounts()
    changed_accounts = False
    imported_files = 0
    updated_files = 0
    added_accounts = 0
    updated_accounts = 0
    skipped = 0
    cpa_duplicates_deleted = 0
    cpa_duplicates_skipped_unmanaged = 0
    local_kept_newer = 0

    local_duplicates_deleted, accounts_path_repaired = _cleanup_local_duplicates(accounts)
    if accounts_path_repaired:
        save_accounts(accounts)

    managed_auth_names = get_managed_cpa_auth_names(accounts)
    cpa_files = list_cpa_files()
    if not cpa_files:
        logger.info("[CPA] 未发现可反向同步的认证文件")
        return {
            "downloaded": 0,
            "updated": 0,
            "accounts_added": 0,
            "accounts_updated": 0,
            "skipped": 0,
            "cpa_duplicates_deleted": 0,
            "local_duplicates_deleted": local_duplicates_deleted,
            "local_kept_newer": 0,
            "total": 0,
        }

    candidates = []
    for item in cpa_files:
        name = (item.get("name") or "").strip()
        if not name or not name.endswith(".json"):
            skipped += 1
            continue

        content = download_from_cpa(name)
        if not content:
            skipped += 1
            continue

        try:
            auth_data = json.loads(content)
        except Exception:
            logger.warning("[CPA] 跳过无效 JSON: %s", name)
            skipped += 1
            continue

        if auth_data.get("type") != "codex":
            logger.info("[CPA] 跳过非 codex 文件: %s", name)
            skipped += 1
            continue

        bundle = _bundle_from_auth_data(auth_data, fallback_name=name)
        email = (bundle.get("email") or item.get("email") or "").lower().strip()
        bundle["email"] = email

        if not email and not name.startswith("codex-main-"):
            logger.info("[CPA] 跳过缺少邮箱的文件: %s", name)
            continue

        candidates.append(
            {
                "name": name,
                "auth_data": auth_data,
                "bundle": bundle,
                "main": name.startswith("codex-main-"),
            }
        )

    grouped = {}
    for item in candidates:
        grouped.setdefault(_auth_identity(item["bundle"], main=item["main"]), []).append(item)

    for items in grouped.values():
        winner = max(
            items,
            key=lambda item: _candidate_score(item["auth_data"], item["bundle"], item["name"], main=item["main"]),
        )
        for item in items:
            if item is winner:
                continue
            if item["name"] not in managed_auth_names:
                cpa_duplicates_skipped_unmanaged += 1
                continue
            if delete_from_cpa(item["name"]):
                cpa_duplicates_deleted += 1

        name = winner["name"]
        bundle = winner["bundle"]
        email = bundle.get("email", "")
        identity_key = _auth_identity(bundle, main=winner["main"])
        local_best = _load_local_best_candidate(identity_key)
        cpa_score = _candidate_score(winner["auth_data"], bundle, name, main=winner["main"])
        local_score = None
        if local_best:
            local_score = _candidate_score(
                local_best["auth_data"], local_best["bundle"], local_best["path"].name, main=local_best["main"]
            )

        if winner["main"]:
            if local_best and local_score >= cpa_score:
                local_kept_newer += 1
                normalized_path = local_best["path"]
            else:
                normalized_path = _normalized_auth_path(bundle, main=True)
                existed = normalized_path.exists()
                previous = None
                if existed:
                    try:
                        previous = normalized_path.read_text(encoding="utf-8")
                    except Exception:
                        previous = None

                normalized_path = Path(_save_normalized_auth_file(bundle, main=True))
                current = normalized_path.read_text(encoding="utf-8")
                if not existed:
                    imported_files += 1
                elif previous != current:
                    updated_files += 1
            if normalized_path.name != name:
                old_path = AUTH_DIR / name
                if old_path.exists() and old_path != normalized_path:
                    old_path.unlink()
            continue

        if local_best and local_score >= cpa_score:
            local_kept_newer += 1
            normalized_path = local_best["path"]
        else:
            normalized_path = _normalized_auth_path(bundle)
            existed = normalized_path.exists()
            previous = None
            if existed:
                try:
                    previous = normalized_path.read_text(encoding="utf-8")
                except Exception:
                    previous = None

            normalized_path = Path(_save_normalized_auth_file(bundle))
            current = normalized_path.read_text(encoding="utf-8")

            if not existed:
                imported_files += 1
            elif previous != current:
                updated_files += 1

        acc = find_account(accounts, email)
        resolved_path = str(normalized_path.resolve())
        inferred_service_id = infer_mail_service_from_email(email) or None
        inferred_provider = infer_mail_provider_from_email(email)
        if acc:
            acc_changed = False
            if acc.get("auth_file") != resolved_path:
                acc["auth_file"] = resolved_path
                acc_changed = True
            if inferred_service_id and not acc.get("mail_service_id"):
                acc["mail_service_id"] = inferred_service_id
                acc_changed = True
            if (
                inferred_provider
                and not acc.get("mail_provider")
                and acc.get("mail_account_id") is None
                and acc.get("cloudmail_account_id") is None
            ):
                acc["mail_provider"] = inferred_provider
                acc_changed = True
            if acc_changed:
                changed_accounts = True
                updated_accounts += 1
        else:
            accounts.append(
                {
                    "email": email,
                    "password": "",
                    "mail_service_id": inferred_service_id,
                    "mail_provider": inferred_provider,
                    "mail_account_id": None,
                    "cloudmail_account_id": None,
                    "status": STATUS_STANDBY,
                    "auth_file": resolved_path,
                    "quota_exhausted_at": None,
                    "quota_resets_at": None,
                    "created_at": time.time(),
                    "last_active_at": None,
                    "auth_retry_count": 0,
                    "auth_last_error": None,
                    "auth_last_error_detail": None,
                    "auth_last_failed_at": None,
                    "auth_retry_after": None,
                    "auth_retry_paused": False,
                    "disabled": False,
                }
            )
            changed_accounts = True
            added_accounts += 1

    if changed_accounts:
        save_accounts(accounts)

    local_duplicates_deleted_after, accounts_path_repaired = _cleanup_local_duplicates(accounts)
    local_duplicates_deleted += local_duplicates_deleted_after
    if accounts_path_repaired:
        save_accounts(accounts)

    logger.info(
        "[CPA] 反向同步完成: 新增文件 %d, 更新文件 %d, 新增账号 %d, 更新账号 %d, 保留本地较新 %d, CPA去重 %d, 未受管跳过 %d, 本地去重 %d, 跳过 %d",
        imported_files,
        updated_files,
        added_accounts,
        updated_accounts,
        local_kept_newer,
        cpa_duplicates_deleted,
        cpa_duplicates_skipped_unmanaged,
        local_duplicates_deleted,
        skipped,
    )
    return {
        "downloaded": imported_files,
        "updated": updated_files,
        "accounts_added": added_accounts,
        "accounts_updated": updated_accounts,
        "skipped": skipped,
        "local_kept_newer": local_kept_newer,
        "cpa_duplicates_deleted": cpa_duplicates_deleted,
        "cpa_duplicates_skipped_unmanaged": cpa_duplicates_skipped_unmanaged,
        "local_duplicates_deleted": local_duplicates_deleted,
        "total": len(cpa_files),
    }


def sync_to_cpa():
    """
    同步本地认证文件到 CPA，只同步 active 状态的账号。
    - active 且 CPA 没有 → 上传
    - CPA 有但不是 active（或本地已删除）→ 从 CPA 删除
    """
    from autoteam.accounts import STATUS_ACTIVE, is_account_disabled, load_accounts, save_accounts

    accounts = load_accounts()
    local_emails = {a["email"].lower() for a in accounts}
    local_duplicates_deleted, accounts_path_repaired = _cleanup_local_duplicates(accounts)
    if accounts_path_repaired:
        save_accounts(accounts)

    # 修复断裂的 auth_file 路径
    changed = False
    for acc in accounts:
        auth_path = acc.get("auth_file")
        if auth_path and not Path(auth_path).exists():
            matches = list(AUTH_DIR.glob(f"codex-{acc['email']}-*.json"))
            if matches:
                acc["auth_file"] = str(matches[0].resolve())
                changed = True
    if changed:
        save_accounts(accounts)

    # active 账号的认证文件
    managed_auth_names = get_managed_cpa_auth_names(accounts)

    active_files = {}
    for acc in accounts:
        if is_account_disabled(acc):
            continue
        if not _account_looks_autoteam_created(acc):
            continue
        if acc["status"] == STATUS_ACTIVE and acc.get("auth_file"):
            path = Path(acc["auth_file"])
            if path.exists():
                active_files[path.name] = path

    # CPA 认证文件
    cpa_files = list_cpa_files()
    cpa_names = {f["name"]: f for f in cpa_files}

    logger.info("[CPA] active 认证文件: %d, CPA 认证文件: %d", len(active_files), len(cpa_files))

    # 上传：所有 active 认证文件（覆盖同名文件，确保 token 最新）
    uploaded = 0
    for name, path in active_files.items():
        logger.info("[CPA] 上传: %s", name)
        if upload_to_cpa(path):
            uploaded += 1
            managed_auth_names.add(name)

    # 删除：CPA 中有但不在 active 列表的（仅限本地管理的账号）
    deleted = 0
    for name, cpa_file in cpa_names.items():
        if name in managed_auth_names and name not in active_files:
            email = cpa_file.get("email", "").lower()
            logger.info("[CPA] 删除非 active 文件: %s (%s)", name, email)
            if delete_from_cpa(name):
                deleted += 1

    logger.info("[CPA] 同步完成: 上传 %d, 删除 %d, 本地去重 %d", uploaded, deleted, local_duplicates_deleted)

    # 最终状态
    final_cpa = list_cpa_files()
    final_local_managed = [f for f in final_cpa if f.get("email", "").lower() in local_emails]
    logger.info("[CPA] CPA 中本地管理: %d, 本地 active: %d", len(final_local_managed), len(active_files))


def sync_main_codex_to_cpa(filepath):
    """同步主号 Codex 认证文件到 CPA。"""
    filepath = Path(filepath)
    if not filepath.exists():
        raise FileNotFoundError(f"主号认证文件不存在: {filepath}")

    name = filepath.name
    existing = {item.get("name"): item for item in list_cpa_files()}
    managed_auth_names = get_managed_cpa_auth_names()

    for old_name in existing:
        if old_name and old_name.startswith("codex-main-") and old_name in managed_auth_names:
            logger.info("[CPA] 删除旧主号文件: %s", old_name)
            delete_from_cpa(old_name)

    if not upload_to_cpa(filepath):
        raise RuntimeError(f"上传主号认证文件失败: {name}")

    logger.info("[CPA] 主号 Codex 已同步: %s", name)
    return {"uploaded": name}


def delete_main_codex_from_cpa():
    """删除 CPA 中的主号 Codex 认证文件。"""
    existing = list_cpa_files()
    deleted = []
    managed_auth_names = get_managed_cpa_auth_names()

    for item in existing:
        name = item.get("name") or ""
        if not name.startswith("codex-main-") or name not in managed_auth_names:
            continue
        logger.info("[CPA] 删除主号文件: %s", name)
        if delete_from_cpa(name):
            deleted.append(name)

    return {"deleted": deleted, "count": len(deleted)}
