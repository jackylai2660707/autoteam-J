import json
from pathlib import Path

from autoteam import cpa_sync


def test_sync_to_cpa_skips_disabled_accounts_and_deletes_remote_copy(monkeypatch, tmp_path):
    enabled_auth = tmp_path / "codex-enabled@example.com-team-a.json"
    disabled_auth = tmp_path / "codex-disabled@example.com-team-b.json"
    enabled_auth.write_text("{}", encoding="utf-8")
    disabled_auth.write_text("{}", encoding="utf-8")

    monkeypatch.setattr(
        "autoteam.accounts.load_accounts",
        lambda: [
            {
                "email": "enabled@example.com",
                "status": "active",
                "auth_file": str(enabled_auth),
                "disabled": False,
                "managed_by_autoteam": True,
            },
            {
                "email": "disabled@example.com",
                "status": "active",
                "auth_file": str(disabled_auth),
                "disabled": True,
                "managed_by_autoteam": True,
            },
        ],
    )
    monkeypatch.setattr("autoteam.accounts.save_accounts", lambda _accounts: None)
    monkeypatch.setattr(cpa_sync, "_cleanup_local_duplicates", lambda _accounts: (0, False))
    monkeypatch.setattr(
        cpa_sync,
        "list_cpa_files",
        lambda: [
            {"name": enabled_auth.name, "email": "enabled@example.com"},
            {"name": disabled_auth.name, "email": "disabled@example.com"},
        ],
    )

    uploaded = []
    deleted = []
    monkeypatch.setattr(cpa_sync, "upload_to_cpa", lambda path: uploaded.append(Path(path).name) or True)
    monkeypatch.setattr(cpa_sync, "delete_from_cpa", lambda name: deleted.append(name) or True)

    cpa_sync.sync_to_cpa()

    assert uploaded == [enabled_auth.name]
    assert deleted == [disabled_auth.name]


def test_upload_to_cpa_registers_name_returned_by_management_api(monkeypatch, tmp_path):
    auth_file = tmp_path / "codex-member@example.com-pat-local.json"
    auth_file.write_text("{}", encoding="utf-8")
    registered = []

    class _Resp:
        status_code = 200
        text = ""

        def json(self):
            return {"file": {"name": "member@example.com.json"}}

    monkeypatch.setattr(cpa_sync, "_require_cpa_base_url", lambda _operation: "http://cpa.local")
    monkeypatch.setattr(cpa_sync, "_headers", lambda: {"Authorization": "Bearer test"})
    monkeypatch.setattr(cpa_sync.requests, "post", lambda *_args, **_kwargs: _Resp())
    monkeypatch.setattr(cpa_sync, "register_managed_cpa_auth", lambda name, **_kwargs: registered.append(name) or name)

    assert cpa_sync.upload_to_cpa(auth_file) is True
    assert set(registered) == {"codex-member@example.com-pat-local.json", "member@example.com.json"}


def test_delete_from_cpa_rejects_unmanaged_auth(monkeypatch):
    monkeypatch.setattr(cpa_sync, "is_managed_cpa_auth", lambda _name: False)
    monkeypatch.setattr(
        cpa_sync.requests,
        "delete",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("unmanaged auth must not be deleted")),
    )

    assert cpa_sync.delete_from_cpa("external.json") is False


def test_delete_from_cpa_allows_managed_auth(monkeypatch):
    calls = []

    class _Resp:
        status_code = 200
        text = ""

    monkeypatch.setattr(cpa_sync, "is_managed_cpa_auth", lambda name: name == "managed.json")
    monkeypatch.setattr(cpa_sync, "_require_cpa_base_url", lambda _operation: "http://cpa.local")
    monkeypatch.setattr(cpa_sync, "_headers", lambda: {"Authorization": "Bearer test"})
    monkeypatch.setattr(
        cpa_sync.requests,
        "delete",
        lambda url, **kwargs: calls.append((url, kwargs)) or _Resp(),
    )

    assert cpa_sync.delete_from_cpa("/tmp/managed.json") is True
    assert calls[0][1]["params"] == {"name": "managed.json"}


def test_set_cpa_auth_disabled_rejects_unmanaged_auth(monkeypatch):
    monkeypatch.setattr(cpa_sync, "is_managed_cpa_auth", lambda _auth: False)
    monkeypatch.setattr(
        cpa_sync.requests,
        "patch",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("unmanaged auth must not be patched")),
    )

    try:
        cpa_sync.set_cpa_auth_disabled("external.json", True)
        raise AssertionError("expected PermissionError")
    except PermissionError as exc:
        assert "external.json" in str(exc)


