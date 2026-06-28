"""Browser backend adapter for login/session capture flows."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path

from playwright.sync_api import sync_playwright

import autoteam.display  # noqa: F401 - ensure Xvfb is available for headed browser backends
from autoteam.config import (
    get_browser_backend,
    get_cloakbrowser_humanize,
    get_cloakbrowser_profile_dir,
    get_cloakbrowser_profile_seed,
    get_playwright_launch_options,
)

logger = logging.getLogger(__name__)

DEFAULT_VIEWPORT = {"width": 1280, "height": 800}
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36"
)


@dataclass
class BrowserSession:
    backend: str
    playwright: object | None = None
    browser: object | None = None
    context: object | None = None
    page: object | None = None

    def close(self):
        for obj in (self.context, self.browser):
            if obj:
                try:
                    obj.close()
                except Exception:
                    pass
        if self.playwright:
            try:
                self.playwright.stop()
            except Exception:
                pass
        self.page = None
        self.context = None
        self.browser = None
        self.playwright = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()
        return False


def _context_options(**overrides) -> dict:
    options = {
        "viewport": DEFAULT_VIEWPORT,
        "user_agent": DEFAULT_USER_AGENT,
    }
    options.update({key: value for key, value in overrides.items() if value is not None})
    return options


def _cloak_launch_kwargs() -> dict:
    launch_kwargs = dict(get_playwright_launch_options())
    launch_kwargs["humanize"] = get_cloakbrowser_humanize()
    return launch_kwargs


def _seeded_cloak_profile_dir() -> str:
    profile_dir = get_cloakbrowser_profile_dir()
    seed = get_cloakbrowser_profile_seed()
    if not profile_dir or not seed:
        return profile_dir

    safe_seed = re.sub(r"[^A-Za-z0-9_.-]+", "_", seed).strip("._")
    if not safe_seed:
        return profile_dir
    return str(Path(profile_dir) / safe_seed)


def new_browser_session(**context_overrides) -> BrowserSession:
    """Create a page with the configured browser backend.

    CloakBrowser returns Playwright-compatible objects, so downstream login
    flows can keep using the normal Playwright Page/Context APIs.
    """
    backend = get_browser_backend()
    context_options = _context_options(**context_overrides)

    if backend == "cloakbrowser":
        try:
            from cloakbrowser import launch, launch_persistent_context
        except ModuleNotFoundError as exc:
            raise RuntimeError(
                "BROWSER_BACKEND=cloakbrowser 需要安装 cloakbrowser；请运行 `uv sync` 或安装项目依赖"
            ) from exc

        profile_dir = _seeded_cloak_profile_dir()
        launch_kwargs = _cloak_launch_kwargs()
        logger.info("[Browser] 使用 CloakBrowser backend%s", f" profile={profile_dir}" if profile_dir else "")
        if profile_dir:
            Path(profile_dir).mkdir(parents=True, exist_ok=True)
            context = launch_persistent_context(profile_dir, **launch_kwargs, **context_options)
            page = context.new_page()
            return BrowserSession(backend=backend, context=context, page=page)
        browser = launch(**launch_kwargs)
        context = browser.new_context(**context_options)
        page = context.new_page()
        return BrowserSession(backend=backend, browser=browser, context=context, page=page)

    playwright = sync_playwright().start()
    try:
        browser = playwright.chromium.launch(**get_playwright_launch_options())
        context = browser.new_context(**context_options)
        page = context.new_page()
        logger.info("[Browser] 使用 Playwright Chromium backend")
        return BrowserSession(
            backend=backend,
            playwright=playwright,
            browser=browser,
            context=context,
            page=page,
        )
    except Exception:
        try:
            playwright.stop()
        except Exception:
            pass
        raise
