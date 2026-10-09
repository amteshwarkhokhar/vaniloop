"""Fork-the-call replay with a stub LLM. Synthetic calls only, no network."""

from vaniloop.models import BotTurnLabel, CallRecord, Ledger, Turn, UserTurnLabel
from vaniloop.replay import buyer_brief, replay, replay_result
from vaniloop.rules import audit_call


def mk_call() -> CallRecord:
    texts = ["Namaste, kya chahiye?", "CCTV camera", "Kitni quantity?", "2 piece", "Aapka naam?", "BuyerA"]
    turns = [Turn(idx=i, speaker="bot" if i % 2 == 0 else "user", start_sec=i * 6.0, end_sec=i * 6.0 + 6, text=t)
             for i, t in enumerate(texts)]
    return CallRecord(call_id="c1", duration_sec=36.0, turns=turns)


def mk_ledger() -> Ledger:
    return Ledger(call_id="c1", scope="bl_collection", final_product="CCTV camera",
                  buyer_facts={"quantity": "2 piece", "name": "BuyerA"},
                  user_turns=[UserTurnLabel(idx=1, frustrated=0.2), UserTurnLabel(idx=5, frustrated=0.7)])


def stub(bot_lines, user_lines=None, log=None):
    """Bot lines are (text, end_call), user lines are (text, hang_up). The last bot line repeats; user lines cycle."""
    state = {"b": 0, "u": 0}
    user_lines = user_lines or [("haan", False)]

    def llm(system, user, **kw):
        if log is not None:
            log.append((system, user, kw.get("cache_tag")))
        if "end_call" in system:
            t, e = bot_lines[min(state["b"], len(bot_lines) - 1)]
            state["b"] += 1
            return {"text": t, "end_call": e}
        t, h = user_lines[state["u"] % len(user_lines)]
        state["u"] += 1
        return {"text": t, "hang_up": h}

    return llm


def brief():
    return buyer_brief(mk_call(), mk_ledger())


def test_buyer_brief_is_pure_code():
    b = brief()
    assert b.goal == "CCTV camera" and b.facts["name"] == "BuyerA" and b.temper == 0.7 and b.language == "hinglish"


def test_prefix_kept_verbatim_then_alternates_with_increasing_idx():
    call = mk_call()
    out = replay(call, 4, "PROMPT", brief(), max_turns=4, llm=stub([("naam?", False)]))
    assert [t.model_dump() for t in out.turns[:4]] == [t.model_dump() for t in call.turns[:4]]
    assert [t.speaker for t in out.turns[4:]] == ["bot", "user", "bot", "user"]
    assert [t.idx for t in out.turns] == list(range(8))
    assert out.call_id.startswith("c1__replay_") and out.call_id.endswith("_0")
    assert out.duration_sec == 8 * 6.0


def test_stops_on_end_call_hang_up_and_max_turns():
    call = mk_call()
    r = replay_result(call, 4, "P", brief(), llm=stub([("naam?", False), ("dhanyavaad", True)]))
    assert r.ended_by == "end_call" and r.call.turns[-1].speaker == "bot" and len(r.call.turns) == 7
    r = replay_result(call, 4, "P", brief(), llm=stub([("naam?", False)], [("bye", True)]))
    assert r.ended_by == "hang_up" and r.call.turns[-1].speaker == "user" and len(r.call.turns) == 6
    r = replay_result(call, 4, "P", brief(), max_turns=3, llm=stub([("naam?", False)]))
    assert r.ended_by == "max_turns" and len(r.call.turns) == 7


def hand_ledger(rec: CallRecord) -> Ledger:
    """Label by hand: a bot line with 'naam' asks the name. The first two bot turns are product and quantity."""
    bots = [BotTurnLabel(idx=t.idx, slot="name" if "naam" in t.text.lower() else "other")
            for t in rec.turns if t.speaker == "bot"]
    bots[0] = BotTurnLabel(idx=bots[0].idx, slot="product")
    bots[1] = BotTurnLabel(idx=bots[1].idx, slot="quantity")
    users = [UserTurnLabel(idx=t.idx) for t in rec.turns if t.speaker == "user"]
    return Ledger(call_id=rec.call_id, scope="bl_collection", final_product="CCTV camera",
                  buyer_facts={"quantity": "2 piece", "name": "BuyerA"},
                  bot_confirmed={"product": "CCTV camera", "quantity": "2 piece"},
                  product_match=True, bot_turns=bots, user_turns=users)


def test_looping_prompt_is_flagged_and_fixed_prompt_is_not():
    call = mk_call()
    # old prompt: asks the name every bot turn, buyer hangs up at the end
    loop_user = [("BuyerA", False)] * 3 +[("bye", True)]
    looped = replay(call, 4, "OLD", brief(), max_turns=12, llm=stub([("Aapka naam?", False)], loop_user))
    a = audit_call(looped, hand_ledger(looped))
    assert {f.parameter: f for f in a.findings}["looping"].status == "Fatal" and "loops" in a.flags
    # patched prompt: asks once, then ends the call
    fixed = replay(call, 4, "NEW", brief(), max_turns=12,
                   llm=stub([("Aapka naam?", False), ("Dhanyavaad, seller call karega", True)]))
    b = audit_call(fixed, hand_ledger(fixed))
    assert {f.parameter: f for f in b.findings}["looping"].status == "Pass"
    assert "loops" not in b.flags


def test_same_seed_and_prompt_send_identical_requests():
    # complete_json caches by (model, system, user, cache_tag). If every request is identical across
    # runs, the second run is served from the cache. We check that, without touching the network.
    call = mk_call()
    runs = []
    for prompt, seed in (("PROMPT", 3), ("PROMPT", 3), ("PROMPT", 4), ("PATCHED", 3)):
        log: list = []
        replay(call, 4, prompt, brief(), max_turns=4, seed=seed, llm=stub([("naam?", False)], log=log))
        runs.append(log)
    assert runs[0] == runs[1] and len(runs[0]) == 4  # one LLM call per generated turn
    assert runs[0] != runs[2]  # a different seed changes the cache tag
    assert runs[0] != runs[3]  # a different prompt changes the system text and the tag
