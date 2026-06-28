import sys
import types

from autoteam import browser_backend


class _FakePage:
    pass


class _FakeContext:
    def __init__(self):
        self.page = _FakePage()
        self.closed = False
        self.context_kwargs = None

    def new_page(self):
        return self.page

    def close(self):
        self.closed = True


class _FakeBrowser:
    def __init__(self):
        self.context = _FakeContext()
        self.closed = False
        self.context_kwargs = None

    def new_context(self, **kwargs):
        self.context_kwargs = kwargs
        return self.context

    def close(self):
        self.closed = True


def test_cloakbrowser_backend_uses_cloak_launch(monkeypatch):
    calls = {}
    browser = _FakeBrowser()

    def fake_launch(**kwargs):
        calls["launch"] = kwargs
        return browser

    fake_module = types.SimpleNamespace(
        launch=fake_launch,
        launch_persistent_context=lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError()),
    )
    monkeypatch.setitem(sys.modules, "cloakbrowser", fake_module)
    monkeypatch.setenv("BROWSER_BACKEND", "cloakbrowser")
    monkeypatch.setenv("CLOAKBROWSER_HUMANIZE", "false")
    monkeypatch.setattr(browser_backend, "get_playwright_launch_options", lambda: {"headless": False})

    session = browser_backend.new_browser_session()

    assert session.backend == "cloakbrowser"
    assert session.browser is browser
    assert session.context is browser.context
    assert session.page is browser.context.page
    assert calls["launch"] == {"headless": False, "humanize": False}
    assert browser.context_kwargs["viewport"] == browser_backend.DEFAULT_VIEWPORT
    session.close()
    assert browser.context.closed is True
    assert browser.closed is True


def test_cloakbrowser_backend_uses_persistent_context(monkeypatch, tmp_path):
    calls = {}
    context = _FakeContext()

    def fake_launch_persistent_context(profile_dir, **kwargs):
        calls["profile_dir"] = profile_dir
        calls["kwargs"] = kwargs
        return context

    fake_module = types.SimpleNamespace(
        launch=lambda **_kwargs: (_ for _ in ()).throw(AssertionError()),
        launch_persistent_context=fake_launch_persistent_context,
    )
    monkeypatch.setitem(sys.modules, "cloakbrowser", fake_module)
    monkeypatch.setenv("BROWSER_BACKEND", "cloakbrowser")
    monkeypatch.setenv("CLOAKBROWSER_PROFILE_DIR", str(tmp_path))
    monkeypatch.setattr(browser_backend, "get_playwright_launch_options", lambda: {"headless": False})

    session = browser_backend.new_browser_session()

    assert session.browser is None
    assert session.context is context
    assert calls["profile_dir"] == str(tmp_path)
    assert calls["kwargs"]["humanize"] is True
    session.close()
    assert context.closed is True


def test_cloakbrowser_backend_appends_profile_seed(monkeypatch, tmp_path):
    calls = {}
    context = _FakeContext()

    def fake_launch_persistent_context(profile_dir, **kwargs):
        calls["profile_dir"] = profile_dir
        calls["kwargs"] = kwargs
        return context

    fake_module = types.SimpleNamespace(
        launch=lambda **_kwargs: (_ for _ in ()).throw(AssertionError()),
        launch_persistent_context=fake_launch_persistent_context,
    )
    monkeypatch.setitem(sys.modules, "cloakbrowser", fake_module)
    monkeypatch.setenv("BROWSER_BACKEND", "cloakbrowser")
    monkeypatch.setenv("CLOAKBROWSER_PROFILE_DIR", str(tmp_path))
    monkeypatch.setenv("CLOAKBROWSER_PROFILE_SEED", "round 2/seed")
    monkeypatch.setattr(browser_backend, "get_playwright_launch_options", lambda: {"headless": False})

    session = browser_backend.new_browser_session()

    assert calls["profile_dir"] == str(tmp_path / "round_2_seed")
    assert (tmp_path / "round_2_seed").exists()
    session.close()
