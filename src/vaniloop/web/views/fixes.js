// Fixes view: one honest summary line, then one row per gate run with old vs new fail rates on one scale.
(() => {
  "use strict";
  const { esc } = V;
  const DECISION = { ship: ["Shipped", "pass"], reject: ["Rejected", "fatal"], inconclusive: ["Not sure", "review"] };
  const num = (x) => (typeof x === "number" && isFinite(x) ? x : null);
  const pct = (x) => (x == null ? "–" : `${Math.round(100 * x)}%`);

  function bar(label, rate, cls, i) {
    const w = rate == null ? 0 : Math.min(100, Math.max(0, 100 * rate));
    return `<span class="rate ${cls}"><span class="label">${label}</span>
      <span class="track"><i style="width:${w.toFixed(2)}%;animation-delay:${i * 50}ms"></i></span>
      <span class="mono pc">${pct(rate)}</span></span>`;
  }

  function row(g, i, names) {
    const [word, tone] = DECISION[g.decision] || [g.decision || "Unknown", "review"];
    const p = num(g.p_value);
    const oldR = num(g.old_fail_rate);
    const newR = num(g.new_fail_rate);
    return `<li class="gate">
      <span class="gcause"><b>${esc(names.get(g.cause_id) || g.cause_id)}</b>
        <span class="label">${esc(g.cause_id)}<span class="sep">/</span>Attempt ${esc(g.attempt ?? 1)}</span></span>
      <span class="gdec"><span class="chip t-${tone}">${esc(word)}</span></span>
      <span class="rates" role="img" aria-label="Fail rate ${pct(oldR)} on the old prompt, ${pct(newR)} on the new prompt">
        ${bar("Old", oldR, "old", i)}${bar("New", newR, "new", i)}</span>
      <span class="mono gp${p == null ? " none" : ""}" title="${p == null ? "No significance test was run" : `alpha ${esc(g.alpha ?? "")}`}">${p == null ? "no test" : `p ${p < 0.001 ? p.toExponential(1) : +p.toFixed(3)}`}</span>
      <span class="greason" title="${esc(g.reason)}">${esc(g.reason)}</span></li>`;
  }

  VIEWS.fixes = {
    async render(el) {
      const [gates, causes] = await Promise.all([V.get("/api/gates"), V.get("/api/causes")]);
      if (!gates.length) {
        el.innerHTML = `<h1 class="view-title">Fixes</h1><p class="empty">No fixes tested yet. Run <code>uv run python -m vaniloop.pipeline prove --cause &lt;id&gt; --prompt &lt;file&gt;</code> to test one.</p>`;
        return;
      }
      const names = new Map((causes.causes || []).map((c) => [c.cause_id, c.name]));
      const count = (d) => gates.filter((g) => g.decision === d).length;
      const shipped = count("ship");
      const rest = [
        count("reject") && `${count("reject")} rejected`,
        count("inconclusive") && `${count("inconclusive")} not sure`,
        ((n) => `${n} ${n === 1 ? "cause" : "causes"}`)(new Set(gates.map((g) => g.cause_id)).size),
      ].filter(Boolean);
      el.innerHTML = `
        <h1 class="view-title"><em>${gates.length}</em> ${gates.length === 1 ? "fix" : "fixes"} tested. <em>${shipped}</em> shipped.</h1>
        <p class="label">${rest.join("<span class='sep'>/</span>")}</p>
        <ol class="gates">${gates.map((g, i) => row(g, i, names)).join("")}</ol>`;
    },
  };
})();
