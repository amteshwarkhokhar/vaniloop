// Call view: the tape (mirrored turns, silences, fault notches), the script with margin notes, and the checks.
(() => {
  "use strict";
  const { esc } = V;
  const PX_PER_SEC = 9;
  const BASIS = { observed: "Observed", spoken_proxy: "Spoken proxy", unavailable: "Unavailable" };
  const pct = (x, total) => `${((100 * x) / total).toFixed(3)}%`;

  function tape(call, faults, dur) {
    const segs = call.turns.map((t) => {
      const words = String(t.text || "").split(/\s+/).filter(Boolean).length;
      const h = Math.min(38, 8 + words * 1.6);
      const side = t.speaker === "bot" ? "bot" : "user";
      return `<span class="seg ${side}" data-turn="${t.idx}" style="left:${pct(t.start_sec, dur)};width:${pct(Math.max(0, t.end_sec - t.start_sec), dur)};height:${h}%"></span>`;
    }).join("");
    const gaps = (call.silences || []).filter((s) => s.end_sec - s.start_sec > 5).map((s) =>
      `<span class="gap" style="left:${pct(s.start_sec, dur)};width:${pct(s.end_sec - s.start_sec, dur)}"><em class="label">${(s.end_sec - s.start_sec).toFixed(1)} s silent</em></span>`).join("");
    const byIdx = new Map(call.turns.map((t) => [t.idx, t]));
    const notches = faults.map((f, i) => {
      const t = byIdx.get(f.onset_turn);
      if (!t) return "";
      return `<button class="notch t-${V.tone(f.status)}" data-turn="${t.idx}" style="left:${pct(t.start_sec, dur)};--d:${i * 60}ms" aria-label="${esc(V.word(f.status))}: ${esc(V.name(f.parameter))} at ${V.clock(t.start_sec)}. Go to the turn."></button>`;
    }).join("");
    const step = dur > 240 ? 60 : dur > 90 ? 30 : 10;
    let ticks = "";
    for (let s = 0; s <= dur; s += step) ticks += `<span style="left:${pct(s, dur)}">${V.clock(s)}</span>`;
    return `<div class="tape-wrap" role="region" aria-label="Call tape" tabindex="0">
      <div class="tape-in" style="min-width:${Math.ceil(dur * PX_PER_SEC)}px">
        <div class="tape">${gaps}<div class="segs">${segs}</div>${notches}</div>
        <div class="ticks mono" aria-hidden="true">${ticks}</div>
      </div></div>`;
  }

  function note(f) {
    return `<div class="note t-${V.tone(f.status)}">
      <span class="label">${esc(V.name(f.parameter))} <span class="st">${esc(V.word(f.status))}</span></span>
      <p>${esc(f.reason)}</p><span class="basis">${esc(BASIS[f.basis] || f.basis || "")}</span></div>`;
  }

  function script(call, faults) {
    return call.turns.map((t) => {
      const notes = faults.filter((f) => f.onset_turn === t.idx);
      return `<li class="turn ${t.speaker === "bot" ? "bot" : "user"}" id="turn-${t.idx}">
        <span class="mono ts">${V.clock(t.start_sec)}</span><span class="spk">${esc(V.speaker(t.speaker))}</span>
        <p class="text">${esc(t.text)}</p>${notes.length ? `<div class="notes">${notes.map(note).join("")}</div>` : ""}</li>`;
    }).join("");
  }

  function checks(findings) {
    if (!findings.length) return `<p class="empty">No checks were recorded for this call.</p>`;
    return `<ul>${findings.map((f) => `<li>
      <span class="dot t-${V.tone(f.status)}"></span><span class="cname">${esc(V.name(f.parameter))}</span>
      <span class="cstat">${esc(V.word(f.status))}</span><p class="creason" title="${esc(f.reason)}">${esc(f.reason)}</p></li>`).join("")}</ul>`;
  }

  VIEWS.call = {
    async render(el, { id }) {
      const back = `<a class="back" href="#/">← All calls</a>`;
      let data;
      try {
        data = await V.get(`/api/call/${encodeURIComponent(id)}`);
      } catch (e) {
        if (e.status !== 404) throw e;
        el.innerHTML = `${back}<p class="empty">No graded call has the id <code>${esc(id)}</code>.</p>`;
        return;
      }
      const call = data.call && Array.isArray(data.call.turns) ? data.call : null;
      const audit = data.audit || {};
      const findings = (audit.findings || []).filter((f) => f && typeof f === "object");
      const faults = findings.filter((f) => f.onset_turn != null && f.status !== "Pass");
      const dur = call ? Math.max(1, call.duration_sec || 0, ...call.turns.map((t) => t.end_sec || 0)) : null;
      el.innerHTML = `${back}
        <header class="call-head">
          <h1 class="call-id mono">${esc(id)}</h1>
          <p class="call-meta"><span class="mono">${V.clock(dur ?? data.call?.duration_sec)}</span>
            <span>${esc(V.scope(audit.scope))}</span>${audit.verdict ? V.chip(audit.verdict) : ""}</p>
          ${audit.review_reason ? `<p class="review"><span class="label">Why it needs review</span>${esc(audit.review_reason)}</p>` : ""}
        </header>
        ${call ? tape(call, faults, dur) : `<p class="empty">This call has no transcript in calls.jsonl, so there is no tape or script.</p>`}
        <div class="call-body">
          <section class="script" aria-label="Script">${call ? `<h2 class="label">Script</h2><ol>${script(call, faults)}</ol>` : ""}</section>
          <aside class="checks" aria-label="Checks"><h2 class="label">Checks</h2>${checks(findings)}</aside>
        </div>`;

      const turnEl = (n) => el.querySelector(`#turn-${n.dataset.turn}`);
      const mark = (n, on) => {
        turnEl(n)?.classList.toggle("hl", on);
        el.querySelector(`.seg[data-turn="${n.dataset.turn}"]`)?.classList.toggle("hl", on);
      };
      el.querySelectorAll(".notch").forEach((n) => {
        n.addEventListener("pointerenter", () => mark(n, true));
        n.addEventListener("pointerleave", () => mark(n, false));
        n.addEventListener("focus", () => mark(n, true));
        n.addEventListener("blur", () => mark(n, false));
        n.addEventListener("click", () => turnEl(n)?.scrollIntoView({ behavior: V.reduced() ? "auto" : "smooth", block: "center" }));
      });

      if ("IntersectionObserver" in window && !V.reduced()) {
        const io = new IntersectionObserver((entries) => entries.forEach((e) => {
          if (!e.isIntersecting) return;
          e.target.classList.remove("rest");
          io.unobserve(e.target);
        }), { rootMargin: "0px 0px -8% 0px" });
        el.querySelectorAll(".note").forEach((n) => { n.classList.add("rest"); io.observe(n); });
      }
    },
  };
})();
