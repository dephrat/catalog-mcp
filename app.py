import os
import sys
from functools import wraps
from flask import Flask, redirect, request, session, url_for, render_template, jsonify
from dotenv import load_dotenv
import anthropic
import providers
import db
import backup
import tagger
import extractor
from datetime import datetime, timezone, timedelta
from concurrent.futures import ThreadPoolExecutor
import json
import threading
import time
import re
import secrets
import hmac
import hashlib
import base64
import gzip
import zlib
import requests

# Per-user job state. Keyed by user_id so one account's sync can't block another's.
# Guarded by _state_lock; safe under the single gunicorn worker this app runs with.
sync_running = {}
detective_running = {}
_state_lock = threading.Lock()

load_dotenv()

# Container stdout is a pipe, so Python block-buffers it and print() output
# never reaches the platform's log stream. Reconfigure before anything logs.
try:
    sys.stdout.reconfigure(line_buffering=True)
    sys.stderr.reconfigure(line_buffering=True)
except AttributeError:  # pragma: no cover - very old interpreters
    pass

# ── Demo mode ─────────────────────────────────────────────────────────────────
# A fabricated catalog behind a bypassed login, so the app is evaluable
# without an Azure registration or a mailbox. The gate is structural, not
# configuration: it requires being executed as a script AND the flag, and
# gunicorn rejects unknown arguments outright — a deployed worker cannot
# start with --demo, and an env-var typo on the dashboard cannot enable it.
DEMO_MODE = __name__ == "__main__" and "--demo" in sys.argv

# Imported unconditionally: it is pure data with no side effects, and
# importing it only under the flag left app.login referencing a name that
# does not exist whenever DEMO_MODE is set after import — which is what any
# test of the bypass has to do.
import demo_seed

if DEMO_MODE:
    # Never the real catalog, whatever DB_PATH says. The demo database is
    # disposable by design; *.db is gitignored so it also cannot commit.
    db.DB_PATH = "demo_catalog.db"

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY")
if not app.secret_key:
    if DEMO_MODE:
        # Ephemeral: demo sessions may die on restart, but a cloner without a
        # .env gets a working app instead of an error about one.
        app.secret_key = secrets.token_urlsafe(32)
    else:
        raise RuntimeError("SECRET_KEY is not set — sessions would be insecure. Set it in .env.")

# ── Session cookie hardening ──────────────────────────────────────────────────
# Secure is conditional because it would break http://localhost, where the
# demo and local development run. PUBLIC_BASE_URL is the honest signal that
# this instance is served over TLS.
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=(os.getenv("PUBLIC_BASE_URL", "").startswith("https://")
                           or os.getenv("REDIRECT_URI", "").startswith("https://")),
)

db.init_db()

if DEMO_MODE:
    print(f"Demo mode: seeded {demo_seed.seed(db)} fabricated threads "
          f"into {db.DB_PATH}")


def recover_after_restart():
    """Clean up work orphaned by whatever ended the previous process.

    A restart mid-sync used to leave a progress bar frozen forever and a paid
    batch uncollected until someone manually triggered another sync.
    """
    interrupted = db.mark_interrupted_syncs()
    if interrupted:
        print(f"  recovered           : {interrupted} interrupted sync(s) marked")

    pending = db.users_with_pending_batches()
    for user_id in pending:
        try:
            tagged = resume_pending_batches(user_id)
            if tagged:
                print(f"  recovered           : {tagged} threads tagged from a pending batch")
        except Exception as e:
            print(f"  batch recovery failed for {user_id}: {e}")


def log_startup_config():
    import threading as _t
    print("─" * 60)
    print("catalog starting")
    print(f"  db path            : {db.DB_PATH}")
    print(f"  admin emails       : {sorted(ADMIN_EMAILS) or 'NONE — nobody can sign in'}")
    print(f"  notify email       : {NOTIFY_EMAIL or '(unset — approvals only at /admin)'}")
    print(f"  resend configured  : {bool(RESEND_API_KEY)}")
    print(f"  public base url    : {PUBLIC_BASE_URL or '(unset)'}")
    print(f"  tag model          : {tagger.TAG_MODEL}")
    print(f"  batch size         : {BATCH_SIZE} threads/request")
    print(f"  batch API above    : {BATCH_API_THRESHOLD} threads")
    print(f"  per-user spend cap : "
          f"{('$%.2f/month' % USER_SPEND_LIMIT_USD) if USER_SPEND_LIMIT_USD else 'none'}")
    print(f"  batch max wait     : {BATCH_MAX_WAIT_SECONDS}s")
    print(f"  scheduled backups  : "
          f"{('every %gh, keeping %d' % (BACKUP_INTERVAL_HOURS, BACKUP_KEEP)) if BACKUP_INTERVAL_HOURS > 0 else 'off'}")
    print(f"  server threads     : {_t.active_count()} active "
          f"(gunicorn worker class is logged separately above)")
    print("─" * 60)


anthropic_client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

# Threads per tagging request. The instruction block (~375 tokens) is resent
# with every request, so larger batches amortise it: 125 tok/thread at 3, 37 at
# 10. Bounded by output truncation — 10 threads x 60 tags is ~2,400 tokens
# against a max_tokens of 8,000.
BATCH_SIZE = 10

# Above this many threads a sync tags via the Batch API at half price. Below
# it the saving is pennies and real-time is instant, so it isn't worth the wait.
BATCH_API_THRESHOLD = 50
# How long to wait on a batch before tagging the remainder in real time. The
# API's own ceiling is 24h; this caps what a user actually experiences.
BATCH_MAX_WAIT_SECONDS = 2 * 3600
BATCH_POLL_SECONDS = 30
# Batch results stay retrievable for 29 days; past that a pending record is
# genuinely dead rather than temporarily unreadable.
BATCH_RECORD_TTL_DAYS = 29
# Consecutive unreadable status checks tolerated before giving up on a batch
# we're actively waiting on. A transient 401/429/network blip must not throw
# away work that has already been paid for.
BATCH_UNKNOWN_TOLERANCE = 3
DETECTIVE_MODEL = "claude-sonnet-4-6"

# Server-side bounds on a Detective session. The browser enforces its own
# round limit, but that is a courtesy: /detective/ask relays whatever it is
# given, so without these an authenticated caller could drive the operator's
# API key as an open-ended model endpoint. Both are deliberately looser than
# the client's own limits — they stop abuse, not ordinary use.
DETECTIVE_MAX_MESSAGES = 60          # ~2 per round against a 20-round client
DETECTIVE_MAX_CHARS = 400_000        # whole conversation, resent every round

# ── Access control config ─────────────────────────────────────────────────────
# Two distinct jobs, deliberately separate env vars:
#   ADMIN_EMAIL  - the Microsoft identity that is auto-approved (matched against
#                  what Graph /me returns; for consumer accounts that is usually
#                  userPrincipalName, not mail).
#   NOTIFY_EMAIL - where access-request notifications are delivered. Defaults to
#                  ADMIN_EMAIL, but they are often different inboxes.
ADMIN_EMAILS = {
    e.strip().lower()
    for e in (os.getenv("ADMIN_EMAIL") or "").split(",")
    if e.strip()
}
# Deliberately not defaulted to "some admin": ADMIN_EMAILS is a set, so the
# fallback picked an arbitrary member that could change between restarts.
# Notifications either go somewhere chosen, or nowhere.
NOTIFY_EMAIL = (os.getenv("NOTIFY_EMAIL") or "").strip()

if DEMO_MODE:
    ADMIN_EMAILS = {demo_seed.DEMO_EMAIL}
    NOTIFY_EMAIL = ""



def is_admin(email):
    return bool(ADMIN_EMAILS) and (email or "").strip().lower() in ADMIN_EMAILS
DENY_COOLDOWN_MINUTES = 10
DECISION_LINK_TTL_HOURS = 168          # emailed approve/deny links last a week

RESEND_API_KEY = os.getenv("RESEND_API_KEY")
MAIL_FROM = os.getenv("MAIL_FROM", "catalog@ephrat.ai")
PUBLIC_BASE_URL = (os.getenv("PUBLIC_BASE_URL") or "").rstrip("/")


# ── Signed decision links ─────────────────────────────────────────────────────
#
# These are bearer capabilities delivered by email, so they are HMAC-signed
# with SECRET_KEY and carry an expiry. The link itself only renders a
# confirmation page — the decision is applied by a POST — because mail
# providers and security scanners routinely prefetch GET links, which would
# otherwise auto-approve every request.

def sign_decision(user_id, decision, expires_at):
    payload = f"{user_id}|{decision}|{expires_at}"
    sig = hmac.new(app.secret_key.encode(), payload.encode(), hashlib.sha256).hexdigest()
    return base64.urlsafe_b64encode(f"{payload}|{sig}".encode()).decode().rstrip("=")


def verify_decision(token):
    """Return (user_id, decision) or None if forged, malformed, or expired."""
    try:
        padded = token + "=" * (-len(token) % 4)
        raw = base64.urlsafe_b64decode(padded.encode()).decode()
        user_id, decision, expires_at, sig = raw.rsplit("|", 3)
    except Exception:
        return None

    expected = hmac.new(
        app.secret_key.encode(),
        f"{user_id}|{decision}|{expires_at}".encode(),
        hashlib.sha256,
    ).hexdigest()
    if not hmac.compare_digest(sig, expected):
        return None
    if decision not in ("approve", "deny"):
        return None
    try:
        if time.time() > float(expires_at):
            return None
    except ValueError:
        return None
    return user_id, decision


