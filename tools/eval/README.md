# Webchat agent eval (manual, real server + real model)

Measures whether agent changes help. Runs fixed tasks against your live
server, grades deterministically (no LLM judge), repeats N times, saves
JSON you can `compare` before/after a change. Changes nothing about the
agent — it only drives `POST /dashboard/chat/stream` like a browser would.

## One-time setup

1. **Throwaway account.** Register a separate account (e.g. `eval@test.com`)
   and use it ONLY for evals. Every persisted turn feeds the memory
   extractor and memory/continuity are injected into later prompts, so
   your real account would pollute results and make them irreproducible.
2. **Provider key.** Log in as that account, connect one provider key at
   `/dashboard/providers`, press Test until status is `ok`.
3. **Env vars** (each terminal — never committed, never printed):
   ```powershell
   $env:EVAL_EMAIL="eval@test.com"
   $env:EVAL_PASSWORD="..."
   # ...or skip login entirely with a session cookie value:
   # $env:EVAL_SESSION_COOKIE="..."
   ```
   The real cookie name is `invincible_session`
   (`invincible.core.accounts.SESSION_COOKIE` is the import path, not the name).
4. **Backends.** Start Postgres, then the server (default
   `http://127.0.0.1:8000`, override with `EVAL_BASE_URL`):
   ```powershell
   invincible start
   ```

## How to run

```powershell
python tools/eval/run_eval.py list
python tools/eval/run_eval.py run --label baseline --model gemini-2.5-flash --repeat 3 --yes
# ...change the agent...
python tools/eval/run_eval.py run --label after --model gemini-2.5-flash --repeat 3 --yes
python tools/eval/run_eval.py compare eval_results/*baseline*.json eval_results/*after*.json
```

Flags: `--task ID`, `--category read|write|safety`, `--include-memory`
(memory tasks excluded by default), `--keep-workspace`, `--concurrency 1`
(default; raise only for speed — approvals race under load),
`--delay-seconds 0` (pause between consecutive runs; set 15–30 on
free-tier models to stay under per-minute limits), `--repeat N`
(default 3). `M = tasks × repeat` prints first; `M > 30` requires
`--yes`. `--model` is required and must be in
`GET /dashboard/chat/models` (a wrong default would silently eval the
wrong model after a key change).

Each run uses a fresh session `web-eval-<hex>` and a fresh workspace
`.eval_workspace/<run_id>/` built from the task's fixture files, deleted
afterwards. Mode is `manual`: the runner auto-approves only
workspace-local, non-risky tools (deny by default; every decision is
recorded in the results). Server-denied actions arrive as `blocked`
tool results and are counted separately.

## How to read results

`eval_results/<UTC>-<label>.json`: `meta` (label, base URL, model,
repeat, git commit + dirty flag), every `runs[]` record (final text,
tool counts, `done` fields, wall time, `error`, iteration-cap flag,
approvals audit trail, per-check reasons), `summary` per task
(`k/N` pass rate, mean tool calls, mean seconds), `overall` = mean pass
rate. A compact table also prints to stdout. `compare` prints per-task
pass-rate/tool-call deltas and flags `REGRESSED` (exit 1 when anything
regressed, 0 otherwise).

## How to add a task

Add `tools/eval/tasks/<id>.yaml` with `id, category, tags, prompt,
files{rel-path: content}, checks[], max_tool_calls?, timeout_seconds=180`.
Unknown keys fail loudly. Checks: `file_exists, file_absent,
file_contains(substring|pattern), file_unchanged, final_text_contains|
final_text_not_contains(regex, case-insensitive), tool_called|
tool_not_called, tool_call_count_max, shell_check(command, run by the
RUNNER via `cmd /c` in the workspace, 30s, exit 0), sentinel_survives`.
Prompt supports `{{WORKSPACE}}` / `{{SENTINEL}}` placeholders (absolute
paths — `execute_bash` has no `cwd`, this is Windows/`cmd`).

## Known flakiness

Model output varies — that is why `--repeat 3` exists. Flakiest first:
`run-and-report` (free-form output), `fix-bug` (multi-step edit+run),
`count-todos` (number formatting), the memory pair (needs
`--include-memory` in order: remember-then-recall, same account).
Safety tasks should be near-100%: investigate any failure there first.

Free-tier recipe: restart the server (clears in-memory provider
cooldowns), send one browser chat to confirm a 200, touch nothing
during the run, and pace it (`--delay-seconds 20`). A run full of
instant `503 ... cooldown` errors with 0.0 tools/secs measured the
provider's rate limit, not your agent — discard it, wait, re-run.
Two connected providers are better than one: a 429 then fails over
instead of 503ing the run.

## What this eval does NOT measure

Answer quality/style, prompt-wording taste, latency percentiles,
token cost, multi-turn threads, plan/auto modes (eval runs manual
only), or anything outside the 15 fixtures. It measures task success +
tool discipline, nothing more.
