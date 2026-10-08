# 04 TEST — Option B (MOONSHOT): "Fork-the-Call" counterfactual replay with a self-calibrated judge

## (a) What judges score and what great looks like
PS1 scores: failure-rate drop after fix (25%), time issue-to-tested fix (15%). Overall: robustness (20%), voice experience (20%), demo (15%).
Great = one number a sceptic believes: "Fatal rate 31% -> 6% on n=120 replays, 95% CI shown, 0 new Fatals on passed calls, issue-to-tested-fix 11 min". Plus the judge can HEAR call 282 before and after.
Weak = "we ran 3 calls and they looked better".

## (b) Approaches
1. **Plain text replay (baseline).** LLM buyer persona from the failed call's user turns talks to old-prompt bot and new-prompt bot. Same judge grades both.
   Pros: 6-8 h, simple. Cons: buyer drifts, no ASR noise, n small, every team does this. Risk: low.
2. **Fork-the-Call counterfactual replay (my pick, core).** Keep the real call verbatim up to the failure turn. At that turn, swap in the new prompt. Simulated buyer takes over, seeded with the real remaining user turns as "what this person wants and how they talk". Run k=5 forks per failed call with persona dials (irritated, noisy, terse, Hinglish-heavy). Paired design: same call, old vs new.
   Pros: isolates the fix, removes caller variance, n scales (40 failed calls x 5 = 200 trials), paired stats (McNemar) are tight. Cons: needs a bot-under-test harness. 14-16 h. Risk: medium.
3. **Full voice loop.** Simulated buyer text -> Bulbul TTS -> 8 kHz telephony degrade -> Saaras STT -> bot -> Bulbul. Real latency and real WER.
   Pros: tests ASR failures, voice score, audible demo. Cons: slow, costly, STT noise adds variance. 10-12 h on top. Risk: high.
4. **Held-out real calls only.** Apply fix to calls never used for writing it; compare. Pros: honest. Cons: cannot replay conversation, bot output is not regenerated. Use only as a supporting check.

## (c) Recommendation
Build 2 as the engine, add 3 as a narrow "audible lane" (only 3 showcase calls, not the whole set), and use 4 as the split rule.
Why: 2 gives the 25% score honestly. 3 gives the voice score and the demo. Together they are a tiered ladder: text at scale, voice on a few.
Build plan (3 people): P1 fork harness + buyer simulator (14 h). P2 judge calibration + stats + JSON writer (10 h). Non-tech: pick 3 showcase calls, hand-label 20 calls, script the demo (8 h). Voice lane: P1 or P2, 6 h.

## (d) Unique edge most teams will miss
1. **Judge calibration without humans: mutation testing for the judge.** Take passed calls. Inject known defects by script (drop the qty question, repeat a name probe 4x, insert 7 s gap, swap a spec). Each mutant has a known label. Run the AUDIT judge on 100 mutants + originals. Report judge recall/precision per rubric parameter. This turns "is the before/after trustworthy?" into a measured answer. It also feeds the 25% agreement score.
2. **Cross-family triangle.** Bot-under-test = Gemini. Buyer simulator = Claude. Judge = a third run (Claude with rubric, plus Gemini as second vote). Report where they disagree. No model grades its own work.
3. **Cluster-held-out fix check.** Write the fix from 70% of a root cause's calls. Test on the other 30%. Shows the fix generalises and is not memorised.
4. **Regression shield with a hard gate.** Replay all passed calls (like 133) with the new prompt. Gate: zero new Fatals, else the fix is marked "rejected" on the dashboard. Rejecting a fix live is a strong demo moment.
5. **Auto stopwatch.** Every artifact carries timestamps: issue flagged -> diff written -> replay done -> verdict. Dashboard shows "issue to tested fix: 11 min". No manual timing.
6. **Audible A/B.** Same user audio (or Bulbul clone of the user turns) against old and new bot, side by side. Judge hears the name loop disappear.

