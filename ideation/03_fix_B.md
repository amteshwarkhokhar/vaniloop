# 03 FIX (lens B: MOONSHOT) - "Every line has a receipt"

## (a) What judges score and what "great" looks like
Judges score this stage through two PS1 numbers: failure-rate drop after fix (25%) and time issue-to-tested-fix (15%). They also score a concrete, reviewable diff per top cause.
Great means: a small diff a QA lead can read in 30 seconds. Each line links to a root cause and to evidence turns. Each line was proven by a replay. No regressions. And the fix names the right surface. Not every cause is a prompt cause.

## (b) Approaches

**A. One-shot LLM rewrite.** Give the model the inferred prompt plus the root cause. Ask for a better prompt.
- Pros: 4 build hours. Easy.
- Cons: Big diffs. Hidden regressions. No proof which line helped. Judges see "we asked an LLM".
- Risk: low to build, high to defend.

**B. Typed rule patch + candidate tournament.** The fixer emits small typed rules (trigger, behavior, anti-example), not prose. Each rule is compiled into a minimal diff. Generate 3 candidates per cause. TEST picks the winner by replay.
- Pros: Reviewable. Attributable. About 14 build hours.
- Cons: Needs a rule-to-diff compiler and a baseline prompt.
- Risk: medium.

**C. Moonshot: "Prompt archaeology + counterfactual fork + line receipts + fix-surface router".** Approach B plus four extra ideas (see d). About 24 build hours across 3 people, with B as the fallback layer.
- Pros: Memorable demo. Defensible numbers.
- Cons: More moving parts.
- Risk: medium-high. Mitigated because every extra idea degrades safely to B.

## (c) Recommendation
Build C on top of B. Day 1 builds B end to end (rule schema, compiler, tournament hook into TEST). Day 2 morning adds the router, fork-replay and ablation. Cut order if late: ablation first, then archaeology depth. Never cut the router. It is the cheapest idea and the most visible.

Why: the data already proves a prompt-only mindset is wrong. In call 282 the bot asked DRONE specs (DJI, 5.1K, flight time) about a home dome camera. That looks like an ISQ list injected from a stale category after the product changed. A prompt edit cannot fix a wrong injected list. It needs a flow/config fix plus a prompt guard. A team that only writes prompt text will "fix" it badly and the replay will not move.

## (d) Unique edge most teams will miss

1. **Prompt archaeology (no prompt available).** Reverse-engineer the bot's hidden rules from bot turns across all 713 transcripts. Examples visible now: "one enquiry per day" (call 10), "I only give info on the current product" (282), "seller details on WhatsApp" close (all three), "seller unavailable" script (133), "ask qty, then ISQs, then city, then name" order. Output `baseline_prompt_inferred.md` with each inferred rule tagged to turn evidence and a confidence. Same method tells us the template slots: `{product}`, `{isq_list}`, `{buyer_name}`. Then every fix is a diff against a document that is itself evidence-backed. Ask organisers for the real prompt on Day 1. If they give it, swap it in and nothing else changes.

2. **Fix-surface router.** Each root cause is routed to one surface before any text is written:
   - `prompt_patch` (wording, rule order, anti-loop rule)
   - `flow_guard` (deterministic code: re-fetch ISQs when the product changes; stop after 2 asks of one slot; dead-air filler)
   - `asr_vocab` (hotwords/lexicon for Saaras: names like "BuyerA", product terms)
   - `kb_entry` (price questions, "can I enquire for 2 products")
   - `no_fix_human` (not safely fixable)
   For 282 the router outputs two fixes: a flow guard (rebind ISQ set to the new MCAT) and a prompt rule (on product change, say so back and confirm). Hard-coded counters beat prompt text for loops, because an LLM prompt cannot count reliably. The Name loop in 282 (4 asks, then a nonsense "pin code" answer) is the example.

