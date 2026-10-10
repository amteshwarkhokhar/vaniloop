// Runs src/vaniloop/web/dash.js against a tiny DOM stub and prints JSON. Usage: node dash_harness.js <scenario>
const fs = require("fs"), path = require("path"), vm = require("vm");
const web = (f) => fs.readFileSync(path.join(__dirname, "..", "src", "vaniloop", "web", f), "utf8");
const src = web("dash.js"), consoleSrc = web("console.js"), homeSrc = web("home.js");

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
for (const id of ["panel", "side", "nav", "side-foot", "menu-now", "dock", "dock-info", "dock-actions", "dock-log", "tune-res", "rb-save"]) ids[id] = el(id);
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
let tid = 0;
const consoleOn = (process.argv[2] || "").startsWith("con-");
const sandbox = {
  btoa: (b) => Buffer.from(b, "binary").toString("base64"), setTimeout: (f, ms) => (timers.push({ id: ++tid, f, ms }), tid), clearTimeout: (i) => { const k = timers.findIndex((t) => t && t.id === i); if (k >= 0) timers[k] = null; },
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
    routes["GET /api/rubric"] = [200, RUB]; routes["POST /api/tune"] = [200, TUNE]; routes["GET /api/tracker"] = [200, TRK];
    for (const g of ["run", "submit", "audit", "rubric", "tune", "tracker"]) { await click({ go: g }); out[g] = ids.panel.innerHTML; }
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
  async "con-rubric"() {
    const out = {}, put = () => calls.filter((c) => c.method === "PUT").map((c) => c.body);
    routes["GET /api/rubric"] = [200, RUB];
    await click({ go: "rubric" });
    out.panel = ids.panel.innerHTML; out.nav = ids.nav.innerHTML;
    await emit("input", { dataset: { op: "param", id: "rules.FRUSTRATED_P" }, value: "0.9" });
    await emit("input", { dataset: { op: "q", q: "spec_fit", f: "instructions" }, value: "New text" });
    routes["PUT /api/rubric"] = [[400, { error: "must be at most 0.8", field: "rules.FRUSTRATED_P" }], [200, { ...RUB, params: [prm("rules.FRUSTRATED_P", "Frustrated at", 0.5, { value: 0.7, overridden: true }), ...RUB.params.slice(1)] }]];
    await click({ op: "save-rubric" });
    out.err = ids.panel.innerHTML;
    await emit("input", { dataset: { op: "param", id: "rules.FRUSTRATED_P" }, value: "0.7" });
    await click({ op: "save-rubric" });
    out.saved = ids.panel.innerHTML; out.put = put();
    routes["POST /api/jobs"] = [202, { job: job("j1", "judge", "running") }];
    await click({ op: "rejudge" });
    out.posts = posts(); out.chain = sandbox.window.VL.console.C.chain;
    return out;
  },
  async "con-rubric-q"() {
    routes["GET /api/rubric"] = [200, RUB];
    routes["PUT /api/rubric"] = [200, RUB];
    await click({ go: "rubric" });
    ids["rb-save"].disabled = true;
    await emit("input", { dataset: { op: "q", q: "spec_fit", f: "true.examples" }, value: "one\ntwo" });
    const afterEdit = ids["rb-save"].disabled;
    routes["PUT /api/rubric"] = [[400, { error: "must be an object", field: "body" }], [200, RUB]];
    await click({ op: "save-rubric" });
    const banner = ids.panel.innerHTML;
    await click({ op: "save-rubric" });
    return { afterEdit, banner, put: calls.filter((c) => c.method === "PUT").map((c) => c.body).slice(-1) };
  },
  async "con-tune"() {
    const out = {};
    routes["GET /api/rubric"] = [200, RUB];
    routes["POST /api/tune"] = [200, TUNE];
    routes["PUT /api/rubric"] = [200, RUB];
    await click({ go: "tune" });
    const tunes = () => calls.filter((c) => c.p === "/api/tune");
    const n0 = tunes().length;
    for (const v of ["0.6", "0.62", "0.65"]) await emit("input", { dataset: { op: "tune", key: "question_fits.true_at" }, value: v });
    out.timers = timers.filter(Boolean).map((t) => t.ms);
    out.beforeFire = tunes().length - n0;
    await fire();
    out.after = tunes().slice(n0).map((c) => c.body);
    out.res = ids["tune-res"].innerHTML;
    await click({ op: "tune-save" });
    out.put = calls.filter((c) => c.method === "PUT").map((c) => c.body);
    routes["POST /api/tune"] = [409, { error: "no raw Jev answers: run judge" }];
    await emit("input", { dataset: { op: "tune", key: "frustrated_p" }, value: "0.4" }); await fire();
    out.noraw = ids["tune-res"].innerHTML;
    return out;
  },
  async "con-tracker"() {
    const out = {}, tp = () => calls.filter((c) => c.method === "POST" && c.p.startsWith("/api/tracker")).map((c) => [c.p, c.body]);
    routes["GET /api/tracker"] = [200, TRK];
    routes["POST /api/tracker/c4/ticket"] = [[400, { error: "ref is required" }], [200, { cause: cz("c4", "ticket_sent") }]];
    routes["POST /api/tracker/c3/ship"] = [[409, { error: "gate no longer ships" }], [202, { job: job("j7", "ship", "running") }]];
    routes["GET /api/jobs/j7/log?from=0"] = [200, { job: job("j7", "ship", "done"), lines: ["new prompt version v3"], next: 1 }];
    out.nav = ids.nav.innerHTML; // before any visit: the sidebar count comes from the boot read
    await click({ go: "tracker" });
    out.board = ids.panel.innerHTML;
    await click({ op: "t-sent", id: "c4" }); out.empty = ids.panel.innerHTML;
    await emit("input", { dataset: { op: "tref", id: "c4" }, value: " T-1 " });
    await click({ op: "t-sent", id: "c4" }); out.bad = ids.panel.innerHTML;
    await click({ op: "t-sent", id: "c4" });
    await click({ op: "ship", id: "c3" }); out.confirm = ids.panel.innerHTML;
    await click({ op: "ship-go", id: "c3" }); out.conflict = ids.panel.innerHTML;
    const reads = () => calls.filter((c) => c.p === "/api/tracker").length;
    const before = reads();
    routes["GET /api/tracker"] = [200, { ...TRK, current_prompt_version: "v3", prompt_versions: [{ version: "v3", created_ist: "12:00 10 Oct", causes: ["c3"], base: "v2" }, ...TRK.prompt_versions] }];
    await click({ op: "ship-go", id: "c3" });
    await fire();
    out.dock = ids["dock-info"].innerHTML; out.rereads = reads() - before; out.after = ids.panel.innerHTML;
    out.posts = tp();
    return out;
  },
  async "con-home"() {
    const out = {};
    routes["GET /api/calls"] = [200, [{ call_id: "c1", verdict: "Pass" }, { call_id: "c2<img src=x>", verdict: "Fatal" }, { call_id: "c3", verdict: null }]];
    routes["GET /api/loop/plan"] = [200, { steps: [{ name: "audit", will_run: false, why: "no new calls", count: 0 }, { name: "test", will_run: true, why: "prove <b>2</b>", count: 2 }], causes: [], new_calls: 0, estimate_usd: 0.9 }];
    await click({ go: "home" }); await tick();
    out.idle = ids.panel.innerHTML; out.nav = ids.nav.innerHTML;
    routes["POST /api/jobs"] = [[400, { error: "confirm", estimate_usd: 0.9 }], [202, { job: job("L1", "loop", "running") }]];
    const loopLog = (from, st, lines, next) => (routes[`GET /api/jobs/L1/log?from=${from}`] = [200, { job: job("L1", "loop", st), lines, next }]);
    loopLog(0, "running", ["LOOP_STEP 1/7 audit", "LOOP_STEP 2/7 judge"], 2);
    loopLog(2, "done", ["LOOP_STEP 4/7 issues", "LOOP_STEP 5/7 fix", "LOOP_STEP 6/7 test", "LOOP_STEP 7/7 report",
      'LOOP_SUMMARY {"ready_to_ship":["c3"],"tickets":["c4","c5"],"watch":[],"cost_usd":0.42}', "LOOP_DONE"], 8);
    await click({ op: "home-run" });
    out.confirm = ids.panel.innerHTML;
    await click({ op: "home-go" });
    out.mid = ids.panel.innerHTML;
    await fire(); await tick();
    out.done = ids.panel.innerHTML;
    out.posts = posts();
    return out;
  },
  async "con-home-job"() {
    routes["GET /api/calls"] = [200, [{ call_id: "c1", verdict: "Fatal" }]];
    routes["GET /api/loop/plan"] = [200, { steps: [], causes: [], new_calls: 0, estimate_usd: 0 }];
    await click({ go: "home" }); await tick();
    await click({ op: "home-replay" });
    routes["POST /api/jobs"] = [202, { job: job("J1", "judge", "running") }];
    logRoute("J1", 0, "running", ["working"], 1);
    await click({ go: "run" }); await click({ op: "run", stage: "judge" });
    await click({ go: "home" });
    return { home: ids.panel.innerHTML };
  },
  async "con-home-ro"() {
    routes["GET /api/calls"] = [200, [{ call_id: "c1", verdict: "Fatal" }]];
    routes["GET /api/loop/plan"] = [200, { steps: [], causes: [], new_calls: 0, estimate_usd: null }];
    await click({ go: "home" }); await tick();
    const out = { ro: ids.panel.innerHTML };
    await click({ op: "home-replay" });
    out.replay = ids.panel.innerHTML;
    for (let i = 0; i < 80; i++) await fire();
    out.end = ids.panel.innerHTML; out.posts = posts();
    return out;
  },
  async "con-tabs"() {
    const out = {};
    await click({ go: "review" }); out.review = ids.panel.innerHTML; out.nav = ids.nav.innerHTML;
    await click({ go: "thresholds" }); out.thresholds = ids.panel.innerHTML;
    return out;
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
const prm = (id, label, value, extra = {}) => ({ id, group: "g", label, help: "Plain help.", type: "float", min: 0, max: 1, step: 0.05, default: value, value, overridden: false, ...extra });
const Q = { instructions: "Ask it.", true: { description: "yes", examples: ["a"] }, false: { description: "no", examples: [] } };
const RUB = { path_name: "rubric.json", jev_version_effective: "4", checks: [
  { id: "looping", name: "No looping <img src=x onerror=alert(1)>", what: "Bot repeats itself.", fatal_when: "Three asks.", nonfatal_when: "Two asks.", prompt_ref: "L69", judged_by: "code", params: ["rules.FRUSTRATED_P", "rules.DEAD_AIR_SEC"] }],
  params: [prm("rules.FRUSTRATED_P", "Frustrated at", 0.5), prm("rules.DEAD_AIR_SEC", "Dead air seconds", 5, { type: "int", max: 30, step: 1 }), prm("jev.Q_TRUE_AT", "Q true", 0.55), prm("jev.Q_FALSE_AT", "Q false", 0.2),
    prm("jev.OPT_TRUE_AT", "Opt true", 0.6), prm("jev.OPT_FALSE_AT", "Opt false", 0.3), prm("jev.PM_TRUE_AT", "PM true", 0.65), prm("jev.PM_FALSE_AT", "PM false", 0.35), prm("jev.N_DROP_AT", "Name drop", 0.35)],
  questions: [{ id: "spec_fit", qid: "q", label: "Spec fit", value: Q, default: Q, overridden: false }] };
const row = (label, split, right) => ({ label, split, n: 40, right, wrong: 40 - right - 5, unsure: 5, gemini_right: 28 });
const TUNE = { labels: [row("question_fits", "dev", 31), row("question_fits", "test", 29)], current: [row("question_fits", "dev", 28), row("question_fits", "test", 29)], flips: { question_fits: 3 }, calls: 712 };
const cz = (id, state, extra = {}) => ({ cause_id: id, name: `Cause ${id}`, parameter: "qty_probing", type: "prompt", owner: "owner a", route: "patch", state, state_since_ist: "2026-10-10T09:00:00+05:30", counts: { fatal: 2, nonfatal: 1, symptoms: 3 }, rubric_version: "4", events: [], ...extra });
const TRK = { current_prompt_version: "v2", prompt_versions: [{ version: "v2", created_ist: "11:00 10 Oct", causes: ["c3"], base: "v1" }], causes: [
  cz("c1", "open"), cz("c2", "in_fix"), cz("c3", "ready_to_ship", { events: [{ ts_ist: "10:00 10 Oct", kind: "gated", run_id: "r1", rubric_version: "4", detail: { p_value: 0.01, lift: 0.25, guard: "pass", note: "<img src=x onerror=alert(1)>" } }] }),
  cz("c4", "ticket_open", { type: "tool", route: "ticket" }), cz("c5", "ticket_sent", { type: "tool" }), cz("c6", "regressed", { name: "Back <img src=x onerror=alert(1)>" }), cz("c7", "shipped"), cz("c8", "watching"), cz("c9", "closed")] };
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
    routes["GET /api/console"] = [200, { ...CON, read_only: scenario === "con-readonly" || scenario === "con-home-ro" }];
    routes["GET /api/jobs"] = [200, { jobs: [] }];
    routes["GET /api/tracker"] = [200, TRK]; // the sidebar reads it at boot
    vm.runInContext(consoleSrc, vctx);
    if (scenario.startsWith("con-home")) vm.runInContext(homeSrc, vctx);
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
    out.panel = ids.panel.innerHTML;
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
