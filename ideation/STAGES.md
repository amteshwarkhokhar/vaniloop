# Stage scopes (read with BRIEF.md)

## 01 AUDIT
Auto-grade every call Pass / Fatal / Non-Fatal with reason + evidence, per rubric parameter; risky-call flags (wrong requirement, frustration, loops, low confidence, human request). Think about: mapping the 17-parameter rubric to LLM judging (one judge vs per-parameter vs extract-then-rule); inferring the "captured BL" from the bot's spoken confirmation; deterministic signals (ffmpeg silencedetect dead air, repeated questions, "Hello?" probes, <10 s calls); agreement with humans (fast hand-labelling, kappa, per-parameter agreement); confidence + abstain-to-human; cost for 713 calls. Output contract: per-call audit JSON consumed by ISSUE.

## 02 ISSUE (root causes + ranking)
Turn per-call failures into a short ranked list of root causes. Think about: clustering approaches (LLM-proposed taxonomy then assignment, embeddings + HDBSCAN, hierarchical, open coding then merge); separating prompt vs ASR vs knowledge-gap vs flow-logic causes; root cause vs symptom (e.g. "drone ISQs on camera" symptom of "stale category after product change"); priority score = frequency x severity x fixability, transparent; stability of clusters across runs; example-call linking. Output contract: root-cause JSON consumed by FIX.

## 03 FIX (prompt fix generation)
For each top root cause, produce a specific, reviewable prompt diff. Think about: we do not have the VANI prompt — reconstruct/infer a baseline prompt from transcripts, or ask organisers; diff-style minimal edits vs rewrite; guarding against regressions (fix A breaks B); generating multiple candidate fixes and selecting by test; non-prompt fixes (ASR hotwords/vocab, flow config, knowledge base) when the cause is not prompt; human review step. Output contract: fix JSON consumed by TEST.

## 04 TEST (prove the fix)
Prove failure rate drops. Think about: replay design — simulated buyer (LLM persona seeded from the real failed call's user turns) talks to bot-with-old-prompt vs bot-with-new-prompt; text-only vs full voice (Sarvam STT/TTS, Sarvam Voice Agents) loop; re-grading with the same AUDIT judge; held-out set and regression suite (calls that passed must still pass); statistical honesty (n, CI); measuring time issue→tested fix; making the demo audible (voice before/after). Output contract: test-result JSON consumed by the dashboard.
