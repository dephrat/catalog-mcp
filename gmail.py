"""The Gmail REST transport.

Same role as graph.py, different shape of API. Gmail has no folders — a
message carries a set of label ids — and change detection is one
mailbox-wide history feed keyed by a historyId, so the whole mailbox is a
single change source. History is used exactly the way delta is on the
Microsoft side: as a change *detector* whose payload is thread ids, with
full threads refetched afterwards through get_thread.
"""
import base64
import os
import time

import requests

from urllib.parse import quote

GMAIL_BASE = "https://gmail.googleapis.com/gmail/v1/users/me"

# Label-equivalents of Graph's excluded well-known folders:
#   SPAM  - costs tagging tokens and dilutes search
#   TRASH - deliberately discarded, auto-purged after 30 days so links rot
#   DRAFT - incomplete, and every autosave would churn the history feed
EXCLUDED_LABELS = frozenset({"SPAM", "TRASH", "DRAFT"})

# The one mailbox-wide change source. Gmail's history feed is not scoped to
# a label, so unlike Graph there is exactly one cursor per account.
MAILBOX_SOURCE_ID = "mailbox"


def get_headers(access_token):
    return {"Authorization": f"Bearer {access_token}"}


def _is_rate_limit_403(response):
    """Gmail reports per-user quota exhaustion as 403, not only 429.

    The status alone is ambiguous — a real permission error is also 403 —
    so only the documented quota reasons are treated as retryable.
    """
    try:
        errors = response.json()["error"]["errors"]
    except Exception:
        return False
    return any(e.get("reason") in ("rateLimitExceeded", "userRateLimitExceeded")
               for e in errors)


def make_request(headers, url, retries=5):
    """Make a Gmail API request with retry on rate limits and 5xx gateway errors.

    Rate limiting is pacing, not failure: those waits back off exponentially
    (capped at 64s) and never consume the retry budget, or a busy mailbox
    would fail threads that merely needed patience. `retries` bounds only
    gateway errors.
    """
    attempt = 0
    limited = 0
    while attempt < retries:
        try:
            response = requests.get(url, headers=headers, timeout=60)
        except requests.ConnectionError:
            # Transient network failure (DNS blips included) — same
            # treatment as a gateway error.
            attempt += 1
            wait = 5 * attempt
            print(f"Network error, retrying in {wait}s... "
                  f"(attempt {attempt}/{retries})")
            time.sleep(wait)
            continue
        if response.status_code == 429 or (
                response.status_code == 403 and _is_rate_limit_403(response)):
            wait = int(response.headers.get("Retry-After", 0)) or min(2 ** limited, 64)
            limited += 1
            print(f"Rate limited, waiting {wait}s...")
            time.sleep(wait)
            continue
        if response.status_code in (502, 503, 504):
            attempt += 1
            wait = 5 * attempt
            print(f"Gateway error {response.status_code}, retrying in {wait}s... "
                  f"(attempt {attempt}/{retries})")
            time.sleep(wait)
            continue
        response.raise_for_status()
        try:
            return response.json()
        except Exception as e:
            raise Exception(f"JSON parse error on {url}: {e}")
    raise Exception(f"Failed after {retries} retries: {url}")


def get_profile(access_token):
    """The signed-in mailbox: emailAddress plus the current historyId."""
    return make_request(get_headers(access_token), f"{GMAIL_BASE}/profile")


# ── History sync ──────────────────────────────────────────────────────────────

def history_changes(access_token, start_history_id=None, sync_query=None):
    """Page the mailbox history feed to completion.

    Returns (thread_ids, removed_message_ids, new_history_id, did_full_resync).

    With no cursor — or a cursor Gmail no longer honours (404: history is only
    retained for about a week) — falls back to enumerating every message in
    the mailbox and cursors from the profile's current historyId, mirroring
    how an expired Graph deltaLink restarts a folder from scratch.
    """
    headers = get_headers(access_token)

    if start_history_id:
        try:
            return _incremental_history(headers, start_history_id) + (False,)
        except requests.HTTPError as e:
            status = e.response.status_code if e.response is not None else None
            if status not in (404, 400):
                raise
            # historyId expired or rejected — restart from scratch.
            print(f"Gmail historyId invalid ({status}); full resync.")

    thread_ids, new_history_id = _full_enumeration(headers, sync_query)
    return thread_ids, [], new_history_id, True


def _incremental_history(headers, start_history_id):
    thread_ids, removed_ids = [], []
    # The response's historyId is the mailbox's current position, valid even
    # when nothing changed; it is the next cursor either way.
    new_history_id = start_history_id
    url = (f"{GMAIL_BASE}/history?startHistoryId={start_history_id}"
           "&maxResults=500")

    while url:
        data = make_request(headers, url)
        new_history_id = data.get("historyId") or new_history_id

        for h in data.get("history", []):
            # Any kind of change marks the thread for a full refetch, so the
            # record types collapse to "touched" vs "deleted".
            for added in h.get("messagesAdded", []):
                tid = (added.get("message") or {}).get("threadId")
                if tid:
                    thread_ids.append(tid)
            for deleted in h.get("messagesDeleted", []):
                msg = deleted.get("message") or {}
                if msg.get("id"):
                    removed_ids.append(msg["id"])
            for key in ("labelsAdded", "labelsRemoved"):
                for change in h.get(key, []):
                    tid = (change.get("message") or {}).get("threadId")
                    if tid:
                        thread_ids.append(tid)

        page_token = data.get("nextPageToken")
        url = (f"{GMAIL_BASE}/history?startHistoryId={start_history_id}"
               f"&maxResults=500&pageToken={page_token}") if page_token else None

    return thread_ids, removed_ids, new_history_id


