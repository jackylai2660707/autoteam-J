"""CPA configuration helpers."""

from urllib.parse import urlsplit, urlunsplit


def normalize_cpa_url(value: object) -> str:
    """Normalize CPA UI/API URLs to the management API base URL.

    Users often paste the browser management page, for example
    https://example.com/management.html. The API client needs only the base
    origin because it appends /v0/management/... endpoints itself.
    """
    raw = str(value or "").strip()
    if not raw:
        return ""

    parsed = urlsplit(raw)
    if not parsed.scheme or not parsed.netloc:
        return raw.rstrip("/")

    path = parsed.path.rstrip("/")
    lower_path = path.lower()
    for marker in ("/v0/management", "/management.html", "/management", "/index.html"):
        index = lower_path.find(marker)
        if index >= 0:
            path = path[:index].rstrip("/")
            break

    return urlunsplit((parsed.scheme, parsed.netloc, path, "", "")).rstrip("/")
