"""The Gmail transport and provider boundary.

The change-detection model is the theme. Gmail has one mailbox-wide history
feed instead of per-folder deltas, and its cursor — a historyId — is only
retained for about a week, so expiry is the normal case after any pause in
syncing, not an edge case. An expired cursor must fall back to enumerating
the mailbox and report did_full_resync, exactly as an expired Graph
deltaLink does per folder.

Every test here stubs requests, so nothing reaches the network.
"""
import base64
import json

import pytest
import requests

import gauth
import gmail
import providers


class FakeResponse:
    def __init__(self, status=200, payload=None, headers=None, text=""):
        self.status_code = status
        self._payload = payload or {}
        self.headers = headers or {}
        self.text = text
        self.content = b"x"

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(response=self)


def b64url(data):
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def http_error(status):
    err = requests.HTTPError(response=FakeResponse(status))

    def raiser(*a, **k):
        raise err

    return raiser


@pytest.fixture(autouse=True)
def no_sleeping(monkeypatch):
    """Retries are exercised for their control flow, not their patience."""
    monkeypatch.setattr(gmail.time, "sleep", lambda s: None)


class TestMakeRequest:
    def test_a_rate_limit_403_is_retried_not_raised(self, monkeypatch):
        """Gmail reports per-user quota exhaustion as 403 rateLimitExceeded,
        which a parallel thread fetch reliably provokes."""
        limited = FakeResponse(403, {"error": {"errors": [
            {"reason": "userRateLimitExceeded"}]}})
        responses = [limited, limited, FakeResponse(200, {"ok": True})]
        monkeypatch.setattr(gmail.requests, "get",
                            lambda url, headers=None, **kw: responses.pop(0))
        assert gmail.make_request({}, "http://x") == {"ok": True}

    def test_a_permission_403_still_raises(self, monkeypatch):
        denied = FakeResponse(403, {"error": {"errors": [
            {"reason": "insufficientPermissions"}]}})
        monkeypatch.setattr(gmail.requests, "get",
                            lambda url, headers=None, **kw: denied)
        with pytest.raises(requests.HTTPError):
            gmail.make_request({}, "http://x")


class TestFullEnumeration:
    def test_sync_query_scopes_the_walk_and_threads_are_deduped(self, monkeypatch):
        monkeypatch.setenv("GMAIL_SYNC_QUERY", "newer_than:6m")
        urls = []

        def fake_request(headers, url, retries=5):
            urls.append(url)
            if "profile" in url:
                return {"historyId": "300"}
            return {"messages": [{"threadId": "t1"}, {"threadId": "t1"},
                                 {"threadId": "t2"}]}

        monkeypatch.setattr(gmail, "make_request", fake_request)
        tids, cursor = gmail._full_enumeration({})
        assert tids == ["t1", "t2"]
        assert cursor == "300"
        assert "q=newer_than%3A6m" in urls[-1]