def send_access_request_email(req):
    """Notify the admin of a pending request. Never raises into the login path."""
    if not RESEND_API_KEY or not NOTIFY_EMAIL:
        missing = []
        if not RESEND_API_KEY:
            missing.append("RESEND_API_KEY")
        if not NOTIFY_EMAIL:
            missing.append("NOTIFY_EMAIL")
        print(f"[access] pending request from {req['email']} — no email sent "
              f"({', '.join(missing)} unset). Approve at /admin.")
        return False

    expires = time.time() + DECISION_LINK_TTL_HOURS * 3600
    base = PUBLIC_BASE_URL or ""
    approve = f"{base}/access/decide/{sign_decision(req['user_id'], 'approve', expires)}"
    deny = f"{base}/access/decide/{sign_decision(req['user_id'], 'deny', expires)}"

    name = req.get("display_name") or "(no name)"
    html = f"""
      <p><strong>{name}</strong> ({req['email']}) requested access to Catalog.</p>
      <p>
        <a href="{approve}">Approve</a> &nbsp;|&nbsp;
        <a href="{deny}">Deny</a>
      </p>
      <p style="color:#666;font-size:12px">
        Each link opens a confirmation page; nothing changes until you confirm.
        Links expire in {DECISION_LINK_TTL_HOURS // 24} days.
        You can also manage requests at {base}/admin
      </p>
    """
    try:
        r = requests.post(
            "https://api.resend.com/emails",
            headers={"Authorization": f"Bearer {RESEND_API_KEY}"},
            json={"from": MAIL_FROM, "to": [NOTIFY_EMAIL],
                  "subject": f"Catalog access request: {req['email']}", "html": html},
            timeout=10,
        )
        if r.status_code >= 400:
            print(f"[access] Resend error {r.status_code}: {r.text[:200]}")
            return False
        return True
    except requests.RequestException as e:
        print(f"[access] could not send notification: {e}")
        return False


def evaluate_access(user_id, email, display_name):
    """Decide whether this account may use the app.

    Returns (allowed, template_context). The admin is always allowed so the
    owner can never lock themselves out.
    """
    now = datetime.now(timezone.utc)
    now_iso = now.isoformat()

    if is_admin(email):
        db.upsert_access_request(user_id, email, display_name, now_iso, status="approved")
        db.set_access_decision(user_id, "approved", now_iso)
        return True, None

    req = db.get_access_request(user_id)

    if req and req["status"] == "approved":
        return True, None

    if req and req["status"] == "denied":
        until = req.get("denied_until")
        if until and now_iso < until:
            return False, {"state": "cooldown", "retry_after": until}
        # Cooldown elapsed — allow a fresh request.
        db.upsert_access_request(user_id, email, display_name, now_iso)
        db.set_access_decision(user_id, "pending", now_iso)
        req = db.get_access_request(user_id)
        notified = send_access_request_email(req)
        if notified:
            db.mark_notified(user_id, now_iso)
        return False, {"state": "pending", "notified": notified}

    if req and req["status"] == "pending":
        # Don't re-notify on every retry; that turns sign-in into a mail bomb.
        return False, {"state": "pending", "notified": bool(req.get("notified_at"))}

    db.upsert_access_request(user_id, email, display_name, now_iso)
    req = db.get_access_request(user_id)
    notified = send_access_request_email(req)
    if notified:
        db.mark_notified(user_id, now_iso)
    return False, {"state": "pending", "notified": notified}


def admin_required(view):
    @wraps(view)
    def wrapper(*args, **kwargs):
        if "user_id" not in session:
            if wants_json():
                return jsonify({"error": "not authenticated", "login": "/login"}), 401
            return redirect(url_for("login"))
        if not is_admin(session.get("user_email")):
            if wants_json():
                return jsonify({"error": "not authorized"}), 403
            return ("Not authorized", 403)
        return view(*args, **kwargs)
    return wrapper


@app.context_processor
def inject_identity():
    return {
        "current_user_email": session.get("user_email"),
        "viewer_user_id": session.get("user_id"),
        "viewer_is_admin": is_admin(session.get("user_email")),
        "demo_mode": DEMO_MODE,
        "legacy_user_id": db.LEGACY_USER_ID,
    }


# ── Request timing ────────────────────────────────────────────────────────────
#
# There was previously no instrumentation anywhere, so no claim about this
# app's speed was measurable. Slow requests are logged; search timing is
# surfaced in the UI so it can be observed rather than asserted.
SLOW_REQUEST_MS = 1000


@app.before_request
def _start_timer():
    request._started_at = time.perf_counter()


@app.after_request
def _log_slow_request(response):
    started = getattr(request, "_started_at", None)
    if started is None:
        return response
    elapsed_ms = (time.perf_counter() - started) * 1000
    response.headers["X-Response-Time-ms"] = f"{elapsed_ms:.0f}"
    if elapsed_ms > SLOW_REQUEST_MS and request.path != "/sync/progress":
        print(f"SLOW {request.method} {request.full_path.rstrip('?')} "
              f"{elapsed_ms:.0f}ms -> {response.status_code}")
    return response


@app.template_filter('matches_query')
def matches_query(tag, query_words):
    return any(word in tag for word in query_words)


class AuthExpired(RuntimeError):
    """Raised when the Microsoft refresh token can no longer be renewed."""


# ── Job state helpers ─────────────────────────────────────────────────────────

# A Detective session is held open by the browser. beforeunload is not a
# reliable signal, so the claim carries a timestamp and expires on its own
# rather than blocking syncs until the process restarts.
DETECTIVE_TTL_SECONDS = 300


def is_running(table, user_id, ttl=None):
    with _state_lock:
        stamp = table.get(user_id)
        if stamp is None:
            return False
        if ttl is not None and (time.time() - stamp) > ttl:
            table.pop(user_id, None)
            return False
        return True


def set_running(table, user_id, value):
    with _state_lock:
        if value:
            table[user_id] = time.time()
        else:
            table.pop(user_id, None)


def touch_running(table, user_id):
    """Refresh a claim so a long but live session doesn't time out."""
    with _state_lock:
        if user_id in table:
            table[user_id] = time.time()
            return True
        return False


# ── Auth helpers ──────────────────────────────────────────────────────────────

# Endpoints the browser calls with fetch(). These must answer 401/JSON on an
# expired session — a redirect hands an HTML login page to a JSON parser and
# the polling loop dies with a syntax error instead of bouncing to login.
JSON_PREFIXES = ("/sync/", "/detective/", "/wipe", "/resync", "/threads/",
                 "/admin/", "/import")


def wants_json():
    return (
        request.method == "POST"
        or request.is_json
        or request.path.startswith(JSON_PREFIXES)
        or request.accept_mimetypes.best == "application/json"
    )


def login_required(view):
    """Reject anything without an authenticated, catalog-scoped session."""
    @wraps(view)
    def wrapper(*args, **kwargs):
        if "user_id" not in session:
            if wants_json():
                return jsonify({"error": "not authenticated", "login": "/login"}), 401
            return redirect(url_for("login"))
        return view(*args, **kwargs)
    return wrapper


def current_user_id():
    return session["user_id"]


def current_provider():
    return providers.get(session.get("provider"))


def get_fresh_token():
    """Mint an access token for the signed-in account.

    Both the refresh cache and the resulting access token stay server-side.
    Nothing bearer-shaped goes in the cookie: it is signed, not encrypted, so
    its contents are readable by anyone who holds it.
    """
    user_id = session.get("user_id")
    if not user_id:
        return None

    token_cache = db.get_token_cache(user_id)
    access_token, new_cache = current_provider().refresh_token(token_cache)
    if new_cache and new_cache != token_cache:
        db.set_token_cache(user_id, new_cache)
    return access_token


# ── Sync helpers ──────────────────────────────────────────────────────────────

def strip_html(text):
    text = re.sub(r'<[^>]+>', ' ', text)
    text = re.sub(r'\s+', ' ', text).strip()
    return text


def _window_query(user_id):
    """Per-user Gmail window as a search query: None (unset, env default
    applies), "" (everything), or "after:YYYY/MM/DD"."""
    after = db.get_sync_after(user_id)
    if after is None or after == "":
        return after
    return "after:" + after.replace("-", "/")


