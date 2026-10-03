"""Benchmark-only MCP server: raw Gmail, no index.

The control condition for the catalog benchmark. Exposes Gmail's own
search (users.messages.list?q=) and message fetch, nothing else — the
same surface any generic Gmail connector gives an agent, run over the
same stored credentials and the same stdio transport as mcp_server.py,
so the only experimental variable is the tag index.

Read-only against the mailbox (gmail.readonly scope). Not part of the
product; lives in bench/ on purpose.
"""
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from dotenv import load_dotenv

load_dotenv(os.path.join(REPO, ".env"))
os.environ.setdefault("DB_PATH", os.path.join(REPO, "catalog.db"))

from mcp.server.mcpserver import MCPServer  # noqa: E402

import db  # noqa: E402
import gauth  # noqa: E402
import gmail  # noqa: E402
import mcp_tools  # noqa: E402

mcp = MCPServer("gmail-raw")


def _token():
    user_id = mcp_tools.resolve_user()
    token, _ = gauth.get_valid_token(db.get_token_cache(user_id))
    if not token:
        raise ValueError("Gmail sign-in expired — sign in again via the Catalog web app.")
    return token


@mcp.tool()
def search_messages(q: str, max_results: int = 20) -> dict:
    """Search the Gmail mailbox with Gmail's own query syntax (from:, subject:,
    after:YYYY/MM/DD, quoted phrases...). Returns message ids with subject,
    sender, date and snippet. Each call is a live Gmail API request."""
    token = _token()
    headers = gmail.get_headers(token)
    from urllib.parse import quote
    data = gmail.make_request(
        headers,
        f"{gmail.GMAIL_BASE}/messages?maxResults={max(1, min(int(max_results), 50))}"
        f"&includeSpamTrash=false&q={quote(q)}",
    )
    out = []
    for m in data.get("messages", [])[: max(1, min(int(max_results), 50))]:
        meta = gmail.make_request(
            headers,
            f"{gmail.GMAIL_BASE}/messages/{m['id']}?format=metadata"
            "&metadataHeaders=Subject&metadataHeaders=From&metadataHeaders=Date",
        )
        payload = meta.get("payload") or {}
        out.append({
            "id": meta.get("id"),
            "thread_id": meta.get("threadId"),
            "subject": gmail.header_value(payload, "Subject"),
            "from": gmail.header_value(payload, "From"),
            "date": gmail.header_value(payload, "Date"),
            "snippet": meta.get("snippet", ""),
        })
    return {"messages": out, "estimate": data.get("resultSizeEstimate")}


@mcp.tool()
def get_message(message_id: str) -> dict:
    """Fetch one message's full text by id (slow: live Gmail fetch)."""
    token = _token()
    data = gmail.get_message(token, message_id)
    if not data:
        return {"error": f"no such message: {message_id}"}
    payload = data.get("payload") or {}
    body = mcp_tools._strip_html(gmail.extract_body(payload))
    return {
        "subject": gmail.header_value(payload, "Subject"),
        "from": gmail.header_value(payload, "From"),
        "date": gmail.header_value(payload, "Date"),
        "body_text": body[:50_000],
        "attachments": [a.get("name", "") for a in gmail.extract_attachments(payload)],
    }


if __name__ == "__main__":
    mcp.run(transport="stdio")
