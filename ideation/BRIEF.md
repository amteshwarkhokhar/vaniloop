# Shared brief — Voice AI Hackathon 2.0, PS1 Quality Audit & Fix Loop

## Event
IndiaMART internal hackathon, 9–10 Oct 2026, 9 AM–6 PM IST, in office. Teams of 3 (1 non-tech). Build on Sarvam AI platform (Saaras STT, Bulbul TTS, Sarvam LLMs, Voice Agents). Submission: 10 Oct 11:59 PM IST: 5–7 min demo video, code + README, skills.md (build journey), Sarvam agent ID, approach note.
RULE: pre-built solutions are disqualified. Today (8 Oct) we DESIGN only. No production code.

## The product being audited
VANI = IndiaMART AI voice bot. It calls buyers who showed intent (searched/enquired a product) to confirm the requirement and post a Buy Lead (BL): product, quantity + unit, specs (ISQs = category-specific spec questions), additional details (BFD), city/state, name. Then it sends seller details on WhatsApp. Variant "OnCall AstBuy": bot connects buyer to a seller live.

## PS1 task (official)
Pipeline on call recordings: (1) auto-grade EVERY call Fatal / Non-Fatal with reason + evidence (transcript snippet/timestamp); (2) group failures into root causes (name, one-line desc, example calls; type = prompt / ASR / knowledge gap / flow-logic); (3) rank by frequency x severity with a transparent priority score; (4) concrete prompt fix (diff) per top cause; (5) TEST the fix — replay failed transcripts/scenarios against revised prompt with a simulated caller, or held-out calls; report failure rate before/after and time issue→tested fix; (6) flag risky calls: wrong requirement, frustration, loops, low confidence, human request; (7) one dashboard: audit → issue → fix → test.
PS1 scoring: agreement with human auditors 25%, root-cause grouping+ranking 25%, failure-rate drop after fix 25%, time issue→tested fix 15%, share of calls audited 10%.
Overall hackathon scoring: business impact 25%, completeness 20%, technical robustness 20%, voice experience 20% (natural, low-latency, multilingual/Hinglish), demo + skills.md 15%.

## Official VANI audit rubric (from QA team) — data/reference/vani_quality_matrix.png
Fatal definition (QA lead, 7 Oct): error that directly impacts the seller via the buyer info captured in the BL/call (wrong product, quantity, unit, details). Non-Fatal: does not materially impact the seller/BL usability.
Parameters: Outcome accuracy (buying intent; always Fatal) · Qty value (Fatal if value effect; 10–20 diff non-fatal) · Qty unit (similar units non-fatal) · Qty probing (Fatal if qty defined and not asked; non-fatal if not defined and asked) · Qty missed (Fatal; non-fatal if voice unclear) · Spec1/Spec2 accuracy (X captured as Y with BL impact) · Spec missed (spec1/2 not captured = Fatal; spec3 = non-fatal) · Looping (probing a parameter >1+2 times; Fatal if buyer irritated → disconnection or missed qty/spec) · WER (Fatal if BL-impacting word) · Additional details/BFD (grossly wrong = Fatal; duplicate/generic = non-fatal) · OnCall AstBuy (seller present but not pitched = Fatal; disposition wrong = non-fatal) · Product change/accuracy (wrong product approved = Fatal) · Category ISQ error (contradicting/confusing ISQs) · MCAT/search error (wrong MCAT) · Dead air (>5 s, >8 s if noisy) · Overall call · Additional observation (e.g. pronunciation).
Baseline (Sep'26, Sarvam-vendor calls): 95% pass, 4% BL fatal.

## Data
- data/raw/call_recordings/: 713 mp3, mono 8 kHz, 831 min total. Median 64 s, p90 130 s, max 387 s. 74 calls < 10 s.
- transcripts/<stem>.json: Gemini 3 Flash transcripts {call_id, model, language, duration_sec, turns:[{speaker: bot|user, start_sec, end_sec, text}]}. Roman-script Hinglish. Timestamps coarse (~1 s). Only 3 exist now; full 713 run pending.
- NOT available (yet): human audit labels, the VANI bot prompt, captured BL fields per call. FAQ allows hand-labelling a subset. Fixes may be written against an inferred prompt.

## Failures already seen in 3 sample transcripts
- 282: product changed (school-bus CCTV → home dome camera) but bot asked DRONE ISQs (DJI brand, 5.1K, flight time) → category ISQ / MCAT error. Name loop: user said "BuyerA" 4x, bot kept asking, said "pin code se pehchaan nahi kar sakti". Non-sequitur "main Hindi mein hi baat kar rahi hoon".
- 10: user asked for 2nd product, bot deflected with "one enquiry per day". Dead air: user says "Hello?" / "Aap sun nahi paa rahe?" (gap ~6.5 s).
- 133: clean 21 s seller-unavailable handoff (pass).

## Data rules
Call data may go only to Claude, OpenAI/Codex, or Gemini. Never to GLM/DeepSeek. Do not copy audio or transcripts outside this folder.

## Output standard for every ideation doc
Plain English, short sentences. For your component: (a) what the judges score here and what "great" looks like; (b) 3+ distinct approaches with pros/cons, build hours for a 3-person team, risk; (c) your recommendation and why; (d) the unique edge that most teams will miss; (e) inputs/outputs contract (JSON schema) so components connect; (f) how we validate it with no human labels; (g) open questions for organisers on Day 1. Max ~1200 words.
