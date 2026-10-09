"""Streamlit dashboard: Audit, Grade and Issues views over vaniloop outputs. Drawing lives in viz.py.

Run: uv run streamlit run src/vaniloop/dashboard.py
"""

from __future__ import annotations

import html
import json
import os
from pathlib import Path

import pandas as pd
import streamlit as st

from vaniloop.grading import (
    HAND_VERDICTS,
    agreement,
    failing_parameters,
    known_parameters,
    load_grades,
    reveal,
    save_grade,
)
from vaniloop.issue import KNOWN
from vaniloop.jsonl import dedupe_by_call_id, read_jsonl
from vaniloop.models import AuditRecord, CallRecord, Cause
from vaniloop.viz import CSS, VERDICT_COLOR, VERDICT_LABEL, call_strip, cause_list, findings, lede, script, wall

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "tests" / "fixtures"
VERDICTS = ["Pass", "Fatal", "NonFatal", "NeedsReview"]
NAMES = {p: v[0] for p, v in KNOWN.items()}  # parameter -> plain name, e.g. qty_probing -> Quantity never asked


# ---------- data ----------
def default_data_dir() -> Path:
    env = os.environ.get("VANILOOP_DATA")
    if env:
        return Path(env)
    runs = ROOT / "runs"
    return runs if (runs / "audits.jsonl").exists() else FIXTURES


def _pick(d: Path, *names: str) -> Path | None:
    for n in names:
        if (d / n).exists():
            return d / n
    return None


def _models(cls, rows: list[dict]) -> list:
    out = []
    for r in rows:
        try:
            out.append(cls.model_validate(r))
        except Exception:  # skip a malformed row, keep the rest
            continue
    return out


def load_audits(d: Path | str) -> list[AuditRecord]:
    p = _pick(Path(d), "audits.jsonl", "synthetic_audits.jsonl")
    return _models(AuditRecord, dedupe_by_call_id(read_jsonl(p)))


def load_causes_doc(d: Path | str) -> tuple[list[Cause], dict]:
    """(causes, meta). Reads the new object format and the old list format. Malformed -> warning + ([], {})."""
    p = _pick(Path(d), "causes.json", "synthetic_causes.json")
    if p is None:
        return [], {}
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
        meta: dict = {}
        if isinstance(raw, dict):
            meta = {k: v for k, v in raw.items() if k != "causes"}
            raw = raw.get("causes", [])
        if not isinstance(raw, list):
            raise ValueError("causes is not a list")
        return [Cause.model_validate(r) for r in raw], meta
    except Exception as e:
        st.warning(f"Could not read {p.name}: {type(e).__name__}. Showing no causes.")
        return [], {}


def load_causes(d: Path | str) -> list[Cause]:
    return load_causes_doc(d)[0]


def load_calls(d: Path | str) -> dict[str, CallRecord]:
    p = _pick(Path(d), "calls.jsonl", "synthetic_calls.jsonl")
    return {c.call_id: c for c in _models(CallRecord, read_jsonl(p))}


def load_grade_sample(d: Path | str, audits: list[AuditRecord]) -> list[str]:
    """The saved grade sample ids, or the first 20 audits when the file is absent or unusable."""
    p = Path(d) / "grade_sample.json"
    if p.exists():
        try:
            raw = json.loads(p.read_text(encoding="utf-8"))
            valid = {a.call_id for a in audits}
            picked = [r for r in raw if isinstance(r, str) and r in valid] if isinstance(raw, list) else []
            if picked:
                return picked
        except Exception:  # a broken sample must fall back, not crash the view
            pass
    return [a.call_id for a in audits[:20]]


# ---------- helpers ----------
def audits_frame(audits: list[AuditRecord]) -> pd.DataFrame:
    rows = []
    for a in audits:
        bad = [f.parameter for f in a.findings if f.status in ("Fatal", "NonFatal")]
        rows.append(
            {
                "call_id": a.call_id,
                "verdict": a.verdict,
                "scope": a.scope,
                "flags": ", ".join(a.flags),
                "parameters": ", ".join(bad),
                "first_fault_turn": a.first_fault_turn,
            }
        )
    return pd.DataFrame(rows, columns=["call_id", "verdict", "scope", "flags", "parameters", "first_fault_turn"])


