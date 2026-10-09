# VANI buyer-side prompt map

Source: `data/reference/vani_prompt_sarvam.md` (3187 lines, read in full). Line refs are L<n> of that file.
Rules checked: `src/vaniloop/rules.py`. Caveat: I had no transcripts for calls 282, 10 and 133. Failure mapping below comes from the descriptions you gave, so "cause" statements are hypotheses.

## 1. Structure

The file is four prompt variants joined into one file. Each variant repeats most shared blocks.

| Lines | Variant | Use |
|---|---|---|
| L1-L1722 | Global instructions, buyer BL flow | Seller unavailable, bot builds a buy lead (L11, L29). Has `is_enrich` branches inside |
| L1723-L2645 | Enrichment | Buyer already has a lead and calls a seller; bot enriches it (L1727-L1729) |
| L2647-L3096 | Redial - No Name City | Call dropped; bot collects only name and city (L2655) |
| L3097-L3187 | Redial_AST | Straight connect to live seller (L3099-L3107) |

Top-level sections of the main variant, in order:
- Persona and limits: role, "never claim human" (L7), "cannot assure on any requirement" (L9), Hindi/English/Telugu only (L13).
- Lead Quality Mandate (L31). Collection Control (L63): central ask limits.
- Per-Turn Decision Procedure (L107), Instruction Priority (L123).
- INFORMATION TO COLLECT (L171), Tools (L225), Information Collection Rules (L255), Voice and TTS rules (L309, L357).
- Closing Rules (L367), End-Call Disposition Table (L373).
- Intent priority list (L405), Intent Catalogue (L445), Intent Handlers (L535-L1001).
- Conversation context and Collection Objective Queue (L1003-L1077).
- Phases: REQUIREMENT CONFIRMATION (L1079), REQUIREMENT COLLECTION (L1199), FINALISATION (L1589).
- CLOSING RULES, STATE GUARDRAILS, Global Collection Limit (L1679-L1721).

Jinja switch variables:
- `is_enrich` '0'/'1': extra live-seller-first block (L39); PRODUCT_CHANGE handler: full confirm-and-reload (L547) vs end the call (L593-L603); ASK_FOR_ALTERNATE_SELLER skips collection when '1' (L831).
- `ast_seller_pns` (live alternate seller phone): non-empty means live seller exists (L39, L163, L207, L1353). Some blocks use `not in ("", "0", "00")` (L1553, L1579, L1593), which is stricter.
- `quantity_unit_options`, `specification_options`: switch the quantity and spec objectives on or off (L177, L183, L1055, L1061).
- `buyer_name`/`updated_buyer_name`, `buyer_city`/`triangulation_response_*`: switch name and location objectives (L189, L195, L1067-L1077, L1467).
- `product_name == ""`: no product in context, so the confirm path changes (L673, L1085, L1097).
- `identity_revealed`, `bl_converted`: when to announce "seller is busy" (L583, L669, L1179).
- `identity_challenge_count` (>2, ==2, else) and `seller_ask_count` (>2): ASK_FOR_SELLER and ASK_FOR_ALTERNATE_SELLER (L621, L647, L831).
- `pcd_pitch`: "same seller will call you back" closing vs WhatsApp closing (L1593, L1627, L1667).
- `off_topic_counter >= 2` (L951), `lead_type == 'LIVE'` (L1771, L1853), `ast_flow_live` (L1567, L1849), `language_name == 'telugu'` (L319), `updated_buyer_name != ""` (L1347, L1397).

Phases (L1079-L1587): REQUIREMENT CONFIRMATION, then REQUIREMENT COLLECTION, then FINALISATION. The phase is "a guidance signal, not a restriction" (L1009). The only legal way back from collection to confirmation is a product change (L1703).

## 2. Slot flow

