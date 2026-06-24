"""自动创建 Codex Access Token 并生成 CPA auth JSON。

这是 `ChatGPT Codex Auth Export` userscript 的后端版：
- 不走旧 Codex OAuth / refresh_token；
- 使用已登录 ChatGPT session 创建 Codex PAT；
- 导出 CPA 可用的 `type=codex` JSON，其中真正 token 在 `headers.authorization`；
- 可选保存到本地 auths/ 并上传 CPA。
"""

from __future__ import annotations

import base64
import json
import logging
import re
import time
from datetime import datetime, timezone
from hashlib import md5
from pathlib import Path
from typing import Any

import requests

from autoteam.auth_storage import AUTH_DIR, ensure_auth_dir, ensure_auth_file_permissions
from autoteam.textio import write_text

logger = logging.getLogger(__name__)

V2_SCOPE_CODEX = "chatgpt.workspace.feature.allow-codex-local-access.access"
V2_SCOPE_HERMES = "chatgpt.workspace.feature.hermes.access"
DEFAULT_TTL_DAYS = 30
MAX_V2_TTL_SECONDS = 364 * 24 * 60 * 60
WHOAMI_URL = "https://auth.openai.com/api/accounts/v1/user-auth-credential/whoami"


class CodexPatExportError(RuntimeError):
    pass


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _iso_after_seconds(seconds: int) -> str:
    return datetime.fromtimestamp(time.time() + int(seconds), timezone.utc).astimezone().isoformat(timespec="seconds")


def _urlsafe_b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _std_b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _decode_jwt_payload(token: str | None) -> dict:
    parts = str(token or "").split(".")
    if len(parts) < 2:
        return {}
    payload = parts[1] + "=" * (-len(parts[1]) % 4)
    try:
        data = json.loads(base64.urlsafe_b64decode(payload.encode("ascii")))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _auth_claims(token: str | None) -> dict:
    payload = _decode_jwt_payload(token)
    claims = payload.get("https://api.openai.com/auth")
    return claims if isinstance(claims, dict) else {}


def _normalize_backend_path(path: str) -> str:
    if path.startswith("/backend-api/"):
        return path
    if path.startswith("/wham/") or path.startswith("/service_accounts/"):
        return f"/backend-api{path}"
    return path


def _api_json(chatgpt_api, method: str, path: str, body: dict | None = None) -> Any:
    result = chatgpt_api._api_fetch(method, _normalize_backend_path(path), body)
    status = int(result.get("status") or 0)
    text = str(result.get("body") or "")
    try:
        data = json.loads(text) if text else None
    except Exception:
        data = text
    if status < 200 or status >= 300:
        message = None
        if isinstance(data, dict):
            message = data.get("detail") or data.get("message") or data.get("error")
        raise CodexPatExportError(str(message or f"HTTP {status}: {text[:200]}"))
    return data


def _phone_verification_required(exc: Exception) -> bool:
    text = str(exc)
    payload = getattr(exc, "payload", None)
    if isinstance(payload, dict):
        text += " " + json.dumps(payload, ensure_ascii=False)
    return "phone_verification_required" in text.lower()


def _ttl_seconds(ttl_days: int | float | None) -> int:
    days = DEFAULT_TTL_DAYS if ttl_days is None else float(ttl_days)
    if days <= 0:
        raise ValueError("TTL 天数必须大于 0")
    return min(int(days * 24 * 60 * 60), MAX_V2_TTL_SECONDS)


def _default_token_name(email: str | None = None) -> str:
    suffix = datetime.now().strftime("%Y-%m-%d %H:%M")
    return f"AutoTeam Codex {email or ''} {suffix}".strip()


def _session_metadata(chatgpt_api) -> dict:
    access_token = getattr(chatgpt_api, "access_token", "") or ""
    claims = _auth_claims(access_token)
    email = getattr(chatgpt_api, "email", "") or claims.get("email") or ""
    try:
        from autoteam.admin_state import get_admin_email

        email = email or get_admin_email()
    except Exception:
        pass
    return {
        "email": email or None,
        "chatgpt_user_id": claims.get("chatgpt_account_user_id")
        or claims.get("account_user_id")
        or claims.get("chatgpt_user_id")
        or claims.get("user_id"),
        "chatgpt_account_id": getattr(chatgpt_api, "account_id", "") or claims.get("chatgpt_account_id") or claims.get("account_id"),
        "chatgpt_plan_type": claims.get("chatgpt_plan_type") or "unknown",
        "chatgpt_account_is_fedramp": False,
    }