def detect_changes(user_id, provider, get_token):
    """Walk every folder's delta feed and return the conversations that changed.

    Delta is used purely as a change-detector: it reports *which* messages
    changed (a tiny payload), and full thread context is refetched afterwards
    via get_thread_messages. That keeps build_threads working on complete
    conversations, which is what the tagger needs.

    Returns (conversation_ids, orphan_thread_ids, excluded, pending_cursors)
    where orphans are threads touched only by a removal and so need
    re-verification, and pending_cursors are the new delta links the caller
    must commit *after* the threads they describe have been stored.
    """
    token = get_token()
    excluded = provider.excluded_container_ids(token)
    folders = provider.list_change_sources(token, exclude_ids=excluded)
    print(f"Checking {len(folders)} folders for new mail "
          f"({len(excluded)} excluded: junk, deleted, drafts)")

    conversation_ids = set()
    removed_message_ids = []
    pending_cursors = {}
    scanned = [0]
    lock = threading.Lock()

    window = _window_query(user_id)
    # Only passed when set, so providers without a window are called as before.
    extra = {} if window is None else {"sync_query": window}

    def scan_folder(folder):
        if db.get_sync_flag(user_id) == "1":
            return

        stored_link = db.get_delta_link(user_id, folder["id"])
        try:
            changed, removed, new_link, full_resync = provider.changes_for_source(
                get_token(), folder["id"], stored_link, **extra
            )
        except Exception as e:
            # One bad folder shouldn't abort the whole sync; its token is left
            # untouched so the next run retries the same range.
            print(f"Delta failed for folder '{folder['name']}': {e}")
            return

        if full_resync and stored_link:
            print(f"Folder '{folder['name']}' required a full resync")

        with lock:
            conversation_ids.update(changed)
            removed_message_ids.extend(removed)

            # Held, not written. A cursor may only advance once the threads it
            # reported are actually in the database — see commit_cursors in
            # run_sync. Writing here meant a failure anywhere in the four
            # phases that follow left the cursor past mail nothing had stored,
            # and the next sync would never hear about it again.
            if new_link:
                pending_cursors[folder["id"]] = new_link

            scanned[0] += 1
            db.set_sync_progress(user_id, scanned[0], len(folders), "checking folders")

    run_parallel(scan_folder, folders, max_workers=4)

    # A removal carries only a message id. Trace it back to its thread so the
    # conversation gets re-verified — this also covers moves between folders,
    # which Graph reports as a removal plus a creation.
    orphan_thread_ids = db.find_thread_ids_by_message_ids(user_id, removed_message_ids)
    if removed_message_ids:
        print(f"{len(removed_message_ids)} messages deleted or moved, "
              f"affecting {len(orphan_thread_ids)} threads")

    return conversation_ids, orphan_thread_ids, excluded, pending_cursors


def rebuild_threads(user_id, provider, conversation_ids, get_token, excluded_folder_ids=frozenset(),
                    phase="updating threads"):
    """Refetch full conversations and return them as thread dicts.

    get_thread_messages spans every folder, so messages sitting in Junk or
    Deleted Items are filtered out here — otherwise excluded mail would
    reappear through thread reconstruction. A conversation left with no
    messages no longer exists anywhere indexed, so its row is deleted.
    """
    threads = []
    deleted = 0
    lock = threading.Lock()
    total = len(conversation_ids)
    done = [0]

    def rebuild(cid):
        nonlocal deleted
        msgs = [
            m for m in provider.get_thread(get_token(), cid)
            if m["container_id"] not in excluded_folder_ids
        ]
        with lock:
            if msgs:
                threads.extend(build_threads(msgs))
            else:
                db.delete_thread(user_id, cid)
                deleted += 1
            done[0] += 1
            db.set_sync_progress(user_id, done[0], total, phase)

    errors = run_parallel(rebuild, list(conversation_ids), max_workers=5)
    if deleted:
        print(f"Dropped {deleted} threads no longer in the mailbox")
    return threads, errors


def build_threads(all_messages):
    """Group normalised messages by thread_id into thread dicts."""
    grouped = {}
    for m in all_messages:
        tid = m["thread_id"]
        bucket = grouped.setdefault(tid, [])
        if not any(existing["id"] == m["id"] for existing in bucket):
            bucket.append(m)

    all_threads = []
    for tid, msgs in grouped.items():
        try:
            msgs.sort(key=lambda x: x.get("date") or "")
            first, last = msgs[0], msgs[-1]

            participants = sorted({
                addr
                for m in msgs
                for addr in ([m.get("from_addr")] + list(m.get("to_addrs") or []))
                if addr
            })

            all_threads.append({
                "thread_id": tid,
                "message_ids": [
                    {
                        "id": m["id"],
                        "web_link": m.get("web_link", ""),
                        "date": (m.get("date") or "")[:10],
                        "has_attachments": m.get("has_attachments", False),
                    }
                    for m in msgs
                ],
                "subject": first.get("subject") or "",
                "participants": participants,
                "participants_str": ", ".join(participants),
                "date_first": first.get("date") or "",
                "date_last": last.get("date") or "",
                "has_attachments": 1 if any(m.get("has_attachments") for m in msgs) else 0,
                "attachments": [],
                "web_link": first.get("web_link", ""),
                "raw_msgs": msgs,
                "last_synced": datetime.now(timezone.utc).isoformat(),
            })
        except Exception as e:
            print(f"Error building thread {tid}: {e}")
            continue

    return all_threads


def filter_new_threads(user_id, all_threads, force_ids=frozenset()):
    """Return threads that are new, newer, or explicitly forced.

    The date_last comparison alone is not sufficient: deleting a message from
    a thread leaves date_last equal or *older*, so a purely date-based filter
    would skip the update and leave the stored row listing a message that no
    longer exists (with a dead Outlook link). Threads traced from a delta
    removal are therefore forced through regardless of date.
    """
    existing = db.get_existing_thread_ids(user_id)
    return [
        t for t in all_threads
        if t["thread_id"] not in existing
        or t["thread_id"] in force_ids
        or t["date_last"] > existing[t["thread_id"]]
    ]


def process_attachments(message_id, attachments_metadata, all_attachment_content):
    """Build attachment records, extracting text from prefetched content."""
    processed = []
    for a in attachments_metadata:
        name = a.get("name") or ""
        base = {
            "name": name,
            "content_type": a.get("content_type", ""),
            "size": a.get("size", 0),
            "actual_char_count": 0,
            "scan_status": "ok",
            "text": ""
        }

        if not name.lower().endswith((".pdf", ".docx")):
            base["scan_status"] = "unsupported"
            processed.append(base)
            continue

        full = all_attachment_content.get((message_id, a["id"]))
        if not full:
            base["scan_status"] = "failed"
            processed.append(base)
            continue

        text = extractor.extract_text(name, base["content_type"], full)
        if not text:
            base["scan_status"] = "failed"
            processed.append(base)
            continue

        char_count = len(text)
        base["actual_char_count"] = char_count
        if char_count > tagger.ATTACHMENT_CAP:
            base["text"] = text[:tagger.ATTACHMENT_CAP]
            base["scan_status"] = "truncated"
        else:
            base["text"] = text
            base["scan_status"] = "ok"

        processed.append(base)
    return processed


def fetch_thread_content(user_id, t, total, fetch_processed, fetch_lock,
                         all_attachment_metadata, all_attachment_content):
    """Extract body text and attachments for a single thread using prefetched data."""
    all_body_parts = []
    all_body_chars = 0
    body_scan_status = "ok"
    all_attachments = []

    for msg_obj in t["message_ids"]:
        mid = msg_obj["id"] if isinstance(msg_obj, dict) else msg_obj

        msg_body = next((m for m in t["raw_msgs"] if m["id"] == mid), {})
        raw_body = strip_html(msg_body.get("body", ""))
        char_count = len(raw_body)
        if char_count > tagger.BODY_CAP:
            body_text, status = raw_body[:tagger.BODY_CAP], "truncated"
        else:
            body_text, status = raw_body, "ok"

        all_body_parts.append(body_text)
        all_body_chars += char_count
        if status != "ok":
            body_scan_status = status

        if msg_obj.get("has_attachments"):
            attachments_metadata = all_attachment_metadata.get(mid, [])
            if attachments_metadata:
                processed = process_attachments(
                    mid, attachments_metadata, all_attachment_content
                )
                all_attachments.extend(processed)

    combined_body = " ".join([b for b in all_body_parts if b])
    if len(combined_body) > tagger.BODY_CAP:
        combined_body = combined_body[:tagger.BODY_CAP]
        body_scan_status = "truncated"

    t["body_text"] = combined_body
    t["body_char_count"] = all_body_chars
    t["body_scan_status"] = body_scan_status
    t["attachments"] = all_attachments
    t["attachment_names"] = ", ".join([a["name"] for a in all_attachments]) if all_attachments else "none"

    with fetch_lock:
        fetch_processed[0] += 1
        db.set_sync_progress(user_id, fetch_processed[0], total, "processing")


def usage_recorder(user_id):
    """Callback that files token counts against a user.

    The day is read when usage is recorded, not when the recorder is built.
    A batch sync can run for hours, so capturing it once filed everything
    after midnight under the previous day — and since the spend limit counts
    from the first of the month, a run crossing month end charged the old
    month and left the new one looking untouched.
    """
    def record(input_tokens, output_tokens, cache_read, batched):
        day = datetime.now(timezone.utc).date().isoformat()
        db.record_usage(user_id, input_tokens, output_tokens, cache_read, batched, day)

    return record


def tag_inputs_for(threads):
    """Shape threads into the tagger's input format."""
    return [{
        "subject": t["subject"],
        "participants": t["participants_str"],
        "date": t["date_first"],
        "body_text": t.get("body_text", ""),
        "body_scan_status": t.get("body_scan_status", "ok"),
        "attachments": [
            {
                "name": a["name"],
                "text": a.get("text", ""),
                "scan_status": a.get("scan_status", "ok"),
            }
            for a in t.get("attachments", [])
        ],
    } for t in threads]


def clean_attachments(thread):
    return [
        {
            "name": a["name"],
            "content_type": a.get("content_type", ""),
            "size": a.get("size", 0),
            "actual_char_count": a.get("actual_char_count", 0),
            "scan_status": a.get("scan_status", "ok"),
        }
        for a in thread.get("attachments", [])
    ]


def store_untagged(user_id, threads):
    """Persist threads before tagging so the catalog is browsable immediately.

    Writes a storage-only copy: the live thread dicts keep their extracted
    attachment text, which tagging still needs. Mutating them here silently
    stripped that text and left attachments contributing nothing to tags.
    """
    for t in threads:
        stored = dict(t)
        stored["ai_tags"] = t.get("ai_tags", [])
        stored["tags_truncated"] = t.get("tags_truncated", 0)
        stored["attachments"] = clean_attachments(t)
        db.upsert_thread(user_id, stored)