- Order: product first (confirm, L1079-L1197). Then the queue top-down: quantity, specs, name, location (L1035-L1077). The bot must finish an objective before moving on (L1051).
- Mandatory vs optional: name is "critical" (L191), quantity is "a critical field" (L305), city and state both must be confirmed (L1455). Nothing may block progression or the seller transfer (L1719, L81, L1327).
- Product confirm: one sentence with the product name plus a purpose clause. "A bare 'X chahiye, sahi?' ... is a hard violation" (L563). No tool call before the buyer confirms (L573). On AFFIRM, call `requested_another_product_tool` (L583).
- Specs: come only from `specification_options` (L185, L1279). Order follows the list (L1279). One field per turn (L1297). Offer at most two options plus "or something else" (L1297). Skip fields the product name already implies (L299). Inferred values are asked as a confirmation (L1289). There is no cap on the number of specs, only a per-field cap.
- Quantity: ask with a unit from `quantity_unit_options`, never say the word "quantity" (L1247). Ask 1: direct. Ask 2: acknowledge the concern and request an estimate. Ask 3: final request (L1249). Range: store the maximum (L1255). Budget or capacity is not a quantity (L1259). Valid answer: store it and move on without confirming (L1261).
- Name: ask with context ("before I transfer your call...", L1357, L1367). If a name is pre-filled, confirm it (L1375). Reject jokes, celebrities, numerics (L1423-L1427).
- Location: confirm triangulated city (L1477), then ask state (L1487). Both city and state are required; never infer state (L1457). Call `update_ast_buy_variables_by_city_tool` once both are known (L239-L243, L1571).
- Finalisation: if a live seller exists, offer the connect: "Shall I connect your call?" (L1603), then set `connect_ast_call`=1 and call `ast_buy_confirmed_tool` (L1607). No live seller: BL Enriched closing (L1669-L1673).

## 3. Explicit limits

Total ask limits (L69): product 4, quantity 3, each spec 2, name 3, city 3, state 3, transfer consent 2. Asks are counted per underlying field "regardless of wording, phase, or intent" (L67). A multi-field question costs one ask per field (L69). After the last ask: set `call_step`=LOOP_DETECTED, defer to the seller, move on (L75). Handlers and topic changes never reset counts (L1713).

Local limits that disagree with L69 (see section 7):
- Quantity: 3 asks (L1239-L1241). Enrichment variant: max 2 (L1947).
- Spec: initial plus one follow-up (L1281, L2257). Also "asked more than twice" triggers LOOP_DETECTED (L1325).
- Name: "maximum of 2 times" (L1431, L2371, L2813).
- City: if no response after more than 2 tries and a live seller exists, skip to finalisation (L1553-L1555).
- Live seller present: "one fair attempt" per slot (L1793, L1803). Enrichment says "STRICT MAX 3" in the heading but "at most 2" in the body (L2615, L2623-L2631).

Other limits:
- Product exhausted: BL Deleted, No Product Requirement, end call (L77).
- Transfer consent exhausted: BL Enriched, existing closing (L79).
- Off-topic: counter at 2 closes (L951-L953, L389).
- Seller requests: `identity_challenge_count` >2 ends the call (L621-L643). `seller_ask_count` >2 skips all collection and goes to transfer (L831-L837).
- Connectivity: first time ask once "were you able to hear me?"; second time end the call (L609-L611, L393).
- Silence or dead air: not specified. The only mention is "silence" as a trigger for the second connectivity strike (L611). There is no latency or wait-time rule.
- One enquiry per day (or similar): no such policy in the file.
- Name collection: see L1331-L1443 (context phrase mandatory L1367, invalid-name list L1423, correction reconfirmed once L1403-L1409).
- Closing rules: set disposition before outcome (L1683); every closing calls `end_interaction` (L1687); closings are statements, never questions (L371, L1693); do not repeat a closing (L371).
- Disposition table (L373-L393): 'BL Deleted' with 'No Product Requirement' / 'Particular Seller' / 'Job Related___BL Not Approved' / 'Selling Related'; 'BL Approved' (L1177); 'BL Enriched' (L1619); 'ENRICHMENT CALL' (L1873).
- `ast_buy_confirmed_tool` and `end_interaction` must never be triggered together (L1613). Redial_AST says the opposite (L3113-L3115).

## 4. Product change and product context

- Classifier: high bar. "If the distinction is ambiguous, assume the buyer is still referring to the current product" (L495). Not a change: variant, brand, narrower spec, synonym (L455-L471). Change: explicit replacement, different category or use (L473-L485).
- Handler (`is_enrich=='0'`, L547-L591): extract the core name (L559), confirm with a purpose clause (L563), wait (L573), on affirm call `requested_another_product_tool` with the English name (L583), on deny restart at Step 1 with no tool call (L589).
- Enrichment/`is_enrich=='1'` variant (L593-L603, L1889-L1899): confirm "do you no longer require X?", then end the call with BL Enriched and tell the buyer to call the same seller in 15-20 minutes. "Do not continue collecting requirements for the new product."
- What happens to ISQs: Collection Control says "A genuine product change starts a fresh allowance for the new product's identification, quantity, and specifications; retain buyer name/location history" (L83). The tool returns "product details" and collection continues "using the returned product details" (L235). Nothing says to discard already-collected specs from the old product. Nothing says to check that the returned `specification_options` fit the confirmed product. The prompt gives only a general "one active product at a time" (L555).
- Only one product at a time (L555). There is no handling for a second, parallel product (see call 10).