3. **Counterfactual fork replay.** Do not replay a whole call. Find the first divergence turn (the first bot turn that causes the failure). Freeze the real call up to there. Fork. Run old prompt and new prompt from that exact turn. The buyer is an LLM persona that first replays the real user turns it has, then improvises in the same voice. This shows "same call, same buyer, one turn changed". For call 10 the fork point is turn 2 ("Hello?" at 14 s unanswered). The new bot must acknowledge the second product and not say "one enquiry per day" as a flat refusal. It is a before/after you can play in audio.

4. **Line receipts by ablation.** After the tournament, remove each new line one at a time. Re-run the fork set. If the pass rate does not drop, the line is deleted. Result: the shortest possible diff, and each surviving line carries a measured receipt ("+9 of 12 forks"). This also limits prompt bloat and cross-rule conflicts. A shorter prompt is a voice benefit too (lower latency, fewer long turns).

5. **Regression canaries by construction.** Every rule ships with its own failing test, plus the `must_still_pass` list: all Pass calls that mention the same slot or product class (133 is a canary for the seller-unavailable script). A rule that flips a canary is rejected automatically. A rule-conflict check asks the LLM "does rule R contradict any existing rule?" before the tournament, to cut cost.

6. **Voice-aware guard.** Each fix must pass lint: max sentence length, no lists of more than 3 options read aloud, Hinglish register kept, no TTS-hostile tokens. This ties FIX to the "voice experience 20%" score.

