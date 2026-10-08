# 03 FIX (A, pragmatic): prompt fix generation

## (a) What judges score and what "great" looks like
FIX feeds two PS1 scores directly: failure-rate drop after fix (25%) and time issue-to-tested-fix (15%). It also feeds "business impact" and "technical robustness".

Great looks like this:
- Each top root cause has one small, readable diff a QA lead can approve in 2 minutes.
- Each diff says which rubric parameter it targets and which real turns prove the need.
- The diff does not break calls that already pass.
- The non-prompt causes (ASR, dead air) get a typed fix too, not a fake prompt edit.
- The clock runs: cause found at T0, tested fix at T1, shown on the dashboard.

## (b) Approaches

**1. Rewrite the whole prompt.** An LLM reads causes and writes a new full prompt.
- Pro: 3 build hours. Sounds impressive.
- Con: no reviewable diff. Hides regressions. Cannot apply to the real VANI prompt.
- Risk: high. Judges cannot trust it.

**2. Rule-block patches on an inferred baseline prompt (recommended).**
- Step 1: reverse-engineer a baseline prompt from the bot turns, split into named sections (opening, product confirm, ISQ loop, qty, city, name, handoff, objections).
- Step 2: for each cause, Claude proposes 3 candidate patches. Each patch is a unified diff that edits or adds one rule in one section.
- Step 3: TEST picks the winner.
- Pro: reviewable, minimal, portable. 10-12 build hours.
- Con: the baseline is a guess. Mitigation: label it "inferred" and ask organisers for the real one (see g).
- Risk: medium. Low if we keep rules scoped.

**3. Prompt optimiser loop (DSPy/GEPA-style).** Auto-mutate the prompt and score by replay.
- Pro: strong story, may find better wording.
- Con: needs many replays, costs tokens, produces messy diffs. 18+ hours.
- Risk: high for 2 days.

**4. Typed fix menu (extension of 2).** Each cause maps to a fix type: `prompt_patch`, `asr_hotword`, `kb_entry`, `flow_rule`, `latency_config`. Only `prompt_patch` and `kb_entry` are replay-tested. The others ship as config artifacts with a "needs vendor change" tag.
- Pro: honest about non-prompt causes. Only 2 extra build hours.
- Risk: low.

## (c) Recommendation
Build approach 2 plus the typed menu from 4. Skip 1 and 3.

Why: the best score per hour. The diff is the demo artifact. Candidates-then-select gives TEST something real to rank. Scoped rules protect the regression suite.

Build plan (3 people, about 12 h):
- Person A (3 h): reverse-engineer the baseline prompt from all 713 transcripts. Sample 40 bot turns per section. Output `baseline_prompt.md` with section IDs.
- Person B (6 h): fix generator. One Claude call per cause. Input is root-cause JSON plus the relevant section text. Output is 3 candidate patches, then a self-critique pass that checks each patch for scope and contradictions.
- Person C, non-tech (3 h): human review UI step. Approve, edit or reject. Also writes the "must-not-break" list of sample passing calls (like call 133).

Generation rules (put these in the generator prompt):
1. Edit one section. Max 5 changed lines per candidate.
2. Write a trigger condition ("when buyer rejects the confirmed product...") and a required behaviour.
3. Anchor each patch to the rubric rule it enforces. Example: the looping rule says max 1+2 probes, so "ask a field at most twice, then move on and add to details".
4. No new facts. Never invent prices, policies or product lists.
5. Add one negative guard: a line saying when the rule must NOT fire.

Worked examples from the 3 samples:
- Call 282, stale category (type prompt/flow): "When the buyer rejects or changes the product, restate the new product once, drop all earlier ISQs, and ask ISQs only for the new product's category. Never offer brands or specs from another category." Guard: do not fire when buyer only adds a spec.
- Call 282, name loop: "Accept the name after the first clear utterance. Never ask for the name more than twice. Never mention pin code in name capture."
- Call 10, second product deflection: "If buyer names a second product, do not state a one-enquiry-per-day limit. Capture it in additional details and continue with the first." (Flag: the limit may be a real policy. Ask organisers.)
- Call 10, dead air: type `latency_config`, not a prompt fix. Suggest an "acknowledge within 2 s" filler rule plus a barge-in setting.

## (d) Unique edge most teams will miss
**Evidence-linked, regression-guarded patches.** Every patch carries three things: the failed turns that justify it, the rubric rule it enforces, and an auto-built guard set. The guard set is 10 passing calls from the same section plus 3 synthetic "should not fire" cases the generator writes itself. TEST reruns these. A fix that drops pass rate on the guard set is rejected automatically.

Second edge: the patch is written as a **portable rule block**. If organisers give the real VANI prompt, we re-anchor the same rule to the real section. No work is lost.

Third edge: we apply the winning prompt to a real Sarvam Voice Agent and give the agent ID. The demo then plays before and after audio.

