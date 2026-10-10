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
  const TONE = { running: "nonfatal", done: "pass", failed: "fatal", cancelled: "review" };
  const jchip = (s) => `<span class="chip t-${esc(V.own(TONE, s) || "review")}">${esc(s)}</span>`;
  const ro = () => !!(C.con && C.con.read_only);
  const dis = () => (ro() ? " disabled" : "");
  const redraw = () => { const it = V.ITEMS.find((i) => i.id === S.now); if (it && it.group === "operate") V.show(); };

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
    $("dock-info").innerHTML = `${jchip(j.status)} <b class="mono">${esc(j.stage)}</b> <span class="mono">${esc(elapsed(j))}</span>${j.status !== "running" && num(j.cost_usd) != null ? ` <span class="mono">${esc(usd(j.cost_usd))}</span>` : ""} <span class="dock-last mono">${esc(last)}</span>`;
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
    D.lines = D.lines.concat(Array.isArray(r.data.lines) ? r.data.lines.map(str) : []);
    D.next = num(r.data.next) == null ? D.next : r.data.next;
    dockDraw(was !== D.job.status);
    if (C.sub.sid && C.sub.watch === id) await loadSub(C.sub.sid, false);
    if (D.job.status === "running") D.timer = setTimeout(tick, POLL_MS);
    else await ended();
  }
  async function ended() {
    dockDraw(true);
    await Promise.all([loadJobs(), C.sub.sid ? loadSub(C.sub.sid, false) : null, V.refresh()]);
    if (D.job.status === "done" && C.chain.length) {
      const next = C.chain.shift();
      const r = await startJob(next, {}, false, true);
      if (!r) C.chain = [];
    } else C.chain = [];
    redraw();
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
    const body = { stage, args };
    if (confirm) body.confirm = true;
    const r = await call("POST", "/api/jobs", body);
    C.msg[stage] = null;
    if (r.status === 202 && r.data.job) {
      C.confirm = null;
      track(r.data.job);
      if (!quiet) C.chain = C.pre && C.pre.stage === stage ? C.pre.chain.slice() : [];
      C.pre = null;
      return r.data.job;
    }
    if (r.status === 400 && r.data.error === "confirm") C.confirm = { stage, estimate: num(r.data.estimate_usd) };
    else if (r.status === 409) {
      C.msg[stage] = { err: `Another job is running (${str(r.data.job_id)}). Wait for it or cancel it in the dock.` };
      if (r.data.job_id && !D.job) await adopt(r.data.job_id);
    } else C.msg[stage] = { err: errText(r) };
    redraw();
    return null;
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
    } else C.err = errText(r);
    V.nav();
    redraw();
    return !!C.con;
  }

  // ---- views ----
  const unavailable = () => `<p class="empty">The console API is not available (${esc(C.err || "loading")}).</p>`;
  const banner = () => (ro() ? `<p class="note ro rv" role="status"><b>This data folder is read-only.</b> It is the demo freeze, so every control here is off. To run stages, start the console on runs/ with <span class="mono">VANILOOP_DATA=runs uv run python -m vaniloop.web</span>.</p>` : "");
  const field = (stage, a, cause) => {
    const v = (C.form[stage] || {})[a.name], id = `f-${stage}-${a.name}`, base = `data-op="arg" data-stage="${esc(stage)}" data-arg="${esc(a.name)}"`;
    const label = `<label class="label" for="${esc(id)}">${esc(a.name.replace(/_/g, " "))}${a.required ? " *" : ""}</label>`;
    if (a.type === "bool") return `<div class="fld chk"><input type="checkbox" id="${esc(id)}" ${base}${v ? " checked" : ""}${dis()}>${label}</div>`;
    if (Array.isArray(a.choices)) return `<div class="fld">${label}<select class="inp" id="${esc(id)}" ${base}${dis()}>${a.choices.map((c) => `<option value="${esc(c)}"${c === v ? " selected" : ""}>${esc(c)}</option>`).join("")}</select></div>`;
    const list = a.name === "cause" ? ' list="cause-ids"' : "";
    const hint = a.type === "call_ids" ? ' placeholder="ids, separated by spaces or commas"' : "";
    return `<div class="fld">${label}<input class="inp" id="${esc(id)}" ${base} type="${a.type === "int" ? "number" : "text"}" value="${esc(v == null ? "" : v)}"${list}${hint}${dis()}></div>`;
  };
  const card = (s) => {
    const m = C.msg[s.id], cf = C.confirm && C.confirm.stage === s.id, hl = C.pre && C.pre.stage === s.id;
    const est = C.confirm && num(C.confirm.estimate) != null ? `Estimate ${usd(C.confirm.estimate)}.` : "Estimate not known.";
    return `<div class="card op-card rv${hl ? " hl" : ""}" data-card="${esc(s.id)}"><h3>${esc(s.label || s.id)}</h3><p class="label">${s.paid ? "Paid stage" : "Free stage"} · <span class="mono">${esc(s.id)}</span></p>` +
      `<div class="flds">${s.args.map((a) => field(s.id, a)).join("") || '<p class="lede">No arguments.</p>'}</div>` +
      (hl && C.pre.chain.length ? `<p class="lede">After it ends, the console runs ${esc(C.pre.chain.join(", "))}.</p>` : "") +
      (cf ? `<div class="confirm" role="alertdialog" aria-label="Confirm paid stage"><p><b>This calls Gemini and spends money.</b> ${esc(est)}</p><button type="button" class="btn pri" data-op="confirm" data-stage="${esc(s.id)}"${dis()}>Confirm and run</button> <button type="button" class="btn" data-op="unconfirm"${dis()}>Cancel</button></div>`
        : `<button type="button" class="btn pri" data-op="run" data-stage="${esc(s.id)}"${dis()}>Run ${esc(s.label || s.id)}</button>`) +
      (m && m.err ? `<p class="err" role="alert">${esc(m.err)}</p>` : "") + `</div>`;
  };
  const jobsTable = () =>
    `<div class="tw rv"><table><thead><tr><th>Stage</th><th>Status</th><th>Started</th><th>Ended</th><th class="r">Exit</th><th class="r">Cost</th></tr></thead><tbody>${C.jobs.map((j) => `<tr><td class="m">${esc(j.stage)}</td><td>${jchip(j.status)}</td><td class="m">${esc(j.started_ist)}</td><td class="m">${esc(j.ended_ist || "–")}</td><td class="m r">${esc(j.exit_code == null ? "–" : j.exit_code)}</td><td class="m r">${esc(usd(j.cost_usd))}</td></tr>`).join("") || '<tr><td colspan="6" class="src">No jobs yet.</td></tr>'}</tbody></table></div>`;
  const causes = () => { const r = S.stage.issues; return r && r.v && Array.isArray(r.v.causes) ? r.v.causes : []; };
  P.run = () => {
    if (!C.con) return V.head("Operate", "Run a <em>stage</em>", "") + unavailable();
    return V.head("Operate", "Run a <em>stage</em>", `One job runs at a time. Data folder: ${C.con.data_dir_name}.`) + banner() +
      `<datalist id="cause-ids">${causes().map((c) => `<option value="${esc(c.cause_id)}">`).join("")}</datalist>` +
      `<div class="cards">${C.con.stages.filter((s) => s.id !== "submit").map(card).join("")}</div>` + V.sec("Job history") + jobsTable();
  };

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
    if (!C.con) return V.head("Operate", "Submit <em>calls</em>", "") + unavailable();
    const sub = C.sub, m = sub.msg;
    const chips = (list, kind) => list.map((x, i) => `<span class="pill">${esc(kind === "f" ? x.name : x)}<button type="button" class="x" data-op="rm-${kind}" data-i="${i}" aria-label="Remove ${esc(kind === "f" ? x.name : x)}"${dis()}>×</button></span>`).join("");
    return V.head("Operate", "Submit <em>calls</em>", "Audio and transcripts, or ids of calls already read in. Each gets a verdict.") + banner() +
      `<div class="card rv"><div class="drop" data-drop="1"><label class="label" for="sub-files">Drop or pick mp3 and transcript json files</label><input id="sub-files" class="inp" type="file" multiple accept=".mp3,.json" data-op="files"${dis()}></div>` +
      `<div class="fld"><label class="label" for="sub-id">Call ids</label><div class="addrow"><input class="inp" id="sub-id" list="sub-ids" autocomplete="off" data-op="id-draft" value="${esc(sub.draft)}" placeholder="type or paste ids"${dis()}><button type="button" class="btn" data-op="add-id"${dis()}>Add</button></div>` +
      `<datalist id="sub-ids">${C.calls.slice(0, 500).map((id) => `<option value="${esc(id)}">`).join("")}</datalist></div>` +
      `<div class="chips">${chips(sub.files, "f")}${chips(sub.ids, "i")}</div>` +
      `<p class="lede">Sending audits the calls with Gemini and spends money.</p><button type="button" class="btn pri" data-op="send"${sub.busy ? " disabled" : dis()}>Send for audit</button>` +
      (m ? `<p class="${m.err ? "err" : "ok"}" role="${m.err ? "alert" : "status"}">${esc(m.err || m.ok)}</p>` : "") + `</div>` +
      (sub.result ? V.sec(`Results · ${sub.sid}`) + `<ul class="items res-list rv">${(sub.result.calls || []).map(resultRow).join("") || '<li class="empty">No results yet.</li>'}</ul>` : "") +
      V.sec("Past submissions") + `<div class="tw rv"><table><tbody>${sub.list.map((s) => `<tr><td class="m">${esc(s.submission_id)}</td><td class="m">${esc(s.created_ist)}</td><td class="m r">${esc(s.calls)} calls</td><td class="r"><button type="button" class="btn sm" data-op="open-sub" data-sid="${esc(s.submission_id)}"${dis()}>Open</button></td></tr>`).join("") || '<tr><td class="src">None yet.</td></tr>'}</tbody></table></div>`;
  };

  // ---- rubric ----
  const RB = { data: null, err: null, edits: {}, qedits: {}, errs: {}, msg: null, saved: false };
  const pval = (p) => (p.id in RB.edits ? RB.edits[p.id] : p.value);
  const pdirty = (p) => p.id in RB.edits && Number(RB.edits[p.id]) !== Number(p.value);
  const qnorm = (q) => ({ instructions: q.instructions, true: { description: q.true.description, examples: q.true.examples || [] }, false: { description: q.false.description, examples: q.false.examples || [] } });
  const qok = (q) => q && q.true && q.false;
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
    if (!C.con) return V.head("Operate", "Audit <em>rubric</em>", "") + unavailable();
    const d = RB.data;
    if (!d) return V.head("Operate", "Audit <em>rubric</em>", "") + (RB.err ? `<p class="empty">The rubric could not load (${esc(RB.err)}).</p>` : '<p class="empty">Loading.</p>');
    const byId = Object.fromEntries(d.params.map((p) => [p.id, p])), used = new Set();
    const check = (c) => {
      const ps = (c.params || []).map((id) => byId[id]).filter(Boolean);
      ps.forEach((p) => used.add(p.id));
      return `<div class="card op-card rv"><h3>${esc(c.name || c.id)}</h3><p class="label"><span class="mono">${esc(c.id)}</span> · ${esc(c.judged_by)}${c.prompt_ref ? ` · ${esc(c.prompt_ref)}` : ""}</p>` +
        `<p class="lede">${esc(c.what)}</p><p class="why"><b>Fatal when</b> ${esc(c.fatal_when)}</p><p class="why"><b>Non-fatal when</b> ${esc(c.nonfatal_when)}</p><div class="flds">${ps.map((p) => paramField(p, c.id)).join("")}</div></div>`;
    };
    const cards = d.checks.map(check).join(""), rest = d.params.filter((p) => !used.has(p.id));
    return V.head("Operate", "Audit <em>rubric</em>", `Each check and the numbers it grades with. Jev version ${d.jev_version_effective || ""}.`) + banner() + savedBanner() +
      `<div class="savebar"><button type="button" class="btn pri" id="rb-save" data-op="save-rubric"${anyDirty() ? dis() : " disabled"}>Save rubric</button></div>` +
      V.sec("Checks") + `<div class="cards">${cards}${rest.length ? `<div class="card op-card rv"><h3>Other numbers</h3><div class="flds">${rest.map((p) => paramField(p, "other")).join("")}</div></div>` : ""}</div>` +
      V.sec("Jev questions") + `<div class="cards">${(d.questions || []).map(qCard).join("")}</div>`;
  };
  const putRubric = async (body) => {
    const r = await call("PUT", "/api/rubric", body);
    if (r.ok && Array.isArray(r.data.params)) { RB.data = r.data; RB.edits = {}; RB.qedits = {}; RB.errs = {}; RB.msg = null; RB.saved = true; }
    else { RB.saved = false; RB.errs = r.data.field ? { [str(r.data.field)]: str(r.data.error) } : {}; RB.msg = r.data.field ? null : errText(r); }
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
    if (!C.con) return V.head("Operate", "Tune the <em>thresholds</em>", "") + unavailable();
    if (!RB.data) return V.head("Operate", "Tune the <em>thresholds</em>", "") + (RB.err ? `<p class="empty">The rubric could not load (${esc(RB.err)}).</p>` : '<p class="empty">Loading.</p>');
    const slider = ([k, label, id]) => {
      const p = sp(id) || { min: 0, max: 1, step: 0.01 }, v = tval(k, id);
      return `<div class="fld"><label class="label" for="s-${esc(k)}">${esc(label)} <output id="o-${esc(k)}" class="mono">${esc(v)}</output></label><input id="s-${esc(k)}" type="range" class="rng" data-op="tune" data-key="${esc(k)}" min="${esc(p.min)}" max="${esc(p.max)}" step="${esc(p.step)}" value="${esc(v)}"${dis()}></div>`;
    };
    return V.head("Operate", "Tune the <em>thresholds</em>", "Move a slider to re-score the reviewed calls. Nothing is saved until you press Save to rubric.") + banner() + savedBanner() +
      `<div class="card rv"><div class="sliders">${SL.map(slider).join("")}</div><button type="button" class="btn pri" data-op="tune-save"${dis()}>Save to rubric</button></div>` + V.sec("Against the reviewed truth") + `<div id="tune-res">${tuneRes()}</div>`;
  };
  async function saveTune() {
    const params = {};
    for (const [k, , id] of SL) { const p = sp(id); if (p && Number(tval(k, id)) !== Number(p.value)) params[id] = Number(tval(k, id)); }
    const r = await putRubric({ params, questions: {} });
    if (r.ok) T.vals = {}; else T.err = { text: errText(r) };
    redraw();
  }

  V.ITEMS.push({ id: "run", group: "operate", name: "Run" }, { id: "submit", group: "operate", name: "Submit calls" }, { id: "rubric", group: "operate", name: "Rubric" }, { id: "tune", group: "operate", name: "Tune" });
  const enter = async () => { if (!C.con) await loadConsole(); await loadJobs(); redraw(); };
  V.enter = {
    run: async () => { await enter(); },
    submit: async () => {
      await enter(); await loadSubs();
      const x = await call("GET", "/api/calls");
      if (x.ok && Array.isArray(x.data)) C.calls = x.data.map((r) => str(r && r.call_id)).filter(Boolean);
      redraw();
    },
    rubric: async () => { await enter(); RB.saved = false; await loadRubric(); redraw(); },
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
    else if (op === "unconfirm") { C.confirm = null; redraw(); }
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
    else if (op === "rejudge") { C.chain = ["regrade", "issues"]; RB.saved = false; RB.msg = null; if (!(await startJob("judge", {}, false, true))) { C.chain = []; RB.msg = (C.msg.judge && C.msg.judge.err) || "Could not start judge."; } redraw(); }
    else if (op === "open-sub") { C.sub.sid = t.dataset.sid; C.sub.watch = null; C.sub.result = null; await loadSub(t.dataset.sid); }
  });
  const onField = (e) => {
    const t = e.target, d = t && t.dataset;
    if (!d) return;
    if (d.op === "arg") { (C.form[d.stage] = C.form[d.stage] || {})[d.arg] = t.type === "checkbox" ? !!t.checked : t.value; }
    else if (d.op === "id-draft") C.sub.draft = t.value;
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
