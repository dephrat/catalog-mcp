"""Per-user Gmail sync window: db state, gmail query precedence, plumbing."""
import app
import db
import gmail
import providers


def _capture(monkeypatch):
    urls = []

    def fake_request(headers, url, retries=5):
        urls.append(url)
        if "profile" in url:
            return {"historyId": "1"}
        return {"messages": []}

    monkeypatch.setattr(gmail, "make_request", fake_request)
    return urls


class TestSyncAfterState:
    def test_unset_is_none(self, user):
        assert db.get_sync_after(user) is None

    def test_round_trip_distinguishes_everything_from_unset(self, user):
        db.set_sync_after(user, "2024-01-15")
        assert db.get_sync_after(user) == "2024-01-15"
        db.set_sync_after(user, "")
        assert db.get_sync_after(user) == ""
        assert db.get_sync_after("someone-else") is None


class TestGmailQueryPrecedence:
    def test_explicit_beats_env(self, monkeypatch):
        monkeypatch.setenv("GMAIL_SYNC_QUERY", "newer_than:6m")
        urls = _capture(monkeypatch)
        gmail._full_enumeration({}, sync_query="after:2024/01/15")
        assert "q=after%3A2024/01/15" in urls[-1]
        assert "newer_than" not in urls[-1]

    def test_explicit_empty_disables_env(self, monkeypatch):
        monkeypatch.setenv("GMAIL_SYNC_QUERY", "newer_than:6m")
        urls = _capture(monkeypatch)
        gmail._full_enumeration({}, sync_query="")
        assert "q=" not in urls[-1]

    def test_none_falls_back_to_env(self, monkeypatch):
        monkeypatch.setenv("GMAIL_SYNC_QUERY", "newer_than:6m")
        urls = _capture(monkeypatch)
        gmail._full_enumeration({}, sync_query=None)
        assert "q=newer_than%3A6m" in urls[-1]

    def test_history_changes_passes_it_through(self, monkeypatch):
        monkeypatch.setenv("GMAIL_SYNC_QUERY", "newer_than:6m")
        urls = _capture(monkeypatch)
        gmail.history_changes("tok", None, sync_query="after:2024/02/01")
        assert "q=after%3A2024/02/01" in urls[-1]

    def test_gmail_provider_forwards_sync_query(self, monkeypatch):
        seen = {}

        def fake(token, cursor, sync_query=None):
            seen["q"] = sync_query
            return [], [], "9", True

        monkeypatch.setattr(gmail, "history_changes", fake)
        providers.GmailProvider().changes_for_source(
            "t", gmail.MAILBOX_SOURCE_ID, None, sync_query="after:2024/01/01")
        assert seen["q"] == "after:2024/01/01"


class TestMicrosoftIgnoresWindow:
    def test_kwarg_is_accepted_and_not_forwarded(self, monkeypatch):
        calls = []

        def fake(token, source, cursor):
            calls.append((token, source, cursor))
            return [], [], "link", False

        import graph as ms_graph
        monkeypatch.setattr(ms_graph, "delta_messages", fake)
        out = providers.MicrosoftProvider().changes_for_source(
            "t", "f1", "c", sync_query="after:2024/01/01")
        assert calls == [("t", "f1", "c")]
        assert out[2] == "link"


class TestWindowQuery:
    def test_unset(self, user):
        assert app._window_query(user) is None

    def test_everything(self, user):
        db.set_sync_after(user, "")
        assert app._window_query(user) == ""

    def test_date_uses_gmail_slashes(self, user):
        db.set_sync_after(user, "2024-01-05")
        assert app._window_query(user) == "after:2024/01/05"