class TestHistoryChanges:
    def test_pages_to_completion(self, monkeypatch):
        pages = [
            {"history": [{"messagesAdded": [{"message": {"threadId": "t1"}}]}],
             "historyId": "150", "nextPageToken": "p2"},
            {"history": [{"messagesAdded": [{"message": {"threadId": "t2"}}]}],
             "historyId": "160"},
        ]
        calls = {"n": 0}

        def fake_request(headers, url, retries=3):
            page = pages[calls["n"]]
            calls["n"] += 1
            if calls["n"] == 2:
                assert "pageToken=p2" in url
            return page

        monkeypatch.setattr(gmail, "make_request", fake_request)
        tids, removed, cursor, full = gmail.history_changes("tok", "100")
        assert tids == ["t1", "t2"]
        assert cursor == "160"
        assert full is False

    def test_removals_are_separated_from_changes(self, monkeypatch):
        monkeypatch.setattr(gmail, "make_request", lambda h, u, retries=3: {
            "history": [
                {"messagesAdded": [{"message": {"threadId": "t1"}}]},
                {"messagesDeleted": [{"message": {"id": "m9", "threadId": "t9"}}]},
            ],
            "historyId": "200",
        })
        tids, removed, cursor, full = gmail.history_changes("tok", "100")
        assert tids == ["t1"]
        assert removed == ["m9"]

    def test_label_changes_also_mark_the_thread(self, monkeypatch):
        """Archiving is a label removal; the thread must be re-verified, not
        ignored, or the catalog keeps a container_id the message lost."""
        monkeypatch.setattr(gmail, "make_request", lambda h, u, retries=3: {
            "history": [{"labelsRemoved": [{"message": {"threadId": "t3"}}]},
                        {"labelsAdded": [{"message": {"threadId": "t4"}}]}],
            "historyId": "201",
        })
        tids, _, _, _ = gmail.history_changes("tok", "100")
        assert tids == ["t3", "t4"]

    def test_no_changes_still_advances_the_cursor(self, monkeypatch):
        monkeypatch.setattr(gmail, "make_request",
                            lambda h, u, retries=3: {"historyId": "300"})
        tids, removed, cursor, full = gmail.history_changes("tok", "100")
        assert (tids, removed, cursor, full) == ([], [], "300", False)

    def test_an_expired_history_id_falls_back_to_full_enumeration(self, monkeypatch):
        """Gmail retains history for about a week; 404 after a pause is the
        normal case and must restart, not raise."""
        def fake_request(headers, url, retries=3):
            if "/history" in url:
                raise requests.HTTPError(response=FakeResponse(404))
            if "/profile" in url:
                return {"emailAddress": "a@example.com", "historyId": "999"}
            return {"messages": [{"id": "m1", "threadId": "t1"}]}

        monkeypatch.setattr(gmail, "make_request", fake_request)
        tids, removed, cursor, full = gmail.history_changes("tok", "old")
        assert tids == ["t1"]
        assert cursor == "999"
        assert full is True

    def test_a_first_run_reports_a_full_resync(self, monkeypatch):
        def fake_request(headers, url, retries=3):
            if "/profile" in url:
                return {"historyId": "50"}
            return {"messages": [{"id": "m1", "threadId": "t1"},
                                 {"id": "m2", "threadId": "t1"}]}

        monkeypatch.setattr(gmail, "make_request", fake_request)
        tids, removed, cursor, full = gmail.history_changes("tok", None)
        assert tids == ["t1"], "one thread id per thread, however many messages"
        assert cursor == "50"
        assert full is True

    def test_full_enumeration_reads_the_cursor_before_the_listing(self, monkeypatch):
        """The historyId must predate the walk: mail landing mid-walk is then
        re-reported next sync instead of falling into the gap forever."""
        order = []

        def fake_request(headers, url, retries=3):
            if "/profile" in url:
                order.append("profile")
                return {"historyId": "1"}
            order.append("messages")
            return {"messages": []}

        monkeypatch.setattr(gmail, "make_request", fake_request)
        gmail.history_changes("tok", None)
        assert order[0] == "profile"

    def test_a_server_error_mid_history_propagates(self, monkeypatch):
        """Only 404/400 mean an expired cursor. A 500 must raise so the sync
        holds the cursor and retries, rather than triggering a full resync."""
        monkeypatch.setattr(gmail, "make_request", http_error(500))
        with pytest.raises(requests.HTTPError):
            gmail.history_changes("tok", "100")


class TestPayloadWalking:
    def test_html_body_is_preferred_over_text(self):
        payload = {"mimeType": "multipart/alternative", "parts": [
            {"mimeType": "text/plain", "body": {"data": b64url(b"plain")}},
            {"mimeType": "text/html", "body": {"data": b64url(b"<p>rich</p>")}},
        ]}
        assert gmail.extract_body(payload) == "<p>rich</p>"

    def test_a_plain_text_only_message_still_yields_a_body(self):
        payload = {"mimeType": "text/plain", "body": {"data": b64url(b"hello")}}
        assert gmail.extract_body(payload) == "hello"

    def test_attachment_parts_are_not_mistaken_for_body(self):
        payload = {"mimeType": "multipart/mixed", "parts": [
            {"mimeType": "text/plain", "body": {"data": b64url(b"the body")}},
            {"mimeType": "text/plain", "filename": "notes.txt",
             "body": {"data": b64url(b"NOT the body"), "attachmentId": "a1"}},
        ]}
        assert gmail.extract_body(payload) == "the body"

    def test_unpadded_base64url_decodes(self):
        # Gmail strips padding; five bytes force one pad character.
        payload = {"mimeType": "text/plain", "body": {"data": b64url(b"hello")}}
        assert len(b64url(b"hello")) % 4 != 0
        assert gmail.extract_body(payload) == "hello"

    def test_nested_multiparts_are_walked(self):
        payload = {"mimeType": "multipart/mixed", "parts": [
            {"mimeType": "multipart/alternative", "parts": [
                {"mimeType": "text/html", "body": {"data": b64url(b"<b>deep</b>")}},
            ]},
            {"mimeType": "application/pdf", "filename": "doc.pdf",
             "body": {"attachmentId": "a1", "size": 123}},
        ]}
        assert gmail.extract_body(payload) == "<b>deep</b>"
        assert gmail.extract_attachments(payload) == [
            {"id": "a1", "name": "doc.pdf", "content_type": "application/pdf",
             "size": 123}]

    def test_inline_images_without_attachment_ids_are_not_attachments(self):
        payload = {"parts": [{"mimeType": "image/png", "filename": "logo.png",
                              "body": {"size": 10}}]}
        assert gmail.extract_attachments(payload) == []

    def test_header_lookup_is_case_insensitive(self):
        payload = {"headers": [{"name": "SUBJECT", "value": "Hi"}]}
        assert gmail.header_value(payload, "Subject") == "Hi"
        assert gmail.header_value(payload, "To") == ""


