#!/usr/bin/env bash
# Smoke test for mcp_server.py: speaks real JSON-RPC over stdio (no agent
# involved).
#
# Part 1 checks that all four catalog tools show up in tools/list.
# Part 2 does one real tools/call round trip against a seeded temp database:
# it proves the server can actually answer a call end to end (tools/list
# alone never exercises a tool handler, DB_PATH resolution, or resolve_user),
# and specifically that a freshly-seeded, never-synced catalog answers
# sync_status cleanly rather than raising.
#
# Usage: scripts/mcp_smoke.sh   (run from the repo root, or anywhere — it
# cd's to its own repo root first)
set -euo pipefail

cd "$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

PYTHON=.venv-mcp/bin/python
SETUP_PYTHON=.venv/bin/python

if [ ! -x "$PYTHON" ]; then
    echo "missing $PYTHON — create .venv-mcp first (see README: MCP server > Setup)" >&2
    exit 1
fi
if [ ! -x "$SETUP_PYTHON" ]; then
    echo "missing $SETUP_PYTHON — create .venv first (see README: Tests)" >&2
    exit 1
fi

INITIALIZE='{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"smoke-test","version":"0"}}}'
INITIALIZED='{"jsonrpc":"2.0","method":"notifications/initialized"}'

# A scratch dir holds every temp file/dir this run needs; one trap cleans it
# all up regardless of where the script exits.
SCRATCH=$(mktemp -d)
trap 'rm -rf "$SCRATCH"' EXIT

# stdout and stderr are kept apart deliberately: stdout must be nothing but
# the JSON-RPC stream (that's what gets grepped/parsed below), and mixing
# stderr in used to let an interleaved log line corrupt a line of JSON or
# hide inside a false-positive grep match.
STDOUT="$SCRATCH/stdout"
STDERR="$SCRATCH/stderr"

# ── Part 1: tools/list ───────────────────────────────────────────────────────
TOOLS_LIST='{"jsonrpc":"2.0","id":2,"method":"tools/list"}'

# The trailing `sleep` keeps stdin open a beat after the last request.
# Redirecting stdout to a regular file (instead of a tty or a pipe straight
# to the terminal) changes how fast the input side hits EOF, and closing
# stdin the instant the last line is written can race the server's async
# response handling and drop a reply before it's flushed — tools/list is
# slow enough to lose that race under plain `prog < input`, even though it
# looks instantaneous when piped straight to a terminal.
{ printf '%s\n%s\n%s\n' "$INITIALIZE" "$INITIALIZED" "$TOOLS_LIST"; sleep 1; } \
    | "$PYTHON" mcp_server.py >"$STDOUT" 2>"$STDERR"

cat "$STDOUT"
if [ -s "$STDERR" ]; then
    echo "--- stderr (tools/list) ---" >&2
    cat "$STDERR" >&2
fi

MISSING=0
for TOOL in search_catalog get_thread list_tags sync_status; do
    if ! grep -q "$TOOL" "$STDOUT"; then
        echo "MISSING TOOL: $TOOL" >&2
        MISSING=1
    fi
done

if [ "$MISSING" -ne 0 ]; then
    echo "smoke test FAILED (tools/list)" >&2
    exit 1
fi

echo "tools/list OK: all four tools present"

# Each tool must carry a non-empty outputSchema (structured output):
# tools/list's result is a single JSON-RPC response line with id 2.
TOOLS_LIST_RESPONSE=$(grep '"id":2' "$STDOUT" || true)
if [ -z "$TOOLS_LIST_RESPONSE" ]; then
    echo "smoke test FAILED: no response to tools/list" >&2
    exit 1
fi

for TOOL in search_catalog get_thread list_tags sync_status; do
    HAS_SCHEMA=$(echo "$TOOLS_LIST_RESPONSE" | jq -r --arg t "$TOOL" \
        '.result.tools[] | select(.name == $t) | (.outputSchema // {} | length > 0)')
    if [ "$HAS_SCHEMA" != "true" ]; then
        echo "MISSING outputSchema for tool: $TOOL" >&2
        MISSING=1
    fi
done

if [ "$MISSING" -ne 0 ]; then
    echo "smoke test FAILED (outputSchema)" >&2
    exit 1
fi

echo "tools/list OK: all four tools carry a non-empty outputSchema"

# ── Part 2: a real tools/call round trip ────────────────────────────────────
# A temp, disposable database — never the real catalog.db — seeded with one
# user and no threads, so this also exercises the empty-catalog path (see
# mcp_tools.EMPTY_CATALOG_NOTICE) on a schema that was initialised properly,
# as opposed to a missing file.
export DB_PATH="$SCRATCH/catalog.db"
export CATALOG_USER="smoke-test@example.com"

