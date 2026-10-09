"""Dashboard drawings: call text is escaped, blind drawings carry no audit, and the wall counts every call."""

import re
from pathlib import Path

from vaniloop.dashboard import load_audits, load_calls, load_causes
from vaniloop.models import AuditRecord, CallRecord, Cause, Evidence, Finding, Silence, Turn
from vaniloop.viz import call_strip, cause_list, findings, lede, script, wall

FIX = Path(__file__).parent / "fixtures"


def _call() -> CallRecord:
    turns = [
        Turn(idx=0, speaker="bot", start_sec=0, end_sec=4, text="Namaste, kitne piece chahiye?"),
        Turn(idx=1, speaker="user", start_sec=4.5, end_sec=6, text="<script>alert(1)</script> do piece"),
        Turn(idx=2, speaker="bot", start_sec=14, end_sec=18, text="Kitne piece chahiye?"),
    ]
    return CallRecord(
        call_id="c1",
        audio_path=None,
        duration_sec=20,
        turns=turns,
        silences=[Silence(start_sec=6.5, end_sec=13.5)],
    )


def _audit() -> AuditRecord:
    ev = Evidence(turn_idx=2, start_sec=14, end_sec=18, quote="Kitne piece chahiye?")
    f = Finding(parameter="qty_missed", status="Fatal", reason="asked again", basis="spoken_proxy", evidence=[ev])
    return AuditRecord(call_id="c1", verdict="Fatal", scope="bl_collection", findings=[f], first_fault_turn=2)


def test_call_text_is_escaped_in_every_drawing():
    """Transcript text reaches the page only as escaped text, never as live HTML.

    The dashboard renders these strings as HTML. Without escaping, a transcript line could inject markup or script.
    """
    for html_out in (script(_call()), script(_call(), _audit()), call_strip(_call(), _audit())):
        assert "<script>" not in html_out
    assert "&lt;script&gt;alert(1)&lt;/script&gt; do piece" in script(_call())


def test_blind_drawings_carry_no_audit_information():
    """With no audit passed, the strip and the script show no fault rule, colour or note.

    The Grade view uses this mode. If audit marks leaked into it, hand grades would no longer be blind.
    """
    strip, text = call_strip(_call()), script(_call())
    assert "onset" not in strip and "first fault" not in strip and "#B3261E" not in strip
    assert 'class="fault"' not in text and "note" not in text


def test_audited_drawings_mark_the_fault_turn_and_the_long_silence():
    """With an audit, the strip marks the first fault turn and the script flags the cited turn with its finding.

    A 7 s gap shows as silence. If this breaks, the detail view stops showing where and why a call failed.
    """
    strip, text = call_strip(_call(), _audit()), script(_call(), _audit(), {"qty_missed": "Quantity asked again"})
    assert "first fault, turn 2" in strip and "7 s of silence" in strip
    assert 'class="fault"' in text and "Fatal: Quantity asked again starts here" in text
    assert "7 s of silence at 0:06" in text and "turn 2" in text


def test_findings_list_faults_first_and_name_passed_checks():
    """The findings panel shows each fault with its plain name and lists passed checks on one line.

    If a fault were hidden among passes, the owner would miss why a call failed.
    """
    a = _audit()
    a.findings.append(Finding(parameter="dead_air", status="Pass", reason="ok", basis="observed"))
    out = findings(a, {"qty_missed": "Quantity asked again", "dead_air": "Long silences"})
    assert out.index("Quantity asked again") < out.index("Passed: Long silences")


def test_wall_and_lede_count_every_call_once_by_verdict():
    """The wall draws one row per audited call in its verdict column, and the lede sentence uses the same counts.

    If counts drifted, the first screen would misstate how many calls are Fatal.
    """
    audits, calls = load_audits(FIX), load_calls(FIX)
    out = wall(audits, calls)
    assert out.count('class="row"') == sum(a.call_id in calls for a in audits)
    fatal = sum(a.verdict == "Fatal" for a in audits)
    graded = sum(a.verdict != "NeedsReview" for a in audits)
    assert f"<b>{fatal} of {graded}</b>" in lede(audits)