def resume_pending_batches(user_id):
    """Collect results for batches submitted by an earlier run.

    Returns the number of threads tagged. Results stay retrievable for 29
    days, so a deploy or crash mid-wait costs nothing but time.
    """
    tagged = 0
    for batch_id, mapping, submitted in db.get_pending_batches(user_id):
        status = tagger.batch_status(batch_id)

        if status == "in_progress":
            print(f"Batch {batch_id} still running; leaving it pending")
            continue

        if status == "unknown":
            # Transient: a 401, a rate limit, a network blip. The batch may be
            # finished and already paid for, so keep the record and try again
            # next time rather than discarding the work.
            if _batch_record_expired(submitted):
                print(f"Batch {batch_id} unreadable and older than "
                      f"{BATCH_RECORD_TTL_DAYS} days; dropping it")
                db.clear_pending_batch(user_id, batch_id)
            else:
                print(f"Batch {batch_id} unreadable right now; leaving it pending")
            continue

        tagged += len(apply_batch_results(user_id, batch_id, mapping))
    return tagged


def _batch_record_expired(submitted_at):
    if not submitted_at:
        return False
    try:
        when = datetime.fromisoformat(submitted_at)
    except (TypeError, ValueError):
        return False
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - when).days > BATCH_RECORD_TTL_DAYS


def apply_batch_results(user_id, batch_id, mapping):
    """Write tags from an ended batch and clear it.

    Returns the set of thread ids actually tagged — not a count. Groups can
    fail individually, and the caller needs to know *which* threads still
    need work rather than assuming a non-zero count means all of them landed.
    """
    counts = {cid: len(ids) for cid, ids in mapping.items()}
    results = tagger.collect_tag_batch(batch_id, counts, on_usage=usage_recorder(user_id))

    tagged = set()
    failed_groups = 0
    for cid, thread_ids in mapping.items():
        got = results.get(cid)
        if not got:
            failed_groups += 1
            continue
        tags_list, truncated_flags = got
        for thread_id, tags, truncated in zip(thread_ids, tags_list, truncated_flags):
            db.set_thread_tags(user_id, thread_id, tags, truncated)
            tagged.add(thread_id)

    db.clear_pending_batch(user_id, batch_id)
    note = f", {failed_groups} groups failed" if failed_groups else ""
    print(f"Batch {batch_id}: tagged {len(tagged)} threads{note}")
    return tagged


def tag_via_batch_api(user_id, threads, total):
    """Submit threads to the Batch API and wait, bounded by BATCH_MAX_WAIT_SECONDS.

    Returns the threads that still need tagging — empty if the batch covered
    everything, or the untagged remainder if it was slow, failed, or errored.
    """
    groups = [threads[i:i + BATCH_SIZE] for i in range(0, len(threads), BATCH_SIZE)]
    mapping = {f"g{i}": [t["thread_id"] for t in g] for i, g in enumerate(groups)}

    batch_id = tagger.submit_tag_batch([tag_inputs_for(g) for g in groups])
    if not batch_id:
        return threads  # submission failed; caller falls back to real time

    db.add_pending_batch(user_id, batch_id, mapping,
                         datetime.now(timezone.utc).isoformat())
    print(f"Submitted batch {batch_id}: {len(groups)} groups, {len(threads)} threads")

    deadline = time.time() + BATCH_MAX_WAIT_SECONDS
    unknown_streak = 0
    while time.time() < deadline:
        if db.get_sync_flag(user_id) == "1":
            return []  # stopped by the user; batch stays pending for next run
        status = tagger.batch_status(batch_id)
        if status == "ended":
            done_ids = apply_batch_results(user_id, batch_id, mapping)
            return [t for t in threads if t["thread_id"] not in done_ids]
        if status == "unknown":
            unknown_streak += 1
            if unknown_streak >= BATCH_UNKNOWN_TOLERANCE:
                # Give up waiting, but leave the record: a later sync can still
                # collect it if the batch was in fact fine.
                print(f"Batch {batch_id} unreadable {unknown_streak}x; "
                      "leaving it pending and tagging in real time")
                return threads
        else:
            unknown_streak = 0
        db.set_sync_progress(user_id, 0, total, "tagging (batch)")
        time.sleep(BATCH_POLL_SECONDS)

    # Took too long. Cancel so we aren't billed for both paths, then salvage
    # whatever it already finished and real-time only the remainder.
    print(f"Batch {batch_id} exceeded {BATCH_MAX_WAIT_SECONDS}s; cancelling and falling back")
    tagger.cancel_batch(batch_id)

    counts = {cid: len(ids) for cid, ids in mapping.items()}
    salvaged = tagger.collect_tag_batch(batch_id, counts, on_usage=usage_recorder(user_id))
    done_ids = set()
    for cid, got in salvaged.items():
        tags_list, truncated_flags = got
        for thread_id, tags, truncated in zip(mapping[cid], tags_list, truncated_flags):
            db.set_thread_tags(user_id, thread_id, tags, truncated)
            done_ids.add(thread_id)
    db.clear_pending_batch(user_id, batch_id)
    if done_ids:
        print(f"Salvaged {len(done_ids)} threads from the cancelled batch")

    return [t for t in threads if t["thread_id"] not in done_ids]


def process_batch(user_id, batch, total, processed, lock):
    """Tag a batch of threads and upsert to DB."""
    if db.get_sync_flag(user_id) == "1":
        return

    tags_list, truncated_flags = tagger.generate_tags_batch(
        tag_inputs_for(batch), on_usage=usage_recorder(user_id))

    for thread, tags, truncated in zip(batch, tags_list, truncated_flags):
        thread["ai_tags"] = tags
        thread["tags_truncated"] = 1 if truncated else 0
        thread["attachments"] = clean_attachments(thread)
        db.upsert_thread(user_id, thread)

    with lock:
        processed[0] += len(batch)
        db.set_sync_progress(user_id, processed[0], total, "tagging")


def run_parallel(fn, items, max_workers):
    """Run fn over items, surfacing worker exceptions instead of swallowing them.

    ThreadPoolExecutor.map defers exceptions into the result iterator; if that
    iterator is never consumed, a failing worker looks like a silent success.
    """
    errors = []
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = [executor.submit(fn, item) for item in items]
        for future in futures:
            try:
                future.result()
            except Exception as e:
                import traceback
                traceback.print_exc()
                errors.append(e)
    return errors


# ── Main sync orchestrator ────────────────────────────────────────────────────

