# Submission sheet: Voice AI Hackathon 2.0, PS1

Every form field, ready to paste. Numbers come from the demo freeze
(`.cache/demo_freeze/FREEZE.md`, 10 Oct 2026 13:05 IST, 712 calls) and `runs/gates.jsonl`.
Fields marked OWNER need a value only the owner has.

---

## 1. Project title

vaniloop: the VANI call audit, root-cause, fix and prove loop

## 2. Short pitch (one sentence, 20 to 280 characters)

vaniloop grades every VANI call Fatal or Non-Fatal with evidence, ranks the root causes, writes a prompt patch for each prompt-side cause, and proves or refuses every patch with fork-the-call replays behind a statistical ship gate, in one console.

(252 characters)

## 3. Problem statement

Quality Audit & Fix Loop

## 4. Approach note (Markdown)

**One concrete call.** A buyer asked for a school-bus CCTV camera, then changed to a home dome camera. VANI kept asking drone spec questions (brand, 5.1K video, flight time), asked the buyer's name four times, and the buyer got irritated. A human auditor marks this call Fatal. vaniloop marks it Fatal too, with the turn, the reason and the owner of each fault: the spec questions are a data fault (wrong ISQ list), the name loop is a prompt fault.

**The loop, stage by stage.**

1. **Ingest.** Gemini transcribes each mp3 and flags per turn: tone, overlap, unclear audio, bot misheard. These flags later separate bad audio from bad prompting.
2. **Audit.** The LLM only labels (slot asked, value given, declined, misheard). Python rules grade 9 parameters of the official VANI quality matrix: intent, category ISQ, product change, quantity probing, quantity missed, quantity accuracy, unit, looping, dead air. Four judgement labels (does this spec question fit the product, do the options fit, is this the same product, is the buyer irritated) come from Jev (TypeSafe), tuned on a 756-item reviewed set. Every finding has a basis: `observed`, `spoken_proxy` or `unavailable`. Missing evidence gives `NeedsReview`, never `Pass`. Risk flags: wrong requirement, frustration, loops, low confidence, human request, dead-air review.
3. **Issue.** In each call the earliest failing finding is the root, later ones are symptoms. A root whose onset sits on unclear audio becomes a `channel` cause (speech owner), never a prompt patch. A cause is a pattern only with 3+ calls across 2+ product categories. Priority = frequency × severity with Wilson bounds, shown in the open.
4. **Fix.** Each cause gets one route: a prompt patch (a diff against the real VANI prompt, with anchor lines and rationale), an evidence ticket when the suspected owner is a tool or the ISQ data, or watch. Patches are written from the fix half of the calls only.
5. **Test.** Fork-the-call replay: the real call is copied verbatim up to the fault, then a simulated buyer seeded from that call continues against the old prompt and the new prompt, K seeds each, on hold-out calls only, after a screen that keeps only calls where the old prompt reproduces the fault. The gate is a one-sided exact stratified permutation test at α 0.025 (two attempts per cause, Bonferroni), plus guards on calls that passed and a rule that an unresolved trial can never count as a pass. A second attempt (`refix`) learns from the replays that still failed.
6. **Ship.** Prompt versions with lineage, an overlap guard between patches, a release check of all shipped patches together, and an append-only tracker (detected → routed → gated → ready to ship → shipped or regressed). The owner approves every ship.
7. **Console.** One local page: Run the loop (shows the plan and the cost first), every call as a square, Audit, Issues, Fix and Test with per-call rows and transcript links, paired old/new replay strips per call, a rubric editor with live threshold scoring, and the tracker.

**Sarvam.** A draft replica of VANI was built in IndiaMART's Sarvam workspace through the Sarvam Voice Agents MCP, with the real prompt, 38 variables and stand-in tools, and exercised with the Tests API. Text replays run on the rendered VANI Jinja prompt because the replica cannot take per-call variables mid-call. The audio-replica design for the next step is in `docs/replica_analysis.md`.

**What we refuse to claim.** No patch cleared the gate on the hackathon data. The gate refused to ship fixes it could not prove. That is the point of the gate.

## 5. Sarvam agent ID / live link

OWNER to confirm. The agent in the repo's build log (`ideation/07_sarvam_replica.md`) is:

