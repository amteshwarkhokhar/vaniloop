# Sarvam VANI replica (draft, text replays only)

Built 2026-10-09 ~12:20-12:30 IST via the sarvam-voice-agents MCP. Nothing deployed, committed, called or touched in production.

## Agent
- Name: "VANI replica (vaniloop test, do not deploy)"
- Agent id: VANI-replic-7f077343-8b50
- Version: 1, status draft (never committed). send_chat works on the draft with app_version=1.
- Voice: "Aparna - Support Agent" (female, Hindi, internal_id 01a0cf16-8d27-75e7-a4f8-f71f57f86443). Initial language Hindi, English also supported (platform defaults, LID on).
- Prompt: main buyer BL flow only, source lines 1-1722, in the single state "start" (this workspace allows one state, so no prompt_scope).
- Intro message set to: "Hello BuyerA ji, aapko school bus CCTV camera for outdoor ki requirement hai na?" (modelled on the opening in transcripts/282). The real opening text is not in the prompt file; this is my invention.
- Existing agent "Voicebot outbound" and everything else in the workspace: untouched.

## Variables (38 total, all type string)
Input (11 from the prompt + 5 found by a second pass), defaults:
- product_name = "school bus CCTV camera for outdoor"
- buyer_name = "BuyerA"
- quantity_unit_options = ["Piece"] (JSON string)
- specification_options = "{}" (empty, so no spec collection)
- buyer_city = "", ast_seller_pns = "" (no live seller), triangulation_response_city = ""
- is_enrich = "0", ast_flow_live = "false", language_name = "hindi", triangulation_response_value = "0"
- bot_name = "Priya" (INVENTED; production value unknown), seller_name = "", ast_seller_company = "", ast_seller_city = ""
- initial_message = same text as the intro above
Outcome / working (agent-updatable, each with a post-call extraction question):
- buyer_disposition, call_outcome, call_step, current_phase, additional_details, image_reference, connect_ast_call
- quantity, quantity_unit, specifications, updated_buyer_name, updated_city_name, updated_state_name
- turn_intent, ast_call_remark
- Counters/flags the prompt branches on: bl_converted="0", identity_revealed="0", identity_challenge_count="0", seller_ask_count="0", off_topic_counter="0", pcd_pitch="", quantity_asked_count="0"
Removed the template's user_name and call_summary.
Not defined: lead_type, buyer_state etc. (not in lines 1-1722; they belong to other variants).

## Tool stand-ins (webhook tools with mock responses, no real endpoint)
All three use url https://example.invalid/mock/... with mock_config.enabled=true and a fixed mock body, enabled on state "start". The mock path was NOT exercised in the smoke test (no tool fired), so "mock really bypasses the network" is unverified.
- requested_another_product_tool(product_name): returns "Product details loaded. Continue the requirement collection with the product in context."
  Production differs: loads the product's real ISQs (specification_options, quantity_unit_options) and product context. The stand-in loads nothing, so a PRODUCT_CHANGE replay keeps the old options and never gets new ones.
- update_ast_buy_variables_by_city_tool(city, state): returns "Location noted. Continue with the next step of the flow."
  Production differs: looks up live-seller availability and sets the ast_* variables (ast_seller_pns etc.). Stand-in sets nothing.
- ast_buy_confirmed_tool(buyer_confirmed): returns "Assisted buy confirmed. Conclude the call politely."
  Production differs: starts the assisted-buy transfer and ends the call itself. Stand-in connects nothing and does not end the call.
- connect_ast_call is a variable, not a tool. "transfer" in the prompt is plain prose, no tool. end_interaction is platform-owned and exists.