def run_sync(user_id, provider, token, token_cache=None):
    def get_token():
        nonlocal token, token_cache
        fresh_token, new_cache = provider.refresh_token(token_cache)
        if fresh_token:
            token = fresh_token
            token_cache = new_cache
        elif token_cache:
            # Refresh failed and the cached token is the only thing left.
            # Say so loudly rather than letting Graph return opaque 401s.
            raise AuthExpired(f"{provider.label} sign-in expired — please sign in again.")
        return token

    try:
        db.set_sync_flag(user_id, "0")
        db.set_sync_progress(user_id, 0, 0, "checking folders")

        # A batch submitted by an earlier run may have finished while the
        # process was down. Collect it before doing anything else.
        resume_pending_batches(user_id)

        # Phase 1: Delta scan — which conversations changed since last sync?
        conversation_ids, orphan_thread_ids, excluded_folders, cursors = detect_changes(
            user_id, provider, get_token)
        conversation_ids |= orphan_thread_ids

        def commit_cursors():
            """Advance the folder delta cursors. Only safe once every thread
            the scan reported is in the database.

            A cursor is a promise that everything before it has been handled.
            Advancing it early is unrecoverable in the one way that matters:
            the next scan simply never mentions that mail again, so the loss is
            silent and permanent until someone runs /resync. Re-reading a
            folder, by contrast, costs one delta call and nothing else —
            filter_new_threads drops anything already current, so nothing is
            re-tagged. When in doubt, do not advance.
            """
            for folder_id, link in cursors.items():
                db.set_delta_link(user_id, folder_id, link)
            cursors.clear()

        if db.get_sync_flag(user_id) == "1":
            db.set_sync_progress(user_id, 0, 0, "stopped")
            return

        if not conversation_ids:
            # Nothing changed, so the database already reflects the mailbox.
            commit_cursors()
            db.set_sync_progress(user_id, 0, 0, "done")
            return

        # Phase 2: Rebuild those conversations in full, then drop any whose
        # content is unchanged (delta also reports read/unread flips, which
        # must not trigger re-tagging).
        # First run has nothing to compare against, so call it indexing.
        first_sync = db.count_threads(user_id) == 0
        phase = "indexing threads" if first_sync else "updating threads"
        rebuilt, rebuild_errors = rebuild_threads(
            user_id, provider, conversation_ids, get_token, excluded_folders, phase=phase)
        all_threads = filter_new_threads(user_id, rebuilt, force_ids=orphan_thread_ids)

        # A conversation that failed to rebuild was never stored, and cursors
        # are per-folder while errors are per-conversation — there is no way to
        # hold back only the affected folder. So hold them all and re-read next
        # run. A permanently failing thread means repeated rescans, which is
        # visible in the log and cheap; the alternative is losing it silently.
        if rebuild_errors:
            print(f"{len(rebuild_errors)} threads failed to rebuild; holding "
                  "delta cursors so the next sync re-reads these folders")

        if not all_threads:
            # Everything the scan reported is already current or was deleted.
            if not rebuild_errors:
                commit_cursors()
            db.set_sync_progress(user_id, 0, 0, "done")
            return

        total = len(all_threads)
        if first_sync:
            print(f"Indexing {total} threads")
        else:
            print(f"{len(conversation_ids)} threads touched; {total} need re-tagging")

        # Phase 3: Batch fetch all attachment metadata + content upfront
        db.set_sync_progress(user_id, 0, total, "reading attachments")

        attachment_message_ids = [
            msg_obj["id"]
            for t in all_threads
            for msg_obj in t["message_ids"]
            if msg_obj.get("has_attachments")
        ]

        all_attachment_metadata = provider.get_attachment_metadata(
            get_token(), attachment_message_ids
        )

        db.set_sync_progress(user_id, 0, total, "downloading attachments")

        content_pairs = [
            (mid, a["id"])
            for mid, attachments in all_attachment_metadata.items()
            for a in attachments
            if (a.get("name") or "").lower().endswith((".pdf", ".docx"))
        ]

        all_attachment_content = provider.get_attachment_content(
            get_token(), content_pairs
        ) if content_pairs else {}

        db.set_sync_progress(user_id, 0, total, "processing")

        # Phase 4: Process thread content in parallel (no API calls)
        fetch_processed = [0]
        fetch_lock = threading.Lock()

        fetch_errors = run_parallel(
            lambda t: fetch_thread_content(
                user_id, t, total, fetch_processed, fetch_lock,
                all_attachment_metadata, all_attachment_content
            ),
            all_threads,
            max_workers=5
        )

        # Phase 5: Tag. Large backfills go through the Batch API at half
        # price; small incremental syncs stay real-time because the saving
        # is pennies and the wait is not.
        store_untagged(user_id, all_threads)

        # Every reported thread is now on disk. Tags are not, but a missing tag
        # is recoverable from the row itself; a thread the next scan never
        # mentions is not. This is the point where the cursor may move.
        if not rebuild_errors:
            commit_cursors()

        to_tag = all_threads
        if len(all_threads) >= BATCH_API_THRESHOLD:
            db.set_sync_progress(user_id, 0, total, "tagging (batch)")
            to_tag = tag_via_batch_api(user_id, all_threads, total)
            if to_tag:
                print(f"{len(to_tag)} threads still need real-time tagging")

        processed = [0]
        lock = threading.Lock()
        tag_errors = []
        if to_tag:
            batches = [to_tag[i:i + BATCH_SIZE] for i in range(0, len(to_tag), BATCH_SIZE)]
            tag_errors = run_parallel(
                lambda batch: process_batch(user_id, batch, total, processed, lock),
                batches,
                max_workers=8
            )
        else:
            processed[0] = total

        if db.get_sync_flag(user_id) == "1":
            status = "stopped"
        elif rebuild_errors or fetch_errors or tag_errors:
            status = "error"
            print(f"Sync finished with {len(rebuild_errors)} rebuild, "
                  f"{len(fetch_errors)} content and {len(tag_errors)} tagging failures")
        else:
            status = "done"
        db.set_sync_progress(user_id, processed[0], total, status)

    except AuthExpired as e:
        print(f"Sync aborted: {e}")
        db.set_sync_progress(user_id, 0, 0, "auth_expired")
    except Exception as e:
        import traceback
        print(f"Sync error: {e}")
        traceback.print_exc()
        db.set_sync_progress(user_id, 0, 0, "error")
    finally:
        set_running(sync_running, user_id, False)


def run_retag_empty(user_id, provider, token, token_cache=None):
    def get_token():
        nonlocal token, token_cache
        fresh_token, new_cache = provider.refresh_token(token_cache)
        if fresh_token:
            token = fresh_token
            token_cache = new_cache
        return token

    try:
        threads = db.get_untagged_threads(user_id)
        total = len(threads)
        db.set_sync_progress(user_id, 0, total, "retagging")

        for i, t in enumerate(threads):
            msgs = provider.get_thread(get_token(), t["thread_id"])
            if not msgs:
                continue

            all_body_parts = []
            for m in msgs:
                all_body_parts.append(strip_html(m.get("body", ""))[:tagger.BODY_CAP])

            combined_body = " ".join([b for b in all_body_parts if b])[:tagger.BODY_CAP]

            tag_inputs = [{
                "subject": t["subject"],
                "participants": ", ".join(json.loads(t.get("participants") or "[]")),
                "date": t["date_first"],
                "body_text": combined_body,
                "body_scan_status": "ok",
                "attachments": json.loads(t.get("attachments") or "[]")
            }]

            tags_list, truncated_flags = tagger.generate_tags_batch(
                tag_inputs, on_usage=usage_recorder(user_id))
            db.set_thread_tags(user_id, t["thread_id"], tags_list[0], truncated_flags[0])

            db.set_sync_progress(user_id, i + 1, total, "retagging")

        db.set_sync_progress(user_id, total, total, "done")

    except AuthExpired as e:
        print(f"Retag aborted: {e}")
        db.set_sync_progress(user_id, 0, 0, "auth_expired")
        return
    except Exception as e:
        import traceback
        print(f"Retag error: {e}")
        traceback.print_exc()
        db.set_sync_progress(user_id, 0, 0, "error")
    finally:
        set_running(sync_running, user_id, False)


# ── Flask routes ──────────────────────────────────────────────────────────────

@app.route("/")
@login_required
def index():
    user_id = current_user_id()

    query = request.args.get("q", "").strip()
    date_from = request.args.get("date_from", "")
    date_to = request.args.get("date_to", "")
    has_attachments = request.args.get("has_attachments")
    sort_by = request.args.get("sort_by", "last_synced")
    search_mode = request.args.get("search_mode", "and")
    has_multiple = request.args.get("has_multiple")
    min_attachment_size = request.args.get("min_attachment_size", "")

    if date_from and date_to and date_from > date_to:
        date_from, date_to = date_to, date_from

    search_ms = None
    if request.args:
        _t0 = time.perf_counter()
        threads, total_matches = db.search_threads(
            user_id,
            limit=db.DEFAULT_SEARCH_LIMIT,
            with_total=True,
            query=query or None,
            has_attachments=True if has_attachments else None,
            has_multiple=has_multiple,
            date_from=date_from or None,
            date_to=date_to or None,
            sort_by=sort_by,
            search_mode=search_mode,
            min_attachment_size=min_attachment_size
        )
        search_ms = (time.perf_counter() - _t0) * 1000
        for t in threads:
            t["ai_tags_list"] = sorted(json.loads(t["ai_tags"] or "[]"))
            t["user_tags_list"] = sorted(json.loads(t["user_tags"] or "[]"))
            t["participants_str"] = ", ".join(json.loads(t["participants"] or "[]"))
            t["outlook_url"] = t.get("web_link") or "https://outlook.live.com/mail/0/inbox"
            t["message_ids_parsed"] = json.loads(t["message_ids"] or "[]")
            attachments = json.loads(t.get("attachments") or "[]")
            t["attachments_incomplete"] = any(
                a.get("scan_status") in ("failed", "truncated")
                for a in attachments
            )
            t["tags_truncated"] = t.get("tags_truncated", 0)
    else:
        threads = None
        total_matches = None

    query_words = [w.lower() for w in query.split()] if query else []

    return render_template("index.html",
        threads=threads,
        search_ms=search_ms,
        total_matches=total_matches,
        result_limit=db.DEFAULT_SEARCH_LIMIT,
        query=query,
        query_words=query_words,
        date_from=date_from,
        date_to=date_to,
        has_attachments=has_attachments,
        sort_by=sort_by,
        search_mode=search_mode,
        has_multiple=has_multiple,
        min_attachment_size=min_attachment_size
    )


@app.route("/login")
def login():
    if DEMO_MODE:
        # No OAuth: the demo account exists only in the demo database, and
        # the token it stores is a placeholder no provider path ever uses —
        # every route that would spend it is disabled below.
        session.clear()
        session["user_id"] = demo_seed.DEMO_USER_ID
        session["user_email"] = demo_seed.DEMO_EMAIL
        session["provider"] = "demo"
        return redirect(url_for("index"))
    requested = request.args.get("provider")
    if not requested:
        # No provider chosen yet: show the chooser instead of assuming one.
        return render_template("login.html", providers=providers.PROVIDERS.values())
    provider = providers.get(requested)
    # /callback has no way to know which provider issued the code, so record it.
    session["pending_provider"] = provider.name

    # CSRF defence for the login itself. Without it, an attacker can send a
    # victim to /callback carrying the attacker's authorisation code, and the
    # victim's browser silently ends up holding a session bound to the
    # attacker's mailbox — every thread they then index, and every tag the
    # API key pays for, lands in someone else's catalog. The state is random,
    # kept in the signed session cookie, and echoed back by the provider.
    state = secrets.token_urlsafe(32)
    session["oauth_state"] = state
    return redirect(provider.auth_url(state=state))


