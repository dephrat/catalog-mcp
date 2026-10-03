#!/usr/bin/env bash
# Smoke test for mcp_server.py: speaks real JSON-RPC over stdio (no agent
# involved).
#
# Part 1 checks that all four catalog tools show up in tools/list, plus the
# catalog://tags resource, the catalog://thread/{thread_id} resource
# template, and the find_document prompt show up in their respective
# /list endpoints.
# Part 2 does real tools/call, resources/read, and completion/complete round
# trips against a seeded temp database: it proves the server can actually
# answer a call end to end (tools/list alone never exercises a tool handler,
# DB_PATH resolution, or resolve_user), and specifically that a
# freshly-seeded, never-synced catalog answers sync_status cleanly rather
# than raising.
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

# ── Part 1: tools/list, resources/list, resources/templates/list, prompts/list ─
TOOLS_LIST='{"jsonrpc":"2.0","id":2,"method":"tools/list"}'
RESOURCES_LIST='{"jsonrpc":"2.0","id":10,"method":"resources/list"}'
RESOURCE_TEMPLATES_LIST='{"jsonrpc":"2.0","id":11,"method":"resources/templates/list"}'
PROMPTS_LIST='{"jsonrpc":"2.0","id":12,"method":"prompts/list"}'

# The trailing `sleep` keeps stdin open a beat after the last request.
# Redirecting stdout to a regular file (instead of a tty or a pipe straight
# to the terminal) changes how fast the input side hits EOF, and closing
# stdin the instant the last line is written can race the server's async
# response handling and drop a reply before it's flushed — tools/list is
# slow enough to lose that race under plain `prog < input`, even though it
# looks instantaneous when piped straight to a terminal.
{ printf '%s\n%s\n%s\n%s\n%s\n%s\n' "$INITIALIZE" "$INITIALIZED" "$TOOLS_LIST" \
    "$RESOURCES_LIST" "$RESOURCE_TEMPLATES_LIST" "$PROMPTS_LIST"; sleep 1; } \
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

# get_thread gains a `ctx: Context` parameter (Task 3, progress reporting)
# that is SDK-injected, not client-supplied — it must never show up in the
# client-visible inputSchema. Guard: thread_id stays the only property and
# the only required one, before and after that change.
GET_THREAD_SCHEMA=$(echo "$TOOLS_LIST_RESPONSE" | jq -c \
    '.result.tools[] | select(.name == "get_thread") | .inputSchema')
if [ -z "$GET_THREAD_SCHEMA" ] || [ "$GET_THREAD_SCHEMA" = "null" ]; then
    echo "smoke test FAILED: get_thread missing inputSchema" >&2
    exit 1
fi
GET_THREAD_REQUIRED=$(echo "$GET_THREAD_SCHEMA" | jq -c '.required')
if [ "$GET_THREAD_REQUIRED" != '["thread_id"]' ]; then
    echo "smoke test FAILED: get_thread inputSchema.required should be [\"thread_id\"], got: $GET_THREAD_REQUIRED" >&2
    exit 1
fi
GET_THREAD_PROPERTIES=$(echo "$GET_THREAD_SCHEMA" | jq -c '.properties | keys')
if [ "$GET_THREAD_PROPERTIES" != '["thread_id"]' ]; then
    echo "smoke test FAILED: get_thread inputSchema.properties should expose only thread_id (no SDK-injected ctx), got: $GET_THREAD_PROPERTIES" >&2
    exit 1
fi
echo "tools/list OK: get_thread inputSchema exposes only thread_id (ctx not client-visible)"

# resources/list must advertise the static catalog://tags resource.
RESOURCES_LIST_RESPONSE=$(grep '"id":10' "$STDOUT" || true)
if [ -z "$RESOURCES_LIST_RESPONSE" ]; then
    echo "smoke test FAILED: no response to resources/list" >&2
    exit 1
fi
HAS_TAGS_RESOURCE=$(echo "$RESOURCES_LIST_RESPONSE" | jq -r \
    '.result.resources[] | select(.uri == "catalog://tags") | .uri')
if [ "$HAS_TAGS_RESOURCE" != "catalog://tags" ]; then
    echo "smoke test FAILED: resources/list missing catalog://tags: $RESOURCES_LIST_RESPONSE" >&2
    exit 1
fi
echo "resources/list OK: catalog://tags present"

