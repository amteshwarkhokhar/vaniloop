# 01 AUDIT — Idea B (MOONSHOT): "Extract, Replay, Rule" with a self-built answer key

## (a) What judges score and what great looks like
- 25% agreement with humans. 10% share of calls audited (target 713/713, 100%).
- Great = every call graded per rubric parameter, each verdict carries a quote + timestamp, and the grade matches how the QA lead thinks: Fatal = "the seller is hurt by the BL".
- Great also = we state our own agreement number with an honest method, and we abstain on hard calls instead of guessing.
- AUDIT feeds ISSUE (25%) and TEST (25%), so clean evidence and stable tags matter more than prose.

## (b) Approaches
1. **One big judge.** Give the full rubric + transcript to one LLM. Get Pass/Fatal/Non-Fatal.
   - Pros: 4 hours. Cons: hides which parameter failed. Drifts on "Fatal if BL-impacting". No confidence. Risk: medium. 4 h.
2. **Per-parameter judges.** 17 small prompts per call.
   - Pros: clear tags, easy to tune. Cons: 17x calls, parameters disagree, no shared facts. Risk: medium. 12 h.
3. **Extract-then-rule (recommended core).** The LLM only extracts facts (events). Deterministic Python rules apply the QA matrix.
   - Pros: transparent, QA lead can edit a rule, cheap, repeatable. Cons: extraction schema takes care. Risk: low-medium. 14 h.
4. **Moonshot stack = 3 + signals + replay + self-made answer key** (below).
   - Adds ~14 h of work on top of 3. Risk: medium. Total ~28 h of a 3-person team's ~48 h. Ship 3 first, add layers in order of value.

## (c) Recommendation
Build approach 3, then add the four moonshot layers in this order: (1) acoustic truth, (2) echo-vs-ear ASR check, (3) seller-view replay, (4) mutation answer key.
Why: each layer fixes a known weak spot of text-only LLM grading, and each makes a demo moment.

**Pipeline per call**
1. *Acoustic pass (no LLM).* ffmpeg `silencedetect` on the mp3 gives real gaps. Gemini timestamps are coarse (~1 s). Call 10 shows bot silence of ~6.5 s after "Hello?". Dead air rule: >5 s (8 s if noise flag). Also: duration <10 s flag, "Hello?"/"sun nahi paa rahe" probes, overlap.
2. *Extract pass (LLM, Gemini 3 Flash; cost ~$0.002–0.006/call, ~$3 for 713).* Output events, not grades: product asked/confirmed, qty asked count, unit, each ISQ asked and answer, name/city asks, repeats, handoff, user irritation, request-for-human, topic change.
3. *Reconstructed BL.* From the bot's own read-back and user answers, rebuild a "shadow BL": product, qty+unit, spec1, spec2, BFD, city, name. Mark each field `stated | inferred | missing`.
4. *Rule pass (Python).* One function per rubric row. Input = events + shadow BL + acoustic facts. Output = verdict + evidence ids. Overall call = worst of parameters; Outcome accuracy is always Fatal when intent is wrong.
5. *Abstain.* If extraction and rules disagree, or models disagree, mark `needs_human` and route to the review queue.

## (d) Unique edge (what others will miss)
1. **Echo-vs-Ear ASR check (WER without ground truth).** The bot's read-back shows what the production ASR *thought* the buyer said. We run an independent ear (Sarvam Saaras, then Gemini) on the user turns. Mismatch on a BL-field word = suspected WER Fatal. Call 10: user says "mini ultrasound"; bot hears "quantum magnetic resonance" and keeps it. Check by diff of bot echo vs. our user text. Plain text diff, no labels needed.
2. **Seller-View Replay.** A second LLM plays the seller. It sees only the shadow BL and answers: "Can I call this buyer and quote? What is wrong?" If a seller cannot act, the call is Fatal. This is the QA lead's own definition turned into a test, and it explains *why* Fatal in one line. Call 282: the BL says dome camera but ISQs are DJI/5.1K/flight time. The seller sees contradictory specs and a missing name. Flag.
3. **Mutation answer key.** No human labels exist, so we make them. Take clean calls (like 133). Programmatically inject faults: insert a repeated question 4x, paste silence of 7 s into audio and shift timestamps, swap an ISQ from another category, drop the qty question. We know the truth for each. We report recall per parameter on 100+ injected calls. This gives a per-parameter agreement number before any QA lead is free.
4. **Loop detector with a number.** Normalised bot-question similarity (embedding or token Jaccard) counts probes per slot. The rubric says "1+2 times". Call 282 name loop = 5 asks, user said "BuyerA" 4x. Fatal if the call ends without the name/qty/spec captured or the user is irritated.
5. **Confidence by 3-model vote.** Run the extract pass with Gemini + Claude + OpenAI on a 15% sample plus all abstain candidates. Disagreement rate = confidence. Only allowed providers are used.

