# Decisions

One line per decision. Newest last. Change a line only with a new line that replaces it.

| Date | Decision | Why |
|---|---|---|
| 2026-10-08 | Audit = extract-then-rule: one LLM call labels each turn, Python rules grade | Verdicts stay transparent and testable. The LLM never grades. |
| 2026-10-08 | Missing evidence gives NeedsReview, not Pass | A wrong Pass hides a Fatal. |
| 2026-10-08 | Cause rank = Wilson low bound × mean severity × fixability. A cause needs 3 calls to rank. | Small counts are noise. |
| 2026-10-08 | Test gate is noise-calibrated: replay old bot vs itself, the flip rate is the floor | A fix must beat run-to-run noise, not zero. (Idea from AgentCI, rebuilt.) |
| 2026-10-08 | Call data goes to Claude, OpenAI/Codex and Gemini only. Sarvam and TypeSafe get synthetic data until approved. | Hackathon rule: customer data stays on premises. |
| 2026-10-09 | Ledger model: Gemini 3 Flash via the IndiaMART LiteLLM proxy | Allowed provider, cheap, fast. |
| 2026-10-09 | Baseline prompt = the real Sarvam VANI prompt (gitignored copy, mapped in `ideation/06_vani_prompt_map.md`) | Replaces the inferred prompt. Fix diffs apply to real text. |
| 2026-10-09 | Loop limits are per slot, from prompt L69 (product 4, quantity 3, spec 2, name 3, city 3) | The prompt sets them. One flat limit of 3 was wrong. |
| 2026-10-09 | Work runs as GitHub issues in work-order format. Sonnet implements, Codex reviews, `bin/check` gates. | Parallel agents without collisions. |
| 2026-10-09 | Sarvam may receive the real VANI prompt and real call text (IndiaMART's Sarvam workspace). Replays run on a draft VANI replica agent there. | Owner approved. Production VANI already runs on Sarvam. Replays come with per-turn traces and use Sarvam credits. |
| 2026-10-09 | `qty_probing` (bot never asks quantity) is NonFatal, not Fatal. | IndiaMART's human audit grades Qty Probe mostly Non-Fatal and fails it only when quantity is defined on the category, which a transcript cannot show. As Fatal it made 41% of calls Fatal against a 4.7% Sarvam human rate (VANI Call Vital report, 23 Aug-26 Sep 2026). |
| 2026-10-09 | `qty_missed` is Fatal only when the buyer never repeats the quantity after a re-ask. A re-ask the buyer answers is NonFatal. | All 7 Fatal cases on 103 calls were one re-ask after a mishear, then the right value. Official Fatal = wrong or missing buyer info in the lead. |
| 2026-10-09 | Calibration re-scores saved ledgers with `pipeline regrade` (no LLM call). The pre-calibration audit is kept as `runs/audits_v1_before_calibration.jsonl`. | Rule changes are free to test. Hand-grade agreement can be reported before and after. |
