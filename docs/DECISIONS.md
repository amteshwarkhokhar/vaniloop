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
| 2026-10-09 | Ship gate = exact one-sided stratified permutation test (conditional CMH / stratified Fisher), α 0.05, K old + K new replays per call; demo design n=5, K=3. Replaces the old-only noise floor. | Codex showed the old-only floor ships a non-fix ~14% of the time. Fable: exact test is deterministic; n=5,K=3 gives 94% power for 0.8→0.2 and ~0.2% false ships. |
| 2026-10-09 | Guard Fatal is re-checked once under NEW and OLD; it rejects only if NEW fails again and OLD does not. A NEW Fatal on any parameter of a target call rejects. | One guard Fatal alone gives ~40% false rejects at a 5% background Fatal rate over 10 guards. |
| 2026-10-09 | Transcribe our own audio with Gemini (src/vaniloop/transcribe.py) and keep per-turn tone, overlap, unclear audio and bot-misheard. | Pre-built pipelines are disqualified. Audio signals fix the missed irritation on call 282. ~$0.003 per call. |
| 2026-10-09 | `category_isq` splits question level from option level: `question_fits_product` False is Fatal, `fits_confirmed_product` False under a fitting question is NonFatal, and an old ledger without the new label is Unknown. | The human rubric grades a wrong spec question F and a wrong option NF. A second check of the 5 Fatals agreed with 2 and would downgrade 3 (right question, wrong options). |
| 2026-10-09 | Fix patches the clean main prompt: `prompt.load()` removes U+FEFF and Markdown escapes, keeps every line number and cuts to lines 1-1722. | The model copies visible text, so raw anchors missed. Lines 1723+ are variants that 100 of 103 audited calls never use. |
| 2026-10-09 | Proof replays use the Gemini-played bot on the clean main prompt (`replay.py`), not the Sarvam replica. | The replica cannot take per-call variables and its counters stay frozen (`ideation/07_sarvam_replica.md`). |
| 2026-10-09 | Replays are as noisy as the forked call: `unclear_rate` is the share of post-fork user turns flagged `audio_unclear` or followed by a `bot_misheard` bot turn (cap 0.8). Each generated buyer turn is unclear with that chance, seeded by `seed:call_id:idx`. The bot sees `[unclear audio]`, the record keeps the real text. | The first gate run (cause_looping) rejected the patch with lift 0: the old prompt looped in 0 of 15 text replays because every buyer line arrived clean. Real loops start when the bot does not hear the buyer (12 of 20 looping calls carry such a flag near the onset). |