## (e) Contract: output JSON per call (`audit/<call_id>.json`), consumed by ISSUE
```json
{
  "$schema": "http://json-schema.org/draft-07/schema#",
  "title": "CallAudit", "type": "object",
  "required": ["call_id","schema_version","overall","params","shadow_bl","signals","risk_flags","confidence","needs_human"],
  "properties": {
    "call_id": {"type":"string"},
    "schema_version": {"const":"1.0"},
    "overall": {"type":"object","required":["verdict","reason"],
      "properties":{"verdict":{"enum":["pass","non_fatal","fatal"]},
                    "reason":{"type":"string","maxLength":240},
                    "primary_param":{"type":"string"}}},
    "params": {"type":"array","items":{"type":"object",
      "required":["param","verdict","reason","evidence"],
      "properties":{
        "param":{"enum":["outcome","qty_value","qty_unit","qty_probing","qty_missed","spec1","spec2","spec_missed","looping","wer","bfd","oncall_astbuy","product_change","category_isq","mcat_search","dead_air","additional_obs"]},
        "verdict":{"enum":["pass","non_fatal","fatal","na"]},
        "reason":{"type":"string"},
        "cause_hint":{"enum":["prompt","asr","knowledge_gap","flow_logic","unknown"]},
        "method":{"enum":["rule","llm","acoustic","echo_ear","seller_replay"]},
        "evidence":{"type":"array","minItems":1,"items":{"type":"object",
          "required":["turn_idx","start_sec","quote"],
          "properties":{"turn_idx":{"type":"integer"},"start_sec":{"type":"number"},
                        "end_sec":{"type":"number"},"quote":{"type":"string"}}}}}}},
    "shadow_bl": {"type":"object","properties":{
      "product":{"$ref":"#/$defs/field"},"qty":{"$ref":"#/$defs/field"},"unit":{"$ref":"#/$defs/field"},
      "spec1":{"$ref":"#/$defs/field"},"spec2":{"$ref":"#/$defs/field"},"bfd":{"$ref":"#/$defs/field"},
      "city":{"$ref":"#/$defs/field"},"name":{"$ref":"#/$defs/field"}}},
    "signals": {"type":"object","properties":{
      "max_silence_sec":{"type":"number"},"silence_events":{"type":"array","items":{"type":"object","properties":{"start":{"type":"number"},"len":{"type":"number"}}}},
      "duration_sec":{"type":"number"},"short_call":{"type":"boolean"},
      "hello_probes":{"type":"integer"},"repeat_counts":{"type":"object","additionalProperties":{"type":"integer"}},
      "asr_mismatches":{"type":"array","items":{"type":"object","properties":{"turn_idx":{"type":"integer"},"bot_heard":{"type":"string"},"ear_heard":{"type":"string"}}}},
      "seller_replay":{"type":"object","properties":{"actionable":{"type":"boolean"},"complaint":{"type":"string"}}}}},
    "risk_flags": {"type":"array","items":{"enum":["wrong_requirement","frustration","loop","low_confidence","human_request","dead_air","asr_suspect","topic_deflection"]}},
    "confidence": {"type":"number","minimum":0,"maximum":1},
    "needs_human": {"type":"boolean"},
    "models": {"type":"object","properties":{"extract":{"type":"string"},"ear":{"type":"string"},"seller":{"type":"string"}}},
    "cost_usd": {"type":"number"}
  },
  "$defs": {"field":{"type":"object","required":["value","status"],
    "properties":{"value":{"type":["string","null"]},"status":{"enum":["stated","inferred","missing"]},"turn_idx":{"type":"integer"}}}}
}
```
Also emit `audit/_summary.json`: counts per verdict, per param, share audited, total cost, abstain count. ISSUE groups on `params[].param` + `cause_hint` + `evidence`. TEST re-runs the same rule pass on replayed transcripts, so the contract stays identical before and after a fix.

## (f) Validation with no human labels
1. **Mutation key** (above): recall/precision per parameter on injected faults. Report as "synthetic agreement".
2. **Three samples we can already judge by eye** (282 fatal; 10 fatal on product/deflection + dead air; 133 pass). Must pass day one.
3. **Hand-label 40 calls in 90 min:** pick by uncertainty (abstain + disagreement), plus 10 random. Two people label independently. Report Cohen's kappa between us and between us and the auditor, per parameter, with n.
4. **Base-rate sanity:** the Sep'26 vendor baseline is 4% BL-fatal. If we say 30%, check for over-flagging. Note the baseline is a different call set.
5. **Stability:** re-run twice, report verdict flip rate. Target <3%.
6. **Ablation:** show one-judge baseline vs. extract-then-rule on the same 40 labels.

## (g) Open questions for organisers (Day 1)
1. Do real QA labels exist for even 30–50 of the 713 calls? Can we see them for validation?
2. Can we get the real VANI prompt and the final BL fields per call? This would replace the shadow BL.
3. Is Fatal decided per call or per parameter? Is Overall = worst parameter?
4. Are bot and user on separate audio channels in the raw system? Our mp3 is mono 8 kHz.
5. Is "one enquiry per day" a deliberate policy? Then call 10 deflection is non-fatal, but the ASR error stays Fatal.
6. Can Saaras STT output be used as the independent ear on these recordings? Is it the vendor's production ASR (conflict of independence)?
7. Do Hinglish pronunciation notes (Additional observation) count in scoring?
8. How is "share of calls audited" counted when we abstain on a call?
