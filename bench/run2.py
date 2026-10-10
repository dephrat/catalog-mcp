"""Stratified benchmark v2: precise vs vague questions, repeated runs.

24 questions labeled by specificity, 2 conditions, REPEATS runs each
(agent runs are nondeterministic; one run per cell overstates certainty).
Reuses run.py's conditions and runner; results land in results2.jsonl.

    python3 run2.py            # full grid (24 x 2 x 3 = 144 runs)
    python3 run2.py p01_bin    # one question, both conditions, all repeats
"""
import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor

import run as v1

BENCH = os.path.dirname(os.path.abspath(__file__))
REPEATS = 3
WORKERS = 3


def main():
    questions = json.load(open(os.path.join(BENCH, "questions2.json")))
    only = sys.argv[1] if len(sys.argv) > 1 else None
    jobs = [(q, cond, r)
            for q in questions if not only or q["id"] == only
            for cond in ("catalog", "gmail")
            for r in range(REPEATS)]
    out_path = os.path.join(BENCH, "results2.jsonl")

    def one(job):
        q, cond, r = job
        try:
            rec = v1.run_one(q, cond)
        except Exception as e:
            rec = {"qid": q["id"], "condition": cond, "wall_s": None,
                   "hit": False, "answer": f"(runner error: {e})"}
        rec["type"] = q["type"]
        rec["repeat"] = r
        return rec

    with open(out_path, "a") as out:
        with ThreadPoolExecutor(max_workers=WORKERS) as pool:
            for rec in pool.map(one, jobs):
                out.write(json.dumps(rec) + "\n")
                out.flush()
                print(f"{rec['qid']:16s} {rec['condition']:7s} r{rec['repeat']} "
                      f"{str(rec['wall_s']):>6}s hit={rec['hit']}", flush=True)
    print(f"done -> {out_path}")


if __name__ == "__main__":
    main()