def resolve_audio(path: str | None) -> Path | None:
    if not path:
        return None
    p = Path(path)
    if not p.is_absolute():
        p = ROOT / p
    return p if p.exists() else None


def open_call(call_id: str) -> None:
    st.session_state["view"] = "Audit"
    st.session_state["call_sel"] = call_id


def pct(n: int, total: int) -> str:
    return f"{(100 * n / total):.0f}%" if total else "0%"


def transcript_frame(call: CallRecord) -> pd.DataFrame:
    """Transcript rows for the Grade view: idx, speaker, start_sec, text, tone, overlap.

    tone and overlap are optional Turn fields added on another branch; getattr keeps this
    view working when a call was produced before those fields existed.
    """
    rows = [
        {
            "idx": t.idx,
            "speaker": t.speaker,
            "start_sec": t.start_sec,
            "text": t.text,
            "tone": getattr(t, "tone", None),
            "overlap": getattr(t, "overlap", None),
        }
        for t in call.turns
    ]
    return pd.DataFrame(rows, columns=["idx", "speaker", "start_sec", "text", "tone", "overlap"])


# ---------- views ----------
def verdict_heading(call_id: str, verdict: str) -> None:
    color = VERDICT_COLOR.get(verdict, "#5E6B6E")
    st.html(
        f"<h3 style='margin:.25rem 0 .1rem'>{html.escape(call_id)} "
        f"<span style='color:{color};font-weight:700'>{VERDICT_LABEL.get(verdict, verdict)}</span></h3>"
    )


def render_detail(a: AuditRecord, call: CallRecord | None) -> None:
    verdict_heading(a.call_id, a.verdict)
    meta = [f"Scope: {a.scope.replace('_', ' ')}"] + ([f"Flags: {', '.join(a.flags)}"] if a.flags else [])
    st.caption(". ".join(meta))
    if a.review_reason:
        st.info(f"Why a person should listen: {a.review_reason}")
    if call is None:
        st.warning("No call record for this call. Run ingest to add it.")
        return
    st.html(call_strip(call, a))
    left, right = st.columns([2, 1], gap="large")
    with left:
        st.html(script(call, a, NAMES))
    with right:
        audio = resolve_audio(call.audio_path)
        if audio:
            start = next((t.start_sec for t in call.turns if t.idx == a.first_fault_turn), None)
            st.audio(str(audio), start_time=int(start or 0))
            if start:
                st.caption(f"Audio starts at the first fault, turn {a.first_fault_turn}, {start:.0f} s in.")
        else:
            st.caption("No audio file for this call.")
        st.html(findings(a, NAMES))


