# 05 - Sarvam platform research (for PS1)

Source: public docs at docs.sarvam.ai, read 8 Oct 2026 via the page summaries of llms.txt indexes. Several answers come from a summarizer, not raw pages. Anything marked "not confirmed" must be checked on Day 1. Nothing was signed up for. No call data was sent.

Indexes: https://docs.sarvam.ai/conversations/llms.txt (Voice Agents), https://docs.sarvam.ai/api/llms.txt (model APIs), https://docs.sarvam.ai/api-reference/llms.txt (API reference).

## Verdict
Sarvam already ships a test-and-measure loop for Voice Agents: simulated-caller tests with an AI judge, a Goal metric, call logs and dashboards. It does NOT group failures across calls and does NOT suggest prompt fixes from calls. That gap is exactly PS1. We build the audit and root-cause layer on top, and we call Sarvam's Tests API as the "test the fix" engine.

## 1. Voice Agents
- Build: a canvas in the Build UI. Draft, then commit an immutable version, then deploy. Flow is "draft, test, deploy, measure, improve". https://docs.sarvam.ai/conversations/build/overview.md , https://docs.sarvam.ai/conversations/build/agent/versioning.md
- Prompt: the "Instruction" block, plain text with sections (Persona, Objective, Context, Guardrails) and inline variables. Genie (built-in AI) can draft or rewrite it and flags issues. Genie does not analyse calls. https://docs.sarvam.ai/conversations/build/system-prompt.md , https://docs.sarvam.ai/conversations/build/genie.md
- Update prompt by API: the REST docs do NOT show it. Not confirmed. BUT the hosted Voice Agents MCP server can "create, clone, configure agents (prompt, variables, tools, knowledge, voice, settings) and commit immutable versions". Mutations need approval. OAuth via Sarvam SSO, token valid 24 h. This is the likely programmatic path for "apply the fix". https://docs.sarvam.ai/conversations/mcp.md
- Test calls: a "test agent" panel in the browser where you speak to the agent. https://docs.sarvam.ai/conversations/quickstart.md
- Simulated tests (key feature): a Test Case = a user scenario (who the caller is, goal, behaviour) + 3-5 yes/no expected behaviours. A simulated user talks to the agent. An AI judge marks each behaviour Pass or Fail with a reason. Global guardrails are one-line rules graded on every case. https://docs.sarvam.ai/conversations/build/tests.md , https://docs.sarvam.ai/conversations/build/tests/write-test-cases.md
- Tests API: base https://apps.sarvam.ai/api/evals/v1/{org_id}/{workspace_id}/test-suites . Create suite and cases, map suite variables to agent variables, run against app_id + app_version (returns 202 + run_id), poll, fetch transcripts and per-behaviour verdicts. Limits: 60 req/min per key, 10 suite runs per 5 min (org-wide), 20 single-case runs/min. No delete, no cancel, no AI-generated cases, no export. https://docs.sarvam.ai/conversations/api/tests/overview.md
- Not confirmed: whether the simulated user is text-only or speaks audio. Not confirmed: whether the simulated user can be Hinglish-tuned.
- Web / text / SDK: Python SDK `sarvam-conv-ai-sdk` (async sessions), Web TypeScript SDK `sarvam-conv-ai-sdk/browser` (mic capture), React Native, Flutter. A session needs org_id, workspace_id, app_id and a user identifier. Voice only. No text-only mode documented. Headless mode: push raw 16-bit PCM yourself. A WebSocket interface exists. An embed widget is mentioned, details not confirmed. https://docs.sarvam.ai/conversations/deploy/deploy-with-code.md , https://docs.sarvam.ai/conversations/deploy/sdks/python.md
- Telephony: bring your own (Exotel, Twilio, Smartflo, Pulse, Intalk, Vobiz, Plivo) or rent a number from Sarvam. Inbound, campaigns, instant outbound API. https://docs.sarvam.ai/conversations/deploy/telephony.md
- Agent ID: the `app_id` in the API paths (agent = "app"). Found in the dashboard URL or settings, with org_id and workspace_id. The submission asks for the "Sarvam agent ID", so use app_id plus the committed app_version. Exact UI location not confirmed. https://docs.sarvam.ai/conversations/api/introduction.md
- Runtime models: ASR is Saaras v4 (fixed). LLM is auto-routed by Sarvam (you cannot pick it). TTS is Bulbul v3 or v4, chosen per agent. https://docs.sarvam.ai/conversations/build/models.md
- Tools: API tool, code tools, call transfer, mocking a tool, knowledge base, on-start/on-end hooks, multi-state flows. Useful for a mock VANI that "posts a BL". https://docs.sarvam.ai/conversations/build/tools.md

