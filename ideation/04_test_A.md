# 04 TEST (prove the fix) - Ideation A (PRAGMATIC)

Verdict: build a text-only replay harness with an LLM "buyer twin" per failed call. Run old prompt vs new prompt, k=5 runs each, re-grade with the same AUDIT judge. Add a small voice demo on top for the video. Do not build a full voice loop first.

## (a) What judges score and what "great" looks like
- PS1 25% "failure-rate drop after fix": needs a before/after number that survives a hard question. Great = paired before/after on the same scenarios, with n and a confidence interval, plus a held-out set the fix never saw.
- PS1 15% "time issue to tested fix": great = a timer stamped on each issue (audit flagged -> fix drafted -> test passed). Target under 10 min per fix, machine-logged, not guessed.
- Overall: robustness 20% (regression suite proves no new breakage), voice experience 20% (audible before/after), demo 15%.
- Great also means honest: say what is simulated and what is real.

## (b) Approaches
**A1. Text replay with buyer twin (core).** An LLM plays the buyer. Its brief comes from the real failed call: the buyer's goal, hidden facts (name "BuyerA", wants home dome camera, qty 1, city unknown) and habits ("Jitna achha hai", repeats name, says "Hello?" when silent). Bot-under-test = prompt P0 or P1 plus a stub product/ISQ list. Loop up to 12 turns.
- Pros: cheap, fast (about 20 s per run), parallel, seedable. Any cluster is testable.
- Cons: the bot prompt is unknown, so P0 is inferred. The twin is not a real buyer.
- Build: 10-12 h. Risk: low-medium.

**A2. Scripted turn replay.** Feed the real user turns in order to old and new bot.
- Pros: 3 h, no twin. Cons: the new bot diverges, so the fixed buyer lines stop making sense. Weak evidence. Use only for single-turn checks (e.g. product change at turn 2). Risk: low value.

**A3. Full voice loop.** Sarvam Saaras STT -> LLM -> Bulbul TTS, or a Sarvam Voice Agent, with the twin speaking via TTS.
- Pros: shows dead air, latency, pronunciation. Cons: slow, flaky, 8 kHz noise, hard to run 100 times. Build: 16-20 h. Risk: high.

**A4. Hybrid (recommended).** A1 for the numbers. A3 only for 2-3 hero scenarios (282 name loop, 10 dead air) recorded for the demo. Build: 14-16 h total across 3 people.

## (c) Recommendation
A4. The score comes from the number, the number comes from A1. Voice is a demo layer, not the measurement.

Plan for 3 people (2 days):
1. Person 1 (tech): harness, twin, runner, judge call, JSON writer. 
2. Person 2 (tech): voice demo with Sarvam Agent ID, cluster split, regression set, stats script.
3. Person 3 (non-tech): hand-label 25-30 calls, write the buyer-twin briefs for top 5 clusters, run the demo script.

Design rules:
- **Reproduction gate.** Before testing any fix, run P0 against the twin. P0 must reproduce the failure in at least 60% of runs. If not, the twin or inferred P0 is wrong. Fix the twin first. This makes the "before" number meaningful.
- **Split per cluster.** For each root cause, split failed calls 50/50: "dev" (the fix author sees these) and "held-out" (never seen). Report both separately. Held-out is the headline number.
- **Regression set.** 20-30 calls that passed (like 133) plus clean-run twins. P1 must keep them passing. Report "newly broken" count.
- **Same judge, different model.** Re-grade with the AUDIT judge unchanged. The judge model must differ from the twin model. Never let the fix author see judge prompts for the held-out set.
- **Cheap deterministic checks beside the judge:** loop = same slot asked 3+ times; dead air = gap over 5 s (8 s noisy); wrong-category = ISQ terms not in the product's list (drone ISQs for CCTV). These cannot be argued with and save judge cost.
- **Stats.** k=5 runs per scenario, temp 0.7, fixed seeds logged. Unit = scenario (average of its 5 runs, to avoid fake n). Paired bootstrap 95% CI on the drop. Add McNemar on scenario pass/fail flips. Report n. If CI crosses zero, say "no proven effect".
- **Time.** Each fix carries timestamps: issue_flagged_at, fix_drafted_at, test_started_at, test_passed_at. Dashboard shows the delta.

## (d) Unique edge most teams will miss
Most teams replay the old call and judge the new reply once. That is anecdote. Our edge is three things: the reproduction gate (proves the "before" is real), the held-out split per root cause (proves the fix generalizes), and the regression suite (proves no harm). Also the "what changed" table: for each scenario, flips fail->pass, pass->fail, unchanged. Judges see losses too, which builds trust. Bonus: twin "stress variants" of the same call (noisy, impatient, code-mixed, silent) so one real failure becomes 4 tests.

## (e) Contract (JSON)
Input from stage 03 (FIX): `{fix_id, root_cause_id, prompt_old, prompt_new, diff, scenario_call_ids[], created_at}`. Input from 01 (AUDIT): per-call grade JSON. Output to dashboard, one file per fix: `test_results/<fix_id>.json`.