def grade_tab(audits: list[AuditRecord], calls: dict[str, CallRecord], data_dir: Path) -> None:
    """Blind hand-grading: grade one call, then reveal the audit and the agreement table."""
    if not audits:
        st.warning("No audits in this data source. Run the audit first.")
        return
    audit_by_id = {a.call_id: a for a in audits}
    sample = [cid for cid in load_grade_sample(data_dir, audits) if cid in audit_by_id]
    ids = sample or [a.call_id for a in audits]
    grades_path = data_dir / "hand_grades.jsonl"
    grades = load_grades(grades_path)
    done = sum(cid in grades for cid in ids)
    st.caption(f"{done} of {len(ids)} calls graded. Listen, read, then grade. The audit stays hidden until you save.")
    labels = {cid: (f"\u2713 {cid}" if cid in grades else f"   {cid}") for cid in ids}
    if st.session_state.get("grade_sel") not in ids:
        st.session_state["grade_sel"] = ids[0]
    sel = st.selectbox("Call", ids, format_func=labels.get, key="grade_sel")
    call = calls.get(sel)
    if call is None:
        st.warning("No call record for this call. Run ingest to add it.")
    else:
        audio = resolve_audio(call.audio_path)
        if audio:
            st.audio(str(audio))
        else:
            st.caption("No audio file for this call.")
        st.html(call_strip(call))  # blind: no audit passed
        st.html(script(call))

    existing = grades.get(sel) or {}
    all_params = known_parameters(audits)
    default_params = [p for p in existing.get("parameters", []) if p in all_params]
    default_verdict = existing.get("verdict") if existing.get("verdict") in HAND_VERDICTS else HAND_VERDICTS[0]
    default_turn = existing.get("first_fault_turn")
    # Widgets are keyed by call id: unsaved choices on one call must never be saved under another.
    with st.form(f"grade_form_{sel}"):
        verdict = st.radio(
            "Your verdict",
            list(HAND_VERDICTS),
            index=list(HAND_VERDICTS).index(default_verdict),
            horizontal=True,
            key=f"grade_verdict_{sel}",
            format_func=lambda v: VERDICT_LABEL.get(v, v),
            help="Fatal: wrong or missing buyer details reach the seller. Non-Fatal: a fault that does not.",
        )
        params = st.multiselect(
            "What went wrong",
            all_params,
            default=default_params,
            format_func=lambda p: NAMES.get(p, p),
            key=f"grade_params_{sel}",
        )
        fault_turn = st.number_input(
            "First fault turn (optional, -1 for none)",
            min_value=-1,
            value=int(default_turn) if default_turn is not None else -1,
            key=f"grade_turn_{sel}",
        )
        note = st.text_area("Note", value=existing.get("note", ""), key=f"grade_note_{sel}")
        submitted = st.form_submit_button("Save grade", type="primary")
    if submitted:
        save_grade(
            grades_path,
            {
                "call_id": sel,
                "verdict": verdict,
                "parameters": list(params),
                "first_fault_turn": None if fault_turn < 0 else int(fault_turn),
                "note": note,
            },
        )
        grades = load_grades(grades_path)

    rev = reveal(sel, grades, audits)
    if rev is None:
        return

    st.divider()
    st.markdown("#### Your grade and the audit")
    hand_col, audit_col = st.columns(2)
    with hand_col:
        hv = rev["hand"].get("verdict")
        st.html(f"<p>You: <b style='color:{VERDICT_COLOR.get(hv, '#1E2B2F')}'>{VERDICT_LABEL.get(hv, hv)}</b></p>")
        st.caption(", ".join(NAMES.get(p, p) for p in rev["hand"].get("parameters") or []) or "No faults chosen.")
    with audit_col:
        if rev["audit"] is None:
            st.write("No audit for this call.")
        else:
            av = rev["audit"].verdict
            color = VERDICT_COLOR.get(av, "#1E2B2F")
            st.html(f"<p>Audit: <b style='color:{color}'>{VERDICT_LABEL.get(av, av)}</b></p>")
            st.caption(", ".join(NAMES.get(p, p) for p in failing_parameters(rev["audit"])) or "No faults found.")
    if rev["verdict_match"]:
        st.success("Same verdict.")
    else:
        st.error("Different verdict. This call goes into calibration.")

    agree = agreement(grades, audits)
    st.markdown("#### Agreement so far")
    st.write(
        f"The audit gives your verdict on {agree['verdict_agree']} of {agree['graded']} graded calls "
        f"({pct(agree['verdict_agree'], agree['graded'])})."
    )
    if agree["per_parameter"]:
        pdf = pd.DataFrame(
            [
                {
                    "check": NAMES.get(p, p),
                    "agree": v["agree"],
                    "compared": v["n"],
                    "rate": "n/a" if v["rate"] is None else f"{v['rate'] * 100:.0f}%",
                }
                for p, v in agree["per_parameter"].items()
            ]
        )
        st.dataframe(pdf, width="stretch", hide_index=True)
    if agree["mismatches"]:
        st.caption("Calls where you and the audit differ")
        st.dataframe(pd.DataFrame(agree["mismatches"], columns=["call_id", "hand", "audit"]), hide_index=True)


