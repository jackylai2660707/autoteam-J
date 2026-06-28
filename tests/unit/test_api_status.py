import json
import logging
import os
import subprocess
import threading
import time

import pytest
from fastapi import HTTPException

from autoteam import accounts, api
from autoteam import config as app_config
from autoteam.cpa_config import normalize_cpa_url


def _set_pool_runtime_config(monkeypatch):
    monkeypatch.setattr(api, "_maybe_reload_runtime_config_from_env_file", lambda *args, **kwargs: False)
    monkeypatch.setattr("autoteam.setup_wizard._read_env", lambda: {})
    monkeypatch.setenv("CLOUDMAIL_BASE_URL", "http://mail.example.com")
    monkeypatch.setenv("CLOUDMAIL_EMAIL", "admin@example.com")
    monkeypatch.setenv("CLOUDMAIL_PASSWORD", "secret")
    monkeypatch.setenv("CLOUDMAIL_DOMAIN", "@example.com")
    monkeypatch.setenv("CPA_URL", "http://127.0.0.1:8317")
    monkeypatch.setenv("CPA_KEY", "key-1")


def test_empty_numeric_env_values_fall_back_to_defaults(monkeypatch):
    monkeypatch.setenv("SUB2API_CONCURRENCY", "")
    monkeypatch.setenv("SUB2API_RATE_MULTIPLIER", "")

    assert app_config._get_int_env("SUB2API_CONCURRENCY", 10) == 10
    assert app_config._get_float_env("SUB2API_RATE_MULTIPLIER", 1.0) == 1.0


def test_setup_save_unhandled_errors_return_json(monkeypatch):
    monkeypatch.setattr(api, "_save_runtime_config", lambda _data: (_ for _ in ()).throw(RuntimeError("boom")))

    result = api.post_setup_save(api.SetupConfig(API_KEY="new-key"))

    assert result.status_code == 500
    assert json.loads(result.body.decode("utf-8"))["message"] == "配置保存失败: boom"


def test_normalize_cpa_url_accepts_management_page_urls():
    assert normalize_cpa_url("https://api.example.com/management.html") == "https://api.example.com"
    assert normalize_cpa_url("https://api.example.com/management.html?token=abc") == "https://api.example.com"
    assert normalize_cpa_url("https://api.example.com/v0/management/auth-files") == "https://api.example.com"
    assert normalize_cpa_url("https://api.example.com/custom/management.html") == "https://api.example.com/custom"


def test_get_status_marks_main_account_without_live_quota(tmp_path, monkeypatch):
    main_email = "owner@example.com"
    auth_file = tmp_path / "codex-main.json"
    auth_file.write_text(json.dumps({"access_token": "token-main"}), encoding="utf-8")

    monkeypatch.setattr(
        "autoteam.accounts.load_accounts",
        lambda: [
            {
                "email": main_email,
                "status": "exhausted",
                "auth_file": "/app/auths/codex-main.json",
                "last_quota": {
                    "primary_pct": 8,
                    "primary_resets_at": 1710000000,
                    "weekly_pct": 1,
                    "weekly_resets_at": 1710600000,
                },
            }
        ],
    )
    monkeypatch.setattr(api, "_is_main_account_email", lambda email: email == main_email)
    monkeypatch.setattr("autoteam.codex_auth.get_saved_main_auth_file", lambda: str(auth_file))
    monkeypatch.setattr(
        "autoteam.codex_auth.check_codex_quota",
        lambda access_token: (_ for _ in ()).throw(AssertionError("status must not check main-account quota")),
    )

    result = api.get_status()

    assert result["quota_cache"] == {}
    assert result["accounts"][0]["is_main_account"] is True
    assert result["accounts"][0]["status"] == "active"
    assert result["summary"] == {
        "active": 1,
        "auth_pending": 0,
        "standby": 0,
        "exhausted": 0,
        "pending": 0,
        "disabled": 0,
        "total": 1,
    }


def test_sanitize_account_keeps_exportable_main_account_active_without_live_quota(tmp_path, monkeypatch):
    main_email = "owner@example.com"
    auth_file = tmp_path / "codex-main.json"
    auth_file.write_text("{}", encoding="utf-8")

    monkeypatch.setattr(api, "_is_main_account_email", lambda email: email == main_email)
    monkeypatch.setattr("autoteam.codex_auth.get_saved_main_auth_file", lambda: str(auth_file))

    sanitized = api._sanitize_account(
        {"email": main_email, "status": "exhausted", "auth_file": "/app/auths/missing.json"}
    )

    assert sanitized["is_main_account"] is True
    assert sanitized["status"] == "active"


def test_sanitize_account_masks_disabled_non_main_status(monkeypatch):
    monkeypatch.setattr(api, "_is_main_account_email", lambda _email: False)

    sanitized = api._sanitize_account({"email": "user@example.com", "status": "active", "disabled": True})

    assert sanitized["raw_status"] == "active"
    assert sanitized["status"] == "disabled"
    assert sanitized["disabled"] is True


def test_toggle_accounts_disabled_treats_string_false_as_enabled(tmp_path, monkeypatch):
    accounts_file = tmp_path / "accounts.json"
    monkeypatch.setattr(accounts, "ACCOUNTS_FILE", accounts_file)
    monkeypatch.setattr(api, "_is_main_account_email", lambda _email: False)
    accounts_file.write_text(
        '[{"email":"member@example.com","status":"active","disabled":"false"}]',
        encoding="utf-8",
    )

    result = api._toggle_accounts_disabled(["member@example.com"], True)

    stored = accounts.load_accounts()[0]
    assert result["updated_emails"] == ["member@example.com"]
    assert result["unchanged_emails"] == []
    assert stored["disabled"] is True


def test_disable_and_enable_account_endpoints_are_disabled(tmp_path, monkeypatch):
    accounts_file = tmp_path / "accounts.json"
    monkeypatch.setattr(accounts, "ACCOUNTS_FILE", accounts_file)
    monkeypatch.setattr(accounts, "get_admin_email", lambda: "owner@example.com")
    monkeypatch.setattr(api, "_is_main_account_email", lambda email: email == "owner@example.com")

    accounts.save_accounts(
        [
            {"email": "member@example.com", "status": "standby", "disabled": False},
            {"email": "owner@example.com", "status": "active", "disabled": False},
        ]
    )

    with pytest.raises(HTTPException) as disabled_exc:
        api.post_disable_account("member@example.com")
    with pytest.raises(HTTPException) as enabled_exc:
        api.post_enable_account("member@example.com")

    assert disabled_exc.value.status_code == 410
    assert enabled_exc.value.status_code == 410
    assert accounts.find_account(accounts.load_accounts(), "member@example.com")["disabled"] is False


def test_bulk_disable_accounts_endpoint_is_disabled(tmp_path, monkeypatch):
    accounts_file = tmp_path / "accounts.json"
    monkeypatch.setattr(accounts, "ACCOUNTS_FILE", accounts_file)
    monkeypatch.setattr(accounts, "get_admin_email", lambda: "owner@example.com")
    monkeypatch.setattr(api, "_is_main_account_email", lambda email: email == "owner@example.com")

    accounts.save_accounts(
        [
            {"email": "first@example.com", "status": "standby", "disabled": False},
            {"email": "second@example.com", "status": "active", "disabled": False},
            {"email": "already@example.com", "status": "standby", "disabled": True},
            {"email": "owner@example.com", "status": "active", "disabled": False},
        ]
    )

    with pytest.raises(HTTPException) as exc:
        api.post_bulk_disable_accounts(
            api.BulkAccountDisableParams(
                emails=[
                    "first@example.com",
                    "second@example.com",
                    "already@example.com",
                    "owner@example.com",
                    "missing@example.com",
                    "first@example.com",
                ]
            )
        )

    assert exc.value.status_code == 410
    stored = {acc["email"]: acc for acc in accounts.load_accounts()}
    assert stored["first@example.com"]["disabled"] is False
    assert stored["second@example.com"]["disabled"] is False
    assert stored["already@example.com"]["disabled"] is True
    assert stored["owner@example.com"]["disabled"] is False


