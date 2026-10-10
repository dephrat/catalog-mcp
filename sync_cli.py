"""Run a mailbox sync from the command line, outside the web app.

Mirrors the /sync route's guards (app.py, ~line 1547: is_running /
set_running against app.sync_running, plus the DB-backed
progress/heartbeat) so a CLI run can't stack a second sync on top of one the
web app already started, and refreshes the stored token the same way
get_fresh_token() does for a browser session — there is no Flask session
here, so the token has to come from the user's row in the DB instead.

load_dotenv() runs before app is imported so SECRET_KEY (which app.py
requires at import time) is populated from .env in a plain shell.

Exit codes: 0 success, 1 sync ran but failed (see server logs / sync_state),
2 credentials missing/expired or CATALOG_USER/--user doesn't resolve to a
user, 3 a sync already looks to be running for this account.
--since (gmail only) stores the window before syncing, like POST /sync/range.
"""
import argparse
import os
import sys

from dotenv import load_dotenv

load_dotenv()

import app  # noqa: E402 — after load_dotenv() so SECRET_KEY is set
import db  # noqa: E402
import mcp_tools  # noqa: E402
import providers  # noqa: E402


def _resolve_user_id(email):
    """mcp_tools.resolve_user() reads CATALOG_USER; --user overrides it for
    this one resolution without leaving the override behind for callers that
    run main() again in the same process (e.g. the test suite)."""
    if email is None:
        return mcp_tools.resolve_user()
    previous = os.environ.get("CATALOG_USER")
    os.environ["CATALOG_USER"] = email
    try:
        return mcp_tools.resolve_user()
    finally:
        if previous is None:
            os.environ.pop("CATALOG_USER", None)
        else:
            os.environ["CATALOG_USER"] = previous


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Sync one account's mailbox into the catalog.")
    parser.add_argument(
        "--user", metavar="EMAIL",
        help="account to sync (overrides CATALOG_USER)")
    parser.add_argument(
        "--since", metavar="YYYY-MM-DD|all",
        help="gmail only: index mail from this date ('all' = everything) "
             "before syncing; widening re-walks existing folders")
    args = parser.parse_args(argv)

    since = None
    if args.since is not None:
        raw = args.since.strip()
        # Empty is not "everything" here: that must be asked for as 'all'.
        since = (None if raw == "" else
                 app._parse_range_date("" if raw.lower() == "all" else raw))
        if since is None:
            print("--since must be YYYY-MM-DD (not in the future) or 'all'",
                  file=sys.stderr)
            return 2

    try:
        user_id = _resolve_user_id(args.user)
    except ValueError as e:
        print(str(e), file=sys.stderr)
        return 2

    # app.sync_running is a plain in-process dict: it only sees a sync
    # started by this same process, so it alone could never catch one the
    # web app's gunicorn worker already has running (or vice versa). The
    # DB-backed sync_state row is what the web app's own /sync route
    # actually trusts for that cross-process case (app.py ~line 1547): a
    # status in db.ACTIVE_STATUSES whose heartbeat ("stale") hasn't gone
    # quiet means a sync is genuinely in flight somewhere, in or out of
    # this process. Checking both — the in-process dict for this process's
    # own runs, the DB heartbeat for everyone else's — is what actually
    # closes the gap the old in-process-only check left.
    if app.is_running(app.sync_running, user_id):
        print(f"sync already running for {user_id}", file=sys.stderr)
        return 3

    progress = db.get_sync_progress(user_id)
    if progress["status"] in db.ACTIVE_STATUSES and not progress["stale"]:
        print(f"sync already running for {user_id}", file=sys.stderr)
        return 3

    if since is not None:
        if mcp_tools.provider_for(user_id) != "gmail":
            print("--since is gmail-only", file=sys.stderr)
            return 2
        widening = app.range_change_widens(user_id, since)
        db.set_sync_after(user_id, since)
        if widening:
            db.clear_delta_links(user_id)

    token_cache = db.get_token_cache(user_id)
    if not token_cache:
        print(
            f"no stored credentials for {user_id} — sign in via the web app "
            "first",
            file=sys.stderr,
        )
        return 2

    provider = providers.get(mcp_tools.provider_for(user_id))
    access_token, new_cache = provider.refresh_token(token_cache)
    if not access_token:
        print(
            f"{provider.label} sign-in expired — please sign in again via "
            "the web app.",
            file=sys.stderr,
        )
        return 2
    if new_cache and new_cache != token_cache:
        db.set_token_cache(user_id, new_cache)

    # run_sync's own finally clears this, but nothing else sets it here —
    # unlike the /sync route, which sets it right before starting the
    # background thread. Without this, the DB-heartbeat guard above would
    # be the only cross-process signal (fine), but the in-process one
    # above (and app.py code that also checks app.sync_running) would never
    # see this run as active while it's in flight.
    app.set_running(app.sync_running, user_id, True)
    app.run_sync(user_id, provider, access_token, token_cache)

    # run_sync records its own outcome in sync_state rather than raising —
    # a swallowed internal failure must not read back as success.
    status = db.get_sync_progress(user_id)["status"]
    if status == "auth_expired":
        print(
            f"{provider.label} sign-in expired — please sign in again via "
            "the web app.",
            file=sys.stderr,
        )
        return 2
    if status == "error":
        print(f"sync failed for {user_id} — see server logs", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