def _fetch_available_scopes(chatgpt_api) -> list[str]:
    try:
        data = _api_json(chatgpt_api, "GET", "/wham/auth-credentials/available-scopes")
    except Exception as exc:
        logger.warning("[CodexPAT] 获取 available scopes 失败，将尝试 v1 fallback: %s", exc)
        return []
    scopes = data.get("scopes") if isinstance(data, dict) else []
    return scopes if isinstance(scopes, list) else []


def _create_v2(chatgpt_api, *, name: str, ttl_seconds: int, include_hermes: bool) -> dict | None:
    available = _fetch_available_scopes(chatgpt_api)
    if V2_SCOPE_CODEX not in available:
        return None
    scopes = [V2_SCOPE_CODEX]
    if include_hermes and V2_SCOPE_HERMES in available:
        scopes.append(V2_SCOPE_HERMES)
    body = {"name": name, "scopes": sorted(set(scopes)), "ttl": ttl_seconds}
    created = _api_json(chatgpt_api, "POST", "/wham/auth-credentials", body)
    if not isinstance(created, dict) or not created.get("access_token"):
        raise CodexPatExportError("v2 创建成功但响应里没有 access_token")
    returned_scopes = set(created.get("scopes") or [])
    if returned_scopes and returned_scopes != set(body["scopes"]):
        credential_id = created.get("credential_id")
        if credential_id:
            try:
                _api_json(chatgpt_api, "DELETE", f"/wham/auth-credentials/{credential_id}")
            except Exception:
                pass
        raise CodexPatExportError("v2 创建后返回的 scopes 与请求不一致")
    return {
        "token": created["access_token"],
        "token_version": "v2",
        "scopes": body["scopes"],
        "credential_id": created.get("credential_id"),
    }


def _encode_uint32(value: int) -> bytes:
    return int(value).to_bytes(4, "big")