## (e) Contract

Input: root-cause JSON from ISSUE (fields used: `cause_id`, `name`, `type`, `rubric_params`, `example_calls`, `priority`). Output below goes to TEST.

```json
{
  "$schema": "http://json-schema.org/draft-07/schema#",
  "title": "FixSet",
  "type": "object",
  "required": ["run_id", "baseline_prompt_id", "created_at_ist", "fixes"],
  "properties": {
    "run_id": {"type": "string"},
    "baseline_prompt_id": {"type": "string", "description": "e.g. inferred_v1 or organiser_v1"},
    "baseline_source": {"enum": ["inferred", "organiser"]},
    "created_at_ist": {"type": "string", "format": "date-time"},
    "fixes": {
      "type": "array",
      "items": {
        "type": "object",
        "required": ["fix_id", "cause_id", "fix_type", "status", "candidates"],
        "properties": {
          "fix_id": {"type": "string"},
          "cause_id": {"type": "string"},
          "fix_type": {"enum": ["prompt_patch", "kb_entry", "asr_hotword", "flow_rule", "latency_config"]},
          "testable_by_replay": {"type": "boolean"},
          "rubric_params": {"type": "array", "items": {"type": "string"}},
          "evidence": {
            "type": "array",
            "items": {"type": "object", "required": ["call_id", "turn_idx", "quote"],
              "properties": {"call_id": {"type": "string"}, "turn_idx": {"type": "integer"}, "quote": {"type": "string"}}}
          },
          "target_section_id": {"type": "string"},
          "candidates": {
            "type": "array", "minItems": 1,
            "items": {
              "type": "object",
              "required": ["candidate_id", "rationale", "diff"],
              "properties": {
                "candidate_id": {"type": "string"},
                "rationale": {"type": "string"},
                "diff": {"type": "string", "description": "unified diff against baseline section"},
                "patched_section_text": {"type": "string"},
                "lines_changed": {"type": "integer"},
                "should_not_fire_when": {"type": "string"},
                "self_critique": {"type": "string"},
                "risk_flags": {"type": "array", "items": {"enum": ["new_fact", "policy_unverified", "too_broad", "contradicts_rule"]}}
              }
            }
          },
          "non_prompt_artifact": {"type": ["object", "null"], "description": "config text for asr_hotword, flow_rule or latency_config"},
          "guard_set": {
            "type": "object",
            "properties": {
              "passing_call_ids": {"type": "array", "items": {"type": "string"}},
              "synthetic_should_not_fire": {"type": "array", "items": {"type": "string"}}
            }
          },
          "test_scenarios": {
            "type": "array",
            "description": "buyer persona seeds for the simulator",
            "items": {"type": "object", "properties": {"source_call_id": {"type": "string"}, "persona": {"type": "string"}, "user_turns": {"type": "array", "items": {"type": "string"}}}}
          },
          "status": {"enum": ["proposed", "approved", "edited", "rejected", "tested"]},
          "reviewer": {"type": ["string", "null"]},
          "selected_candidate_id": {"type": ["string", "null"]},
          "timing": {"type": "object", "properties": {"cause_detected_at": {"type": "string"}, "fix_generated_at": {"type": "string"}, "fix_approved_at": {"type": ["string", "null"]}}}
        }
      }
    }
  }
}
```

TEST writes `selected_candidate_id` and the `tested` status back, so the dashboard reads one file.

## (f) Validation with no human labels
- **Replay on its own failures.** The failure rate on the cause's example calls must fall under the patched prompt. This is TEST's job, but FIX must hand over scenarios.
- **Guard set.** Pass rate on the guard calls must not fall. Rubric-based re-grading uses the same AUDIT judge.
- **Scope check (cheap, no replay).** A second Claude call, different prompt, answers: does the patch contain a new fact, a contradiction with another section, or a trigger wider than the evidence? Reject on any yes.
- **Evidence sanity.** Every `quote` must appear verbatim in the cited transcript. A string match, zero cost.
- **Consistency.** Generate each patch twice at temperature >0. If the two disagree on the core behaviour, mark low confidence and send to the human.
- **Hand-label tiny set.** Label 15 calls ourselves, using the matrix. Use them to sanity check the replay judge.

## (g) Open questions for organisers (Day 1)
1. Can we get the real VANI prompt, or a redacted version? Fallback is the inferred one.
2. Is "one enquiry per day" a real policy? Is it fixable?
3. Which settings can a Sarvam Voice Agent change: prompt only, or also STT hotwords, barge-in, and latency?
4. Is a fix to the ISQ list a prompt matter or a category-data matter? Who owns it?
5. Do the human auditors score on the bot's spoken confirmation, or on the posted BL? We have only the transcripts.
6. May we play synthetic caller audio in the demo?
7. What counts as "tested" for scoring: replay on failed calls, or held-out calls?