def test_cause_list_keeps_rank_order_and_one_shared_axis():
    """Causes appear in the given rank order, and every interval bar is drawn on the same axis.

    A per-row axis would make a rare cause look as frequent as a common one.
    """
    causes = load_causes(FIX)
    out = cause_list(causes)
    positions = [out.index(c.name) for c in causes]
    assert positions == sorted(positions)
    assert out.count("Bars share one axis") == 1


def test_cited_turns_before_the_onset_are_context_not_fault():
    """Asks within the limit are cited as evidence but drawn as context; the fault rule starts at the onset turn.

    Found on a real 5:45 call: three allowed city asks showed as Fatal while the finding said the fault began at the
    fourth. The page must agree with the finding, and name it once, not on every cited turn.
    """
    call = _call()
    call.turns.append(Turn(idx=3, speaker="bot", start_sec=18.5, end_sec=19.5, text="City bataiye?"))
    ev = [Evidence(turn_idx=i, start_sec=0, end_sec=1, quote="ask") for i in (0, 2, 3)]
    f = Finding(parameter="looping", status="Fatal", reason="r", basis="observed", evidence=ev, onset_turn=3)
    audit = AuditRecord(call_id="c1", verdict="Fatal", scope="bl_collection", findings=[f], first_fault_turn=3)
    text = script(call, audit, {"looping": "Asks again"})
    assert text.count('class="context"') == 2 and text.count('class="fault"') == 1
    assert text.count("Counts toward: Asks again") == 1 and text.count("Fatal: Asks again starts here") == 1
    assert "color-mix" in call_strip(call, audit)


def test_wall_scale_and_strip_edges_stay_inside_the_timeline():
    """With two calls the wall scale is the longer call, and a zero-length last turn draws no bar past the end.

    Review found the p95 index picked the shorter call (hiding most of the longer one) and an end turn overflowing.
    """
    short = CallRecord(call_id="s", audio_path=None, duration_sec=10, turns=[], silences=[])
    long_ = _call()
    long_.duration_sec = 100
    audits = [AuditRecord(call_id=c, verdict="Pass", scope="bl_collection", findings=[]) for c in ("s", "c1")]
    assert "Rows longer than 1:40" in wall(audits, {"s": short, "c1": long_})
    edge = _call()
    edge.turns.append(Turn(idx=3, speaker="bot", start_sec=20, end_sec=20, text="end"))
    assert "Turn 3," not in call_strip(edge)


def test_cause_list_numbers_patterns_only_and_labels_every_other_status():
    """Only patterns may carry a rank number, and every non-pattern must show a label that says why.

    Without it an outlier would read as the most urgent cause and the owner would chase a data signal.
    """

    def c(cid, status):
        return Cause(cause_id=cid, name=f"Name {cid}", description="", type="prompt", parameter=cid, status=status)

    out = cause_list([c("a", "ranked"), c("b", "pattern_unconfirmed"), c("c", "outlier"), c("d", "single_category")])
    assert re.findall(r'<span class="n">([^<]+)</span>', out) == ["1", "2", "&ndash;", "&ndash;"]
    assert "Pattern, not replayed yet" in out
    assert "the old prompt did not reproduce it" in out and "fewer than 2 product categories" in out


def test_cause_list_labels_a_channel_cause_as_channel_not_as_a_small_watch():
    """A channel cause must show the channel label and the channel lever, never the under-3-calls watch label.

    Without it a bad-audio cause would read as a small prompt fault and the owner would try to patch it.
    """
    ch = Cause(
        cause_id="cause_channel_x", name="N", description="", type="channel", parameter="x", status="watch",
        fatal_calls=["a"],
    )  # fmt: skip
    out = cause_list([ch])
    assert "the bot misheard the buyer at the root turn" in out and "no prompt fix" in out
    assert "under 3 calls" not in out and '<span class="n">&ndash;</span>' in out