@app.route("/callback")
def callback():
    code = request.args.get("code")
    if not code:
        return "Auth failed — no code returned.", 400

    # Single-use: pop before comparing, so a replayed callback finds nothing
    # to match against even if the value leaked. compare_digest because this
    # is a secret being checked against attacker-supplied input.
    expected = session.pop("oauth_state", None)
    returned = request.args.get("state", "")
    if not expected or not hmac.compare_digest(expected, returned):
        # No session established, so a forged callback leaves nothing behind.
        session.pop("pending_provider", None)
        return ("Auth failed — this sign-in did not start here. "
                "Please sign in again from the app."), 400

    try:
        provider = providers.get(session.get("pending_provider"))
        access_token, token_cache = provider.token_from_code(code)

        # Resolve the signed-in identity; the catalog is scoped to this user_id.
        me = provider.get_identity(access_token)
        user_id = me.get("id")
        if not user_id:
            return "Auth error: could not resolve account identity.", 500

        email = me.get("email") or ""
        display_name = me.get("display_name") or ""

        # Gate before establishing a catalog session. An unapproved account
        # gets no session and therefore no ability to spend the API key.
        allowed, ctx = evaluate_access(user_id, email, display_name)
        if not allowed:
            session.clear()
            return render_template("pending.html", email=email, **ctx), 403

        db.upsert_user(user_id, email, display_name,
                       datetime.now(timezone.utc).isoformat())

        db.set_token_cache(user_id, token_cache)

        session.clear()
        # Identity only. The credentials are in the database.
        session["user_id"] = user_id
        session["user_email"] = email
        session["provider"] = provider.name
        return redirect(url_for("index"))
    except Exception as e:
        print("AUTH ERROR:", e)
        return f"Auth error: {e}", 500


@app.route("/access/decide/<token>")
def access_decide_confirm(token):
    """Render a confirmation page for an emailed decision link.

    Deliberately does not apply the decision: mail scanners prefetch links,
    and a mutating GET would auto-approve every request that hits an inbox.
    """
    parsed = verify_decision(token)
    if not parsed:
        return render_template("decision.html", state="invalid"), 400

    user_id, decision = parsed
    req = db.get_access_request(user_id)
    if not req:
        return render_template("decision.html", state="missing"), 404

    return render_template("decision.html", state="confirm", decision=decision,
                           req=req, token=token)


@app.route("/access/decide/<token>", methods=["POST"])
def access_decide_apply(token):
    parsed = verify_decision(token)
    if not parsed:
        return render_template("decision.html", state="invalid"), 400

    user_id, decision = parsed
    now = datetime.now(timezone.utc)
    denied_until = None
    if decision == "deny":
        denied_until = (now + timedelta(minutes=DENY_COOLDOWN_MINUTES)).isoformat()

    ok = db.set_access_decision(
        user_id,
        "approved" if decision == "approve" else "denied",
        now.isoformat(),
        denied_until,
    )
    if not ok:
        return render_template("decision.html", state="missing"), 404

    return render_template("decision.html", state="done", decision=decision,
                           req=db.get_access_request(user_id))


# Haiku 4.5 list prices; batch work is billed at half.
PRICE_IN_PER_MTOK = 1.0
PRICE_OUT_PER_MTOK = 5.0

# Per-account spend ceiling for the current calendar month, in dollars.
# Unset means no limit, which is the right default for a single-operator
# instance — the person paying is the person spending. Set it before letting
# anyone else in: an approved account can otherwise cost the operator ~$6.65
# by syncing a large mailbox, repeatedly, with nothing but the shared
# workspace cap in the way.
_limit = (os.getenv("USER_SPEND_LIMIT_USD") or "").strip()
try:
    USER_SPEND_LIMIT_USD = float(_limit) if _limit else None
except ValueError:
    print(f"USER_SPEND_LIMIT_USD is not a number ({_limit!r}); treating as unset.")
    USER_SPEND_LIMIT_USD = None


def estimate_cost(stats):
    """Rough spend for a user's tagging. Batch requests bill at 50%."""
    batch_share = (stats["batched_requests"] / stats["requests"]) if stats["requests"] else 0
    discount = 1 - (0.5 * batch_share)
    return (
        stats["input_tokens"] / 1e6 * PRICE_IN_PER_MTOK
        + stats["output_tokens"] / 1e6 * PRICE_OUT_PER_MTOK
    ) * discount


def month_start():
    return datetime.now(timezone.utc).strftime("%Y-%m-01")


def spend_this_month(user_id):
    return estimate_cost(db.usage_since(user_id, month_start()))


def over_spend_limit(user_id):
    """(exceeded, spent, limit). Admins are exempt: the limit exists to stop
    an approved guest spending the operator's money, and the operator
    stopping their own work mid-sync helps nobody."""
    limit = USER_SPEND_LIMIT_USD
    if limit is None or is_admin(session.get("user_email")):
        return False, 0.0, limit
    spent = spend_this_month(user_id)
    return spent >= limit, spent, limit


@app.route("/admin")
@admin_required
def admin():
    usage = db.usage_by_user()
    for user_id, stats in usage.items():
        stats["cost"] = estimate_cost(stats)

    # Deliberately independent of usage. Needing to be tagged has nothing to
    # do with having recorded token spend, and tying them together hid the
    # retag control on exactly the accounts that needed it.
    return render_template("admin.html",
                           requests=db.list_access_requests(),
                           usage=usage,
                           untagged=db.untagged_by_user(),
                           backups=backup_status(),
                           cooldown=DENY_COOLDOWN_MINUTES)


@app.route("/admin/decide", methods=["POST"])
@admin_required
def admin_decide():
    data = request.get_json(silent=True) or {}
    user_id = data.get("user_id")
    decision = data.get("decision")
    if not user_id or decision not in ("approve", "deny", "revoke"):
        return jsonify({"error": "bad request"}), 400

    now = datetime.now(timezone.utc)
    status = {"approve": "approved", "deny": "denied", "revoke": "denied"}[decision]
    denied_until = None
    if decision == "deny":
        denied_until = (now + timedelta(minutes=DENY_COOLDOWN_MINUTES)).isoformat()

    if not db.set_access_decision(user_id, status, now.isoformat(), denied_until):
        return jsonify({"error": "not found"}), 404
    return jsonify({"ok": True, "status": status})


@app.route("/healthz")
def healthz():
    """Unauthenticated liveness and version check.

    Answering "is my change actually deployed?" needed a behaviour visible
    without signing in, which meant guessing from whatever the last commit
    happened to alter. Render exposes the deployed commit; this reports it.
    Nothing here is secret — the repository is public and the SHA identifies
    a commit anyone can already read.
    """
    return jsonify({
        "ok": True,
        "commit": (os.getenv("RENDER_GIT_COMMIT") or "unknown")[:7],
        "demo": DEMO_MODE,
    })


@app.route("/logout")
def logout():
    # Drop stored credentials too, so a signed-out account leaves no refresh
    # token behind. A sync already running keeps its own in-memory copy.
    user_id = session.get("user_id")
    if user_id and not DEMO_MODE:
        db.clear_token_cache(user_id)
    session.clear()
    return redirect(url_for("login"))


@app.route("/sync")
@login_required
def sync():
    if DEMO_MODE:
        return redirect(url_for("index"))  # nothing to sync a fabricated mailbox against
    user_id = current_user_id()
    if is_running(detective_running, user_id, ttl=DETECTIVE_TTL_SECONDS):
        return redirect(url_for("index"))

    # A sync whose heartbeat has gone quiet is dead, whatever the in-memory
    # flag says. Refusing to start in that state is what left the UI with no
    # way out except an undocumented URL.
    if is_running(sync_running, user_id):
        if not db.get_sync_progress(user_id).get("stale"):
            return redirect(url_for("index"))
        print(f"Previous sync for {user_id} looks dead; starting a new one")
        set_running(sync_running, user_id, False)

    exceeded, spent, limit = over_spend_limit(user_id)
    if exceeded:
        print(f"[spend] refusing sync for {user_id}: ${spent:.2f} of ${limit:.2f}")
        return redirect(url_for("index", spend_limited="1"))

    set_running(sync_running, user_id, True)
    t = threading.Thread(
        target=run_sync,
        args=(user_id, current_provider(), get_fresh_token(),
              db.get_token_cache(user_id)),
        daemon=True
    )
    t.start()
    return redirect(url_for("index"))


def _parse_range_date(value):
    """Validate a YYYY-MM-DD window start: "" (everything) or a real,
    non-future date. Returns the normalised string, or None if invalid."""
    if not isinstance(value, str):
        return None
    value = value.strip()
    if value == "":
        return ""
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        return None
    try:
        d = datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        return None
    if d > datetime.now(timezone.utc).date():
        return None
    return value


def _effective_window_date(user_id):
    """The date the user's window currently starts at, or None for everything.
    Stored value wins; unset falls back to the env default's after: date."""
    stored = db.get_sync_after(user_id)
    if stored is not None:
        return stored or None
    m = re.search(r"after:(\d{4})[/-](\d{2})[/-](\d{2})",
                  os.getenv("GMAIL_SYNC_QUERY", ""))
    if not m:
        return None
    try:
        return datetime.strptime("-".join(m.groups()), "%Y-%m-%d").strftime("%Y-%m-%d")
    except ValueError:
        return None


@app.route("/sync/range", methods=["GET", "POST"])
@login_required
def sync_range():
    user_id = current_user_id()
    provider = current_provider()
    gmail_user = provider.name == "gmail"
    if request.method == "GET":
        out = {"provider": provider.name, "sync_after": db.get_sync_after(user_id),
               "editable": gmail_user}
        if not gmail_user:
            out["reason"] = "sync range is gmail-only"
        return jsonify(out)

    if not gmail_user:
        return jsonify({"error": "sync range is gmail-only"}), 400
    body = request.get_json(silent=True) or {}
    new = _parse_range_date(body.get("after"))
    if new is None:
        return jsonify({"error": "after must be YYYY-MM-DD, not in the future, or empty"}), 400

    stored = db.get_sync_after(user_id)
    current = _effective_window_date(user_id)
    if stored is None and current is None:
        # Prior window unknown (not "everything"): any explicit choice may
        # reach mail the existing cursor skipped, so re-walk if one exists.
        widening = db.has_delta_links(user_id)
    elif stored is not None and new == stored:
        widening = False
    else:
        widening = (current is not None) and (new == "" or new < current)
    if widening and (is_running(sync_running, user_id)
                     or is_running(detective_running, user_id, ttl=DETECTIVE_TTL_SECONDS)):
        return jsonify({"error": "a sync is running; try again when it finishes"}), 409

    db.set_sync_after(user_id, new)
    if widening:
        db.clear_delta_links(user_id)
    return jsonify({"stored": True, "full_rewalk_next_scan": widening})


