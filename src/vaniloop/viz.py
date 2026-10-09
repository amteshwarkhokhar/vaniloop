"""HTML and SVG for the dashboard: call strips, the wall of calls, the transcript as a script, the cause list.

Pure functions, no streamlit import: the dashboard renders the strings and the tests read them.
Every piece of call text goes through html.escape. Pass audit=None to draw a call blind (Grade view).
"""

from __future__ import annotations

import html
import math

from .models import AuditRecord, CallRecord, Cause

VERDICT_ORDER = ("Fatal", "NonFatal", "Pass", "NeedsReview")
VERDICT_LABEL = {"Fatal": "Fatal", "NonFatal": "Non-Fatal", "Pass": "Pass", "NeedsReview": "Needs review"}
VERDICT_COLOR = {"Fatal": "#B3261E", "NonFatal": "#B86E00", "Pass": "#2E7D4F", "NeedsReview": "#5B4BB7"}
SPEAKER_COLOR = {"bot": "#2F5D62", "user": "#8FA9AD"}
SPEAKER_NAME = {"bot": "VANI", "user": "Buyer"}
SILENCE_MIN = 5.0  # rubric: dead air is a gap longer than 5 s
FAILING = ("Fatal", "NonFatal")

CSS = """
<style>
.vl, .vl * { font-variant-numeric: tabular-nums; }
.vl-lede { font-size: 1.25rem; line-height: 1.45; max-width: 46rem; margin: 0 0 1.25rem; }
.vl-lede b { font-weight: 700; }
.vl-wall { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 0 1.5rem; margin-bottom: .5rem; }
.vl-col h4 { font-size: 1rem; font-weight: 700; margin: 0 0 .4rem; padding: 0 0 .3rem;
  border-bottom: 3px solid var(--c); display: flex; justify-content: space-between; }
.vl-col h4 span { font-weight: 400; }
.vl-col .row { position: relative; height: 6px; margin: 0 0 3px; overflow: hidden; }
.vl-col .row span { position: absolute; top: 0; height: 6px; }
.vl-col .row .len { top: 2px; height: 2px; left: 0; background: #E1E4DF; }
.vl-col .row .b { background: #2F5D62; }
.vl-col .row .u { top: 1px; height: 4px; background: #8FA9AD; }
.vl-col .row .f { top: -1px; height: 8px; width: 2px; background: var(--c); }
.vl-col .row:hover { outline: 1px solid #1E2B2F; outline-offset: 1px; }
.vl-axis { font-size: .8rem; color: #5E6B6E; margin: .2rem 0 1.5rem; }
.vl-key { font-size: .85rem; color: #5E6B6E; display: flex; gap: 1.25rem; flex-wrap: wrap; margin: .4rem 0 1rem; }
.vl-key i { display: inline-block; width: .9rem; height: .55rem; margin-right: .35rem; vertical-align: middle; }
.vl-strip { display: grid; grid-template-columns: 3.25rem minmax(0, 1fr); column-gap: .5rem; margin: .75rem 0 1rem; }
.vl-strip .names { display: grid; grid-template-rows: 16px 16px; row-gap: 6px; padding-top: 18px;
  font-size: .8rem; color: #5E6B6E; line-height: 16px; }
.vl-strip .lanes { position: relative; height: 62px; }
.vl-strip .lanes > span { position: absolute; }
.vl-strip .sil { top: 14px; height: 46px; background: #E8E2CF; }
.vl-strip .turn { height: 16px; border-radius: 2px; min-width: 2px; }
.vl-strip .turn.bot { top: 18px; }
.vl-strip .turn.user { top: 40px; }
.vl-strip .onset { top: 0; bottom: 0; border-left: 2px solid var(--c); }
.vl-strip .onset span { position: absolute; left: 5px; top: -3px; font-size: .75rem;
  color: var(--c); white-space: nowrap; }
.vl-strip .ticks { grid-column: 2; position: relative; height: 1.1rem; font-size: .75rem; color: #5E6B6E; }
.vl-strip .ticks span { position: absolute; top: 0; }
.vl-script { list-style: none; padding: 0; margin: .5rem 0 0; max-width: 52rem; }
.vl-script li { display: grid; grid-template-columns: 3.5rem 4.5rem minmax(0, 1fr); gap: 0 .75rem;
  padding: .45rem 0 .45rem .75rem; border-left: 3px solid transparent; line-height: 1.55; }
.vl-script li + li { border-top: 1px solid #E1E4DF; }
.vl-script .t { color: #5E6B6E; font-size: .9rem; padding-top: .1rem; }
.vl-script .t small { display: block; font-size: .75rem; }
.vl-script .who { font-weight: 700; font-size: .9rem; padding-top: .1rem; }
.vl-script .who.bot { color: #2F5D62; }
.vl-script .who.user { color: #4F6C71; }
.vl-script .who small { display: block; font-weight: 400; color: #5E6B6E; }
.vl-script p { margin: 0; max-width: 72ch; }
.vl-script .note { grid-column: 3; margin: .3rem 0 0; font-size: .9rem; color: var(--c); }
.vl-script li.fault { border-left-color: var(--c); background: color-mix(in srgb, var(--c) 6%, transparent); }
.vl-script li.context { border-left: 3px dashed color-mix(in srgb, var(--c) 45%, transparent); }
.vl-script li.gap { display: block; color: #5E6B6E; font-size: .9rem; font-style: italic; }
.vl-finds { list-style: none; padding: 0; margin: 0; }
.vl-finds li { border-left: 3px solid var(--c); padding: .1rem 0 .1rem .75rem; margin: 0 0 .9rem; }
.vl-finds b { display: block; }
.vl-finds span { color: var(--c); font-weight: 700; }
.vl-finds p { margin: .15rem 0 0; }
.vl-pass { color: #5E6B6E; font-size: .9rem; }
ol.vl-causes, ol.vl-script { padding-left: 0; margin-left: 0; }
.vl-causes { list-style: none; padding: 0; margin: 0 0 1rem; max-width: 60rem; }
.vl-causes li { display: grid; grid-template-columns: 2rem minmax(0, 1.6fr) minmax(8rem, 1fr) minmax(0, 1fr);
  gap: .75rem; align-items: center; padding: .7rem 0; border-top: 1px solid #E1E4DF; }
.vl-causes .n { font-size: 1.5rem; font-weight: 700; color: #5E6B6E; }
.vl-causes b { display: block; }
.vl-causes small { color: #5E6B6E; }
.vl-causes .ci { display: block; position: relative; height: 14px; margin-bottom: .2rem;
  background: linear-gradient(#E1E4DF, #E1E4DF) 0 6px / 100% 2px no-repeat; }
.vl-causes .ci span { position: absolute; top: 2px; height: 10px; border-radius: 2px; }
@media (max-width: 720px) {
  .vl-wall { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .vl-script li { grid-template-columns: 3rem minmax(0, 1fr); }
  .vl-script li .who { grid-column: 2; }
  .vl-script li p, .vl-script .note { grid-column: 2; }
  .vl-causes li { grid-template-columns: 2rem minmax(0, 1fr); }
  .vl-causes li > :nth-child(n+3) { grid-column: 2; }
}
</style>
"""