def test_set_cpa_auth_disabled_allows_managed_auth(monkeypatch):
    calls = []

    class _Resp:
        status_code = 200
        text = ""

        def json(self):
            return {"status": "ok"}

    monkeypatch.setattr(cpa_sync, "is_managed_cpa_auth", lambda auth: "managed.json" in cpa_sync.cpa_auth_name_candidates(auth))
    monkeypatch.setattr(cpa_sync, "_require_cpa_base_url", lambda _operation: "http://cpa.local")
    monkeypatch.setattr(cpa_sync, "_headers", lambda: {"Authorization": "Bearer test"})
    monkeypatch.setattr(cpa_sync.requests, "patch", lambda url, **kwargs: calls.append((url, kwargs)) or _Resp())

    assert cpa_sync.set_cpa_auth_disabled({"name": "managed.json"}, True) == {"status": "ok"}
    assert calls[0][1]["json"] == {"name": "managed.json", "disabled": True}


def test_get_managed_cpa_auth_names_ignores_manual_env_by_default(monkeypatch):
    monkeypatch.setenv("CPA_MANAGED_AUTH_NAMES", "manual.json")
    monkeypatch.delenv("CPA_ALLOW_MANUAL_MANAGED_AUTH_NAMES", raising=False)
    monkeypatch.setattr(cpa_sync, "_load_managed_cpa_auth_registry", lambda: {"auths": {}})

    assert "manual.json" not in cpa_sync.get_managed_cpa_auth_names(accounts=[])


def test_get_managed_cpa_auth_names_allows_manual_env_when_explicitly_enabled(monkeypatch):
    monkeypatch.setenv("CPA_MANAGED_AUTH_NAMES", "manual.json")
    monkeypatch.setenv("CPA_ALLOW_MANUAL_MANAGED_AUTH_NAMES", "true")
    monkeypatch.setattr(cpa_sync, "_load_managed_cpa_auth_registry", lambda: {"auths": {}})

    assert "manual.json" in cpa_sync.get_managed_cpa_auth_names(accounts=[])


def test_get_managed_cpa_auth_names_does_not_trust_password_mail_provider_only(monkeypatch):
    monkeypatch.setattr(cpa_sync, "_load_managed_cpa_auth_registry", lambda: {"auths": {}})

    names = cpa_sync.get_managed_cpa_auth_names(
        accounts=[
            {
                "email": "manual@example.com",
                "password": "secret",
                "mail_provider": "cloudflare_temp_email",
                "auth_file": "/tmp/manual.json",
            }
        ]
    )

    assert "manual.json" not in names


def test_get_managed_cpa_auth_names_does_not_trust_string_false_managed_flag(monkeypatch):
    monkeypatch.setattr(cpa_sync, "_load_managed_cpa_auth_registry", lambda: {"auths": {}})

    names = cpa_sync.get_managed_cpa_auth_names(
        accounts=[
            {
                "email": "manual@example.com",
                "managed_by_autoteam": "false",
                "mail_account_id": "addr-should-not-matter",
                "auth_file": "/tmp/manual.json",
            }
        ]
    )

    assert "manual.json" not in names


def test_get_managed_cpa_auth_names_explicit_false_overrides_registry(monkeypatch):
    monkeypatch.setattr(cpa_sync, "_load_managed_cpa_auth_registry", lambda: {"auths": {"manual.json": {"name": "manual.json"}}})

    names = cpa_sync.get_managed_cpa_auth_names(
        accounts=[
            {
                "email": "manual@example.com",
                "managed_by_autoteam": "false",
                "mail_account_id": "addr-should-not-matter",
                "auth_file": "/tmp/manual.json",
            }
        ]
    )

    assert "manual.json" not in names


def test_cpa_auth_active_accepts_blank_status_when_not_disabled():
    assert cpa_sync.cpa_auth_is_disabled({"disabled": "false"}) is False
    assert cpa_sync.cpa_auth_is_active({"disabled": False}) is True
    assert cpa_sync.cpa_auth_is_active({"disabled": "false", "status": ""}) is True
    assert cpa_sync.cpa_auth_is_active({"disabled": 0, "status": "enabled"}) is True
    assert cpa_sync.cpa_auth_is_active({"disabled": False, "status": "ok"}) is True
    assert cpa_sync.cpa_auth_is_active({"disabled": True, "status": "active"}) is False
    assert cpa_sync.cpa_auth_is_active({"disabled": "true", "status": "active"}) is False
    assert cpa_sync.cpa_auth_is_active({"disabled": False, "status": "disabled"}) is False
    assert cpa_sync.cpa_auth_is_active(None) is False