def audit_tab(audits: list[AuditRecord], calls: dict[str, CallRecord]) -> None:
    if not audits:
        st.warning("No audits in this data source. Run the audit first.")
        return
    st.html(lede(audits))
    st.html(wall(audits, calls))

    df = audits_frame(audits)
    df["verdict"] = df["verdict"].map(lambda v: VERDICT_LABEL.get(v, v))
    df["parameters"] = df["parameters"].map(lambda s: ", ".join(NAMES.get(p, p) for p in s.split(", ") if p))
    all_flags = sorted({f for a in audits for f in a.flags})
    all_params = sorted({f.parameter for a in audits for f in a.findings})
    c1, c2, c3 = st.columns(3)
    fv = c1.multiselect("Verdict", VERDICTS, format_func=lambda v: VERDICT_LABEL.get(v, v))
    ff = c2.multiselect("Flag", all_flags)
    fp = c3.multiselect("Fault", all_params, format_func=lambda p: NAMES.get(p, p))
    keep = []
    for a in audits:
        if fv and a.verdict not in fv:
            continue
        if ff and not set(ff) & set(a.flags):
            continue
        if fp and not any(f.parameter in fp and f.status in ("Fatal", "NonFatal") for f in a.findings):
            continue
        keep.append(a.call_id)
    view = df[df.call_id.isin(keep)].rename(
        columns={
            "call_id": "Call",
            "verdict": "Verdict",
            "scope": "Scope",
            "flags": "Flags",
            "parameters": "Faults",
            "first_fault_turn": "First fault turn",
        }
    )
    st.caption(f"{len(view)} of {len(audits)} calls")
    st.dataframe(view, width="stretch", hide_index=True)

    ids = [a.call_id for a in audits]
    if st.session_state.get("call_sel") not in ids:
        st.session_state["call_sel"] = ids[0]
    sel = st.selectbox("Open a call", ids, key="call_sel")
    st.query_params["call"] = sel  # the address bar now links to this call
    render_detail(next(a for a in audits if a.call_id == sel), calls.get(sel))


def issues_tab(causes: list[Cause], total_calls: int) -> None:
    if not causes:
        st.warning("No causes yet. Run `uv run python -m vaniloop.pipeline issues` after the audit.")
        return
    st.html(
        f"<p class='vl vl-lede'>{len(causes)} causes across {total_calls} graded calls, most urgent first. "
        "Urgency weighs how often a cause happens, how bad it is and how easy it is to fix.</p>"
    )
    st.html(cause_list(causes))
    labels = {c.cause_id: c.name for c in causes}
    cid = st.selectbox("Show the calls behind a cause", list(labels), format_func=labels.get, key="cause_sel")
    cs = next(c for c in causes if c.cause_id == cid)
    members = [(c, "Fatal") for c in cs.fatal_calls] + [(c, "NonFatal") for c in cs.nonfatal_calls]
    st.caption(f"{len(members)} calls. Open one to see where it goes wrong.")
    for call_id, kind in members:
        a, b = st.columns([2, 5])
        a.button(f"Open {call_id}", key=f"open_{cs.cause_id}_{call_id}", on_click=open_call, args=(call_id,))
        b.html(f"<span style='color:{VERDICT_COLOR[kind]}'>{VERDICT_LABEL[kind]}</span>")


def main() -> None:
    st.set_page_config(page_title="vaniloop", page_icon="\u260e", layout="wide", initial_sidebar_state="collapsed")
    st.html(CSS)
    data_dir = default_data_dir()
    audits, (causes, meta), calls = load_audits(data_dir), load_causes_doc(data_dir), load_calls(data_dir)
    with st.sidebar:
        st.caption("Data source")
        st.code(str(data_dir), language=None)
        st.caption(f"{len(audits)} audits, {len(causes)} causes, {len(calls)} calls")
    st.title("vaniloop")
    st.caption("Grades VANI calls, finds why they fail, and tests prompt fixes before they ship.")
    if "deep_link" not in st.session_state:  # only the session's first run reads ?call=<id>; later the app writes it
        linked = st.query_params.get("call", "")
        st.session_state["deep_link"] = linked
        if any(a.call_id == linked for a in audits):
            open_call(linked)
    view = st.radio(
        "View", ["Audit", "Grade", "Issues", "Fix", "Test"], horizontal=True, key="view", label_visibility="collapsed"
    )
    if view == "Audit":
        audit_tab(audits, calls)
    elif view == "Grade":
        grade_tab(audits, calls, data_dir)
    elif view == "Issues":
        issues_tab(causes, meta.get("denominator", sum(a.verdict != "NeedsReview" for a in audits)))
    elif view == "Fix":
        st.info(
            "No fixes to show yet. The fix stage writes prompt patches for the top causes: "
            "`uv run python -m vaniloop.pipeline fix --prompt data/reference/vani_prompt_sarvam.md`."
        )
    else:
        st.info("No replays to show yet. The test stage replays failing calls with the old and the fixed prompt.")


if __name__ == "__main__":
    main()
