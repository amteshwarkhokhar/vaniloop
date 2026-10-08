# 01 AUDIT — Approach A (pragmatic)

Lens: most score per build hour. Three people, two days. Transcripts are Gemini Flash text, so the whole stage is text plus a few cheap audio signals.

## (a) What judges score and what "great" looks like
- Agreement with human auditors (25%). Great: per-parameter agreement, not only overall. Kappa reported. Honest "abstain" on unclear calls.
- Share of calls audited (10%). Great: 713 of 713, with a visible count of "auto-graded" vs "sent to human".
- Feeds root-cause (25%) and fix tests (25%). Great: every verdict has a turn-index evidence quote and a stable parameter code. Downstream stages can group and re-grade without parsing prose.
- Fatal means "hurts the seller through the BL". Great: the judge reasons about BL impact, not about bot style.

## (b) Approaches

**1. One big judge.** One LLM prompt: rubric plus transcript, returns Pass/Fatal/Non-Fatal and a reason.
- Pros: 3 build hours. Cheap.
- Cons: Weak on per-parameter agreement. Verdicts drift. No evidence per parameter. Hard to re-run in TEST.
- Risk: Medium. Low score on the 25% agreement line.

**2. Per-parameter judges.** 17 separate prompts per call.
- Pros: Focused. Easy to tune one parameter.
- Cons: 17 x 713 = 12k calls. Cost and latency up. Parameters contradict each other (qty vs unit). 14+ build hours.
- Risk: Medium. Slow iteration.

**3. Extract-then-rule (hybrid).** Step 1: one LLM call extracts a structured "call ledger" (see below) from the transcript. Step 2: Python rules apply the rubric to the ledger. Step 3: deterministic signals (silence, repeats, short calls) are added in code. Step 4: a second model re-checks only Fatal or low-confidence calls.
- Pros: The rubric lives in readable code, so fatal/non-fatal logic is transparent and editable in minutes. One LLM call per call. Evidence is attached by design. TEST reuses it.
- Cons: Extraction errors flow into rules. Needs the ledger schema to be right.
- Build: about 14 hours total. Risk: Low to medium.

## (c) Recommendation: Approach 3
Reason: the rubric is a decision table. Decision tables belong in code, not in a prompt. The LLM does only what it is good at: reading Hinglish and pulling facts. Cost check from the run log: about 0.002 to 0.006 USD per call, so 713 calls cost about 2 to 3 USD per pass. We can afford 3 passes.

Pipeline:
1. **Ledger extraction** (Gemini 3 Flash, JSON mode, temp 0). Per call it returns:
   - `product_initial`, `product_final`, `product_changed`
   - `bl_inferred`: qty, unit, spec1, spec2, spec3, BFD, city, name. Each is read from the bot's spoken confirmations and the user's answers. Each gets the turn index it came from, or null.
   - `slots`: for every thing the bot asked (qty, each ISQ, city, name): ask turn indexes, answer turn indexes, final status (captured / refused / not_answered / never_asked).
   - `buyer_said_unprompted`: facts the user gave that the bot never used (qty, spec, second product).
   - `buyer_signals`: irritation, "hello?" probes, asks for human, price questions, language complaints.
   - `bot_anomalies`: non-sequiturs, wrong-category questions, deflections.
2. **Rules in code** map the ledger to the 17 parameters. Example: `qty_missed = FATAL if buyer_said_unprompted.qty and not bl.qty`. Example: `looping = FATAL if ask_count>3 and (irritation or slot not captured)`.
3. **Deterministic signals**:
   - `ffmpeg silencedetect` on audio: gaps over 5 s (8 s if noise). Cross-check with transcript gaps. In call 10 the gap 30.0 to 36.5 is 6.5 s and matches.
   - Repeated-question detector: normalised bot question similarity (token Jaccard) across turns.
   - "Hello?" / "sun rahe ho" regex on user turns.
   - Calls under 10 s with no user turn: "no engagement" bucket, not graded Fatal for BL parameters.
