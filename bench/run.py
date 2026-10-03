"""Benchmark runner: catalog MCP vs raw Gmail, same model, same harness.

Runs each question through `claude -p` twice — once with only the catalog
server, once with only the raw-Gmail server — and records wall time, turns,
cost and whether the answer contains the expected fact. Results land in
bench/results.jsonl; summarize with bench/summarize.py.
"""
import json
import os
import subprocess
import sys
import time

BENCH = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(BENCH)
VENV_PY = os.path.join(REPO, ".venv-mcp", "bin", "python")

CONDITIONS = {
    "catalog": {
        "mcpServers": {"catalog": {"command": VENV_PY,
                                   "args": [os.path.join(REPO, "mcp_server.py")]}},
        "allowed": "mcp__catalog__search_catalog,mcp__catalog__get_thread,"
                   "mcp__catalog__list_tags,mcp__catalog__sync_status",
    },
    "gmail": {
        "mcpServers": {"gmailraw": {"command": VENV_PY,
                                    "args": [os.path.join(BENCH, "gmail_server.py")]}},
        "allowed": "mcp__gmailraw__search_messages,mcp__gmailraw__get_message",
    },
}

PROMPT = ("Answer the question below using ONLY the email tools available to you. "
          "Be specific; include exact numbers/dates/names when the mail contains them, "
          "and name the email (subject + date) you based the answer on.\n\nQuestion: {q}")

DISALLOWED = "Bash,Read,Write,Edit,Glob,Grep,WebSearch,WebFetch,Task,NotebookEdit"


def run_one(question, condition):
    cfg = CONDITIONS[condition]
    cfg_path = os.path.join(BENCH, f"mcp-{condition}.json")
    with open(cfg_path, "w") as f:
        json.dump({"mcpServers": cfg["mcpServers"]}, f)
    cmd = [
        "claude", "-p", PROMPT.format(q=question["question"]),
        "--model", "sonnet",
        "--output-format", "json",
        "--mcp-config", cfg_path,
        "--strict-mcp-config",
        "--allowedTools", cfg["allowed"],
        "--disallowedTools", DISALLOWED,
        "--max-turns", "25",
    ]
    t0 = time.monotonic()
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=600,
                          cwd=BENCH)  # neutral cwd: no project context
    wall = time.monotonic() - t0
    try:
        data = json.loads(proc.stdout)
    except ValueError:
        data = {"result": proc.stdout, "parse_error": True, "stderr": proc.stderr[-2000:]}
    answer = data.get("result") or ""
    hit = any(s.lower() in answer.lower() for s in question["expect_any"])
    return {
        "qid": question["id"], "condition": condition,
        "wall_s": round(wall, 1),
        "num_turns": data.get("num_turns"),
        "cost_usd": data.get("total_cost_usd"),
        "duration_api_ms": data.get("duration_api_ms"),
        "hit": hit,
        "answer": answer[:1500],
    }


def main():
    questions = json.load(open(os.path.join(BENCH, "questions.json")))
    only = sys.argv[1] if len(sys.argv) > 1 else None
    out_path = os.path.join(BENCH, "results.jsonl")
    with open(out_path, "a") as out:
        for q in questions:
            if only and q["id"] != only:
                continue
            for cond in ("catalog", "gmail"):
                print(f"{q['id']} / {cond} ...", flush=True)
                try:
                    rec = run_one(q, cond)
                except subprocess.TimeoutExpired:
                    rec = {"qid": q["id"], "condition": cond, "wall_s": 600,
                           "hit": False, "answer": "(timeout)"}
                out.write(json.dumps(rec) + "\n")
                out.flush()
                print(f"  {rec['wall_s']}s, turns={rec.get('num_turns')}, "
                      f"hit={rec['hit']}", flush=True)
    print(f"done -> {out_path}")


if __name__ == "__main__":
    main()