def esc(x) -> str:
    return html.escape(str(x), quote=True)


def mmss(sec: float) -> str:
    s = max(0, int(round(sec)))
    return f"{s // 60}:{s % 60:02d}"


def call_length(call: CallRecord) -> float:
    ends = [t.end_sec for t in call.turns] + [s.end_sec for s in call.silences]
    return max([call.duration_sec or 0.0, *ends, 1.0])


def _marks(audit: AuditRecord | None) -> tuple[dict[int, str], dict[int, str]]:
    """(fault turns, context turns), each turn idx -> the worst status citing it (Fatal over NonFatal).

    A finding's evidence can include turns before its onset: the first three city asks are allowed and only
    the fourth is the fault. Those earlier turns are context, drawn lighter, so the page agrees with the onset.
    """
    fault: dict[int, str] = {}
    context: dict[int, str] = {}
    for f in audit.findings if audit else []:
        if f.status not in FAILING:
            continue
        for e in f.evidence:
            if e.turn_idx is None:
                continue
            bucket = context if f.onset_turn is not None and e.turn_idx < f.onset_turn else fault
            if bucket.get(e.turn_idx) != "Fatal":
                bucket[e.turn_idx] = f.status
    for idx in fault:
        context.pop(idx, None)
    return fault, context


def _onset_sec(call: CallRecord, audit: AuditRecord | None) -> float | None:
    if audit is None or audit.first_fault_turn is None:
        return None
    return next((t.start_sec for t in call.turns if t.idx == audit.first_fault_turn), None)


