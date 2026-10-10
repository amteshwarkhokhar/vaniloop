// vaniloop console: run stages, submit calls, and the job dock. Builds on window.VL from dash.js. Every API string goes through esc().
(() => {
  "use strict";
  const V = window.VL;
  if (!V) return;
  const { S, P, esc, str, num, chip } = V;
  const $ = (id) => document.getElementById(id);
  const POLL_MS = 1000;
  const C = { con: null, err: null, jobs: [], form: {}, msg: {}, confirm: null, pre: null, chain: [], calls: [], sub: { files: [], ids: [], draft: "", msg: null, sid: null, result: null, list: [], busy: false } };
  const D = { job: null, lines: [], next: 0, open: false, timer: null, err: null };
  const JOB = { running: ["Running", "nonfatal", "◐"], done: ["Done", "pass", "✓"], failed: ["Failed", "fatal", "✕"], cancelled: ["Stopped", "review", "■"] };
  const jchip = (s) => { const [w, t, g] = V.own(JOB, s) || [str(s), "review", "○"]; return V.badge(t, w, g); };
  const NAME = { audit: "Grade calls", judge: "Label with Jev", regrade: "Regrade with Jev labels", issues: "Group into causes", agreement: "Compare the judges", fix: "Write patches", prove: "Test a patch", ship: "Ship a passed patch", submit: "Grade submitted calls", loop: "The whole loop" };
  const SENT = {
    audit: "Grade the calls that have no audit row. A model labels each turn and Python rules give the grade.",
    judge: "Label every turn with Jev, so the rules read Jev's judgement.", regrade: "Re-apply the Python rules to the Jev labels.",
    issues: "Group the faults into ranked causes.", agreement: "Compare the Jev and Gemini labels and report agreement per field.",
    fix: "Write a prompt patch for each patch-routed cause.", prove: "Replay a cause's calls under the old and new prompt and gate the patch.",
    ship: "Makes a new prompt version from a patch that passed. Approve and ship in the tracker does the same.",
  };
  const leaf = (o) => (o && typeof o === "object" && "value" in o ? o.value : null);
  const ro = () => !!(C.con && C.con.read_only);
  const dis = () => (ro() ? " disabled" : "");
  const redraw = () => { const it = V.ITEMS.find((i) => i.id === S.now); if (it && it.group === "operate") V.show(); };

  // ---- the whole loop (#147): plan, confirm, progress, result ----
  const L = { plan: null, planErr: null, noLoop: false, frozen: null, view: "plan", job: null, i: 0, total: 7, name: "", last: "", summary: null, badSummary: false, done: false, failed: null, key: "" };
  const PARTS = { audit: ["Grade new calls", "audit", true], judge: ["Label with Jev", "audit", false], regrade: ["Regrade with Jev labels", "audit", false], issues: ["Group into causes", "issues", false], fix: ["Write patches", "fix", true], test: ["Test patches", "gate", true], report: ["Report", null, false] };
  const ORDER = ["audit", "judge", "regrade", "issues", "fix", "test", "report"];
  const RUN_ALONE = { audit: "audit", judge: "judge", regrade: "regrade", issues: "issues", fix: "fix", test: "prove" };
  const ARG_LABEL = { top: "Causes to fix", n: "Calls per test", k: "Replays per call" };
  const PWORD = { done: ["Done", "pass", "✓"], running: ["Running", "nonfatal", "◐"], wait: ["Waiting", "review", "○"], skip: ["Skips", "review", "–"], failed: ["Failed", "fatal", "✕"], stopped: ["Stopped", "review", "■"], run: ["Will run", "pass", "●"] };
  const partLabel = (n) => (V.own(PARTS, n) || [n])[0];
  const join = (xs) => (xs.length < 2 ? xs[0] || "" : xs.slice(0, -1).join(", ") + " and " + xs[xs.length - 1]);
  const pstep = (n) => (L.plan && (L.plan.steps || []).find((s) => s.name === n)) || {};

  async function call(method, path, body) {
    try {
      const res = await fetch(path, { method, headers: body === undefined ? { Accept: "application/json" } : { Accept: "application/json", "Content-Type": "application/json" }, body: body === undefined ? undefined : JSON.stringify(body) });
      const data = await res.json().catch(() => null);
      return { ok: res.ok, status: res.status, data: data && typeof data === "object" ? data : {} };
    } catch (e) {
      return { ok: false, status: 0, data: { error: "network error" } };
    }
  }
  const errText = (r) => (str(r.data.error) || `request failed (${r.status})`) + (r.data.field ? ` (field: ${str(r.data.field)})` : "");

  // IST helpers. The API gives "HH:MM:SS DD Mon" in IST, with no year.
  const MON = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
  const istMs = (stamp) => {
    const m = /^(\d\d):(\d\d):(\d\d) (\d{1,2}) (\w{3})$/.exec(str(stamp));
    if (!m || MON.indexOf(m[5]) < 0) return null;
    return Date.UTC(new Date(Date.now() + 19800000).getUTCFullYear(), MON.indexOf(m[5]), +m[4], +m[1], +m[2], +m[3]);
  };
  const elapsed = (j) => {
    const a = istMs(j.started_ist), b = j.ended_ist ? istMs(j.ended_ist) : Date.now() + 19800000;
    if (a == null || b == null) return "";
    const s = Math.max(0, Math.round((b - a) / 1000));
    return s >= 60 ? `${Math.floor(s / 60)}m ${String(s % 60).padStart(2, "0")}s` : `${s}s`;
  };
  const usd = (v) => (num(v) == null ? "–" : `$${V.fmt(v, 2)}`);

  // ---- job dock ----
  function dockDraw(actions) {
    const j = D.job;
    $("dock").hidden = !j;
    if (!j) return;
    const last = D.err ? D.err : D.lines.length ? D.lines[D.lines.length - 1] : "waiting for output";
    const nm = V.own(NAME, j.stage) || j.stage;
    const part = j.stage === "loop" && L.job && L.job.id === j.id && L.i ? ` · part ${L.i} of ${L.total}: ${partLabel(L.name)}` : "";
    $("dock-info").innerHTML = `${jchip(j.status)} <b>${esc(nm)}</b>${esc(part)} <span class="mono">${esc(elapsed(j))}</span>${j.status !== "running" && num(j.cost_usd) != null ? ` <span class="mono">${esc(usd(j.cost_usd))}</span>` : ""} <span class="dock-last mono">${esc(last)}</span>`;
    if (actions) {
      $("dock-actions").innerHTML = `${j.status === "running" ? `<button type="button" class="btn sm" data-op="cancel"${dis()}>Cancel</button>` : ""}<button type="button" class="btn sm" data-op="dock-toggle" aria-expanded="${D.open}" aria-controls="dock-log">${D.open ? "Hide log" : "Log"}</button>`;
    }
    $("dock-log").hidden = !D.open;
    if (D.open) $("dock-log").innerHTML = D.lines.slice(-500).map(esc).join("\n");
  }
  function track(job) {
    clearTimeout(D.timer);
    D.job = job; D.lines = []; D.next = 0; D.err = null; D.fails = 0;
    dockDraw(true);
    tick();
  }
  async function tick() {
    const id = D.job && D.job.id;
    if (!id) return;
    const r = await call("GET", `/api/jobs/${encodeURIComponent(id)}/log?from=${D.next}`);
    if (!D.job || D.job.id !== id) return;
    if (!r.ok) {
      D.err = `log unavailable: ${errText(r)}. Retrying.`; dockDraw(true);
      D.fails = (D.fails || 0) + 1;
      D.timer = setTimeout(tick, Math.min(8000, POLL_MS * 2 ** (D.fails - 1)));
      return;
    }
    D.err = null; D.fails = 0;
    const was = D.job.status;
    D.job = r.data.job || D.job;
    const fresh = Array.isArray(r.data.lines) ? r.data.lines.map(str) : [];
    D.lines = D.lines.concat(fresh);
    D.next = num(r.data.next) == null ? D.next : r.data.next;
    if (D.job.stage === "loop") onLog(D.job, fresh);
    dockDraw(was !== D.job.status);
    if (C.sub.sid && C.sub.watch === id) await loadSub(C.sub.sid, false);
    if (D.job.status === "running") D.timer = setTimeout(tick, POLL_MS);
    else await ended();
  }
  async function ended() {
    dockDraw(true);
    await Promise.all([loadJobs(), C.sub.sid ? loadSub(C.sub.sid, false) : null, TR.data ? loadTracker() : null, V.refresh()]);
    if (D.job && D.job.stage === "loop") {
      L.job = D.job;
      L.view = L.failed || D.job.status === "failed" || D.job.status === "cancelled" ? "failed" : "result";
      await loadPlan();
    }
    if (D.job && D.job.status === "done" && C.chain.length) {
      const next = C.chain.shift();
      const r = await startJob(next, {}, false, true);
      if (!r) C.chain = [];
    } else C.chain = [];
    redraw();
    if (D.job && D.job.stage === "loop" && S.now === "home") { const h = L.view === "result" ? $("loop-res-h") : $("loop-h"); if (h) h.focus(); }
  }

  // ---- starting jobs ----
  function argsOf(stage) {
    const def = (C.con.stages.find((s) => s.id === stage) || { args: [] }).args, f = C.form[stage] || {}, out = {};
    for (const a of def) {
      const v = f[a.name];
      if (a.type === "bool") { if (v !== undefined) out[a.name] = !!v; }
      else if (v === undefined || v === "" || v === null) continue;
      else if (a.type === "int") out[a.name] = Number(v);
      else if (a.type === "call_ids") { const ids = str(v).split(/[\s,]+/).filter(Boolean); if (ids.length) out[a.name] = ids; }
      else out[a.name] = str(v);
    }
    return out;
  }
  // Returns the job on success, else null. Errors land in C.msg[stage]; nothing throws.
  async function startJob(stage, args, confirm, quiet) {
    if (C.starting) return null;
    C.starting = true;
    try {
      const body = { stage, args };
      if (confirm) body.confirm = true;
      const r = await call("POST", "/api/jobs", body);
      C.msg[stage] = null;
      if (r.status === 202 && r.data.job) {
        C.confirm = null;
        track(r.data.job);
        if (!quiet) C.chain = C.pre && C.pre.stage === stage ? C.pre.chain.slice() : [];
        C.pre = null;
        if (stage === "loop") { L.frozen = L.plan; resetLoop(r.data.job); }
        redraw();
        if (stage === "loop") { const h = $("loop-h"); if (h) h.focus(); }
        return r.data.job;
      }
      if (r.status === 400 && r.data.error === "confirm") C.confirm = { stage, estimate: num(r.data.estimate_usd) };
      else if (r.status === 409) {
        C.msg[stage] = { err: `Another job is running (${str(r.data.job_id)}). Wait for it or cancel it in the dock.` };
        if (r.data.job_id && !D.job) await adopt(r.data.job_id);
      } else C.msg[stage] = { err: errText(r) };
      redraw();
      if (C.confirm && C.confirm.stage === "loop") { const b = $("loop-go"); if (b) b.focus(); }
      return null;
    } finally { C.starting = false; }
  }
  async function adopt(id) {
    const r = await call("GET", `/api/jobs/${encodeURIComponent(id)}/log?from=0`);
    if (r.ok && r.data.job) track(r.data.job);
  }
  async function loadJobs() {
    const r = await call("GET", "/api/jobs");
    if (r.ok && Array.isArray(r.data.jobs)) C.jobs = r.data.jobs;
  }
  async function loadConsole() {
    const r = await call("GET", "/api/console");
    if (r.ok && Array.isArray(r.data.stages)) {
      C.con = r.data; C.err = null;
      for (const s of C.con.stages) {
        const f = (C.form[s.id] = C.form[s.id] || {});
        for (const a of s.args) if (!(a.name in f) && a.default != null) f[a.name] = a.default;
      }
      if (C.con.busy_job_id && !D.job) await adopt(C.con.busy_job_id);
      await loadTracker(); // the sidebar counts what waits for you
    } else C.err = errText(r);
    $("side-foot").innerHTML = C.con ? `Data folder: <span class="mono">${esc(C.con.data_dir_name)}</span>${ro() ? " " + V.badge("nonfatal", "Read-only", "–") : ""}` : "";
    V.nav();
    redraw();
    return !!C.con;
  }

  // ---- the whole loop: plan, confirm, progress, result ----
  const HOME_LEDE = "Grade VANI's calls, find why calls go wrong, fix the prompt and test the fix. Nothing ships until you approve it.";
  const loopStage = () => (C.con ? C.con.stages.find((s) => s.id === "loop") : null);
  const loopAvail = () => !!(C.con && loopStage() && !L.noLoop);
  const navHome = () => (L.job && L.job.status === "running" && L.i ? `Running, part ${L.i} of 7` : L.plan && num(L.plan.new_calls) ? `${L.plan.new_calls} new calls` : "");
  async function loadPlan() {
    if (C.con && !loopStage()) { L.noLoop = true; L.plan = null; L.planErr = null; return; }
    const t = num((C.form.loop || {}).top);
    const r = await call("GET", `/api/loop/plan?top=${t == null ? 3 : t}`);
    if (r.ok && Array.isArray(r.data.steps)) { L.plan = r.data; L.planErr = null; L.noLoop = false; }
    else { L.plan = null; L.noLoop = r.status === 404; L.planErr = L.noLoop ? null : errText(r); }
  }
  function planSentence() {
    const on = (n) => !!pstep(n).will_run, parts = [];
    if (on("audit")) { const c = pstep("audit").count; parts.push(`grade ${c != null ? `${c} ` : ""}new calls`); }
    if (on("judge") || on("regrade")) parts.push("label and regrade them with Jev");
    if (on("issues")) parts.push("rebuild the causes");
    if (on("fix")) { const c = pstep("fix").count; parts.push(`write patches for ${c != null ? `${c} ` : ""}causes`); }
    if (on("test")) parts.push("test them");
    return parts.length ? `This run will ${join(parts)}.` : "Nothing new to grade or fix. Running now only rebuilds the causes.";
  }
  const planCost = () => (num(L.plan && L.plan.estimate_usd) == null ? `Cost not known yet. ${str(L.plan && L.plan.estimate_basis)}` : `About ${usd(L.plan.estimate_usd)}, based on ${str(L.plan.estimate_basis)}.`);
  const resetLoop = (job) => { L.job = job; L.i = 0; L.total = 7; L.name = ""; L.last = ""; L.summary = null; L.badSummary = false; L.done = false; L.failed = null; L.view = "running"; L.key = ""; };
  const RX = { step: /^\s*LOOP_STEP (\d+)\/(\d+) ([a-z_]+)\s*$/, sum: /^\s*LOOP_SUMMARY (\{.*\})\s*$/, done: /^\s*LOOP_DONE\s*$/, fail: /^\s*LOOP_FAILED (\S+)(?: (.*))?$/ };
  function parseLoop(lines) {
    for (const raw of lines) {
      const s = str(raw); let m;
      if ((m = RX.step.exec(s))) { L.i = +m[1]; L.total = +m[2] || 7; L.name = m[3]; }
      else if ((m = RX.sum.exec(s))) { try { const o = JSON.parse(m[1]); L.summary = o && typeof o === "object" ? o : null; } catch (e) { L.badSummary = true; } }
      else if (RX.done.test(s)) L.done = true;
      else if ((m = RX.fail.exec(s))) L.failed = { step: m[1], reason: m[2] || "" };
      else if (s.trim()) L.last = s;
    }
  }
  function onLog(job, lines) {
    if (!L.job || L.job.id !== job.id) resetLoop(job);
    L.job = job;
    parseLoop(lines);
    loopTick(job);
  }
  const pstate = (k, job) => {
    const name = ORDER[k - 1], sk = !!(L.frozen && (L.frozen.steps || []).some((s) => s.name === name && !s.will_run));
    if (L.failed) { const f = ORDER.indexOf(L.failed.step) + 1 || L.i || 1; return k < f ? (sk ? "skip" : "done") : k === f ? "failed" : "wait"; }
    if (L.done) return sk ? "skip" : "done";
    if (job && job.status === "cancelled") return k < L.i ? (sk ? "skip" : "done") : k === L.i ? "stopped" : "wait";
    if (k < L.i) return sk ? "skip" : "done";
    if (k === L.i) return L.i ? "running" : "wait";
    return sk ? "skip" : "wait";
  };
  const pbadge = (st) => { const [w, t, g] = V.own(PWORD, st) || PWORD.wait; return V.badge(t, w, g); };
  const partsHtml = (job) => ORDER.map((n, k) => `<li class="part">${pbadge(pstate(k + 1, job))} <span class="pnum">${k + 1}</span> ${esc(partLabel(n))}</li>`).join("");
  function stepBadge(id) {
    if (id === "ingest") return V.badge("review", "Used as is", "–");
    const ks = ORDER.map((n, k) => [n, k + 1]).filter(([n]) => (V.own(PARTS, n) || [])[1] === id), sts = ks.map(([, k]) => pstate(k, L.job));
    const ri = sts.indexOf("running");
    if (sts.includes("failed")) return V.badge("fatal", "Failed", "✕");
    if (ri >= 0) return V.badge("nonfatal", `Running, part ${ri + 1} of ${ks.length}`, "◐");
    if (sts.every((s) => s === "done" || s === "skip")) return V.badge("pass", "Done", "✓");
    if (sts.includes("stopped")) return V.badge("review", "Stopped", "■");
    return V.badge("review", "Waiting", "○");
  }
  function loopTick(job) {
    const last = $("loop-last"), time = $("loop-time");
    if (last) last.textContent = L.last;
    if (time) time.textContent = `${elapsed(job)} so far`;
    const key = `${L.i}|${job.status}|${!!L.failed}|${L.done}`;
    if (key === L.key) return;
    L.key = key;
    const live = $("loop-live");
    if (live) live.innerHTML = partsHtml(job);
    if (document.querySelectorAll) document.querySelectorAll("[data-step-st]").forEach((el) => { el.innerHTML = stepBadge(el.getAttribute ? el.getAttribute("data-step-st") : ""); });
    const say = $("loop-say");
    if (say) say.textContent = L.i ? `Part ${L.i} of ${L.total}: ${partLabel(L.name)}` : "Starting.";
    const nv = $("nav-val-home");
    if (nv) nv.textContent = navHome();
  }

  // ---- views ----
  const unavailable = () => `<p class="empty">The console API is not available (${esc(C.err || "loading")}).</p>`;
  const banner = () => (ro() ? `<p class="note ro" role="status"><b>This data folder is read-only.</b> It is the demo freeze, so every control here is off. To run, start the console on runs/: <span class="mono">VANILOOP_DATA=runs uv run python -m vaniloop.web</span></p>` : "");
  const field = (stage, a, cause, lbl) => {
    const v = (C.form[stage] || {})[a.name], id = `f-${stage}-${a.name}`, base = `data-op="arg" data-stage="${esc(stage)}" data-arg="${esc(a.name)}"`;
    const label = `<label class="label" for="${esc(id)}">${esc(lbl || a.name.replace(/_/g, " "))}${a.required ? " *" : ""}</label>`;
    if (a.type === "bool") return `<div class="fld chk"><input type="checkbox" id="${esc(id)}" ${base}${v ? " checked" : ""}${dis()}>${label}</div>`;
    if (Array.isArray(a.choices)) return `<div class="fld">${label}<select class="inp" id="${esc(id)}" ${base}${dis()}>${a.choices.map((c) => `<option value="${esc(c)}"${c === v ? " selected" : ""}>${esc(c)}</option>`).join("")}</select></div>`;
    const list = a.name === "cause" ? ' list="cause-ids"' : "";
    const hint = a.type === "call_ids" ? ' placeholder="ids, separated by spaces or commas"' : "";
    return `<div class="fld">${label}<input class="inp" id="${esc(id)}" ${base} type="${a.type === "int" ? "number" : "text"}" value="${esc(v == null ? "" : v)}"${list}${hint}${dis()}></div>`;
  };
  const card = (s) => {
    const m = C.msg[s.id], cf = C.confirm && C.confirm.stage === s.id, hl = C.pre && C.pre.stage === s.id;
    const est = C.confirm && num(C.confirm.estimate) != null ? `Estimate ${usd(C.confirm.estimate)}.` : "Estimate not known.";
    return `<div class="card op-card rv${hl ? " hl" : ""}" data-card="${esc(s.id)}"><h3>${esc(V.own(NAME, s.id) || s.label || s.id)}</h3><p class="label">${s.paid ? "Costs money" : "Free"} · <span class="mono">${esc(s.id)}</span></p><p class="lede">${esc(SENT[s.id] || "")}</p>` +
      `<div class="flds">${s.args.map((a) => field(s.id, a)).join("") || '<p class="lede">No arguments.</p>'}</div>` +
      (hl && C.pre.chain.length ? `<p class="lede">After it ends, the console runs ${esc(C.pre.chain.map((id) => V.own(NAME, id) || id).join(", then "))}.</p>` : "") +
      (cf ? `<div class="confirm" role="alertdialog" aria-label="Confirm paid stage"><p><b>This calls Gemini and spends money.</b> ${esc(est)}</p><button type="button" class="btn pri" data-op="confirm" data-stage="${esc(s.id)}"${dis()}>Confirm and run</button> <button type="button" class="btn" data-op="unconfirm"${dis()}>Cancel</button></div>`
        : `<button type="button" class="btn pri" data-op="run" data-stage="${esc(s.id)}"${dis()}>Run ${esc(s.label || s.id)}</button>`) +
      (m && m.err ? `<p class="err" role="alert">${esc(m.err)}</p>` : "") + `</div>`;
  };
  const jobsTable = () =>
    `<div class="tw rv"><table><thead><tr><th>Stage</th><th>Status</th><th>Started</th><th>Ended</th><th class="r">Exit</th><th class="r">Cost</th></tr></thead><tbody>${C.jobs.map((j) => `<tr><td class="m">${esc(j.stage)}</td><td>${jchip(j.status)}</td><td class="m">${esc(j.started_ist)}</td><td class="m">${esc(j.ended_ist || "–")}</td><td class="m r">${esc(j.exit_code == null ? "–" : j.exit_code)}</td><td class="m r">${esc(usd(j.cost_usd))}</td></tr>`).join("") || '<tr><td colspan="6" class="src">No jobs yet.</td></tr>'}</tbody></table></div>`;
  const causes = () => { const r = S.stage.issues; return r && r.v && Array.isArray(r.v.causes) ? r.v.causes : []; };
  P.run = () => {
    if (!C.con) return V.head("Run one step", "") + unavailable();
    return V.head("Run one step", "Run one stage by itself, with its own options. One job runs at a time.") + banner() +
      `<datalist id="cause-ids">${causes().map((c) => `<option value="${esc(c.cause_id)}">`).join("")}</datalist>` +
      `<div class="cards">${C.con.stages.filter((s) => s.id !== "submit" && s.id !== "loop").map(card).join("")}</div>` + V.sec("Job history") + jobsTable();
  };

  // ---- home: run the whole loop ----
  const causeName = (id) => { const c = causes().find((x) => x.cause_id === id); return (c && c.name) || id; };
  const runButton = () => `<button type="button" class="btn pri loopbtn" id="loop-run" data-op="run" data-stage="loop"${dis()}>Run the whole loop</button>`;
  const loopArgs = () => ((loopStage() || {}).args || []);
  function planDetails() {
    const parts = (L.plan && L.plan.steps) || [];
    const rows = (parts || []).map((s) => {
      const [label, , paid] = V.own(PARTS, s.name) || [s.name, null, false], cnt = num(s.count);
      return `<li class="prow">${pbadge(s.will_run ? "run" : "skip")}<b>${esc(label)}</b> <span class="src">${paid ? "Costs money" : "Free"}</span>${cnt != null ? ` <span class="src">${esc(cnt)}</span>` : ""}${s.will_run ? "" : `<p class="src">${esc(s.why)}</p>`}</li>`;
    }).join("");
    const all = (L.plan && L.plan.causes) || [], work = all.filter((c) => c.next_attempt != null), skip = all.filter((c) => c.next_attempt == null);
    const max = leaf(S.cfg && S.cfg.fix_loop && S.cfg.fix_loop.max_attempts) || 0;
    const causeRow = (c) => `<li>${esc(causeName(c.cause_id))}${c.next_attempt != null ? ` <span class="src">Try ${esc(c.next_attempt)} of ${esc(max)}</span>` : ""}<p class="src">${esc(c.why)}</p></li>`;
    return `<details class="details"><summary>Run details</summary><p class="grp2">What will run</p><ol class="parts">${rows}</ol>` +
      `<p class="grp2">Options</p><div class="flds">${loopArgs().map((a) => field("loop", a, null, V.own(ARG_LABEL, a.name))).join("")}</div>` +
      (work.length ? `<p class="grp2">Causes it will work on (${work.length})</p><ul class="causes">${work.map(causeRow).join("")}</ul>` : "") +
      (skip.length ? `<details><summary>Causes it will skip, and why (${skip.length})</summary><ul class="causes">${skip.map(causeRow).join("")}</ul></details>` : "") + `</details>`;
  }
  const stamp = (j) => { const m = /^(\d\d):(\d\d):\d\d (\d{1,2}) (\w{3})$/.exec(str(j.ended_ist || j.started_ist)); return m ? `${m[1]}:${m[2]} IST, ${m[3]} ${m[4]}` : ""; };
  const lastLoopJob = () => C.jobs.find((j) => j.stage === "loop" && j.status !== "running") || null;
  function lastRun() {
    const j = lastLoopJob();
    if (!j) return "";
    const why = j.status === "cancelled" ? "you stopped it" : j.status === "failed" ? "stopped" : "finished";
    return `<p class="src runlast">Last run: ${esc(why)} ${esc(stamp(j))}. ${esc(elapsed(j))}. ${esc(usd(j.cost_usd))}. <button type="button" class="btn sm" data-op="loop-last" data-view>Show result</button></p>`;
  }
  function confirmCard() {
    const est = num(C.confirm.estimate);
    return `<div class="card loopcard rv" role="alertdialog" aria-labelledby="loop-cf-h"><h2 id="loop-cf-h">Start the loop?</h2>` +
      `<p class="lede">This run uses Gemini and spends money. ${est == null ? "Cost not known yet." : `About ${esc(usd(est))}.`}</p>` +
      `<p class="lede">It never ships a fix. Fixes that pass wait for your approval.</p>` +
      `<div class="confirm"><button type="button" class="btn pri" id="loop-go" data-op="confirm" data-stage="loop"${dis()}>Run loop</button> <button type="button" class="btn" data-op="unconfirm"${dis()}>Cancel</button></div></div>`;
  }
  function planCard() {
    if (!loopAvail()) return `<div class="card loopcard rv"><h2>Run the whole loop</h2><p class="lede">The one-button loop is not on this server yet. Run each step from Run one step.</p></div>`;
    if (!L.plan) return `<div class="card loopcard rv"><h2>Run the whole loop</h2><p class="lede">${L.planErr ? `Could not load the plan (${esc(L.planErr)}). You can still run the loop. The cost shows before it starts.` : "Loading the plan."}</p>${L.planErr ? runButton() : ""}</div>`;
    if (C.confirm && C.confirm.stage === "loop") return confirmCard();
    const roWhy = ro() ? `<p class="note ro" role="status">This data folder is read-only (the demo freeze). You can see the plan, but you cannot run it.</p>` : "";
    return `<div class="card loopcard rv"><h2>Run the whole loop</h2><p class="lede">${esc(planSentence())}</p><p class="lede">${esc(planCost())}</p>${roWhy}` +
      `<div class="loopact">${runButton()}<span class="src">It never ships a fix. Fixes that pass wait for your approval in the tracker.</span></div>${planDetails()}${lastRun()}</div>`;
  }
  function runningCard() {
    const j = L.job || {};
    return `<div class="card loopcard rv"><h2 id="loop-h" tabindex="-1">Running part ${esc(L.i || 1)} of ${esc(L.total)}: ${esc(partLabel(L.name))}.</h2>` +
      `<p class="lede"><span id="loop-time" class="mono">${esc(elapsed(j))} so far</span></p><ul id="loop-live" class="parts">${partsHtml(j)}</ul>` +
      `<p class="src">Last line: <span id="loop-last" class="mono">${esc(L.last)}</span></p><p id="loop-say" class="sr" role="status" aria-live="polite">${esc(L.i ? `Part ${L.i} of ${L.total}: ${partLabel(L.name)}` : "Starting.")}</p>` +
      `<div class="loopact"><button type="button" class="btn danger" data-op="cancel"${dis()}>Stop the loop</button> <button type="button" class="btn" data-op="dock-toggle" data-view>Show the log</button></div></div>`;
  }
  const RES = { ready_to_ship: "Waiting for your approval", exhausted: "Did not pass", rejected: "Did not pass", gated_inconclusive: "Will try again next run", open: "Could not test", in_fix: "Could not test" };
  const sumCauses = () => { const c = L.summary && L.summary.causes; return Array.isArray(c) ? c : c && typeof c === "object" ? Object.values(c) : []; };
  function resultRows() {
    const fz = L.frozen && Array.isArray(L.frozen.causes) ? L.frozen.causes.filter((c) => c.next_attempt != null) : null;
    return (fz || sumCauses()).map((c) => {
      const t = (TR.data && (TR.data.causes || []).find((x) => x.cause_id === c.cause_id)) || {};
      return { name: causeName(c.cause_id), st: t.state || (fz ? "open" : c.state) || "open", reason: (c.failed && c.failed.reason) || c.error || "" };
    });
  }
  function resultCard() {
    const s = L.summary || {}, cost = num((L.job || {}).cost_usd) != null ? L.job.cost_usd : s.cost_usd, groups = {};
    for (const r of resultRows()) { const g = V.own(RES, r.st) || "Could not test"; (groups[g] = groups[g] || []).push(r); }
    const tickets = (TR.data ? TR.data.causes : []).filter((c) => c.state === "ticket_open").map((c) => ({ name: c.name, id: c.cause_id }));
    const grp = (name, list, act) => (list && list.length ? `<div class="rgroup"><h3>${name === "Waiting for your approval" ? `<a href="#/tracker">${esc(name)} (${list.length})</a>` : `${esc(name)} (${list.length})`}</h3><ul class="causes">${list.map((r) => `<li>${esc(r.name)}${act && act(r) ? ` <a href="#/tracker">${esc(act(r))}</a>` : r.reason ? ` <span class="src">${esc(r.reason)}</span>` : ""}</li>`).join("")}</ul></div>` : "");
    return `<div class="card loopcard rv"><h2 id="loop-res-h" tabindex="-1">The loop finished</h2>` +
      `<p class="lede">Finished ${esc(stamp(L.job || {}))}. It took ${esc(elapsed(L.job || {}))} and cost ${esc(usd(cost))}. Nothing went live.</p>` +
      (L.badSummary || !L.summary ? `<p class="src">The summary line is missing, so the log has the details.</p>` : "") +
      grp("Waiting for your approval", groups["Waiting for your approval"], () => "Review and ship") + grp("Tickets to send", tickets, () => "Send the ticket") + grp("Did not pass", groups["Did not pass"]) +
      grp("Will try again next run", groups["Will try again next run"]) + grp("Could not test", groups["Could not test"]) +
      `<div class="loopact"><a class="btn pri" href="#/tracker">Open the tracker</a> <button type="button" class="btn" data-op="loop-plan" data-view>Plan the next run</button></div></div>`;
  }
  function failedCard() {
    const j = L.job || {};
    if (!L.failed) return `<div class="card loopcard rv failed"><h2 id="loop-h" tabindex="-1">The loop stopped</h2><p class="lede">The loop stopped with exit code ${esc(j.exit_code == null ? "?" : j.exit_code)} before it said why. Show the log.</p><div class="loopact"><button type="button" class="btn" data-op="dock-toggle" data-view>Show the log</button></div></div>`;
    const idx = ORDER.indexOf(L.failed.step) + 1 || L.i || 1, name = partLabel(L.failed.step), alone = V.own(RUN_ALONE, L.failed.step);
    if (str(L.failed.reason).trim() === "cancelled") return `<div class="card loopcard rv failed"><h2 id="loop-h" tabindex="-1">The loop stopped</h2><p class="lede">You stopped the loop during part ${esc(idx)} of ${esc(L.total)}. Finished parts keep their results.</p><div class="loopact"><button type="button" class="btn" data-op="loop-plan" data-view>Plan the next run</button> <button type="button" class="btn" data-op="dock-toggle" data-view>Show the log</button></div></div>`;
    return `<div class="card loopcard rv failed"><h2 id="loop-h" tabindex="-1">The loop stopped at part ${esc(idx)} of ${esc(L.total)}: ${esc(name)}.</h2><p class="lede">Reason: ${esc(L.failed.reason)}.</p><p class="lede">Part ${esc(Math.max(0, idx - 1))} finished, and its results are saved. Nothing went live. Running again skips work that is already done.</p><div class="loopact"><button type="button" class="btn pri" data-op="run" data-stage="loop"${dis()}>Run the loop again</button>${alone ? ` <button type="button" class="btn" data-op="prefill" data-stage="${esc(alone)}"${dis()}>Run this part alone</button>` : ""} <button type="button" class="btn" data-op="dock-toggle" data-view>Show the log</button></div></div>`;
  }
  const homeCard = () => (L.view === "running" && L.job ? runningCard() : L.view === "result" ? resultCard() : L.view === "failed" ? failedCard() : planCard());
  const gradeCard = () => `<div class="card rv gradep"><h2>Grade some calls</h2><p class="lede">Add audio, transcripts or call ids and get a grade for each call.</p><button type="button" class="btn" data-go="submit" data-view>Grade some calls</button></div>`;
  function waitRow(c, kind) {
    const act = kind === "ready_to_ship" ? "Review and ship" : kind === "ticket_open" ? "Send the ticket" : "";
    return `<li>${esc(c.name)} ${schip(c.state)}${act ? ` <a href="#/tracker">${act}</a>` : ` <span class="src">See the tracker.</span>`}</li>`;
  }
  function waitingBox() {
    const cs = TR.data ? TR.data.causes : [], rs = cs.filter((c) => c.state === "ready_to_ship"), tk = cs.filter((c) => c.state === "ticket_open"), rg = cs.filter((c) => c.state === "regressed");
    return V.sec("Waiting for you") + (rs.length || tk.length || rg.length ? `<ul class="wait">${rs.map((c) => waitRow(c, "ready_to_ship")).join("")}${tk.map((c) => waitRow(c, "ticket_open")).join("")}${rg.map((c) => waitRow(c, "regressed")).join("")}</ul>` : `<p class="note">Nothing waits for you.</p>`);
  }
  function stepsList() {
    const ids = ["ingest", "audit", "issues", "fix", "gate"], names = { ingest: "Ingest", audit: "Audit", issues: "Issues", fix: "Fix", gate: "Test" }, href = { ingest: "#/ingest", audit: "#/audit", issues: "#/issues", fix: "#/fix", gate: "#/gate" };
    const meaning = { ingest: "Read call transcripts and audio in as call records.", audit: "Grade each call. A model labels each turn, and rules grade it.", issues: "Faults grouped by their cause, worst first.", fix: "A prompt patch per cause, with the old text, new text and reason.", gate: "Each patch is replayed on the same calls as the old prompt." };
    const heads = V.heads ? V.heads() : {};
    return V.sec("The five steps") + `<div class="steprows">` + ids.map((id, i) => `<a class="steprow" href="${href[id]}"><span class="snum">${i + 1}</span><span class="stitle">${esc(names[id])}</span><span class="sval">${esc(heads[id] || "no data yet")}</span><p class="smean">${esc(meaning[id])}</p><span class="sbadge" data-step-st="${id}">${stepBadge(id)}</span></a>`).join("") + `</div>`;
  }
  P.home = () => (C.con ? V.head("The fix loop", HOME_LEDE) + banner() + `<div class="home">${homeCard()}${gradeCard()}${waitingBox()}${stepsList()}</div>` : V.head("The fix loop", HOME_LEDE) + unavailable());

  // ---- submit ----
  const NAME_OK = /^[A-Za-z0-9._-]{1,120}\.(mp3|json)$/;
  function addFiles(list) {
    const sub = C.sub;
    for (const f of Array.from(list || [])) {
      if (!NAME_OK.test(str(f.name))) { sub.msg = { err: `${str(f.name)}: use letters, digits, dot, dash or underscore, and end in .mp3 or .json.` }; continue; }
      if (num(f.size) != null && f.size > 25 * 1024 * 1024) { sub.msg = { err: `${str(f.name)} is over 25 MB.` }; continue; }
      if (sub.files.length >= 20) { sub.msg = { err: "At most 20 files." }; break; }
      if (!sub.files.some((x) => x.name === f.name)) sub.files.push(f);
    }
  }
  function addIds(text) {
    for (const id of str(text).split(/[\s,]+/).filter(Boolean)) if (!C.sub.ids.includes(id)) C.sub.ids.push(id);
    C.sub.draft = "";
  }
  async function b64(file) {
    const bytes = new Uint8Array(await file.arrayBuffer());
    let bin = "";
    for (let i = 0; i < bytes.length; i += 0x8000) bin += String.fromCharCode.apply(null, bytes.subarray(i, i + 0x8000));
    return btoa(bin);
  }
  async function send() {
    const sub = C.sub;
    if (sub.busy) return;
    if (sub.draft) addIds(sub.draft);
    if (!sub.files.length && !sub.ids.length) { sub.msg = { err: "Add a file or a call id first." }; return redraw(); }
    sub.busy = true; sub.msg = { ok: "Sending." }; redraw();
    const files = await Promise.all(sub.files.map(async (f) => ({ name: f.name, data_b64: await b64(f) })));
    const r = await call("POST", "/api/submit", { files, call_ids: sub.ids });
    if (r.status !== 201 || !r.data.submission_id) { sub.busy = false; sub.msg = { err: errText(r) }; return redraw(); }
    const sid = r.data.submission_id;
    const j = await call("POST", "/api/jobs", { stage: "submit", args: { submission_id: sid }, confirm: true });
    sub.busy = false; sub.sid = sid; sub.result = { calls: [] }; sub.files = []; sub.ids = [];
    if (j.status === 202 && j.data.job) { sub.watch = j.data.job.id; sub.msg = { ok: `Submission ${sid} started.` }; track(j.data.job); }
    else sub.msg = { err: j.status === 409 ? `Uploaded as ${sid}, but another job is running (${str(j.data.job_id)}). Wait for it, then send again from the Run view.` : errText(j) };
    await loadSub(sid, false);
    redraw();
  }
  async function loadSub(sid, draw = true) {
    const r = await call("GET", `/api/submit/${encodeURIComponent(sid)}`);
    if (r.ok && C.sub.sid === sid) C.sub.result = r.data;
    else if (!r.ok) C.sub.msg = { err: errText(r) };
    if (draw) redraw();
  }
  async function loadSubs() {
    const r = await call("GET", "/api/submit");
    if (r.ok && Array.isArray(r.data.submissions)) C.sub.list = r.data.submissions;
  }
  const findings = (c) => (Array.isArray(c.findings) ? c.findings : []).filter((f) => f && (f.status === "Fatal" || f.status === "NonFatal"));
  const resultRow = (c) =>
    `<li class="res"><div class="who"><a href="/#/call/${esc(encodeURIComponent(str(c.call_id)))}">${esc(c.call_id)}</a><br><span class="src">${esc(c.source)}</span></div><div>` +
    `<p>${c.error ? `<span class="chip t-fatal">Error</span> ${esc(c.error)}` : c.verdict ? `${chip(c.verdict)} <span class="pill">${esc(str(c.scope || "unclear").replace(/_/g, " "))}</span>` : '<span class="src">waiting for the verdict</span>'}</p>` +
    findings(c).map((f) => `<p class="finding">${chip(f.status)} <b>${esc(V.own(V.NAMES, f.parameter) || f.parameter)}</b>${f.onset_turn != null ? ` · turn ${esc(f.onset_turn)}` : ""}<br><span class="src">${esc(f.reason)}</span></p>`).join("") +
    (Array.isArray(c.flags) && c.flags.length ? `<p class="chips">${c.flags.map((x) => `<span class="pill">${esc(str(x).replace(/_/g, " "))}</span>`).join("")}</p>` : "") + `</div></li>`;
  P.submit = () => {
    if (!C.con) return V.head("Submit calls", "") + unavailable();
    const sub = C.sub, m = sub.msg;
    const chips = (list, kind) => list.map((x, i) => `<span class="pill">${esc(kind === "f" ? x.name : x)}<button type="button" class="x" data-op="rm-${kind}" data-i="${i}" aria-label="Remove ${esc(kind === "f" ? x.name : x)}"${dis()}>×</button></span>`).join("");
    return V.head("Submit calls", "Add audio, transcripts or the ids of calls already read in, and get a grade for each call. Grading uses Gemini and costs money.") + banner() +
      `<div class="card rv"><div class="drop" data-drop="1"><label class="label" for="sub-files">Drop or pick mp3 and transcript json files</label><input id="sub-files" class="inp" type="file" multiple accept=".mp3,.json" data-op="files"${dis()}></div>` +
      `<div class="fld"><label class="label" for="sub-id">Call ids</label><div class="addrow"><input class="inp" id="sub-id" list="sub-ids" autocomplete="off" data-op="id-draft" value="${esc(sub.draft)}" placeholder="type or paste ids"${dis()}><button type="button" class="btn" data-op="add-id"${dis()}>Add</button></div>` +
      `<datalist id="sub-ids">${C.calls.slice(0, 500).map((id) => `<option value="${esc(id)}">`).join("")}</datalist></div>` +
      `<div class="chips">${chips(sub.files, "f")}${chips(sub.ids, "i")}</div>` +
      `<button type="button" class="btn pri" data-op="send"${sub.busy ? " disabled" : dis()}>Grade these calls</button>` +
      (m ? `<p class="${m.err ? "err" : "ok"}" role="${m.err ? "alert" : "status"}">${esc(m.err || m.ok)}</p>` : "") + `</div>` +
      (sub.result ? V.sec("Grades for this batch") + `<ul class="items res-list">${(sub.result.calls || []).map(resultRow).join("") || '<li class="empty">No results yet.</li>'}</ul>` : "") +
      V.sec("Earlier batches") + `<div class="tw"><table><tbody>${sub.list.map((s) => `<tr><td class="m">${esc(s.submission_id)}</td><td class="m">${esc(s.created_ist)}</td><td class="m r">${esc(s.calls)} calls</td><td class="r"><button type="button" class="btn sm" data-op="open-sub" data-sid="${esc(s.submission_id)}"${dis()}>Open</button></td></tr>`).join("") || '<tr><td class="src">None yet.</td></tr>'}</tbody></table></div>`;
  };

  // ---- rubric ----
  const RB = { data: null, err: null, edits: {}, qedits: {}, errs: {}, msg: null, saved: false };
  const pval = (p) => (p.id in RB.edits ? RB.edits[p.id] : p.value);
  const pdirty = (p) => p.id in RB.edits && Number(RB.edits[p.id]) !== Number(p.value);
  // The API nests a question's current texts under value, with the defaults under default.
  const qv = (q) => (q && (q.value || q)) || {};
  const qnorm = (q) => { const v = qv(q); return { instructions: v.instructions, true: { description: v.true.description, examples: v.true.examples || [] }, false: { description: v.false.description, examples: v.false.examples || [] } }; };
  const qok = (q) => !!(qv(q).true && qv(q).false);
  const qdirty = (q) => qok(q) && q.id in RB.qedits && JSON.stringify(RB.qedits[q.id]) !== JSON.stringify(qnorm(q));
  const anyDirty = () => !!RB.data && (RB.data.params.some(pdirty) || (RB.data.questions || []).some(qdirty));
  const errFor = (id) => Object.keys(RB.errs).filter((f) => f === id || f.startsWith(`${id}.`) || f.startsWith(`questions.${id}`)).map((f) => `<p class="err" role="alert">${esc(RB.errs[f])}</p>`).join("");
  async function loadRubric() {
    const r = await call("GET", "/api/rubric");
    if (r.ok && Array.isArray(r.data.params)) { RB.data = r.data; RB.err = null; } else RB.err = errText(r);
  }
  const paramField = (p, ctx) => {
    const v = pval(p), changed = Number(v) !== Number(p.default), id = `p-${ctx}-${p.id}`;
    return `<div class="fld prm"><label class="label" for="${esc(id)}">${esc(p.label)}</label><div class="addrow"><input class="inp" id="${esc(id)}" type="number" data-op="param" data-id="${esc(p.id)}" min="${esc(p.min)}" max="${esc(p.max)}" step="${esc(p.step)}" value="${esc(v)}"${dis()}>` +
      `<span class="chg" data-chg="${esc(p.id)}"${changed ? "" : " hidden"}>changed</span><button type="button" class="btn sm" data-op="reset-param" data-id="${esc(p.id)}" data-ctx="${esc(ctx)}"${changed || pdirty(p) ? dis() : " disabled"}>Reset</button></div>` +
      `<p class="src">${esc(p.help)} Default ${esc(p.default)}.</p>${errFor(p.id)}</div>`;
  };
  const savedBanner = () => (RB.saved ? `<p class="note rv" role="status"><b>Saved.</b> The change is not live until you re-judge and regrade. <button type="button" class="btn sm" data-op="rejudge"${dis()}>Re-judge and regrade</button></p>` : "") + (RB.msg ? `<p class="err" role="alert">${esc(RB.msg)}</p>` : "");
  const qCard = (q) => {
    if (!qok(q)) return "";
    const e = RB.qedits[q.id] || qnorm(q), on = (f) => `data-op="q" data-q="${esc(q.id)}" data-f="${f}"`;
    const area = (f, label, v) => `<div class="fld"><label class="label" for="q-${esc(q.id)}-${f}">${label}</label><textarea class="inp" id="q-${esc(q.id)}-${f}" rows="${f === "instructions" ? 5 : 3}" ${on(f)}${dis()}>${esc(v)}</textarea></div>`;
    const ex = (x) => (x.examples || []).join("\n");
    const changed = q.overridden || qdirty(q);
    return `<div class="card op-card rv"><h3>${esc(q.label || q.id)}</h3><p class="label"><span class="mono">${esc(q.id)}</span> <span class="chg"${changed ? "" : " hidden"}>changed</span></p><div class="flds">` +
      area("instructions", "Instructions", e.instructions) + area("true.description", "True means", e.true.description) + area("true.examples", "True examples, one per line", ex(e.true)) +
      area("false.description", "False means", e.false.description) + area("false.examples", "False examples, one per line", ex(e.false)) + `</div>${errFor(q.id)}` +
      `<button type="button" class="btn sm" data-op="reset-q" data-q="${esc(q.id)}"${q.overridden || qdirty(q) ? dis() : " disabled"}>Reset</button></div>`;
  };
  P.rubric = () => {
    if (!C.con) return V.head("Rubric", "") + unavailable();
    const d = RB.data;
    if (!d) return V.head("Rubric", "") + (RB.err ? `<p class="empty">The rubric could not load (${esc(RB.err)}).</p>` : '<p class="empty">Loading.</p>');
    const byId = Object.fromEntries(d.params.map((p) => [p.id, p])), used = new Set();
    const check = (c) => {
      const ps = (c.params || []).map((id) => byId[id]).filter(Boolean);
      ps.forEach((p) => used.add(p.id));
      return `<div class="card op-card rv"><h3>${esc(c.name || c.id)}</h3><p class="label"><span class="mono">${esc(c.id)}</span> · ${esc(c.judged_by)}${c.prompt_ref ? ` · ${esc(c.prompt_ref)}` : ""}</p>` +
        `<p class="lede">${esc(c.what)}</p><p class="why"><b>Fatal when</b> ${esc(c.fatal_when)}</p><p class="why"><b>Non-fatal when</b> ${esc(c.nonfatal_when)}</p><div class="flds">${ps.map((p) => paramField(p, c.id)).join("")}</div></div>`;
    };
    const cards = d.checks.map(check).join(""), rest = d.params.filter((p) => !used.has(p.id));
    return V.head("Rubric", "The checks each call is graded on, and the numbers each check uses. A change counts only after Jev labels again and the calls are regraded.") + banner() + savedBanner() +
      `<div class="savebar"><button type="button" class="btn pri" id="rb-save" data-op="save-rubric"${anyDirty() ? dis() : " disabled"}>Save rubric</button> <span class="src">Save turns on after you change a number or a question.</span></div>` +
      V.sec("Checks") + `<div class="cards">${cards}${rest.length ? `<div class="card op-card rv"><h3>Other numbers</h3><div class="flds">${rest.map((p) => paramField(p, "other")).join("")}</div></div>` : ""}</div>` +
      V.sec("Jev questions") + `<div class="cards">${(d.questions || []).map(qCard).join("")}</div>`;
  };
  const putRubric = async (body) => {
    const r = await call("PUT", "/api/rubric", body);
    if (r.ok && Array.isArray(r.data.params)) { RB.data = r.data; RB.edits = {}; RB.qedits = {}; RB.errs = {}; RB.msg = null; RB.saved = true; }
    else {
      const f = str(r.data.field), own = RB.data.params.some((p) => p.id === f) || (RB.data.questions || []).some((q) => f === `questions.${q.id}` || f.startsWith(`questions.${q.id}.`));
      RB.saved = false; RB.errs = own ? { [f]: str(r.data.error) } : {}; RB.msg = own ? null : errText(r); // a field with no input of its own shows in the banner
    }
    return r;
  };
  async function saveRubric() {
    const params = {}, questions = {};
    for (const p of RB.data.params) if (pdirty(p)) {
      const n = Number(RB.edits[p.id]);
      if (!isFinite(n) || RB.edits[p.id] === "") { RB.errs = { [p.id]: "Enter a number." }; return redraw(); }
      params[p.id] = n === p.default ? (p.overridden ? null : p.value) : n;
    }
    for (const q of RB.data.questions || []) if (qdirty(q)) questions[q.id] = q.default && JSON.stringify(RB.qedits[q.id]) === JSON.stringify(qnorm({ id: q.id, ...q.default })) ? null : RB.qedits[q.id];
    await putRubric({ params, questions });
    redraw();
  }
  const qedit = (qid, f, text) => {
    const q = RB.data.questions.find((x) => x.id === qid);
    if (!qok(q)) return;
    const e = (RB.qedits[qid] = RB.qedits[qid] || qnorm(q)), [side, part] = f.split(".");
    if (f === "instructions") e.instructions = text;
    else e[side][part] = part === "examples" ? text.split("\n").map((l) => l.trim()).filter(Boolean) : text;
  };

  // ---- tune ----
  const SL = [["question_fits.true_at", "Question fits: true at", "jev.Q_TRUE_AT"], ["question_fits.false_at", "Question fits: false at", "jev.Q_FALSE_AT"], ["option_fits.true_at", "Each option fits: true at", "jev.OPT_TRUE_AT"],
    ["option_fits.false_at", "Each option fits: false at", "jev.OPT_FALSE_AT"], ["product_match.true_at", "Same product: true at", "jev.PM_TRUE_AT"], ["product_match.false_at", "Same product: false at", "jev.PM_FALSE_AT"],
    ["name_check_drop_at", "Name check drops at", "jev.N_DROP_AT"], ["frustrated_p", "Frustrated at", "rules.FRUSTRATED_P"]];
  const LBL = { question_fits: "Question fits", with_options_fits: "Each option fits", option_fits: "Each option fits", product_match: "Same product", irritated: "Irritated" };
  const T = { vals: {}, res: null, err: null, timer: null, seq: 0 };
  const sp = (id) => (RB.data ? RB.data.params.find((p) => p.id === id) : null);
  const tval = (k, id) => (k in T.vals ? T.vals[k] : sp(id) ? sp(id).value : 0);
  const tbody = () => {
    const out = {};
    for (const [k, , id] of SL) { const [a, b] = k.split("."); if (b) (out[a] = out[a] || {})[b] = Number(tval(k, id)); else out[a] = Number(tval(k, id)); }
    return out;
  };
  async function runTune() {
    const n = ++T.seq, r = await call("POST", "/api/tune", { thresholds: tbody() });
    if (n !== T.seq) return;
    if (r.ok) { T.res = r.data; T.err = null; } else T.err = { text: errText(r), noraw: r.status === 409 };
    const el = $("tune-res");
    if (el) el.innerHTML = tuneRes(); else redraw();
  }
  const delta = (a, b, up) => (num(a) == null || num(b) == null || a === b ? "" : ` <span class="${(a > b) === up ? "ok" : "no"}">${a > b ? "+" : ""}${a - b}</span>`);
  const tuneRes = () => {
    if (T.err) return `<p class="err" role="alert">${esc(T.err.text)}</p>${T.err.noraw ? `<button type="button" class="btn" data-op="prefill" data-stage="judge" data-chain=""${dis()}>Run judge</button>` : ""}`;
    const r = T.res;
    if (!r) return '<p class="empty">Scoring.</p>';
    const cur = (x) => (r.current || []).find((c) => c.label === x.label && c.split === x.split) || {};
    return `<div class="tw"><table><thead><tr><th>Label</th><th>Split</th><th class="r">Calls</th><th class="r">Right</th><th class="r">Wrong</th><th class="r">Unsure</th><th class="r">Gemini right</th></tr></thead><tbody>${(r.labels || []).map((x) => {
      const c = cur(x);
      return `<tr><td>${esc(V.own(LBL, x.label) || x.label)}</td><td class="m">${esc(x.split === "test" ? "held-out test" : "dev")}</td><td class="m r">${esc(x.n)}</td><td class="m r">${esc(x.right)}${delta(x.right, c.right, true)}</td><td class="m r">${esc(x.wrong)}${delta(x.wrong, c.wrong, false)}</td><td class="m r">${esc(x.unsure)}</td><td class="m r">${esc(x.gemini_right)}</td></tr>`;
    }).join("")}</tbody></table></div><p class="lede">Changes against the current values show in colour. Labels that would flip over all ${esc(r.calls)} calls:</p><div class="chips">${Object.entries(r.flips || {}).map(([k, n]) => `<span class="pill">${esc(V.own(LBL, k) || k)}<b>${esc(n)}</b></span>`).join("")}</div>`;
  };
  P.tune = () => {
    if (!C.con) return V.head("Tune", "") + unavailable();
    if (!RB.data) return V.head("Tune", "") + (RB.err ? `<p class="empty">The rubric could not load (${esc(RB.err)}).</p>` : '<p class="empty">Loading.</p>');
    const slider = ([k, label, id]) => {
      const p = sp(id) || { min: 0, max: 1, step: 0.01 }, v = tval(k, id);
      return `<div class="fld"><label class="label" for="s-${esc(k)}">${esc(label)} <output id="o-${esc(k)}" class="mono">${esc(v)}</output></label><input id="s-${esc(k)}" type="range" class="rng" data-op="tune" data-key="${esc(k)}" min="${esc(p.min)}" max="${esc(p.max)}" step="${esc(p.step)}" value="${esc(v)}"${dis()}></div>`;
    };
    return V.head("Tune", "Move a slider to see how the reviewed calls would be graded. Nothing is saved until you press Save to rubric.") + banner() + savedBanner() +
      `<div class="card rv"><div class="sliders">${SL.map(slider).join("")}</div><button type="button" class="btn pri" data-op="tune-save"${dis()}>Save to rubric</button></div>` + V.sec("Against the reviewed truth") + `<div id="tune-res">${tuneRes()}</div>`;
  };
  async function saveTune() {
    const params = {};
    for (const [k, , id] of SL) { const p = sp(id); if (p && Number(tval(k, id)) !== Number(p.value)) params[id] = Number(tval(k, id)); }
    const r = await putRubric({ params, questions: {} });
    if (r.ok) T.vals = {}; else T.err = { text: errText(r) };
    redraw();
  }

  // ---- issues tracker ----
  const TR = { data: null, err: null, ref: {}, note: {}, ship: null, msg: {} };
  const GROUPS = [["Open", ["open"]], ["In fix", ["in_fix", "gated_inconclusive"]], ["Ready to ship", ["ready_to_ship"]], ["Shipped", ["shipped"]], ["Tickets", ["ticket_open", "ticket_sent", "ticket_resolved"]], ["Watching", ["watching"]], ["Done or failed", ["closed", "rejected", "exhausted", "regressed"]]];
  const STONE = { regressed: "fatal", rejected: "review", exhausted: "review", closed: "pass", shipped: "pass", ready_to_ship: "nonfatal", in_fix: "nonfatal", gated_inconclusive: "nonfatal" };
  const schip = (st) => V.badge(V.own(STONE, st) || "review", str(st).replace(/_/g, " "));
  async function loadTracker() {
    const r = await call("GET", "/api/tracker");
    if (r.ok && Array.isArray(r.data.causes)) { TR.data = r.data; TR.err = null; } else TR.err = errText(r);
  }
  // An ISO time stamped in IST (+05:30) reads as "14:42 10 Oct"; anything else passes through unchanged.
  const when = (v) => {
    const m = /^\d{4}-(\d{2})-(\d{2})T(\d{2}):(\d{2})(?::\d{2}(?:\.\d+)?)?\+05:30$/.exec(str(v));
    return m ? `${m[3]}:${m[4]} ${+m[2]} ${MON[+m[1] - 1]}` : str(v);
  };
  const scalars = (d) => Object.entries(d && typeof d === "object" ? d : {}).filter(([, v]) => v == null || typeof v !== "object").map(([k, v]) => `<span>${esc(k.replace(/_/g, " "))} <b>${esc(v)}</b></span>`).join("");
  const gated = (c) => [...(c.events || [])].reverse().find((e) => e && e.kind === "gated");
  const tact = (c) => {
    const id = esc(c.cause_id), m = TR.msg[c.cause_id], err = m ? `<p class="err" role="alert">${esc(m)}</p>` : "";
    if (c.state === "ticket_open") return `<div class="addrow"><input class="inp" aria-label="Ticket id or link" placeholder="Ticket id or link" data-op="tref" data-id="${id}" value="${esc(TR.ref[c.cause_id] || "")}"${dis()}><button type="button" class="btn sm" data-op="t-sent" data-id="${id}"${dis()}>Mark ticket sent</button></div>${err}`;
    if (c.state === "ticket_sent") return `<div class="addrow"><input class="inp" aria-label="Resolution note" placeholder="Note" data-op="tnote" data-id="${id}" value="${esc(TR.note[c.cause_id] || "")}"${dis()}><button type="button" class="btn sm" data-op="t-done" data-id="${id}"${dis()}>Mark resolved</button></div>${err}`;
    if (c.state !== "ready_to_ship") return err;
    if (TR.ship !== c.cause_id) return `<button type="button" class="btn sm" data-op="ship" data-id="${id}"${dis()}>Approve and ship</button>${err}`;
    const g = gated(c);
    return `<div class="confirm" role="alertdialog" aria-label="Confirm ship"><p><b>Ship this patch into a new prompt version?</b></p><p class="gate-f">${g ? scalars(g.detail) : "<span>no gate record</span>"}</p><button type="button" class="btn pri sm" data-op="ship-go" data-id="${id}"${dis()}>Confirm and ship</button> <button type="button" class="btn sm" data-op="ship-no"${dis()}>Cancel</button></div>${err}`;
  };
  const tcard = (c) => {
    const ev = Array.isArray(c.events) ? c.events : [], k = c.counts || {};
    return `<article class="card tcard rv"><h3>${esc(c.name)}</h3><p class="meta">${schip(c.state)}<span class="pill">${esc(c.type)}</span><span class="pill">owner ${esc(c.owner || "unassigned")}</span></p>` +
      `<p class="pc"><b>${esc(k.fatal)}</b> fatal · <b>${esc(k.nonfatal)}</b> non-fatal · <b>${esc(k.symptoms)}</b> symptoms</p><p class="src">${esc(c.cause_id)} · ${esc(c.route)} · since ${esc(when(c.state_since_ist))}</p>${tact(c)}` +
      `<details><summary>Timeline (${esc(ev.length)})</summary><ol class="tl">${ev.map((e) => `<li><span class="mono">${esc(when(e.ts_ist))}</span> <b>${esc(str(e.kind).replace(/_/g, " "))}</b>${e.run_id ? ` <span class="src">${esc(e.run_id)}</span>` : ""}<br><span class="gate-f">${scalars(e.detail)}</span></li>`).join("") || '<li class="src">No events.</li>'}</ol></details></article>`;
  };
  P.tracker = () => {
    const d = TR.data;
    if (!C.con) return V.head("Tracker", "") + unavailable();
    if (!d) return V.head("Tracker", "") + (TR.err ? `<p class="empty">The tracker could not load (${esc(TR.err)}).</p>` : '<p class="empty">Loading.</p>');
    const cols = GROUPS.map(([name, states]) => {
      const cs = d.causes.filter((c) => states.includes(c.state));
      return `<section class="col" aria-label="${esc(name)}"><h2 class="sec">${esc(name)} <span class="src">${esc(cs.length)}</span></h2>${cs.map(tcard).join("") || '<p class="src">None.</p>'}</section>`;
    }).join("");
    return V.head("Tracker", "Every cause, where it stands, and what waits for you. Fixes that passed wait here for your approval.") + banner() + `<div class="board">${cols}</div>` + V.sec("Prompt versions") +
      `<ul class="items rv">${(d.prompt_versions || []).map((v) => `<li class="res"><div class="who"><b>${esc(v.version)}</b>${v.version === d.current_prompt_version ? ' <span class="chip t-pass">current</span>' : ""}<br>${esc(v.created_ist)}${v.base ? `<br>from ${esc(v.base)}` : ""}</div><div>${(v.causes || []).map((x) => `<span class="pill">${esc(x)}</span>`).join(" ") || '<span class="src">no causes</span>'}</div></li>`).join("") || '<li class="empty">None yet.</li>'}</ul>`;
  };
  const tpost = async (id, path, body) => {
    const r = await call("POST", `/api/tracker/${encodeURIComponent(id)}/${path}`, body);
    TR.msg[id] = r.ok ? null : errText(r);
    if (r.ok) await loadTracker();
    redraw();
  };

  V.ITEMS.push({ id: "home", group: "operate", name: "The loop", side: "loop" }, { id: "run", group: "operate", name: "Run one step", side: "tools" }, { id: "submit", group: "operate", name: "Submit calls", side: "tab", step: 1, tab: "Submit calls" }, { id: "rubric", group: "operate", name: "Rubric", side: "tab", step: 2, tab: "Rubric" },
    { id: "tune", group: "operate", name: "Tune", side: "tab", step: 2, tab: "Tune" }, { id: "tracker", group: "operate", name: "Tracker", side: "needs" });
  V.vals = () => { const n = TR.data && TR.data.causes.filter((c) => c.state === "ready_to_ship" || c.state === "ticket_open").length; return { tracker: n == null ? "" : n ? `${n} waiting` : "None waiting", home: navHome() }; };
  const enter = async () => { if (!C.con) await loadConsole(); await loadJobs(); redraw(); };
  V.enter = {
    home: async () => { await enter(); await loadTracker(); await loadPlan(); redraw(); },
    run: async () => { await enter(); },
    submit: async () => {
      await enter(); await loadSubs();
      const x = await call("GET", "/api/calls");
      if (x.ok && Array.isArray(x.data)) C.calls = x.data.map((r) => str(r && r.call_id)).filter(Boolean);
      redraw();
    },
    rubric: async () => { await enter(); RB.saved = false; await loadRubric(); redraw(); },
    tracker: async () => { await enter(); await loadTracker(); redraw(); },
    tune: async () => { await enter(); await loadRubric(); redraw(); runTune(); },
  };
  const BARS = { audit: ["audit", ["judge", "regrade"]], issues: ["issues", []], fix: ["fix", []], gate: ["prove", []] };
  V.stageBar = (id) => {
    const b = V.own(BARS, id);
    if (!b || !C.con) return "";
    const st = C.con.stages.find((s) => s.id === b[0]);
    return st ? `<div class="stagebar"><button type="button" class="btn" data-op="prefill" data-stage="${esc(b[0])}" data-chain="${esc(b[1].join(","))}"${dis()}>Run ${esc((st.label || st.id).toLowerCase())}${b[1].length ? `, ${esc(b[1].join(" and "))}` : ""}</button></div>` : "";
  };

  // ---- events: one delegated handler per type, no inline handlers ----
  document.addEventListener("click", async (e) => {
    const t = e.target.closest("button");
    const op = t && t.dataset && t.dataset.op;
    if (!op || t.disabled) return;
    const stage = t.dataset.stage;
    if (op === "run") await startJob(stage, argsOf(stage), false);
    else if (op === "confirm") await startJob(stage, argsOf(stage), true);
    else if (op === "unconfirm") { const wasLoop = C.confirm && C.confirm.stage === "loop"; C.confirm = null; redraw(); if (wasLoop) { const b = $("loop-run"); if (b) b.focus(); } }
    else if (op === "prefill") { C.pre = { stage, chain: str(t.dataset.chain).split(",").filter(Boolean) }; C.confirm = null; V.go("run"); }
    else if (op === "cancel" && D.job) { const r = await call("POST", `/api/jobs/${encodeURIComponent(D.job.id)}/cancel`, {}); if (!r.ok) { D.err = errText(r); dockDraw(false); } else { clearTimeout(D.timer); tick(); } }
    else if (op === "dock-toggle") { D.open = !D.open; dockDraw(true); }
    else if (op === "add-id") { addIds(C.sub.draft); redraw(); }
    else if (op === "rm-f") { C.sub.files.splice(Number(t.dataset.i), 1); redraw(); }
    else if (op === "rm-i") { C.sub.ids.splice(Number(t.dataset.i), 1); redraw(); }
    else if (op === "send") await send();
    else if (op === "save-rubric") await saveRubric();
    else if (op === "tune-save") await saveTune();
    else if (op === "reset-param") { const p = sp(t.dataset.id); if (p) { RB.edits[p.id] = String(p.default); delete RB.errs[p.id]; redraw(); const f = $(`p-${t.dataset.ctx}-${p.id}`); if (f) f.focus(); } }
    else if (op === "reset-q") { const q = RB.data.questions.find((x) => x.id === t.dataset.q); if (q && q.default) { RB.qedits[q.id] = qnorm({ id: q.id, ...q.default }); delete RB.errs[q.id]; redraw(); } }
    else if (op === "ship") { TR.ship = t.dataset.id; redraw(); }
    else if (op === "ship-no") { TR.ship = null; redraw(); }
    else if (op === "ship-go") {
      const id = t.dataset.id, r = await call("POST", `/api/tracker/${encodeURIComponent(id)}/ship`, { confirm: true });
      if (r.status === 202 && r.data.job) { TR.ship = null; TR.msg[id] = null; track(r.data.job); } else TR.msg[id] = errText(r);
      redraw();
    }
    else if (op === "t-sent") { const ref = str(TR.ref[t.dataset.id]).trim(); if (!ref) { TR.msg[t.dataset.id] = "Enter the ticket id or link first."; redraw(); } else await tpost(t.dataset.id, "ticket", { action: "sent", ref }); }
    else if (op === "t-done") await tpost(t.dataset.id, "ticket", { action: "resolved", note: str(TR.note[t.dataset.id]) });
    else if (op === "rejudge") { C.chain = ["regrade", "issues"]; RB.saved = false; RB.msg = null; if (!(await startJob("judge", {}, false, true))) { C.chain = []; RB.msg = (C.msg.judge && C.msg.judge.err) || "Could not start judge."; } redraw(); }
    else if (op === "open-sub") { C.sub.sid = t.dataset.sid; C.sub.watch = null; C.sub.result = null; await loadSub(t.dataset.sid); }
    else if (op === "loop-plan") { L.view = "plan"; redraw(); }
    else if (op === "loop-last") {
      const j = lastLoopJob();
      if (j) {
        const from = Math.max(0, (num(j.log_lines) || 0) - 200), r = await call("GET", `/api/jobs/${encodeURIComponent(j.id)}/log?from=${from}`);
        if (r.ok) { resetLoop(r.data.job || j); onLog(r.data.job || j, Array.isArray(r.data.lines) ? r.data.lines : []); L.view = L.failed ? "failed" : "result"; }
      }
      redraw();
    }
  });
  const onField = (e) => {
    const t = e.target, d = t && t.dataset;
    if (!d) return;
    if (d.op === "arg") { (C.form[d.stage] = C.form[d.stage] || {})[d.arg] = t.type === "checkbox" ? !!t.checked : t.value; }
    else if (d.op === "id-draft") C.sub.draft = t.value;
    else if (d.op === "tref") TR.ref[d.id] = t.value;
    else if (d.op === "tnote") TR.note[d.id] = t.value;
    else if (d.op === "param") {
      RB.edits[d.id] = t.value;
      const p = sp(d.id), save = $("rb-save");
      if (save && !ro()) save.disabled = !anyDirty();
      if (document.querySelectorAll && p) document.querySelectorAll(`[data-chg="${d.id}"]`).forEach((m) => (m.hidden = Number(t.value) === Number(p.default)));
    } else if (d.op === "q") {
      qedit(d.q, d.f, t.value);
      const save = $("rb-save");
      if (save && !ro()) save.disabled = !anyDirty();
    }
    else if (d.op === "tune") {
      T.vals[d.key] = Number(t.value);
      const o = $(`o-${d.key}`);
      if (o) o.textContent = t.value;
      clearTimeout(T.timer);
      T.timer = setTimeout(runTune, 250);
    }
  };
  document.addEventListener("input", onField);
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && C.confirm && C.confirm.stage === "loop") { e.preventDefault(); C.confirm = null; redraw(); const b = $("loop-run"); if (b) b.focus(); }
  });
  document.addEventListener("change", (e) => {
    onField(e);
    const d = e.target && e.target.dataset;
    if (d && d.op === "files") { addFiles(e.target.files); redraw(); }
    else if (d && d.op === "id-draft" && C.sub.draft) { addIds(C.sub.draft); redraw(); }
  });
  document.addEventListener("dragover", (e) => { if (e.target.closest && e.target.closest("[data-drop]")) e.preventDefault(); });
  document.addEventListener("drop", (e) => {
    if (!(e.target.closest && e.target.closest("[data-drop]"))) return;
    e.preventDefault();
    addFiles(e.dataTransfer && e.dataTransfer.files);
    redraw();
  });

  window.VL.console = { C, D, call };
  loadConsole();
})();
