// vaniloop dashboard: a sidebar (Home, Results, Configuration, Tools), a page head with one action, and one panel per page.
// Every data string goes through esc().
(() => {
  "use strict";
  const str = (s) => (s == null || typeof s === "object" || typeof s === "function" ? "" : String(s));
  const esc = (s) => str(s).replace(/[&<>"']/g, (c) => `&#${c.charCodeAt(0)};`);
  const $ = (id) => document.getElementById(id);
  const num = (v) => (typeof v === "number" && isFinite(v) ? v : null);
  const fmt = (v, d = 0) => (num(v) == null ? "–" : v.toLocaleString("en-IN", { minimumFractionDigits: d, maximumFractionDigits: d }));
  const pct = (v) => (num(v) == null ? "–" : `${Math.round(v * 100)}%`);
  const leaf = (o) => (o && typeof o === "object" && "value" in o ? o.value : null);
  const TONE = { Fatal: "fatal", NonFatal: "nonfatal", NeedsReview: "review", Unknown: "review", Pass: "pass" };
  const WORD = { Fatal: "Fatal", NonFatal: "Non-fatal", NeedsReview: "Needs review", Unknown: "Unknown", Pass: "Pass" };
  const NAMES = {
    product_change: "Right product", category_isq: "Right questions", qty_probing: "Quantity asked", qty_missed: "Not asked twice",
    qty_accuracy: "Right number", qty_unit: "Right unit", looping: "No looping", dead_air: "No dead air", intent: "Buying intent",
  };
  const own = (d, k) => (typeof k === "string" && Object.hasOwn(d, k) ? d[k] : undefined);
  const tone = (v) => own(TONE, v) || "review";
  const word = (v) => own(WORD, v) || str(v);
  // A badge is a word and a glyph, so colour is never the only signal.
  const GLYPH = { pass: "✓", fatal: "✕", nonfatal: "◐", review: "○" };
  const badge = (t, w, g) => `<span class="chip t-${esc(t)}"><span aria-hidden="true">${esc(g || own(GLYPH, t))}</span> ${esc(w)}</span>`;
  const chip = (v) => badge(tone(v), word(v));
  const DECISION = { ship: ["Passed", "pass"], reject: ["Did not pass", "fatal"], inconclusive: ["Unclear", "review"] };
  const dchip = (d) => { const [w, t] = own(DECISION, d) || [str(d), "review"]; return badge(t, w); };
  const cap = (s) => str(s).replace(/^./, (c) => c.toUpperCase());

  // side: the sidebar group (top, results, config, tools) or "tab" for a tab of its family. fam groups a page with its tabs.
  // group keeps show() and redraw() as they were. An icon is a fixed SVG path, never data.
  const ITEMS = [
    { id: "ingest", group: "stage", name: "Ingest", side: "results", fam: "ingest", step: 1, tab: "Overview" },
    { id: "audit", group: "stage", name: "Audit", side: "results", fam: "audit", step: 2, tab: "Results" },
    { id: "review", group: "stage", name: "Judge accuracy", side: "tab", fam: "audit", step: 2, tab: "Judge accuracy" },
    { id: "issues", group: "stage", name: "Issues", side: "results", fam: "issues", step: 3 }, { id: "fix", group: "stage", name: "Fix", side: "results", fam: "fix", step: 4 },
    { id: "gate", group: "stage", name: "Test", side: "results", fam: "gate", step: 5 },
    { id: "judge", group: "config", name: "Judges", side: "config", fam: "judge" },
    { id: "thresholds", group: "config", name: "All constants", side: "tab", fam: "tune", tab: "All constants" },
    { id: "fixloop", group: "config", name: "Test settings", side: "config", fam: "fixloop" }, { id: "data", group: "config", name: "Data files", side: "config", fam: "data" },
  ];
  const SIDE = [["top", ""], ["results", "Results"], ["config", "Configuration"], ["tools", "Tools"]];
  const ORDER = ["home", "tracker", "ingest", "audit", "issues", "fix", "gate", "rubric", "tune", "judge", "fixloop", "data", "run"];
  const tabsOf = (fam) => ITEMS.filter((i) => i.fam === fam && i.tab).sort((a, b) => (a.side === "tab") - (b.side === "tab")); // the family's own page first
  const ICON = {
    home: '<path d="M20 12a8 8 0 1 1-2.3-5.6"/><path d="M20 4.5V8h-3.5"/>',
    ingest: '<path d="M12 4v10"/><path d="m8 10 4 4 4-4"/><path d="M4 15v3a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-3"/>',
    audit: '<circle cx="11" cy="11" r="6.5"/><path d="m16 16 4 4"/><path d="m8.5 11 1.8 1.8 3.4-3.6"/>',
    issues: '<path d="M12 4 3 19h18z"/><path d="M12 10v4"/><path d="M12 16.8v.2"/>',
    fix: '<path d="M14.5 6.5a4 4 0 0 0-5.3 5.3L4 17l3 3 5.2-5.2a4 4 0 0 0 5.3-5.3l-2.4 2.4-2.6-.4-.4-2.6z"/>',
    gate: '<path d="M9 3h6"/><path d="M10 3v6l-5 9a2 2 0 0 0 1.7 3h10.6a2 2 0 0 0 1.7-3l-5-9V3"/><path d="M7.6 15h8.8"/>',
    rubric: '<path d="M7 4h10a2 2 0 0 1 2 2v14H7a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2z"/><path d="M9 9h6"/><path d="M9 13h6"/>',
    tune: '<path d="M5 6h9"/><path d="M18 6h1"/><circle cx="16" cy="6" r="2"/><path d="M5 12h3"/><path d="M12 12h7"/><circle cx="10" cy="12" r="2"/><path d="M5 18h9"/><path d="M18 18h1"/><circle cx="16" cy="18" r="2"/>',
    judge: '<path d="M12 4v16"/><path d="M5 8h14"/><path d="m5 8-2.5 6a3 3 0 0 0 5 0z"/><path d="m19 8-2.5 6a3 3 0 0 0 5 0z"/>',
    fixloop: '<circle cx="12" cy="12" r="3"/><path d="M12 3v2M12 19v2M3 12h2M19 12h2M5.6 5.6l1.4 1.4M17 17l1.4 1.4M5.6 18.4 7 17M17 7l1.4-1.4"/>',
    data: '<ellipse cx="12" cy="6" rx="7" ry="2.5"/><path d="M5 6v12c0 1.4 3.1 2.5 7 2.5s7-1.1 7-2.5V6"/><path d="M5 12c0 1.4 3.1 2.5 7 2.5s7-1.1 7-2.5"/>',
    tracker: '<path d="M9 6h11"/><path d="M9 12h11"/><path d="M9 18h11"/><path d="m3.5 6 1 1 2-2"/><path d="m3.5 12 1 1 2-2"/><path d="m3.5 18 1 1 2-2"/>',
    run: '<circle cx="12" cy="12" r="8.5"/><path d="m10.3 8.8 5 3.2-5 3.2z"/>',
    calls: '<path d="M4 10v4"/><path d="M8 7v10"/><path d="M12 4v16"/><path d="M16 8v8"/><path d="M20 11v2"/>',
  };
  const icon = (id) => `<span class="ic" aria-hidden="true"><svg viewBox="0 0 24 24">${own(ICON, id) || ""}</svg></span>`;
  const S = { cfg: null, stage: {}, judge: null, avail: [], now: "home", filt: { kind: "all", verdict: "all", fatal: false }, calls: null, causes: null, cv: { verdict: "Fatal", n: 25 } };
  const famHead = (fam) => ITEMS.find((i) => i.fam === fam && i.side !== "tab");

  async function get(path) {
    const res = await fetch(path, { headers: { Accept: "application/json" } });
    const body = await res.json().catch(() => null);
    if (!res.ok) throw new Error((body && body.error) || res.statusText);
    return body;
  }
  const settle = (p) => p.then((v) => ({ v }), (e) => ({ e }));
  const sum = (o) => Object.values(o || {}).reduce((a, b) => a + (num(b) || 0), 0);

  function heads() {
    const s = S.stage, g = (k) => (s[k] && !s[k].e ? s[k].v : null);
    const ing = g("ingest"), au = g("audit"), is = g("issues"), fx = g("fix"), ga = g("gate");
    const shipped = ga ? ga.gates.filter((x) => x.decision === "ship").length : 0;
    return {
      ...(window.VL && window.VL.vals ? window.VL.vals() : {}),
      ingest: ing && `${fmt(ing.calls)} calls`,
      audit: au && `${fmt(au.verdicts.Fatal || 0)} fatal`,
      issues: is && `${fmt(is.causes.length)} causes`,
      fix: fx && `${fmt(fx.patches.length)} patches`,
      gate: ga && `${fmt(shipped)} of ${fmt(ga.gates.length)} passed`,
    };
  }

  function nav() {
    const h = heads(), cur = ITEMS.find((i) => i.id === S.now) || {};
    const row = (it) => {
      const on = it.id === S.now ? "page" : it.fam && it.fam === cur.fam ? "step" : "";
      return `<button class="item" type="button" data-go="${it.id}"${on ? ` aria-current="${on}"` : ""}>${icon(it.id)}<span class="nm">${esc(it.name)}</span><span class="val">${esc(h[it.id] || "")}</span></button>`;
    };
    const rank = (i) => (ORDER.indexOf(i.id) < 0 ? 99 : ORDER.indexOf(i.id));
    const of = (side) => ITEMS.filter((i) => i.side === side).sort((a, b) => rank(a) - rank(b)).map(row).join("");
    $("nav").innerHTML = SIDE.map(([side, label]) => { const rows = of(side); return rows ? (label ? `<p class="grp">${esc(label)}</p>` : "") + rows : ""; }).join("") +
      `<a class="item" href="/#/">${icon("calls")}<span class="nm">All calls</span><span class="val" aria-hidden="true">↗</span></a>`;
  }

  function flat(o, path = []) {
    if (!o || typeof o !== "object") return [];
    if ("value" in o && "source" in o) return [{ key: path.join(" › ").replace(/_/g, " "), value: o.value, source: o.source }];
    return Object.entries(o).flatMap(([k, v]) => flat(v, [...path, k]));
  }
  const bn = (n, label, t = "", dec = 0, unit = "") =>
    `<div class="bn ${t ? `t-${t}` : ""}"><span class="n">${fmt(n, dec)}${unit ? `<small>${esc(unit)}</small>` : ""}</span><p class="label">${esc(label)}</p></div>`;
  // The page head: a kicker, the title, one sentence and the page's one action, then the family's pages as a segmented control.
  const head = (title, lede) => {
    const it = ITEMS.find((i) => i.id === S.now) || {}, tabs = tabsOf(it.fam), top = famHead(it.fam) || it;
    const kicker = it.step ? `Step ${it.step} of 5` : top.side === "config" ? "Configuration" : "";
    const act = window.VL && window.VL.stageBar ? window.VL.stageBar(it.id) : "";
    return `<header class="phead"><div class="phead-t">${kicker ? `<p class="kicker">${esc(kicker)}</p>` : ""}<h1 class="ptitle" tabindex="-1">${esc(title)}</h1>${lede ? `<p class="lede">${esc(lede)}</p>` : ""}</div>` +
      (act ? `<div class="phead-a">${act}</div>` : "") + `</header>` +
      (tabs.length > 1 ? `<nav class="tabs" aria-label="${esc(top.name)} pages">${tabs.map((t) => `<a href="#/${t.id}"${t.id === it.id ? ' aria-current="page"' : ""}>${esc(t.tab)}</a>`).join("")}</nav>` : "");
  };
  const sec = (t) => `<h2 class="sec">${esc(t)}</h2>`;
  const failed = (e) => `<p class="empty">This panel could not load (${esc(e.message)}).</p>`;
  // Per-call rows from GET /api/calls (the active judge's grades). Each row links to the call's transcript.
  const PAGE = 25, CV = [["Fatal", "Fatal"], ["NonFatal", "Non-fatal"], ["NeedsReview", "Needs review"], ["Pass", "Pass"], ["all", "All"]];
  const clock = (s) => { if (num(s) == null || s < 0) return "–"; const r = Math.round(s); return `${Math.floor(r / 60)}:${String(r % 60).padStart(2, "0")}`; };
  const callList = () => (S.calls && Array.isArray(S.calls.v) ? S.calls.v.filter((c) => c && typeof c === "object" && typeof c.call_id === "string" && c.call_id) : null);
  const callRow = (c) => {
    const ff = c.first_fault && typeof c.first_fault === "object" ? c.first_fault : null, why = ff ? str(ff.reason) : "";
    return `<li>${chip(c.verdict)}<a class="cid" href="/#/call/${esc(encodeURIComponent(c.call_id))}">${esc(c.call_id)} <small>Transcript</small></a>` +
      `<span class="ck">${ff ? esc(own(NAMES, ff.parameter) || ff.parameter) : ""}</span><span class="rsn"${why ? ` title="${esc(why)}"` : ""}>${ff ? esc(why) : "No fault found"}</span><span class="dur">${esc(clock(c.duration_sec))}</span></li>`;
  };
  const callsSec = () => {
    const r = S.calls;
    if (!r || r.e) return sec("Calls") + (r ? failed(r.e) : '<p class="empty">Loading.</p>');
    const all = callList() || [], cv = S.cv, act = S.cfg && leaf(S.cfg.judge.active);
    const vis = all.filter((c) => cv.verdict === "all" || c.verdict === cv.verdict), rest = vis.length - cv.n;
    const n = (v) => (v === "all" ? all.length : all.filter((c) => c.verdict === v).length);
    return sec("Calls") + (act && S.judge !== act ? `<p class="label">These rows show ${esc(cap(act))} grades.</p>` : "") +
      `<div class="pick calls-f"><span class="label" id="calls-l">Show</span><div class="seg" role="group" aria-labelledby="calls-l">${CV.map(([v, l]) => `<button type="button" data-view data-calls="${esc(v)}" aria-pressed="${cv.verdict === v}">${esc(l)} <span class="n">${esc(fmt(n(v)))}</span></button>`).join("")}</div></div>` +
      `<ul class="calls-list rv">${vis.slice(0, cv.n).map(callRow).join("") || `<li class="empty">${all.length ? "No calls match." : "No graded calls yet."}</li>`}</ul>` +
      (rest > 0 ? `<button type="button" class="btn sm calls-more" data-view data-more="calls">Show ${esc(Math.min(PAGE, rest))} more</button>` : "");
  };
  // Under a cause: up to 10 of its own calls (fatal_calls and nonfatal_calls in GET /api/causes), most severe first.
  // Not "first fault is the cause's check": a channel cause shares its check with a prompt cause. Left out until both lists load.
  const idList = (x) => (Array.isArray(x) ? x.filter((s) => typeof s === "string" && s) : []);
  const faultCalls = (id) => {
    const all = callList(), cs = S.causes && S.causes.v && Array.isArray(S.causes.v.causes) ? S.causes.v.causes : null;
    const c = cs && typeof id === "string" && id ? cs.find((x) => x && typeof x === "object" && x.cause_id === id) : null;
    if (!all || !c) return "";
    const mem = new Set([...idList(c.fatal_calls), ...idList(c.nonfatal_calls)]), m = all.filter((r) => mem.has(r.call_id));
    return m.length ? `<details class="calls-d"><summary>Calls with this fault (${esc(fmt(m.length))})</summary><ul class="calls-list">${m.slice(0, 10).map(callRow).join("")}</ul>${m.length > 10 ? '<p class="label">The first 10 are shown.</p>' : ""}</details>` : "";
  };

  const P = {};
  const LABEL_NAME = { question_fits: "Question fits the product", option_fits: "Each option fits", product_match: "Same product" };
  const band = (name, t, f) =>
    `<div class="card rv"><p class="label">${esc(name)}</p><div class="band" role="img" aria-label="${esc(name)}: false below ${esc(f)}, unsure between, true above ${esc(t)}"><span class="f" style="width:${f * 100}%">False</span><span class="u" style="width:${(t - f) * 100}%">Unsure</span><span class="t" style="width:${(1 - t) * 100}%">True</span>` +
    `<span class="tick" style="left:${f * 100}%">${esc(f)}</span><span class="tick" style="left:${t * 100}%">${esc(t)}</span></div><div class="axis"><span>0</span><span>1</span></div></div>`;
  P.judge = () => {
    const j = S.cfg.judge, th = j.thresholds || {};
    const bands = Object.keys(th)
      .filter((k) => th[k] && typeof th[k] === "object" && "true_at" in th[k])
      .map((k) => {
        const t = num(leaf(th[k].true_at)), f = num(leaf(th[k].false_at));
        return t === null || f === null ? "" : band(LABEL_NAME[k] || k, t, f);
      })
      .join("");
    const drop = th.name_check_drop_at ? leaf(th.name_check_drop_at) : null;
    return (
      head("Judges", "Gemini lays out each call, and Jev answers each true-or-false question about it.") + sec("Who does what") +
      `<div class="roles rv"><div class="card role"><p class="label">Structure</p><p class="big">${esc(leaf(j.structure_model))}</p><p class="lede">Gemini reads the call and lays out its turns, specs and quantities.</p></div>` +
      `<div class="card role"><p class="label">Judgement</p><p class="big">${esc(leaf(j.jev_model))}</p><p class="lede">Jev, version <span class="mono">${esc(leaf(j.jev_version))}</span>, labels each judgement as true, false or unsure.</p></div></div>` +
      sec("Where Jev says true or false") +
      `<p class="lede rv">Each label has its own band, tuned on the reviewed calls. Below the low mark Jev says false. Above the high mark it says true. Between, the label stays unsure and the call goes to review.</p>` +
      bands +
      (drop !== null ? `<p class="lede rv">A spec turn's question label is dropped to unsure when Jev's check that the turn asks that spec is at or below <span class="mono">${esc(drop)}</span>.</p>` : "") +
      sec("Which grades the pages show") +
      `<p class="lede rv">Showing <b class="mono">${esc(S.judge)}</b> numbers. <span class="mono">${esc(S.cfg.judge.active.source)}</span></p>`
    );
  };
  const rows = (items) =>
    `<div class="tw rv"><table><thead><tr><th>Setting</th><th class="m">Value</th><th>Source</th></tr></thead><tbody>${items
      .map((r) => `<tr><td>${esc(r.key)}</td><td class="m">${esc(r.value)}</td><td class="src">${esc(r.source)}</td></tr>`)
      .join("")}</tbody></table></div>`;
  P.thresholds = () => head("All constants", "Every number the Python rules grade with, and the file it lives in.") + rows(flat(S.cfg.audit));
  P.fixloop = () => {
    const fl = S.cfg.fix_loop, last = flat(fl.last_run);
    return (
      head("Test settings", "How many tries each cause gets, and how sure a test must be before a patch passes.") +
      rows(flat({ ...fl, last_run: undefined })) + sec("Last run") +
      (last.length ? rows(last.map((r) => ({ ...r, key: r.key.replace(/^last run › /, "") }))) : `<p class="note">No test has run yet.</p>`)
    );
  };
  P.data = () => {
    const d = S.cfg.data;
    return (
      head("Data files", "The files in this data folder, with their row counts and the time each one changed, in IST.") +
      `<div class="tw rv"><table><thead><tr><th>File</th><th class="r">Rows</th><th class="r">Updated</th></tr></thead><tbody>${d.files
        .map((f) => `<tr><td class="m">${esc(f.name)}</td><td class="m r">${f.rows == null ? '<span class="src">not made yet</span>' : esc(fmt(f.rows))}</td><td class="m r">${esc(f.updated_ist || "–")}</td></tr>`)
        .join("")}</tbody></table></div>`
    );
  };
  P.ingest = (d) =>
    head("Ingest", "The calls this folder holds, read in from transcripts and audio.") +
    `<p class="note">New files are read in from the command line with <span class="mono">uv run python -m vaniloop.ingest</span>. To grade a few calls from here, use <a href="#/submit">Submit calls</a>.</p>` +
    `<div class="big-nums" style="margin-top:24px">${bn(d.calls, "Calls")}${bn(d.with_audio, "With audio")}${bn(d.minutes_total, "Minutes", "", 1)}${bn(d.median_sec, "Median", "", 1, "s")}${bn(d.p90_sec, "90th percentile", "", 1, "s")}${bn(d.short_calls, "Short calls")}</div>`;
  P.audit = (d) => {
    const order = ["Fatal", "NonFatal", "NeedsReview", "Pass"], v = d.verdicts, tot = d.total || 1;
    const checks = Object.entries(d.by_parameter);
    const pick = S.avail.length > 1 ? `<div class="pick"><span class="label" id="pick-l">Show grades from</span><div class="seg" role="group" aria-labelledby="pick-l">${S.avail.map((j) => `<button type="button" data-view data-judge="${esc(j)}" aria-pressed="${j === S.judge}">${esc(cap(j))}</button>`).join("")}</div></div>` : "";
    return (
      head("Audit", `${cap(d.judge)} graded ${fmt(d.total)} calls. A model labels each turn, and Python rules give the grade.`) + pick +
      `<div class="vbar" role="img" aria-label="Verdicts">${order.filter((k) => v[k]).map((k) => `<span class="t-${tone(k)}" style="width:${(100 * v[k]) / tot}%"></span>`).join("")}</div>` +
      `<div class="big-nums" style="margin-top:24px">${order.map((k) => bn(v[k] || 0, word(k), tone(k))).join("")}</div>` +
      sec("Call types") + `<div class="chips">${Object.entries(d.scopes).map(([k, n]) => `<span class="pill">${esc(k === "bl_collection" ? "lead call" : k.replace(/_/g, " "))}<b>${esc(n)}</b></span>`).join("")}</div>` +
      sec("Checks") +
      `<div class="tw rv"><table><thead><tr><th>Check</th>${order.concat("Unknown").map((k) => `<th class="r">${esc(word(k))}</th>`).join("")}</tr></thead><tbody>${checks
        .map(([p, c]) => `<tr><td>${esc(own(NAMES, p) || p)}</td>${order.concat("Unknown").map((k) => `<td class="r">${c[k] ? `<span class="cell t-${tone(k)}">${esc(c[k])}</span>` : '<span class="cell z">0</span>'}</td>`).join("")}</tr>`)
        .join("")}</tbody></table></div>` + callsSec() +
      sec("Flags") + `<div class="chips rv">${Object.entries(d.flags).map(([k, n]) => `<span class="pill">${esc(k.replace(/_/g, " "))}<b>${esc(n)}</b></span>`).join("") || '<span class="empty">None.</span>'}</div>`
    );
  };
  P.issues = (d) => {
    const fa = (c) => num(c.fatal) || 0, nf = (c) => num(c.nonfatal) || 0, max = Math.max(1, ...d.causes.map((c) => fa(c) + nf(c)));
    return (
      head("Issues", "Faults grouped by their cause. The cause behind the most fatal calls comes first.") +
      `<ol class="ranked rv">${d.causes.map((c) => `<li><div><h3>${esc(c.name)}</h3><p class="meta"><span>${esc(c.cause_id)}</span><span>${esc(own(NAMES, c.parameter) || c.parameter)}</span><span>${esc(str(c.type).replace(/_/g, " "))}</span><span>${esc(str(c.status).replace(/_/g, " "))}</span><span>owner ${esc(c.owner || "unassigned")}</span></p></div>` +
        `<div><div class="pair"><span class="a" style="width:${(100 * fa(c)) / max}%"></span><span class="b" style="width:${(100 * nf(c)) / max}%"></span></div><p class="pc"><b>${esc(c.fatal)}</b> fatal · <b>${esc(c.nonfatal)}</b> non-fatal</p></div>${faultCalls(c.cause_id)}</li>`).join("")}</ol>`
    );
  };
  const diff = (p, extra = "") =>
    `<div class="card rv"><h3>${esc(p.cause_id)} <span class="label">attempt ${esc(p.attempt)}</span> ${p.gate_decision ? dchip(p.gate_decision) : ""}</h3><div class="diff"><div class="rm"><span class="label">Removed</span>${esc(p.old_text)}</div><div class="ad"><span class="label">Added</span>${esc(p.new_text)}</div></div><p class="lede">${esc(p.rationale)}</p>${extra}</div>`;
  P.fix = (d) =>
    head("Fix", "For each cause the prompt can fix: the old text, the new text and the reason for the change.") +
    (d.patches.length ? d.patches.map((p) => diff(p)).join("") : `<p class="note rv"><b>No patches yet.</b> Prompt-side causes get a patch once the fix stage runs.</p>`) +
    (d.tickets.length ? sec("Evidence tickets") + d.tickets.map((t) => (t && typeof t === "object" ? t : { cause_id: t })).map((t) => `<div class="card rv"><h3>${esc(t.cause_id)}</h3><p class="lede">${esc(t.summary || t.name || "")}</p></div>`).join("") : "") +
    (d.history.length ? sec("Earlier attempts") + d.history.map((p) => diff(p)).join("") : "");
  P.gate = (d) => {
    const name = (id) => { const c = S.stage.issues && S.stage.issues.v && S.stage.issues.v.causes.find((x) => x.cause_id === id); return c ? c.name : ""; };
    const screen = (id) => d.screens.find((s) => s.cause_id === id);
    const shipped = d.gates.filter((g) => g.decision === "ship").length;
    const rate = (l, v, t) => `<div class="rate"><span>${l}</span><span class="tr"><i class="t-${t}" style="width:${Math.max(1, (v || 0) * 100)}%"></i></span><b>${pct(v)}</b></div>`;
    // per_call comes from a file: keep only rows with a non-empty string id, clamp k to 1..40 and fails to 0..k.
    const pairs = (g) => (Array.isArray(g.per_call) ? g.per_call : []).filter((c) => c && typeof c === "object" && typeof c.call_id === "string" && c.call_id).map((c) => {
      const k = Math.min(40, Math.max(1, Math.round(num(c.k) || 1))), f = (v) => Math.min(k, Math.max(0, Math.round(num(v) || 0)));
      return { id: c.call_id, k, o: f(c.old_fails), n: f(c.new_fails) };
    });
    const strip = (f, k, l) => `<span class="trial" role="img" data-l="${l}" aria-label="${l} prompt: ${f} of ${k} replays failed">${Array.from({ length: k }, (_, i) => `<i class="${i < f ? "bad" : "good"}"></i>`).join("")}</span>`;
    const calls = (n) => `${n} ${n === 1 ? "call" : "calls"}`;
    const verdict = (g, ps) => {
      const better = ps.filter((p) => p.n < p.o).length, worse = ps.filter((p) => p.n > p.o).length;
      return `<p class="verdict">Old prompt failed ${pct(g.old_fail_rate)} of replays, new prompt ${pct(g.new_fail_rate)}.${ps.length ? ` ${calls(better)} got better, ${worse} got worse.` : ""}</p>`;
    };
    const table = (ps) => !ps.length ? "" :
      `<div class="pairs-h label"><span>Call</span><span>Old prompt</span><span>New prompt</span><span></span></div><ul class="pairs">${ps.map((p) => {
        const [t, w] = p.n < p.o ? ["pass", "better"] : p.n > p.o ? ["fatal", "worse"] : ["review", "same"];
        return `<li><a class="cid" href="/#/call/${esc(encodeURIComponent(p.id))}">${esc(p.id)}</a>${strip(p.o, p.k, "Old")}${strip(p.n, p.k, "New")}${badge(t, w)}</li>`;
      }).join("")}</ul>`;
    const stats = (g, s) => {
      const cost = num(g.cost_usd) != null ? g.cost_usd : s ? s.cost_usd : null, list = (v) => (Array.isArray(v) ? v.map(str).filter(Boolean) : []);
      return `<details class="gate-d"><summary>How this was decided</summary><div class="gate-f"><span>lift <b>${esc(num(g.lift) == null ? "–" : (g.lift > 0 ? "+" : "") + Math.round(g.lift * 100) + " pts")}</b></span><span>p <b>${esc(g.p_value == null ? "–" : g.p_value)}</b> / α <b>${esc(g.alpha)}</b></span><span>${esc(g.n_calls)} calls × ${esc(g.k_seeds)} seeds</span>` +
        (s ? `<span>screened <b>${esc(s.screened)}</b> → kept <b>${esc(s.kept)}</b></span>` : "") + (num(cost) != null ? `<span>cost <b>$${esc(fmt(cost, 2))}</b></span>` : "") +
        (typeof g.holdout === "boolean" ? `<span>hold-out calls <b>${g.holdout ? "yes" : "no"}</b></span>` : "") +
        `<span>guard breaks <b>${esc(list(g.guard_breaks).join(", ") || "none")}</b></span>` + (list(g.guard_flaky).length ? `<span>flaky guards <b>${esc(list(g.guard_flaky).join(", "))}</b></span>` : "") + `</div></details>`;
    };
    return (
      head("Test", "Each patch is replayed on the same calls as the old prompt. It passes only if it clearly does better.") +
      `<p class="gate-sum">${esc(shipped)} of ${esc(d.gates.length)} tests passed.</p>` +
      d.gates.map((g) => { const ps = pairs(g); return `<div class="gate rv"><div class="gate-h"><h3>${esc(name(g.cause_id) || g.cause_id)}</h3>${dchip(g.decision)}<span class="label">attempt ${esc(g.attempt)}</span></div>` +
        verdict(g, ps) + rate("Old", g.old_fail_rate, "fatal") + rate("New", g.new_fail_rate, "pass") + table(ps) +
        `<p class="why">${esc(g.reason)}</p>${stats(g, screen(g.cause_id))}</div>`; }).join("")
    );
  };
  const KINDS = { spec: "Spec questions", irritation: "Irritation", intent: "Intent", quantity: "Quantity" };
  P.review = (d) => {
    const f = S.filt, kinds = Object.keys(d.totals);
    const labs = (it) => (Array.isArray(it.labels) ? it.labels : []).map((l) => (l && typeof l === "object" ? l : {}));
    const right = (it, k) => labs(it).length > 0 && labs(it).every((l) => l[k]);
    const cls = (it) => (right(it, "jev_ok") && right(it, "gemini_ok") ? "both" : right(it, "jev_ok") ? "jev" : right(it, "gemini_ok") ? "gem" : "none");
    const vis = d.items.filter((it) => (f.kind === "all" || it.kind === f.kind) && (f.verdict === "all" || cls(it) === f.verdict) && (!f.fatal || it.decides_fatal));
    const accCard = (k) => {
      const a = d.accuracy[k] || {};
      const part = (lbl, x) => (x ? `<dt>${lbl}<small>${esc(x.n)} labels</small></dt><dd>Jev ${pct(x.jev_right / x.n)}<br><span class="src">Gemini ${pct(x.gemini_right / x.n)}</span></dd>` : "");
      return `<div class="card acc rv"><h3>${esc(own(KINDS, k) || k)}</h3><p class="label">${esc(sum(d.totals[k]))} reviewed</p><dl class="kv" style="margin-top:14px">${part("Control", a.control)}${part("Contested", a.contested)}</dl></div>`;
    };
    const opts = (key, list) => `<div class="seg" role="group" aria-label="${esc(key)}">${list.map(([v, l]) => `<button type="button" data-f="${key}" data-v="${esc(v)}" aria-pressed="${f[key] === v}">${esc(l)}</button>`).join("")}</div>`;
    return (
      head("Judge accuracy", "Where Jev and Gemini disagreed, a reviewer read the call and marked who was right.") +
      `<div class="cards">${kinds.map(accCard).join("") || '<p class="note">Nothing reviewed yet.</p>'}</div>` + sec("Items") +
      `<div class="filters rv">${opts("kind", [["all", "All kinds"]].concat(kinds.map((k) => [k, own(KINDS, k) || k])))}${opts("verdict", [["all", "Any"], ["jev", "Jev right"], ["gem", "Gemini right"], ["both", "Both right"], ["none", "Both wrong"]])}` +
      `<div class="seg"><button type="button" data-fatal="1" aria-pressed="${f.fatal}">Decides Fatal</button></div></div>` +
      `<p class="label rv" style="margin-bottom:6px">${esc(vis.length)} of ${esc(d.items.length)}</p>` +
      `<ul class="items rv">${vis.map((it) => `<li><div class="who"><a href="/#/call/${esc(encodeURIComponent(str(it.call_id)))}">${esc(it.call_id)}</a>${it.idx != null ? ` · turn ${esc(it.idx)}` : ""}<br>${esc(it.reviewer)}${it.control ? ' · control' : ""}${it.decides_fatal ? '<span class="tag">Fatal</span>' : ""}</div><div>` +
        `<div class="lb">${labs(it).map((l) => `<span>${esc(str(l.label).replace(/_/g, " "))}: Jev <span class="${l.jev_ok ? "ok" : "no"}">${esc(l.jev)}</span> · Gemini <span class="${l.gemini_ok ? "ok" : "no"}">${esc(l.gemini)}</span> · truth <b>${esc(l.truth)}</b></span>`).join("")}</div><p>${esc(it.reason)}</p></div></li>`).join("") || '<li class="empty">No items match.</li>'}</ul>`
    );
  };

  function show() {
    const it = ITEMS.find((i) => i.id === S.now) || ITEMS.find((i) => i.id === "home") || ITEMS.find((i) => i.id === "ingest");
    S.now = it.id;
    const main = $("panel"), stub = (body) => head(it.name, "") + body; // a loading or failed page keeps its title and tabs
    let html;
    try {
      if (it.group === "config") html = S.cfg ? P[it.id]() : stub(failed(S.cfgErr || new Error("no config")));
      else if (it.group === "operate") html = P[it.id]();
      else {
        const r = S.stage[it.id];
        html = !r ? stub('<p class="empty">Loading.</p>') : r.e ? stub(failed(r.e)) : P[it.id](r.v);
      }
    } catch (err) {
      html = stub(failed(err));
    }
    main.innerHTML = html;
    nav();
    $("menu-now").textContent = `Menu: ${it.name}`;
    if ($("tb-title")) $("tb-title").textContent = it.name;
  }

  function go(id, push = true) {
    S.now = id;
    if (push && location.hash !== `#/${id}`) history.replaceState(null, "", `#/${id}`);
    closeMenu();
    show();
    if (window.VL && window.VL.enter && window.VL.enter[id]) window.VL.enter[id]();
    $("panel").focus({ preventScroll: true });
    window.scrollTo(0, 0);
  }
  const menuBtn = () => document.querySelector(".menu");
  const inertTargets = () => [$("panel"), document.querySelector(".bar .mark"), document.querySelector(".toolbar")].filter(Boolean);
  const isOpen = () => $("side").classList.contains("open");
  const focusables = () =>
    [menuBtn(), ...$("side").querySelectorAll("a[href], button:not([disabled])")].filter((el) => el && !el.hidden && !(el.closest && el.closest("[hidden]")));
  function setMenu(open, restore) {
    $("side").classList.toggle("open", open);
    menuBtn().setAttribute("aria-expanded", String(open));
    inertTargets().forEach((el) => (el.inert = open));
    if (open) { const f = focusables()[1]; if (f) f.focus(); }
    else if (restore) menuBtn().focus();
  }
  function closeMenu(restore = false) {
    if (isOpen()) setMenu(false, restore);
  }
  // Ask for the audit of one judge. Only the newest ask for the judge now shown may land.
  async function loadAudit() {
    const judge = S.judge, n = (S.auditSeq = (S.auditSeq || 0) + 1);
    const r = await settle(get(`/api/stage/audit${judge ? `?judge=${encodeURIComponent(judge)}` : ""}`));
    if (n !== S.auditSeq || judge !== S.judge) return false;
    S.stage.audit = r;
    return true;
  }

  document.addEventListener("click", async (e) => {
    const t = e.target.closest("button");
    if (!t) return;
    if (t.dataset.go) go(t.dataset.go);
    else if (t.dataset.judge) {
      S.judge = t.dataset.judge;
      if (await loadAudit()) { show(); const b = document.querySelector(`[data-judge="${S.judge}"]`); if (b) b.focus(); }
    } else if (t.dataset.f) { S.filt[t.dataset.f] = t.dataset.v; keepScroll(); }
    else if (t.dataset.fatal) { S.filt.fatal = !S.filt.fatal; keepScroll(); }
    else if (t.dataset.calls) { S.cv = { verdict: t.dataset.calls, n: PAGE }; keepScroll(); refocus(`[data-calls="${S.cv.verdict}"]`); }
    else if (t.dataset.more) { const k = S.cv.n; S.cv.n += PAGE; keepScroll(); refocus(`.calls-list li:nth-child(${k + 1}) .cid`); }
    else if (t.classList.contains("menu")) {
      setMenu(!isOpen(), true);
    }
  });
  function keepScroll() {
    const y = window.scrollY;
    show();
    window.scrollTo(0, y);
  }
  // After a redraw, focus the pressed control (as the judge toggle does) or the first row that "more" added.
  const refocus = (sel) => { const b = document.querySelector(sel); if (b) b.focus({ preventScroll: true }); };
  document.addEventListener("keydown", (e) => {
    if (!isOpen()) return;
    if (e.key === "Escape") { e.preventDefault(); closeMenu(true); return; }
    if (e.key !== "Tab") return;
    const f = focusables(), i = f.indexOf(document.activeElement);
    if (!f.length) return;
    if (e.shiftKey && i <= 0) { e.preventDefault(); f[f.length - 1].focus(); }
    else if (!e.shiftKey && (i === -1 || i === f.length - 1)) { e.preventDefault(); f[0].focus(); }
  });
  matchMedia("(min-width: 861px)").addEventListener("change", (e) => { if (e.matches) closeMenu(); });
  // The toolbar shows the page name once the large title scrolls away, as on Apple platforms.
  window.addEventListener("scroll", () => { const tb = document.querySelector(".toolbar"); if (tb) tb.classList.toggle("scrolled", window.scrollY > 48); }, { passive: true });
  window.addEventListener("hashchange", () => {
    const id = location.hash.replace(/^#\/?/, "");
    if (id === "panel") history.replaceState(null, "", `#/${S.now}`); // the skip link only moves focus
    else if (ITEMS.some((i) => i.id === id)) go(id, false);
  });

  async function boot() {
    const names = ["ingest", "audit", "issues", "fix", "gate", "review"];
    const [cfg, calls, causes, ...rest] = await Promise.all([settle(get("/api/config")), settle(get("/api/calls")), settle(get("/api/causes")), ...names.map((n) => settle(get(`/api/stage/${n}`)))]);
    names.forEach((n, i) => (S.stage[n] = rest[i]));
    S.calls = calls; S.causes = causes;
    if (cfg.v) {
      S.cfg = cfg.v;
      S.avail = leaf(cfg.v.judge.available) || [];
      S.judge = leaf(cfg.v.judge.active);
    } else S.cfgErr = cfg.e;
    const id = location.hash.replace(/^#\/?/, "");
    if (ITEMS.some((i) => i.id === id)) S.now = id;
    show();
    if (window.VL && window.VL.enter && window.VL.enter[S.now]) window.VL.enter[S.now]();
  }
  // Reload the stage data after a job ends. An Operate panel keeps its own state, so only the sidebar is redrawn there.
  async function refresh() {
    const [calls, causes, ...rs] = await Promise.all([settle(get("/api/calls")), settle(get("/api/causes")), ...["ingest", "issues", "fix", "gate", "review"].map((n) => settle(get(`/api/stage/${n}`)))]);
    ["ingest", "issues", "fix", "gate", "review"].forEach((n, i) => (S.stage[n] = rs[i]));
    S.calls = calls; S.causes = causes;
    await loadAudit();
    const it = ITEMS.find((i) => i.id === S.now);
    if (it && it.group === "operate") nav(); else show();
  }
  // The small surface console.js builds on.
  window.VL = { S, P, ITEMS, esc, str, num, fmt, own, tone, chip, badge, head, sec, get, show, go, nav, refresh, NAMES, icon };
  boot();
})();