def _pct(t: float, total: float) -> str:
    return f"{min(max(t, 0.0), total) / total * 100:.2f}%"


def call_strip(call: CallRecord, audit: AuditRecord | None = None) -> str:
    """The call to scale: VANI on the top lane, the buyer below, long silences shaded, the first fault as a rule.

    Plain positioned divs, not SVG: Streamlit's HTML sanitiser drops SVG shapes. Hover a bar for its turn.
    With audit=None nothing from the audit is drawn, so the strip is safe for blind grading.
    """
    total = call_length(call)
    faults, context = _marks(audit)
    parts = []
    for s in call.silences:
        if s.duration > SILENCE_MIN:
            parts.append(
                f'<span class="sil" style="left:{_pct(s.start_sec, total)};'
                f'width:{_pct(s.duration, total)}" title="{s.duration:.0f} s of silence"></span>'
            )
    for t in call.turns:
        if t.start_sec >= total:
            continue  # nothing left of the timeline to draw on
        fill = VERDICT_COLOR[faults[t.idx]] if t.idx in faults else SPEAKER_COLOR.get(t.speaker, "#999")
        if t.idx in context:
            fill = f"color-mix(in srgb, {VERDICT_COLOR[context[t.idx]]} 35%, #F5F6F3)"
        parts.append(
            f'<span class="turn {esc(t.speaker)}" style="left:{_pct(t.start_sec, total)};'
            f"width:{_pct(min(max(t.end_sec - t.start_sec, total / 400), total - t.start_sec), total)};"
            f'background:{fill}" '
            f'title="Turn {t.idx}, {SPEAKER_NAME.get(t.speaker, t.speaker)}, {mmss(t.start_sec)}"></span>'
        )
    onset = _onset_sec(call, audit)
    label = f"Call timeline, {mmss(total)} long, {len(call.turns)} turns"
    if onset is not None:
        c = VERDICT_COLOR.get(audit.verdict, VERDICT_COLOR["Fatal"])
        parts.append(
            f'<span class="onset" style="left:{_pct(onset, total)};--c:{c}">'
            f"<span>first fault, turn {audit.first_fault_turn}</span></span>"
        )
        label += f", first fault at turn {audit.first_fault_turn}"
    step = 30 if total > 120 else 10
    ticks = "".join(f'<span style="left:{_pct(s, total)}">{mmss(s)}</span>' for s in range(0, int(total) + 1, step))
    return (
        f'<div class="vl vl-strip" role="img" aria-label="{esc(label)}">'
        f'<span class="names"><span>VANI</span><span>Buyer</span></span>'
        f'<div class="lanes">{"".join(parts)}</div><div class="ticks">{ticks}</div></div>'
    )


