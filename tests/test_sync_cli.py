"""sync_cli.py: run a mailbox sync from the command line.

No network and no real sync here — app.run_sync and the provider's token
refresh are monkeypatched. These tests only verify the CLI's own plumbing:
which user it resolves, whether it guards against a sync already running,
how it reacts to a dead refresh token, and what it hands to run_sync.
"""
import time

import pytest

import app
import db
import providers
import sync_cli


class FakeProvider:
    """Stand-in for a providers.MailProvider — scripted refresh, no network."""

    name = "fake"
    label = "FakeMail"

    def __init__(self, access_token="fresh-token", new_cache="new-cache"):
        self.access_token = access_token
        self.new_cache = new_cache
        self.refresh_calls = []

    def refresh_token(self, token_cache):
        self.refresh_calls.append(token_cache)
        if not self.access_token:
            return (None, None)
        return (self.access_token, self.new_cache)


@pytest.fixture
def seeded(user, monkeypatch):
    """A registered user with a stored (non-JSON) token cache, email known."""
    email = "owner@example.com"
    db.upsert_user(user, email, "Owner", "2024-01-01T00:00:00Z")
    db.set_token_cache(user, "msal-cache-blob")
    monkeypatch.delenv("CATALOG_USER", raising=False)
    return user, email


@pytest.fixture(autouse=True)
def clean_sync_state():
    """app.sync_running is process-global; never leak a claim between tests."""
    yield
    app.sync_running.clear()
    app.detective_running.clear()


def _patch_run_sync(monkeypatch):
    calls = []

    def fake_run_sync(user_id, provider, token, token_cache=None):
        calls.append((user_id, provider, token, token_cache))

    monkeypatch.setattr(app, "run_sync", fake_run_sync)
    return calls


def test_happy_path_calls_run_sync_with_refreshed_token(seeded, monkeypatch):
    user_id, email = seeded
    fake_provider = FakeProvider(access_token="fresh-token", new_cache="new-cache")
    monkeypatch.setattr(providers, "get", lambda name: fake_provider)
    calls = _patch_run_sync(monkeypatch)

    rc = sync_cli.main(["--user", email])

    assert rc == 0
    assert calls == [(user_id, fake_provider, "fresh-token", "msal-cache-blob")]
    assert db.get_token_cache(user_id) == "new-cache"


def test_dead_refresh_exits_2_with_sign_in_message(seeded, monkeypatch, capsys):
    user_id, email = seeded
    fake_provider = FakeProvider(access_token=None)
    monkeypatch.setattr(providers, "get", lambda name: fake_provider)
    calls = _patch_run_sync(monkeypatch)

    rc = sync_cli.main(["--user", email])

    assert rc == 2
    assert calls == []
    err = capsys.readouterr().err.lower()
    assert "sign" in err and "web app" in err


def test_already_running_exits_3_without_calling_run_sync(seeded, monkeypatch):
    """Exercises the in-process simulation only: app.sync_running is a
    per-process dict, so this proves the guard works within one process, not
    that it catches a sync the web app started in a different process."""
    user_id, email = seeded
    fake_provider = FakeProvider()
    monkeypatch.setattr(providers, "get", lambda name: fake_provider)
    calls = _patch_run_sync(monkeypatch)
    app.set_running(app.sync_running, user_id, True)

    rc = sync_cli.main(["--user", email])

    assert rc == 3
    assert calls == []
    assert fake_provider.refresh_calls == []


def test_ambiguous_user_exits_2(monkeypatch, fresh_db, capsys):
    """resolve_user()'s ValueError (e.g. multiple users, no CATALOG_USER/--user
    to disambiguate) must land as a documented exit 2, not an uncaught
    traceback."""
    db.upsert_user("u1", "a@example.com", "A", "2024-01-01T00:00:00Z")
    db.upsert_user("u2", "b@example.com", "B", "2024-01-01T00:00:00Z")
    monkeypatch.delenv("CATALOG_USER", raising=False)
    calls = _patch_run_sync(monkeypatch)

    rc = sync_cli.main([])

    assert rc == 2
    assert calls == []
    err = capsys.readouterr().err
    assert "multiple users" in err