# resources/templates/list must advertise the thread template.
TEMPLATES_LIST_RESPONSE=$(grep '"id":11' "$STDOUT" || true)
if [ -z "$TEMPLATES_LIST_RESPONSE" ]; then
    echo "smoke test FAILED: no response to resources/templates/list" >&2
    exit 1
fi
HAS_THREAD_TEMPLATE=$(echo "$TEMPLATES_LIST_RESPONSE" | jq -r \
    '.result.resourceTemplates[] | select(.uriTemplate == "catalog://thread/{thread_id}") | .uriTemplate')
if [ "$HAS_THREAD_TEMPLATE" != "catalog://thread/{thread_id}" ]; then
    echo "smoke test FAILED: resources/templates/list missing catalog://thread/{thread_id}: $TEMPLATES_LIST_RESPONSE" >&2
    exit 1
fi
echo "resources/templates/list OK: catalog://thread/{thread_id} present"

# prompts/list must advertise find_document.
PROMPTS_LIST_RESPONSE=$(grep '"id":12' "$STDOUT" || true)
if [ -z "$PROMPTS_LIST_RESPONSE" ]; then
    echo "smoke test FAILED: no response to prompts/list" >&2
    exit 1
fi
HAS_FIND_DOCUMENT=$(echo "$PROMPTS_LIST_RESPONSE" | jq -r \
    '.result.prompts[] | select(.name == "find_document") | .name')
if [ "$HAS_FIND_DOCUMENT" != "find_document" ]; then
    echo "smoke test FAILED: prompts/list missing find_document: $PROMPTS_LIST_RESPONSE" >&2
    exit 1
fi
echo "prompts/list OK: find_document present"

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
db.upsert_thread('smoke-user', {
    'thread_id': 'smoke-thread-1',
    'message_ids': [{'id': 'm1', 'web_link': '', 'date': '2024-03-01',
                      'has_attachments': False}],
    'subject': 'Smoke test thread',
    'participants': ['smoke-test@example.com', 'sender@example.com'],
    'date_first': '2024-03-01T10:00:00Z',
    'date_last': '2024-03-01T10:00:00Z',
    'has_attachments': 0,
    'attachments': [],
    'web_link': '',
    'ai_tags': ['smoketagvalue'],
    'user_tags': [],
    'manually_reviewed': 0,
    'last_synced': '2024-03-01T10:00:00Z',
    'body_char_count': 12,
    'body_scan_status': 'ok',
    'tags_truncated': 0,
})
"

SYNC_STATUS_CALL='{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"sync_status","arguments":{}}}'
BOGUS_THREAD_CALL='{"jsonrpc":"2.0","id":4,"method":"tools/call","params":{"name":"get_thread","arguments":{"thread_id":"no-such-thread-xyz"}}}'
TAGS_RESOURCE_READ='{"jsonrpc":"2.0","id":5,"method":"resources/read","params":{"uri":"catalog://tags"}}'
BOGUS_THREAD_RESOURCE_READ='{"jsonrpc":"2.0","id":6,"method":"resources/read","params":{"uri":"catalog://thread/no-such-thread-xyz"}}'
COMPLETE_SEEDED_TAG='{"jsonrpc":"2.0","id":7,"method":"completion/complete","params":{"ref":{"type":"ref/prompt","name":"find_document"},"argument":{"name":"tag","value":"smoketag"}}}'
COMPLETE_NO_MATCH_TAG='{"jsonrpc":"2.0","id":8,"method":"completion/complete","params":{"ref":{"type":"ref/prompt","name":"find_document"},"argument":{"name":"tag","value":"zzz"}}}'

: >"$STDOUT"
: >"$STDERR"

# Same stdin-EOF race as Part 1, this time against tools/call.
{ printf '%s\n%s\n%s\n%s\n%s\n%s\n%s\n%s\n' "$INITIALIZE" "$INITIALIZED" \
    "$SYNC_STATUS_CALL" "$BOGUS_THREAD_CALL" "$TAGS_RESOURCE_READ" \
    "$BOGUS_THREAD_RESOURCE_READ" "$COMPLETE_SEEDED_TAG" "$COMPLETE_NO_MATCH_TAG"; sleep 1; } \
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

# ── Part 3: resources/read and completion/complete ──────────────────────────

TAGS_READ_RESPONSE=$(grep '"id":5' "$STDOUT" || true)
if [ -z "$TAGS_READ_RESPONSE" ]; then
    echo "smoke test FAILED: no response to resources/read catalog://tags" >&2
    cat "$STDOUT" >&2
    exit 1
