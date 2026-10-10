# Evaluation notes

How the benchmark evolved, what each version got wrong, and what the
current numbers do and do not support. The harness is `run.py`/`run2.py`,
question sets `questions*.json`, raw results `results*.jsonl` (gitignored:
they contain real mailbox contents).

## v1 — 8 questions, single run, substring grading

Two conditions, same model (Sonnet), same headless harness, same
credentials; only the toolset differs (catalog MCP vs a raw-Gmail MCP
control built for the comparison). Result: catalog 7/8, control 4/8,
14.8s vs 21.8s mean.

What v1 got wrong, in the order it got it wrong:

1. **Its first full grid was garbage that looked like data.** All 16 runs
   "completed" in ~1s — the CLI was silently unauthenticated and login
   errors were scored as misses. Fix: validity checks before trusting any
   number.
2. **The grader convicted a correct answer.** On "when did I cancel
   Metergy?" the control read the actual emails and answered more
   precisely than the metadata-derived expected date. Fix: auto-grading
   demoted to a screen; hand adjudication is the verdict; disagreements
   published ("4/8, 3 partial") instead of hidden.
3. **n=8 with no repeats overstated certainty** — see v2.

## v2 — 24 questions stratified precise/vague, 3 repeats per cell

144 runs (~$9). Motivation: v1 suggested the advantage concentrated in
vague queries; testing only where you win is selection bias, so the set
was split 12 precise (subject keywords recoverable) / 12 vague
(circumstantial recall only) and every cell repeated 3× because agent
runs are nondeterministic.

What v2 got wrong before it said anything useful:

4. **It scored API-billing failures as wrong answers.** The account ran
   out of credit 110 runs in; 34 "Credit balance is too low" transcripts
   graded as misses. Fix: the runner now marks error-shaped transcripts
   invalid instead of wrong (`run.py`, `invalid` field), and those cells
   were re-run.
5. **Its ground truth went stale while it was being written.** Several
   "most recent X" questions were keyed to an Oct 3 snapshot; the live
   mailbox moved on (newer Spotify and render.com purchases, a newer
   e-transfer, a newer maintenance request), so both systems were marked
   wrong for being *more* current than the answer key. One question's
   `expect_any` also missed a third valid answer (a settlement-offer
   thread the key didn't list). Lesson: "most recent" is unstable ground
   truth by construction; pin questions to immutable facts.

## Current numbers (v2, run-level, before adjudication)

| condition | precise acc | vague acc | precise time | vague time |
|---|---|---|---|---|
| catalog   | 89% (32/36) | 86% (31/36) | 17.6s | 21.8s |
| raw Gmail | 78% (28/36) | 72% (26/36) | 15.2s | 39.1s |

Hand adjudication (notes above) moves several of BOTH sides' remaining
misses into "answer key's fault," compressing the accuracy gap further.

## What survives scrutiny

- **v1's "nearly double the accuracy" does not.** At n=24×3 the accuracy
  edge is real but modest (~+11-14 points run-level, less after
  adjudication).
- **The vague-recall speed gap does:** 21.8s vs 39.1s mean — the control
  burns rounds flailing through keyword search space that the tag index
  collapses into one query.
- **Consistency does:** the control's vague failures are 0/3 flails on
  the same questions every time; catalog's worst cells are 1-2/3, and it
  never scored 0/3 on a precise question.
- **A real catalog limitation surfaced:** the index answers confidently
  from its last sync; raw Gmail sees this morning's mail. Freshness is a
  genuine trade-off, now with evidence (the maintenance-request
  question), mitigated but not removed by scheduled syncs.

Limits that still apply: one model, one mailbox, questions authored by
the person who built the index.
