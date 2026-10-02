#!/usr/bin/env bash
# Smoke test for mcp_server.py: speaks real JSON-RPC over stdio (no agent
# involved) and checks that all four catalog tools show up in tools/list.
#
# Usage: scripts/mcp_smoke.sh   (run from the repo root, or anywhere — it
# cd's to its own repo root first)
set -euo pipefail

cd "$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

PYTHON=.venv-mcp/bin/python

if [ ! -x "$PYTHON" ]; then
    echo "missing $PYTHON — create .venv-mcp first (see task-4 brief step 1)" >&2
    exit 1
fi

INITIALIZE='{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"smoke-test","version":"0"}}}'
INITIALIZED='{"jsonrpc":"2.0","method":"notifications/initialized"}'
TOOLS_LIST='{"jsonrpc":"2.0","id":2,"method":"tools/list"}'

OUTPUT=$(printf '%s\n%s\n%s\n' "$INITIALIZE" "$INITIALIZED" "$TOOLS_LIST" | "$PYTHON" mcp_server.py 2>&1)

echo "$OUTPUT"

MISSING=0
for TOOL in search_catalog get_thread list_tags sync_status; do
    if ! grep -q "$TOOL" <<< "$OUTPUT"; then
        echo "MISSING TOOL: $TOOL" >&2
        MISSING=1
    fi
done

if [ "$MISSING" -ne 0 ]; then
    echo "smoke test FAILED" >&2
    exit 1
fi

echo "smoke test PASSED: all four tools present"
