// Call view: the tape (mirrored turns, silences, fault notches), the script with margin notes, and the checks.
(() => {
  "use strict";
  const { esc } = V;
  const PX_PER_SEC = 9;
  const BASIS = { observed: "Observed", spoken_proxy: "Spoken proxy", unavailable: "Unavailable" };
  const pct = (x, total) => `${((100 * x) / total).toFixed(3)}%`;
  const num = (x) => (typeof x === "number" && isFinite(x) ? x : null);

  function tape(call, faults, dur) {
    const segs = call.turns.map((t) => {
      const words = V.str(t.text).split(/\s+/).filter(Boolean).length;
      const h = Math.min(38, 8 + words * 1.6);
      const side = t.speaker === "bot" ? "bot" : "user";
      return `<span class="seg ${side}" data-turn="${esc(t.idx)}" style="left:${pct(t.start_sec, dur)};width:${pct(Math.max(0, t.end_sec - t.start_sec), dur)};height:${h}%"></span>`;
    }).join("");
    const gaps = (Array.isArray(call.silences) ? call.silences : []).filter((s) => s && s.end_sec - s.start_sec > 5).map((s) =>
      `<span class="gap" style="left:${pct(s.start_sec, dur)};width:${pct(s.end_sec - s.start_sec, dur)}"><em class="label">${(s.end_sec - s.start_sec).toFixed(1)} s silent</em></span>`).join("");
    const byIdx = new Map(call.turns.map((t) => [t.idx, t]));
    const notches = faults.map((f, i) => {
      const t = byIdx.get(f.onset_turn);
      if (!t) return "";
      return `<button class="notch t-${esc(V.tone(f.status))}" data-turn="${esc(t.idx)}" style="left:${pct(t.start_sec, dur)};--d:${i * 60}ms" aria-label="${esc(V.word(f.status))}: ${esc(V.name(f.parameter))} at ${esc(V.clock(t.start_sec))}. Go to the turn."></button>`;
    }).join("");
    const base = dur > 240 ? 60 : dur > 90 ? 30 : 10;
    const step = Math.max(base, Math.ceil(dur / 200 / base) * base); // at most about 200 ticks, even for a bad duration
    let ticks = "";
    for (let s = 0; s <= dur; s += step) ticks += `<span style="left:${pct(s, dur)}">${V.clock(s)}</span>`;
    return `<div class="tape-wrap" role="region" aria-label="Call tape" tabindex="0">
      <div class="tape-in" style="min-width:${Math.min(Math.ceil(dur * PX_PER_SEC), 20000)}px">
        <div class="tape">${gaps}<div class="segs">${segs}</div>${notches}</div>
        <div class="ticks mono" aria-hidden="true">${ticks}</div>
      </div></div>`;
  }

  function note(f) {
    return `<div class="note t-${esc(V.tone(f.status))}">
      <span class="label">${esc(V.name(f.parameter))} <span class="st">${esc(V.word(f.status))}</span></span>
      <p>${esc(f.reason)}</p><span class="basis">${esc(V.own(BASIS, f.basis) || f.basis || "")}</span></div>`;
  }

  function script(call, faults) {
    return call.turns.map((t) => {
      const notes = faults.filter((f) => f.onset_turn === t.idx);
      return `<li class="turn ${t.speaker === "bot" ? "bot" : "user"}" id="turn-${esc(t.idx)}" tabindex="-1">
        <span class="mono ts">${V.clock(t.start_sec)}</span><span class="spk">${esc(V.speaker(t.speaker))}</span>
        <p class="text">${esc(t.text)}</p>${notes.length ? `<div class="notes">${notes.map(note).join("")}</div>` : ""}</li>`;
    }).join("");
  }

  // Worst first: Fatal, Non-fatal, Unknown or Needs review, Pass, then Not applicable.
  const RANK = { Fatal: 0, NonFatal: 1, Unknown: 2, NeedsReview: 2, Pass: 3, NotApplicable: 4 };
  const rank = (s) => V.own(RANK, s) ?? 2;

  function checks(findings) {
    if (!findings.length) return `<p class="empty">No checks were recorded for this call.</p>`;
    const sorted = [...findings].sort((a, b) => rank(a.status) - rank(b.status));
    return `<ul>${sorted.map((f) => `<li>
      <span class="dot t-${esc(V.tone(f.status))}"></span><span class="cname">${esc(V.name(f.parameter))}</span>
      <span class="cstat">${esc(V.word(f.status))}</span><p class="creason" title="${esc(f.reason)}">${esc(f.reason)}</p></li>`).join("")}</ul>`;
  }

  VIEWS.call = {
    async render(el, { id }, signal) {
      const back = `<a class="back" href="#/">← All calls</a>`;
      let data;
      try {
        data = await V.get(`/api/call/${encodeURIComponent(id)}`, signal);
      } catch (e) {
        if (signal?.aborted) return;
        if (e.status !== 404) throw e;
        el.innerHTML = `${back}<p class="empty">No graded call has the id <code>${esc(id)}</code>.</p>`;
        return;
      }
      if (signal?.aborted) return;
      // A turn index goes into ids and data attributes, so only integer indices are kept.
      const call = data.call && Array.isArray(data.call.turns)
        ? { ...data.call, turns: data.call.turns.filter((t) => t && Number.isInteger(t.idx)) }
        : null;
      const audit = data.audit || {};
      const findings = (audit.findings || []).filter((f) => f && typeof f === "object");
      const faults = findings.filter((f) => Number.isInteger(f.onset_turn) && f.status !== "Pass");
      // Unknown length shows "–", never a made-up 0:01.
      const ends = call ? call.turns.map((t) => num(t.end_sec) ?? 0) : [];
      const known = Math.max(0, num(data.call?.duration_sec) ?? 0, ...ends) || null;
      const dur = Math.max(1, known ?? 0);
      const turns = call?.turns.length;
      el.innerHTML = `${back}
        <header class="call-head">
          <h1 class="call-id mono">${esc(id)}</h1>
          <p class="call-meta"><span class="mono">${V.clock(known)}</span>
            <span>${esc(V.scope(audit.scope))}</span>${audit.verdict ? V.chip(audit.verdict) : ""}</p>
          ${audit.review_reason ? `<p class="review"><span class="label">Why it needs review</span>${esc(audit.review_reason)}</p>` : ""}
        </header>
        ${!call ? `<p class="empty">This call has no transcript in calls.jsonl, so there is no tape or script.</p>`
          : turns ? tape(call, faults, dur) : `<p class="empty">No transcript turns for this call.</p>`}
        <div class="call-body">
          <section class="script" aria-label="Script">${turns ? `<h2 class="label">Script</h2><ol>${script(call, faults)}</ol>` : ""}</section>
          <aside class="checks" aria-label="Checks"><h2 class="label">Checks</h2>${checks(findings)}</aside>
        </div>`;

      const turnEl = (n) => el.querySelector(`#turn-${CSS.escape(n.dataset.turn)}`);
      const mark = (n, on) => {
        turnEl(n)?.classList.toggle("hl", on);
        el.querySelector(`.seg[data-turn="${CSS.escape(n.dataset.turn)}"]`)?.classList.toggle("hl", on);
      };
      // Listeners and the observer end when the router aborts this render (a route change).
      const on = { signal };
      el.querySelectorAll(".notch").forEach((n) => {
        n.addEventListener("pointerenter", () => mark(n, true), on);
        n.addEventListener("pointerleave", () => mark(n, false), on);
        n.addEventListener("focus", () => mark(n, true), on);
        n.addEventListener("blur", () => mark(n, false), on);
        n.addEventListener("click", () => {
          const row = turnEl(n);
          if (!row) return;
          row.focus({ preventScroll: true });
          row.scrollIntoView({ behavior: V.reduced() ? "auto" : "smooth", block: "center" });
        }, on);
      });

      if ("IntersectionObserver" in window && !V.reduced()) {
        const io = new IntersectionObserver((entries) => entries.forEach((e) => {
          if (!e.isIntersecting) return;
          e.target.classList.remove("rest");
          io.unobserve(e.target);
        }), { rootMargin: "0px 0px -8% 0px" });
        el.querySelectorAll(".note").forEach((n) => { n.classList.add("rest"); io.observe(n); });
        signal?.addEventListener("abort", () => io.disconnect(), { once: true });
      }
    },
  };
})();