4. **Second opinion** (Claude, different family) on every Fatal, every low-confidence call and a 10% random Pass sample. It sees the ledger plus transcript. Disagreement sets `needs_human=true`.
5. **Confidence**: run the extraction twice (temp 0 and 0.4). Agreement rate on the verdict, plus a model self-score, gives `confidence`. Below 0.6 abstains to human.

Overall call = Fatal if any parameter is Fatal. Else Non-Fatal if any is Non-Fatal. Else Pass.

## (d) Unique edge most teams will miss
- **The "slot ledger".** Most teams ask an LLM "was there a loop?". We count asks per slot in code. This gives exact loop evidence (call 282: name asked 5 times, user said "BuyerA" 4 times) and drives the qty-probing and spec-missed rules.
- **Inferred BL.** Fatal in this rubric means "wrong BL". No BL is available, so we rebuild it from the bot's own confirmations. We then show it next to the evidence. Judges see we understood the rubric's real definition.
- **Stale category check.** Compare the ISQs the bot asked with `product_final`. Call 282 (drone ISQs on a dome camera) becomes a rule, not a vibe. This also feeds ISSUE as a root-cause hint.
- **WER proxy.** Our transcripts are themselves ASR output, so WER cannot be measured against truth. Instead flag BL-critical tokens (qty, unit, product, name) where the bot's confirmation disagrees with the user's words. Optional upgrade: re-transcribe only those flagged calls with a second STT and compare.
- **Calibrated abstain.** We report agreement on auto-graded calls only, plus the abstain rate. That is more honest and scores better than guessing.

## (e) Output contract (one file per call: `audit/<call_id>.json`)
```json
{
  "$schema": "http://json-schema.org/draft-07/schema#",
  "title": "CallAudit",
  "type": "object",
  "required": ["call_id","audit_version","overall","params","risk_flags","confidence","needs_human","bl_inferred","signals"],
  "properties": {
    "call_id": {"type":"string"},
    "audit_version": {"type":"string", "description":"rubric+prompt hash; TEST re-grades with same version"},
    "overall": {"enum":["PASS","NON_FATAL","FATAL","UNGRADED"]},
    "overall_reason": {"type":"string"},
    "params": {
      "type":"array",
      "items": {
        "type":"object",
        "required":["code","verdict","reason","evidence"],
        "properties": {
          "code": {"enum":["OUTCOME","QTY_VALUE","QTY_UNIT","QTY_PROBING","QTY_MISSED","SPEC1","SPEC2","SPEC_MISSED","LOOPING","WER","BFD","ONCALL_ASTBUY","PRODUCT_CHANGE","CATEGORY_ISQ","MCAT_SEARCH","DEAD_AIR","OBSERVATION"]},
          "verdict": {"enum":["PASS","NON_FATAL","FATAL","NA"]},
          "reason": {"type":"string"},
          "evidence": {
            "type":"array",
            "items": {"type":"object","required":["turn_idx","quote"],
              "properties":{"turn_idx":{"type":"integer"},"start_sec":{"type":"number"},"end_sec":{"type":"number"},"quote":{"type":"string"}}}
          },
          "source": {"enum":["llm","rule","signal","second_opinion"]},
          "rule_id": {"type":"string"},
          "confidence": {"type":"number","minimum":0,"maximum":1}
        }
      }
    },
    "bl_inferred": {
      "type":"object",
      "properties": {
        "product_initial":{"type":"string"}, "product_final":{"type":"string"}, "product_changed":{"type":"boolean"},
        "qty":{"type":["string","null"]}, "unit":{"type":["string","null"]},
        "specs":{"type":"array","items":{"type":"object","properties":{"name":{"type":"string"},"value":{"type":["string","null"]},"turn_idx":{"type":["integer","null"]}}}},
        "bfd":{"type":["string","null"]}, "city_state":{"type":["string","null"]}, "name":{"type":["string","null"]}
      }
    },
    "slots": {
      "type":"array",
      "items":{"type":"object","properties":{
        "slot":{"type":"string"}, "ask_turns":{"type":"array","items":{"type":"integer"}},
        "answer_turns":{"type":"array","items":{"type":"integer"}},
        "status":{"enum":["captured","refused","not_answered","never_asked"]}}}
    },
    "signals": {
      "type":"object",
      "properties":{
        "duration_sec":{"type":"number"}, "max_gap_sec":{"type":"number"}, "gaps_over_5s":{"type":"array","items":{"type":"number"}},
        "hello_probes":{"type":"integer"}, "repeat_question_max":{"type":"integer"},
        "stale_category":{"type":"boolean"}, "short_call":{"type":"boolean"}
      }
    },
    "risk_flags": {
      "type":"array",
      "items":{"type":"object","required":["flag","severity"],
        "properties":{"flag":{"enum":["WRONG_REQUIREMENT","FRUSTRATION","LOOP","LOW_CONFIDENCE","HUMAN_REQUEST","DEAD_AIR","NON_SEQUITUR","DEFLECTION"]},
                      "severity":{"enum":["low","med","high"]},"turn_idx":{"type":"integer"}}}
    },
    "confidence": {"type":"number","minimum":0,"maximum":1},
    "needs_human": {"type":"boolean"},
    "models": {"type":"object","properties":{"extractor":{"type":"string"},"second_opinion":{"type":["string","null"]}}}
  }
}
```
ISSUE reads `params[].code` + `evidence` + `bl_inferred` + `risk_flags`. TEST re-runs the same audit on simulated transcripts of the same shape (`turns[]`), same `audit_version`.