class TestGmailProviderBoundary:
    """The same contract test_graph.py::TestProviderBoundary pins for
    Microsoft, kept from the Gmail side."""

    def test_messages_are_normalised_away_from_gmail_shapes(self):
        normalised = providers.GmailProvider._normalise({
            "id": "m1",
            "threadId": "t1",
            "internalDate": "1709287200000",  # 2024-03-01T10:00:00Z
            "labelIds": ["INBOX", "IMPORTANT"],
            "payload": {
                "headers": [
                    {"name": "Subject", "value": "Hello"},
                    {"name": "From", "value": "Alice <a@example.com>"},
                    {"name": "To", "value": "Bob <b@example.com>, c@example.com"},
                ],
                "mimeType": "text/html",
                "body": {"data": b64url(b"<p>hi</p>")},
            },
        }, account="me@example.com")
        assert normalised == {
            "id": "m1", "thread_id": "t1", "subject": "Hello",
            "from_addr": "a@example.com",
            "to_addrs": ["b@example.com", "c@example.com"],
            "date": "2024-03-01T10:00:00+00:00", "has_attachments": False,
            "body": "<p>hi</p>",
            "web_link": "https://mail.google.com/mail/?authuser=me@example.com#all/m1",
            "container_id": "INBOX",
        }

    def test_an_excluded_label_wins_the_container_id(self):
        """container_id exists so excluded mail gets filtered out of rebuilt
        threads. A trashed message also carrying INBOX history must present
        as TRASH, or it reappears through thread reconstruction."""
        normalised = providers.GmailProvider._normalise(
            {"id": "m1", "labelIds": ["INBOX", "TRASH"]})
        assert normalised["container_id"] == "TRASH"

    def test_a_bare_message_normalises_with_fallbacks(self):
        normalised = providers.GmailProvider._normalise({"id": "m1"})
        assert normalised["thread_id"] == "m1"
        assert normalised["from_addr"] == ""
        assert normalised["to_addrs"] == []
        assert normalised["date"] == ""

    def test_changed_thread_ids_are_deduplicated(self, monkeypatch):
        monkeypatch.setattr(gmail, "history_changes",
                            lambda tok, cur: (["t1", "t2", "t1"], ["m9"], "new", False))
        tids, removed, cursor, full = providers.GmailProvider().changes_for_source(
            "tok", gmail.MAILBOX_SOURCE_ID, "old")
        assert tids == ["t1", "t2"]
        assert (removed, cursor, full) == (["m9"], "new", False)

    def test_a_vanished_thread_yields_an_empty_list(self, monkeypatch):
        monkeypatch.setattr(gmail, "get_thread", lambda tok, tid: None)
        assert providers.GmailProvider().get_thread("tok", "t1") == []

    def test_attachment_content_arrives_decoded(self, monkeypatch):
        monkeypatch.setattr(gmail, "make_request",
                            lambda h, u, retries=3: {"data": b64url(b"%PDF-real")})
        out = providers.GmailProvider().get_attachment_content("tok", [("m1", "a1")])
        assert out[("m1", "a1")] == b"%PDF-real", "extract_text expects bytes"

    def test_a_failed_attachment_is_dropped_rather_than_passed_on(self, monkeypatch):
        monkeypatch.setattr(gmail, "get_attachment", lambda tok, m, a: None)
        assert providers.GmailProvider().get_attachment_content(
            "tok", [("m1", "a1")]) == {}

    def test_attachment_metadata_covers_every_requested_message(self, monkeypatch):
        monkeypatch.setattr(gmail, "get_message", lambda tok, mid: {
            "payload": {"parts": [{"filename": "f.pdf", "mimeType": "application/pdf",
                                   "body": {"attachmentId": "a1", "size": 5}}]}
        } if mid == "m1" else None)
        out = providers.GmailProvider().get_attachment_metadata("tok", ["m1", "m2"])
        assert out["m1"][0]["name"] == "f.pdf"
        assert out["m2"] == [], "a vanished message yields [], not a missing key"

    def test_identity_comes_from_the_mailbox_profile(self, monkeypatch):
        monkeypatch.setattr(gmail, "get_profile",
                            lambda tok: {"emailAddress": "a@example.com", "historyId": "5"})
        me = providers.GmailProvider().get_identity("tok")
        assert me == {"id": "a@example.com", "email": "a@example.com", "display_name": ""}

    def test_one_mailbox_wide_change_source(self):
        sources = providers.GmailProvider().list_change_sources("tok")
        assert len(sources) == 1
        assert sources[0]["id"] == gmail.MAILBOX_SOURCE_ID

    def test_the_registry_resolves_gmail(self):
        assert providers.get("gmail").name == "gmail"
        assert providers.get(None).name == providers.DEFAULT_PROVIDER


