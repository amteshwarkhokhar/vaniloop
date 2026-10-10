# Console API contract

Source of truth for the console backend (`src/vaniloop/console.py`, `src/vaniloop/web.py`). Shared by the rubric (#125), pipeline (#127), web (#128) and UI (#129) work. Submit, rubric and tune endpoints land in the second PR of #128.

The console is local only (127.0.0.1). It reads and writes the data folder `data_dir()` (VANILOOP_DATA, default runs/).

### Security, for every POST and PUT
- `Content-Type: application/json` is required. Otherwise 415.
- An `Origin` header is required, and its host must be in ALLOWED_HOSTS with the server's port. Otherwise 403. This blocks drive-by requests from other sites.
- The body is at most 30 MB. Otherwise 413.
- Read-only mode: when `data_dir()` resolves inside `<repo>/.cache/` (for example the demo freeze), every POST and PUT returns 403 `{"error": "read-only data folder"}`.

### GET /api/console
`{read_only: bool, data_dir_name: str, busy_job_id: str|null, stages: [{id, label, paid: bool, args: [{name, type: "int"|"bool"|"str"|"call_ids", required: bool, default, choices?: [str]}]}]}`

### Stages
The server builds argv from validated args only. It never passes client strings to a shell. Each job runs `uv run python -m vaniloop.pipeline --runs <data_dir> <stage> ...` with stdin from /dev/null. It runs in its own process group, with `VANILOOP_RUBRIC=<data_dir>/rubric.json`.

| id | args | paid |
|---|---|---|
| audit | calls?: [call_id], limit?: int 1..1000 | yes (Gemini) |
| judge | calls?: [call_id] | no (Jev, logged at $0) |
| regrade | none. The server picks ledgers_jev.jsonl when it exists, else ledgers.jsonl. | no |
| issues | none | no |
| agreement | none | no |
| fix | top?: int 1..8 = 3, holdout?: bool = true | yes |
| prove | cause: str (must be a cause_id in causes.json), n?: int 2..20 = 8, k?: int 1..5 = 3, attempt?: int 1..2 = 1, holdout?: bool = true, screen?: bool = true | yes |
| ship | cause: str (must be a cause_id in causes.json and in state ready_to_ship). Needs `"confirm": true`. Runs `pipeline ship --cause`, which makes a new prompt version. | no (changes the live prompt) |
| submit | submission_id: str (from POST /api/submit) | yes |

Stage `ship` needs `"confirm": true` too: 400 `{"error": "confirm"}` without it, and 409 `{"error": "<cause> is <state>, not ready_to_ship"}` when the tracker state is not ready_to_ship. A paid stage needs `"confirm": true` in the body. Otherwise the server returns 400 `{"error": "confirm", "estimate_usd": number|null}`.

### Jobs
- `POST /api/jobs {stage, args, confirm?}`. Responses:
  - 202 `{job}`;
  - 400 `{error, field}`;
  - 409 `{error: "busy", job_id}`, because only one job runs at a time.
- The job object: `{id, stage, args, status: "running"|"done"|"failed"|"cancelled", started_ist: "HH:MM:SS DD Mon", ended_ist|null, exit_code|null, cost_usd|null, log_lines: int}`.
  - `cost_usd` is the sum of the llm.COSTS_PATH rows between start and end, without reserve rows.
- Jobs persist in `<data>/console/jobs.jsonl`, with the last row per id winning. On restart, a job whose process is gone becomes "failed".
- `GET /api/jobs` returns `{jobs: [job]}`, newest first, at most 50.
- `GET /api/jobs/<id>/log?from=<n>` returns `{job, lines: [str], next: int}`.
  - The log file is `<data>/console/jobs/<id>.log`.
  - ANSI codes are stripped.
  - A line that looks like a secret (`sk-`, `AIza`, `Bearer `, `api_key=`) is masked.
- `POST /api/jobs/<id>/cancel` sends SIGTERM to the process group. It returns 200 `{job}`.

### Submit calls
- `POST /api/submit {files?: [{name, data_b64}], call_ids?: [str]}` returns 201 `{submission_id, files: [{name, kind: "mp3"|"transcript"}], call_ids}`.
  - Each name must match `^[A-Za-z0-9._-]{1,120}\.(mp3|json)$`.
  - At most 20 files, each at most 25 MB.
  - Each call_id must exist in `<data>/calls.jsonl`.
  - At least one file or one call_id is required.
  - The files go to `<data>/console/submissions/<sid>/inbox/`. The client then sends POST /api/jobs `{stage: "submit", args: {submission_id}, confirm: true}`.
- `GET /api/submit/<sid>` returns `{submission_id, status, calls: [{call_id, source: "upload"|"existing", verdict|null, scope|null, findings: [{parameter, status, reason, onset_turn}], flags: [str], error|null}]}`.
  - It reads `<sid>/audits.jsonl` and `<sid>/errors.jsonl`.
- `GET /api/submit` returns `{submissions: [{submission_id, created_ist, calls: int}]}`, newest first.

### Rubric
- `GET /api/rubric` returns `rubric.describe(path)` (see #125).
- `PUT /api/rubric {params: {id: value|null}, questions: {id: {instructions, true: {description, examples}, false: {description, examples}}|null}}`. A null value removes the override.
  - It returns 200 with the new describe, or 400 `{error, field}`.

### Tune
- `POST /api/tune {thresholds: {question_fits: {true_at, false_at}, option_fits: {true_at, false_at}, product_match: {true_at, false_at}, name_check_drop_at, frustrated_p}}` returns 200 `tune.score(...)` (see #126).
  - It returns 409 `{error: "no raw Jev answers: run judge"}` when `<data>/jev_raw.jsonl` is missing.
  - It returns 400 for a bad value.


### Implementation notes
- Check order for every POST and PUT: Host, Origin, read-only folder, content type, body size, JSON object.
- `build_argv` in `console.py` is the only place that makes argv. It gets validated values only.
- Stage `agreement` runs the pipeline command `judge-agreement`.
- `estimate_usd` is always null for now: there is no estimator.
- A job row also keeps `pid`, `started_utc` and `ended_utc` in jobs.jsonl. The API hides them.
- `--calls` takes one comma-separated value. Stage `submit` runs with `--runs <data>/console/submissions/<sid>` and `--inbox <sid>/inbox --from <data>`. Its `--calls` come from `meta.json` in the submission folder.
- `fix`, `prove` and `ship` pass `--prompt data/reference/vani_prompt_sarvam.md`. When it is missing the job is refused: 400 `{error: "VANI prompt file missing", field: "prompt"}`.

### Tracker (#130)
- `GET /api/tracker` returns `{causes: [...], prompt_versions: [...], current_prompt_version}`. Each cause has `state`, `route`, `counts` and `events`. An event that skipped its prerequisite state has `ignored: true`. `prompt_versions_error` is set when `prompts/versions.json` is malformed.
- `POST /api/tracker/<cause_id>/ticket {action: "sent", ref: str 1..200} | {action: "resolved", note: str}` returns 200 `{cause}`, 400 for a bad body, 409 `{error}` for a wrong state and 404 for an unknown cause.
- `POST /api/tracker/<cause_id>/ship {confirm: true}` starts the `ship` job and returns 202 `{job}`. The UI polls the job. 409 `{error}` when the state is not ready_to_ship, 409 `{error: "busy", job_id}` when another job runs, 400 `{error: "confirm"}` without confirm.
- Both POSTs use the same security checks as every POST (see Security).
