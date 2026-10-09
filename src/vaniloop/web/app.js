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
  // Own-property lookup: a data string such as "constructor" must not reach Object.prototype.
  const own = (dict, k) => (typeof k === "string" && Object.hasOwn(dict, k) ? dict[k] : undefined);
  const tone = (v) => own(TONE, v) || "review";
  const word = (v) => own(WORDS, v) || v || "";

  const V = (window.V = {
    esc, own, tone, word,
    reduced: () => motion.matches,
    name: (p) => own(NAMES, p) || p || "",
    scope: (s) => (s === "bl_collection" ? "lead call" : String(s || "unclear").replace(/_/g, " ")),
    speaker: (s) => (s === "bot" ? "VANI" : s === "user" ? "Buyer" : s || ""),
    chip: (v) => `<span class="chip t-${esc(tone(v))}">${esc(word(v))}</span>`,
    shortId: (id) => (id.length > 14 ? `${id.slice(0, 10)}…` : id),
    clock(sec) {
      if (sec == null || !isFinite(sec)) return "–";
      const s = Math.round(sec);
      return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
    },
    async get(path, signal) {
      const res = await fetch(path, { headers: { Accept: "application/json" }, signal });
      const body = await res.json().catch(() => null);
      if (!res.ok) throw Object.assign(new Error(body?.error || res.statusText), { status: res.status });
      return body;
    },
  });

  const VIEWS = (window.VIEWS = window.VIEWS || {});

  VIEWS.invalid = {
    render(el) {
      el.innerHTML = `<p class="empty">This link is not valid. <a href="#/">← All calls</a></p>`;
    },
  };

  function parse() {
    const [head, ...rest] = location.hash.replace(/^#\/?/, "").split("/");
    if (head === "call" && rest.length) {
      try {
        return { view: "call", nav: "calls", params: { id: decodeURIComponent(rest.join("/")) } };
      } catch {
        return { view: "invalid", nav: "calls", params: {} };
      }
    }
    if (head === "causes" || head === "fixes") return { view: head, nav: head, params: {} };
    return { view: "calls", nav: "calls", params: {} };
  }

  // Each route change gets a token (seq) and an AbortController. A newer route aborts the older
  // one: its fetches, listeners and observers end, and its late completion is dropped.
  let seq = 0;
  let ctl = null;
  async function route(e) {
    const my = ++seq;
    ctl?.abort();
    ctl = new AbortController();
    const { signal } = ctl;
    const main = document.getElementById("view");
    // A superseded fade-out (fill: forwards) would hold main at opacity 0. Cancel it first.
    main.getAnimations().forEach((a) => a.cancel());
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
      await VIEWS[r.view].render(view, r.params, signal);
    } catch (err) {
      if (my !== seq) return;
      view.innerHTML =
        err instanceof TypeError && !err.status
          ? `<p class="empty">The server did not answer (${V.esc(err.message)}). Start it with <code>uv run python -m vaniloop.web</code> and reload.</p>`
          : `<p class="empty">This view could not be shown (${V.esc(err.message)}).</p>`;
    }
    if (my !== seq) return;
    // Keep keyboard focus in the new view. The first load keeps the browser default.
    if (e?.type === "hashchange") {
      const h = view.querySelector("h1");
      if (h && !h.hasAttribute("tabindex")) h.tabIndex = -1;
      (h || main).focus({ preventScroll: true });
    }
    if (!V.reduced()) {
      view.animate([{ opacity: 0, transform: "translateY(12px)" }, { opacity: 1, transform: "none" }], { duration: 320, easing: "cubic-bezier(.2,.7,.1,1)" });
    }
  }

  window.addEventListener("hashchange", route);
  document.addEventListener("DOMContentLoaded", route);
})();