## (f) Validation with no human labels
1. **Golden trio.** Calls 282, 10, 133 have known answers from the brief. 282 must be FATAL (CATEGORY_ISQ, LOOPING, MCAT_SEARCH). 10 must flag DEAD_AIR and a deflection. 133 must PASS. Run first, every time.
2. **Fast hand-labelling.** Three people label 60 stratified calls in about 90 minutes: overall verdict plus Fatal reason code only. Pick 20 predicted Fatal, 20 Non-Fatal, 20 Pass. Two people label each, the third breaks ties. Report overall accuracy, Cohen's kappa and per-parameter agreement. Label a second batch only if time remains.
3. **Fault injection.** Take 30 clean Pass calls. Programmatically inject known faults: delete a qty answer, repeat a bot question 4 times, add a 7 s gap, swap a unit in the bot confirmation. Measure recall per parameter. This tests recall with zero human labels.
4. **Self-consistency.** Re-run on 100 calls. Report verdict flip rate. High flip rate means abstain more.
5. **Cross-model check.** Claude vs Gemini agreement on Fatal. Disagreements go to the hand-label queue first.

Cost: extraction about 2 to 3 USD per full pass. Second opinion on about 30% of calls adds about 3 USD. Total under 15 USD with reruns.

Build plan (hours): ledger prompt + schema 4, rules 4, signals 3, second opinion + confidence 2, label tool and agreement report 3. Total about 16 across 3 people. The non-tech member does the hand-labelling.

## (g) Open questions for organisers (Day 1)
1. Are there any human-graded calls (even 20) we can use as a gold set?
2. Is the VANI prompt, or the ISQ lists per category, available? This makes the stale-category and spec rules exact.
3. Does the bot confirm the BL aloud at the end? If a captured-BL export exists per call, we can use it instead of inferring.
4. Can call audio go to Sarvam Saaras for a second-opinion transcript, given the rule that call data goes only to Claude, OpenAI or Gemini?
5. How do QA treat calls where the buyer hangs up before any BL field? Is "no intent" Fatal under OUTCOME?
6. Do auditors grade the 74 calls under 10 s, or exclude them?
7. How many Fatal parameters make one call Fatal in the "Overall call" field? Any one, as we assume?
