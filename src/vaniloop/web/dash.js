// vaniloop dashboard: a pipeline sidebar and one panel per item. Every data string goes through esc().
(() => {
  "use strict";
  const reduced = matchMedia("(prefers-reduced-motion: reduce)");
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
  const chip = (v) => `<span class="chip t-${esc(tone(v))}">${esc(word(v))}</span>`;
  const DECISION = { ship: ["Shipped", "pass"], reject: ["Rejected", "fatal"], inconclusive: ["Inconclusive", "review"] };
  const dchip = (d) => { const [w, t] = own(DECISION, d) || [str(d), "review"]; return `<span class="chip t-${t}">${esc(w)}</span>`; };

  const ITEMS = [
    { id: "judge", group: "config", name: "Judge" },
    { id: "thresholds", group: "config", name: "Thresholds" },
    { id: "fixloop", group: "config", name: "Fix loop" },
    { id: "data", group: "config", name: "Data" },
    { id: "ingest", group: "stage", name: "Ingest" },
    { id: "audit", group: "stage", name: "Audit" },
    { id: "issues", group: "stage", name: "Issues" },
    { id: "fix", group: "stage", name: "Fix" },
    { id: "gate", group: "stage", name: "Gate" },
    { id: "review", group: "stage", name: "Jev review" },
  ];
  const S = { cfg: null, stage: {}, judge: null, avail: [], now: "ingest", filt: { kind: "all", verdict: "all", fatal: false } };

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
    const ing = g("ingest"), au = g("audit"), is = g("issues"), fx = g("fix"), ga = g("gate"), rv = g("review");
    const shipped = ga ? ga.gates.filter((x) => x.decision === "ship").length : 0;
    const cfg = S.cfg;
    return {
      judge: cfg ? S.judge : null,
      thresholds: cfg ? `${flat(cfg.audit).length} set` : null,
      fixloop: cfg ? `α ${leaf(cfg.fix_loop.alpha_per_attempt)}` : null,
      data: cfg ? `${cfg.data.files.filter((f) => f.rows != null).length}/${cfg.data.files.length} files` : null,
      ingest: ing && `${fmt(ing.calls)} calls`,
      audit: au && `${fmt(au.verdicts.Fatal || 0)} Fatal`,
      issues: is && `${fmt(is.causes.length)} causes`,
      fix: fx && `${fmt(fx.patches.length)} patches`,
      gate: ga && `${fmt(ga.gates.length)} gates · ${fmt(shipped)} shipped`,
      review: rv && `${fmt(Object.values(rv.totals).reduce((a, t) => a + sum(t), 0))} reviewed`,
    };
  }

  function nav() {
    const h = heads();
    const one = (it) => {
      const cur = it.id === S.now ? ' aria-current="page"' : "";
      const hd = h[it.id];
      if (it.group !== "stage")
        return `<button class="item" type="button" data-go="${it.id}"${cur}><span>${esc(it.name)}</span><span class="val">${esc(hd)}</span></button>`;
      return `<button class="node" type="button" data-go="${it.id}"${cur}><span class="nm">${esc(it.name)}</span><span class="hd${hd ? "" : " none"}">${esc(hd || "no data yet")}</span></button>`;
    };
    $("nav").innerHTML =
      `<p class="label grp">Configure</p>${ITEMS.filter((i) => i.group === "config").map(one).join("")}` +
      `<p class="label grp">Pipeline</p><div class="rail">${ITEMS.filter((i) => i.group === "stage").map(one).join("")}</div>` +
      (ITEMS.some((i) => i.group === "operate") ? `<p class="label grp">Operate</p>${ITEMS.filter((i) => i.group === "operate").map(one).join("")}` : "");
  }

  function judgeToggle() {
    $("judge-wrap").hidden = S.avail.length < 2;
    $("judge").innerHTML = S.avail
      .map((j) => `<button type="button" data-judge="${esc(j)}" aria-pressed="${j === S.judge}">${esc(j)}</button>`)
      .join("");
  }

  function flat(o, path = []) {
    if (!o || typeof o !== "object") return [];
    if ("value" in o && "source" in o) return [{ key: path.join(" › ").replace(/_/g, " "), value: o.value, source: o.source }];
    return Object.entries(o).flatMap(([k, v]) => flat(v, [...path, k]));
  }
  const bn = (n, label, t = "", dec = 0, unit = "") =>
    `<div class="bn ${t ? `t-${t}` : ""}"><span class="n">${num(n) == null ? "–" : `<span data-count="${n}" data-dec="${dec}">${fmt(n, dec)}</span>`}${unit ? `<small>${esc(unit)}</small>` : ""}</span><p class="label">${esc(label)}</p></div>`;
  const head = (eyebrow, title, lede) =>
    `<p class="label eyebrow rv">${esc(eyebrow)}</p><h1 class="ptitle rv" tabindex="-1">${title}</h1>${lede ? `<p class="lede rv">${esc(lede)}</p>` : ""}`;
  const sec = (t) => `<h2 class="sec rv">${esc(t)}</h2>`;
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
      head("Configure", "Who <em>judges</em>", "Two models share the audit. One reads the transcript, the other makes the judgement calls.") +
      `<div class="roles rv"><div class="card role"><p class="label">Structure</p><p class="big">${esc(leaf(j.structure_model))}</p><p class="lede">Gemini reads the call and lays out its turns, specs and quantities.</p></div>` +
      `<div class="card role"><p class="label">Judgement</p><p class="big">${esc(leaf(j.jev_model))}</p><p class="lede">Jev, version <span class="mono">${esc(leaf(j.jev_version))}</span>, labels each judgement as true, false or unsure.</p></div></div>` +
      sec("Probability bands") +
      `<p class="lede rv">Each label has its own band, tuned on the reviewed calls. Below the low mark Jev says false. Above the high mark it says true. Between, the label stays unsure and the call goes to review.</p>` +
      bands +
      (drop !== null ? `<p class="lede rv">A spec turn's question label is dropped to unsure when Jev's check that the turn asks that spec is at or below <span class="mono">${esc(drop)}</span>.</p>` : "") +
      sec("Active") +
      `<p class="lede rv">Showing <b class="mono">${esc(S.judge)}</b> numbers. <span class="mono">${esc(S.cfg.judge.active.source)}</span></p>`
    );
  };
  const rows = (items) =>
    `<div class="tw rv"><table><thead><tr><th>Setting</th><th class="m">Value</th><th>Source</th></tr></thead><tbody>${items
      .map((r) => `<tr><td>${esc(r.key)}</td><td class="m">${esc(r.value)}</td><td class="src">${esc(r.source)}</td></tr>`)
      .join("")}</tbody></table></div>`;
  P.thresholds = () => head("Configure", "Audit <em>thresholds</em>", "Each constant the Python rules grade with, and where it lives.") + rows(flat(S.cfg.audit));
  P.fixloop = () => {
    const fl = S.cfg.fix_loop, last = flat(fl.last_run);
    return (
      head("Configure", "The <em>fix loop</em>", "How many tries a cause gets, and the bar a patch must clear.") +
      rows(flat({ ...fl, last_run: undefined })) + sec("Last run") +
      (last.length ? rows(last.map((r) => ({ ...r, key: r.key.replace(/^last run › /, "") }))) : `<p class="note rv">No gate has run yet.</p>`)
    );
  };
  P.data = () => {
    const d = S.cfg.data;
    return (
      head("Configure", "The <em>data</em>", `Folder ${leaf(d.folder)}. Times in IST.`) +
      `<div class="tw rv"><table><thead><tr><th>File</th><th class="r">Rows</th><th class="r">Updated</th></tr></thead><tbody>${d.files
        .map((f) => `<tr><td class="m">${esc(f.name)}</td><td class="m r">${f.rows == null ? '<span class="src">not made yet</span>' : esc(fmt(f.rows))}</td><td class="m r">${esc(f.updated_ist || "–")}</td></tr>`)
        .join("")}</tbody></table></div>`
    );
  };
  P.ingest = (d) =>
    head("Stage 1 · Ingest", "Calls, <em>read in</em>", "Transcripts and audio turned into call records.") +
    `<div class="big-nums rv">${bn(d.calls, "Calls")}${bn(d.with_audio, "With audio")}${bn(d.minutes_total, "Minutes", "", 1)}${bn(d.median_sec, "Median", "", 1, "s")}${bn(d.p90_sec, "90th percentile", "", 1, "s")}${bn(d.short_calls, "Short calls")}</div>`;
  P.audit = (d) => {
    const order = ["Fatal", "NonFatal", "NeedsReview", "Pass"], v = d.verdicts, tot = d.total || 1;
    const checks = Object.entries(d.by_parameter);
    return (
      head("Stage 2 · Audit", `<em>${esc(d.judge)}</em> judged ${esc(fmt(d.total))} calls`, "Python rules grade. The model only labels.") +
      `<div class="vbar rv" role="img" aria-label="Verdicts">${order.filter((k) => v[k]).map((k) => `<span class="t-${tone(k)}" style="width:${(100 * v[k]) / tot}%"></span>`).join("")}</div>` +
      `<div class="legend rv">${order.map((k) => `<span class="chip t-${tone(k)}">${esc(word(k))} <b>${esc(v[k] || 0)}</b></span>`).join("")}</div>` +
      `<div class="big-nums rv" style="margin-top:36px">${order.map((k) => bn(v[k] || 0, word(k), tone(k))).join("")}</div>` +
      sec("Scope") + `<div class="chips rv">${Object.entries(d.scopes).map(([k, n]) => `<span class="pill">${esc(k === "bl_collection" ? "lead call" : k.replace(/_/g, " "))}<b>${esc(n)}</b></span>`).join("")}</div>` +
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
      head("Stage 3 · Issues", "Causes, <em>ranked</em>", "Faults grouped by why they happen.") +
      `<ol class="ranked rv">${d.causes.map((c) => `<li><div><h3>${esc(c.name)}</h3><p class="meta"><span>${esc(c.cause_id)}</span><span>${esc(own(NAMES, c.parameter) || c.parameter)}</span><span>${esc(str(c.type).replace(/_/g, " "))}</span><span>${esc(str(c.status).replace(/_/g, " "))}</span><span>owner ${esc(c.owner || "unassigned")}</span></p></div>` +
        `<div><div class="pair"><span class="a" style="width:${(100 * fa(c)) / max}%"></span><span class="b" style="width:${(100 * nf(c)) / max}%"></span></div><p class="pc"><b>${esc(c.fatal)}</b> fatal · <b>${esc(c.nonfatal)}</b> non-fatal</p></div></li>`).join("")}</ol>`
    );
  };
  const diff = (p, extra = "") =>
    `<div class="card rv"><h3>${esc(p.cause_id)} <span class="label">attempt ${esc(p.attempt)}</span> ${p.gate_decision ? dchip(p.gate_decision) : ""}</h3><div class="diff"><div class="rm"><span class="label">Removed</span>${esc(p.old_text)}</div><div class="ad"><span class="label">Added</span>${esc(p.new_text)}</div></div><p class="lede">${esc(p.rationale)}</p>${extra}</div>`;
  P.fix = (d) =>
    head("Stage 4 · Fix", "Prompt <em>patches</em>", "Old text against new text, with the reason.") +
    (d.patches.length ? d.patches.map((p) => diff(p)).join("") : `<p class="note rv"><b>No patches yet.</b> Prompt-side causes get a patch once the fix stage runs.</p>`) +
    (d.tickets.length ? sec("Evidence tickets") + d.tickets.map((t) => (t && typeof t === "object" ? t : { cause_id: t })).map((t) => `<div class="card rv"><h3>${esc(t.cause_id)}</h3><p class="lede">${esc(t.summary || t.name || "")}</p></div>`).join("") : "") +
    (d.history.length ? sec("Earlier attempts") + d.history.map((p) => diff(p)).join("") : "");
  P.gate = (d) => {
    const name = (id) => { const c = S.stage.issues && S.stage.issues.v && S.stage.issues.v.causes.find((x) => x.cause_id === id); return c ? c.name : ""; };
    const screen = (id) => d.screens.find((s) => s.cause_id === id);
    const shipped = d.gates.filter((g) => g.decision === "ship").length;
    const rate = (l, v, t) => `<div class="rate"><span>${l}</span><span class="tr"><i class="t-${t}" style="width:${Math.max(1, (v || 0) * 100)}%"></i></span><b>${pct(v)}</b></div>`;
    return (
      head("Stage 5 · Gate", "Did the patch <em>earn</em> it?", "Old prompt against new, replayed on the same calls.") +
      (shipped ? "" : `<p class="note rv"><b>Nothing shipped.</b> No patch has cleared the gate yet.</p>`) +
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
      head("Stage 6 · Jev review", "Who got it <em>right</em>?", "Fable read each contested label against the call. Control labels are ones both judges agreed on.") +
      `<div class="cards">${kinds.map(accCard).join("") || '<p class="note">Nothing reviewed yet.</p>'}</div>` + sec("Items") +
      `<div class="filters rv">${opts("kind", [["all", "All kinds"]].concat(kinds.map((k) => [k, own(KINDS, k) || k])))}${opts("verdict", [["all", "Any"], ["jev", "Jev right"], ["gem", "Gemini right"], ["both", "Both right"], ["none", "Both wrong"]])}` +
      `<div class="seg"><button type="button" data-fatal="1" aria-pressed="${f.fatal}">Decides Fatal</button></div></div>` +
      `<p class="label rv" style="margin-bottom:6px">${esc(vis.length)} of ${esc(d.items.length)}</p>` +
      `<ul class="items rv">${vis.map((it) => `<li><div class="who"><a href="/#/call/${esc(encodeURIComponent(str(it.call_id)))}">${esc(it.call_id)}</a>${it.idx != null ? ` · turn ${esc(it.idx)}` : ""}<br>${esc(it.reviewer)}${it.control ? ' · control' : ""}${it.decides_fatal ? '<span class="tag">Fatal</span>' : ""}</div><div>` +
        `<div class="lb">${labs(it).map((l) => `<span>${esc(str(l.label).replace(/_/g, " "))}: Jev <span class="${l.jev_ok ? "ok" : "no"}">${esc(l.jev)}</span> · Gemini <span class="${l.gemini_ok ? "ok" : "no"}">${esc(l.gemini)}</span> · truth <b>${esc(l.truth)}</b></span>`).join("")}</div><p>${esc(it.reason)}</p></div></li>`).join("") || '<li class="empty">No items match.</li>'}</ul>`
    );
  };

  function countUp(root) {
    root.querySelectorAll("[data-count]").forEach((el) => {
      const to = Number(el.dataset.count), dec = Number(el.dataset.dec) || 0;
      if (reduced.matches || !isFinite(to)) return;
      const t0 = performance.now();
      const tick = (t) => {
        const p = Math.min(1, (t - t0) / 900), e = 1 - Math.pow(1 - p, 3);
        el.textContent = fmt(to * e, dec);
        if (p < 1) requestAnimationFrame(tick);
        else el.textContent = fmt(to, dec);
      };
      requestAnimationFrame(tick);
    });
  }
  function stagger(root) {
    [...root.querySelectorAll(".rv")].forEach((el, i) => el.style.setProperty("--i", Math.min(i, 14)));
  }

  function show() {
    const it = ITEMS.find((i) => i.id === S.now) || ITEMS[4];
    S.now = it.id;
    const main = $("panel");
    let html;
    try {
      if (it.group === "config") html = S.cfg ? P[it.id]() : failed(S.cfgErr || new Error("no config"));
      else if (it.group === "operate") html = P[it.id]();
      else {
        const r = S.stage[it.id];
        html = !r ? '<p class="empty">Loading.</p>' : r.e ? failed(r.e) : P[it.id](r.v);
        if (window.VL && window.VL.stageBar) html = window.VL.stageBar(it.id) + html;
      }
    } catch (err) {
      html = failed(err);
    }
    main.innerHTML = html;
    stagger(main);
    countUp(main);
    nav();
    $("menu-now").textContent = it.name;
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
      judgeToggle();
      if (await loadAudit()) show();
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
    if (ITEMS.some((i) => i.id === id)) go(id, false);
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
    judgeToggle();
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
  window.VL = { S, P, ITEMS, esc, str, num, fmt, own, tone, chip, head, sec, get, show, go, nav, refresh, NAMES };
  boot();
})();
