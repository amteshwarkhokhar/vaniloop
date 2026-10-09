# AGENTS.md — vaniloop

Instructions for any coding agent (Claude Code, Codex, others) in this repo.
Read this file first. Then read the issue you were given. The issue is your work order.

## What this repo is

The IndiaMART hackathon entry for PS1, "Quality Audit & Fix Loop" (9–10 Oct 2026).
VANI is the buyer-side voice bot. It calls a buyer, confirms the requirement and posts a
Buy Lead. vaniloop grades VANI calls, finds the causes of bad calls, proposes prompt
fixes and proves the fixes with replays.

| Stage | Module | Status |
|---|---|---|
| Ingest: transcript + audio → `CallRecord` | `src/vaniloop/ingest.py` | built |
| Audit: LLM labels turns, Python rules grade | `ledger.py`, `rules.py` | built, never run on a live LLM |
| Issue: group faults into ranked causes | `issue.py` | built |
| Fix: prompt diffs against the real VANI prompt | — | not started |
| Test: fork-the-call replay, noise floor, guards | — | not started |
| Dashboard | `dashboard.py` (Streamlit) | Audit + Issues views |

Data contracts for every stage: `src/vaniloop/models.py`. Change them only in an issue that says so.

## Rules that protect call data

1. **Call data never enters git.** `transcripts/`, `runs/`, `data/raw/`, `.cache/` and
   `data/reference/vani_prompt_sarvam.md` are gitignored. Tests that need real calls
   skip when the files are absent. Fixtures in `tests/fixtures/` are synthetic.
2. **No real buyer names, phone numbers or quotes longer than one line** in code, tests,
   docs, issues or PR text. Use placeholders such as `BuyerA`. `bin/check` scans for phone numbers.
3. **Which models may read call data:** Claude, OpenAI/Codex and Google Gemini only.
   GLM, DeepSeek and other providers work on code only, in a checkout with no data.
   Sarvam and TypeSafe get synthetic data only until the owner approves.
4. **Secrets live in `.env`** (`LLM_BASE_URL`, `LLM_API_KEY`, `LLM_MODEL`). Never print,
   log or commit a value.

## Audit design rules (keep these when you change `rules.py`)

- The LLM only labels. Python rules grade. Every finding has a basis:
  `observed`, `spoken_proxy` or `unavailable`.
- Missing evidence gives `NeedsReview`, never `Pass`.
- Grade against the real VANI prompt. The prompt map is `ideation/06_vani_prompt_map.md`
  (line refs such as L69 point into the gitignored prompt copy).
- Every rule change ships with a test that fails without it.

## How work flows

1. Each task is one GitHub issue in the work-order format
   (`.github/ISSUE_TEMPLATE/work-order.md`): goal, allowed paths, forbidden paths,
   done-when checks, verify command, stop rule.
2. One branch per issue: `<issue-number>-<slug>`. Implementers are Sonnet subagents.
3. Run `bin/check` before you commit. CI runs the same script.
4. Open a PR that says `Closes #<n>`. Codex reviews (at most 3 rounds). The owner merges.
5. Update the issue checklist and `docs/DECISIONS.md` when a decision changes.

## Commands

```bash
uv sync                                               # once
bin/check                                             # tests + data-leak scan; must pass
uv run pytest -q                                      # tests only
uv run python -m vaniloop.ingest                      # transcripts → runs/calls.jsonl
uv run python -m vaniloop.pipeline audit --limit 20   # audit (needs LLM key in .env)
uv run python -m vaniloop.pipeline issues             # runs/audits.jsonl → runs/causes.json
uv run streamlit run src/vaniloop/dashboard.py        # dashboard (VANILOOP_DATA=dir to override)
```

## Where to look

- Decisions and why: `docs/DECISIONS.md`
- Research and the plan: `ideation/` (`ps1-plan.html` is the visual plan)
- The real prompt, mapped with line refs: `ideation/06_vani_prompt_map.md`

## Working style

- Hackathon pace: build the smallest thing that proves the stage, then iterate.
- Explain with one concrete call, in plain words.
