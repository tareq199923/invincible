"""Eval launcher: ``run`` / ``compare`` / ``merge`` / ``list``.

Reads ``os.environ`` for EVAL_* like ``tools/replay_payload.py`` does.
Never prints passwords or cookies.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from contextlib import asynccontextmanager
from datetime import datetime, timezone

import httpx

try:
    from tools.eval import report, runner
    from tools.eval import tasks as task_schema
except ImportError:
    # Script mode (`python tools/eval/run_eval.py`): the repo root is not
    # on sys.path, so `tools.eval` is unimportable. Tests import via the
    # `tools.eval` package path (pytest.ini `pythonpath = .`) and never
    # hit this branch.
    import sys
    from pathlib import Path as _Path

    sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))
    from tools.eval import report, runner
    from tools.eval import tasks as task_schema

DEFAULT_PORT = 8000


def base_url_default() -> str:
    return os.environ.get("EVAL_BASE_URL", f"http://127.0.0.1:{DEFAULT_PORT}")


def _redacted_env() -> str:
    # Confirm presence without ever echoing values.
    email = "set" if os.environ.get("EVAL_EMAIL") else "missing"
    pw = "set" if os.environ.get("EVAL_PASSWORD") else "missing"
    cookie = "set" if os.environ.get("EVAL_SESSION_COOKIE") else "missing"
    return f"EVAL_EMAIL={email} EVAL_PASSWORD={pw} EVAL_SESSION_COOKIE={cookie}"


def cmd_list(_args: argparse.Namespace) -> int:
    try:
        found = task_schema.load_tasks(runner.TASK_DIR)
    except ValueError as e:
        print(f"task load failed: {e}")
        return 2
    print(f"{len(found)} tasks in {runner.TASK_DIR}:")
    for task in found:
        tags = f" [{','.join(task.tags)}]" if task.tags else ""
        print(f"  {task.id:<28} {task.category:<8}{tags}")
    return 0


def cmd_compare(args: argparse.Namespace) -> int:
    try:
        with open(args.a, encoding="utf-8") as fh:
            a = json.load(fh)
        with open(args.b, encoding="utf-8") as fh:
            b = json.load(fh)
    except (OSError, ValueError) as e:
        print(f"compare failed: {e}")
        return 2
    rows = report.compare_summaries(a.get("summary", {}), b.get("summary", {}))
    label_a = a.get("meta", {}).get("label", args.a)
    label_b = b.get("meta", {}).get("label", args.b)
    print(f"base={label_a}  other={label_b}")
    print(report.render_compare(rows))
    regressed = [r for r in rows if r["status"] == "REGRESSED"]
    if regressed:
        print(f"\n{len(regressed)} regressed: "
              + ", ".join(r["task"] for r in regressed))
        return 1
    return 0


async def _run_async(args: argparse.Namespace) -> int:
    try:
        found = task_schema.load_tasks(runner.TASK_DIR)
    except ValueError as e:
        print(f"task load failed: {e}")
        return 2
    selected = task_schema.filter_tasks(
        found, task_id=args.task, category=args.category,
        include_memory=args.include_memory, tag=args.tag,
    )
    if args.task and not selected:
        print(f"no task {args.task!r} (after filters). See `list`.")
        return 2
    if not selected:
        print("no tasks selected (filters excluded everything).")
        return 2
    total = len(selected) * args.repeat
    print(f"{len(selected)} tasks x {args.repeat} repeats "
          f"= {total} agent runs (model={args.model})")
    if total > 30 and not args.yes:
        print("M > 30: re-run with --yes to confirm the spend.")
        return 2
    if not args.model:
        print("--model is required (e.g. --model gemini-2.5-flash).")
        return 2
    base_url = args.base_url
    print(f"server: {base_url}  auth: {_redacted_env()}")

    email = os.environ.get("EVAL_EMAIL")
    password = os.environ.get("EVAL_PASSWORD")
    session_cookie = os.environ.get("EVAL_SESSION_COOKIE")

    @asynccontextmanager
    async def factory():
        async with httpx.AsyncClient(cookies=httpx.Cookies()) as client:
            await runner.login(
                client, base_url, email, password, session_cookie)
            await runner.preflight(client, base_url, args.model)
            yield client

    # Fail fast BEFORE any agent run: one preflight on a throwaway client.
    # The numeric user id confirms WHICH account the eval authenticated
    # as (catches "providers connected under the wrong account" instantly).
    @asynccontextmanager
    async def probe():
        async with httpx.AsyncClient(cookies=httpx.Cookies()) as client:
            await runner.login(
                client, base_url, email, password, session_cookie)
            ids, me_id = await runner.preflight(client, base_url, args.model)
            print(f"auth ok: user_id={me_id} models={ids}")
            yield client

    async with probe():
        pass

    escape_report: dict = {}
    runs = await runner.run_all(
        base_url, args.model, selected, repeat=args.repeat,
        concurrency=args.concurrency, keep_workspace=args.keep_workspace,
        delay_seconds=args.delay_seconds,
        client_factory=factory,
        escape_report=escape_report,
    )
    summary, overall = report.summarize_runs(runs)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_path = runner.RESULTS_DIR / f"{stamp}-{args.label}.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "meta": {
            "label": args.label,
            "base_url": base_url,
            "model": args.model,
            "repeat": args.repeat,
            "git": runner.git_meta(),
            "escape_detection": {
                "disabled": escape_report.get("disabled", False),
                "batch_escapes": escape_report.get("batch_escapes", []),
            },
        },
        "summary": summary,
        "overall": overall,
        "runs": runs,
    }
    try:
        runner.check_no_secrets(payload)
    except runner.EvalError as e:
        print(str(e))
        return 2
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2)
    print(report.render_table(summary, overall))
    print(f"\nsaved {out_path}")
    if runner.escape_exit_code(runs, escape_report) != 0:
        print("ESCAPE DETECTED: one or more runs wrote outside "
              ".eval_workspace/ and eval_results/ - see escaped_files / "
              "meta.escape_detection. Failing the batch.")
        return 1
    return 0


def cmd_merge(args: argparse.Namespace) -> int:
    """Combine result files into one, dropping infra-failed runs.

    Guards: every input must be the same model (a cross-model baseline
    is meaningless), and every task must keep >=1 genuine run.
    """
    payloads = []
    for path in args.files:
        try:
            with open(path, encoding="utf-8") as fh:
                payloads.append((path, json.load(fh)))
        except (OSError, ValueError) as e:
            print(f"merge failed: cannot read {path}: {e}")
            return 2
    models = {p.get("meta", {}).get("model") for _, p in payloads}
    if len(models) != 1 or None in models:
        found = sorted(m for m in models if m)
        print(f"merge refused: files disagree on model: {found}")
        return 2
    model = models.pop()

    kept: list[dict] = []
    dropped = 0
    for _, payload in payloads:
        for run in payload.get("runs", []):
            if run.get("error"):
                dropped += 1
            else:
                kept.append(run)
    if not kept:
        print("merge refused: every run is an infra failure.")
        return 2

    by_task: dict[str, int] = {}
    for run in kept:
        by_task[run["task_id"]] = by_task.get(run["task_id"], 0) + 1
    empty = sorted({
        run["task_id"] for _, payload in payloads
        for run in payload.get("runs", [])
    } - set(by_task))
    if empty:
        print("merge refused: zero genuine runs remain for: "
              + ", ".join(empty))
        return 2
    uneven = {t: n for t, n in sorted(by_task.items())
              if n != max(by_task.values())}
    if uneven:
        note = ", ".join(f"{t}={n}" for t, n in uneven.items())
        print(f"note: uneven coverage (top-up incomplete?): {note}")

    summary, overall = report.summarize_runs(kept)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_path = runner.RESULTS_DIR / f"{stamp}-{args.label}.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "meta": {
            "label": args.label,
            "model": model,
            "merged_from": [path for path, _ in payloads],
            "git": runner.git_meta(),
        },
        "summary": summary,
        "overall": overall,
        "runs": kept,
    }
    try:
        runner.check_no_secrets(payload)
    except runner.EvalError as e:
        print(str(e))
        return 2
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2)
    print(report.render_table(summary, overall))
    print(f"\nmerged {len(kept)} runs "
          f"({dropped} infra failures dropped) from {len(payloads)} files")
    print(f"saved {out_path}")
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    try:
        return asyncio.run(_run_async(args))
    except runner.EvalError as e:
        print(f"eval stopped: {e}")
        return 2
    except KeyboardInterrupt:
        print("interrupted.")
        return 130


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="run_eval.py",
        description="Webchat agent eval: run fixed tasks, grade, compare.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_run = sub.add_parser("run", help="run tasks against the live server")
    p_run.add_argument("--label", required=True)
    p_run.add_argument("--model", required=True)
    p_run.add_argument("--repeat", type=int, default=3)
    p_run.add_argument("--task", default=None)
    p_run.add_argument("--category", default=None,
                       choices=["read", "write", "safety"])
    p_run.add_argument("--tag", default=None,
                       help="only run tasks carrying this tag (e.g. hard)")
    p_run.add_argument("--include-memory", action="store_true")
    p_run.add_argument("--keep-workspace", action="store_true")
    p_run.add_argument("--concurrency", type=int, default=1)
    p_run.add_argument("--delay-seconds", type=float, default=0.0,
                       help="pause between consecutive runs "
                            "(free-tier per-minute limits)")
    p_run.add_argument("--yes", action="store_true")
    p_run.add_argument("--base-url", default=None)
    p_run.set_defaults(func=cmd_run)

    p_cmp = sub.add_parser("compare", help="diff two result files")
    p_cmp.add_argument("a")
    p_cmp.add_argument("b")
    p_cmp.set_defaults(func=cmd_compare)

    p_mrg = sub.add_parser(
        "merge",
        help="combine result files (drops infra-failed runs; "
             "same model only)")
    p_mrg.add_argument("--label", required=True)
    p_mrg.add_argument("files", nargs="+")
    p_mrg.set_defaults(func=cmd_merge)

    p_list = sub.add_parser("list", help="list tasks (no server needed)")
    p_list.set_defaults(func=cmd_list)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if getattr(args, "base_url", None) is None and args.command == "run":
        args.base_url = base_url_default()
    if args.command == "run" and args.repeat < 1:
        print("--repeat must be >= 1.")
        return 2
    if args.command == "run" and args.delay_seconds < 0:
        print("--delay-seconds must be >= 0.")
        return 2
    func = args.func
    if args.command == "run":
        return func(args)
    return func(args)


if __name__ == "__main__":
    sys.exit(main())
