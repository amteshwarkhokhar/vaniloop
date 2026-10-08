# 02 ISSUE (A, pragmatic): root-cause grouping and ranking

## (a) What judges score, and what great looks like
- PS1 gives 25% for grouping plus ranking. Failure-rate drop (25%) and time-to-fix (15%) also depend on us: FIX only gets as good as our causes.
- Great: 5 to 8 root causes, not 40. Each has a plain name, a one-line description, a type (prompt / ASR / knowledge gap / flow-logic), 3+ example calls with evidence, and a score the judge can recompute by hand.
- Great also means causes, not symptoms. "Drone ISQs asked on a camera" is a symptom. "Bot keeps the old category after the buyer changes product" is the cause.
- Each cause says what to change. "Fixable by prompt" is a field, not a guess.

## (b) Approaches

**1. Rubric bucket count (naive).** Group by AUDIT's failed rubric parameter (Qty missed, Looping, etc.).
- Pros: 1 build hour, zero LLM risk, perfectly stable.
- Cons: groups symptoms, not causes. Call 282 would fall under Looping, Category ISQ and Product change at once. Weak on "root cause" scoring.
- Build: 3 h. Risk: low score.

**2. LLM open-coding, then merge (recommended base).** One LLM call per failed call: "what went wrong, in mechanism terms, and where in the flow". Then one LLM pass merges codes into a taxonomy, then each failure is assigned to a taxonomy cause.
- Pros: yields real causes. Works with no labels. Cheap (a few hundred failed calls). Transparent: every cause links to calls and quotes.
- Cons: taxonomy can wobble between runs. Needs a stability guard.
- Build: 10 to 12 h. Risk: medium.

**3. Embeddings + HDBSCAN on failure descriptions.** Embed per-call failure notes, cluster, name clusters with an LLM.
- Pros: fully data-driven. Looks sophisticated.
- Cons: Hinglish embeddings are noisy. Many small calls produce a "noise" bucket. Hard to explain to judges. Names still need an LLM. Needs an embedding model that is allowed (Claude/OpenAI/Gemini only).
- Build: 14 to 16 h. Risk: high for little gain at about 100 to 300 failures.

**4. Hybrid (my pick): approach 2 plus a seeded taxonomy.** Start from 10 to 12 known mechanisms (below). The LLM may only assign to a seed or propose a NEW cause. A merge pass accepts new causes only if 3+ calls share them. Build 12 to 14 h. Risk: low to medium.

## (c) Recommendation
Approach 4. Reasons: it is stable, cheap, explainable, and it works from hour 1 because seeds come from the 3 transcripts and the rubric. Nothing needs training. Team split: 1 dev builds the pipeline, 1 dev builds the score and stability check, the non-tech member reviews the top 8 cause names and 3 examples each (this is the human check judges like).

Seed mechanisms (inferred from samples, to be extended):
1. Stale category after product change (282: school-bus CCTV to home dome, drone ISQs). Type: flow-logic.
2. Unrelated or wrong-category ISQ asked (282). Type: knowledge gap.
3. Name capture loop / slot not accepted (282: "BuyerA" x4). Type: flow-logic.
4. Off-topic or non-sequitur reply (282: "Hindi mein hi baat kar rahi hoon", "pin code se pehchaan"). Type: prompt.
5. Multi-product request deflected as "one enquiry per day" (10). Type: prompt/policy.
6. Dead air or unheard user, "Hello?" (10, gap about 6.5 s). Type: ASR/latency.
7. Bot repeats stale confirmation, ignores correction (10). Type: flow-logic.
8. Vague answer ("jitna achha hai") mishandled or forced to a value (282). Type: prompt.
9. Quantity or unit not captured or wrong. Type: ASR or flow.
10. Hinglish entity mis-heard (product, city, name). Type: ASR.
11. Seller-pitch skipped in AstBuy. Type: flow-logic.
12. Price question answered with a promise ("best rate milega"). Type: prompt.

Priority score (all three terms 1 to 5, shown in the dashboard):
`score = frequency_pct x severity x fixability`
- frequency_pct = failed calls with this cause / audited calls (also shown as raw count and share of all fatals).
- severity = 5 if the cause produced a Fatal on a BL field (product, qty, unit, spec1/2), 3 for irritation, loop or disconnect, 2 for non-fatal BL damage, 1 for style. Use the max over linked calls, plus the mean.
- fixability = 5 prompt-only, 3 prompt plus a tool or state rule, 2 needs knowledge base or ISQ data, 1 ASR or infra, outside the prompt. The LLM proposes it and gives one line of reason.
- Show a sort by score and also by raw count. Judges can recompute it.
- Multi-cause calls: each call has one primary cause (counted for frequency) and up to two secondary causes (shown, not counted). Frequency sums to 100% of failures.

## (d) Unique edge most teams will miss
1. **Symptom to cause collapse, shown visually.** For each cause, a small "symptom tree": rubric params that fired (Looping, ISQ error, Product change) all roll up into one named mechanism. Count how many separate fatal flags one cause explains. For 282 that is 3 flags to 1 cause. This directly answers "root cause, not symptom".
2. **Causal first-turn.** Ask the LLM for the earliest turn where the call went wrong (turn index + quote). Group on the first error, not the last visible symptom. Call 282's first error is turn 2, not the name loop at turn 20.
3. **Stability score.** Run the clustering 3 times at different temperatures or orders. Report per-cause stability (share of calls with the same cause). Drop or flag causes below 0.7. A published stability number is rare and a strong robustness story.
4. **Fix-impact estimate.** Show "fixing cause 1 removes about N fatal calls (X% of fatals)". It sets the expected failure-rate drop that TEST then proves.
5. **ASR separation.** Flag ASR causes where the transcript is the likely culprit (garbled, name echoed 4x). Do not send them to prompt FIX. This keeps FIX credible.