## 5. Failure mapping

**Call 282 (school bus CCTV to dome camera at turn 7, drone specs at turns 12, 14, 18)**
- The prompt classifies a change as PRODUCT_CHANGE only on explicit replacement or a different category (L453-L485). "Dome camera, mobile connect" is a narrower form of a CCTV camera and is plausibly NOT a change under L459 and L471. So the prompt is ambiguous here. If the bot did treat it as a change, L563 and L583 require a purpose-clause confirmation and then a tool call.
- Drone specs: the spec questions come only from `specification_options` (L1279). The prompt never tells the bot to check that the options match the confirmed product. It is silent on this, so a prompt diff can help. Hypothesis: the tool or MCAT mapping returned drone ISQs for "dome camera". The prompt cannot be the source of "drone". This is probably a data/tool bug plus a missing guard.
- Each drone spec was asked once at turns 12, 14, 18. If they are three different fields, this is within the L1281 limit. So it is not a looping violation. It is a category-fit violation, which matches the `category_isq` rule.
- Name asked 5 times (turns 24-32): forbidden under every version of the limit: "buyer name = 3" (L69), "maximum of 2 times" (L1431). L75 and L1435 say to continue without the name after the limit. The prompt has no frustration-specific rule. This is an execution/compliance bug, not a prompt gap. A prompt diff could only add emphasis (see section 7, item 1).

**Call 10 (second product deflected, >5 s dead-air)**
- If `is_enrich=='1'`: the prompt explicitly says to deflect a new product and end the call (L599-L603, L1895-L1899). The deflection is compliant, so the audit must not fail it. If `is_enrich=='0'`: the handler is confirm, then tool call (L555-L591). Deflecting is a violation. The audit needs the `is_enrich` flag. The ledger has no such field.
- "Adding another requirement while still wanting the current product" is explicitly not PRODUCT_CHANGE (L467), yet no instruction says what to do with the extra product. The prompt is silent here, so it is a candidate diff: record in `additional_details`, tell the buyer the seller can discuss it, or queue a second pass.
- Dead-air: the prompt has no rule (see section 3). Pure latency or infra issue, not fixable by a prompt diff beyond "after a silence, re-prompt once". The `connectivity` handler (L609-L611) only covers the buyer's silence.

**Call 133 (seller unavailable, handoff, graded Pass)**
- Seller unavailable is the normal scenario (L11, L29). The handoff is the intended end (L1603-L1607). The prompt requires closed collection first (L81, L1701), with one explicit exemption for live-seller priority (L41, L1791). So Pass is plausible for the handoff, but the ledger scope `seller_unavailable_handoff` makes `qty_*`, `product_change` all NotApplicable (rules.py L249, L265, L337). That means a handoff with no confirmed product, no name and no city also passes.
- The prompt says product confirmation (L1175-L1177), name and city (L1067-L1077) precede transfer, unless exhausted, deferred or live-seller exempt (L1793-L1803). The audit should check that precondition instead of exempting by scope. I could not confirm whether 133 had these gaps without the transcript.

## 6. Rule impact (rules.py vs prompt)

