"""Run a mailbox sync from the command line, outside the web app.

Mirrors the /sync route's guards (app.py: is_running / set_running against
app.sync_running) so a CLI run can't stack a second sync on top of one the
web app already started, and refreshes the stored token the same way
get_fresh_token() does for a browser session — there is no Flask session
here, so the token has to come from the user's row in the DB instead.

load_dotenv() runs before app is imported so SECRET_KEY (which app.py
requires at import time) is populated from .env in a plain shell.
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
    args = parser.parse_args(argv)

    user_id = _resolve_user_id(args.user)

    if app.is_running(app.sync_running, user_id):
        print(f"sync already running for {user_id}", file=sys.stderr)
        return 3

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

    app.run_sync(user_id, provider, access_token, token_cache)
    return 0


if __name__ == "__main__":
    sys.exit(main())
