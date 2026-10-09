// Causes view: ranked causes. Each row opens to show the description and its member calls.
(() => {
  "use strict";
  const { esc } = V;
  const STATUS = {
    ranked: "Recurring pattern", pattern_unconfirmed: "Pattern not confirmed", watch: "Watching",
    outlier: "Outlier", single_category: "One category only", isolated: "Isolated",
  };
  const status = (s) => V.own(STATUS, s) || String(s || "").replace(/_/g, " ");
  const list = (x) => (Array.isArray(x) ? x : []);
  const seg = (n, max, t) => (n ? `<i class="t-${esc(t)}" style="width:${((100 * n) / max).toFixed(2)}%"></i>` : "");
  const link = (id, v) =>
    `<a class="mlink" href="#/call/${encodeURIComponent(id)}"><span class="dot t-${esc(V.tone(v))}"></span><span class="mono">${esc(id)}</span></a>`;

  function row(c, i, max) {
    const fatal = list(c.fatal_calls).length;
    const non = list(c.nonfatal_calls).length;
    const sym = list(c.symptom_calls);
    const n = fatal + non;
    const owner = c.owner
      ? `<span class="chip owner">${esc(c.owner)}</span>${c.owner_evidence ? `<span class="ev" title="${esc(c.owner_evidence)}">${esc(c.owner_evidence)}</span>` : ""}`
      : "";
    const calls = [
      ...list(c.fatal_calls).map((id) => link(id, "Fatal")),
      ...list(c.nonfatal_calls).map((id) => link(id, "NonFatal")),
      ...sym.map((id) => link(id, "NeedsReview")),
    ].join("");
    const pid = `cause-${i}`;
    return `<li class="cause">
      <button class="crow" aria-expanded="false" aria-controls="${pid}">
        <span class="mono rank">${i + 1}</span>
        <span class="kname"><b>${esc(c.name || c.cause_id)}</b><span class="label">${esc(V.name(c.parameter))}</span></span>
        <span class="cowner">${owner}</span>
        <span class="count">${n}</span>
        <span class="split" role="img" aria-label="${fatal} Fatal, ${non} Non-fatal">
          ${seg(fatal, max, "fatal")}${seg(non, max, "nonfatal")}</span>
        <span class="label cstatus">${esc(status(c.status))}</span>
        <span class="caret" aria-hidden="true"></span>
      </button>
      <div class="panel" id="${pid}" inert><div><div class="panel-in">
        ${c.description ? `<p class="desc">${esc(c.description)}</p>` : ""}
        <p class="label">${n} ${n === 1 ? "call" : "calls"}: ${fatal} Fatal, ${non} Non-fatal${sym.length ? `, ${sym.length} with symptoms only` : ""}</p>
        <div class="members">${calls || `<span class="quiet">No member calls listed.</span>`}</div>
      </div></div></div></li>`;
  }

  VIEWS.causes = {
    async render(el, params, signal) {
      const body = await V.get("/api/causes", signal);
      if (signal?.aborted) return;
      const causes = list(body?.causes).filter((c) => c && typeof c === "object");
      if (!causes.length) {
        el.innerHTML = `<h1 class="view-title">Causes</h1><p class="empty">No causes yet. Run <code>uv run python -m vaniloop.pipeline issues</code> to group faults into causes.</p>`;
        return;
      }
      const ids = new Set(causes.flatMap((c) => [...list(c.fatal_calls), ...list(c.nonfatal_calls)]));
      const max = Math.max(1, ...causes.map((c) => list(c.fatal_calls).length + list(c.nonfatal_calls).length));
      el.innerHTML = `
        <h1 class="view-title"><em>${causes.length}</em> ${causes.length === 1 ? "cause" : "causes"} found in <em>${ids.size}</em> ${ids.size === 1 ? "call" : "calls"}.</h1>
        <p class="lede">In rank order. Open a cause to see its calls.</p>
        <div class="causes-head label" aria-hidden="true"><span></span><span>Cause</span><span>Owner</span><span class="r">Calls</span><span>Fatal / Non-fatal</span><span>Status</span><span></span></div>
        <ol class="causes">${causes.map((c, i) => row(c, i, max)).join("")}</ol>`;
      el.querySelector(".causes").addEventListener("click", (e) => {
        const b = e.target.closest(".crow");
        if (!b) return;
        const open = b.getAttribute("aria-expanded") !== "true";
        b.setAttribute("aria-expanded", open);
        b.parentElement.classList.toggle("open", open);
        b.nextElementSibling.inert = !open;
      }, { signal });
    },
  };
})();
