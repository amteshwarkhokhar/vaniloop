// Runs src/vaniloop/web/dash.js against a tiny DOM stub and prints JSON. Usage: node dash_harness.js <scenario>
const fs = require("fs"), path = require("path"), vm = require("vm");
const web = (f) => fs.readFileSync(path.join(__dirname, "..", "src", "vaniloop", "web", f), "utf8");
const src = web("dash.js"), consoleSrc = web("console.js");

function el(id) {
  const cls = new Set();
  const e = {
    id, innerHTML: "", textContent: "", hidden: false, inert: false, attrs: {}, dataset: {}, style: { setProperty() {} }, kids: [],
    classList: { add: (c) => cls.add(c), remove: (c) => cls.delete(c), contains: (c) => cls.has(c), toggle(c, on) { const v = on === undefined ? !cls.has(c) : on; v ? cls.add(c) : cls.delete(c); return v; } },
    setAttribute(k, v) { e.attrs[k] = v; }, focus() { doc.activeElement = e; }, closest: () => null, querySelectorAll: () => e.kids,
  };
  return e;
}
const ids = {}, doc = { activeElement: null, listeners: {} };
for (const id of ["panel", "side", "nav", "judge", "judge-wrap", "menu-now", "dock", "dock-info", "dock-actions", "dock-log"]) ids[id] = el(id);
const menu = el("menu"), mark = el("mark"), a1 = el("a1"), a2 = el("a2");
ids.side.kids = [a1, a2];
doc.getElementById = (id) => ids[id];
doc.querySelector = (q) => (q === ".menu" ? menu : q === ".bar .mark" ? mark : null);
doc.addEventListener = (t, f) => (doc.listeners[t] = (doc.listeners[t] || []).concat(f));

const cfg = {
  judge: { available: { value: ["jev", "gemini"] }, active: { value: "jev", source: "t" }, structure_model: { value: "m" }, jev_model: { value: "j" }, jev_version: { value: "1" }, thresholds: { question_fits: { true_at: { value: 0.55 }, false_at: { value: 0.2 } }, product_match: { true_at: { value: 0.65 }, false_at: { value: 0.35 } }, name_check_drop_at: { value: 0.35 } } },
  audit: {}, fix_loop: { alpha_per_attempt: { value: 0.05 }, last_run: {} }, data: { files: [], folder: { value: "d" } },
};
const audit = (judge, total) => ({ judge, total, verdicts: {}, scopes: {}, by_parameter: {}, flags: {} });
const stages = {
  ingest: { calls: 1 }, audit: audit("jev", 3),
  issues: { causes: [{ cause_id: "C-01", name: "Cause one", parameter: "qty_probing", type: "prompt", status: { value: "open" }, fatal: 2, nonfatal: 1 }] },
  fix: { patches: [], tickets: ["C-07"], history: [] }, gate: { gates: [], screens: [] },
  review: { totals: { spec: { a: 1 } }, accuracy: {}, items: [
    { kind: "spec", call_id: { toString: "x" }, reviewer: "r", reason: "no labels here" },
    { kind: "spec", call_id: "call 1", reviewer: "r", reason: "ok", labels: [null, { label: "a", jev: "t", gemini: "f", truth: "t", jev_ok: true }] },
  ] },
};
const pending = {}, calls = [], timers = [], routes = {};
const consoleOn = (process.argv[2] || "").startsWith("con-");
const sandbox = {
  btoa: (b) => Buffer.from(b, "binary").toString("base64"), setTimeout: (f, ms) => timers.push({ f, ms }), clearTimeout: (i) => { timers[i - 1] = null; },
  document: doc, console, history: { replaceState() {} },
  location: { hash: "" }, performance: { now: () => 0 }, requestAnimationFrame() {},
  matchMedia: () => ({ matches: false, addEventListener() {} }),
  fetch(p, opts = {}) {
    const body = (v, status = 200) => ({ ok: status < 400, status, json: async () => v });
    const method = opts.method || "GET";
    if (consoleOn) {
      calls.push({ method, p, body: opts.body ? JSON.parse(opts.body) : null, headers: opts.headers || {} });
      let r = routes[`${method} ${p}`];
      if (r && Array.isArray(r[0])) r = r.length > 1 ? r.shift() : r[0];
      if (r) return Promise.resolve(body(r[1], r[0]));
    }
    if (p === "/api/config") return Promise.resolve(body(cfg));
    if (!consoleOn && p.includes("?judge=")) return new Promise((res) => (pending[p] = () => res(body(audit(p.split("=")[1], p.endsWith("gemini") ? 7 : 3)))));
    return Promise.resolve(body(stages[p.split("?")[0].split("/").pop()]));
  },
};
sandbox.window = { addEventListener() {}, scrollTo() {}, scrollY: 0 };
const tick = () => new Promise((r) => setTimeout(r, 5));
const fire = async () => { const t = timers.splice(0).filter(Boolean); t.forEach((x) => x.f()); await tick(); };
const emit = async (type, target) => { (doc.listeners[type] || []).forEach((f) => f({ target, preventDefault() {} })); await tick(); };
const click = async (dataset) => { (doc.listeners.click || []).forEach((f) => f({ target: { closest: () => ({ dataset, classList: { contains: (c) => c === "menu" && !!dataset.menu }, setAttribute() {} }) } })); await tick(); };
const key = (k, shiftKey = false) => { let pd = false; doc.listeners.keydown.forEach((f) => f({ key: k, shiftKey, preventDefault() { pd = true; } })); return pd; };