| Rule | Prompt vs rule | Line |
|---|---|---|
| `category_isq` | Confirms. Specs must come from the active product's `specification_options` and reset on a genuine product change. The "fits product" check is our own, the prompt does not state it. | L83, L1279 |
| `product_change` | Refines. Bot must confirm with a purpose clause before the tool call, and the classifier has a high bar. The rule must also branch on `is_enrich`: in enrichment, ending the call on a product change is correct. | L563, L495, L599 |
| `qty_probing` | Contradicts `QTY_ALWAYS_DEFINED=True`. Quantity is only asked when `quantity_unit_options` is non-empty. It is also skippable on refusal or seller deferral, and with a live seller only one fair attempt is needed. | L177, L1267, L1793 |
| `qty_missed` | Contradicts. The prompt says store a valid quantity "without confirmation" and never echo it. A missing spoken confirmation is not a fault. The check should look at the stored variable, not the bot's speech. | L1261, L263, L2603 |
| `qty_accuracy` | Refines. Ranges must be stored as the maximum. The rule's NonFatal "range collapsed to one value" is correct behaviour if the value is the max. Flag only when the value is not the max. | L1255, L1993 |
| `qty_unit` | Refines. Unit comes from `quantity_unit_options`, custom count units are valid, capacity or size must not be used as the unit. | L1247, L1257, L1953 |
| `looping` (LOOP_MAX_ASKS=3) | Partly contradicts. Per-slot caps differ: spec 2, name 3 (or 2), quantity 3, product 4, city 3, state 3, consent 2. A uniform "4th ask" threshold misses a 3rd spec ask and a 3rd name ask in the variants that say 2. Use per-slot caps, and treat "frustrated" as an aggravator rather than the trigger. | L69, L1281, L1431 |
| `dead_air` 5 s | Prompt is silent. Neither confirms nor contradicts. The 5 s comes from the quality matrix. | L611 only |

New auditable rules the prompt implies, ranked by value:
1. Per-slot ask caps from L69 (name 3/2, spec 2, quantity 3, product 4, city and state 3, consent 2). Replaces the uniform threshold. L69, L1281, L1431.
2. Live seller on the line: no collection loops. At most one fair attempt per slot, and no open-ended "anything else?". L1793, L53, L175.
3. Spec questions fit the confirmed product after a product change (stronger form of `category_isq`). L83, L1279.
4. Transfer preconditions: product confirmed, and name and city asked or closed before the handoff. Replaces the scope exemption. L1175, L1701.
5. `is_enrich`-aware product change: enrichment must end the call and not collect for the new product. L599-L603.
6. Product confirmation with a purpose clause before any tool call or move to collection. L563, L575.
7. No echo of captured values; one filler then next question. L263, L2603.
8. Quantity asked with a unit, never the word "quantity". L1247, L1957.
9. Closing is a statement, no question after it, `end_interaction` always called. L371, L1693, L1687.
10. No promise of the original seller's contact details; bot never claims to be human or to assure a requirement. L747, L7, L9.

## 7. Prompt weaknesses (targets for the Fix stage)

1. Name limit contradiction: 3 (L69), 2 (L1431, L2371, L2813), heading "STRICT MAX 3" but body 2 (L2615, L2623). Relevant to call 282. Fix: one number, one place.
2. Product allowance: "foure-ask" 4 (L69) vs "five-ask" (L977). Typo in L69 too.
3. Quantity: 3 asks (L1239) vs 2 asks (L1947) vs one attempt with a live seller (L1793). Also "do not skip quantity solely because the buyer requested a seller" (L2037, L2243) vs "never delay seller connection for quantity" (L41, L49).
4. Duplicated blocks that have drifted: CLOSING RULES / STATE GUARDRAILS / Global Collection Limit (L1679, L3053, L3159; also L367 and L2595); FINALISATION (L1589, L2523, L2963, L3099); name collection (L1331, L2271, L2713); location (L1447, L2387, L2829). Real drift: ambiguous location skips city after 2 tries in one copy (L1553-L1555) and "do not proceed until the city is known" in others (L2491, L2931).
5. Tool vs end_interaction: "never trigger together" (L1613, L2987) vs "immediately call end_interaction" after the tool (L3113-L3115).
6. Product change gaps: no instruction to drop old-product specs, no check that returned `specification_options` fit the confirmed product (L235, L83), no handling for a second product mentioned beside the first (L467, L555). The high bar (L495) can keep "dome camera" under "CCTV camera". Targets for calls 282 and 10.
7. Live-seller flag inconsistent: `ast_seller_pns != ""` (L39, L207, L1353) vs `not in ("", "0", "00")` (L1553, L1579, L1593). A value of "0" counts as a live seller in some branches and not in others.
8. Undefined references: "Loop Guard" (L591), "Adaptive Follow-up Strategy" (L1047, L1701) is only defined as a header in the enrichment variant (L2615) and as "Global Collection Limit" (L1709); `current_phase` values differ ('requirement_collect' L583 vs 'REQUIREMENT COLLECTION' L1177). Also no dead-air rule at all (L611 is the only silence mention).
