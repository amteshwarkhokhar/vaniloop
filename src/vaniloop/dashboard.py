"""Streamlit dashboard: Audit and Issues views over vaniloop outputs.

Run: uv run streamlit run src/vaniloop/dashboard.py
"""

from __future__ import annotations

import html
import json
import os
from pathlib import Path

import pandas as pd
import streamlit as st

from vaniloop.jsonl import dedupe_by_call_id, read_jsonl
from vaniloop.models import AuditRecord, CallRecord, Cause

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "tests" / "fixtures"
VERDICTS = ["Pass", "Fatal", "NonFatal", "NeedsReview"]
SILENCE_MIN = 5.0
STATUS_COLOR = {"ranked": "#1b7f3b", "isolated": "#b45309", "watch": "#6b7280"}


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


def evidence_colors(a: AuditRecord) -> dict[int, str]:
    """turn idx -> colour; Fatal (red) wins over NonFatal (amber)."""
    out: dict[int, str] = {}
    for f in a.findings:
        if f.status not in ("Fatal", "NonFatal"):
            continue
        for e in f.evidence:
            if e.turn_idx is None:
                continue
            if f.status == "Fatal" or out.get(e.turn_idx) != "#fecaca":
                out[e.turn_idx] = "#fecaca" if f.status == "Fatal" else "#fde68a"
    return out


def first_evidence_sec(a: AuditRecord) -> float | None:
    secs = [e.start_sec for f in a.findings for e in f.evidence if e.start_sec is not None]
    return min(secs) if secs else None


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


# ---------- views ----------
def render_detail(a: AuditRecord, call: CallRecord | None) -> None:
    st.subheader(f"{a.call_id}: {a.verdict}")
    st.caption(f"scope: {a.scope} | flags: {', '.join(a.flags) or 'none'}")
    if a.review_reason:
        st.info(f"Review reason: {a.review_reason}")
    left, right = st.columns([3, 2])
    with left:
        st.markdown("**Transcript**")
        if call is None:
            st.warning("No call record found for this call.")
        else:
            colors = evidence_colors(a)
            events = [(t.start_sec, 0, t) for t in call.turns]
            events += [(s.start_sec, 1, s) for s in call.silences if s.duration > SILENCE_MIN]
            for _, kind, ev in sorted(events, key=lambda x: (x[0], x[1])):
                if kind == 1:
                    st.markdown(f"`{ev.start_sec:.1f}s` *silence {ev.duration:.1f}s*")
                    continue
                bg = colors.get(ev.idx, "#f3f4f6")
                st.markdown(
                    f"<div style='background:{bg};color:#111;padding:6px 10px;border-radius:6px;margin:3px 0'>"
                    f"<b>{ev.speaker}</b> <small>#{ev.idx} @ {ev.start_sec:.1f}s</small><br>{html.escape(ev.text)}</div>",
                    unsafe_allow_html=True,
                )
    with right:
        st.markdown("**Findings**")
        for f in a.findings:
            if f.status in ("NotApplicable",):
                continue
            st.markdown(f"**{f.parameter}**: {f.status} ({f.basis})")
            st.write(f.reason)
            for e in f.evidence:
                if e.quote:
                    at = f" @ {e.start_sec:.1f}s" if e.start_sec is not None else ""
                    st.markdown(f"> {e.quote}  \n> turn {e.turn_idx}{at}")
        st.markdown("**Flags**: " + (", ".join(a.flags) or "none"))
        start = first_evidence_sec(a)
        audio = resolve_audio(call.audio_path) if call else None
        if audio:
            st.audio(str(audio), start_time=int(start or 0))
        else:
            st.caption("Audio file not available.")


