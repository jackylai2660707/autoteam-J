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