const job = (id, stage, status, extra = {}) => ({ id, stage, args: {}, status, started_ist: "10:00:00 10 Oct", ended_ist: status === "running" ? null : "10:00:05 10 Oct", exit_code: status === "running" ? null : 0, cost_usd: 0.12, log_lines: 1, ...extra });
const posts = () => calls.filter((c) => c.method === "POST" && c.p === "/api/jobs").map((c) => c.body);
const logRoute = (id, from, st, lines, next) => (routes[`GET /api/jobs/${id}/log?from=${from}`] = [200, { job: job(id, "audit", st), lines, next }]);
const SC = {
  async "con-run"() {
    const out = {};
    await click({ go: "run" });
    routes["POST /api/jobs"] = [[400, { error: "confirm", estimate_usd: 1.5 }], [202, { job: job("j1", "audit", "running") }]];
    logRoute("j1", 0, "running", ["line one <img src=x onerror=alert(1)>"], 1);
    logRoute("j1", 1, "done", ["finished"], 2);
    await click({ op: "run", stage: "audit" });
    out.confirm = ids.panel.innerHTML;
    await click({ op: "confirm", stage: "audit" });
    out.posts = posts();
    out.info1 = ids["dock-info"].innerHTML; out.hidden = ids.dock.hidden;
    await click({ op: "dock-toggle" });
    out.log1 = ids["dock-log"].innerHTML;
    await fire();
    out.info2 = ids["dock-info"].innerHTML; out.log2 = ids["dock-log"].innerHTML; out.actions = ids["dock-actions"].innerHTML;
    out.refetched = calls.some((c) => c.p === "/api/stage/issues");
    return out;
  },
  async "con-errors"() {
    const out = {};
    await click({ go: "run" });
    routes["POST /api/jobs"] = [409, { error: "busy", job_id: "j9" }];
    await click({ op: "run", stage: "judge" });
    out.busy = ids.panel.innerHTML;
    routes["POST /api/jobs"] = [400, { error: "limit too big", field: "limit" }];
    await click({ op: "run", stage: "audit" });
    out.bad = ids.panel.innerHTML;
    return out;
  },
  async "con-submit"() {
    const out = {}, row = (id, source, verdict, findings) => ({ call_id: id, source, verdict, scope: "bl_collection", findings, flags: [], error: null });
    routes["GET /api/calls"] = [200, [{ call_id: "c1" }, { call_id: "c2" }]];
    routes["POST /api/submit"] = [201, { submission_id: "s1", files: [{ name: "a.mp3", kind: "mp3" }], call_ids: ["c1"] }];
    routes["POST /api/jobs"] = [202, { job: job("j2", "submit", "running") }];
    logRoute("j2", 0, "running", ["working"], 1);
    routes["GET /api/submit/s1"] = [200, { submission_id: "s1", status: "running", calls: [
      row("c1", "existing", "Fatal", [{ parameter: "qty_probing", status: "Fatal", reason: "asked twice <img src=x onerror=1>", onset_turn: 3 }]), row("up1", "upload", "Pass", [])] }];
    await click({ go: "submit" });
    out.datalist = ids.panel.innerHTML;
    await emit("change", { dataset: { op: "files" }, files: [{ name: "a.mp3", size: 3, arrayBuffer: async () => new Uint8Array([1, 2, 3]).buffer }] });
    await emit("input", { dataset: { op: "id-draft" }, value: "c1" });
    await click({ op: "add-id" });
    await click({ op: "send" });
    out.submit = calls.find((c) => c.p === "/api/submit" && c.method === "POST").body;
    out.jobs = posts();
    out.panel = ids.panel.innerHTML;
    return out;
  },
  async "con-readonly"() {
    const out = {};
    for (const g of ["run", "submit", "audit"]) { await click({ go: g }); out[g] = ids.panel.innerHTML; }
    return out;
  },
  async "con-backoff"() {
    const out = { delays: [] };
    await click({ go: "run" });
    routes["POST /api/jobs"] = [202, { job: job("j1", "audit", "running") }];
    const bad = [503, { error: "down" }];
    routes["GET /api/jobs/j1/log?from=0"] = [bad, bad, bad, bad, bad, bad, [200, { job: job("j1", "audit", "done"), lines: ["ok"], next: 1 }]];
    await click({ op: "run", stage: "judge" });
    for (let i = 0; i < 7; i++) { out.delays.push(timers.filter(Boolean).map((t) => t.ms)); await fire(); }
    out.info = ids["dock-info"].innerHTML;
    return out;
  },
  async "con-cancel"() {
    await click({ go: "run" });
    routes["POST /api/jobs"] = [202, { job: job("j1", "audit", "running") }];
    routes["GET /api/jobs/j1/log?from=0"] = [200, { job: job("j1", "audit", "running"), lines: [], next: 0 }];
    routes["POST /api/jobs/j1/cancel"] = [200, { job: job("j1", "audit", "cancelled") }];
    await click({ op: "run", stage: "judge" });
    await click({ op: "cancel" });
    return calls.filter((c) => c.p.endsWith("/cancel"));
  },
  async "con-chain"() {
    const out = {};
    await click({ go: "audit" });
    out.bar = ids.panel.innerHTML;
    await click({ op: "prefill", stage: "audit", chain: "judge,regrade" });
    out.run = ids.panel.innerHTML;
    routes["POST /api/jobs"] = [[400, { error: "confirm", estimate_usd: null }], [202, { job: job("j1", "audit", "running") }], [202, { job: job("j2", "judge", "running") }], [202, { job: job("j3", "regrade", "running") }]];
    for (const id of ["j1", "j2", "j3"]) logRoute(id, 0, "done", [], 0);
    await click({ op: "run", stage: "audit" });
    out.estimate = ids.panel.innerHTML;
    await click({ op: "confirm", stage: "audit" });
    for (let i = 0; i < 4; i++) await fire();
    out.posts = posts();
    return out;
  },
};
const CON = { read_only: false, data_dir_name: "runs", busy_job_id: null, stages: [
  { id: "audit", label: "Audit", paid: true, args: [{ name: "calls", type: "call_ids", required: false, default: null }, { name: "limit", type: "int", required: false, default: null }] },
  { id: "judge", label: "Judge", paid: false, args: [{ name: "calls", type: "call_ids", required: false, default: null }] },
  { id: "regrade", label: "Regrade", paid: false, args: [] }, { id: "issues", label: "Issues", paid: false, args: [] },
  { id: "fix", label: "Fix", paid: true, args: [{ name: "top", type: "int", required: false, default: 3 }, { name: "holdout", type: "bool", required: false, default: true }] },
  { id: "prove", label: "Prove", paid: true, args: [{ name: "cause", type: "str", required: true, default: null }] },
  { id: "submit", label: "Submit", paid: true, args: [{ name: "submission_id", type: "str", required: true, default: null }] }] };