def audit_tab(audits: list[AuditRecord], calls: dict[str, CallRecord]) -> None:
    if not audits:
        st.warning("No audits found in this data source.")
        return
    n = len(audits)
    counts = {v: sum(a.verdict == v for a in audits) for v in VERDICTS}
    cols = st.columns(5)
    cols[0].metric("Calls audited", n)
    for col, v in zip(cols[1:], VERDICTS):
        col.metric(v, f"{counts[v]} ({pct(counts[v], n)})")

    df = audits_frame(audits)
    all_flags = sorted({f for a in audits for f in a.flags})
    all_params = sorted({f.parameter for a in audits for f in a.findings})
    c1, c2, c3 = st.columns(3)
    fv = c1.multiselect("Verdict", VERDICTS)
    ff = c2.multiselect("Flag", all_flags)
    fp = c3.multiselect("Parameter (non-pass finding)", all_params)
    keep = []
    for a in audits:
        if fv and a.verdict not in fv:
            continue
        if ff and not set(ff) & set(a.flags):
            continue
        if fp and not any(f.parameter in fp and f.status in ("Fatal", "NonFatal") for f in a.findings):
            continue
        keep.append(a.call_id)
    view = df[df.call_id.isin(keep)]
    st.caption(f"{len(view)} of {n} calls")
    st.dataframe(view, use_container_width=True, hide_index=True)

    ids = [a.call_id for a in audits]
    if st.session_state.get("call_sel") not in ids:
        st.session_state["call_sel"] = ids[0]
    sel = st.selectbox("Call detail", ids, key="call_sel")
    render_detail(next(a for a in audits if a.call_id == sel), calls.get(sel))


def issues_tab(causes: list[Cause], total_calls: int) -> None:
    if not causes:
        st.warning("No causes found in this data source.")
        return
    st.caption(f"Causes in given order. Frequency is over {total_calls} eligible calls.")
    hdr = st.columns([3, 1.2, 1.6, 0.8, 1, 1.6, 1, 1, 1.2])
    for c, t in zip(hdr, ["Name", "Type", "Parameter", "Fatal", "NonFatal", "Frequency (interval)", "Priority", "Rank score", "Status"]):
        c.markdown(f"**{t}**")
    for cs in causes:
        freq = (len(set(cs.fatal_calls) | set(cs.nonfatal_calls)) / total_calls) if total_calls else 0.0
        row = st.columns([3, 1.2, 1.6, 0.8, 1, 1.6, 1, 1, 1.2])
        row[0].write(f"{cs.cause_id} {cs.name}")
        row[1].write(cs.type)
        row[2].write(cs.parameter)
        row[3].write(len(cs.fatal_calls))
        row[4].write(len(cs.nonfatal_calls))
        row[5].write(f"{freq * 100:.1f}% ({cs.freq_ci_low * 100:.1f}-{cs.freq_ci_high * 100:.1f}%)")
        row[6].write(f"{cs.priority:.3f}")
        row[7].write(f"{cs.rank_score:.3f}")
        row[8].markdown(
            f"<span style='background:{STATUS_COLOR.get(cs.status, '#6b7280')};color:#fff;"
            f"padding:2px 10px;border-radius:12px'>{cs.status}</span>",
            unsafe_allow_html=True,
        )
    st.divider()
    labels = {c.cause_id: f"{c.cause_id} {c.name}" for c in causes}
    cid = st.selectbox("Cause", list(labels), format_func=labels.get, key="cause_sel")
    cs = next(c for c in causes if c.cause_id == cid)
    st.write(cs.description)
    members = [(c, "Fatal") for c in cs.fatal_calls] + [(c, "NonFatal") for c in cs.nonfatal_calls]
    st.markdown(f"**Member calls ({len(members)})**")
    for call_id, kind in members:
        a, b = st.columns([1, 5])
        a.button(f"Open {call_id}", key=f"open_{cs.cause_id}_{call_id}", on_click=open_call, args=(call_id,))
        b.write(kind)


def main() -> None:
    st.set_page_config(page_title="vaniloop", layout="wide")
    data_dir = default_data_dir()
    audits, (causes, meta), calls = load_audits(data_dir), load_causes_doc(data_dir), load_calls(data_dir)
    with st.sidebar:
        st.markdown("**Data source**")
        st.code(str(data_dir))
        st.caption(f"{len(audits)} audits, {len(causes)} causes, {len(calls)} calls")
    st.title("vaniloop")
    view = st.radio("View", ["Audit", "Issues", "Fix", "Test"], horizontal=True, key="view", label_visibility="collapsed")
    if view == "Audit":
        audit_tab(audits, calls)
    elif view == "Issues":
        issues_tab(causes, meta.get("denominator", sum(a.verdict != "NeedsReview" for a in audits)))
    elif view == "Fix":
        st.info("Coming next: fix diffs")
    else:
        st.info("Coming next: before/after replay")


if __name__ == "__main__":
    main()