def wall(audits: list[AuditRecord], calls: dict[str, CallRecord]) -> str:
    """Every audited call as one thin row drawn to a shared time scale, in four verdict columns.

    Column height is the count. Row length is the call length. A tick marks where the first fault starts.
    """
    lengths = sorted(call_length(c) for c in calls.values()) or [1.0]
    longest = lengths[-1]
    scale = lengths[math.ceil(0.95 * len(lengths)) - 1]  # nearest-rank p95; longer rows run off the edge
    cols = []
    for v in VERDICT_ORDER:
        members = [a for a in audits if a.verdict == v and a.call_id in calls]
        members.sort(key=lambda a: -call_length(calls[a.call_id]))
        rows = []
        for a in members:
            call = calls[a.call_id]
            length = call_length(call)
            bars = [f'<span class="len" style="width:{_pct(length, scale)}"></span>']
            bars += [
                f'<span class="{"b" if t.speaker == "bot" else "u"}" style="left:{_pct(t.start_sec, scale)};'
                f'width:{_pct(max(min(t.end_sec, scale) - t.start_sec, 0.6), scale)}"></span>'
                for t in call.turns
                if t.start_sec < scale
            ]
            onset = _onset_sec(call, a)
            if onset is not None and onset < scale:
                bars.append(f'<span class="f" style="left:{_pct(onset, scale)}"></span>')
            rows.append(f'<div class="row" title="{esc(a.call_id)}, {mmss(length)}">{"".join(bars)}</div>')
        cols.append(
            f'<div class="vl-col" style="--c:{VERDICT_COLOR[v]}"><h4>{VERDICT_LABEL[v]}<span>{len(members)}</span></h4>'
            f'<div class="rows" role="img" aria-label="{len(members)} calls graded {VERDICT_LABEL[v]}">'
            f"{''.join(rows)}</div></div>"
        )
    key = (
        f'<div class="vl-key"><span><i style="background:{SPEAKER_COLOR["bot"]}"></i>VANI speaking</span>'
        f'<span><i style="background:{SPEAKER_COLOR["user"]}"></i>Buyer speaking</span>'
        f'<span><i style="background:{VERDICT_COLOR["Fatal"]};width:2px"></i>First fault</span>'
        f"<span>Each row is one call, drawn to scale. Rows longer than {mmss(scale)} run off the edge. "
        f"The longest call is {mmss(longest)}.</span></div>"
    )
    return f'<div class="vl"><div class="vl-wall">{"".join(cols)}</div>{key}</div>'


def lede(audits: list[AuditRecord]) -> str:
    """One plain sentence with the verdict split, in place of a row of big numbers."""
    n = {v: sum(a.verdict == v for a in audits) for v in VERDICT_ORDER}
    graded = len(audits) - n["NeedsReview"]
    return (
        f'<p class="vl vl-lede"><b>{n["Fatal"]} of {graded}</b> graded calls are Fatal. '
        f"{n['NonFatal']} have a Non-Fatal fault and {n['Pass']} pass. "
        f"{n['NeedsReview']} more need a person to listen.</p>"
    )


def _who(t) -> str:
    extra = []
    tone = getattr(t, "tone", None)
    if t.speaker == "user" and tone in ("irritated", "confused"):
        extra.append(tone)
    if getattr(t, "overlap", None):
        extra.append("talks over")
    small = f"<small>{esc(', '.join(extra))}</small>" if extra else ""
    return f'<span class="who {esc(t.speaker)}">{esc(SPEAKER_NAME.get(t.speaker, t.speaker))}{small}</span>'


def script(call: CallRecord, audit: AuditRecord | None = None, names: dict[str, str] | None = None) -> str:
    """The transcript as a script: time, speaker, words. Long silences sit between turns.

    With an audit, turns in a Fatal or Non-Fatal fault get a coloured rule. Cited turns before the fault starts
    get a dashed rule. Each finding is named once, where it starts.
    With audit=None the script carries no audit information, for blind grading.
    """
    names = names or {}
    notes: dict[int, list[str]] = {}  # each finding speaks once: where its fault starts, and where its context starts
    for f in audit.findings if audit else []:
        if f.status not in FAILING:
            continue
        name = names.get(f.parameter, f.parameter)
        cited = sorted(e.turn_idx for e in f.evidence if e.turn_idx is not None)
        before = [i for i in cited if f.onset_turn is not None and i < f.onset_turn]
        start = f.onset_turn if f.onset_turn is not None else (cited[0] if cited else None)
        if before:
            notes.setdefault(before[0], []).append(f"Counts toward: {name}")
        if start is not None:
            notes.setdefault(start, []).append(f"{VERDICT_LABEL[f.status]}: {name} starts here")
    faults, context = _marks(audit)
    events = [(t.start_sec, 0, t) for t in call.turns]
    events += [(s.start_sec, 1, s) for s in call.silences if s.duration > SILENCE_MIN]
    rows = []
    for _, kind, ev in sorted(events, key=lambda e: (e[0], e[1])):
        if kind == 1:
            rows.append(f'<li class="gap">{ev.duration:.0f} s of silence at {mmss(ev.start_sec)}</li>')
            continue
        status = faults.get(ev.idx) or context.get(ev.idx)
        kind = "fault" if ev.idx in faults else "context" if ev.idx in context else ""
        cls = f' class="{kind}"' if kind else ""
        style = f' style="--c:{VERDICT_COLOR[status]}"' if status else ""
        note = f'<span class="note">{esc("; ".join(notes[ev.idx]))}</span>' if ev.idx in notes else ""
        rows.append(
            f'<li{cls}{style}><span class="t">{mmss(ev.start_sec)}<small>turn {ev.idx}</small></span>'
            f"{_who(ev)}<p>{esc(ev.text)}</p>{note}</li>"
        )
    return f'<ol class="vl vl-script" aria-label="Transcript">{"".join(rows)}</ol>'