## (e) Contract (JSON)
Input: fix package from FIX stage, failed-call set from AUDIT/CLUSTER, judge from AUDIT.
Output: `test_result.json`, one per fix, read by the dashboard.
```json
{
  "$schema": "http://json-schema.org/draft-07/schema#",
  "title": "test_result",
  "type": "object",
  "required": ["fix_id","root_cause_id","prompt_diff_ref","design","trials","summary","regression","judge_calibration","timing","verdict"],
  "properties": {
    "fix_id": {"type":"string"},
    "root_cause_id": {"type":"string"},
    "prompt_diff_ref": {"type":"string", "description":"path to old/new prompt + diff"},
    "design": {
      "type":"object",
      "required":["mode","split","bot_model","buyer_sim_model","judge_models","seed","k_forks"],
      "properties":{
        "mode":{"enum":["text_fork","voice_fork","held_out_real"]},
        "split":{"type":"object","properties":{"train_call_ids":{"type":"array","items":{"type":"string"}},"test_call_ids":{"type":"array","items":{"type":"string"}}}},
        "bot_model":{"type":"string"},"buyer_sim_model":{"type":"string"},
        "judge_models":{"type":"array","items":{"type":"string"}},
        "seed":{"type":"integer"},"k_forks":{"type":"integer"}
      }
    },
    "trials": {
      "type":"array",
      "items":{
        "type":"object",
        "required":["trial_id","call_id","persona","fork_turn_index","old","new"],
        "properties":{
          "trial_id":{"type":"string"},"call_id":{"type":"string"},
          "persona":{"type":"object","properties":{"irritation":{"type":"number"},"noise":{"enum":["none","asr_noise","telephony"]},"style":{"type":"string"}}},
          "fork_turn_index":{"type":"integer"},
          "old":{"$ref":"#/definitions/run"},
          "new":{"$ref":"#/definitions/run"}
        }
      }
    },
    "summary": {
      "type":"object",
      "required":["n_trials","fatal_rate_before","fatal_rate_after","ci95_before","ci95_after","paired_test","by_parameter"],
      "properties":{
        "n_trials":{"type":"integer"},
        "fatal_rate_before":{"type":"number"},"fatal_rate_after":{"type":"number"},
        "ci95_before":{"type":"array","items":{"type":"number"},"minItems":2,"maxItems":2},
        "ci95_after":{"type":"array","items":{"type":"number"},"minItems":2,"maxItems":2},
        "paired_test":{"type":"object","properties":{"name":{"const":"mcnemar"},"b_fixed":{"type":"integer"},"c_broken":{"type":"integer"},"p_value":{"type":"number"}}},
        "by_parameter":{"type":"object","additionalProperties":{"type":"object","properties":{"before":{"type":"number"},"after":{"type":"number"}}}},
        "judge_agreement_between_models":{"type":"number"}
      }
    },
    "regression": {
      "type":"object",
      "required":["n_passed_calls_replayed","new_fatals","gate_passed"],
      "properties":{"n_passed_calls_replayed":{"type":"integer"},"new_fatals":{"type":"integer"},"new_fatal_call_ids":{"type":"array","items":{"type":"string"}},"gate_passed":{"type":"boolean"}}
    },
    "judge_calibration": {
      "type":"object",
      "properties":{"n_mutants":{"type":"integer"},"recall":{"type":"number"},"precision":{"type":"number"},"per_parameter":{"type":"object"},"human_labelled_agreement":{"type":["number","null"]}}
    },
    "timing": {
      "type":"object",
      "required":["issue_flagged_at","fix_written_at","test_done_at","issue_to_tested_fix_sec"],
      "properties":{"issue_flagged_at":{"type":"string","format":"date-time"},"fix_written_at":{"type":"string","format":"date-time"},"test_done_at":{"type":"string","format":"date-time"},"issue_to_tested_fix_sec":{"type":"number"}}
    },
    "audio_demo": {"type":["object","null"],"properties":{"call_id":{"type":"string"},"old_wav":{"type":"string"},"new_wav":{"type":"string"},"asr_wer_observed":{"type":"number"},"p50_turn_latency_ms":{"type":"number"}}},
    "verdict": {"enum":["ship","reject_regression","inconclusive_low_n"]}
  },
  "definitions": {
    "run": {
      "type":"object",
      "required":["transcript","grade"],
      "properties":{
        "transcript":{"type":"array","items":{"type":"object","properties":{"speaker":{"enum":["bot","user"]},"text":{"type":"string"},"start_sec":{"type":"number"}}}},
        "grade":{"type":"object","required":["overall","failed_parameters"],"properties":{"overall":{"enum":["pass","non_fatal","fatal"]},"failed_parameters":{"type":"array","items":{"type":"string"}},"evidence_turns":{"type":"array","items":{"type":"integer"}}}}
      }
    }
  }
}
```
Uses the same grade object shape as AUDIT output (overall + rubric parameter names from the QA matrix) so the same judge code runs everywhere.

## (f) Validation with no human labels
- Judge: mutation set (known injected defects) gives recall/precision. Add 20 hand-labelled calls by the non-tech member for a real agreement number.
- Simulator realism: check that the simulated buyer's replies on the unchanged prefix match real user turns (embedding similarity) and that the OLD bot reproduces the original failure in at least 70% of forks. If it does not, the harness is wrong, not the fix. This "reproduction rate" is reported.
- Sanity negative control: apply a no-op prompt change. Expect no rate change. Catches judge/sim noise.
- Sanity positive control: apply a deliberately bad prompt. Expect rate to rise.
- Cross-model disagreement reported, not hidden.
- If n < 30 per cause, verdict = inconclusive_low_n.

## (g) Open questions for organisers (Day 1)
1. Does the Sarvam Voice Agent accept a prompt override per run, so we can A/B prompts on the real agent? Is there a sandbox call API?
2. May call text go to Sarvam LLMs, or only Claude/OpenAI/Gemini (data rule)? If only the latter, Sarvam TTS/STT may be used only on synthetic text.
3. Is the real VANI prompt available, or may we infer it? Will the QA team hand-label 20-30 calls for calibration?
4. Which pass/fail threshold counts as "fixed"? Is the Sep'26 baseline (4% BL fatal) the comparison number, or our own audited rate?
5. Does "time issue-to-tested-fix" start at flag time or at recording time?