(async () => {
  const scenario = process.argv[2];
  sandbox.location.hash = scenario === "race" ? "#/audit" : "";
  const vctx = vm.createContext(sandbox);
  vm.runInContext(src, vctx);
  if (consoleOn) {
    routes["GET /api/console"] = [200, { ...CON, read_only: scenario === "con-readonly" }];
    routes["GET /api/jobs"] = [200, { jobs: [] }];
    vm.runInContext(consoleSrc, vctx);
    await tick();
    return console.log(JSON.stringify(await SC[scenario]()));
  }
  await tick();
  const out = {};
  if (scenario === "race") {
    await click({ judge: "gemini" });
    await click({ judge: "jev" });
    pending["/api/stage/audit?judge=jev"](); await tick();
    pending["/api/stage/audit?judge=gemini"](); await tick();
    out.panel = ids.panel.innerHTML; out.toggle = ids.judge.innerHTML;
  } else if (scenario === "panels") {
    for (const id of ["issues", "fix", "review"]) { await click({ go: id }); out[id] = ids.panel.innerHTML; }
  } else if (scenario === "judge") {
    await click({ go: "judge" }); out.judge = ids.panel.innerHTML;
  } else if (scenario === "drawer") {
    menu.dataset.menu = "1";
    await click({ menu: "1" });
    out.opened = { open: ids.side.classList.contains("open"), inert: ids.panel.inert && mark.inert, focus: doc.activeElement && doc.activeElement.id };
    key("Tab"); doc.activeElement = a2; const wrapFwd = key("Tab"); out.wrapFwd = wrapFwd && doc.activeElement.id;
    doc.activeElement = menu; const wrapBack = key("Tab", true); out.wrapBack = wrapBack && doc.activeElement.id;
    key("Escape");
    out.closed = { open: ids.side.classList.contains("open"), inert: ids.panel.inert || mark.inert, focus: doc.activeElement.id, expanded: menu.attrs["aria-expanded"] };
  }
  console.log(JSON.stringify(out));
})().catch((e) => { console.error(e); process.exit(1); });
