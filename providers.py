"""Mail provider abstraction.

Everything above this layer — thread building, tagging, storage, search —
is provider-agnostic. Each provider is responsible for authentication and
for translating its own API into the normalised shapes below.

Normalised message:
    {
        "id":              str,   provider message id
        "thread_id":       str,   conversation/thread this belongs to
        "subject":         str,
        "from_addr":       str,   single address, "" if unknown
        "to_addrs":        [str],
        "date":            str,   ISO 8601, sortable
        "has_attachments": bool,
        "body":            str,   raw HTML or text; caller strips markup
        "web_link":        str,   deep link back into the provider's UI
        "container_id":    str,   folder/label id, used for exclusions
    }

Normalised attachment metadata:
    {"id": str, "name": str, "content_type": str, "size": int}

Change detection is expressed as *sources* with *cursors* rather than
folders with delta tokens, because the two providers differ here:
Microsoft Graph exposes a delta feed per mail folder, while Gmail exposes
a single mailbox-wide history feed. A provider that needs only one cursor
returns a single source.
"""

import auth as ms_auth
import gauth as g_auth
import gmail as g_mail
import graph as ms_graph


class MailProvider:
    """Interface every provider implements. See module docstring for shapes."""

    name = "unset"
    label = "unset"

    # ── Authentication ────────────────────────────────────────────────────
    def auth_url(self, state=None):
        """`state` is echoed back to the callback unmodified, and is how the
        callback tells its own redirect from one an attacker induced."""
        raise NotImplementedError

    def token_from_code(self, code):
        """-> (access_token, serialised_token_cache)"""
        raise NotImplementedError

    def refresh_token(self, token_cache):
        """-> (access_token or None, serialised_token_cache or None)"""
        raise NotImplementedError

    def get_identity(self, access_token):
        """-> {"id", "email", "display_name"}"""
        raise NotImplementedError

    # ── Change detection ──────────────────────────────────────────────────
    def excluded_container_ids(self, access_token):
        """Container ids whose messages should never be indexed."""
        raise NotImplementedError

    def list_change_sources(self, access_token, exclude_ids=frozenset()):
        """-> [{"id", "name"}]  — each carries its own cursor."""
        raise NotImplementedError

    def changes_for_source(self, access_token, source_id, cursor, *,
                           sync_query=None):
        """-> (thread_ids, removed_message_ids, new_cursor, did_full_resync)"""
        raise NotImplementedError

    # ── Content ───────────────────────────────────────────────────────────
    def get_thread(self, access_token, thread_id):
        """-> [normalised message]; empty if the thread no longer exists."""
        raise NotImplementedError

    def get_attachment_metadata(self, access_token, message_ids):
        """-> {message_id: [normalised attachment metadata]}"""
        raise NotImplementedError

    def get_attachment_content(self, access_token, pairs):
        """pairs: [(message_id, attachment_id)] -> {pair: raw bytes}"""
        raise NotImplementedError


class MicrosoftProvider(MailProvider):
    """Personal Microsoft accounts via Graph."""

    name = "microsoft"
    label = "Microsoft / Outlook"

    def auth_url(self, state=None):
        return ms_auth.get_auth_url(state=state)

    def token_from_code(self, code):
        token, cache = ms_auth.get_token_from_code(code)
        return token["access_token"], cache

    def refresh_token(self, token_cache):
        return ms_auth.get_valid_token(token_cache)

    def get_identity(self, access_token):
        me = ms_graph.get_me(access_token)
        return {
            "id": me.get("id"),
            "email": me.get("mail") or me.get("userPrincipalName") or "",
            "display_name": me.get("displayName") or "",
        }

    def excluded_container_ids(self, access_token):
        return ms_graph.get_excluded_folder_ids(access_token)

    def list_change_sources(self, access_token, exclude_ids=frozenset()):
        # One source per mail folder: Graph's delta feed is folder-scoped.
        return ms_graph.list_mail_folders(access_token, exclude_ids=exclude_ids)

    def changes_for_source(self, access_token, source_id, cursor, *,
                           sync_query=None):
        # Graph delta has no window; the per-user range is Gmail-only.
        changed, removed, new_cursor, full = ms_graph.delta_messages(
            access_token, source_id, cursor
        )
        thread_ids = [c["conversationId"] for c in changed if c.get("conversationId")]
        return thread_ids, removed, new_cursor, full

    def get_thread(self, access_token, thread_id):
        data = ms_graph.get_thread_messages(access_token, thread_id)
        return [self._normalise(m) for m in data.get("value", []) if m.get("id")]

    @staticmethod
    def _normalise(m):
        return {
            "id": m["id"],
            "thread_id": m.get("conversationId") or m["id"],
            "subject": m.get("subject") or "",
            "from_addr": (m.get("from") or {}).get("emailAddress", {}).get("address") or "",
            "to_addrs": [
                r.get("emailAddress", {}).get("address")
                for r in (m.get("toRecipients") or [])
                if r.get("emailAddress", {}).get("address")
            ],
            "date": m.get("receivedDateTime") or "",
            "has_attachments": bool(m.get("hasAttachments")),
            "body": (m.get("body") or {}).get("content") or m.get("bodyPreview") or "",
            "web_link": m.get("webLink") or "",
            "container_id": m.get("parentFolderId") or "",
        }

    def get_attachment_metadata(self, access_token, message_ids):
        raw = ms_graph.batch_get_attachment_metadata(access_token, message_ids)
        return {
            mid: [
                {
                    "id": a.get("id"),
                    "name": a.get("name") or "",
                    "content_type": (a.get("contentType") or "").lower(),
                    "size": a.get("size", 0),
                }
                for a in items
                if a.get("id")
            ]
            for mid, items in raw.items()
        }

    def get_attachment_content(self, access_token, pairs):
        import base64

        raw = ms_graph.batch_get_attachment_content(access_token, pairs)
        out = {}
        for pair, payload in raw.items():
            if not payload:
                continue
            content = payload.get("contentBytes")
            if not content:
                continue
            try:
                out[pair] = base64.b64decode(content)
            except Exception:
                continue
        return out


