# vaniloop web UI: design and build spec

## Subject

- **Who uses it:** a QA lead at IndiaMART, and the hackathon judges.
- **What it shows:** how every VANI call was graded, why it failed, who owns each fault, and whether a fix is proven.
- **Where it runs:** locally only, at `http://127.0.0.1:8765`. It reads `runs/` (or the folder in `VANILOOP_DATA`). It never publishes data anywhere.
- **Look:** minimal and luxurious, like a mastering studio for voice. Quiet, precise, with one signature moment.

## Tokens (use only these)

```css
:root{
  --night:#111316;   /* page ground */
  --slate:#191C20;   /* raised surface */
  --seam:#2A2F35;    /* hairlines, borders */
  --ivory:#ECE7DD;   /* primary text */
  --ash:#8E9196;     /* secondary text */
  --champagne:#CDB58A; /* the one accent: active nav, focus, playhead, wordmark dot */
  --fatal:#C0606A; --nonfatal:#CF9F5B; --review:#8396C4; --pass:#86AF95;  /* verdicts, muted jewels */
  --display:"Bodoni Moda", "Didot", Georgia, serif;          /* wordmark, big numerals, view titles only */
  --body:"Hanken Grotesk", system-ui, -apple-system, sans-serif;
  --mono:"IBM Plex Mono", ui-monospace, Menlo, monospace;    /* timecodes, ids, small labels */
  --ease:cubic-bezier(.2,.7,.1,1);
}
```

- **Fonts:** load from Google Fonts with one link: Bodoni Moda (opsz, 400 and 500, italic 400), Hanken Grotesk 400/500/600, IBM Plex Mono 400/500. Give each a real fallback stack.
- **Type scale:**
  - Hero sentence: Bodoni Moda 56px/1.1, letter-spacing -0.01em.
  - View titles: Bodoni 40px.
  - Big numerals: Bodoni 48px, tabular.
  - Body: 15px/1.6.
  - Labels: mono 11px, uppercase, 0.14em tracking.
- **Spacing:** 8px base. Page side gutter 40px, 20px under 720px. Max content width 1240px.
- **Corners:** 2px radius everywhere. Rows are separated by hairlines (1px var(--seam)). No cards with shadows, no gradients, no glow, no emoji.

## Layout

```
┌──────────────────────────────────────────────────────────────┐
│ vaniloop·  VANI call quality        Calls  Causes  Fixes     │  header, 72px, hairline below
├──────────────────────────────────────────────────────────────┤
│ 713 calls heard. 52 carry a Fatal fault.                     │  hero sentence, numbers from data
│ ▁▂▅▃▁▇▂▃▅▁▂▃ … (the spectrum: one hairline bar per call) …   │  signature, 180px tall, full width
│ All 713  ● Fatal 52  ● Non-fatal 301  ● Review 88  ● Pass 272│  filter tabs = legend: dot, word, mono count
│ 133_9085…   01:32   lead call   Fatal   Looping: asked city 4×│  call rows, hairline-separated
└──────────────────────────────────────────────────────────────┘
```

### Views (hash routing, no framework)

1. `#/`: **Calls**. The hero sentence, the spectrum, the filter tabs, and the call list. The legend lives in the filter tabs: each tab shows the verdict dot, the word and its count in mono, and "All" shows the total. A tab also filters the spectrum. The list is virtualised or paged at 100 rows, with a "Show 100 more" button.
2. `#/call/<call_id>`: **Call**.
   - Back link "← All calls".
   - Title: the short call id in mono, then duration, scope and the verdict chip.
   - **The tape:** a 120px band. VANI turns are segments above a centre hairline and buyer turns below it, mirrored like a waveform, positioned by start and end seconds. Silences over 5 s are shaded gaps. Each fault is a notch on the tape (garnet for Fatal, amber for Non-fatal) at its onset turn.
   - **The script:** below the tape. Each turn is one row: mono timestamp, a small-caps speaker label (VANI or Buyer), then the text. When a finding has its onset on a turn, a margin note sits to the right of that turn: parameter name, status, reason and basis.
   - **The checks:** a side column with all checks. Each has a status dot, a human name, the status, and the reason in one line.
   - Hovering a notch highlights its turn. Clicking it scrolls to the turn.
3. `#/causes`: **Causes**, ranked as in causes.json.
   - Each row: human name, owner chip and evidence (only if the fields exist), the number of calls, a thin two-tone bar for Fatal vs Non-fatal roots, and the status in words.
   - Click a row to expand: the description, then the member call ids as links to `#/call/<id>`.