def _full_enumeration(headers, sync_query=None):
    """Every thread id in the mailbox, plus the historyId to cursor from.

    The profile's historyId is read *before* the listing: mail arriving
    during the walk is then re-reported by the next incremental sync rather
    than lost in the gap.

    The walk honours GMAIL_SYNC_QUERY (a Gmail search query, e.g.
    "newer_than:6m") so a first import can be scoped to recent mail. Later
    incremental syncs follow the history cursor and ignore it, so new mail
    still arrives; deduplication is by thread id, and the walk sees every
    thread with a matching message, so widening the query later and
    re-importing only adds.
    """
    profile = make_request(headers, f"{GMAIL_BASE}/profile")
    new_history_id = profile.get("historyId")

    base = f"{GMAIL_BASE}/messages?maxResults=500&includeSpamTrash=false"
    # An explicit per-user window wins over the env default; "" means the
    # user chose everything, so the env window must not apply either.
    if sync_query is None:
        sync_query = os.getenv("GMAIL_SYNC_QUERY", "")
    sync_query = sync_query.strip()
    if sync_query:
        base += f"&q={quote(sync_query)}"

    # Dedup here, not just downstream: a 40-message thread would otherwise
    # be reported 40 times.
    thread_ids = []
    seen = set()
    url = base
    while url:
        data = make_request(headers, url)
        for m in data.get("messages", []):
            tid = m.get("threadId")
            if tid and tid not in seen:
                seen.add(tid)
                thread_ids.append(tid)
        page_token = data.get("nextPageToken")
        url = f"{base}&pageToken={page_token}" if page_token else None

    return thread_ids, new_history_id


# ── Content ───────────────────────────────────────────────────────────────────

def get_thread(access_token, thread_id):
    """Full thread with message payloads; None if it no longer exists."""
    try:
        return make_request(
            get_headers(access_token),
            f"{GMAIL_BASE}/threads/{thread_id}?format=full",
        )
    except requests.HTTPError as e:
        status = e.response.status_code if e.response is not None else None
        if status == 404:
            return None
        raise


def get_message(access_token, message_id):
    """Full single message; None if it no longer exists."""
    try:
        return make_request(
            get_headers(access_token),
            f"{GMAIL_BASE}/messages/{message_id}?format=full",
        )
    except requests.HTTPError as e:
        status = e.response.status_code if e.response is not None else None
        if status == 404:
            return None
        raise


def get_attachment(access_token, message_id, attachment_id):
    """Raw attachment bytes, already base64url-decoded; None on failure.

    Decoding here keeps base64 vs base64url out of everything above the
    transport — the provider contract is that bytes arrive ready to use.
    """
    try:
        data = make_request(
            get_headers(access_token),
            f"{GMAIL_BASE}/messages/{message_id}/attachments/{attachment_id}",
        )
    except Exception as e:
        print(f"Attachment fetch error for {message_id}/{attachment_id}: {e}")
        return None
    encoded = (data or {}).get("data")
    if not encoded:
        return None
    try:
        return base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
    except Exception:
        return None


# ── Payload walking ───────────────────────────────────────────────────────────

def walk_parts(payload):
    """Flatten a MIME payload tree into a list of parts, root included."""
    parts = []
    stack = [payload or {}]
    while stack:
        part = stack.pop()
        parts.append(part)
        stack.extend(part.get("parts") or [])
    return parts


def extract_body(payload):
    """Best body from a payload tree: HTML preferred, text as fallback.

    strip_html upstream handles both, so handing back HTML when it exists
    keeps parity with Graph, whose body.content is usually HTML too.
    """
    html_part, text_part = "", ""
    for part in walk_parts(payload):
        if part.get("filename"):
            continue  # attachments are not body
        encoded = (part.get("body") or {}).get("data")
        if not encoded:
            continue
        try:
            decoded = base64.urlsafe_b64decode(
                encoded + "=" * (-len(encoded) % 4)).decode("utf-8", "replace")
        except Exception:
            continue
        mime = (part.get("mimeType") or "").lower()
        if mime == "text/html" and not html_part:
            html_part = decoded
        elif mime.startswith("text/") and not text_part:
            text_part = decoded

    return html_part or text_part


def extract_attachments(payload):
    """Attachment metadata from a payload tree, in normalised shape."""
    out = []
    for part in walk_parts(payload):
        body = part.get("body") or {}
        if part.get("filename") and body.get("attachmentId"):
            out.append({
                "id": body["attachmentId"],
                "name": part["filename"],
                "content_type": (part.get("mimeType") or "").lower(),
                "size": body.get("size", 0),
            })
    return out


def header_value(payload, name):
    """A single RFC 2822 header value, "" if absent."""
    for h in (payload or {}).get("headers", []):
        if (h.get("name") or "").lower() == name.lower():
            return h.get("value") or ""
    return ""
