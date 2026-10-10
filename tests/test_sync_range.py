"""Sync range endpoints: store the Gmail window, preview its cost."""
import app
import db
import gmail
import providers


def _client(user, monkeypatch, provider="gmail"):
    monkeypatch.setattr(app, "DEMO_MODE", False)
    monkeypatch.delenv("GMAIL_SYNC_QUERY", raising=False)
    app.app.config["TESTING"] = True
    c = app.app.test_client()
    with c.session_transaction() as s:
        s["user_id"] = user
        s["provider"] = provider
    return c


def _links(user):
    db.set_delta_link(user, "f1", "cursor")


class TestRange:
    def test_requires_login(self, fresh_db):
        app.app.config["TESTING"] = True
        assert app.app.test_client().get("/sync/range").status_code == 401

    def test_get_reports_window(self, user, monkeypatch):
        c = _client(user, monkeypatch)
        db.set_sync_after(user, "2024-01-01")
        j = c.get("/sync/range").get_json()
        assert j["sync_after"] == "2024-01-01" and j["editable"] is True

    def test_microsoft_post_is_400_and_stores_nothing(self, user, monkeypatch):
        c = _client(user, monkeypatch, provider="microsoft")
        r = c.post("/sync/range", json={"after": "2024-01-01"})
        assert r.status_code == 400
        assert r.get_json()["error"] == "sync range is gmail-only"
        assert db.get_sync_after(user) is None
        assert c.get("/sync/range").get_json()["editable"] is False

    def test_narrowing_keeps_cursors(self, user, monkeypatch):
        c = _client(user, monkeypatch)
        db.set_sync_after(user, "2024-01-01")
        _links(user)
        r = c.post("/sync/range", json={"after": "2025-01-01"})
        assert r.get_json() == {"stored": True, "full_rewalk_next_scan": False}
        assert db.get_sync_after(user) == "2025-01-01"
        assert db.get_delta_link(user, "f1") == "cursor"

    def test_widening_clears_cursors(self, user, monkeypatch):
        c = _client(user, monkeypatch)
        db.set_sync_after(user, "2024-01-01")
        _links(user)
        r = c.post("/sync/range", json={"after": "2023-01-01"})
        assert r.get_json() == {"stored": True, "full_rewalk_next_scan": True}
        assert db.get_delta_link(user, "f1") is None

    def test_everything_from_dated_widens(self, user, monkeypatch):
        c = _client(user, monkeypatch)
        db.set_sync_after(user, "2024-01-01")
        _links(user)
        r = c.post("/sync/range", json={"after": ""})
        assert r.get_json()["full_rewalk_next_scan"] is True
        assert db.get_sync_after(user) == ""

    def test_unset_with_env_default_is_dated(self, user, monkeypatch):
        c = _client(user, monkeypatch)
        monkeypatch.setenv("GMAIL_SYNC_QUERY", "after:2024/06/01")
        _links(user)
        r = c.post("/sync/range", json={"after": ""})
        assert r.get_json()["full_rewalk_next_scan"] is True
        assert db.get_delta_link(user, "f1") is None

    def test_unknown_prior_window_with_cursor_everything_widens(self, user, monkeypatch):
        c = _client(user, monkeypatch)
        _links(user)
        r = c.post("/sync/range", json={"after": ""})
        assert r.get_json() == {"stored": True, "full_rewalk_next_scan": True}
        assert db.get_delta_link(user, "f1") is None

    def test_unknown_prior_window_with_cursor_date_widens(self, user, monkeypatch):
        c = _client(user, monkeypatch)
        _links(user)
        r = c.post("/sync/range", json={"after": "2024-01-01"})
        assert r.get_json()["full_rewalk_next_scan"] is True
        assert db.get_delta_link(user, "f1") is None

    def test_resaving_identical_value_is_noop(self, user, monkeypatch):
        c = _client(user, monkeypatch)
        db.set_sync_after(user, "2024-01-01")
        _links(user)
        r = c.post("/sync/range", json={"after": "2024-01-01"})
        assert r.get_json() == {"stored": True, "full_rewalk_next_scan": False}
        assert db.get_delta_link(user, "f1") == "cursor"

    def test_unknown_prior_window_without_cursor_does_not_rewalk(self, user, monkeypatch):
        c = _client(user, monkeypatch)
        r = c.post("/sync/range", json={"after": ""})
        assert r.get_json() == {"stored": True, "full_rewalk_next_scan": False}
        assert db.get_sync_after(user) == ""

    def test_widen_during_running_sync_is_409_and_changes_nothing(self, user, monkeypatch):
        c = _client(user, monkeypatch)
        db.set_sync_after(user, "2024-01-01")
        _links(user)
        app.set_running(app.sync_running, user, True)
        try:
            r = c.post("/sync/range", json={"after": "2020-01-01"})
        finally:
            app.set_running(app.sync_running, user, False)
        assert r.status_code == 409
        assert db.get_sync_after(user) == "2024-01-01"
        assert db.get_delta_link(user, "f1") == "cursor"

    def test_bad_date_is_400(self, user, monkeypatch):
        c = _client(user, monkeypatch)
        for bad in ["nope", "2024-13-45", "2999-01-01", None, 5]:
            assert c.post("/sync/range", json={"after": bad}).status_code == 400
        assert db.get_sync_after(user) is None