## 2. Cowork
An agentic workspace at indus.sarvam.ai/indus. Chat for ad-hoc tasks, plus reusable agents that run on demand or on a schedule, and Connectors (Gmail, Slack, GitHub, Notion). No-code. https://docs.sarvam.ai/cowork/overview.md , https://docs.sarvam.ai/cowork/build/creating-an-agent.md
Use: a scheduled "daily QA digest" agent that mails or posts the top root causes. Low value for the core pipeline. Not confirmed: API access, code execution, or Voice Agents integration. Treat as an optional demo garnish.

## 3. Saaras STT
- Models: Saaras v4 (default, new) and v3. Legacy Saarika exists. https://docs.sarvam.ai/api/getting-started/models/saaras.md
- Languages: 22 Indian languages plus English, auto language detection (returns `language_probability`).
- Modes (`mode`): transcribe, translate, verbatim (keeps fillers and repeats), translit (Roman script), codemix (English in English, Indic in native script). For Hinglish, `translit` or `codemix` is the one to test. Which is best on real VANI audio: not confirmed.
- Batch API: up to 2 h per file, 20 files per job, diarization up to 20 speakers, chunk-level timestamps only (no word-level), webhook on completion. Calls here are max 387 s, so batch works. https://docs.sarvam.ai/api/api-guides-tutorials/speech-to-text/batch-api.md
- REST: 30 s max per request. Streaming: WebSocket.
- Confidence: only language probability is documented. Per-word ASR confidence not confirmed. (The Voice Agents Log Analyser shows ASR confidence per turn, but only for calls made on the platform.) https://docs.sarvam.ai/conversations/monitor/agent-analytics/log-analyser.md
- Keyterms (custom vocabulary): `keyterms`, up to 50 terms, 64 chars each, v4 only, works on REST, batch, streaming. Bias only, no guarantee. Use for IndiaMART product names and units. https://docs.sarvam.ai/api/api-guides-tutorials/speech-to-text/how-to/keyterms.md
- 8 kHz telephony: not explicitly confirmed for STT. Docs say raw PCM must be 16 kHz and other formats are auto-detected. Our mp3s are mono 8 kHz, so test one file on Day 1.
- Use for us: an independent second transcript to compare with Gemini 3 Flash. Disagreement on a number or product word flags a possible WER fatal.

## 4. Bulbul TTS
- v3 (current) and v4 (listed in Voice Agents). 30+ voices (e.g. Shubh default, Priya, Aditya, Ritu). 11 languages: 10 Indian plus English. Code-mixed English plus Indic supported. https://docs.sarvam.ai/api/getting-started/models/bulbul.md
- v3: pace 0.5-2.0x. No pitch or loudness control in v3 (those are v2 only). No SSML. Pronunciation dictionary API exists, good for "BL", "MCAT", brand names. Sample rate 8-48 kHz (8 kHz for telephony). Streaming max 24 kHz.
- REST limit 2,500 characters per request. HTTP stream and WebSocket available.
- Latency numbers: not confirmed in what I read. Measure on Day 1.

## 5. Sarvam LLMs
- `sarvam-105b` (128K context, reasoning on by default, MoE) and `sarvam-105b-conversations` (32K, tuned for voice). Open-weight models also hosted (GLM-5.3, Gemma 4 31B, DeepSeek V4 Flash). Sarvam-30B and Sarvam-M are deprecated. https://docs.sarvam.ai/api/getting-started/models/sarvam-105b.md
- OpenAI-compatible chat completions at /v1 and /v2 (v2 is beta and adds open-weight models). JSON schema output via `response_format`. Tool calling supported. Streaming supported. Bearer auth works. https://docs.sarvam.ai/api/api-guides-tutorials/chat-completion/overview.md
- Set `reasoning_effort=None` or raise `max_tokens`, or reasoning eats the output budget.
- DATA RULE: call data may go only to Claude, OpenAI or Gemini. GLM and DeepSeek are banned for call data, so never select those hosted models. Is sending call data to sarvam-105b allowed? Not covered by the brief. Ask organisers (they are the hackathon vendor, so likely yes).