def findings(audit: AuditRecord, names: dict[str, str] | None = None) -> str:
    """Faults first, each with its status colour, reason and evidence basis. Passed checks in one line."""
    names = names or {}
    bad = sorted((f for f in audit.findings if f.status in FAILING), key=lambda f: f.status != "Fatal")
    items = [
        f'<li style="--c:{VERDICT_COLOR[f.status]}"><b>{esc(names.get(f.parameter, f.parameter))}</b>'
        f"<span>{VERDICT_LABEL[f.status]}</span>{f' from turn {esc(f.onset_turn)}' if f.onset_turn is not None else ''}"
        f'<p>{esc(f.reason)}</p><p class="vl-pass">Evidence: {esc(f.basis.replace("_", " "))}</p></li>'
        for f in bad
    ]
    unknown = [names.get(f.parameter, f.parameter) for f in audit.findings if f.status == "Unknown"]
    passed = [names.get(f.parameter, f.parameter) for f in audit.findings if f.status == "Pass"]
    tail = ""
    if unknown:
        tail += f'<p class="vl-pass">Could not tell: {esc(", ".join(unknown))}.</p>'
    if passed:
        tail += f'<p class="vl-pass">Passed: {esc(", ".join(passed))}.</p>'
    body = f'<ul class="vl-finds">{"".join(items)}</ul>' if items else '<p class="vl-pass">No faults found.</p>'
    return f'<div class="vl">{body}{tail}</div>'


def cause_list(causes: list[Cause]) -> str:
    """Ranked causes. Each shows its frequency interval on one shared axis, so widths compare across rows."""
    top = max((c.freq_ci_high for c in causes), default=0.5)
    axis = min(1.0, (int(top * 10) + 1) / 10)
    lever = {"prompt": "Prompt fix", "flow_logic": "Flow or logic fix", "asr": "Speech recognition fix"}
    rows = []
    for i, c in enumerate(causes, 1):
        lo, hi = c.freq_ci_low / axis * 100, c.freq_ci_high / axis * 100
        color = VERDICT_COLOR["Fatal"] if c.fatal_calls else VERDICT_COLOR["NonFatal"]
        bar = (
            f'<span class="ci" role="img" aria-label="{c.freq_ci_low:.0%} to {c.freq_ci_high:.0%} of calls">'
            f'<span style="left:{lo:.1f}%;width:{max(hi - lo, 0.8):.1f}%;background:{color}"></span></span>'
        )
        counts = ", ".join(
            x
            for x in (
                f"{len(c.fatal_calls)} Fatal" if c.fatal_calls else "",
                f"{len(c.nonfatal_calls)} Non-Fatal" if c.nonfatal_calls else "",
            )
            if x
        )
        rows.append(
            f'<li><span class="n">{i}</span><span><b>{esc(c.name)}</b><small>{esc(c.description)}</small></span>'
            f"<span>{bar}<small>{c.freq_ci_low:.0%} to {c.freq_ci_high:.0%} of calls</small></span>"
            f"<span>{counts}<br><small>{lever.get(c.type, esc(c.type))}</small></span></li>"
        )
    return (
        f'<ol class="vl vl-causes" aria-label="Causes, most urgent first">{"".join(rows)}</ol>'
        f'<p class="vl vl-axis">Bars share one axis from 0 to {axis:.0%} of graded calls (95% interval).</p>'
    )
