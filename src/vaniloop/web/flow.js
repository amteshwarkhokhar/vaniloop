// vaniloop Pipeline: every step on one horizontal line of panes, a waveform that shows the run, and a call mode
// with the transcript (faults in red) and each step's output for that call. Builds on window.VL and VL.console.
// Every data string goes through esc().
(() => {
  "use strict";
  const V = window.VL;
  if (!V || !V.console) return;
  const { S, P, esc, str, num, fmt, own } = V;
  const K = V.console;
  const F = { calls: null, call: null, data: null, err: null, seq: 0 };
  const LOOP = ["audit", "judge", "regrade", "issues", "fix", "test", "report"];
  const LOOP_NAME = { audit: "Audit", judge: "Judge", regrade: "Regrade", issues: "Issues", fix: "Fix", test: "Test", report: "Report" };
  const BARS = 10; // waveform bars per loop step
  const VERDICTS = ["Fatal", "NonFatal", "NeedsReview", "Pass"];
  const WORDS = { Fatal: "Fatal", NonFatal: "Non-fatal", NeedsReview: "Needs review", Unknown: "Unknown", Pass: "Pass", NotApplicable: "Not applicable" };
  const stage = (k) => { const r = S.stage[k]; return r && !r.e ? r.v : null; };
  const name = (p) => own(V.NAMES, p) || str(p).replace(/_/g, " ");
  const word = (v) => own(WORDS, v) || str(v);
  const clock = (s) => (num(s) == null ? "–" : `${Math.floor(s / 60)}:${String(Math.floor(s % 60)).padStart(2, "0")}`);
  const list = (x) => (Array.isArray(x) ? x.filter((y) => y && typeof y === "object") : []);

  // ---- the run: one state per loop step from the job log ----
  function run() {
    const j = K.D.job;
    const st = { steps: {}, cur: null, label: "Ready", tone: "idle" };
    LOOP.forEach((s) => (st.steps[s] = "wait"));
    if (!j) {
      const last = (K.C.jobs || []).find((x) => x && x.stage === "loop");
      if (last) { LOOP.forEach((s) => (st.steps[s] = last.status === "done" ? "done" : "wait")); st.label = `Last run: ${str(last.status)} at ${str(last.ended_ist || last.started_ist)} IST`; st.tone = last.status === "done" ? "done" : "idle"; }
      return st;
    }
    if (j.stage === "loop") {
      for (const ln of K.D.lines) {
        let m;
        if ((m = /^LOOP_STEP (\d+)\/\d+ (\w+)/.exec(ln))) { if (st.cur) st.steps[st.cur] = "done"; st.cur = m[2]; st.steps[m[2]] = "run"; }
        else if (/^LOOP_DONE/.test(ln)) { LOOP.forEach((s) => (st.steps[s] = "done")); st.cur = null; }
        else if ((m = /^LOOP_FAILED (\w+) ?(.*)$/.exec(ln))) { st.steps[m[1]] = "fail"; st.cur = null; st.fail = `${m[1]}: ${m[2]}`; }
      }
    } else if (LOOP.includes(j.stage) || j.stage === "prove") {
      const s = j.stage === "prove" ? "test" : j.stage;
      st.steps[s] = "run"; st.cur = s;
    }
    if (j.status !== "running") LOOP.forEach((s) => { if (st.steps[s] === "run") st.steps[s] = j.status === "done" ? "done" : "fail"; });
    const done = LOOP.filter((s) => st.steps[s] === "done").length;
    st.label = j.status === "running" ? `Running ${own(LOOP_NAME, st.cur) || str(j.stage)} · step ${done + 1} of ${LOOP.length} · started ${str(j.started_ist)} IST`
      : st.fail ? `Stopped at ${st.fail}` : `${j.status === "done" ? "Done" : str(j.status)} · started ${str(j.started_ist)} IST${num(j.cost_usd) != null ? ` · $${fmt(j.cost_usd, 2)}` : ""}`;
    st.tone = j.status === "running" ? "run" : st.fail || j.status !== "done" ? "fail" : "done";
    return st;
  }
  // A waveform: ten bars per step, a fixed voice-like shape. Done bars are indigo, the running step's bars move.
  function wave(st) {
    const bars = LOOP.map((s, k) => Array.from({ length: BARS }, (_, i) => {
      const n = k * BARS + i, h = Math.round(22 + 70 * Math.abs(Math.sin(n * 0.71) * Math.cos(n * 0.23)));
      return `<i class="w-${st.steps[s]}" style="height:${h}%;--d:${i * 70}ms"></i>`;
    }).join("")).join("");
    const done = LOOP.filter((s) => st.steps[s] === "done").length;
    return `<section class="wave-box" aria-label="Run progress"><div class="wave" role="progressbar" aria-valuemin="0" aria-valuemax="${LOOP.length}" aria-valuenow="${done}" aria-valuetext="${esc(st.label)}">${bars}</div>` +
      `<ol class="wave-steps">${LOOP.map((s) => `<li class="ws-${st.steps[s]}">${esc(own(LOOP_NAME, s))}</li>`).join("")}</ol>` +
      `<p class="wave-label t-${st.tone === "fail" ? "fatal" : st.tone === "done" ? "pass" : "review"}"><span class="dot"></span>${esc(st.label)}</p></section>`;
  }

  // ---- the panes ----
  const pane = (n, title, sub, body, wide) =>
    `<section class="pn${wide ? " wide" : ""}" aria-label="${esc(title)}"><header class="pn-h"><span class="pn-n">${esc(n)}</span><div><h2>${esc(title)}</h2><p class="src">${esc(sub)}</p></div></header><div class="pn-b">${body}</div></section>`;
  const kv = (rows) => `<dl class="kvs">${rows.map(([k, v]) => `<dt>${esc(k)}</dt><dd>${esc(v)}</dd>`).join("")}</dl>`;
  const none = (t) => `<p class="empty">${esc(t)}</p>`;

  function allPanes() {
    const ing = stage("ingest"), au = stage("audit"), is = stage("issues"), fx = stage("fix"), ga = stage("gate");
    const calls = list(F.calls), rank = (c) => { const i = VERDICTS.indexOf(c.verdict); return i < 0 ? 9 : i; };
    const squares = calls.slice().sort((a, b) => rank(a) - rank(b)).map((c) =>
      `<span class="cd t-${c.verdict ? esc(V.tone(c.verdict)) : "none"}" data-pick="${esc(c.call_id)}" title="${esc(c.call_id)} · ${esc(word(c.verdict) || "Not graded")}"></span>`).join("");
    const p1 = ing ? kv([["Calls", fmt(ing.calls)], ["With audio", fmt(ing.with_audio)], ["Minutes", fmt(ing.minutes_total)], ["Median length", `${fmt(ing.median_sec, 1)} s`]]) +
      (squares ? `<p class="src pick-hint">Pick a square to follow one call through every step.</p><div class="sq">${squares}</div>` : "") : none("No ingest results.");
    const v = (au && au.verdicts) || {}, tot = (au && au.total) || 1;
    const checks = au ? Object.entries(au.by_parameter || {}).map(([p, c]) => [p, (c.Fatal || 0) + (c.NonFatal || 0), c.Fatal || 0]).sort((a, b) => b[2] - a[2] || b[1] - a[1]) : [];
    const p2 = au ? `<div class="vbar" aria-hidden="true">${VERDICTS.filter((k) => v[k]).map((k) => `<span class="t-${V.tone(k)}" style="width:${(100 * v[k]) / tot}%"></span>`).join("")}</div>` +
      `<p class="legend">${VERDICTS.map((k) => `<span class="lg">${V.chip(k)}<b>${esc(fmt(v[k] || 0))}</b></span>`).join("")}</p>` +
      `<ul class="rows">${checks.map(([p, n, f]) => `<li><span>${esc(name(p))}</span><b>${esc(fmt(f))} fatal · ${esc(fmt(n - f))} non-fatal</b></li>`).join("")}</ul>` : none("No audit results.");
    const p3 = is ? `<ol class="rows">${list(is.causes).slice(0, 12).map((c) => `<li><span>${esc(c.name)}</span><b>${esc(fmt(num(c.fatal) || 0))} fatal · ${esc(fmt(num(c.nonfatal) || 0))} non-fatal</b></li>`).join("")}</ol>` : none("No causes yet.");
    const p4 = fx ? (list(fx.patches).map((p) => `<div class="patch"><p><b>${esc(p.cause_id)}</b> · attempt ${esc(p.attempt)} ${p.gate_decision ? V.badge(p.gate_decision === "ship" ? "pass" : p.gate_decision === "reject" ? "fatal" : "review", str(p.gate_decision)) : ""}</p><p class="old">${esc(p.old_text)}</p><p class="new">${esc(p.new_text)}</p></div>`).join("") || none("No patches yet.")) +
      `<p class="src">${esc(fmt(list(fx.tickets).length + (Array.isArray(fx.tickets) ? fx.tickets.filter((t) => typeof t === "string").length : 0)))} tickets for tool or data faults</p>` : none("No fix results.");
    const p5 = ga ? `<ul class="rows">${list(ga.gates).map((g) => `<li><span>${esc(g.cause_id)} · attempt ${esc(g.attempt)}</span><b>${esc(fmtRate(g.old_fail_rate))} → ${esc(fmtRate(g.new_fail_rate))} ${esc(str(g.decision))}</b></li>`).join("") || '<li class="empty">No tests yet.</li>'}</ul>` : none("No test results.");
    return pane(1, "Ingest", "Calls read in", p1) + pane(2, "Audit", "Gemini lays out, Jev labels, Python grades", p2) + pane(3, "Issues", "Faults grouped into causes", p3) +
      pane(4, "Fix", "Prompt patches", p4) + pane(5, "Test", "Old prompt vs new, on replays", p5);
  }
  const fmtRate = (r) => (num(r) == null ? "–" : `${Math.round(r * 100)}%`);

  // The transcript: a fault turn is marked in its tone, the check and reason sit on its onset turn,
  // and each evidence quote is marked inside the text.
  function transcript(d) {
    const turns = list(d.call && d.call.turns).filter((t) => Number.isInteger(t.idx));
    if (!turns.length) return none("This call has no transcript turns.");
    const bad = list(d.audit && d.audit.findings).filter((f) => f.status === "Fatal" || f.status === "NonFatal");
    const at = new Map(), quotes = new Map(), labels = new Map();
    const tone = (f) => (f.status === "Fatal" ? "fatal" : "nonfatal");
    for (const f of bad) {
      if (Number.isInteger(f.onset_turn)) (at.get(f.onset_turn) || at.set(f.onset_turn, []).get(f.onset_turn)).push(f);
      for (const e of list(f.evidence)) if (Number.isInteger(e.turn_idx)) (quotes.get(e.turn_idx) || quotes.set(e.turn_idx, []).get(e.turn_idx)).push({ q: str(e.quote), t: tone(f) });
    }
    const lg = d.ledger || {};
    for (const b of list(lg.bot_turns)) {
      const bits = [b.slot && `${str(b.slot)}${b.spec_name ? `: ${str(b.spec_name)}` : ""}`, b.question_fits_product === false && "✕ question does not fit", b.question_fits_product === true && "✓ fits", b.fits_confirmed_product === false && "✕ off product"].filter(Boolean);
      if (Number.isInteger(b.idx) && bits.length) labels.set(b.idx, bits);
    }
    for (const u of list(lg.user_turns)) {
      const bits = [u.declined && "declined", u.frustrated && "frustrated", u.asks_for_human && "asks for a person", u.gave_value_for && `gave ${str(u.gave_value_for)}`].filter(Boolean);
      if (Number.isInteger(u.idx) && bits.length) labels.set(u.idx, (labels.get(u.idx) || []).concat(bits));
    }
    // Find each quote in the raw text, keep spans that do not overlap, then escape every piece on its own.
    const mark = (text, qs) => {
      const raw = str(text), spans = [];
      for (const { q, t } of qs || []) {
        const needle = str(q).trim(), i = needle.length > 2 ? raw.indexOf(needle) : -1;
        if (i >= 0 && !spans.some((x) => i < x.e && i + needle.length > x.s)) spans.push({ s: i, e: i + needle.length, t });
      }
      let out = "", at = 0;
      for (const x of spans.sort((a, b) => a.s - b.s)) { out += `${esc(raw.slice(at, x.s))}<mark class="t-${x.t}">${esc(raw.slice(x.s, x.e))}</mark>`; at = x.e; }
      return out + esc(raw.slice(at));
    };
    return `<ol class="tr">${turns.map((t) => {
      const fs = at.get(t.idx) || [], worst = fs.some((f) => f.status === "Fatal") ? "fatal" : fs.length ? "nonfatal" : (quotes.get(t.idx) || [])[0] ? quotes.get(t.idx)[0].t : "";
      return `<li id="ft-${esc(t.idx)}" class="tt ${t.speaker === "bot" ? "bot" : "user"}${worst ? ` fault t-${worst}` : ""}"><span class="tt-h"><b>${t.speaker === "bot" ? "VANI" : "Buyer"}</b> <span class="mono">${esc(clock(num(t.start_sec)))}</span> <span class="src">turn ${esc(t.idx)}</span></span>` +
        `<p>${mark(t.text, quotes.get(t.idx))}</p>` +
        (labels.get(t.idx) ? `<p class="lbls">${labels.get(t.idx).map((x) => `<span class="lbl${/✕|declined|frustrated/.test(x) ? " bad" : ""}">${esc(x)}</span>`).join("")}</p>` : "") +
        fs.map((f) => `<p class="flag t-${tone(f)}">${f.status === "Fatal" ? "✕" : "◐"} ${esc(word(f.status))} · ${esc(name(f.parameter))}<span>${esc(f.reason)}</span></p>`).join("") + `</li>`;
    }).join("")}</ol>`;
  }
  function callPanes(d) {
    const c = d.call || {}, a = d.audit || {}, turns = list(c.turns);
    const findings = list(a.findings).slice().sort((x, y) => VERDICTS.concat("Unknown").indexOf(x.status) - VERDICTS.concat("Unknown").indexOf(y.status));
    const p0 = transcript(d);
    const p1 = kv([["Length", clock(num(c.duration_sec))], ["Turns", fmt(turns.length)], ["Language", str(c.language) || "–"], ["Silences", fmt(list(c.silences).length)], ["Audio", str(c.audio_path).split("/").pop() || "–"]]);
    const p2 = `<p>${a.verdict ? V.chip(a.verdict) : ""} <span class="pill">${esc(str(a.scope || "unclear").replace(/_/g, " "))}</span></p>` +
      (Array.isArray(a.flags) && a.flags.length ? `<p class="chips">${a.flags.map((x) => `<span class="pill">${esc(str(x).replace(/_/g, " "))}</span>`).join("")}</p>` : "") +
      `<ul class="finds">${findings.map((f) => `<li class="t-${esc(V.tone(f.status))}">${V.chip(f.status)} <b>${esc(name(f.parameter))}</b>${Number.isInteger(f.onset_turn) ? ` <button type="button" class="turn-go" data-turn="${esc(f.onset_turn)}" data-view>turn ${esc(f.onset_turn)}</button>` : ""}<p>${esc(f.reason)}</p><p class="src">${esc(str(f.basis).replace(/_/g, " "))}</p></li>`).join("") || '<li class="empty">No checks.</li>'}</ul>`;
    const lg = d.ledger;
    const p3 = lg ? kv([["Judge", str(d.ledger_source) || "–"], ["Product at start", str(lg.initial_product) || "–"], ["Product at end", str(lg.final_product) || "–"], ["Product changed at", lg.product_changed_at_turn == null ? "–" : `turn ${lg.product_changed_at_turn}`]]) +
      `<p class="src">Turn labels sit under each turn in the transcript: ${esc(fmt(list(lg.bot_turns).length))} VANI turns, ${esc(fmt(list(lg.user_turns).length))} buyer turns.</p>` : none("No turn labels for this call.");
    const ROLE = { fatal: ["Fatal", "fatal"], nonfatal: ["Non-fatal", "nonfatal"], symptom: ["Symptom", "review"] };
    const p4 = list(d.causes).map((x) => { const r = own(ROLE, x.role) || [str(x.role), "review"]; return `<div class="cause">${V.badge(r[1], r[0])} <b>${esc(x.name || x.cause_id)}</b><p class="src">${esc(x.cause_id)} · ${esc(str(x.type))} · ${esc(str(x.status).replace(/_/g, " "))}</p></div>`; }).join("") || none("This call is in no cause.");
    const p5 = list(d.patches).map((p) => `<div class="patch"><p><b>${esc(p.cause_id)}</b> · attempt ${esc(p.attempt)} ${p.decision || p.gate_decision ? `<span class="pill">${esc(str(p.decision || p.gate_decision))}</span>` : ""}</p><p class="old">${esc(p.old_text)}</p><p class="new">${esc(p.new_text)}</p>${p.rationale ? `<p class="src">${esc(p.rationale)}</p>` : ""}</div>`).join("") || none("No patch for this call's causes yet.");
    const p6 = list(d.gates).length ? `<ul class="rows">${list(d.gates).map((g) => `<li><span>${esc(g.cause_id)} · attempt ${esc(g.attempt)}</span><b>old prompt failed ${esc(g.old_fails)} of ${esc(g.k)} · new failed ${esc(g.new_fails)} of ${esc(g.k)} · ${esc(str(g.decision))}</b></li>`).join("")}</ul>` : none("This call was not replayed in a test.");
    return pane("▶", "Transcript", `${turns.length} turns · faults marked in red`, p0, true) + pane(1, "Ingest", "The call as read in", p1) + pane(2, "Audit", "Findings for this call", p2) +
      pane(3, "Judge", "Jev turn labels", p3) + pane(4, "Issues", "Causes that hold this call", p4) + pane(5, "Fix", "Patches for those causes", p5) + pane(6, "Test", "This call's replays", p6);
  }

  P.flow = () => {
    const calls = list(F.calls);
    const opts = calls.slice(0, 800).map((c) => `<option value="${esc(c.call_id)}">`).join("");
    const head = `<header class="phead"><div class="phead-t"><p class="kicker">One page</p><h1 class="ptitle" tabindex="-1">Pipeline</h1>` +
      `<p class="lede">Every step on one line. Pick a call to follow it from its transcript to its test.</p></div></header>`;
    const pick = `<div class="flow-pick"><div class="seg" role="group" aria-label="Show"><button type="button" data-op="flow-all" data-view aria-pressed="${!F.call}">All calls</button>` +
      `<button type="button" data-op="flow-worst" data-view aria-pressed="false">Worst call</button></div>` +
      `<label class="label" for="flow-id">Call</label><input id="flow-id" class="inp" list="flow-ids" autocomplete="off" placeholder="type or pick a call id" value="${esc(F.call || "")}" data-op="flow-id"><datalist id="flow-ids">${opts}</datalist>` +
      (F.call ? `<span class="pill mono">${esc(F.call)}</span>` : "") + `</div>`;
    const body = !F.call ? allPanes() : F.err ? none(`This call could not load (${F.err}).`) : F.data ? callPanes(F.data) : none("Loading the call.");
    return head + `<div id="flow-wave">${wave(run())}</div>` + pick + `<div class="lane" tabindex="0" aria-label="Steps, left to right"><div class="lane-in">${body}</div></div>`;
  };
  async function pickCall(id) {
    F.call = str(id).trim() || null; F.data = null; F.err = null;
    const n = ++F.seq;
    V.show();
    if (!F.call) return;
    const r = await K.call("GET", `/api/call/${encodeURIComponent(F.call)}`);
    if (n !== F.seq) return;
    if (r.ok) F.data = r.data; else F.err = str(r.data.error) || `status ${r.status}`;
    if (S.now === "flow") V.show();
  }
  document.addEventListener("click", (e) => {
    const sq = e.target.closest && e.target.closest("[data-pick]");
    if (sq && S.now === "flow") { pickCall(sq.dataset.pick); return; }
    const t = e.target.closest && e.target.closest("button");
    const op = t && t.dataset && t.dataset.op;
    if (op === "flow-all") pickCall("");
    else if (op === "flow-worst") { const c = list(F.calls)[0]; if (c) pickCall(c.call_id); }
    else if (t && t.classList && t.classList.contains("turn-go")) {
      const row = document.getElementById(`ft-${t.dataset.turn}`);
      if (row) { row.scrollIntoView({ behavior: "smooth", block: "center", inline: "nearest" }); row.classList.add("flash"); setTimeout(() => row.classList.remove("flash"), 1200); }
    }
  });
  document.addEventListener("change", (e) => { const d = e.target && e.target.dataset; if (d && d.op === "flow-id") pickCall(e.target.value); });

  V.ITEMS.push({ id: "flow", group: "operate", name: "Pipeline", side: "top", fam: "flow" });
  V.enter.flow = async () => {
    const c = await K.call("GET", "/api/calls");
    if (c.ok && Array.isArray(c.data)) F.calls = c.data;
    if (S.now === "flow") V.show();
  };
  // Follow the run: redraw only the waveform on each log poll, so the panes keep their scroll.
  const prev = V.onJob;
  V.onJob = () => {
    if (prev) prev();
    if (S.now !== "flow") return;
    const w = document.getElementById("flow-wave");
    if (w) w.innerHTML = wave(run()); else V.show();
  };
})();