```json
{
  "$schema": "http://json-schema.org/draft-07/schema#",
  "title": "TestResult",
  "type": "object",
  "required": ["fix_id","root_cause_id","config","reproduction","before","after","heldout","regression","timing","scenarios","verdict"],
  "properties": {
    "fix_id": {"type":"string"},
    "root_cause_id": {"type":"string"},
    "config": {
      "type":"object",
      "required":["mode","k_runs","temperature","seed","bot_model","twin_model","judge_model","judge_version","max_turns"],
      "properties":{
        "mode":{"enum":["text","voice"]},
        "k_runs":{"type":"integer"},"temperature":{"type":"number"},"seed":{"type":"integer"},
        "bot_model":{"type":"string"},"twin_model":{"type":"string"},
        "judge_model":{"type":"string"},"judge_version":{"type":"string"},
        "max_turns":{"type":"integer"},
        "prompt_old_sha":{"type":"string"},"prompt_new_sha":{"type":"string"},
        "p0_is_inferred":{"type":"boolean"}
      }
    },
    "reproduction": {
      "type":"object",
      "description":"P0 vs twin must reproduce the original failure",
      "required":["rate","passed_gate"],
      "properties":{"rate":{"type":"number"},"threshold":{"type":"number"},"passed_gate":{"type":"boolean"}}
    },
    "before": {"$ref":"#/definitions/rateBlock"},
    "after":  {"$ref":"#/definitions/rateBlock"},
    "heldout":{
      "type":"object",
      "required":["before","after","drop_pp","ci95_pp"],
      "properties":{
        "before":{"$ref":"#/definitions/rateBlock"},"after":{"$ref":"#/definitions/rateBlock"},
        "drop_pp":{"type":"number"},"ci95_pp":{"type":"array","items":{"type":"number"},"minItems":2,"maxItems":2}
      }
    },
    "regression":{
      "type":"object",
      "required":["n_scenarios","newly_broken","fatal_before","fatal_after"],
      "properties":{"n_scenarios":{"type":"integer"},"newly_broken":{"type":"integer"},
        "newly_broken_ids":{"type":"array","items":{"type":"string"}},
        "fatal_before":{"type":"number"},"fatal_after":{"type":"number"}}
    },
    "timing": {
      "type":"object",
      "required":["issue_flagged_at","fix_drafted_at","test_passed_at","issue_to_tested_fix_sec"],
      "properties":{"issue_flagged_at":{"type":"string","format":"date-time"},
        "fix_drafted_at":{"type":"string","format":"date-time"},
        "test_started_at":{"type":"string","format":"date-time"},
        "test_passed_at":{"type":"string","format":"date-time"},
        "issue_to_tested_fix_sec":{"type":"number"}}
    },
    "scenarios": {
      "type":"array",
      "items":{
        "type":"object",
        "required":["scenario_id","source_call_id","split","variant","outcome_old","outcome_new"],
        "properties":{
          "scenario_id":{"type":"string"},"source_call_id":{"type":"string"},
          "split":{"enum":["dev","heldout","regression"]},
          "variant":{"enum":["original","noisy","impatient","silent","codemix"]},
          "outcome_old":{"type":"array","items":{"$ref":"#/definitions/run"}},
          "outcome_new":{"type":"array","items":{"$ref":"#/definitions/run"}},
          "flip":{"enum":["fail_to_pass","pass_to_fail","same_fail","same_pass"]}
        }
      }
    },
    "verdict":{"type":"object","required":["status","note"],
      "properties":{"status":{"enum":["proven","not_proven","regressed","gate_failed"]},"note":{"type":"string"}}},
    "audio_demo":{"type":"object","properties":{"before_wav":{"type":"string"},"after_wav":{"type":"string"},"sarvam_agent_id":{"type":"string"}}}
  },
  "definitions":{
    "rateBlock":{"type":"object","required":["n_scenarios","n_runs","fatal_rate","nonfatal_rate","by_parameter"],
      "properties":{"n_scenarios":{"type":"integer"},"n_runs":{"type":"integer"},
        "fatal_rate":{"type":"number"},"nonfatal_rate":{"type":"number"},
        "by_parameter":{"type":"object","additionalProperties":{"type":"number"}}}},
    "run":{"type":"object","required":["run_id","grade","transcript_ref"],
      "properties":{"run_id":{"type":"string"},"grade":{"enum":["pass","non_fatal","fatal"]},
        "failed_parameters":{"type":"array","items":{"type":"string"}},
        "evidence":{"type":"string"},"transcript_ref":{"type":"string"},
        "checks":{"type":"object","properties":{"loop":{"type":"boolean"},"dead_air":{"type":"boolean"},"wrong_category_isq":{"type":"boolean"}}}}}
  }
}
```
`failed_parameters` uses the names from the QA matrix (e.g. "Looping Behavior", "Category ISQ's Error").

## (f) Validation with no human labels
1. Reproduction gate (above): P0 must re-fail the way the real call did.
2. Judge sanity: run the AUDIT judge on the 3 known failures (282, 10, 133). It must give fatal, fatal/non-fatal, pass. Add 25-30 hand-labelled calls by Person 3 as a small anchor set. Report agreement.
3. Twin realism: a blind check. Mix 10 twin buyer turns with 10 real ones. A teammate guesses which are fake. Near 50% is good. Also check the twin never leaks hidden facts the bot did not ask for.
4. Negative control: run P0 vs P0 (no change). The "drop" must be about 0 and CI must include 0. This shows the noise level.
5. Sabotage control: a deliberately bad prompt P-bad must score worse. This shows the test can detect harm.
6. Judge/twin from different model families to avoid self-preference.

## (g) Open questions for organisers (Day 1)
1. Is the real VANI prompt or ISQ list provided? If not, is an inferred prompt acceptable?
2. Data rule says only Claude/OpenAI/Gemini may see call data. May Sarvam LLM or Sarvam Voice Agent receive paraphrased or synthetic buyer text? Until answered, voice demo uses synthetic scripts only, no real transcript text.
3. How do they compute "failure rate drop": on simulated replays, on held-out real calls, or both?
4. Is there a human-labelled set for scoring agreement, and can we see even 10 examples?
5. Does "time issue to tested fix" start at audit flag or at the first listen? Is automated timing accepted?
6. Is a Sarvam agent ID for the fixed bot required, or one agent per version (old and new)?
