# 02 ISSUE (B, moonshot): "Causes that prove themselves"

Idea in one line: a root cause is not a label. It is a testable claim: "if this one thing changes, these N calls would have passed." We name causes from the first fault in each call, then we check each claim by replaying the call with the cause switched off.

## (a) What judges score and what great looks like
Root-cause grouping and ranking is 25% of PS1. Judges read a short list and ask: are the names right, is the order sensible, can I see the maths?
Great looks like:
- 5 to 9 causes, each with name, one-line description, type (prompt / ASR / knowledge gap / flow-logic), 2+ example calls with turn evidence.
- Causes, not symptoms. "Drone ISQs on a camera" is a symptom. "Category not refreshed after product change" is the cause.
- A score anyone can recompute from a table.
- Proof the cause is real. Most teams will only count.

## (b) Approaches
1. **LLM taxonomy then assign.** One Claude/Gemini pass reads failure summaries and proposes a taxonomy. A second pass assigns each failed call. Pros: fast, readable names. Cons: unstable between runs, no proof, symptoms leak in. Build: 6 h. Risk: low.
2. **Embeddings + HDBSCAN on failure evidence.** Pros: data-driven, shows a cluster map. Cons: with ~713 calls and ~5% fatal (~35 to 90 failures) clusters are tiny and noisy. Hinglish embeddings group by topic (camera, drone), not by cause. Names still need an LLM. Build: 8 h. Risk: medium.
3. **Moonshot: first-fault tracing + counterfactual verification (recommended).**
   - Step A: for each failed call, an LLM finds the FIRST FAULT TURN, the earliest bot turn where the call leaves the ideal path. Everything after is downstream. Call 282: first fault is turn 2 (bot accepts "ghar par", then keeps school-bus context), not the name loop at the end.
   - Step B: write a MECHANISM card per call: trigger, bot behaviour, consequence (a causal chain, 3 links).
   - Step C: merge cards by mechanism, not by words. Merging is an LLM pass with a rule: two cards merge only if one prompt change would fix both. Each cause becomes a CAUSE HYPOTHESIS with an intervention sentence.
   - Step D: COUNTERFACTUAL TEST. Cut the transcript at the first fault turn. Re-run the bot side with the intervention added to an inferred prompt, against a user simulator that replays the real user's later lines (see (d)). Count how many member calls flip to pass.
   - Step E: rank by saved fatal calls x severity, not by raw frequency.
   Pros: causes carry evidence, type is decided by experiment, FIX gets a ready hypothesis. Cons: more moving parts, needs the inferred prompt. Build: 16 to 20 h split over two people. Risk: medium-high. Fallback: stop after Step C and still ship a good grouping.
4. (Add-on) Open coding then merge by humans. Slow. Skip.

## (c) Recommendation
Build 3, layered so it degrades safely. Hours 0 to 6: Steps A to C (this alone equals approach 1 but better, because of first-fault logic). Hours 6 to 16: Step D on top 3 causes only. Step E is a spreadsheet formula.
Why: it hits grouping (25%), feeds failure-drop (25%) and time-to-fix (15%) directly, and the demo story is strong: "each cause on this slide has a green tick: we replayed it".

## (d) The unique edge
1. **First-fault attribution.** Others label the last visible symptom. We label the cause that started the chain. The 282 call becomes one cause with three symptoms: wrong ISQs, name loop, off-topic reply.
2. **Type by ablation, not by opinion.** Run up to 3 cheap what-ifs per cause:
   - Oracle-ASR: swap the user turn for a clean human-style rewrite. If the call flips, type = ASR.
   - Prompt patch: add the intervention. If it flips, type = prompt.
   - Fact injection: add the missing fact (e.g. "a user may ask for a 2nd product: log it as note, do not refuse"). If only this flips, type = knowledge gap.
   - None flips but a state reset flips (e.g. re-run ISQ lookup after product change): type = flow-logic.
   The type field becomes evidence, not a guess.
3. **Transparent score with a fixability term measured, not guessed.** `priority = saved_calls x severity_weight x (1 + share_of_members_flipped)`. Severity weights come from the QA matrix: Fatal-BL-impacting 3, looping with disconnect 3, Fatal-other 2, Non-Fatal 1. Weights live in one visible table.
4. **Cause DAG.** Causes can chain ("stale category" -> "wrong ISQ" -> "buyer irritated" -> "loop"). Show the DAG. Fixing the root of a chain gets credit for downstream calls.
5. **Stability badge.** Run the grouping 3 times with different seeds and sample orders. Show a Jaccard score per cause. Judges rarely see this.
6. **Cross-call echo.** Cause 3 in the sample data (bot says "one enquiry per day" on 2nd product request, call 10) can be searched across all 713 transcripts as a regex/LLM probe to count how often it fires even in PASS calls. That gives "silent exposure": calls graded pass that still carry the defect.