def test_no_such_catalog_user_exits_2(monkeypatch, fresh_db, capsys):
    db.upsert_user("u1", "a@example.com", "A", "2024-01-01T00:00:00Z")
    calls = _patch_run_sync(monkeypatch)

    rc = sync_cli.main(["--user", "nobody@example.com"])

    assert rc == 2
    assert calls == []
    err = capsys.readouterr().err
    assert "no such user" in err


def test_db_heartbeat_already_running_exits_3_without_calling_run_sync(
        seeded, monkeypatch):
    """The in-process app.sync_running dict can't see a sync the web app's
    own gunicorn worker started in a different process — only the DB-backed
    progress/heartbeat (the same thing app.py's /sync route trusts) can.
    This exercises that path with app.sync_running left untouched."""
    user_id, email = seeded
    fake_provider = FakeProvider()
    monkeypatch.setattr(providers, "get", lambda name: fake_provider)
    calls = _patch_run_sync(monkeypatch)
    assert not app.is_running(app.sync_running, user_id)
    db.set_sync_progress(user_id, 3, 10, "indexing threads")

    rc = sync_cli.main(["--user", email])

    assert rc == 3
    assert calls == []
    assert fake_provider.refresh_calls == []


def test_db_heartbeat_stale_status_does_not_block(seeded, monkeypatch):
    """A recorded active status whose heartbeat has gone quiet (stale) means
    the previous sync died rather than genuinely being in flight — the CLI
    must be allowed to start a new one, mirroring app.py's /sync route."""
    user_id, email = seeded
    fake_provider = FakeProvider()
    monkeypatch.setattr(providers, "get", lambda name: fake_provider)
    calls = _patch_run_sync(monkeypatch)
    db.set_sync_progress(user_id, 3, 10, "indexing threads")
    monkeypatch.setattr(db, "STALE_AFTER_SECONDS", -1)  # instantly stale

    rc = sync_cli.main(["--user", email])

    assert rc == 0
    assert calls == [(user_id, fake_provider, "fresh-token", "msal-cache-blob")]


def test_run_sync_recorded_error_status_exits_1(seeded, monkeypatch):
    """run_sync swallows its own failures into sync_state rather than
    raising (app.py's run_sync records status "error" in its except clause).
    A CLI run that quietly returned 0 over that would hide a failed sync."""
    user_id, email = seeded
    fake_provider = FakeProvider()
    monkeypatch.setattr(providers, "get", lambda name: fake_provider)

    def fake_run_sync(*args, **kwargs):
        db.set_sync_progress(user_id, 0, 0, "error")

    monkeypatch.setattr(app, "run_sync", fake_run_sync)

    rc = sync_cli.main(["--user", email])

    assert rc == 1


def test_run_sync_recorded_auth_expired_status_exits_2_with_sign_in_message(
        seeded, monkeypatch, capsys):
    user_id, email = seeded
    fake_provider = FakeProvider()
    monkeypatch.setattr(providers, "get", lambda name: fake_provider)

    def fake_run_sync(*args, **kwargs):
        db.set_sync_progress(user_id, 0, 0, "auth_expired")

    monkeypatch.setattr(app, "run_sync", fake_run_sync)

    rc = sync_cli.main(["--user", email])

    assert rc == 2
    err = capsys.readouterr().err.lower()
    assert "sign" in err and "web app" in err


def test_no_stored_credentials_exits_2(user, monkeypatch, capsys):
    """Never signed in via the web app: no token cache at all."""
    email = "nocache@example.com"
    db.upsert_user(user, email, "Owner", "2024-01-01T00:00:00Z")
    monkeypatch.delenv("CATALOG_USER", raising=False)
    calls = _patch_run_sync(monkeypatch)

    rc = sync_cli.main(["--user", email])

    assert rc == 2
    assert calls == []
    err = capsys.readouterr().err.lower()
    assert "sign" in err and "web app" in err
