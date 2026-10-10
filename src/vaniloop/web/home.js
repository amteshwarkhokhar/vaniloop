// vaniloop home: the calls in this folder, one button for the whole loop, and the five steps with their latest results.
// Builds on window.VL (dash.js) and window.VL.console (console.js). Every data string goes through esc().
(() => {
  "use strict";
  const V = window.VL;
  if (!V || !V.console) return;
  const { S, P, esc, str, num, fmt, own } = V;
  const K = V.console;
  const $ = (id) => document.getElementById(id);
  const H = { calls: null, plan: null, planErr: null, seen: false, replay: null, last: {}, lastRun: "", endedFor: null };
  const STEPS = [
    { id: "ingest", name: "Ingest", what: "Read in the calls" },
    { id: "audit", name: "Audit", what: "Grade every call" },
    { id: "issues", name: "Issues", what: "Group the faults into causes" },
    { id: "fix", name: "Fix", what: "Write a prompt patch per cause" },
    { id: "gate", name: "Test", what: "Replay calls to prove each patch" },
  ];
  // The card a job step lights up, and what the card says while that step runs.
  const CARD = { ingest: "ingest", audit: "audit", judge: "audit", regrade: "audit", issues: "issues", fix: "fix", test: "gate", prove: "gate" };
  const DOING = {
    ingest: "Reading in calls", audit: "Gemini lays out new calls", judge: "Jev labels each turn", regrade: "Python rules grade each call",
    issues: "Grouping faults into causes", fix: "Writing prompt patches", test: "Replaying calls with each patch", prove: "Replaying calls with the patch",
  };
  const VERDICTS = ["Fatal", "NonFatal", "NeedsReview", "Pass"];
  const WORDS = { Fatal: "Fatal", NonFatal: "Non-fatal", NeedsReview: "Needs review", Pass: "Pass" };
  const STATE = { wait: ["Waiting", "review", "○"], run: ["Running", "nonfatal", "◐"], done: ["Done", "pass", "✓"], fail: ["Stopped", "fatal", "✕"] };
  const CHECK = '<svg viewBox="0 0 16 16" aria-hidden="true"><path d="m3.5 8.5 3 3 6-7"/></svg>';
  const PLAY = '<svg viewBox="0 0 16 16" aria-hidden="true"><path d="M5 3.2v9.6L12.6 8z"/></svg>';
  const stage = (k) => { const r = S.stage[k]; return r && !r.e ? r.v : null; };
  const ro = () => !!(K.C.con && K.C.con.read_only);
  const running = () => !!(K.D.job && K.D.job.status === "running");
  const replaying = () => !!(H.replay && !H.replay.ended);
  const plural = (n, one, many) => `${fmt(n)} ${n === 1 ? one : many}`;
  const count = (x) => (Array.isArray(x) ? x.length : num(x) || 0);

  // One state per card, read from the job log: wait, run, done or fail. null when no job is on screen.
  function live() {
    if (H.replay) return H.replay.st;
    const j = K.D.job;
    if (!j) return null;
    const st = { cards: {}, doing: {}, prog: {}, summary: null, fail: null, stage: j.stage };
    const set = (card, step, prog) => { st.cards[card] = "run"; st.doing[card] = own(DOING, step) || step; st.prog[card] = prog || null; };
    if (j.stage === "loop") {
      STEPS.forEach((s) => (st.cards[s.id] = "wait"));
      st.cards.ingest = "done"; // the loop grades the calls already read in
      let cur = null;
      for (const ln of K.D.lines) {
        let m;
        if ((m = /^LOOP_STEP \d+\/\d+ (\w+)/.exec(ln))) {
          const card = own(CARD, m[1]) || null;
          if (cur && cur !== card) st.cards[cur] = "done";
          if (card) set(card, m[1]);
          cur = card;
        } else if ((m = /^LOOP_SUMMARY (.*)$/.exec(ln))) {
          try { st.summary = JSON.parse(m[1]); } catch (e) { st.summary = null; }
        } else if (/^LOOP_DONE/.test(ln)) { STEPS.forEach((s) => (st.cards[s.id] = "done")); cur = null; }
        else if ((m = /^LOOP_FAILED (\w+) ?(.*)$/.exec(ln))) {
          const card = own(CARD, m[1]) || cur;
          if (card) st.cards[card] = "fail";
          st.fail = { step: m[1], reason: m[2] || "failed" };
          cur = null;
        }
      }
    } else if (j.stage === "submit") {
      st.cards.ingest = "wait"; st.cards.audit = "wait";
      for (const ln of K.D.lines) {
        let m;
        if ((m = /^STEP (\w+) (\d+)\/(\d+)/.exec(ln))) {
          const card = own(CARD, m[1]);
          if (card === "audit") st.cards.ingest = "done";
          if (card) set(card, m[1], [+m[2], +m[3]]);
        } else if (/^SUBMIT_DONE/.test(ln)) { st.cards.ingest = "done"; st.cards.audit = "done"; }
      }
    } else if (own(CARD, j.stage)) set(CARD[j.stage], j.stage);
    if (j.status !== "running") for (const k of Object.keys(st.cards)) if (st.cards[k] === "run") st.cards[k] = j.status === "done" ? "done" : "fail";
    return st;
  }

  // ---- the parts of the page ----
  const big = (n, label) => `<p class="st-big"><b>${esc(fmt(n))}</b> ${esc(label)}</p>`;
  function result(id) {
    const d = stage(id);
    if (!d) return '<p class="st-line">No results yet.</p>';
    if (id === "ingest") return big(d.calls, d.calls === 1 ? "call" : "calls") + `<p class="st-line">${esc(fmt(d.with_audio))} with audio · ${esc(fmt(d.minutes_total))} minutes</p>`;
    if (id === "audit") {
      const v = d.verdicts || {}, tot = d.total || 1;
      return big(v.Fatal || 0, "fatal") +
        `<div class="vbar mini" aria-hidden="true">${VERDICTS.filter((k) => v[k]).map((k) => `<span class="t-${V.tone(k)}" style="width:${(100 * v[k]) / tot}%"></span>`).join("")}</div>` +
        `<p class="st-line">${esc(fmt(v.NonFatal || 0))} non-fatal · ${esc(fmt(v.NeedsReview || 0))} to review · ${esc(fmt(v.Pass || 0))} pass</p>`;
    }
    if (id === "issues") { const c = d.causes || []; return big(c.length, c.length === 1 ? "cause" : "causes") + (c[0] ? `<p class="st-line">Top: ${esc(c[0].name)}</p>` : ""); }
    if (id === "fix") return big(d.patches.length, d.patches.length === 1 ? "patch" : "patches") + `<p class="st-line">${esc(plural(d.tickets.length, "ticket", "tickets"))} for tool or data faults</p>`;
    const g = d.gates || [];
    return big(g.filter((x) => x.decision === "ship").length, "passed") + `<p class="st-line">${esc(plural(g.length, "test", "tests"))} so far</p>`;
  }
  function card(s, i, st) {
    const state = (st && st.cards[s.id]) || "idle", p = st && st.prog[s.id], sb = own(STATE, state);
    const doing = state === "run" ? `${st.doing[s.id] || "Working"}${p ? ` · ${fmt(p[0])} of ${fmt(p[1])}` : ""}` : state === "fail" && st.fail ? `Stopped: ${st.fail.reason}` : s.what;
    const bar = state !== "run" ? "" : p
      ? `<div class="st-bar" role="progressbar" aria-label="${esc(s.name)} progress" aria-valuemin="0" aria-valuemax="${esc(p[1])}" aria-valuenow="${esc(p[0])}"><i style="width:${Math.round((100 * p[0]) / Math.max(1, p[1]))}%"></i></div>`
      : `<div class="st-bar ind" role="progressbar" aria-label="${esc(s.name)} running"><i></i></div>`;
    return `<li class="st st-${state}" data-step="${esc(s.id)}"><div class="st-h"><span class="st-n" aria-hidden="true">${state === "done" ? CHECK : i + 1}</span><h3>${esc(s.name)}</h3>${sb ? V.badge(sb[1], sb[0], sb[2]) : ""}</div>` +
      `<p class="st-do">${esc(doing)}</p>${bar}<div class="st-res">${state === "wait" || state === "run" ? '<p class="st-line">Results show when this step ends.</p>' : result(s.id)}</div><a class="st-more" href="#/${esc(s.id)}">Details<span aria-hidden="true"> ›</span></a></li>`;
  }
  function planText(p) {
    const step = (n) => (Array.isArray(p.steps) && p.steps.find((s) => s && s.name === n)) || {};
    const f = step("fix"), t = step("test");
    const parts = [
      num(p.new_calls) ? `${plural(p.new_calls, "new call", "new calls")} to grade` : "No new calls to grade",
      f.will_run && num(f.count) ? `${plural(f.count, "patch", "patches")} to write` : "",
      t.will_run && num(t.count) ? `${plural(t.count, "patch", "patches")} to test` : "",
      num(p.estimate_usd) == null ? "Cost not known yet" : `About $${fmt(p.estimate_usd, 2)}`,
    ];
    return parts.filter(Boolean).join(" · ");
  }
  function runBox(st) {
    const con = K.C.con, busy = running(), cf = K.C.confirm && K.C.confirm.stage === "loop", m = K.C.msg.loop;
    const off = !con || ro() || busy || replaying() ? " disabled" : "";
    const label = busy && K.D.job.stage === "loop" ? "Running the loop…" : "Run the loop";
    const est = cf && num(K.C.confirm.estimate) != null ? `Estimate $${fmt(K.C.confirm.estimate, 2)}.` : "Estimate not known.";
    const plan = H.plan ? planText(H.plan) : H.planErr ? `The plan could not load (${H.planErr}).` : "Reading the plan.";
    const steps = H.plan && Array.isArray(H.plan.steps) ? H.plan.steps.filter((s) => s && typeof s === "object") : [];
    return (cf
      ? `<div class="confirm" role="alertdialog" aria-label="Confirm the loop"><p><b>This run calls paid models.</b> ${esc(est)} It never ships a patch.</p>` +
        `<button type="button" class="btn" data-op="home-cancel">Cancel</button> <button type="button" class="btn pri" data-op="home-go">Run the loop</button></div>`
      : `<button type="button" class="btn pri lg" data-op="home-run"${off}>${PLAY}${esc(label)}</button>`) +
      `<div class="run-meta"><p class="plan-line">${esc(plan)}</p>` +
      (steps.length ? `<details class="plan"><summary>What will run</summary><ol>${steps.map((s) => `<li><b>${esc(str(s.name))}</b> ${s.will_run ? "" : '<span class="src">(skipped)</span> '}<span class="src">${esc(s.why)}</span></li>`).join("")}</ol></details>` : "") + `</div>` +
      (busy || cf ? "" : `<button type="button" class="btn" data-op="home-replay" data-view${replaying() ? " disabled" : ""}>Replay the last run</button>`) +
      (m && m.err ? `<p class="err" role="alert">${esc(m.err)}</p>` : "") +
      (ro() ? `<p class="note ro">This data folder is read-only, so Run is off. Replay shows the saved results step by step. Nothing is graded again.</p>` : "");
  }
  function callsPanel(st) {
    const list = Array.isArray(H.calls) ? H.calls.filter((c) => c && typeof c === "object") : null, ing = stage("ingest");
    const n = list ? list.length : ing ? ing.calls : null;
    const by = {};
    (list || []).forEach((c) => (by[c.verdict] = (by[c.verdict] || 0) + 1));
    const rank = (c) => { const i = VERDICTS.indexOf(c.verdict); return i < 0 ? 9 : i; };
    const dots = (list || []).slice().sort((a, b) => rank(a) - rank(b)).map((c, i) =>
      `<a class="cd t-${c.verdict ? esc(V.tone(c.verdict)) : "none"}" href="/#/call/${esc(encodeURIComponent(str(c.call_id)))}" tabindex="-1" title="${esc(c.call_id)} · ${esc(own(WORDS, c.verdict) || "Not graded")}" style="--i:${i}"></a>`).join("");
    const scan = st && (st.cards.audit === "run" || st.cards.ingest === "run") ? " scan" : "";
    const html = `<section class="card calls" aria-labelledby="calls-h"><div class="calls-h"><h2 id="calls-h"><b>${esc(fmt(n))}</b> calls</h2>` +
      `<span class="src">${esc(K.C.con ? K.C.con.data_dir_name : "")}</span><a class="btn sm" href="#/submit">Add calls…</a></div>` +
      (list ? `<p class="legend">${VERDICTS.map((k) => `<span class="lg">${V.chip(k)}<b>${esc(fmt(by[k] || 0))}</b></span>`).join("")}</p>` +
        `<div id="home-grid" class="calls-grid${H.seen ? " seen" : ""}${scan}" role="img" aria-label="${esc(VERDICTS.map((k) => `${by[k] || 0} ${own(WORDS, k)}`).join(", "))}">${dots}</div>` +
        `<p class="calls-foot src">Each square is one call, worst first. Open a call for its transcript and the proof behind each grade, or see <a href="/#/">all calls</a>.</p>`
        : '<p class="empty">Loading the calls.</p>') + `</section>`;
    if (list) H.seen = true; // the squares pop in once
    return html;
  }
  function doneBox(st) {
    if (!st) return "";
    if (st.stage === "replay") return H.replay && H.replay.ended ? '<p class="note done-box" role="status"><b>Replay finished.</b> These are the saved results. Nothing was graded again.</p>' : "";
    if (st.stage !== "loop") return "";
    if (st.fail) return `<p class="note done-box" role="status"><b>The loop stopped at ${esc(st.fail.step)}.</b> ${esc(st.fail.reason)}</p>`;
    const s = st.summary;
    if (!s || typeof s !== "object") return "";
    return `<div class="card done-box" role="status"><h3>The loop finished</h3><p>${esc(plural(count(s.ready_to_ship), "patch", "patches"))} ready to ship · ` +
      `${esc(plural(count(s.tickets), "ticket", "tickets"))} · ${esc(plural(count(s.watch), "cause", "causes"))} watched${num(s.cost_usd) != null ? ` · $${esc(fmt(s.cost_usd, 2))}` : ""}</p>` +
      `<p class="src">Nothing ships without you. Approve each patch in the <a href="#/tracker">Tracker</a>.</p></div>`;
  }
  const waiting = () => {
    const t = V.vals ? str(V.vals().tracker) : "";
    return t && t !== "None waiting" ? `<p class="home-wait"><a href="#/tracker">${esc(t)} in the Tracker</a>. The loop never ships a patch: you approve each one there.</p>` : "";
  };

  P.home = () => {
    const st = live();
    STEPS.forEach((s, i) => (H.last[s.id] = card(s, i, st)));
    H.lastRun = runBox(st);
    return `<header class="phead"><div class="phead-t"><p class="kicker">Quality loop</p><h1 class="ptitle" tabindex="-1">Run the loop</h1>` +
      `<p class="lede">Grade VANI calls, find why calls fail, write a prompt fix and prove it on replays. Nothing ships without you.</p></div></header>` +
      `<div id="home-run" class="home-run">${H.lastRun}</div>` + callsPanel(st) +
      `<h2 class="sec">Steps</h2><ol id="home-steps" class="steps" aria-label="Loop steps">${STEPS.map((s) => H.last[s.id]).join("")}</ol>` +
      `<div id="home-done" aria-live="polite">${doneBox(st)}</div>` + waiting();
  };
  // Redraw only what changed, so the squares and the running bars keep their animation.
  function drawLive() {
    if (S.now !== "home") return;
    const ol = $("home-steps"), rb = $("home-run");
    if (!ol || !ol.querySelector || !rb) { V.show(); return; }
    const st = live();
    STEPS.forEach((s, i) => {
      const html = card(s, i, st), li = ol.querySelector(`[data-step="${s.id}"]`);
      if (li && H.last[s.id] !== html) { li.outerHTML = html; H.last[s.id] = html; }
    });
    const run = runBox(st);
    if (run !== H.lastRun) { rb.innerHTML = run; H.lastRun = run; }
    const g = $("home-grid");
    if (g) g.classList.toggle("scan", !!st && (st.cards.audit === "run" || st.cards.ingest === "run"));
    const db = $("home-done");
    if (db) db.innerHTML = doneBox(st);
  }
  // Replay the saved results step by step. It reads nothing new and writes nothing.
  const REPLAY_MS = 1300, FRAME_MS = 100;
  function replay() {
    if (replaying() || running()) return;
    const n = stage("ingest") ? num(stage("ingest").calls) || 0 : 0, order = STEPS.map((s) => s.id);
    const st = { cards: {}, doing: {}, prog: {}, summary: null, fail: null, stage: "replay" };
    const DO = { ingest: "Reading in calls", audit: "Grading each call", issues: "Grouping faults into causes", fix: "Writing prompt patches", gate: "Replaying calls with each patch" };
    order.forEach((k) => (st.cards[k] = "wait"));
    H.replay = { st, k: 0, t: 0, ended: false };
    const frame = () => {
      const R = H.replay;
      if (!R || R.st !== st) return;
      const id = order[R.k];
      R.t += FRAME_MS;
      st.cards[id] = "run"; st.doing[id] = DO[id];
      st.prog[id] = id === "ingest" || id === "audit" ? [Math.min(n, Math.round((n * R.t) / REPLAY_MS)), n] : null;
      if (R.t >= REPLAY_MS) { st.cards[id] = "done"; st.prog[id] = null; R.k += 1; R.t = 0; }
      if (R.k >= order.length) R.ended = true;
      drawLive();
      if (!R.ended) R.timer = setTimeout(frame, FRAME_MS);
    };
    frame();
  }

  document.addEventListener("click", async (e) => {
    const t = e.target.closest && e.target.closest("button");
    const op = t && t.dataset && t.dataset.op;
    if (!op || t.disabled || !op.startsWith("home-")) return;
    if (op === "home-run") { H.replay = null; await K.startJob("loop", {}, false); }
    else if (op === "home-go") { H.replay = null; await K.startJob("loop", {}, true); }
    else if (op === "home-cancel") { K.C.confirm = null; V.show(); }
    else if (op === "home-replay") replay();
  });

  V.ITEMS.push({ id: "home", group: "operate", name: "Home", side: "top", fam: "home" });
  V.enter.home = async () => {
    const [c, p] = await Promise.all([K.call("GET", "/api/calls"), K.call("GET", "/api/loop/plan")]);
    if (c.ok && Array.isArray(c.data)) H.calls = c.data;
    if (p.ok) { H.plan = p.data; H.planErr = null; } else H.planErr = str(p.data.error) || `status ${p.status}`;
    if (S.now === "home") V.show();
  };
  // console.js calls this on each log poll. A job that ended reloads the plan and the calls once.
  V.onJob = () => {
    drawLive();
    const j = K.D.job;
    if (j && j.status !== "running" && H.endedFor !== j.id) { H.endedFor = j.id; V.enter.home(); }
  };
})();