## (e) Contract (JSON Schema, draft 2020-12, informal)
Input from AUDIT: array of `audit_result` (`call_id`, `verdict` fatal|non_fatal|pass, `params_failed[]` with matrix parameter names, `severity`, `evidence[] {turn_idx, start_sec, quote}`, `confidence`). Plus transcripts by `call_id`.

Output `root_causes.json`:
```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "title": "RootCauseReport",
  "type": "object",
  "required": ["run_id","generated_at_ist","n_calls_audited","n_failed","severity_weights","causes","unassigned"],
  "properties": {
    "run_id": {"type":"string"},
    "generated_at_ist": {"type":"string","format":"date-time"},
    "n_calls_audited": {"type":"integer"},
    "n_failed": {"type":"integer"},
    "severity_weights": {"type":"object","additionalProperties":{"type":"number"}},
    "causes": {"type":"array","items":{
      "type":"object",
      "required": ["cause_id","name","description","type","member_calls","frequency","severity","priority_score","fix_hint","verification"],
      "properties": {
        "cause_id": {"type":"string"},
        "name": {"type":"string","maxLength":60},
        "description": {"type":"string","maxLength":200},
        "type": {"enum":["prompt","asr","knowledge_gap","flow_logic"]},
        "type_evidence": {"type":"array","items":{"type":"string"}},
        "mechanism": {"type":"object","required":["trigger","bot_behaviour","consequence"],
          "properties":{"trigger":{"type":"string"},"bot_behaviour":{"type":"string"},"consequence":{"type":"string"}}},
        "matrix_parameters": {"type":"array","items":{"type":"string"}},
        "member_calls": {"type":"array","items":{
          "type":"object","required":["call_id","first_fault_turn","evidence"],
          "properties":{"call_id":{"type":"string"},"first_fault_turn":{"type":"integer"},
            "role":{"enum":["primary","secondary"]},
            "evidence":{"type":"array","items":{"type":"object",
              "properties":{"turn_idx":{"type":"integer"},"start_sec":{"type":"number"},"quote":{"type":"string"}}}}}}},
        "frequency": {"type":"integer"},
        "frequency_pct_of_calls": {"type":"number"},
        "severity": {"type":"number"},
        "priority_score": {"type":"number"},
        "score_breakdown": {"type":"object","properties":{"saved_calls":{"type":"number"},"severity_weight":{"type":"number"},"fixability":{"type":"number"}}},
        "downstream_of": {"type":"array","items":{"type":"string"}},
        "fix_hint": {"type":"string","description":"One intervention sentence for FIX."},
        "inferred_prompt_section": {"type":"string"},
        "repro_scenarios": {"type":"array","items":{"type":"object","properties":{"call_id":{"type":"string"},"cut_at_turn":{"type":"integer"},"user_script":{"type":"array","items":{"type":"string"}}}}},
        "verification": {"type":"object","required":["status"],"properties":{
          "status":{"enum":["verified","partial","unverified"]},
          "flipped":{"type":"integer"},"tested":{"type":"integer"},
          "ablations":{"type":"object","properties":{"oracle_asr":{"type":"number"},"prompt_patch":{"type":"number"},"fact_injection":{"type":"number"},"state_reset":{"type":"number"}}}}},
        "stability": {"type":"number","minimum":0,"maximum":1}
      }}},
    "unassigned": {"type":"array","items":{"type":"string"}}
  }
}
```
FIX reads `fix_hint`, `inferred_prompt_section`, `repro_scenarios`. TEST reuses `repro_scenarios` and `member_calls`. DASHBOARD reads everything.

## (f) Validation with no human labels
- Hand-label ~30 failures ourselves in 90 minutes (3 people, 10 each, 2 raters on 10 overlap) and score cause assignment agreement (Cohen kappa on the overlap).
- Three seeded runs: stability >= 0.8 per top cause.
- Synthetic injection: copy 20 passing transcripts, plant known defects (stale-category swap, loop, dead-air gap) with a script, and check ISSUE recovers them with the right cause. Gives precision/recall with ground truth.
- Counterfactual flip rate is itself a label-free validity signal. A cause that flips 0 of its members is flagged "unverified" and downranked.
- Different model family for the grouper and the judge (Claude groups, Gemini or GPT verifies).
- Sanity on the three samples: expect 282 -> stale category (primary), 10 -> deflection policy + dead air, 133 -> pass.
Honest limit: the counterfactual runs against an INFERRED prompt and a simulated user. Report it as "directional", not proof.

## (g) Open questions for organisers (Day 1)
1. Can we get the real VANI prompt, or at least the ISQ flow and the "one enquiry per day" rule?
2. Are human audit labels for any calls available, even 30? They would calibrate everything.
3. Is a cause allowed to span several rubric parameters? Is grouping judged by rubric parameter or by root cause?
4. Which counts as "failure rate": calls fatal, or BL fatal only (4% baseline)?
5. Do you accept replay against a simulated caller as the TEST proof?
6. Is the 713-call transcript run (Gemini) final, or will a better ASR pass arrive? ASR-vs-prompt split depends on this.
7. Is Sarvam LLM allowed for the simulator, given data rules?