## (e) Contract (consumes ISSUE root cause JSON, feeds TEST)

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "title": "fix_bundle",
  "type": "object",
  "required": ["schema_version", "generated_at_ist", "baseline_prompt", "fixes"],
  "properties": {
    "schema_version": {"const": "1.0"},
    "generated_at_ist": {"type": "string", "format": "date-time"},
    "baseline_prompt": {
      "type": "object",
      "required": ["source", "path", "sha256"],
      "properties": {
        "source": {"enum": ["organiser_supplied", "inferred"]},
        "path": {"type": "string"},
        "sha256": {"type": "string"},
        "inferred_rules": {"type": "array", "items": {
          "type": "object", "required": ["rule_id", "text", "evidence", "confidence"],
          "properties": {
            "rule_id": {"type": "string"}, "text": {"type": "string"},
            "evidence": {"type": "array", "items": {"$ref": "#/$defs/evidence"}},
            "confidence": {"type": "number", "minimum": 0, "maximum": 1}}}}
      }
    },
    "fixes": {"type": "array", "items": {"$ref": "#/$defs/fix"}}
  },
  "$defs": {
    "evidence": {
      "type": "object", "required": ["call_id", "turn_index"],
      "properties": {"call_id": {"type": "string"}, "turn_index": {"type": "integer"},
                     "start_sec": {"type": "number"}, "quote": {"type": "string"}}
    },
    "fix": {
      "type": "object",
      "required": ["fix_id", "root_cause_id", "surface", "title", "rationale",
                   "candidates", "fork_points", "must_still_pass", "human_review", "status"],
      "properties": {
        "fix_id": {"type": "string"},
        "root_cause_id": {"type": "string"},
        "rubric_params": {"type": "array", "items": {"type": "string"}},
        "surface": {"enum": ["prompt_patch", "flow_guard", "asr_vocab", "kb_entry", "no_fix_human"]},
        "title": {"type": "string"},
        "rationale": {"type": "string", "description": "One plain sentence: why this surface, why this edit."},
        "candidates": {"type": "array", "minItems": 1, "items": {
          "type": "object",
          "required": ["candidate_id", "rules", "unified_diff", "token_delta"],
          "properties": {
            "candidate_id": {"type": "string"},
            "rules": {"type": "array", "items": {
              "type": "object", "required": ["rule_id", "trigger", "behavior"],
              "properties": {
                "rule_id": {"type": "string"},
                "trigger": {"type": "string"},
                "behavior": {"type": "string"},
                "anti_example": {"type": "string"},
                "good_example": {"type": "string"},
                "touches_inferred_rule": {"type": "array", "items": {"type": "string"}},
                "voice_lint_pass": {"type": "boolean"}}}},
            "unified_diff": {"type": "string", "description": "Diff against baseline_prompt (prompt_patch) or pseudo-code/YAML (other surfaces)."},
            "token_delta": {"type": "integer"},
            "conflict_check": {"type": "object", "properties": {
              "conflicts": {"type": "array", "items": {"type": "string"}},
              "checked_by_model": {"type": "string"}}}
          }}},
        "fork_points": {"type": "array", "items": {
          "type": "object", "required": ["call_id", "fork_turn_index", "failure_description", "expected_behavior"],
          "properties": {
            "call_id": {"type": "string"}, "fork_turn_index": {"type": "integer"},
            "frozen_history_turns": {"type": "integer"},
            "failure_description": {"type": "string"},
            "expected_behavior": {"type": "string"},
            "success_check": {"type": "string", "description": "Machine-checkable: regex, rubric param, or AUDIT judge param that must be Pass."},
            "buyer_persona": {"type": "object", "properties": {
              "style": {"type": "string"}, "real_user_turns": {"type": "array", "items": {"type": "string"}}}},
            "held_out": {"type": "boolean"}}}},
        "must_still_pass": {"type": "array", "items": {"type": "string"}, "description": "call_ids of Pass calls used as canaries."},
        "ablation": {"type": "object", "properties": {
          "run": {"type": "boolean"},
          "line_receipts": {"type": "array", "items": {
            "type": "object", "properties": {
              "rule_id": {"type": "string"}, "forks_fixed_with": {"type": "integer"},
              "forks_fixed_without": {"type": "integer"}, "kept": {"type": "boolean"}}}}}},
        "human_review": {"type": "object", "required": ["required"], "properties": {
          "required": {"type": "boolean"}, "reviewer": {"type": "string"},
          "decision": {"enum": ["pending", "approved", "edited", "rejected"]}, "note": {"type": "string"}}},
        "status": {"enum": ["proposed", "in_test", "accepted", "rejected"]},
        "created_from_issue_at_ist": {"type": "string", "format": "date-time"}
      }
    }
  }
}
```
TEST reads `candidates[].unified_diff`, `fork_points`, `must_still_pass`. TEST writes back the winning `candidate_id`, `line_receipts` and the before/after numbers. `created_from_issue_at_ist` starts the "time issue to tested fix" clock.

## (f) Validation with no human labels
1. **Planted-bug recovery.** Take the inferred prompt. Inject 5 known defects (remove the loop rule, hard-code the wrong ISQ set, drop the product-change confirm). Run AUDIT and ISSUE and FIX. Does the loop find and repair the planted defect? This gives ground truth we made ourselves.
2. **Held-out siblings.** Fit the fix on half of a cluster's calls. Score on the other half. A fix that only works on its training calls is overfit.
3. **Canary flips.** Count Pass calls that become Fail. Target is zero.
4. **Cross-family judge.** Generator is Claude. The re-grade judge is Gemini (or OpenAI). Never the same model that wrote the fix. Only Claude, OpenAI or Gemini get call data.
5. **Ablation sanity.** A line that never matters gets cut, so surviving lines are real.
6. **Hand-check.** The non-tech teammate reads 10 diffs and 10 fork replays and marks "would a QA lead approve". Report this as a small hand-labelled sample, with n shown.
Honest reporting: show n and a confidence interval for every before/after number. Say that simulated buyers are not real buyers.

## (g) Open questions for organisers (Day 1)
1. Can we get the real VANI prompt, or at least the template slots and the ISQ injection logic? If not, may we label our reconstructed prompt "inferred"?
2. Is "fix" allowed to be a flow/config/ASR change, or must it be prompt text only? (Router depends on this.)
3. Which Sarvam surface runs the replay: Sarvam LLM text-only, or Voice Agent with our prompt? Is the agent ID tied to one prompt?
4. How will "failure rate before/after" be measured by the judges: our replay, or their held-out calls?
5. Does the "one enquiry per day" rule and the WhatsApp close come from policy (must keep) or from the prompt (may change)?
6. Is the Sep'26 baseline (95% pass, 4% BL fatal) the number to beat?