@app.route("/sync/range/preview")
@login_required
def sync_range_preview():
    if current_provider().name != "gmail":
        return jsonify({"error": "sync range is gmail-only"}), 400
    after = _parse_range_date(request.args.get("after"))
    if after is None:
        return jsonify({"error": "after must be YYYY-MM-DD, not in the future, or empty"}), 400
    import gmail
    from urllib.parse import quote
    url = f"{gmail.GMAIL_BASE}/messages?maxResults=1&includeSpamTrash=false"
    if after:
        url += f"&q={quote('after:' + after.replace('-', '/'))}"
    expired = {"error": f"{current_provider().label} sign-in expired — sign in again"}
    try:
        token = get_fresh_token()
        if not token:
            return jsonify(expired), 401
        data = gmail.make_request(gmail.get_headers(token), url)
        est = int(data.get("resultSizeEstimate", 0))
    except AuthExpired:
        return jsonify(expired), 401
    except Exception:
        return jsonify({"error": "gmail did not answer — try again"}), 502
    return jsonify({"estimated_threads": est,
                    "estimated_tagging_usd": round(est * 0.0006, 2),
                    "note": "estimate"})


@app.route("/sync/progress")
@login_required
def sync_progress():
    return jsonify(db.get_sync_progress(current_user_id()))


@app.route("/sync/stop")
@login_required
def stop_sync():
    db.set_sync_flag(current_user_id(), "1")
    return jsonify({"ok": True})


@app.route("/wipe", methods=["POST"])
@login_required
def wipe():
    if DEMO_MODE:
        # The demo catalog is only seeded at startup, so wiping it would leave
        # an empty app with no way back short of a restart. The button is
        # hidden, but hiding a control is not the same as disabling it.
        return jsonify({"error": "wipe is disabled in demo mode"}), 403
    # Also drops this user's delta tokens, so the next sync rebuilds from
    # scratch rather than reporting "nothing changed" against an empty catalog.
    db.wipe_db(current_user_id())
    return jsonify({"ok": True})


@app.route("/resync", methods=["POST"])
@login_required
def resync():
    """Force the next sync to re-scan every folder from scratch.

    Recovery hatch for delta drift: keeps existing threads and tags, but
    discards the folder tokens so nothing missed can stay missed.
    """
    if DEMO_MODE:
        return jsonify({"error": "sync is disabled in demo mode"}), 409
    user_id = current_user_id()
    if is_running(sync_running, user_id):
        return jsonify({"error": "sync in progress"}), 409
    db.clear_delta_links(user_id)
    return jsonify({"ok": True})


@app.route("/retag-empty")
@login_required
def retag_empty():
    if DEMO_MODE:
        return "Retagging is disabled in demo mode", 400
    user_id = current_user_id()
    if is_running(sync_running, user_id):
        return "Sync already running", 400

    set_running(sync_running, user_id, True)
    t = threading.Thread(
        target=run_retag_empty,
        args=(user_id, current_provider(), get_fresh_token(),
              db.get_token_cache(user_id)),
        daemon=True
    )
    t.start()
    return redirect(url_for("index"))


MAX_USER_TAGS_PER_REQUEST = 20
MAX_TAG_LENGTH = 60


@app.route("/threads/<path:thread_id>/tags", methods=["POST"])
@login_required
def edit_tags(thread_id):
    """Add or remove hand-written tags on a thread."""
    data = request.get_json(silent=True) or {}
    add = data.get("add") or []
    remove = data.get("remove") or []

    if not isinstance(add, list) or not isinstance(remove, list):
        return jsonify({"error": "add/remove must be lists"}), 400
    if len(add) + len(remove) > MAX_USER_TAGS_PER_REQUEST:
        return jsonify({"error": "too many tags in one request"}), 400

    add = [str(t)[:MAX_TAG_LENGTH] for t in add]
    remove = [str(t)[:MAX_TAG_LENGTH] for t in remove]

    tags = db.edit_user_tags(current_user_id(), thread_id, add=add, remove=remove)
    if tags is None:
        return jsonify({"error": "thread not found"}), 404
    return jsonify({"ok": True, "user_tags": tags})


MAX_IMPORT_BYTES = 200 * 1024 * 1024


def _decompress_bounded(raw, limit):
    """Inflate a gzip body, refusing to allocate more than `limit` bytes.

    gzip.decompress() expands the whole stream into one buffer before anyone
    can check its size, so the compressed-length check upstream says nothing
    about what is about to be allocated: a few MB of repeated bytes inflate to
    gigabytes and take the single worker down with them. Inflating in chunks
    lets the limit apply to the output, which is the number that matters.
    """
    out = bytearray()
    d = zlib.decompressobj(16 + zlib.MAX_WBITS)   # 16 = expect a gzip header
    view = memoryview(raw)
    for i in range(0, len(view), 1 << 20):
        out += d.decompress(view[i:i + (1 << 20)], limit - len(out) + 1)
        if len(out) > limit:
            raise ValueError("decompressed body exceeds limit")
    out += d.flush()
    if not d.eof:
        # decompressobj, unlike gzip.decompress, reports a truncated stream by
        # simply stopping — no exception, just short output. Detecting that is
        # the whole reason import errors can say "looks truncated", and a
        # partial inflate that happened to parse would import half a catalog.
        raise EOFError("compressed stream ended before its end-of-stream marker")
    if len(out) > limit:
        raise ValueError("decompressed body exceeds limit")
    return bytes(out)


@app.route("/export")
@login_required
def export_catalog():
    """Download this account's catalog, tags included.

    Re-tagging an existing catalog on another machine costs real money; this
    moves it for nothing, and doubles as the backup a deployment otherwise
    has no way to take.
    """
    user_id = current_user_id()
    payload = db.export_threads(user_id)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    email = (session.get("user_email") or "catalog").split("@")[0]

    # Gzipped: an 11k-thread catalog is ~19MB of JSON but a fraction of that
    # compressed, which keeps the upload well clear of proxy body limits.
    body = gzip.compress(json.dumps(payload).encode(), compresslevel=6)
    print(f"Export: {len(body):,} bytes gzipped for {user_id}")
    return app.response_class(
        body,
        mimetype="application/gzip",
        headers={"Content-Disposition":
                 f'attachment; filename="catalog-{email}-{stamp}.json.gz"'},
    )


@app.route("/import", methods=["POST"])
@login_required
def import_catalog():
    """Load an exported catalog into this account."""
    user_id = current_user_id()
    if is_running(sync_running, user_id) and not db.get_sync_progress(user_id).get("stale"):
        return jsonify({"error": "sync in progress"}), 409

    raw = request.get_data(cache=False)
    declared = request.content_length
    print(f"Import: received {len(raw):,} bytes "
          f"(Content-Length: {declared if declared is not None else 'absent'})")

    if len(raw) > MAX_IMPORT_BYTES:
        return jsonify({"error": "file too large"}), 413
    if not raw:
        return jsonify({"error": "empty upload — the file never arrived"}), 400

    # gzip is accepted so a large catalog doesn't have to cross the network raw.
    if raw[:2] == b"\x1f\x8b":
        try:
            raw = _decompress_bounded(raw, MAX_IMPORT_BYTES)
            print(f"Import: decompressed to {len(raw):,} bytes")
        except ValueError:
            return jsonify({"error": "file too large once decompressed"}), 413
        except EOFError:
            # Truncated upload — by far the most common failure, and the one
            # that previously escaped as an unhandled 500.
            return jsonify({"error": f"upload looks truncated: {len(raw):,} "
                                     "compressed bytes received, and the gzip "
                                     "stream ends mid-way"}), 400
        except OSError as e:
            return jsonify({"error": f"could not decompress: {e}"}), 400

    try:
        payload = json.loads(raw)
    except ValueError as e:
        # Say what actually went wrong. "not valid JSON" told the user nothing,
        # and a truncated upload is by far the most likely cause.
        truncated = not raw.rstrip().endswith(b"}")
        detail = (
            f"upload looks truncated: {len(raw):,} bytes received"
            + (f", Content-Length said {declared:,}" if declared else "")
            if truncated else f"malformed JSON at position {getattr(e, 'pos', '?')}"
        )
        print(f"Import failed: {detail} ({e})")
        return jsonify({"error": detail}), 400

    replace = request.args.get("replace") == "1"
    try:
        imported = db.import_threads(user_id, payload, replace=replace)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    print(f"Imported {imported} threads for {user_id} (replace={replace})")
    return jsonify({"ok": True, "imported": imported,
                    "total": db.count_threads(user_id)})


@app.route("/detective")
@login_required
def detective():
    return render_template("detective.html")


