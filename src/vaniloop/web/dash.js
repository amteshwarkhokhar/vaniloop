// vaniloop dashboard: a step sidebar, tabs on each step and one panel per page. Every data string goes through esc().
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

  // side: where the sidebar lists the page (step, needs, tools) or "tab" for a tab on its step. group keeps show() and redraw() as they were.
  const ITEMS = [
    { id: "ingest", group: "stage", name: "Ingest", side: "step", step: 1, tab: "Overview" }, { id: "data", group: "config", name: "Data files", side: "tab", step: 1, tab: "Data files" },
    { id: "audit", group: "stage", name: "Audit", side: "step", step: 2, tab: "Results" }, { id: "judge", group: "config", name: "Judges", side: "tab", step: 2, tab: "Judges" },
    { id: "thresholds", group: "config", name: "All constants", side: "tab", step: 2, tab: "All constants" }, { id: "review", group: "stage", name: "Judge accuracy", side: "tab", step: 2, tab: "Judge accuracy" },
    { id: "issues", group: "stage", name: "Issues", side: "step", step: 3 }, { id: "fix", group: "stage", name: "Fix", side: "step", step: 4 },
    { id: "gate", group: "stage", name: "Test", side: "step", step: 5, tab: "Results" }, { id: "fixloop", group: "config", name: "Test settings", side: "tab", step: 5, tab: "Test settings" },
  ];
  const TABS = { 1: ["ingest", "submit", "data"], 2: ["audit", "rubric", "tune", "judge", "thresholds", "review"], 5: ["gate", "fixloop"] };
  const S = { cfg: null, stage: {}, judge: null, avail: [], now: "home", filt: { kind: "all", verdict: "all", fatal: false } };

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
      const step = it.side === "step", on = it.id === S.now ? "page" : step && it.step === cur.step ? "step" : "";
      const vid = it.id === "home" ? ' id="nav-val-home"' : "";
      return `<button class="item" type="button" data-go="${it.id}"${on ? ` aria-current="${on}"` : ""}>${step ? `<span class="num">${it.step}</span>` : ""}<span class="nm">${esc(it.name)}</span><span class="val"${vid}>${esc(step && !h[it.id] ? "no data yet" : h[it.id])}</span></button>`;
    };
    const of = (side) => ITEMS.filter((i) => i.side === side).map(row).join("");
    $("nav").innerHTML = `${of("loop")}<p class="grp">Steps</p>${of("step")}<p class="grp">Needs you</p>${of("needs")}<p class="grp">Tools</p>${of("tools")}<a class="item" href="/#/"><span class="nm">All calls</span><span aria-hidden="true">→</span></a>`;
  }

  function flat(o, path = []) {
    if (!o || typeof o !== "object") return [];
    if ("value" in o && "source" in o) return [{ key: path.join(" › ").replace(/_/g, " "), value: o.value, source: o.source }];
    return Object.entries(o).flatMap(([k, v]) => flat(v, [...path, k]));
  }
  const bn = (n, label, t = "", dec = 0, unit = "") =>
    `<div class="bn ${t ? `t-${t}` : ""}"><span class="n">${fmt(n, dec)}${unit ? `<small>${esc(unit)}</small>` : ""}</span><p class="label">${esc(label)}</p></div>`;
  // The page head: "Step n of 5", the title, one sentence, the tabs of its step, then the stage bar. The title is plain text.
  const head = (title, lede) => {
    const it = ITEMS.find((i) => i.id === S.now) || {}, tabs = (TABS[it.step] || []).map((id) => ITEMS.find((i) => i.id === id)).filter(Boolean);
    const step = ITEMS.find((i) => i.side === "step" && i.step === it.step) || it;
    return (it.step ? `<p class="kicker">Step ${esc(it.step)} of 5</p>` : "") + `<h1 class="ptitle" tabindex="-1">${esc(title)}</h1>${lede ? `<p class="lede">${esc(lede)}</p>` : ""}` +
      (tabs.length > 1 ? `<nav class="tabs" aria-label="${esc(step.name)} pages">${tabs.map((t) => `<a href="#/${t.id}"${t.id === it.id ? ' aria-current="page"' : ""}>${esc(t.tab)}</a>`).join("")}</nav>` : "") +
      (window.VL && window.VL.stageBar ? window.VL.stageBar(it.id) : "");
  };
  const sec = (t) => `<h2 class="sec">${esc(t)}</h2>`;
  const failed = (e) => `<p class="empty">This panel could not load (${esc(e.message)}).</p>`;

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
        .join("")}</tbody></table></div>` +
      sec("Flags") + `<div class="chips rv">${Object.entries(d.flags).map(([k, n]) => `<span class="pill">${esc(k.replace(/_/g, " "))}<b>${esc(n)}</b></span>`).join("") || '<span class="empty">None.</span>'}</div>`
    );
  };
  P.issues = (d) => {
    const fa = (c) => num(c.fatal) || 0, nf = (c) => num(c.nonfatal) || 0, max = Math.max(1, ...d.causes.map((c) => fa(c) + nf(c)));
    return (
      head("Issues", "Faults grouped by their cause. The cause behind the most fatal calls comes first.") +
      `<ol class="ranked rv">${d.causes.map((c) => `<li><div><h3>${esc(c.name)}</h3><p class="meta"><span>${esc(c.cause_id)}</span><span>${esc(own(NAMES, c.parameter) || c.parameter)}</span><span>${esc(str(c.type).replace(/_/g, " "))}</span><span>${esc(str(c.status).replace(/_/g, " "))}</span><span>owner ${esc(c.owner || "unassigned")}</span></p></div>` +
        `<div><div class="pair"><span class="a" style="width:${(100 * fa(c)) / max}%"></span><span class="b" style="width:${(100 * nf(c)) / max}%"></span></div><p class="pc"><b>${esc(c.fatal)}</b> fatal · <b>${esc(c.nonfatal)}</b> non-fatal</p></div></li>`).join("")}</ol>`
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
    return (
      head("Test", "Each patch is replayed on the same calls as the old prompt. It passes only if it clearly does better.") +
      (shipped ? "" : `<p class="note"><b>Nothing has passed.</b> No patch has cleared its test yet.</p>`) +
      d.gates.map((g) => { const s = screen(g.cause_id); return `<div class="gate rv"><div class="gate-h"><h3>${esc(g.cause_id)}</h3>${dchip(g.decision)}<span class="label">attempt ${esc(g.attempt)}</span></div><p class="label" style="margin:-10px 0 18px">${esc(name(g.cause_id))}</p>` +
        rate("Old", g.old_fail_rate, "fatal") + rate("New", g.new_fail_rate, "pass") +
        `<div class="gate-f"><span>lift <b>${esc(num(g.lift) == null ? "–" : (g.lift > 0 ? "+" : "") + Math.round(g.lift * 100) + " pts")}</b></span><span>p <b>${esc(g.p_value == null ? "–" : g.p_value)}</b> / α <b>${esc(g.alpha)}</b></span><span>${esc(g.n_calls)} calls × ${esc(g.k_seeds)} seeds</span>` +
        (s ? `<span>screened <b>${esc(s.screened)}</b> → kept <b>${esc(s.kept)}</b></span><span>cost <b>$${esc(fmt(s.cost_usd, 2))}</b></span>` : "") + `</div><p class="why">${esc(g.reason)}</p></div>`; }).join("")
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
    const it = ITEMS.find((i) => i.id === S.now) || ITEMS[0];
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
  const inertTargets = () => [$("panel"), document.querySelector(".bar .mark")].filter(Boolean);
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
    else if (t.classList.contains("menu")) {
      setMenu(!isOpen(), true);
    }
  });
  function keepScroll() {
    const y = window.scrollY;
    show();
    window.scrollTo(0, y);
  }
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
  window.addEventListener("hashchange", () => {
    const id = location.hash.replace(/^#\/?/, "");
    if (id === "panel") history.replaceState(null, "", `#/${S.now}`); // the skip link only moves focus
    else if (ITEMS.some((i) => i.id === id)) go(id, false);
  });

  async function boot() {
    const names = ["ingest", "audit", "issues", "fix", "gate", "review"];
    const [cfg, ...rest] = await Promise.all([settle(get("/api/config")), ...names.map((n) => settle(get(`/api/stage/${n}`)))]);
    names.forEach((n, i) => (S.stage[n] = rest[i]));
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
    const rs = await Promise.all(["ingest", "issues", "fix", "gate", "review"].map((n) => settle(get(`/api/stage/${n}`))));
    ["ingest", "issues", "fix", "gate", "review"].forEach((n, i) => (S.stage[n] = rs[i]));
    await loadAudit();
    const it = ITEMS.find((i) => i.id === S.now);
    if (it && it.group === "operate") nav(); else show();
  }
  // The small surface console.js builds on.
  window.VL = { S, P, ITEMS, esc, str, num, fmt, own, tone, chip, badge, head, sec, get, show, go, nav, refresh, NAMES, heads };
  boot();
})();