4. `#/fixes`: **Fixes**, one row per gate run.
   - Each row: the cause, the attempt number, and the decision chip ("Shipped", "Rejected" or "Not sure").
   - Old vs new fail rate as two thin horizontal bars on one shared scale, the p-value in mono, and the reason in one line.
   - Above the rows, one honest line built from the data, for example "10 fixes tested. 0 shipped."

## Human names for checks (use everywhere)

| Parameter | Human name |
|---|---|
| product_change | Right product |
| category_isq | Right questions |
| qty_probing | Quantity asked |
| qty_missed | Not asked twice |
| qty_accuracy | Right number |
| qty_unit | Right unit |
| looping | No looping |
| dead_air | No dead air |
| intent | Buying intent |

Unknown parameters show their raw name.

Verdict words:

| Data value | Shown as |
|---|---|
| Fatal | Fatal |
| NonFatal | Non-fatal |
| NeedsReview | Needs review |
| Pass | Pass |

## Motion (choreographed, short; honour prefers-reduced-motion by showing the end state)

- **Spectrum on load:** the bars rise from 0 to their height, staggered left to right over 900ms total, with 420ms per bar on --ease. Then one champagne playhead hairline sweeps across once over 1.4s and fades.
- **Hover a bar:** the bar lifts 6px and brightens. A small tooltip shows the call id, duration and verdict. Click opens the call.
- **Filter change:** bars not in the filter fade to 15% opacity over 240ms. List rows cross-fade.
- **View change:** the old view fades out over 120ms. The new view fades in and rises 12px over 320ms.
- **Call tape:** segments draw left to right over 700ms. Notches pop in afterwards, staggered 60ms. Margin notes fade in when their turn scrolls into view (IntersectionObserver), each from a visible resting state. Content must never stay hidden if the observer does not fire.
- **Causes:** a row expands by animating height over 260ms.
- **Fixes:** the rate bars grow from 0 to their width over 500ms on view entry.

## Copy

- Plain sentence case. Active voice.
- Empty state for a missing runs file: "No graded calls yet. Run `uv run python -m vaniloop.pipeline audit` to grade calls."
- Never claim what the data does not show.

## API (served by `src/vaniloop/web.py`, stdlib only)

- `GET /api/summary` →
  `{"total":int, "verdicts":{"Fatal":n,"NonFatal":n,"NeedsReview":n,"Pass":n}, "scopes":{...}, "updated_ist":"HH:MM DD Mon", "spend_usd":float|null}`
- `GET /api/calls` →
  `[{"call_id","verdict","duration_sec","scope","first_fault":{"parameter","status","reason"}|null}]`, sorted by verdict severity, then duration descending.
- `GET /api/call/<id>` →
  `{"call": <CallRecord as in runs/calls.jsonl>, "audit": <AuditRecord as in runs/audits.jsonl>}`, or 404 JSON `{"error":"..."}`.
- `GET /api/causes` → the contents of `runs/causes.json`, or `{"causes":[]}`.
- `GET /api/gates` → the rows of `runs/gates.jsonl`, in file order.
- Static: `/` serves `src/vaniloop/web/index.html`. `/static/<file>` serves files from `src/vaniloop/web/`. Reject path traversal.
- Bind to 127.0.0.1 only. CLI: `uv run python -m vaniloop.web [--port 8765]`. It prints the URL.
- Use `vaniloop.jsonl.read_jsonl` and `dedupe_by_call_id` as dashboard.py does. The data dir is `VANILOOP_DATA` or `runs/`.

## Files

- `src/vaniloop/web.py`: the server.
- `src/vaniloop/web/index.html`: a shell with `<title>vaniloop</title>`, the font link, and `<main id="view">`.
- `src/vaniloop/web/app.css`: tokens, base, header and shared components.
- `src/vaniloop/web/app.js`: router, fetch helpers, human names, shared helpers.
- `src/vaniloop/web/views/calls.js`, `call.js`, `causes.js`, `fixes.js`: one view each. Each exports `render(el, params)` via `window.VIEWS[name]`.
- `src/vaniloop/web/views/*.css`, if a view needs its own styles.
- Synthetic fixture: `tests/fixtures/web_runs/`. About 14 synthetic calls with placeholder names (BuyerA), covering all verdicts, plus causes.json and gates.jsonl. No real data.
