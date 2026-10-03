# Catalog MCP — Spec-Depth Milestone

Make catalog-mcp demonstrate the MCP spec's underused surfaces properly:
structured output, resources, prompts, completions, progress — plus a
README that leads with the agent story and the measured numbers. Grounded
in Anthropic's "Writing effective tools for agents" and the MCP spec's
tools/resources/prompts pages.

## Constraints carried over

Server stays read-only, four tools, stdio. Tool logic stays in
`mcp_tools.py` (3.9, tested by the main suite); `mcp_server.py` stays the
thin 3.12 wrapper. No new tools — the reading is emphatic that tool count
hurts; depth goes into the spec surfaces instead. Installed SDK is `mcp`
v2 (`MCPServer`, `Context`); verified available: `tool(structured_output=)`,
`resource(uri)` with `{param}` templates, `prompt()`, `completion()`,
`Context.report_progress`.

## What gets added

1. **Structured output** on all four tools: `structured_output=True` with
   typed returns (`TypedDict`s declared in mcp_server.py mirroring the
   mcp_tools dict shapes) so tools/list carries a real outputSchema.
   Because every tool can also return `{"error": ...}` or `{"notice": ...}`,
   the TypedDicts use `total=False` with those keys included.
2. **Resources**: `catalog://thread/{thread_id}` resource template —
   read returns the same payload as the `get_thread` tool (one shared code
   path), letting a host attach a thread as context without a tool call.
   Plus a static `catalog://tags` resource (the tag vocabulary, JSON).
3. **Prompt**: `find_document(description, tag="")` — a user-invocable
   template that instructs the model to search the catalog (tags first,
   then `get_thread` on finalists) for a half-remembered document.
4. **Completions**: `@mcp.completion()` handler completing the prompt's
   `tag` argument (and the resource template's `thread_id` is NOT
   completed — ids are opaque) from the tag vocabulary via
   `mcp_tools.list_tags(prefix=...)`. Spec scopes completions to prompt
   args and resource-template params; this is the prompt-arg case.
5. **Progress**: `get_thread` tool accepts `Context` and emits
   `report_progress` before/after the live provider fetch (the one slow
   call).
6. **README overhaul**: lead with the MCP server story; add a worked
   transcript (real search_catalog→get_thread exchange), the benchmark
   table (7/8 vs 4/8, 14.8s vs 21.8s mean, ~$0.08/question), and the
   indexing-cost paragraph (first import: hours, Gmail-quota-bound, ~$1-2
   tagging; steady-state sync: ~6 min measured; recall afterwards: ~15s).
   Keep the web-app section, demoted below.
7. **Smoke test** extended: resources/list, prompts/list, one
   resources/read of `catalog://tags`, one completion/complete call.

## Non-goals

Sampling, elicitation, HTTP transports, MCPB packaging, more tools, auth
changes, multi-user. Benchmark harness stays as-is.

## Acceptance

3.9 suite green; smoke test (extended) passes end-to-end over real stdio;
`tools/list` shows outputSchema on all four tools; completion returns tag
names for a prefix.
