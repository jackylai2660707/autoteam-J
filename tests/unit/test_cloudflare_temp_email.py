from autoteam import cloudflare_temp_email
from autoteam.cloudflare_temp_email import (
    choose_cloudflare_temp_email_domain,
    parse_cloudflare_temp_email_domain_options,
)


def test_parse_cloudflare_temp_email_multi_random_domains():
    assert parse_cloudflare_temp_email_domain_options("xxxxx.a.com ; xxxx.b.com") == [
        {"domain": "a.com", "enable_random_subdomain": True},
        {"domain": "b.com", "enable_random_subdomain": True},
    ]


def test_parse_cloudflare_temp_email_plain_and_wildcard_domains():
    assert parse_cloudflare_temp_email_domain_options("mail.example.com;*.pool.example.com;{random}.alt.com") == [
        {"domain": "mail.example.com", "enable_random_subdomain": False},
        {"domain": "pool.example.com", "enable_random_subdomain": True},
        {"domain": "alt.com", "enable_random_subdomain": True},
    ]


def test_choose_cloudflare_temp_email_domain_round_robins_options():
    cloudflare_temp_email._DOMAIN_OPTION_ROUND_ROBIN_STATE.clear()

    picks = [
        choose_cloudflare_temp_email_domain("first.example.com;*.second.example.com"),
        choose_cloudflare_temp_email_domain("first.example.com;*.second.example.com"),
        choose_cloudflare_temp_email_domain("first.example.com;*.second.example.com"),
    ]

    assert picks == [
        {"domain": "first.example.com", "enable_random_subdomain": False},
        {"domain": "second.example.com", "enable_random_subdomain": True},
        {"domain": "first.example.com", "enable_random_subdomain": False},
    ]


def test_search_emails_by_recipient_does_not_resolve_account_id_when_not_requested(monkeypatch):
    client = cloudflare_temp_email.CloudflareTempEmailClient(
        service={
            "base_url": "https://mail.example.com",
            "admin_password": "secret",
            "domain": "icloud.com",
        }
    )

    monkeypatch.setattr(
        client,
        "_request",
        lambda *_args, **_kwargs: {
            "results": [
                {
                    "id": 7,
                    "address": "target@icloud.com",
                    "subject": "Invite",
                    "from": "noreply@openai.com",
                    "text": "hello",
                }
            ]
        },
    )
    monkeypatch.setattr(
        client,
        "_resolve_account_id_for_email",
        lambda _email: (_ for _ in ()).throw(AssertionError("must not resolve account id")),
    )

    emails = client.search_emails_by_recipient("target@icloud.com", size=5)

    assert [item["emailId"] for item in emails] == [7]
    assert emails[0]["accountEmail"] == "target@icloud.com"
