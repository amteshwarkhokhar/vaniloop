// Calls view: the hero sentence, the spectrum (one hairline per call), the legend, the filter and the call list.
(() => {
  "use strict";
  const { esc } = V;
  const ORDER = ["Fatal", "NonFatal", "NeedsReview", "Pass"];
  const PAGE = 100;
  let filter = "all";

  const plural = (n, one, many) => `${n} ${n === 1 ? one : many}`;

  function row(c) {
    const f = c.first_fault;
    const fault = f
      ? `<b>${esc(V.name(f.parameter))}</b>${esc(f.reason)}`
      : `<span class="quiet">No fault found</span>`;
    return `<li><a class="row" href="#/call/${encodeURIComponent(c.call_id)}">
      <span class="mono cid" title="${esc(c.call_id)}">${esc(V.shortId(c.call_id))}</span>
      <span class="mono dur">${V.clock(c.duration_sec)}</span>
      <span class="scope">${esc(V.scope(c.scope))}</span>
      <span class="verdict">${V.chip(c.verdict)}</span>
      <span class="fault">${fault}</span></a></li>`;
  }

  VIEWS.calls = {
    async render(el) {
      const [sum, calls] = await Promise.all([V.get("/api/summary"), V.get("/api/calls")]);
      if (!sum.total || !calls.length) {
        el.innerHTML = `<p class="empty">No graded calls yet. Run <code>uv run python -m vaniloop.pipeline audit</code> to grade calls.</p>`;
        return;
      }
      const fatal = sum.verdicts.Fatal || 0;
      const meta = [
        sum.updated_ist && `Updated ${sum.updated_ist} IST`,
        sum.spend_usd != null && `Grading spend $${sum.spend_usd.toFixed(2)}`,
      ].filter(Boolean);
      el.innerHTML = `
        <section class="hero">
          <h1 class="hero-line"><span><em>${sum.total}</em> ${sum.total === 1 ? "call" : "calls"} heard.</span>
            <span>${fatal ? `<em>${fatal}</em> ${fatal === 1 ? "carries" : "carry"} a Fatal fault.` : "None carries a Fatal fault."}</span></h1>
          ${meta.length ? `<p class="label">${meta.join("<span class='sep'>/</span>")}</p>` : ""}
        </section>
        <figure class="spectrum" aria-label="${plural(calls.length, "call", "calls")}, one line each, height by length, grouped by verdict">
          <div class="bars"></div><div class="playhead"></div><div class="tip" role="tooltip" hidden></div>
        </figure>
        <ul class="legend">${ORDER.map((v) => `<li><span class="dot t-${V.tone(v)}"></span><span class="label">${V.word(v)}</span><span class="mono num">${sum.verdicts[v] || 0}</span></li>`).join("")}</ul>
        <div class="filters" role="group" aria-label="Show calls by verdict">
          ${["all", ...ORDER].map((v) => `<button class="f" data-v="${v}" aria-pressed="${v === filter}">${v === "all" ? "All" : V.word(v)}</button>`).join("")}
        </div>
        <div class="rows-head label" aria-hidden="true"><span>Call</span><span>Length</span><span>Scope</span><span>Verdict</span><span>First fault</span></div>
        <ol class="rows"></ol>
        <button class="f more" hidden></button>`;

      const fig = el.querySelector(".spectrum");
      const bars = el.querySelector(".bars");
      const tip = el.querySelector(".tip");
      const max = Math.max(1, ...calls.map((c) => c.duration_sec || 0));
      const span = 900 - 420;
      fig.style.setProperty("--bw", calls.length > 240 ? "1px" : "2px");
      bars.innerHTML = calls.map((c, i) => {
        const h = 14 + 84 * ((c.duration_sec || 0) / max);
        const d = calls.length > 1 ? (i / (calls.length - 1)) * span : 0;
        return `<a class="bar t-${V.tone(c.verdict)}" href="#/call/${encodeURIComponent(c.call_id)}" data-i="${i}" tabindex="-1" aria-hidden="true"><i style="height:${h.toFixed(1)}%;animation-delay:${d.toFixed(0)}ms"></i></a>`;
      }).join("");

      const playhead = el.querySelector(".playhead");
      if (V.reduced()) playhead.remove();
      else playhead.addEventListener("animationend", () => playhead.remove());

      bars.addEventListener("pointerover", (e) => {
        const bar = e.target.closest(".bar");
        if (!bar) return;
        const c = calls[bar.dataset.i];
        tip.textContent = `${c.call_id}  ·  ${V.clock(c.duration_sec)}  ·  ${V.word(c.verdict)}`;
        tip.hidden = false;
        const half = tip.offsetWidth / 2;
        const x = bar.offsetLeft + bar.offsetWidth / 2;
        tip.style.left = `${Math.min(Math.max(x, half), fig.clientWidth - half)}px`;
        tip.style.bottom = `${bar.firstElementChild.offsetHeight + 16}px`;
      });
      bars.addEventListener("pointerleave", () => (tip.hidden = true));

      const rows = el.querySelector(".rows");
      const more = el.querySelector(".more");
      let shown = PAGE;
      const match = (c) => filter === "all" || c.verdict === filter;
      function list() {
        const items = calls.filter(match);
        rows.innerHTML = items.length ? items.slice(0, shown).map(row).join("") : `<li class="empty none">No calls with this verdict.</li>`;
        more.hidden = items.length <= shown;
        more.textContent = `Show ${Math.min(PAGE, items.length - shown)} more`;
      }
      function apply() {
        bars.querySelectorAll(".bar").forEach((b) => b.classList.toggle("dim", !match(calls[b.dataset.i])));
        shown = PAGE;
        list();
        if (!V.reduced()) rows.animate([{ opacity: 0 }, { opacity: 1 }], { duration: 240, easing: "ease-out" });
      }
      el.querySelector(".filters").addEventListener("click", (e) => {
        const b = e.target.closest("button[data-v]");
        if (!b || b.dataset.v === filter) return;
        filter = b.dataset.v;
        el.querySelectorAll(".filters [data-v]").forEach((x) => x.setAttribute("aria-pressed", x === b));
        apply();
      });
      more.addEventListener("click", () => {
        shown += PAGE;
        list();
      });
      bars.querySelectorAll(".bar").forEach((b) => b.classList.toggle("dim", !match(calls[b.dataset.i])));
      list();
    },
  };
})();