class GmailProvider(MailProvider):
    """Google accounts via the Gmail REST API."""

    name = "gmail"
    label = "Google / Gmail"

    def auth_url(self, state=None):
        return g_auth.get_auth_url(state=state)

    def token_from_code(self, code):
        token, cache = g_auth.get_token_from_code(code)
        return token["access_token"], cache

    def refresh_token(self, token_cache):
        return g_auth.get_valid_token(token_cache)

    def get_identity(self, access_token):
        # gmail.readonly is the only scope, so identity comes from the
        # mailbox profile: the address is both the id and the email. Google
        # addresses are stable, and asking for an OpenID scope solely to get
        # a numeric subject would widen the consent screen for nothing.
        profile = g_mail.get_profile(access_token)
        email = profile.get("emailAddress") or ""
        return {"id": email, "email": email, "display_name": ""}

    def excluded_container_ids(self, access_token):
        # System labels with fixed ids — no lookup call needed.
        return set(g_mail.EXCLUDED_LABELS)

    def list_change_sources(self, access_token, exclude_ids=frozenset()):
        # One source: Gmail's history feed spans the whole mailbox.
        return [{"id": g_mail.MAILBOX_SOURCE_ID, "name": "All Mail"}]

    def changes_for_source(self, access_token, source_id, cursor, *,
                           sync_query=None):
        thread_ids, removed, new_cursor, full = g_mail.history_changes(
            access_token, cursor, sync_query=sync_query
        )
        # History reports one record per change; a busy thread repeats.
        return list(dict.fromkeys(thread_ids)), removed, new_cursor, full

    def get_thread(self, access_token, thread_id):
        data = g_mail.get_thread(access_token, thread_id)
        if not data:
            return []
        account = self._account_email(access_token)
        return [self._normalise(m, account)
                for m in data.get("messages", []) if m.get("id")]

    # Access tokens rotate roughly hourly, so this stays a handful of
    # entries; it exists so a full sync costs one profile call, not one
    # per thread.
    _email_by_token = {}

    def _account_email(self, access_token):
        email = self._email_by_token.get(access_token)
        if email is None:
            email = g_mail.get_profile(access_token).get("emailAddress") or ""
            if len(self._email_by_token) > 16:
                self._email_by_token.clear()
            self._email_by_token[access_token] = email
        return email

    @staticmethod
    def _normalise(m, account=""):
        from datetime import datetime, timezone
        from email.utils import getaddresses, parseaddr

        payload = m.get("payload") or {}

        # internalDate is ms since epoch and is what Gmail itself sorts by;
        # the Date header is sender-supplied and lies.
        date = ""
        if m.get("internalDate"):
            try:
                date = datetime.fromtimestamp(
                    int(m["internalDate"]) / 1000, tz=timezone.utc).isoformat()
            except (ValueError, OverflowError):
                pass

        # container_id is singular upstream but Gmail labels are a set. What
        # the exclusion filter needs is: does this message sit somewhere
        # excluded? So an excluded label wins; otherwise any label stands in.
        labels = m.get("labelIds") or []
        container = next((l for l in labels if l in g_mail.EXCLUDED_LABELS),
                         labels[0] if labels else "")

        return {
            "id": m["id"],
            "thread_id": m.get("threadId") or m["id"],
            "subject": g_mail.header_value(payload, "Subject"),
            "from_addr": parseaddr(g_mail.header_value(payload, "From"))[1],
            "to_addrs": [
                addr for _, addr in getaddresses(
                    [g_mail.header_value(payload, "To")]) if addr
            ],
            "date": date,
            "has_attachments": bool(g_mail.extract_attachments(payload)),
            "body": g_mail.extract_body(payload),
            # #all resolves regardless of which label the message carries.
            # ?authuser=<address> pins the link to the synced account —
            # /u/0 is merely "first signed-in account", and the /u/<email>
            # path form 404s intermittently in multi-account sessions.
            "web_link": (f"https://mail.google.com/mail/?authuser={account}"
                         f"#all/{m['id']}" if account else
                         f"https://mail.google.com/mail/u/0/#all/{m['id']}"),
            "container_id": container,
        }

    def get_attachment_metadata(self, access_token, message_ids):
        # No $batch equivalent worth the multipart plumbing here: attachment-
        # bearing messages are a small minority (9 threads in 90 days on the
        # measured mailbox), so these stay individual GETs.
        results = {}
        for mid in message_ids:
            msg = g_mail.get_message(access_token, mid)
            results[mid] = g_mail.extract_attachments(
                (msg or {}).get("payload")) if msg else []
        return results

    def get_attachment_content(self, access_token, pairs):
        out = {}
        for mid, aid in pairs:
            content = g_mail.get_attachment(access_token, mid, aid)
            if content:
                out[(mid, aid)] = content
        return out


PROVIDERS = {p.name: p for p in (MicrosoftProvider(), GmailProvider())}
DEFAULT_PROVIDER = "microsoft"


def get(name):
    return PROVIDERS.get(name or DEFAULT_PROVIDER, PROVIDERS[DEFAULT_PROVIDER])