fi
TAGS_READ_PAYLOAD=$(echo "$TAGS_READ_RESPONSE" | jq -r '.result.contents[0].text')
if ! echo "$TAGS_READ_PAYLOAD" | jq -e 'has("tags")' >/dev/null; then
    echo "smoke test FAILED: catalog://tags payload missing tags key: $TAGS_READ_PAYLOAD" >&2
    exit 1
fi
HAS_SMOKETAG=$(echo "$TAGS_READ_PAYLOAD" | jq -r '[.tags[].tag] | index("smoketagvalue") != null')
if [ "$HAS_SMOKETAG" != "true" ]; then
    echo "smoke test FAILED: catalog://tags missing seeded tag: $TAGS_READ_PAYLOAD" >&2
    exit 1
fi
echo "resources/read OK: catalog://tags returned known-shape JSON with the seeded tag"

# Review Focus: an unknown thread id must come back as the same serialized
# {"error": "..."} shape the tool gives, not a traceback.
BOGUS_READ_RESPONSE=$(grep '"id":6' "$STDOUT" || true)
if [ -z "$BOGUS_READ_RESPONSE" ]; then
    echo "smoke test FAILED: no response to resources/read catalog://thread/bogus" >&2
    cat "$STDOUT" >&2
    exit 1
fi
if echo "$BOGUS_READ_RESPONSE" | jq -e 'has("error")' >/dev/null; then
    echo "smoke test FAILED: resources/read catalog://thread/bogus errored at the protocol level (expected a serialized {\"error\":...} payload instead): $BOGUS_READ_RESPONSE" >&2
    exit 1
fi
BOGUS_READ_PAYLOAD=$(echo "$BOGUS_READ_RESPONSE" | jq -r '.result.contents[0].text')
if ! echo "$BOGUS_READ_PAYLOAD" | jq -e 'has("error")' >/dev/null; then
    echo "smoke test FAILED: catalog://thread/bogus payload missing error key: $BOGUS_READ_PAYLOAD" >&2
    exit 1
fi
echo "resources/read OK: catalog://thread/<unknown-id> returned a serialized error, not a traceback"

COMPLETE_SEEDED_RESPONSE=$(grep '"id":7' "$STDOUT" || true)
if [ -z "$COMPLETE_SEEDED_RESPONSE" ]; then
    echo "smoke test FAILED: no response to completion/complete (seeded tag prefix)" >&2
    cat "$STDOUT" >&2
    exit 1
fi
COMPLETE_SEEDED_VALUES=$(echo "$COMPLETE_SEEDED_RESPONSE" | jq -c '.result.completion.values')
if ! echo "$COMPLETE_SEEDED_VALUES" | jq -e 'index("smoketagvalue") != null' >/dev/null; then
    echo "smoke test FAILED: completion/complete did not return the seeded tag: $COMPLETE_SEEDED_RESPONSE" >&2
    exit 1
fi
echo "completion/complete OK: seeded tag prefix returned smoketagvalue"

# Review Focus: a prefix matching no tags must return empty values, not an error.
COMPLETE_NO_MATCH_RESPONSE=$(grep '"id":8' "$STDOUT" || true)
if [ -z "$COMPLETE_NO_MATCH_RESPONSE" ]; then
    echo "smoke test FAILED: no response to completion/complete (no-match prefix)" >&2
    cat "$STDOUT" >&2
    exit 1
fi
if echo "$COMPLETE_NO_MATCH_RESPONSE" | jq -e 'has("error")' >/dev/null; then
    echo "smoke test FAILED: completion/complete with a no-match prefix returned a protocol error instead of empty values: $COMPLETE_NO_MATCH_RESPONSE" >&2
    exit 1
fi
COMPLETE_NO_MATCH_VALUES=$(echo "$COMPLETE_NO_MATCH_RESPONSE" | jq -c '.result.completion.values')
if [ "$COMPLETE_NO_MATCH_VALUES" != "[]" ]; then
    echo "smoke test FAILED: completion/complete with prefix 'zzz' should return empty values: $COMPLETE_NO_MATCH_RESPONSE" >&2
    exit 1
fi
echo "completion/complete OK: prefix with no matching tags returned empty values"