## (e) Contract

**Input (from AUDIT), one record per audited call.** Fields I assume. AUDIT owner should confirm.
```json
{
  "call_id": "282_90685047_X413J1UG5",
  "verdict": "fatal",
  "failures": [
    {"param": "category_isq_error", "severity": "fatal",
     "reason": "Asked drone ISQs for dome camera",
     "evidence": [{"turn": 13, "t_sec": 57.0, "quote": "DJI ya MI ya kuch aur?"}]}
  ],
  "transcript_ref": "transcripts/282_90685047_X413J1UG5.json"
}
```

**Output (to FIX and DASHBOARD):** `issues.json`
```json
{
  "$schema": "http://json-schema.org/draft-07/schema#",
  "type": "object",
  "required": ["run_id", "generated_at", "audited_calls", "failed_calls", "root_causes"],
  "properties": {
    "run_id": {"type": "string"},
    "generated_at": {"type": "string", "format": "date-time"},
    "audited_calls": {"type": "integer"},
    "failed_calls": {"type": "integer"},
    "score_formula": {"type": "string", "const": "frequency_pct * severity * fixability"},
    "stability": {"type": "object", "properties": {
      "runs": {"type": "integer"}, "mean_agreement": {"type": "number"}}},
    "root_causes": {
      "type": "array",
      "items": {
        "type": "object",
        "required": ["cause_id", "rank", "name", "description", "type",
                     "call_count", "frequency_pct", "severity", "fixability",
                     "priority_score", "fixable_by_prompt", "examples", "symptom_params"],
        "properties": {
          "cause_id": {"type": "string", "pattern": "^RC[0-9]{2}$"},
          "rank": {"type": "integer", "minimum": 1},
          "name": {"type": "string", "maxLength": 60},
          "description": {"type": "string", "maxLength": 200},
          "type": {"enum": ["prompt", "ASR", "knowledge_gap", "flow_logic"]},
          "call_count": {"type": "integer"},
          "frequency_pct": {"type": "number", "description": "call_count / audited_calls * 100"},
          "fatal_count": {"type": "integer"},
          "severity": {"type": "integer", "minimum": 1, "maximum": 5},
          "severity_reason": {"type": "string"},
          "fixability": {"type": "integer", "minimum": 1, "maximum": 5},
          "fixability_reason": {"type": "string"},
          "priority_score": {"type": "number"},
          "stability": {"type": "number", "minimum": 0, "maximum": 1},
          "fixable_by_prompt": {"type": "boolean"},
          "symptom_params": {"type": "array", "items": {"type": "string"},
            "description": "Rubric params that this cause explains"},
          "suspected_prompt_area": {"type": "string",
            "description": "e.g. category-change handling, slot-fill retry rule"},
          "examples": {
            "type": "array", "minItems": 1,
            "items": {"type": "object", "required": ["call_id", "first_error_turn", "quote"],
              "properties": {
                "call_id": {"type": "string"},
                "first_error_turn": {"type": "integer"},
                "t_sec": {"type": "number"},
                "quote": {"type": "string"},
                "role": {"enum": ["primary", "secondary"]}}}},
          "call_ids": {"type": "array", "items": {"type": "string"}}
        }
      }
    },
    "unassigned_call_ids": {"type": "array", "items": {"type": "string"}}
  }
}
```
FIX consumes `root_causes[].{cause_id, name, description, type, examples, suspected_prompt_area}` for the top N with `fixable_by_prompt=true`. TEST uses `call_ids` as the replay set. DASHBOARD uses all fields.

## (f) Validation with no human labels
- **Hand-seeded check:** the 3 known calls must map to cause 1, 3, 5/6 (we know the answer). Use as unit test.
- **Self-label 40 calls:** the team labels the cause for about 40 failed calls in 1 hour (non-tech member leads). Report cause-match rate.
- **Stability:** 3 runs, report agreement. Fail if under 70%.
- **Cross-model check:** assign a sample with Claude and with Gemini (both allowed). Report Cohen's kappa.
- **Sanity:** every failed call has exactly one primary cause. Frequencies sum to 100. Each cause has at least 3 calls or is folded into "Other".
- **Coverage:** share of failed calls in "Other" must stay under 15%.
- **Downstream proof:** the cause list is good if TEST shows the fix for cause X lowers X's call count in the replay. This ties to the 25% failure-drop score.

## (g) Open questions for organisers (Day 1)
1. Will any human audit labels or a hand-labelled sample be given, so we can check cause match?
2. Do auditors use a root-cause list already? We can adopt their names.
3. Is the bot prompt, or the flow spec, shareable? It lifts fixability scoring from guess to fact.
4. Does "frequency" mean calls with the cause, or total rubric flags? We assume calls.
5. Is the severity weighting free to choose, or must Fatal always outrank frequency?
6. Which allowed model for bulk calls (cost or rate caps)? We plan Claude or Gemini Flash.
7. Are repeat callers or the same buyer across several calls in the set?

## Build plan (about 14 h, 3 people)
- H0 to 2: seed taxonomy, prompt for per-call coding (JSON mode), schema.
- H2 to 6: run on all failed calls, merge pass, assignment.
- H6 to 9: score, fixability, symptom tree, stability runs.
- H9 to 12: self-label 40 calls, fix seeds, freeze.
- H12 to 14: hand to FIX and DASHBOARD, demo screenshots.
Fallback if time runs out: approach 1 (rubric count) plus LLM naming only. Gives a valid contract in 3 h.