def test_bulk_enable_accounts_endpoint_is_disabled(tmp_path, monkeypatch):
    accounts_file = tmp_path / "accounts.json"
    monkeypatch.setattr(accounts, "ACCOUNTS_FILE", accounts_file)
    monkeypatch.setattr(accounts, "get_admin_email", lambda: "owner@example.com")
    monkeypatch.setattr(api, "_is_main_account_email", lambda email: email == "owner@example.com")

    accounts.save_accounts(
        [
            {"email": "first@example.com", "status": "standby", "disabled": True},
            {"email": "second@example.com", "status": "active", "disabled": True},
            {"email": "already@example.com", "status": "standby", "disabled": False},
            {"email": "owner@example.com", "status": "active", "disabled": False},
        ]
    )

    with pytest.raises(HTTPException) as exc:
        api.post_bulk_enable_accounts(
            api.BulkAccountDisableParams(
                emails=[
                    "first@example.com",
                    "second@example.com",
                    "already@example.com",
                    "owner@example.com",
                    "missing@example.com",
                ]
            )
        )

    assert exc.value.status_code == 410
    stored = {acc["email"]: acc for acc in accounts.load_accounts()}
    assert stored["first@example.com"]["disabled"] is True
    assert stored["second@example.com"]["disabled"] is True
    assert stored["already@example.com"]["disabled"] is False
    assert stored["owner@example.com"]["disabled"] is False


def test_get_status_counts_disabled_and_skips_live_quota_checks(tmp_path, monkeypatch):
    enabled_auth = tmp_path / "enabled.json"
    disabled_auth = tmp_path / "disabled.json"
    enabled_auth.write_text(json.dumps({"access_token": "token-enabled"}), encoding="utf-8")
    disabled_auth.write_text(json.dumps({"access_token": "token-disabled"}), encoding="utf-8")

    monkeypatch.setattr(
        "autoteam.accounts.load_accounts",
        lambda: [
            {"email": "enabled@example.com", "status": "active", "auth_file": str(enabled_auth), "disabled": False},
            {"email": "disabled@example.com", "status": "active", "auth_file": str(disabled_auth), "disabled": True},
        ],
    )
    monkeypatch.setattr(api, "_is_main_account_email", lambda _email: False)

    def fake_check_quota(access_token):
        raise AssertionError(f"status must not check quota for {access_token}")

    monkeypatch.setattr("autoteam.codex_auth.check_codex_quota", fake_check_quota)

    result = api.get_status()

    assert result["quota_cache"] == {}
    assert {item["email"]: item["status"] for item in result["accounts"]} == {
        "enabled@example.com": "active",
        "disabled@example.com": "disabled",
    }
    assert result["summary"] == {
        "active": 1,
        "auth_pending": 0,
        "standby": 0,
        "exhausted": 0,
        "pending": 0,
        "disabled": 1,
        "total": 2,
    }


def test_post_setup_save_only_requires_api_key_and_generates_one(monkeypatch):
    written = {}

    def fake_write_env(key, value):
        written[key] = value

    monkeypatch.setattr("autoteam.setup_wizard._write_env", fake_write_env)
    monkeypatch.setattr("autoteam.setup_wizard._verify_mail_provider", lambda provider=None: True)
    monkeypatch.setattr("autoteam.setup_wizard._verify_cpa", lambda: True)
    monkeypatch.setattr("secrets.token_urlsafe", lambda _n: "generated-token")
    monkeypatch.setattr("importlib.reload", lambda module: module)
    monkeypatch.setattr(api, "API_KEY", "")
    monkeypatch.delenv("CPA_URL", raising=False)
    monkeypatch.delenv("CLOUDMAIL_BASE_URL", raising=False)
    monkeypatch.delenv("CLOUDMAIL_EMAIL", raising=False)
    monkeypatch.delenv("CLOUDMAIL_PASSWORD", raising=False)
    monkeypatch.delenv("CLOUDMAIL_DOMAIN", raising=False)
    monkeypatch.delenv("API_KEY", raising=False)

    result = api.post_setup_save(
        api.SetupConfig(
            CLOUDMAIL_BASE_URL="http://mail.example.com",
            CLOUDMAIL_EMAIL="admin@example.com",
            CLOUDMAIL_PASSWORD="secret",
            CLOUDMAIL_DOMAIN="@example.com",
            CPA_URL="",
            CPA_KEY="key-1",
            PLAYWRIGHT_PROXY_URL="",
            PLAYWRIGHT_PROXY_BYPASS="",
            API_KEY="",
        )
    )

    assert written["API_KEY"] == "generated-token"
    assert result["api_key"] == "generated-token"
    assert api.API_KEY == "generated-token"
    assert "CPA_URL" not in written