- Agent id: `VANI-replic-7f077343-8b50` ("VANI replica (vaniloop test, do not deploy)"), version 1, draft, IndiaMART workspace, built 9 Oct 2026 via the Sarvam MCP.
- A second draft sandbox exists: `VANI-replic-0dd220cc-43c3`.

If the demo-video session built a separate agent, use that id instead.

## 6. Demo video URL

OWNER (another session is producing it).

## 7. Impact on success metrics

**Share of calls audited: 100%.** 712 of 712 ingested calls graded, about $0.004 per call (transcription $0.0024, labels $0.0017; Jev priced at 0 because its price is unknown). Total LLM spend to the demo freeze: $12.60.

**Verdicts (712 calls):** Fatal 38, Non-Fatal 285, Pass 225, NeedsReview 164. Fatal is 5.3% of all calls and 6.9% of the 548 graded calls, against the human audit's 4.7% Fatal on Sarvam calls (VANI Call Vital report, 23 Aug to 26 Sep 2026). NeedsReview is 23%: every call where the evidence was missing goes to a human instead of a guessed Pass.

**Agreement with reviewers:** on the held-out half of a 756-item reviewed set, the judgement labels score 91% (buyer irritated), 85% (same product), 86% (spec question fits the product, 2% wrong, 11% unsure) and 60% (question with options fits, 13% wrong, 28% unsure). Unsure sends the call to review, it never passes.

**Root causes:** 4 ranked patterns from 548 graded calls. Quantity never asked (128 calls, priority 128), bot keeps asking for the same detail (11 Fatal + 100 Non-Fatal, priority 77.5), product change not followed (10 Fatal + 9 Non-Fatal, routed to a tool ticket), spec questions from the wrong category (5 Fatal + 20 Non-Fatal, routed to a data ticket). 5 channel causes (bad audio) are separated and watched, not patched.

**Failure rate before and after a fix:** the best patch (quantity never asked, attempt 2) cut the old prompt's 50% replay failure rate to 25% on 8 hold-out calls × 3 seeds. The gate still did not ship it: 3 trials per arm had unresolved evidence, so no p-value was computed. Looping attempt 2: 87.5% → 83.3%, no effect. No patch shipped. Two causes were routed to evidence tickets because a prompt edit cannot fix a wrong ISQ list or a missing tool call.

**Time from issue to tested fix:** one gate run (patch written → decision) took 9 to 26 minutes and $0.25 to $1.02 per attempt. The one-button loop (audit → judge → issues → fix → test → report) on a 16-call folder took 13 minutes and $1.05 (17:21 to 17:34 IST, 10 Oct).

**Robustness:** 974 tests on synthetic fixtures, a data-leak scan in CI, call data never in git, every finding with evidence and basis, every gate decision with its p-value, lift, seeds, cost and paired replays.

## 8. Solution folder (optional)

Repo: https://github.com/amteshwarkhokhar/vaniloop (private today). OWNER decision: make it public or share a Drive folder.

Before making it public, check:

```bash
bin/check                      # tests + phone-number scan + tracked-data scan
git ls-files | grep -Ei 'transcripts|runs/|data/raw|\.env$|vani_prompt'   # must print nothing
gh repo edit amteshwarkhokhar/vaniloop --visibility public --accept-visibility-change-consequences
```

Contents the judges see: code in `src/vaniloop/`, README, synthetic sample outputs in `tests/fixtures/`, decisions in `docs/DECISIONS.md`, research in `ideation/`.

---

## Rubric map (for the video and the form)

| Judged on | Where it shows |
|---|---|
| Business impact | Section 7: 100% of calls audited at $0.004 each, 4 ranked causes with owners, tickets for the two faults a prompt cannot fix |
| Technical robustness | The ship gate (exact test, hold-out, screen, guards, unresolved never passes), 974 tests, data-leak CI |
| Solution completeness | All 7 PS1 outputs: per-call grade with evidence, root causes with type and examples, transparent priority, prompt diffs, replay test with before/after and time, risk flags, one dashboard |
| Voice experience | The demo call in the video, the Sarvam replica, Hinglish transcripts with tone and overlap flags |