# ── Part 4: resources degrade like tools on a resolve_user() ValueError ─────
# Two users, no CATALOG_USER set: resolve_user() raises "multiple users; set
# CATALOG_USER" (a ValueError). The tools/call layer already turns that into
# a clean {"error": "..."} via _tolerate_errors; the resource surface must
# do the same via its own _tolerate_errors_json, not propagate it into a
# protocol-level INTERNAL_ERROR.
unset CATALOG_USER
DB_PATH_AMBIGUOUS="$SCRATCH/catalog-ambiguous.db"
export DB_PATH="$DB_PATH_AMBIGUOUS"

"$SETUP_PYTHON" -c "
import db
db.init_db()
db.upsert_user('smoke-user-1', 'one@example.com', 'One', '2024-01-01T00:00:00Z')
db.upsert_user('smoke-user-2', 'two@example.com', 'Two', '2024-01-01T00:00:00Z')
"

AMBIGUOUS_TAGS_READ='{"jsonrpc":"2.0","id":9,"method":"resources/read","params":{"uri":"catalog://tags"}}'
# Completions degrade the same way: tag_names() calls resolve_user() too, so
# the same ambiguous-user ValueError must come back as an empty values list,
# never a protocol-level error (there is no error slot in a completion).
AMBIGUOUS_COMPLETE='{"jsonrpc":"2.0","id":17,"method":"completion/complete","params":{"ref":{"type":"ref/prompt","name":"find_document"},"argument":{"name":"tag","value":"any"}}}'

: >"$STDOUT"
: >"$STDERR"

{ printf '%s\n%s\n%s\n%s\n' "$INITIALIZE" "$INITIALIZED" "$AMBIGUOUS_TAGS_READ" \
    "$AMBIGUOUS_COMPLETE"; sleep 1; } \
    | "$PYTHON" mcp_server.py >"$STDOUT" 2>"$STDERR"

if [ -s "$STDERR" ]; then
    echo "--- stderr (ambiguous-user resource read) ---" >&2
    cat "$STDERR" >&2
fi

AMBIGUOUS_RESPONSE=$(grep '"id":9' "$STDOUT" || true)
if [ -z "$AMBIGUOUS_RESPONSE" ]; then
    echo "smoke test FAILED: no response to resources/read catalog://tags (ambiguous user)" >&2
    cat "$STDOUT" >&2
    exit 1
fi
if echo "$AMBIGUOUS_RESPONSE" | jq -e 'has("error")' >/dev/null; then
    echo "smoke test FAILED: resources/read catalog://tags errored at the protocol level on an ambiguous user instead of degrading like the tool layer: $AMBIGUOUS_RESPONSE" >&2
    exit 1
fi
AMBIGUOUS_PAYLOAD=$(echo "$AMBIGUOUS_RESPONSE" | jq -r '.result.contents[0].text')
if ! echo "$AMBIGUOUS_PAYLOAD" | jq -e 'has("error")' >/dev/null; then
    echo "smoke test FAILED: catalog://tags payload missing error key for ambiguous user: $AMBIGUOUS_PAYLOAD" >&2
    exit 1
fi
echo "resources/read OK: catalog://tags degrades to a serialized error (not a protocol-level failure) on resolve_user()'s multi-user ValueError"

AMBIGUOUS_COMPLETE_RESPONSE=$(grep '"id":17' "$STDOUT" || true)
if [ -z "$AMBIGUOUS_COMPLETE_RESPONSE" ]; then
    echo "smoke test FAILED: no response to completion/complete (ambiguous user)" >&2
    cat "$STDOUT" >&2
    exit 1
fi
if echo "$AMBIGUOUS_COMPLETE_RESPONSE" | jq -e 'has("error")' >/dev/null; then
    echo "smoke test FAILED: completion/complete errored at the protocol level on an ambiguous user instead of degrading to empty values: $AMBIGUOUS_COMPLETE_RESPONSE" >&2
    exit 1
fi
AMBIGUOUS_COMPLETE_VALUES=$(echo "$AMBIGUOUS_COMPLETE_RESPONSE" | jq -c '.result.completion.values')
if [ "$AMBIGUOUS_COMPLETE_VALUES" != "[]" ]; then
    echo "smoke test FAILED: completion/complete on an ambiguous user should return empty values, got: $AMBIGUOUS_COMPLETE_RESPONSE" >&2
    exit 1
fi
echo "completion/complete OK: ambiguous user degrades to empty values (not a protocol-level error)"

echo "smoke test PASSED"