def test_get_setup_status_only_requires_api_key(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text("", encoding="utf-8")

    monkeypatch.setattr("autoteam.setup_wizard.ENV_FILE", env_file)
    monkeypatch.setattr("autoteam.setup_wizard.ENV_EXAMPLE", tmp_path / ".env.example")
    for key in ("API_KEY", "CLOUDMAIL_BASE_URL", "CPA_KEY"):
        monkeypatch.delenv(key, raising=False)

    result = api.get_setup_status()

    assert result["configured"] is False
    assert result["fields"] == [
        {
            "key": "API_KEY",
            "prompt": "API 鉴权密钥（回车自动生成）",
            "default": "",
            "optional": False,
            "configured": False,
        }
    ]


def test_get_runtime_config_returns_current_values_from_env_file(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            [
                "CLOUDMAIL_BASE_URL=http://mail.example.com",
                "CLOUDMAIL_EMAIL=admin@example.com",
                "CLOUDMAIL_PASSWORD=secret",
                "CLOUDMAIL_DOMAIN=@example.com",
                "CPA_URL=http://127.0.0.1:8317",
                "CPA_KEY=key-1",
                "SUB2API_CONCURRENCY=12",
                "SUB2API_PROXY=Residential Pool",
                "SUB2API_OPENAI_WS_MODE=ctx_pool",
                "SUB2API_OPENAI_PASSTHROUGH=true",
                "PLAYWRIGHT_PROXY_URL=socks5://127.0.0.1:1080",
                "PLAYWRIGHT_PROXY_BYPASS=localhost,127.0.0.1",
                "API_KEY=runtime-key",
            ]
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr("autoteam.setup_wizard.ENV_FILE", env_file)
    for key in (
        "CLOUDMAIL_BASE_URL",
        "CLOUDMAIL_EMAIL",
        "CLOUDMAIL_PASSWORD",
        "CLOUDMAIL_DOMAIN",
        "CPA_URL",
        "CPA_KEY",
        "SUB2API_CONCURRENCY",
        "SUB2API_PROXY",
        "SUB2API_OPENAI_WS_MODE",
        "SUB2API_OPENAI_PASSTHROUGH",
        "PLAYWRIGHT_PROXY_URL",
        "PLAYWRIGHT_PROXY_BYPASS",
        "API_KEY",
    ):
        monkeypatch.delenv(key, raising=False)

    result = api.get_runtime_config()
    fields = {field["key"]: field for field in result["fields"]}

    assert result["configured"] is True
    assert fields["CLOUDMAIL_EMAIL"]["value"] == "admin@example.com"
    assert fields["CLOUDMAIL_EMAIL"]["runtime_required"] is True
    assert fields["CPA_KEY"]["value"] == "key-1"
    assert fields["CPA_KEY"]["runtime_required"] is True
    assert fields["SUB2API_CONCURRENCY"]["value"] == "12"
    assert fields["SUB2API_CONCURRENCY"]["runtime_required"] is False
    assert fields["SUB2API_PROXY"]["value"] == "Residential Pool"
    assert fields["SUB2API_PROXY"]["runtime_required"] is False
    assert fields["SUB2API_OPENAI_WS_MODE"]["value"] == "ctx_pool"
    assert fields["SUB2API_OPENAI_PASSTHROUGH"]["value"] == "true"
    assert fields["PLAYWRIGHT_PROXY_URL"]["value"] == "socks5://127.0.0.1:1080"
    assert fields["PLAYWRIGHT_PROXY_URL"]["runtime_required"] is False
    assert fields["PLAYWRIGHT_PROXY_BYPASS"]["value"] == "localhost,127.0.0.1"
    assert fields["API_KEY"]["value"] == "runtime-key"
    assert fields["API_KEY"]["runtime_required"] is True


def test_get_runtime_config_switches_required_mail_fields_by_provider(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            [
                "MAIL_PROVIDER=cloudflare_temp_email",
                "CF_TEMP_EMAIL_BASE_URL=https://tempmail.example.com/admin",
                "CF_TEMP_EMAIL_ADMIN_PASSWORD=secret",
                "CF_TEMP_EMAIL_DOMAIN=example.com",
                "API_KEY=runtime-key",
            ]
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr("autoteam.setup_wizard.ENV_FILE", env_file)
    for key in (
        "MAIL_PROVIDER",
        "CF_TEMP_EMAIL_BASE_URL",
        "CF_TEMP_EMAIL_ADMIN_PASSWORD",
        "CF_TEMP_EMAIL_DOMAIN",
        "CLOUDMAIL_BASE_URL",
        "CLOUDMAIL_EMAIL",
        "CLOUDMAIL_PASSWORD",
        "CLOUDMAIL_DOMAIN",
        "API_KEY",
    ):
        monkeypatch.delenv(key, raising=False)

    result = api.get_runtime_config()
    fields = {field["key"]: field for field in result["fields"]}

    assert result["configured"] is True
    assert fields["MAIL_PROVIDER"]["value"] == "cloudflare_temp_email"
    assert fields["CF_TEMP_EMAIL_BASE_URL"]["runtime_required"] is True
    assert fields["CF_TEMP_EMAIL_ADMIN_PASSWORD"]["runtime_required"] is True
    assert fields["CF_TEMP_EMAIL_DOMAIN"]["runtime_required"] is True
    assert fields["CLOUDMAIL_BASE_URL"]["runtime_required"] is False
    assert fields["CLOUDMAIL_EMAIL"]["runtime_required"] is False


def test_get_runtime_config_exposes_structured_mail_services(tmp_path, monkeypatch):
    services = [
        {
            "id": "cm-1",
            "type": "cloudmail",
            "base_url": "https://mail.example.com/api",
            "email": "admin@example.com",
            "password": "secret-1",
            "domain": "pool.example.com",
        },
        {
            "id": "cf-1",
            "type": "cloudflare_temp_email",
            "base_url": "https://temp.example.com",
            "admin_password": "secret-2",
            "domain": "mail.example.com",
        },
    ]
    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            [
                f"MAIL_SERVICES_JSON={json.dumps(services, separators=(',', ':'))}",
                "MAIL_SERVICE_DEFAULT=cf-1",
                "API_KEY=runtime-key",
            ]
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr("autoteam.setup_wizard.ENV_FILE", env_file)
    for key in (
        "MAIL_SERVICES_JSON",
        "MAIL_SERVICE_DEFAULT",
        "MAIL_PROVIDER",
        "CLOUDMAIL_BASE_URL",
        "CLOUDMAIL_EMAIL",
        "CLOUDMAIL_PASSWORD",
        "CLOUDMAIL_DOMAIN",
        "CF_TEMP_EMAIL_BASE_URL",
        "CF_TEMP_EMAIL_ADMIN_PASSWORD",
        "CF_TEMP_EMAIL_DOMAIN",
        "API_KEY",
    ):
        monkeypatch.delenv(key, raising=False)

    result = api.get_runtime_config()

    assert [item["id"] for item in result["mail_services"]] == ["cm-1", "cf-1"]
    assert result["mail_service_default"] == "cf-1"
    fields = {field["key"]: field for field in result["fields"]}
    assert fields["MAIL_PROVIDER"]["value"] == "cloudflare_temp_email"


def test_put_runtime_config_saves_structured_mail_services_and_mirrors_default(monkeypatch):
    written = {}

    services = [
        {
            "id": "cm-1",
            "type": "cloudmail",
            "base_url": "https://mail.example.com/api",
            "email": "admin@example.com",
            "password": "secret-1",
            "domain": "pool.example.com",
        },
        {
            "id": "cf-1",
            "type": "cloudflare_temp_email",
            "base_url": "https://temp.example.com",
            "admin_password": "secret-2",
            "domain": "mail.example.com",
        },
    ]

    monkeypatch.setattr("autoteam.setup_wizard._write_env", lambda key, value: written.__setitem__(key, value))
    monkeypatch.setattr("autoteam.setup_wizard._verify_mail_service", lambda service=None: True)
    monkeypatch.setattr("importlib.reload", lambda module: module)
    monkeypatch.setattr(api, "API_KEY", "old-key")
    monkeypatch.setenv("API_KEY", "old-key")
    monkeypatch.setenv("CLOUDMAIL_BASE_URL", "https://old-mail.example.com/api")
    monkeypatch.setenv("CLOUDMAIL_EMAIL", "old@example.com")
    monkeypatch.setenv("CLOUDMAIL_PASSWORD", "old-secret")
    monkeypatch.setenv("CLOUDMAIL_DOMAIN", "@old.example.com")

    result = api.put_runtime_config(
        api.SetupConfig(
            API_KEY="old-key",
            mail_services=services,
            mail_service_default="cf-1",
        )
    )

    assert result["message"] == "配置保存成功"
    assert json.loads(written["MAIL_SERVICES_JSON"])[1]["id"] == "cf-1"
    assert written["MAIL_SERVICE_DEFAULT"] == "cf-1"
    assert written["MAIL_PROVIDER"] == "cloudflare_temp_email"
    assert written["CF_TEMP_EMAIL_BASE_URL"] == "https://temp.example.com"
    assert written["CF_TEMP_EMAIL_ADMIN_PASSWORD"] == "secret-2"
    assert written["CF_TEMP_EMAIL_DOMAIN"] == "mail.example.com"
    assert written["CLOUDMAIL_BASE_URL"] == ""
    assert written["CLOUDMAIL_EMAIL"] == ""
    assert written["CLOUDMAIL_PASSWORD"] == ""
    assert written["CLOUDMAIL_DOMAIN"] == ""


def test_put_runtime_config_reports_structured_mail_service_verification_reason(monkeypatch):
    services = [
        {
            "id": "cf-1",
            "type": "cloudflare_temp_email",
            "base_url": "https://temp.example.com",
            "admin_password": "wrong-secret",
            "domain": "mail.example.com",
        }
    ]

    def fake_verify_mail_service(service=None, *, raise_errors=False):
        if raise_errors:
            raise RuntimeError("登录失败: HTTP 401 unauthorized")
        return False

    monkeypatch.setattr("autoteam.setup_wizard._write_env", lambda key, value: None)
    monkeypatch.setattr("autoteam.setup_wizard._verify_mail_service", fake_verify_mail_service)
    monkeypatch.setattr("importlib.reload", lambda module: module)
    monkeypatch.setattr(api, "API_KEY", "old-key")
    monkeypatch.setenv("API_KEY", "old-key")

    result = api.put_runtime_config(
        api.SetupConfig(
            API_KEY="old-key",
            mail_services=services,
            mail_service_default="cf-1",
        )
    )

    assert result.status_code == 400
    message = json.loads(result.body.decode("utf-8"))["message"]
    assert "Cloudflare Temp Email (mail.example.com)" in message
    assert "HTTP 401 unauthorized" in message
    assert "api_key" not in json.loads(result.body.decode("utf-8"))


def test_put_runtime_config_reports_cpa_verification_reason(monkeypatch):
    def fake_verify_cpa(*, raise_errors=False):
        if raise_errors:
            raise RuntimeError("CPA 密钥无效 (401)，请检查 CPA_KEY")
        return False

    monkeypatch.setattr("autoteam.setup_wizard._write_env", lambda key, value: None)
    monkeypatch.setattr("autoteam.setup_wizard._verify_cpa", fake_verify_cpa)
    monkeypatch.setattr("importlib.reload", lambda module: module)
    monkeypatch.setattr(api, "API_KEY", "old-key")
    monkeypatch.setenv("API_KEY", "old-key")

    result = api.put_runtime_config(
        api.SetupConfig(
            API_KEY="old-key",
            SYNC_TARGET_CPA="true",
            CPA_URL="http://cpa.example.com",
            CPA_KEY="bad-key",
        )
    )

    assert result.status_code == 400
    assert json.loads(result.body.decode("utf-8"))["message"] == "CPA: CPA 密钥无效 (401)，请检查 CPA_KEY"


def test_put_runtime_config_normalizes_cpa_management_page_url(monkeypatch):
    written = {}

    monkeypatch.setattr("autoteam.setup_wizard._write_env", lambda key, value: written.__setitem__(key, value))
    monkeypatch.setattr("autoteam.setup_wizard._verify_cpa", lambda *args, **kwargs: True)
    monkeypatch.setattr("importlib.reload", lambda module: module)
    monkeypatch.setattr(api, "API_KEY", "old-key")
    monkeypatch.setenv("API_KEY", "old-key")

    result = api.put_runtime_config(
        api.SetupConfig(
            API_KEY="old-key",
            SYNC_TARGET_CPA="true",
            CPA_URL="https://api.example.com/management.html",
            CPA_KEY="manager-password",
        )
    )

    assert result["message"] == "配置保存成功"
    assert written["CPA_URL"] == "https://api.example.com"


def test_put_runtime_config_allows_partial_runtime_fields_when_api_key_exists(monkeypatch):
    written = {}

    def fake_write_env(key, value):
        written[key] = value

    monkeypatch.setattr("autoteam.setup_wizard._write_env", fake_write_env)
    monkeypatch.setattr(
        "autoteam.setup_wizard._verify_mail_provider",
        lambda provider=None: (_ for _ in ()).throw(AssertionError("mail provider verify should not run")),
    )
    monkeypatch.setattr(
        "autoteam.setup_wizard._verify_cpa",
        lambda: (_ for _ in ()).throw(AssertionError("cpa verify should not run")),
    )
    monkeypatch.setattr("importlib.reload", lambda module: module)
    monkeypatch.setattr(api, "API_KEY", "old-key")

    monkeypatch.setenv("API_KEY", "old-key")
    monkeypatch.delenv("CLOUDMAIL_BASE_URL", raising=False)
    monkeypatch.delenv("CLOUDMAIL_EMAIL", raising=False)
    monkeypatch.delenv("CLOUDMAIL_PASSWORD", raising=False)
    monkeypatch.delenv("CLOUDMAIL_DOMAIN", raising=False)
    monkeypatch.delenv("CPA_URL", raising=False)
    monkeypatch.delenv("CPA_KEY", raising=False)

    result = api.put_runtime_config(
        api.SetupConfig(
            CLOUDMAIL_BASE_URL="",
            CLOUDMAIL_EMAIL="",
            CLOUDMAIL_PASSWORD="",
            CLOUDMAIL_DOMAIN="",
            CPA_URL="",
            CPA_KEY="",
            PLAYWRIGHT_PROXY_URL="",
            PLAYWRIGHT_PROXY_BYPASS="",
            API_KEY="old-key",
        )
    )

    assert result["message"] == "配置保存成功"
    assert written["API_KEY"] == "old-key"
    assert "CPA_URL" not in written


def test_put_runtime_config_accepts_numeric_sub2api_fields(monkeypatch):
    written = {}

    def fake_write_env(key, value):
        written[key] = value

    monkeypatch.setattr("autoteam.setup_wizard._write_env", fake_write_env)
    monkeypatch.setattr(
        "autoteam.setup_wizard._verify_mail_provider",
        lambda provider=None: (_ for _ in ()).throw(AssertionError("mail provider verify should not run")),
    )
    monkeypatch.setattr(
        "autoteam.setup_wizard._verify_cpa",
        lambda: (_ for _ in ()).throw(AssertionError("cpa verify should not run")),
    )
    monkeypatch.setattr("importlib.reload", lambda module: module)
    monkeypatch.setattr(api, "API_KEY", "old-key")
    monkeypatch.setenv("API_KEY", "old-key")

    result = api.put_runtime_config(
        api.SetupConfig(
            API_KEY="old-key",
            SUB2API_CONCURRENCY=15,
            SUB2API_PROXY="Residential Pool",
            SUB2API_PRIORITY=2,
            SUB2API_RATE_MULTIPLIER=1.5,
            SUB2API_AUTO_PAUSE_ON_EXPIRED=True,
            SUB2API_OPENAI_PASSTHROUGH=False,
            SUB2API_OVERWRITE_ACCOUNT_SETTINGS=True,
        )
    )

    assert result["message"] == "配置保存成功"
    assert written["SUB2API_CONCURRENCY"] == "15"
    assert written["SUB2API_PROXY"] == "Residential Pool"
    assert written["SUB2API_PRIORITY"] == "2"
    assert written["SUB2API_RATE_MULTIPLIER"] == "1.5"
    assert written["SUB2API_AUTO_PAUSE_ON_EXPIRED"] == "true"
    assert written["SUB2API_OPENAI_PASSTHROUGH"] == "false"
    assert written["SUB2API_OVERWRITE_ACCOUNT_SETTINGS"] == "true"


def test_put_runtime_config_accepts_numeric_sub2api_proxy(monkeypatch):
    written = {}

    monkeypatch.setattr("autoteam.setup_wizard._write_env", lambda key, value: written.setdefault(key, value))
    monkeypatch.setattr("importlib.reload", lambda module: module)
    monkeypatch.setattr(api, "API_KEY", "old-key")
    monkeypatch.setenv("API_KEY", "old-key")

    result = api.put_runtime_config(api.SetupConfig(API_KEY="old-key", SUB2API_PROXY=15))

    assert result["message"] == "配置保存成功"
    assert written["SUB2API_PROXY"] == "15"


def test_put_runtime_config_disabling_cpa_skips_stale_cpa_validation(monkeypatch):
    written = {}

    monkeypatch.setattr("autoteam.setup_wizard._write_env", lambda key, value: written.setdefault(key, value))
    monkeypatch.setattr(
        "autoteam.setup_wizard._read_env",
        lambda: {
            "SYNC_TARGET_CPA": "true",
            "CPA_URL": "http://127.0.0.1:8317",
            "CPA_KEY": "old-key",
            "API_KEY": "old-key",
        },
    )
    monkeypatch.setattr("autoteam.setup_wizard._verify_mail_provider", lambda provider=None: True)
    monkeypatch.setattr(
        "autoteam.setup_wizard._verify_cpa",
        lambda: (_ for _ in ()).throw(AssertionError("cpa verify should not run after disabling cpa sync")),
    )
    monkeypatch.setattr("importlib.reload", lambda module: module)
    monkeypatch.setattr(api, "API_KEY", "old-key")
    monkeypatch.setenv("API_KEY", "old-key")

    result = api.put_runtime_config(
        api.SetupConfig(
            API_KEY="old-key",
            SYNC_TARGET_CPA="false",
            CPA_URL="http://127.0.0.1:8317",
            CPA_KEY="old-key",
        )
    )

    assert result["message"] == "配置保存成功"
    assert written["SYNC_TARGET_CPA"] == "false"


def test_post_account_login_endpoint_is_disabled():
    with pytest.raises(HTTPException) as exc:
        api.post_account_login(api.LoginAccountParams(email="user@example.com"))
    assert exc.value.status_code == 410


def test_get_auto_check_config_includes_target_seats(monkeypatch):
    monkeypatch.setattr(
        api,
        "_auto_check_config",
        {
            "interval": 300,
            "target_seats": 7,
            "replace_with_pending_invite": True,
            "replace_mode": "create_invite",
            "threshold": 10,
            "min_low": 2,
            "retry_add_phone": True,
            "add_phone_max_retries": 3,
        },
    )

    assert api.get_auto_check_config() == {
        "interval": 300,
        "target_seats": 5,
        "replace_with_pending_invite": True,
        "replace_mode": "create_invite",
        "max_chatgpt_active": 5,
        "min_chatgpt_active": 1,
        "max_allowed_chatgpt_active": 5,
        "threshold": 10,
        "min_low": 2,
        "retry_add_phone": True,
        "add_phone_max_retries": 3,
    }


def test_set_auto_check_config_persists_values_to_env(monkeypatch):
    written = {}
    restart_event = threading.Event()
    sync_calls = []

    monkeypatch.setattr("autoteam.setup_wizard._write_env", lambda key, value: written.setdefault(key, value))
    monkeypatch.setattr(
        api,
        "_auto_check_config",
        {
            "interval": 300,
            "target_seats": 5,
            "replace_with_pending_invite": False,
            "replace_mode": "pending_invite",
            "threshold": 10,
            "min_low": 2,
            "retry_add_phone": True,
            "add_phone_max_retries": 3,
        },
    )
    monkeypatch.setattr(api, "_auto_check_restart", restart_event)
    monkeypatch.setattr(api, "_sync_runtime_env_reload_state", lambda: sync_calls.append("synced"))

    result = api.set_auto_check_config(
        api.AutoCheckConfig(
            interval=420,
            target_seats=6,
            replace_with_pending_invite=True,
            replace_mode="create-invite",
            threshold=15,
            min_low=3,
            retry_add_phone=False,
            add_phone_max_retries=5,
        )
    )

    assert result == {
        "interval": 420,
        "target_seats": 5,
        "replace_with_pending_invite": True,
        "replace_mode": "create_invite",
        "max_chatgpt_active": 5,
        "min_chatgpt_active": 1,
        "max_allowed_chatgpt_active": 5,
        "threshold": 15,
        "min_low": 3,
        "retry_add_phone": False,
        "add_phone_max_retries": 5,
    }
    assert written == {
        "AUTO_CHECK_INTERVAL": "420",
        "AUTO_CHECK_TARGET_SEATS": "5",
        "AUTO_CHECK_REPLACE_WITH_PENDING_INVITE": "true",
        "AUTO_CHECK_REPLACE_MODE": "create_invite",
        "AUTO_CHECK_THRESHOLD": "15",
        "AUTO_CHECK_MIN_LOW": "3",
        "AUTO_CHECK_RETRY_ADD_PHONE": "false",
        "AUTO_CHECK_ADD_PHONE_MAX_RETRIES": "5",
    }
    assert restart_event.is_set() is True
    assert sync_calls == ["synced"]
    assert os.environ["AUTO_CHECK_INTERVAL"] == "420"
    assert os.environ["AUTO_CHECK_TARGET_SEATS"] == "5"
    assert os.environ["AUTO_CHECK_REPLACE_WITH_PENDING_INVITE"] == "true"
    assert os.environ["AUTO_CHECK_REPLACE_MODE"] == "create_invite"
    assert os.environ["AUTO_CHECK_THRESHOLD"] == "15"
    assert os.environ["AUTO_CHECK_MIN_LOW"] == "3"
    assert os.environ["AUTO_CHECK_RETRY_ADD_PHONE"] == "false"
    assert os.environ["AUTO_CHECK_ADD_PHONE_MAX_RETRIES"] == "5"


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ({"SUB2API_CONCURRENCY": "0"}, "SUB2API_CONCURRENCY 必须是正整数"),
        ({"SUB2API_PROXY": "0"}, "SUB2API_PROXY 必须是 Sub2API 代理 ID（正整数）或代理名称"),
        ({"SUB2API_PROXY": "-1"}, "SUB2API_PROXY 必须是 Sub2API 代理 ID（正整数）或代理名称"),
        ({"SUB2API_RATE_MULTIPLIER": "0"}, "SUB2API_RATE_MULTIPLIER 必须是大于 0 的数字"),
        ({"SUB2API_AUTO_PAUSE_ON_EXPIRED": "maybe"}, "SUB2API_AUTO_PAUSE_ON_EXPIRED 必须是 true 或 false"),
        ({"SUB2API_OPENAI_WS_MODE": "socket"}, "SUB2API_OPENAI_WS_MODE 必须是 off、ctx_pool 或 passthrough"),
    ],
)
def test_put_runtime_config_rejects_invalid_sub2api_default_settings(monkeypatch, payload, message):
    monkeypatch.setattr("autoteam.setup_wizard._write_env", lambda key, value: None)
    monkeypatch.setattr("importlib.reload", lambda module: module)
    monkeypatch.setattr(api, "API_KEY", "old-key")
    monkeypatch.setenv("API_KEY", "old-key")

    result = api.put_runtime_config(api.SetupConfig(API_KEY="old-key", **payload))

    assert result.status_code == 400
    assert json.loads(result.body.decode("utf-8"))["message"] == message


def test_get_runtime_config_source_returns_env_content(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text("CLOUDMAIL_EMAIL=admin@example.com\nAPI_KEY=test-key\n", encoding="utf-8")

    monkeypatch.setattr("autoteam.setup_wizard.ENV_FILE", env_file)
    monkeypatch.setattr("autoteam.setup_wizard.ENV_EXAMPLE", tmp_path / ".env.example")

    result = api.get_runtime_config_source()

    assert result["path"].endswith(".env")
    assert "CLOUDMAIL_EMAIL=admin@example.com" in result["content"]
    assert "API_KEY=test-key" in result["content"]


def test_runtime_env_file_hot_reload_updates_current_process_without_restart(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            [
                "CPA_URL=http://100.78.125.121:8317",
                "API_KEY=new-key",
            ]
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr("autoteam.setup_wizard.ENV_FILE", env_file)
    monkeypatch.setattr(
        api,
        "_RUNTIME_ENV_BASE",
        {
            "CPA_URL": "http://127.0.0.1:8317",
            "CPA_KEY": "external-key",
            "API_KEY": "old-key",
        },
    )
    monkeypatch.setattr(api, "_runtime_env_reload_state", {"signature": None})
    monkeypatch.setattr(api, "_reload_runtime_config_modules", lambda: None)
    monkeypatch.setattr(
        api, "_sync_runtime_globals", lambda: setattr(api, "API_KEY", api.os.environ.get("API_KEY", ""))
    )

    monkeypatch.setenv("CPA_URL", "http://127.0.0.1:8317")
    monkeypatch.setenv("CPA_KEY", "external-key")
    monkeypatch.setenv("API_KEY", "old-key")

    changed = api._maybe_reload_runtime_config_from_env_file(force=True)

    assert changed is True
    assert api.os.environ["CPA_URL"] == "http://100.78.125.121:8317"
    assert api.os.environ["CPA_KEY"] == "external-key"
    assert api.API_KEY == "new-key"


@pytest.mark.parametrize(
    ("endpoint", "args", "action_label"),
    [
        ("post_check", (), "检查并切换 seat"),
        ("post_rotate", (api.TaskParams(target=5),), "swap_seat"),
        ("post_swap_seats", (api.SwapSeatParams(),), "swap_seat"),
    ],
)
def test_swap_task_endpoints_require_cpa_config(monkeypatch, endpoint, args, action_label):
    monkeypatch.setattr("autoteam.setup_wizard._read_env", lambda: {})
    for key in (
        "MAIL_PROVIDER",
        "CLOUDMAIL_BASE_URL",
        "CLOUDMAIL_EMAIL",
        "CLOUDMAIL_PASSWORD",
        "CLOUDMAIL_DOMAIN",
        "CPA_URL",
        "CPA_KEY",
    ):
        monkeypatch.delenv(key, raising=False)

    with pytest.raises(HTTPException) as exc:
        getattr(api, endpoint)(*args)

    assert exc.value.status_code == 400
    assert action_label in exc.value.detail
    assert "配置面板" in exc.value.detail
    assert "CPA_URL" in exc.value.detail
    assert "CPA_KEY" in exc.value.detail
    assert "CLOUDMAIL_BASE_URL" not in exc.value.detail


@pytest.mark.parametrize(
    ("endpoint", "args", "action_label"),
    [
        ("post_auto_detect_replace", (api.PendingInviteConsumeParams(),), "自动检测替换"),
        ("post_manage_teams", (api.ManageTeamsParams(replace_with_pending_invite=True),), "多 Team 自动调度"),
        ("post_add", (api.PendingInviteConsumeParams(),), "消费 pending invite 替换"),
        ("post_invite_add", (api.InviteAddParams(force_create_invite=True),), "新增 invite 注册"),
    ],
)
def test_invite_task_endpoints_require_cpa_config_first(monkeypatch, endpoint, args, action_label):
    monkeypatch.setattr("autoteam.setup_wizard._read_env", lambda: {})
    for key in (
        "CPA_URL",
        "CPA_KEY",
        "CF_TEMP_EMAIL_BASE_URL",
        "CF_TEMP_EMAIL_ADMIN_PASSWORD",
        "CF_TEMP_EMAIL_DOMAIN",
    ):
        monkeypatch.delenv(key, raising=False)

    with pytest.raises(HTTPException) as exc:
        getattr(api, endpoint)(*args)

    assert exc.value.status_code == 400
    assert action_label in exc.value.detail
    assert "CPA_URL" in exc.value.detail
    assert "CPA_KEY" in exc.value.detail
    assert "CF_TEMP_EMAIL_BASE_URL" not in exc.value.detail


@pytest.mark.parametrize(
    ("endpoint", "args", "action_label"),
    [
        ("post_auto_detect_replace", (api.PendingInviteConsumeParams(),), "自动检测替换"),
        ("post_manage_teams", (api.ManageTeamsParams(replace_with_pending_invite=True),), "多 Team 自动替换"),
        ("post_add", (api.PendingInviteConsumeParams(),), "注册新号"),
        ("post_invite_add", (api.InviteAddParams(force_create_invite=True),), "新增 invite 注册"),
    ],
)
def test_invite_task_endpoints_require_current_cloudflare_temp_email_config(monkeypatch, endpoint, args, action_label):
    monkeypatch.setattr("autoteam.setup_wizard._read_env", lambda: {})
    monkeypatch.setenv("MAIL_PROVIDER", "cloudflare_temp_email")
    monkeypatch.setenv("CPA_URL", "http://127.0.0.1:8317")
    monkeypatch.setenv("CPA_KEY", "key-1")
    for key in (
        "CF_TEMP_EMAIL_BASE_URL",
        "CF_TEMP_EMAIL_ADMIN_PASSWORD",
        "CF_TEMP_EMAIL_DOMAIN",
    ):
        monkeypatch.delenv(key, raising=False)

    with pytest.raises(HTTPException) as exc:
        getattr(api, endpoint)(*args)

    assert exc.value.status_code == 400
    assert action_label in exc.value.detail
    assert "当前邮箱服务（Cloudflare Temp Email）" in exc.value.detail
    assert "CF_TEMP_EMAIL_BASE_URL" in exc.value.detail
    assert "CLOUDMAIL_BASE_URL" not in exc.value.detail
    assert "CPA_KEY" not in exc.value.detail


@pytest.mark.parametrize(
    ("endpoint", "args"),
    [("post_fill", (api.TaskParams(),)), ("post_cleanup", (api.CleanupParams(),)), ("post_reset_quota", ())],
)
def test_legacy_mutating_task_endpoints_are_disabled(endpoint, args):
    with pytest.raises(HTTPException) as exc:
        getattr(api, endpoint)(*args)
    assert exc.value.status_code == 410


def test_cancel_task_marks_running_task_as_cancelling(monkeypatch):
    task = {
        "task_id": "task-1",
        "command": "rotate",
        "params": {},
        "status": "running",
        "created_at": time.time(),
        "started_at": time.time(),
        "finished_at": None,
        "result": None,
        "error": None,
        "cancel_requested": False,
        "cancel_requested_at": None,
        "cancel_message": "任务已终止",
    }

    monkeypatch.setattr(api, "_tasks", {"task-1": task})

    result = api.cancel_task("task-1")

    assert result["task_id"] == "task-1"
    assert result["status"] == "cancelling"
    assert task["status"] == "cancelling"
    assert task["cancel_requested"] is True
    assert task["cancel_requested_at"] is not None
    assert task["error"] == "任务终止中"


def test_run_task_marks_cancel_requested_task_as_cancelled(monkeypatch):
    task = {
        "task_id": "task-1",
        "command": "fill",
        "params": {},
        "status": "pending",
        "created_at": time.time(),
        "started_at": None,
        "finished_at": None,
        "result": None,
        "error": None,
        "cancel_requested": True,
        "cancel_requested_at": time.time(),
        "cancel_message": "任务已终止",
    }

    monkeypatch.setattr(api, "_tasks", {"task-1": task})
    monkeypatch.setattr(api, "_playwright_lock", threading.Lock())
    monkeypatch.setattr(api, "_current_task_id", None)

    api._run_task("task-1", lambda: (_ for _ in ()).throw(AssertionError("should not run")))

    assert task["status"] == "cancelled"
    assert task["error"] == "任务已终止"
    assert task["started_at"] is not None
    assert task["finished_at"] is not None
    assert api._current_task_id is None


def test_start_task_reserves_lock_until_worker_runs(monkeypatch):
    class _DeferredThread:
        def __init__(self, target=None, args=(), kwargs=None, daemon=None):
            self.target = target
            self.args = args
            self.kwargs = kwargs or {}
            self.daemon = daemon

        def start(self):
            return None

    lock = threading.Lock()
    monkeypatch.setattr(api, "_tasks", {})
    monkeypatch.setattr(api, "_playwright_lock", lock)
    monkeypatch.setattr(api, "_current_task_id", None)
    monkeypatch.setattr(api.threading, "Thread", _DeferredThread)

    task = api._start_task("one", lambda: None, {})

    assert task["status"] == "pending"
    assert lock.locked() is True
    with pytest.raises(HTTPException) as exc:
        api._start_task("two", lambda: None, {})
    assert exc.value.status_code == 409

    lock.release()


def test_admin_cancel_does_not_release_task_owned_lock(monkeypatch):
    class _AdminLogin:
        def __init__(self):
            self.stopped = False

        def stop(self):
            self.stopped = True

    lock = threading.Lock()
    assert lock.acquire(blocking=False) is True
    admin_login = _AdminLogin()

    monkeypatch.setattr(api, "_playwright_lock", lock)
    monkeypatch.setattr(api, "_playwright_lock_owner", "task:running")
    monkeypatch.setattr(api, "_playwright_lock_owner_lock_id", id(lock))
    monkeypatch.setattr(api, "_admin_login_api", admin_login)
    monkeypatch.setattr(api, "_admin_login_step", "code_required")
    monkeypatch.setattr(api._pw_executor, "run", lambda func, *args, **kwargs: func(*args, **kwargs))

    result = api.post_admin_login_cancel()

    assert result["message"] == "管理员登录已取消"
    assert admin_login.stopped is True
    assert api._admin_login_api is None
    assert lock.locked() is True
    assert api._playwright_lock_owner == "task:running"

    lock.release()


def test_admin_session_import_parses_cookie_input(monkeypatch):
    captured = {}

    class _FakeChatGPTTeamAPI:
        def import_admin_session(self, email, session_token):
            captured["email"] = email
            captured["session_token"] = session_token
            return {"email": email, "session_len": len(session_token)}

        def stop(self):
            captured["stopped"] = True

    monkeypatch.setattr("autoteam.chatgpt_api.ChatGPTTeamAPI", _FakeChatGPTTeamAPI)
    monkeypatch.setattr(api._pw_executor, "run", lambda func, *args, **kwargs: func(*args, **kwargs))
    monkeypatch.setattr(api, "_admin_login_api", None)
    monkeypatch.setattr(api, "_admin_login_step", None)
    monkeypatch.setattr(api, "_playwright_lock", threading.Lock())
    monkeypatch.setattr(api, "_playwright_lock_owner", None)
    monkeypatch.setattr(api, "_playwright_lock_owner_lock_id", None)
    monkeypatch.setattr(api, "_admin_status", lambda: {"configured": True})
    monkeypatch.setattr(api, "_main_codex_status", lambda: {"configured": False})

    result = api.post_admin_login_session(
        api.AdminSessionParams(
            email="owner@example.com",
            session_token=(
                "Cookie: __Secure-next-auth.session-token.0=chunk-a; "
                "__Secure-next-auth.session-token.1=chunk-b"
            ),
        )
    )

    assert result["status"] == "completed"
    assert captured == {
        "email": "owner@example.com",
        "session_token": "chunk-achunk-b",
        "stopped": True,
    }


@pytest.mark.parametrize(
    ("endpoint", "action_label"),
    [
        ("get_cpa_files", "查看 CPA 文件"),
    ],
)
def test_cpa_endpoints_require_cpa_config(monkeypatch, endpoint, action_label):
    monkeypatch.setattr("autoteam.setup_wizard._read_env", lambda: {})
    for key in ("CPA_URL", "CPA_KEY"):
        monkeypatch.delenv(key, raising=False)

    with pytest.raises(HTTPException) as exc:
        getattr(api, endpoint)()

    assert exc.value.status_code == 400
    assert action_label in exc.value.detail
    assert "配置面板" in exc.value.detail
    assert "CPA_URL" in exc.value.detail
    assert "CPA_KEY" in exc.value.detail


@pytest.mark.parametrize(
    ("endpoint", "args"),
    [
        ("post_sync", ()),
        ("post_sync_from_cpa", ()),
    ],
)
def test_local_sync_endpoints_are_disabled(endpoint, args):
    with pytest.raises(HTTPException) as exc:
        getattr(api, endpoint)(*args)

    assert exc.value.status_code == 410


def test_post_sync_rejects_sub2api_only_legacy_path(monkeypatch):
    monkeypatch.setenv("SYNC_TARGET_SUB2API", "true")
    monkeypatch.setenv("SUB2API_URL", "http://sub2api.example.com")
    monkeypatch.setenv("SUB2API_EMAIL", "admin@example.com")
    monkeypatch.setenv("SUB2API_PASSWORD", "secret")
    monkeypatch.delenv("SYNC_TARGET_CPA", raising=False)
    monkeypatch.delenv("CPA_URL", raising=False)
    monkeypatch.delenv("CPA_KEY", raising=False)
    monkeypatch.setattr("autoteam.sync_targets.sync_to_configured_targets", lambda: {"sub2api": {"created": 1}})

    with pytest.raises(HTTPException) as exc:
        api.post_sync()

    assert exc.value.status_code == 410


def test_add_task_rejects_sub2api_only_legacy_path(monkeypatch):
    monkeypatch.setattr("autoteam.setup_wizard._read_env", lambda: {})
    monkeypatch.setenv("CLOUDMAIL_BASE_URL", "http://mail.example.com")
    monkeypatch.setenv("CLOUDMAIL_EMAIL", "admin@example.com")
    monkeypatch.setenv("CLOUDMAIL_PASSWORD", "secret")
    monkeypatch.setenv("CLOUDMAIL_DOMAIN", "@example.com")
    monkeypatch.setenv("SYNC_TARGET_SUB2API", "true")
    monkeypatch.setenv("SUB2API_URL", "http://sub2api.example.com")
    monkeypatch.setenv("SUB2API_EMAIL", "admin@example.com")
    monkeypatch.setenv("SUB2API_PASSWORD", "secret")
    monkeypatch.delenv("SYNC_TARGET_CPA", raising=False)
    monkeypatch.delenv("CPA_URL", raising=False)
    monkeypatch.delenv("CPA_KEY", raising=False)
    monkeypatch.setattr(api, "_start_task", lambda command, func, params, *args, **kwargs: {"task_id": command})

    with pytest.raises(HTTPException) as exc:
        api.post_add()

    assert exc.value.status_code == 400
    assert "CPA_URL" in exc.value.detail


def test_put_runtime_config_source_applies_env_and_updates_api_key(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text("API_KEY=old-key\n", encoding="utf-8")

    monkeypatch.setattr("autoteam.setup_wizard.ENV_FILE", env_file)
    monkeypatch.setattr("autoteam.setup_wizard.ENV_EXAMPLE", tmp_path / ".env.example")
    monkeypatch.setattr("autoteam.setup_wizard._verify_mail_provider", lambda provider=None: True)
    monkeypatch.setattr("autoteam.setup_wizard._verify_cpa", lambda: True)
    monkeypatch.setattr("importlib.reload", lambda module: module)
    monkeypatch.setattr(api, "API_KEY", "old-key")

    for key in (
        "CLOUDMAIL_BASE_URL",
        "CLOUDMAIL_EMAIL",
        "CLOUDMAIL_PASSWORD",
        "CLOUDMAIL_DOMAIN",
        "CPA_URL",
        "CPA_KEY",
        "PLAYWRIGHT_PROXY_URL",
        "PLAYWRIGHT_PROXY_BYPASS",
        "API_KEY",
    ):
        monkeypatch.delenv(key, raising=False)

    result = api.put_runtime_config_source(
        api.SourceConfig(
            content="\n".join(
                [
                    "CLOUDMAIL_BASE_URL=http://mail.example.com",
                    "CLOUDMAIL_EMAIL=admin@example.com",
                    "CLOUDMAIL_PASSWORD=secret",
                    "CLOUDMAIL_DOMAIN=@example.com",
                    "CPA_URL=http://127.0.0.1:8317",
                    "CPA_KEY=key-1",
                    "API_KEY=new-key",
                ]
            )
        )
    )

    assert result["message"] == "源文件保存成功"
    assert result["api_key"] == "new-key"
    assert api.API_KEY == "new-key"
    assert env_file.read_text(encoding="utf-8").splitlines()[0] == "CLOUDMAIL_BASE_URL=http://mail.example.com"


def test_auto_check_skips_rotate_when_pool_configs_are_missing(tmp_path, monkeypatch, caplog):
    auth_file = tmp_path / "active.json"
    auth_file.write_text('{"access_token": "token-low"}', encoding="utf-8")

    updates = []
    started = []

    monkeypatch.setattr(api, "_auto_check_config", {"interval": 0, "threshold": 10, "min_low": 1})
    monkeypatch.setattr(api, "_auto_check_stop", threading.Event())
    monkeypatch.setattr(api, "_auto_check_restart", threading.Event())
    monkeypatch.setattr(api, "_maybe_reload_runtime_config_from_env_file", lambda *args, **kwargs: False)
    monkeypatch.setattr(api, "_is_main_account_email", lambda _email: False)
    monkeypatch.setattr("autoteam.setup_wizard._read_env", lambda: {})
    for key in (
        "CLOUDMAIL_BASE_URL",
        "CLOUDMAIL_EMAIL",
        "CLOUDMAIL_PASSWORD",
        "CLOUDMAIL_DOMAIN",
        "CPA_URL",
        "CPA_KEY",
    ):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(
        "autoteam.accounts.load_accounts",
        lambda: [{"email": "low@example.com", "status": "active", "auth_file": str(auth_file)}],
    )
    monkeypatch.setattr(
        "autoteam.codex_auth.check_codex_quota",
        lambda _token: ("ok", {"primary_pct": 95, "primary_resets_at": 1234567890, "weekly_pct": 1}),
    )
    monkeypatch.setattr("autoteam.accounts.update_account", lambda email, **kwargs: updates.append((email, kwargs)))
    monkeypatch.setattr(
        api,
        "_start_task",
        lambda command, func, params, *args, **kwargs: started.append((command, params, args, kwargs)),
    )

    stop_event = api._auto_check_stop
    wait_calls = {"count": 0}

    def fake_wait(_seconds):
        wait_calls["count"] += 1
        return wait_calls["count"] > 1

    monkeypatch.setattr(stop_event, "wait", fake_wait)

    with caplog.at_level(logging.WARNING):
        api._auto_check_loop()

    assert updates == []
    assert started == []
    assert "跳过自动 swap_seat" in caplog.text
    assert "配置面板" in caplog.text


def _run_auto_check_once(monkeypatch, *, config=None, teams=None, repair_result=None):
    _set_pool_runtime_config(monkeypatch)
    monkeypatch.setattr(
        api,
        "_auto_check_config",
        {
            "interval": 0,
            "target_seats": 2,
            "replace_with_pending_invite": False,
            **(config or {}),
        },
    )
    monkeypatch.setattr(api, "_auto_check_stop", threading.Event())
    monkeypatch.setattr(api, "_auto_check_restart", threading.Event())
    monkeypatch.setattr(api, "_maybe_reload_runtime_config_from_env_file", lambda *args, **kwargs: False)

    started = []
    repair_calls = []

    def fake_start_task(command, func, params, *args, **kwargs):
        started.append({"command": command, "func": func, "params": params, "args": args, "kwargs": kwargs})
        return {"task_id": command}

    def fake_repair_pat_auths(**kwargs):
        repair_calls.append(kwargs)
        return repair_result or {"mode": "repair_pat_auths", "summary": {"repaired": 0, "failed": 0}}

    monkeypatch.setattr("autoteam.manager.cmd_repair_pat_auths", fake_repair_pat_auths)
    monkeypatch.setattr("autoteam.manager.cmd_swap_seats", lambda *args, **kwargs: {"mode": "swap_seat"})
    monkeypatch.setattr("autoteam.manager.cmd_auto_detect_replace", lambda *args, **kwargs: {"mode": "auto_detect_replace"})
    monkeypatch.setattr("autoteam.manager.cmd_manage_teams", lambda *args, **kwargs: {"mode": "multi_team_manage"})
    monkeypatch.setattr("autoteam.team_context.get_team_contexts", lambda **_kwargs: teams or [])
    monkeypatch.setattr(api, "_start_task", fake_start_task)

    stop_event = api._auto_check_stop
    wait_calls = {"count": 0}

    def fake_wait(_seconds):
        wait_calls["count"] += 1
        return wait_calls["count"] > 1

    monkeypatch.setattr(stop_event, "wait", fake_wait)

    api._auto_check_loop()
    return started, repair_calls


def _run_started_task(started, index=0):
    task = started[index]
    return task["func"](*task["args"], **task["kwargs"])


def test_auto_check_triggers_auto_swap_without_local_quota_or_account_updates(monkeypatch):
    monkeypatch.setattr(
        "autoteam.accounts.load_accounts",
        lambda: (_ for _ in ()).throw(AssertionError("auto-check must not inspect local accounts")),
    )
    monkeypatch.setattr(
        "autoteam.codex_auth.check_codex_quota",
        lambda _token: (_ for _ in ()).throw(AssertionError("auto-check must not check local auth quota")),
    )
    monkeypatch.setattr(
        "autoteam.accounts.update_account",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("auto-check must not update local account quota")),
    )

    started, repair_calls = _run_auto_check_once(monkeypatch, config={"target_seats": 4})

    assert repair_calls == []
    assert len(started) == 1
    assert started[0]["command"] == "auto-swap-seats"
    assert started[0]["params"] == {
        "max_chatgpt_active": 4,
        "trigger": "auto-check",
        "replace_with_pending_invite": False,
        "replace_mode": "pending_invite",
        "pat_repair_already_run": True,
    }
    assert started[0]["args"] == (4, False, "pending_invite")

    assert _run_started_task(started) == {"mode": "swap_seat"}
    assert repair_calls == [{"limit": 2}]


def test_auto_check_triggers_auto_detect_replace_when_enabled(monkeypatch):
    monkeypatch.setenv("CF_TEMP_EMAIL_BASE_URL", "http://cfmail.example.com")
    monkeypatch.setenv("CF_TEMP_EMAIL_ADMIN_PASSWORD", "secret")
    monkeypatch.setenv("CF_TEMP_EMAIL_DOMAIN", "example.com")

    started, repair_calls = _run_auto_check_once(
        monkeypatch, config={"target_seats": 2, "replace_with_pending_invite": True}
    )

    assert repair_calls == []
    assert len(started) == 1
    assert started[0]["command"] == "auto-detect-replace"
    assert started[0]["params"] == {
        "max_chatgpt_active": 2,
        "trigger": "auto-check",
        "replace_with_pending_invite": True,
        "replace_mode": "pending_invite",
        "pat_repair_already_run": True,
    }
    assert started[0]["args"] == (2, True, "pending_invite")

    assert _run_started_task(started) == {"mode": "auto_detect_replace"}
    assert repair_calls == [{"limit": 2}]


def test_auto_check_passes_create_invite_replace_mode(monkeypatch):
    monkeypatch.setenv("CF_TEMP_EMAIL_BASE_URL", "http://cfmail.example.com")
    monkeypatch.setenv("CF_TEMP_EMAIL_ADMIN_PASSWORD", "secret")
    monkeypatch.setenv("CF_TEMP_EMAIL_DOMAIN", "example.com")

    started, repair_calls = _run_auto_check_once(
        monkeypatch,
        config={"target_seats": 2, "replace_with_pending_invite": True, "replace_mode": "create_invite"},
    )

    assert repair_calls == []
    assert len(started) == 1
    assert started[0]["command"] == "auto-detect-replace"
    assert started[0]["params"]["replace_mode"] == "create_invite"
    assert started[0]["args"] == (2, True, "create_invite")

    assert _run_started_task(started) == {"mode": "auto_detect_replace"}
    assert repair_calls == [{"limit": 2}]


def test_auto_check_requires_cf_temp_config_when_pending_invite_replace_enabled(monkeypatch, caplog):
    _set_pool_runtime_config(monkeypatch)
    monkeypatch.setattr(
        api,
        "_auto_check_config",
        {"interval": 0, "target_seats": 2, "replace_with_pending_invite": True},
    )
    monkeypatch.setattr(api, "_auto_check_stop", threading.Event())
    monkeypatch.setattr(api, "_auto_check_restart", threading.Event())
    monkeypatch.setattr(api, "_maybe_reload_runtime_config_from_env_file", lambda *args, **kwargs: False)
    for key in ("CF_TEMP_EMAIL_BASE_URL", "CF_TEMP_EMAIL_ADMIN_PASSWORD", "CF_TEMP_EMAIL_DOMAIN"):
        monkeypatch.delenv(key, raising=False)

    started = []
    monkeypatch.setattr(api, "_start_task", lambda *args, **kwargs: started.append((args, kwargs)))

    stop_event = api._auto_check_stop
    wait_calls = {"count": 0}

    def fake_wait(_seconds):
        wait_calls["count"] += 1
        return wait_calls["count"] > 1

    monkeypatch.setattr(stop_event, "wait", fake_wait)

    with caplog.at_level(logging.WARNING):
        api._auto_check_loop()

    assert started == []
    assert "自动 pending invite 补位" in caplog.text
    assert "CF_TEMP_EMAIL_BASE_URL" in caplog.text


def test_auto_check_uses_manage_teams_for_multiple_team_contexts(monkeypatch):
    teams = [object(), object()]

    started, repair_calls = _run_auto_check_once(
        monkeypatch,
        config={"target_seats": 3, "replace_with_pending_invite": False},
        teams=teams,
    )

    assert repair_calls == []
    assert len(started) == 1
    assert started[0]["command"] == "manage-teams"
    assert started[0]["params"] == {
        "max_chatgpt_active": 3,
        "trigger": "auto-check",
        "replace_with_pending_invite": False,
        "replace_mode": "pending_invite",
        "pat_repair_already_run": True,
    }
    assert started[0]["args"] == (3, False, "pending_invite", teams)

    assert _run_started_task(started) == {"mode": "multi_team_manage"}
    assert repair_calls == [{"limit": 2, "team_context": teams[0]}, {"limit": 2, "team_context": teams[1]}]


def test_auto_check_does_not_run_pat_repair_when_task_start_is_busy(monkeypatch, caplog):
    _set_pool_runtime_config(monkeypatch)
    monkeypatch.setattr(
        api,
        "_auto_check_config",
        {"interval": 0, "target_seats": 2, "replace_with_pending_invite": False},
    )
    monkeypatch.setattr(api, "_auto_check_stop", threading.Event())
    monkeypatch.setattr(api, "_auto_check_restart", threading.Event())
    monkeypatch.setattr(api, "_maybe_reload_runtime_config_from_env_file", lambda *args, **kwargs: False)
    monkeypatch.setattr("autoteam.team_context.get_team_contexts", lambda **_kwargs: [])

    repair_calls = []
    monkeypatch.setattr("autoteam.manager.cmd_repair_pat_auths", lambda **kwargs: repair_calls.append(kwargs))

    def busy_start_task(*_args, **_kwargs):
        raise HTTPException(status_code=409, detail="busy")

    monkeypatch.setattr(api, "_start_task", busy_start_task)

    stop_event = api._auto_check_stop
    wait_calls = {"count": 0}

    def fake_wait(_seconds):
        wait_calls["count"] += 1
        return wait_calls["count"] > 1

    monkeypatch.setattr(stop_event, "wait", fake_wait)

    with caplog.at_level(logging.WARNING):
        api._auto_check_loop()

    assert repair_calls == []
    assert "跳过自动 swap_seat" in caplog.text


def test_auto_check_team_member_count_times_out_without_blocking(monkeypatch):
    monkeypatch.setattr(api, "_run_playwright_probe", lambda *args, **kwargs: (_ for _ in ()).throw(TimeoutError()))

    started = time.monotonic()
    result = api._auto_check_team_member_count(timeout_seconds=0.05, retries=1)
    elapsed = time.monotonic() - started

    assert result == -1
    assert elapsed < 0.15


def test_auto_check_team_member_count_retries_three_times_on_timeout(monkeypatch):
    attempts = {"count": 0}

    def fake_probe(*args, **kwargs):
        attempts["count"] += 1
        raise TimeoutError()

    monkeypatch.setattr(api, "_run_playwright_probe", fake_probe)

    result = api._auto_check_team_member_count(timeout_seconds=0.01, retries=3)

    assert result == -1
    assert attempts["count"] == 3


def test_parse_playwright_probe_stdout_uses_last_json_line():
    stdout = """
    [13:05:56] INFO     noisy log
    some extra text
    {"count": 5}
    """

    assert api._parse_playwright_probe_stdout(stdout) == {"count": 5}


def test_auto_check_team_member_count_logs_after_final_timeout(monkeypatch, caplog):
    monkeypatch.setattr(api, "_run_playwright_probe", lambda *args, **kwargs: (_ for _ in ()).throw(TimeoutError()))

    with caplog.at_level(logging.WARNING):
        result = api._auto_check_team_member_count(timeout_seconds=0.01, retries=1)

    assert result == -1
    assert "已重试 1 次" in caplog.text


def test_run_playwright_probe_kills_process_group_on_timeout(monkeypatch):
    killed = []

    class _FakeProc:
        def __init__(self):
            self.pid = 1234

        def communicate(self, timeout=None):
            raise subprocess.TimeoutExpired(cmd="probe", timeout=timeout)

        def kill(self):
            killed.append("kill")

    monkeypatch.setattr(api.subprocess, "Popen", lambda *args, **kwargs: _FakeProc())
    monkeypatch.setattr(api.os, "killpg", lambda pid, sig: killed.append((pid, sig)))

    with pytest.raises(TimeoutError):
        api._run_playwright_probe("team-member-count", timeout_seconds=0.01)

    assert killed[0][0] == 1234


def test_playwright_executor_raises_after_timeout(monkeypatch):
    executor = api._PlaywrightExecutor()

    original_wait = threading.Event.wait

    def fake_wait(self, timeout=None):
        if timeout == 1:
            return False
        return original_wait(self, timeout)

    monkeypatch.setattr(threading.Event, "wait", fake_wait)

    try:
        with pytest.raises(TimeoutError):
            executor.run(lambda: None, timeout_seconds=1)

        with pytest.raises(RuntimeError):
            executor.run(lambda: None, timeout_seconds=1)
    finally:
        executor.stop()


def test_auto_check_wait_returns_restart_soon_after_config_update(monkeypatch):
    monkeypatch.setattr(api, "_auto_check_stop", threading.Event())
    monkeypatch.setattr(api, "_auto_check_restart", threading.Event())

    def trigger_restart():
        time.sleep(0.05)
        api._auto_check_restart.set()

    thread = threading.Thread(target=trigger_restart, daemon=True)
    thread.start()

    started = time.monotonic()
    result = api._auto_check_wait(5)
    elapsed = time.monotonic() - started

    assert result == "restart"
    assert elapsed < 0.5