class TestPreview:
    def _mock(self, monkeypatch, estimate=1000):
        calls = []

        def fake(headers, url, retries=5):
            calls.append(url)
            return {"resultSizeEstimate": estimate}
        monkeypatch.setattr(gmail, "make_request", fake)
        monkeypatch.setattr(app, "get_fresh_token", lambda: "tok")
        return calls

    def test_preview_estimates_cost(self, user, monkeypatch):
        c = _client(user, monkeypatch)
        calls = self._mock(monkeypatch)
        j = c.get("/sync/range/preview?after=2024-01-01").get_json()
        assert j == {"estimated_threads": 1000, "estimated_tagging_usd": 0.6,
                     "note": "estimate"}
        assert "maxResults=1" in calls[0] and "after%3A2024/01/01" in calls[0].replace("%2F", "/")

    def test_preview_everything_has_no_q(self, user, monkeypatch):
        c = _client(user, monkeypatch)
        calls = self._mock(monkeypatch)
        assert c.get("/sync/range/preview?after=").status_code == 200
        assert "q=" not in calls[0]

    def test_invalid_or_future_date_is_400_without_gmail_call(self, user, monkeypatch):
        c = _client(user, monkeypatch)
        calls = self._mock(monkeypatch)
        for bad in ["junk", "2024-02-31", "2999-01-01"]:
            assert c.get(f"/sync/range/preview?after={bad}").status_code == 400
        assert c.get("/sync/range/preview").status_code == 400
        assert calls == []

    def test_microsoft_preview_is_400(self, user, monkeypatch):
        c = _client(user, monkeypatch, provider="microsoft")
        assert c.get("/sync/range/preview?after=2024-01-01").status_code == 400


class TestRangeTemplate:
    def test_gmail_renders_control(self, user, monkeypatch):
        html = _client(user, monkeypatch).get("/").get_data(as_text=True)
        assert 'id="range-panel"' in html and 'id="range-change"' in html

    def test_microsoft_renders_disabled_with_reason(self, user, monkeypatch):
        html = _client(user, monkeypatch, provider="microsoft").get("/").get_data(as_text=True)
        assert "range control is gmail-only" in html
        assert 'id="range-panel"' not in html and 'id="range-change"' not in html

    def test_demo_hides_control(self, user, monkeypatch):
        c = _client(user, monkeypatch)
        monkeypatch.setattr(app, "DEMO_MODE", True)
        html = c.get("/").get_data(as_text=True)
        assert 'id="range-line"' not in html and 'id="range-panel"' not in html


class TestPreviewErrors:
    def test_expired_auth_is_401(self, user, monkeypatch):
        c = _client(user, monkeypatch)
        monkeypatch.setattr(app, "get_fresh_token", lambda: None)
        r = c.get("/sync/range/preview?after=2024-01-01")
        assert r.status_code == 401
        assert "sign-in expired — sign in again" in r.get_json()["error"]

    def test_gmail_http_error_is_502(self, user, monkeypatch):
        import requests
        c = _client(user, monkeypatch)
        monkeypatch.setattr(app, "get_fresh_token", lambda: "t")
        def boom(*a, **k):
            raise requests.HTTPError("503")
        monkeypatch.setattr(gmail, "make_request", boom)
        r = c.get("/sync/range/preview?after=2024-01-01")
        assert r.status_code == 502
        assert r.get_json()["error"] == "gmail did not answer — try again"