## 6. SDKs, auth, limits, pricing
- Python `pip install sarvamai`, JS `npm install sarvamai`. Env var `SARVAM_API_KEY`. Header `api-subscription-key` (model APIs) or `X-API-Key` (Voice Agents APIs). https://docs.sarvam.ai/api/getting-started/sdks.md , https://docs.sarvam.ai/api-reference/authentication.md
- Rate limits are per account, tiered. STT REST 60-4,000 req/min, TTS 60-1,000, chat 60-1,000, sarvam-105b only 40-120. 429 on overflow. https://docs.sarvam.ai/api/getting-started/ratelimits.md
- Pricing (INR): STT Rs 30-45 per hour (diarization costs more). TTS Rs 30 per 10K characters. LLMs Rs 19.80-126 per million input tokens. New users get Rs 100 credit. https://docs.sarvam.ai/api/getting-started/pricing.md
- Budget check: 831 min of audio is about 13.9 h, so about Rs 420-630 for STT. The free Rs 100 will not cover it. Ask for hackathon credits.

## 7. Built-in evals, analytics, QA (do not rebuild)
| Feature | What it does | Gap we fill |
|---|---|---|
| Tests (sim caller + AI judge) | Pass/Fail per behaviour with reasons, via UI and API | We generate cases from failure clusters, then score before/after |
| Goals | One rule-based success metric per agent, evaluated on live and test calls | Only one goal. Not the 15-parameter VANI rubric |
| Agent Analytics, Boards, Call Logs | Connectivity, engagement, tools, group-by, SQL dashboards, CSV export | Only for calls made on the platform |
| Log Analyser | Single-call turn inspector (ASR text, audio, confidence, LLM and tool I/O) | Docs say it does NOT group causes or suggest fixes |
| Analytics API | attempts, interactions, recordings, transcripts endpoints | Platform calls only |
| Genie | Edits and reviews prompts | Does not read calls |

Key point: no documented way to import our 713 external recordings into Call Logs ("upload/import not covered"). So the audit stays in our own pipeline. Sarvam covers the replay and test half. https://docs.sarvam.ai/conversations/monitor/agent-analytics/call-logs.md , https://docs.sarvam.ai/conversations/monitor/agent-analytics/goals.md
There is also a Call Analytics cookbook (batch STT with diarization, then LLM summary). It is a toy version of step 1. https://docs.sarvam.ai/api/cookbook/guides/call-analytics-pipeline.md . Do not copy it as-is, since pre-built solutions are disqualified. Read it only for API shape.

## 8. How PS1 scores on "voice experience" (20%)
1. Voice replay of the failure and the fix. Take a bad call (282 product-change loop). Build a "simulated buyer" from the real buyer's lines. Render buyer turns with Bulbul (Hindi voice, pace about 0.9-1.0, 8 kHz). Render bot turns of the revised prompt with Bulbul. Play before/after side by side in the dashboard. Judges hear the fix.
2. Run the fix on a real Sarvam Voice Agent. Create a VANI-like agent, load the revised prompt as a committed version, run the Tests API with our failure-derived cases against the old and new version. Report pass rate before/after. The agent ID in the submission then points at something real.
3. Live demo moment: speak Hinglish into the browser test panel to the fixed agent, and show the Log Analyser turn that used to loop now passing.
4. Use keyterms (product names, units like "piece", "kg") and the Bulbul pronunciation dictionary to show we tuned for Indian speech.
5. Dead-air and latency metrics from Gemini timestamps, plus Sarvam's own response-time fields if we run live calls.

## 9. Open questions for organisers (Day 1)
- Can we update an agent prompt by API or only by MCP/UI? Is `app_id` the "agent ID" to submit?
- Is the Tests simulated caller voice or text? Can its persona speak Hinglish?
- Are hackathon credits and higher rate limits provided (Tests: 10 suite runs per 5 min is tight)?
- Is calling sarvam-105b with IndiaMART call data allowed under the data rule?
- Can external recordings be imported into Call Logs or Boards?
- Does Saaras accept 8 kHz mp3 well, and which mode best handles Hinglish?
- Is the real VANI prompt or agent shared, or do we build our own?