@app.route("/detective/search")
@login_required
def detective_search():
    user_id = current_user_id()
    if is_running(sync_running, user_id):
        return jsonify({"error": "sync in progress"}), 409
    touch_running(detective_running, user_id)

    query = request.args.get("q", "").strip()
    if not query:
        return jsonify({"count": 0, "threads": []})

    size_map = {"small": 10, "medium": 100, "large": 500, "xlarge": 1000, "huge": 5000}
    size_label = request.args.get("min_attachment_size", "")

    threads = db.search_threads(
        user_id,
        query=query,
        search_mode=request.args.get("search_mode", "or"),
        sort_by="date_desc",
        date_from=request.args.get("date_from") or None,
        date_to=request.args.get("date_to") or None,
        has_attachments=True if request.args.get("has_attachments") == "1" else None,
        has_multiple=request.args.get("has_multiple") or None,
        min_attachment_size=size_map.get(size_label)
    )

    result = [{
        "thread_id": t["thread_id"],
        "subject": t["subject"],
        "date_first": t["date_first"],
        "date_last": t["date_last"],
        "web_link": t.get("web_link", ""),
        "participants_preview": ", ".join(json.loads(t.get("participants") or "[]")),
        "tags_preview": json.loads(t.get("ai_tags") or "[]"),
    } for t in threads]
    return jsonify({"count": len(result), "threads": result})


def mark_last_message_cacheable(messages):
    """Put a cache breakpoint on the final message so the growing Detective
    history is read from cache instead of reprocessed every round.

    Returns a shallow copy; the caller's list is left untouched.
    """
    if not messages:
        return messages

    out = list(messages)
    last = dict(out[-1])
    content = last.get("content")

    if isinstance(content, str):
        last["content"] = [{
            "type": "text",
            "text": content,
            "cache_control": {"type": "ephemeral"},
        }]
    elif isinstance(content, list) and content:
        blocks = [dict(b) if isinstance(b, dict) else b for b in content]
        if isinstance(blocks[-1], dict):
            blocks[-1]["cache_control"] = {"type": "ephemeral"}
        last["content"] = blocks
    else:
        return messages  # nothing safe to mark

    out[-1] = last
    return out


@app.route("/detective/ask", methods=["POST"])
@login_required
def detective_ask():
    touch_running(detective_running, current_user_id())

    data = request.get_json(silent=True) or {}
    messages = data.get("messages", [])
    system_prompt = data.get("system")
    if not messages:
        return jsonify({"error": "no messages"}), 400

    if not isinstance(messages, list):
        return jsonify({"error": "messages must be a list"}), 400
    if len(messages) > DETECTIVE_MAX_MESSAGES:
        return jsonify({"error": "this Detective session has run too long — "
                                 "start a new search"}), 400

    size = len(json.dumps(messages)) + len(system_prompt or "")
    if size > DETECTIVE_MAX_CHARS:
        return jsonify({"error": "this Detective session has grown too large — "
                                 "start a new search"}), 400

    # Second breakpoint, on the last message of the history. Each round's
    # prefix is identical to the previous round's, so the accumulated
    # conversation is read at ~0.1x and only the new turn is written.
    # This matters far more than the system prompt: history grows every
    # round while the system prompt is fixed.
    messages = mark_last_message_cacheable(messages)

    kwargs = {
        "model": DETECTIVE_MODEL,
        "max_tokens": 4000,
        "messages": messages,
    }
    if system_prompt:
        # First breakpoint. Byte-identical across all rounds of a session,
        # so it's written once and read cheaply thereafter.
        kwargs["system"] = [{
            "type": "text",
            "text": system_prompt,
            "cache_control": {"type": "ephemeral"},
        }]

    exceeded, spent, limit = over_spend_limit(current_user_id())
    if exceeded:
        return jsonify({"error": f"This account has reached its ${limit:.2f} "
                                 f"monthly limit (${spent:.2f} used). It resets "
                                 f"on the first of next month."}), 402

    if not os.getenv("ANTHROPIC_API_KEY"):
        # Without a key the client raises TypeError from inside the SDK, which
        # is neither an APIStatusError nor an APIConnectionError — so this used
        # to escape as a 500 with a traceback. Say what is actually wrong.
        return jsonify({"error": "No ANTHROPIC_API_KEY is configured, so "
                                 "Detective cannot run. Search still works."}), 503

    try:
        response = anthropic_client.messages.create(**kwargs)
    except anthropic.APIStatusError as e:
        print(f"Detective API error: {e}")
        return jsonify({"error": "model request failed"}), 502
    except anthropic.APIConnectionError as e:
        print(f"Detective connection error: {e}")
        return jsonify({"error": "could not reach the model"}), 503
    except Exception as e:
        # Last resort: a misconfiguration should not hand the browser a
        # traceback in place of the JSON its polling loop expects.
        print(f"Detective unexpected error: {type(e).__name__}: {e}")
        return jsonify({"error": "the model request could not be made"}), 500

    text = next((b.text for b in response.content if b.type == "text"), "")
    return jsonify({
        "reply": text,
        "usage": {
            "cache_read": response.usage.cache_read_input_tokens,
            "cache_write": response.usage.cache_creation_input_tokens,
            "input": response.usage.input_tokens,
            "output": response.usage.output_tokens,
        },
    })


@app.route("/detective/start", methods=["POST"])
@login_required
def detective_start():
    user_id = current_user_id()
    if is_running(sync_running, user_id):
        return jsonify({"error": "sync in progress"}), 409
    set_running(detective_running, user_id, True)
    return jsonify({"ok": True})


@app.route("/detective/done", methods=["POST"])
@login_required
def detective_done():
    set_running(detective_running, current_user_id(), False)
    return jsonify({"ok": True})


@app.route("/detective/status")
@login_required
def detective_status():
    return jsonify({"running": is_running(detective_running, current_user_id(), ttl=DETECTIVE_TTL_SECONDS)})


# ── Scheduled backups ─────────────────────────────────────────────────────────
#
# The database is the only copy of a personal archive and of the tagging that
# was paid for to build it, and until this existed the only protection was
# remembering to run backup.py by hand.
#
# A timer thread rather than a platform cron job: job state already lives in
# this process, the app runs a single gunicorn worker by design, and a cron job
# on Render is a separate paid service. One worker means exactly one scheduler
# and no coordination to get wrong.
#
# Snapshots land beside the database, which covers a bad import, a wipe or
# corruption — but not losing the disk. Off-disk copies are a separate problem.

BACKUP_INTERVAL_HOURS = float(os.getenv("BACKUP_INTERVAL_HOURS", "24"))
BACKUP_KEEP = int(os.getenv("BACKUP_KEEP", "7"))
BACKUP_POLL_SECONDS = 900

# A sync claim is only honoured while it is fresh. Otherwise one sync that died
# without clearing its flag would suppress every future backup — the failure
# would be silent, and would disable exactly the thing that recovers from it.
BACKUP_SYNC_GRACE_SECONDS = 3600

_backup_status = {"last_error": None, "last_run": None}


def newest_backup_age_seconds():
    """Age of the most recent snapshot, or None if there are none.

    Measured from the filesystem rather than from a counter this process
    keeps, so a scheduler that died reports an ageing backup instead of
    whatever value it last managed to set.
    """
    snapshots = backup.existing()
    if not snapshots:
        return None
    return max(0.0, time.time() - os.path.getmtime(snapshots[0]))


def backup_due():
    if BACKUP_INTERVAL_HOURS <= 0:
        return False
    age = newest_backup_age_seconds()
    return age is None or age >= BACKUP_INTERVAL_HOURS * 3600


def run_scheduled_backup():
    """Take one snapshot if the newest is older than the interval."""
    if not backup_due():
        return False

    # Not a correctness guard: the online backup API takes a read lock per page
    # and is safe under concurrent writes. This only keeps a large import from
    # competing with the snapshot for the same disk, and the next poll retries.
    with _state_lock:
        busy = any(time.time() - stamp < BACKUP_SYNC_GRACE_SECONDS
                   for stamp in sync_running.values())
    if busy:
        return False

    try:
        code = backup.take(keep=BACKUP_KEEP)
    except Exception as e:
        # A failed backup must never take the app down with it.
        _backup_status["last_error"] = str(e)
        print(f"  scheduled backup failed: {e}")
        return False

    _backup_status["last_run"] = time.time()
    _backup_status["last_error"] = None if code == 0 else "snapshot refused"
    return code == 0


def backup_status():
    age = newest_backup_age_seconds()
    return {
        "enabled": BACKUP_INTERVAL_HOURS > 0,
        "interval_hours": BACKUP_INTERVAL_HOURS,
        "count": len(backup.existing()),
        "age_hours": None if age is None else age / 3600,
        "overdue": backup_due(),
        "last_error": _backup_status["last_error"],
    }


def _backup_loop():
    while True:
        time.sleep(BACKUP_POLL_SECONDS)
        run_scheduled_backup()


def start_backup_scheduler():
    """Start the timer thread, unless backups are off or this is the demo."""
    if BACKUP_INTERVAL_HOURS <= 0 or DEMO_MODE:
        return None
    thread = threading.Thread(target=_backup_loop, daemon=True,
                              name="backup-scheduler")
    thread.start()
    return thread


log_startup_config()
recover_after_restart()
start_backup_scheduler()


if __name__ == "__main__":
    # PORT is overridable because macOS ships AirPlay Receiver bound to 5000,
    # so the default fails to bind on a stock Mac — which is the first thing a
    # new clone runs.
    app.run(
        # Localhost by default; HOST=0.0.0.0 for a container where the
        # reverse proxy is the one doing the exposing.
        host=os.getenv("HOST", "127.0.0.1"),
        port=int(os.getenv("PORT", "5000")),
        debug=bool(os.getenv("FLASK_DEBUG")),
    )
