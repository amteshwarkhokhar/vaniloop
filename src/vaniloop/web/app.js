// vaniloop UI shell: hash router, fetch helpers, human names. Views register on window.VIEWS.
(() => {
  "use strict";
  const NAMES = {
    product_change: "Right product", category_isq: "Right questions", qty_probing: "Quantity asked",
    qty_missed: "Not asked twice", qty_accuracy: "Right number", qty_unit: "Right unit",
    looping: "No looping", dead_air: "No dead air", intent: "Buying intent",
  };
  const WORDS = { Fatal: "Fatal", NonFatal: "Non-fatal", NeedsReview: "Needs review", Pass: "Pass", Unknown: "Unknown" };
  const TONE = { Fatal: "fatal", NonFatal: "nonfatal", NeedsReview: "review", Unknown: "review", Pass: "pass" };
  const motion = matchMedia("(prefers-reduced-motion: reduce)");
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => `&#${c.charCodeAt(0)};`);
  const tone = (v) => TONE[v] || "review";
  const word = (v) => WORDS[v] || v || "";

  const V = (window.V = {
    esc, tone, word,
    reduced: () => motion.matches,
    name: (p) => NAMES[p] || p || "",
    scope: (s) => (s === "bl_collection" ? "lead call" : String(s || "unclear").replace(/_/g, " ")),
    speaker: (s) => (s === "bot" ? "VANI" : s === "user" ? "Buyer" : s || ""),
    chip: (v) => `<span class="chip t-${tone(v)}">${esc(word(v))}</span>`,
    shortId: (id) => (id.length > 14 ? `${id.slice(0, 10)}…` : id),
    clock(sec) {
      if (sec == null || !isFinite(sec)) return "–";
      const s = Math.round(sec);
      return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
    },
    async get(path) {
      const res = await fetch(path, { headers: { Accept: "application/json" } });
      const body = await res.json().catch(() => null);
      if (!res.ok) throw Object.assign(new Error(body?.error || res.statusText), { status: res.status });
      return body;
    },
  });

  const VIEWS = (window.VIEWS = window.VIEWS || {});
  const soon = (title) => ({
    render(el) {
      el.innerHTML = `<h1 class="view-title">${title}</h1><p class="lede">Coming next.</p>`;
    },
  });
  VIEWS.causes = VIEWS.causes || soon("Causes");
  VIEWS.fixes = VIEWS.fixes || soon("Fixes");

  function parse() {
    const [head, ...rest] = location.hash.replace(/^#\/?/, "").split("/");
    if (head === "call" && rest.length) return { view: "call", nav: "calls", params: { id: decodeURIComponent(rest.join("/")) } };
    if (head === "causes" || head === "fixes") return { view: head, nav: head, params: {} };
    return { view: "calls", nav: "calls", params: {} };
  }

  let seq = 0;
  async function route() {
    const my = ++seq;
    const main = document.getElementById("view");
    const r = parse();
    document.querySelectorAll("[data-nav]").forEach((a) => {
      if (a.dataset.nav === r.nav) a.setAttribute("aria-current", "page");
      else a.removeAttribute("aria-current");
    });
    let out = null;
    if (!V.reduced() && main.childElementCount) {
      out = main.animate([{ opacity: 1 }, { opacity: 0 }], { duration: 120, easing: "ease-out", fill: "forwards" });
      await out.finished.catch(() => {});
    }
    if (my !== seq) return;
    main.replaceChildren();
    out?.cancel();
    window.scrollTo(0, 0);
    const view = document.createElement("div");
    main.append(view);
    try {
      await VIEWS[r.view].render(view, r.params);
    } catch (e) {
      view.innerHTML = `<p class="empty">The server did not answer (${V.esc(e.message)}). Start it with <code>uv run python -m vaniloop.web</code> and reload.</p>`;
    }
    if (my !== seq) return;
    if (!V.reduced()) {
      view.animate([{ opacity: 0, transform: "translateY(12px)" }, { opacity: 1, transform: "none" }], { duration: 320, easing: "cubic-bezier(.2,.7,.1,1)" });
    }
  }

  window.addEventListener("hashchange", route);
  document.addEventListener("DOMContentLoaded", route);
})();