"$SETUP_PYTHON" -c "
import db
db.init_db()
db.upsert_user('smoke-user', '$CATALOG_USER', 'Smoke Test', '2024-01-01T00:00:00Z')
"

SYNC_STATUS_CALL='{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"sync_status","arguments":{}}}'
BOGUS_THREAD_CALL='{"jsonrpc":"2.0","id":4,"method":"tools/call","params":{"name":"get_thread","arguments":{"thread_id":"no-such-thread-xyz"}}}'

: >"$STDOUT"
: >"$STDERR"

# Same stdin-EOF race as Part 1, this time against tools/call.
{ printf '%s\n%s\n%s\n%s\n' "$INITIALIZE" "$INITIALIZED" "$SYNC_STATUS_CALL" "$BOGUS_THREAD_CALL"; sleep 1; } \
    | "$PYTHON" mcp_server.py >"$STDOUT" 2>"$STDERR"

if [ -s "$STDERR" ]; then
    echo "--- stderr (tools/call) ---" >&2
    cat "$STDERR" >&2
fi

RESPONSE=$(grep '"id":3' "$STDOUT" || true)
if [ -z "$RESPONSE" ]; then
    echo "smoke test FAILED: no response to tools/call sync_status" >&2
    cat "$STDOUT" >&2
    exit 1
fi

IS_ERROR=$(echo "$RESPONSE" | jq -r '.result.isError')
if [ "$IS_ERROR" != "false" ]; then
    echo "smoke test FAILED: sync_status call errored: $RESPONSE" >&2
    exit 1
fi

# The payload is nested text: {"result":{"content":[{"type":"text","text":"<json>"}],"isError":false}}
PAYLOAD=$(echo "$RESPONSE" | jq -r '.result.content[0].text')
if ! echo "$PAYLOAD" | jq -e 'has("thread_count")' >/dev/null; then
    echo "smoke test FAILED: sync_status payload missing thread_count: $PAYLOAD" >&2
    exit 1
fi

# structuredContent is the SDK's separate, schema-validated echo of the same
# payload — proves outputSchema isn't just advertised but actually populated.
STRUCTURED_THREAD_COUNT=$(echo "$RESPONSE" | jq -r '.result.structuredContent.thread_count')
if [ "$STRUCTURED_THREAD_COUNT" = "null" ] || [ -z "$STRUCTURED_THREAD_COUNT" ]; then
    echo "smoke test FAILED: sync_status structuredContent missing thread_count: $RESPONSE" >&2
    exit 1
fi

echo "tools/call OK: sync_status round trip succeeded ($(echo "$PAYLOAD" | jq -c .))"

# get_thread on a bogus thread_id must surface mcp_tools' {"error": "..."}
# shape cleanly through the SDK's own outputSchema validation, not fail as a
# schema-validation error (Review Focus: this is the whole point of the
# "error" key being present on every TypedDict here).
ERROR_RESPONSE=$(grep '"id":4' "$STDOUT" || true)
if [ -z "$ERROR_RESPONSE" ]; then
    echo "smoke test FAILED: no response to tools/call get_thread (bogus id)" >&2
    cat "$STDOUT" >&2
    exit 1
fi

ERROR_IS_ERROR=$(echo "$ERROR_RESPONSE" | jq -r '.result.isError')
if [ "$ERROR_IS_ERROR" != "false" ]; then
    echo "smoke test FAILED: bogus get_thread should surface as a clean {\"error\":...} payload, not a tool-call error: $ERROR_RESPONSE" >&2
    exit 1
fi

ERROR_PAYLOAD=$(echo "$ERROR_RESPONSE" | jq -r '.result.content[0].text')
if ! echo "$ERROR_PAYLOAD" | jq -e 'has("error")' >/dev/null; then
    echo "smoke test FAILED: bogus get_thread payload missing error key: $ERROR_PAYLOAD" >&2
    exit 1
fi

ERROR_STRUCTURED=$(echo "$ERROR_RESPONSE" | jq -r '.result.structuredContent | has("error")')
if [ "$ERROR_STRUCTURED" != "true" ]; then
    echo "smoke test FAILED: bogus get_thread structuredContent missing error key: $ERROR_RESPONSE" >&2
    exit 1
fi

echo "tools/call OK: bogus get_thread surfaced a clean error, not a schema-validation failure"
echo "smoke test PASSED"