def _generate_agent_identity_keypair() -> dict:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ed25519

    private_key = ed25519.Ed25519PrivateKey.generate()
    public_key = private_key.public_key()
    public_raw = public_key.public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    private_pkcs8 = private_key.private_bytes(
        encoding=serialization.Encoding.DER,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    prefix = b"ssh-ed25519"
    ssh_payload = _encode_uint32(len(prefix)) + prefix + _encode_uint32(len(public_raw)) + public_raw
    return {
        "public_key": f"ssh-ed25519 {_std_b64(ssh_payload)}",
        "private_key_pkcs8_der_base64": _std_b64(private_pkcs8),
    }


def _build_v1_agent_token(record: dict) -> str:
    header = {"alg": "none", "typ": "JWT"}
    payload = {
        "agent_runtime_id": record.get("agent_runtime_id"),
        "agent_private_key": record.get("private_key_pkcs8_der_base64"),
        "account_id": record.get("account_id"),
        "chatgpt_user_id": record.get("chatgpt_user_id"),
        "email": record.get("email"),
        "plan_type": record.get("plan_type"),
        "chatgpt_account_is_fedramp": record.get("chatgpt_account_is_fedramp"),
    }
    return ".".join(
        [
            _urlsafe_b64(json.dumps(header, separators=(",", ":")).encode("utf-8")),
            _urlsafe_b64(json.dumps(payload, separators=(",", ":")).encode("utf-8")),
            _urlsafe_b64(b"sig"),
        ]
    )


def _create_v1(chatgpt_api, *, name: str, ttl_seconds: int) -> dict:
    _api_json(chatgpt_api, "POST", "/wham/agent-identities/prepare-create", {})
    try:
        jwt_result = _api_json(chatgpt_api, "POST", "/wham/agent-identities/jwt", {"name": name, "ttl": ttl_seconds})
        if isinstance(jwt_result, dict) and jwt_result.get("agent_identity"):
            return {"token": jwt_result["agent_identity"], "token_version": "v1-jwt", "scopes": ["codex"]}
    except Exception as exc:
        logger.warning("[CodexPAT] 后端 JWT 分支不可用，回退本地 Ed25519: %s", exc)

    keypair = _generate_agent_identity_keypair()
    created = _api_json(
        chatgpt_api,
        "POST",
        "/wham/agent-identities",
        {"agent_public_key": keypair["public_key"], "name": name, "ttl": ttl_seconds},
    )
    if not isinstance(created, dict):
        raise CodexPatExportError("v1 创建返回格式无效")
    token = _build_v1_agent_token({**created, **keypair})
    return {
        "token": token,
        "token_version": "v1-local-jwt",
        "scopes": ["codex"],
        "agent_runtime_id": created.get("agent_runtime_id"),
    }


def _hydrate_metadata(personal_access_token: str, fallback: dict) -> dict:
    try:
        resp = requests.get(
            WHOAMI_URL,
            headers={"Authorization": f"Bearer {personal_access_token}", "Accept": "application/json"},
            timeout=20,
        )
        if resp.status_code == 200:
            data = resp.json()
            if isinstance(data, dict):
                return data
        logger.warning("[CodexPAT] whoami 返回 HTTP %s，使用 session 元数据", resp.status_code)
    except Exception as exc:
        logger.warning("[CodexPAT] whoami 失败，使用 session 元数据: %s", exc)
    return fallback


def build_export_json(session_access_token: str, personal_access_token: str, metadata: dict, ttl_seconds: int) -> dict:
    return {
        "access_token": session_access_token,
        "account_id": metadata.get("chatgpt_account_id") or metadata.get("account_id") or "",
        "disabled": False,
        "email": metadata.get("email") or "",
        "expired": _iso_after_seconds(ttl_seconds),
        "headers": {"authorization": f"Bearer {personal_access_token}"},
        "id_token": None,
        "last_refresh": _now_iso(),
        "refresh_token": None,
        "type": "codex",
        "websockets": True,
    }


def _safe_filename_email(email: str | None) -> str:
    value = str(email or "codex-auth").strip() or "codex-auth"
    return re.sub(r'[\\/:*?"<>|]+', "_", value)


def save_export_json(payload: dict, *, main: bool = False) -> Path:
    ensure_auth_dir()
    email = str(payload.get("email") or "").strip().lower()
    account_id = str(payload.get("account_id") or "").strip()
    if main:
        suffix = account_id or md5(email.encode("utf-8")).hexdigest()[:8]
        path = AUTH_DIR / f"codex-main-{suffix}.json"
        for old in AUTH_DIR.glob("codex-main-*.json"):
            if old != path and old.exists():
                old.unlink()
    else:
        hash_id = md5((account_id or email).encode("utf-8")).hexdigest()[:8] if (account_id or email) else "unknown"
        path = AUTH_DIR / f"codex-{_safe_filename_email(email)}-pat-{hash_id}.json"
        if email:
            for old in AUTH_DIR.glob(f"codex-{_safe_filename_email(email)}-*.json"):
                if old != path and old.exists():
                    old.unlink()
    write_text(path, json.dumps(payload, ensure_ascii=False, indent=2))
    ensure_auth_file_permissions(path)
    return path


def create_codex_auth_json_from_chatgpt_api(
    chatgpt_api,
    *,
    ttl_days: int | float | None = None,
    include_hermes: bool = False,
    token_name: str | None = None,
) -> dict:
    if not getattr(chatgpt_api, "access_token", None):
        raise CodexPatExportError("ChatGPT session 尚未取得 access_token，无法创建 Codex PAT")
    ttl = _ttl_seconds(ttl_days)
    metadata = _session_metadata(chatgpt_api)
    name = (token_name or _default_token_name(metadata.get("email"))).strip()
    try:
        creation = _create_v2(chatgpt_api, name=name, ttl_seconds=ttl, include_hermes=include_hermes)
    except Exception as exc:
        if _phone_verification_required(exc):
            raise CodexPatExportError("当前账号需要先完成 reauth / phone verification，无法自动创建 Codex PAT") from exc
        logger.warning("[CodexPAT] v2 创建失败，尝试 v1: %s", exc)
        creation = None
    if not creation:
        try:
            creation = _create_v1(chatgpt_api, name=name, ttl_seconds=ttl)
        except Exception as exc:
            if _phone_verification_required(exc):
                raise CodexPatExportError("当前账号需要先完成 reauth / phone verification，无法自动创建 Codex PAT") from exc
            raise
    hydrated = _hydrate_metadata(creation["token"], metadata)
    payload = build_export_json(chatgpt_api.access_token, creation["token"], hydrated, ttl)
    return {"payload": payload, "creation": creation, "metadata": hydrated, "ttl_seconds": ttl}


def create_save_upload_codex_auth_from_chatgpt_api(
    chatgpt_api,
    *,
    ttl_days: int | float | None = None,
    include_hermes: bool = False,
    token_name: str | None = None,
    upload: bool = True,
    main: bool = False,
) -> dict:
    result = create_codex_auth_json_from_chatgpt_api(
        chatgpt_api,
        ttl_days=ttl_days,
        include_hermes=include_hermes,
        token_name=token_name,
    )
    path = save_export_json(result["payload"], main=main)
    uploaded = False
    if upload:
        from autoteam.cpa_sync import upload_to_cpa

        uploaded = bool(upload_to_cpa(path))
        if not uploaded:
            raise CodexPatExportError(f"上传 CPA 失败: {path.name}")
    return {
        "auth_file": str(path),
        "uploaded": uploaded,
        "filename": path.name,
        "email": result["payload"].get("email"),
        "account_id": result["payload"].get("account_id"),
        "expired": result["payload"].get("expired"),
        "token_version": result["creation"].get("token_version"),
        "scopes": result["creation"].get("scopes"),
        "credential_id": result["creation"].get("credential_id"),
    }


class _PageBackedChatGPTAPI:
    """用已登录的 Playwright page 复用 ChatGPT 后端 API。"""

    def __init__(self, page, *, session: dict | None = None):
        self.page = page
        self.session = session or {}
        self.access_token = self.session.get("accessToken") or ""
        user = self.session.get("user") if isinstance(self.session.get("user"), dict) else {}
        self.email = user.get("email") or self.session.get("email") or ""
        account = self.session.get("account") if isinstance(self.session.get("account"), dict) else {}
        self.account_id = (
            self.session.get("account_id")
            or account.get("id")
            or account.get("account_id")
            or _auth_claims(self.access_token).get("chatgpt_account_id")
            or ""
        )

    def _api_fetch(self, method, path, body=None):
        normalized = _normalize_backend_path(path)
        return self.page.evaluate(
            """async ({method, path, body}) => {
                const resp = await fetch(path, {
                    method,
                    credentials: 'include',
                    headers: {'content-type': 'application/json', 'accept': 'application/json'},
                    body: body === null || body === undefined ? undefined : JSON.stringify(body),
                });
                const text = await resp.text();
                return {status: resp.status, body: text};
            }""",
            {"method": method, "path": normalized, "body": body},
        )


def _session_from_page(page) -> dict:
    result = page.evaluate(
        """async () => {
            const resp = await fetch('/api/auth/session', {credentials: 'include', headers: {'accept': 'application/json'}});
            const text = await resp.text();
            return {status: resp.status, body: text};
        }"""
    )
    status = int(result.get("status") or 0)
    if status < 200 or status >= 300:
        raise CodexPatExportError(f"获取 ChatGPT session 失败: HTTP {status}")
    try:
        data = json.loads(str(result.get("body") or "{}"))
    except Exception as exc:
        raise CodexPatExportError("ChatGPT session 返回非 JSON") from exc
    if not isinstance(data, dict) or not data.get("accessToken"):
        raise CodexPatExportError("ChatGPT session 缺少 accessToken")
    return data


def create_save_upload_codex_auth_from_page(
    page,
    *,
    ttl_days: int | float | None = None,
    include_hermes: bool = False,
    token_name: str | None = None,
    upload: bool = True,
    main: bool = False,
) -> dict:
    """从注册/登录后的 Playwright page 创建 Codex PAT 并保存/上传 CPA。"""
    session = _session_from_page(page)
    return create_save_upload_codex_auth_from_chatgpt_api(
        _PageBackedChatGPTAPI(page, session=session),
        ttl_days=ttl_days,
        include_hermes=include_hermes,
        token_name=token_name,
        upload=upload,
        main=main,
    )