class TestGoogleAuth:
    def _cache(self, expires_at):
        return json.dumps({"refresh_token": "R", "access_token": "A",
                           "expires_at": expires_at})

    def test_a_fresh_cached_token_makes_no_network_call(self, monkeypatch):
        import time as _t
        monkeypatch.setattr(gauth.requests, "post",
                            lambda *a, **k: pytest.fail("must not hit the network"))
        cache = self._cache(_t.time() + 3600)
        token, new_cache = gauth.get_valid_token(cache)
        assert token == "A"
        assert new_cache == cache

    def test_an_expired_token_is_refreshed_and_the_refresh_token_kept(self, monkeypatch):
        """Google usually omits refresh_token from a refresh response; a
        cache that dropped it would work once and die on the next expiry."""
        monkeypatch.setattr(gauth.requests, "post", lambda url, data=None, timeout=None:
                            FakeResponse(200, {"access_token": "A2", "expires_in": 3599}))
        token, new_cache = gauth.get_valid_token(self._cache(0))
        assert token == "A2"
        assert json.loads(new_cache)["refresh_token"] == "R"

    def test_a_dead_refresh_token_signals_relogin(self, monkeypatch):
        monkeypatch.setattr(gauth.requests, "post", lambda url, data=None, timeout=None:
                            FakeResponse(400, {"error": "invalid_grant"}))
        assert gauth.get_valid_token(self._cache(0)) == (None, None)

    def test_an_empty_or_garbage_cache_signals_relogin(self):
        assert gauth.get_valid_token(None) == (None, None)
        assert gauth.get_valid_token("not json") == (None, None)

    def test_code_exchange_serialises_the_refresh_token(self, monkeypatch):
        monkeypatch.setattr(gauth.requests, "post", lambda url, data=None, timeout=None:
                            FakeResponse(200, {"access_token": "A", "refresh_token": "R",
                                               "expires_in": 3599}))
        result, cache = gauth.get_token_from_code("code")
        assert result["access_token"] == "A"
        assert json.loads(cache)["refresh_token"] == "R"

    def test_a_failed_exchange_raises(self, monkeypatch):
        monkeypatch.setattr(gauth.requests, "post", lambda url, data=None, timeout=None:
                            FakeResponse(400, {"error": "invalid_request"}))
        with pytest.raises(Exception, match="Auth failed"):
            gauth.get_token_from_code("bad")

    def test_the_auth_url_requests_offline_consent(self):
        """Without access_type=offline + prompt=consent Google issues no
        refresh token on a repeat sign-in, and the first sync after an hour
        dies with an unrefreshable cache."""
        url = gauth.get_auth_url(state="S")
        assert "access_type=offline" in url
        assert "prompt=consent" in url
        assert "state=S" in url
        assert "gmail.readonly" in url
