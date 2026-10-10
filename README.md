# vaniloop

Audit, root-cause, fix and prove loop for VANI, IndiaMART's buyer-side voice bot.
Built for the IndiaMART Voice AI Hackathon 2.0 (9–10 Oct 2026), problem statement 1,
"Quality Audit & Fix Loop".

VANI calls a buyer, confirms the requirement and posts a Buy Lead. vaniloop reads the
call recordings, grades every call Fatal / Non-Fatal / Pass with evidence, groups the
faults into ranked root causes, writes a prompt patch for each prompt-side cause, and
proves or refuses each patch with replays behind a statistical ship gate. One local
console shows the whole loop.

## The loop

| Stage | What it does | Module |
|---|---|---|
| Ingest | mp3 → Gemini transcript with per-turn tone, overlap and unclear-audio flags → `CallRecord` | `transcribe.py`, `ingest.py` |
| Audit | An LLM labels each turn (slot, value, declined, misheard). Python rules grade 9 rubric parameters. Jev (TypeSafe) makes the four judgement labels. | `ledger.py`, `jev.py`, `rules.py`, `grading.py` |
| Issue | Root-vs-symptom per call, audio-born faults split off as `channel` causes, patterns ranked by frequency × severity with Wilson bounds | `issue.py`, `categorize.py`, `rootcause.py` |
| Fix | A prompt patch (diff against the real VANI prompt) for prompt-owned causes, an evidence ticket for tool- and data-owned causes, watch for the rest | `fix.py` |
| Test | Fork-the-call replays: a simulated buyer seeded from the real call, old prompt vs new prompt, K seeds per call, exact stratified permutation test, guards on calls that passed | `replay.py`, `render.py`, `prove.py`, `gate.py` |
| Ship | Prompt versions with lineage, a release check of all shipped patches together, an append-only issue tracker | `pipeline.py ship`, `tracker.py` |
| Console | Home (one Run-the-loop button), Audit, Issues, Fix, Test, Tracker, Rubric editor, call pages with transcripts | `web.py`, `console.py`, `web/` |

Data contracts for every stage live in `src/vaniloop/models.py`.

### Audit design rules

- The LLM only labels. Python rules grade. Every finding carries a basis:
  `observed`, `spoken_proxy` or `unavailable`.
- Missing evidence gives `NeedsReview`, never `Pass`.
- Grading follows the official VANI quality matrix (Fatal = wrong or missing buyer info
  in the lead). The real VANI prompt is the baseline for every patch.
- Every rule change ships with a test that fails without it.

### Why the gate is strict

A patch ships only when the new prompt fails fewer replays than the old one on calls
that the old prompt reproduces, with a one-sided exact test at α 0.025 (two looks per
cause, Bonferroni), no new Fatal on a guard call, and no unresolved trial. A patch that
cannot be proven is refused, and the refusal is shown with the paired replays. On the
hackathon data no patch cleared the gate. That is the result, not a gap: the gate kept
four unproven edits out of a production prompt.

## Quick start

```bash
uv sync                                               # once; Python 3.12+
cp .env.example .env                                  # fill LLM_* and TYPESAFE_API_KEY
bin/check                                             # tests + data-leak scan

uv run python -m vaniloop.ingest                      # transcripts → runs/calls.jsonl
uv run python -m vaniloop.pipeline audit --limit 20   # Gemini labels + rule grades
uv run python -m vaniloop.pipeline judge              # Jev judgement labels
uv run python -m vaniloop.pipeline regrade --ledgers runs/ledgers_jev.jsonl --audits runs/audits_jev.jsonl
uv run python -m vaniloop.pipeline issues             # ranked root causes → runs/causes.json
uv run python -m vaniloop.pipeline fix --prompt data/reference/vani_prompt_sarvam.md --top 3
uv run python -m vaniloop.pipeline prove --cause cause_qty_probing --n 8 --k 3 --holdout --screen
uv run python -m vaniloop.pipeline loop --prompt ... --top 3   # all of the above, ships nothing

uv run python -m vaniloop.web                         # console at http://127.0.0.1:8765
```

`VANILOOP_DATA=<dir>` points the console and the pipeline at another data folder.

## Outputs

Every stage writes one file in `runs/` (gitignored, because it holds call data):

| File | Contents |
|---|---|
| `calls.jsonl` | one `CallRecord` per call: turns, timestamps, audio flags |
| `ledgers.jsonl`, `ledgers_jev.jsonl` | per-turn labels (Gemini structure, Jev judgements) |
| `audits.jsonl` | per-call verdict, 9 findings with reason, evidence turn and basis, risk flags |
| `causes.json` | ranked causes with type, owner, member calls, symptoms, priority |
| `patches.json` | prompt patches with anchor lines, rationale, attempt history |
| `tickets/*.md` | evidence tickets for tool- and data-owned causes |
| `replays.jsonl`, `gates.jsonl` | every replay and every gate decision with p, lift, cost |
| `tracker.jsonl` | the issue tracker: detected → routed → gated → shipped / regressed |

Synthetic samples of each file, safe to read, are in `tests/fixtures/`. The console
can run on them: `VANILOOP_DATA=tests/fixtures/web_runs uv run python -m vaniloop.web`.

## Rules that protect call data

- Call data never enters git: `transcripts/`, `runs/`, `data/raw/`, `.cache/` and the
  prompt copy are gitignored. `bin/check` fails on tracked call data or phone numbers.
- No real buyer names or multi-line quotes in code, tests, docs or issues.
- Secrets live in `.env` only.

## Where to look

- Why each decision was made: `docs/DECISIONS.md`
- Research and the plan per stage: `ideation/`
- Console API: `docs/console_api.md`; UI design: `docs/ui_design.md`
- Submission text and numbers: `docs/SUBMISSION.md`

## Tests

```bash
uv run pytest -q        # 974 tests, synthetic fixtures only
```