## Deviations from the source prompt
1. Unescaped Google-Docs Markdown escapes (\_ \* \[ etc.) and removed 306 U+FEFF characters.
2. Doc-chip variable reads (BOM-wrapped, no "@", e.g. product_name, bot_name, turn_intent, initial_message) became Jinja {{name}}. Needed so values render; plain tokens would have stayed literal. 4 read-refs glued to the next word in the source ("bot_namefrom", "seller_nameor", "product_nameand") got a space.
3. Tool references "@ xxx_tool" became "tool:xxx_tool" (platform syntax). Writes "@ variable" kept as is.
4. Platform allows only "##" headings. The source already uses only "##" (18 headings), so no heading edits were forced.
5. Jinja ({% if %}, |int, elif) kept verbatim; the platform accepted it. Not flattened. Whether it renders at runtime is only inferred from the replies (below), not seen: the trace has no rendered prompt.
6. Hand-copy slips found by diffing the stored prompt against my processed source (the only 2 non-whitespace differences out of 861 lines): "Calltool:" vs "Call tool:" (intended fix) and "move to the next eligible field" became "move on to the next eligible field" in the last Global Collection Limit paragraph (my typo, not fixed).
7. Source quirks kept: "foure-ask", "five-ask" vs 4, name limit 3 vs 2, and the stray "]" after "whenever appropriate.]".

## Smoke test (draft v1, interaction 20261009/2fdb53c0-12:29:24-81c90648, 4 turns)
1. Buyer "Haan ji, bilkul chahiye." Bot (Hindi/Hinglish): seller busy, will transfer to best seller, asks how many pieces.
2. Buyer "2 piece". Bot: asks city and state (no echo of the quantity).
3. Buyer "Pune, Maharashtra". Bot: confirms "Pune, Maharashtra, sahi hai na?"
4. Buyer "Haan sahi hai. Mera naam BuyerB hai." Bot: closes with the BL Enriched closing ("requirement note ho gayi hai... WhatsApp par seller details") and calls end_interaction.
Behaviour matches the prompt for these defaults (is_enrich 0, no live seller, name prefilled so no name ask, specs empty). No ast tools fired, as expected with ast_seller_pns "".
Not verified: any tool mock, PRODUCT_CHANGE, specs, the live-seller branch, Telugu branch.

## Trace fields (analytics operation='chat', id=interaction id, params.app_id)
Top level: interaction_id, lifecycle_events[], turns[], id.
- lifecycle_events[]: event_type (ServerToolEvent.on_start / on_end), display_name, status, input, output.
  - on_start.input.on_start_input: agent_variables (all 38 with values at start), internal_variables (is_test_run), user_information, sarvam_variables (current_day/date/time/datetime, start_datetime, language_name).
  - on_start.output: app_metadata (app_id, app_version), agent_variables, initial_bot_message, multilingual_initial_bot_message.language_text_mapping (per language), on_start_mode.
  - on_end.input.on_end_input: agent_variables (final), internal_variables.interaction_end_reason (e.g. agent_request), sarvam_variables, interaction_transcript.interaction_transcript[] (role, en_text, indic_text; in my run it held only the last agent line).
- turns[]: turn_id, is_merged, events[].
  - events[] type ServerChatLLMEvent.assistant: input.text (the typed buyer text), input.llm_input{summarize_mode, language_code, llm_trace_id, return_text}; output.text.audio (the reply text), output.text.tool_calls[] {id, type, function.name, function.arguments (JSON string)}; status.
  - events[] type ServerToolEvent.<tool_name> (seen: end_interaction): input.args, output.text, status.
Not present: ASR text or confidence (text chat has no ASR), the rendered prompt, model name, token counts, latency or timestamps per turn, intermediate "thinking", variable updates per turn.

## What blocks faithful replays
1. No per-interaction variable injection: send_chat starts from the agent's defaults. One agent = one scenario. Different calls (name, product, quantity options, ast_seller_pns, is_enrich, buyer_city, triangulation city) need either a config write between replays (race-prone, one write at a time, edits the draft) or one agent per scenario.
2. allow_mid_call_variable_update is false and is immutable after creation. In the trace, agent_variables at on_end equal the start values, so the "@ variable = x" writes in the prompt have no effect. Counters and flags the prompt branches on (quantity_asked_count, identity_challenge_count, seller_ask_count, off_topic_counter, pcd_pitch, bl_converted, identity_revealed, current_phase) are frozen at defaults for the whole call. Effects: the "seller is busy" reveal can repeat each turn, the ">2 asks" end-call branches cannot trigger via counters, PRODUCT_CHANGE/identity logic that depends on updated values will not match production. To change this I would need to create a new agent with the flag set at creation; I made only the one agent allowed.
3. The post-call extraction (post_interaction_prompt) of outcome variables is not visible in the chat trace, so call_outcome / buyer_disposition cannot be read per replay. Read them from the bot's closing text and end_interaction.
4. Tool results are mocked and set no variables, so ISQ loading (specification_options after a product change) and live-seller branches are not reproducible. The ISQ-mismatch failure (call 282) cannot be replayed faithfully; only the prompt's reaction to a given option set can.
5. The intro (initial_message) is delivered by the platform and is not a turn in the trace. Whether the LLM sees it as context is not shown. The Hindi intro translation also contains a "{{BuyerA}}" artefact from auto-translation.
6. Production LLM, temperature (0.5 here) and TTS/ASR settings may differ. No model selection is available in this workspace. Text replays skip ASR errors and latency, so dead-air and mis-hearing failures are out of scope.
7. bot_name ("Priya") and initial_message are guesses.

## v2 attempt (2026-10-09, IST afternoon): mid-call variable updates could NOT be enabled
- Goal: second draft agent with allow_mid_call_variable_update=true, then counter check and a tiny eval run. Stopped at step 1 per the stop rule.
- Clone path: configure_agent(clone_from=VANI-replic-7f077343-8b50, config={llm_config.agent_config.allow_mid_call_variable_update: true}) was refused: "allow_mid_call_variable_update is immutable after agent creation". The platform clone had already been created before the overlay was refused.
- Side effect: stray draft agent VANI-replic-0dd220cc-43c3, named "VANI replica v2 (vaniloop test, do not deploy)". It is a plain copy of v1 (same prompt with the typo, same 3 mock tools: clones DO carry tools, same 38 variables). Flag is false. No delete-agent tool exists in the MCP, so it stays until someone removes it in the console. Not committed, not deployed.
- Plain create path not tried: the tool description says create = clone template, then apply config as an update, which hits the same refusal; a failed try would leave another stray agent.
- Flag state elsewhere (read only): "Voicebot outbound" = false. So no existing source agent carries it, and cloning cannot inherit it.
- Not done: counter verification (name refused 3 times), test suite and eval run (no suite created), typo fix.
- To unblock: someone with console access creates a draft agent with the flag on (create-time setting), then I overwrite config, tools and prompt on it via the MCP (writes after creation are fine).

## Eval shape (sandbox agent VANI-replic-0dd220cc-43c3, flag still false, 2026-10-09 ~12:38 IST)
- Typo fixed on the sandbox clone: stored prompt now equals the source except the "Call tool:" spacing fix (diffed, 861 lines, 1 difference). Agent v1 prompt (VANI-replic-7f077343-8b50) still has the typo; left as is.
- Suite: 01M4FQP7FTZFDRCHX5PWF3CB05 ("VANI replica v2 eval smoke"), bound to the sandbox agent. Scenario: 01M4FQP7H8KWG3H595DRGJSRCB ("Name refused every time", category name_collection, max_turns 20).
- Suite create needs: app_id, name, test_cases[] {name, category, user_scenario, expected_behaviors[{name, description}], variable_overrides[{name,value}], max_turns}, optional variables[{name,value}]. The create reply is thin (ids only); read back with evals get_suite.
- Variable mapping: the suite variable buyer_name had to be mapped to the agent variable with configure_test_suite(mapping={"buyer_name":"buyer_name"}, app_id, app_version=1). Reply: changed ["mapping"], map_id 01M4FQPH9WJBMGRH0QATNN1Q1V.
- variable_overrides worked: the run's on_start agent_variables show buyer_name "" (default is "BuyerA"), and the bot asked the name.
- Run: run_eval kind=scenario frequency=2, app_version=1 (draft). run id 01M4FQVXQ7DBSWFPHNR828M8FH. Reply gives only the run id.
- Polling: evals get_run needs params.kind="scenario" and app_id. Without test_case_id it returns the summary: run_id, test_suite_id, is_single_case, status (running, then completed), total_test_cases (2 = frequency), total_test_cases_completed, total_pass, frequency, test_case_summaries[{test_case_id, test_case_name, total_eval_pass, total_runs}]. Completed = 2/2 pass.
  With test_case_id it returns results[] (one per frequency index). evals list_runs on the suite stayed empty for this scenario run.
- Per-run result fields: run_id, test_suite_id, test_case_id, frequency_index, test_case_name, interaction_id, transcript[], evaluation_results[].
  - transcript[] items: role (assistant/user), content, debug_logs (null for the intro and user turns; for agent turns a list of ServerChatLLMEvent.assistant / ServerToolEvent.<tool> events with input, output.audio, output.tool_calls[], and for tools input.args, output.message_to_llm, message_to_user, break_chat_agent_loop).
  - evaluation_results[] items: name (the behaviour name), verdict ("Pass"), reasoning (free text). No numeric score, no cost or credits field.
- Grader reasoning, example (run 1): "The agent asked for the user's name only once, and when the user declined, it skipped asking again." Both runs passed both behaviours (run 0 asked the name twice, run 1 once; both then asked city/state and closed with the BL Enriched closing).
- Timing: simulated user is an LLM (Hinglish, varied wording per run). One run took about 31 s (interaction 12:38:33 to 12:39:04 IST). The 2 runs ran one after the other, status showed 1/2 complete about 3-4 minutes after start and completed shortly after, so allow 5-6 minutes for frequency 2.
- Trace readable per run: yes. analytics(operation='chat', id=<interaction_id>, params.app_id) works for the eval interaction (e.g. 20261009/311d0be9-12:38:33-92a3769b). Same fields as v1, plus user_information.identifier "evals@sarvam.ai" and turn 1 (intro) with empty events.
- Findings that affect replica fidelity:
  1. The "mock" webhooks are NOT mocked at runtime: update_ast_buy_variables_by_city_tool called out and returned message_to_llm "[Errno -2] Name or service not known" (DNS failure on example.invalid). The bot ignored the error and closed normally. So mock/mock_config is ignored; the stand-ins are real HTTP calls to a dead host. Harmless (no data sent to a live system) but a tool result is always an error. A replica needing a successful tool reply needs a reachable URL or a validator-style tool.
  2. The agent variable buyer_name is "" in this run, but the intro still renders "नमस्ते  जी": the Hindi intro translation has a "{{BuyerA}}" placeholder that renders empty. The English intro is fine.
  3. Counters stay frozen (flag false): turn-by-turn the agent_variables at on_end equal the start values.
  4. Without the flag the bot still obeyed name limits in this run (asked 1-2 times, moved on). That is model behaviour, not counter-driven.