def test_sync_from_cpa_backfills_mail_service_binding(monkeypatch, tmp_path):
    auth_dir = tmp_path / "auths"
    auth_dir.mkdir()

    monkeypatch.setattr(cpa_sync, "AUTH_DIR", auth_dir)
    monkeypatch.setattr(cpa_sync, "ensure_auth_dir", lambda: auth_dir)
    monkeypatch.setattr(cpa_sync, "ensure_auth_file_permissions", lambda _path: None)
    monkeypatch.setattr(cpa_sync, "_cleanup_local_duplicates", lambda accounts: (0, False))
    monkeypatch.setattr(
        cpa_sync,
        "list_cpa_files",
        lambda: [{"name": "codex-user@pool.example.com-team-a.json", "email": "user@pool.example.com"}],
    )
    monkeypatch.setattr(
        cpa_sync,
        "download_from_cpa",
        lambda _name: (
            '{"type":"codex","email":"user@pool.example.com","access_token":"token","refresh_token":"refresh","expires_at":"2099-01-01T00:00:00Z"}'
        ),
    )
    monkeypatch.setattr(cpa_sync, "delete_from_cpa", lambda _name: True)
    monkeypatch.setattr("autoteam.accounts.load_accounts", lambda: [])
    saved = {}
    monkeypatch.setattr(
        "autoteam.accounts.save_accounts",
        lambda items: saved.setdefault("accounts", [dict(item) for item in items]),
    )
    monkeypatch.setattr(
        "autoteam.mail_provider.infer_mail_service_from_email",
        lambda email: "cm-1" if email == "user@pool.example.com" else "",
    )
    monkeypatch.setattr(
        "autoteam.mail_provider.infer_mail_provider_from_email",
        lambda email: "cloudmail" if email == "user@pool.example.com" else "",
    )

    result = cpa_sync.sync_from_cpa()

    assert result["accounts_added"] == 1
    assert saved["accounts"][0]["mail_service_id"] == "cm-1"
    assert saved["accounts"][0]["mail_provider"] == "cloudmail"


def test_sync_from_cpa_accepts_userscript_export_and_preserves_pat_headers(monkeypatch, tmp_path):
    auth_dir = tmp_path / "auths"
    auth_dir.mkdir()

    userscript_export = {
        "access_token": "chatgpt-session-access-token",
        "account_id": "acc-1",
        "disabled": "false",
        "email": "user@example.com",
        "expired": "2099-01-01T00:00:00+08:00",
        "headers": {"authorization": "Bearer codex-personal-access-token"},
        "id_token": None,
        "last_refresh": "2026-06-24T12:00:00+08:00",
        "refresh_token": None,
        "type": "codex",
        "websockets": "false",
    }

    monkeypatch.setattr(cpa_sync, "AUTH_DIR", auth_dir)
    monkeypatch.setattr(cpa_sync, "ensure_auth_dir", lambda: auth_dir)
    monkeypatch.setattr(cpa_sync, "ensure_auth_file_permissions", lambda _path: None)
    monkeypatch.setattr(cpa_sync, "_cleanup_local_duplicates", lambda accounts: (0, False))
    monkeypatch.setattr(
        cpa_sync,
        "list_cpa_files",
        # userscript 下载名通常是 email.json，不一定是旧的 codex-*.json。
        lambda: [{"name": "user@example.com.json", "email": "user@example.com", "type": "codex"}],
    )
    monkeypatch.setattr(cpa_sync, "download_from_cpa", lambda _name: json.dumps(userscript_export))
    monkeypatch.setattr(cpa_sync, "delete_from_cpa", lambda _name: True)
    monkeypatch.setattr("autoteam.accounts.load_accounts", lambda: [])
    saved = {}
    monkeypatch.setattr(
        "autoteam.accounts.save_accounts",
        lambda items: saved.setdefault("accounts", [dict(item) for item in items]),
    )
    monkeypatch.setattr("autoteam.mail_provider.infer_mail_service_from_email", lambda _email: "")
    monkeypatch.setattr("autoteam.mail_provider.infer_mail_provider_from_email", lambda _email: "")

    result = cpa_sync.sync_from_cpa()

    assert result["accounts_added"] == 1
    auth_file = Path(saved["accounts"][0]["auth_file"])
    data = json.loads(auth_file.read_text(encoding="utf-8"))
    assert data["headers"]["authorization"] == "Bearer codex-personal-access-token"
    assert data["refresh_token"] is None
    assert data["id_token"] is None
    assert data["disabled"] is False
    assert data["websockets"] is False
