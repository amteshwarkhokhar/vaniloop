// Runs src/vaniloop/web/dash.js against a tiny DOM stub and prints JSON. Usage: node dash_harness.js <scenario>
const fs = require("fs"), path = require("path"), vm = require("vm");
const src = fs.readFileSync(path.join(__dirname, "..", "src", "vaniloop", "web", "dash.js"), "utf8");

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
for (const id of ["panel", "side", "nav", "judge", "judge-wrap", "menu-now"]) ids[id] = el(id);
const menu = el("menu"), mark = el("mark"), a1 = el("a1"), a2 = el("a2");
ids.side.kids = [a1, a2];
doc.getElementById = (id) => ids[id];
doc.querySelector = (q) => (q === ".menu" ? menu : q === ".bar .mark" ? mark : null);
doc.addEventListener = (t, f) => (doc.listeners[t] = f);

const cfg = {
  judge: { available: { value: ["jev", "gemini"] }, active: { value: "jev", source: "t" }, structure_model: { value: "m" }, jev_model: { value: "j" }, jev_version: { value: "1" }, thresholds: { true_at: { value: 0.7 }, false_at: { value: 0.3 } } },
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
const pending = {};
const sandbox = {
  document: doc, console, history: { replaceState() {} },
  location: { hash: "" }, performance: { now: () => 0 }, requestAnimationFrame() {},
  matchMedia: () => ({ matches: false, addEventListener() {} }),
  fetch(p) {
    const body = (v) => ({ ok: true, json: async () => v });
    if (p === "/api/config") return Promise.resolve(body(cfg));
    if (p.includes("?judge=")) return new Promise((res) => (pending[p] = () => res(body(audit(p.split("=")[1], p.endsWith("gemini") ? 7 : 3)))));
    return Promise.resolve(body(stages[p.split("/").pop()]));
  },
};
sandbox.window = { addEventListener() {}, scrollTo() {}, scrollY: 0 };
const tick = () => new Promise((r) => setTimeout(r, 5));
const click = async (dataset) => { doc.listeners.click({ target: { closest: () => ({ dataset, classList: { contains: (c) => c === "menu" && !!dataset.menu }, setAttribute() {} }) } }); await tick(); };
const key = (k, shiftKey = false) => { let pd = false; doc.listeners.keydown({ key: k, shiftKey, preventDefault() { pd = true; } }); return pd; };

(async () => {
  const scenario = process.argv[2];
  sandbox.location.hash = scenario === "race" ? "#/audit" : "";
  vm.runInNewContext(src, sandbox);
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
