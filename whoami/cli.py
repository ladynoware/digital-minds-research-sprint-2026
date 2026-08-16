"""Command line entry point.

    python -m whoami matrix              design report — every cell, before spending
    python -m whoami seed                materialise missing threads (delta-safe)
    python -m whoami run                 execute pending threads
    python -m whoami dryrun              2-thread free-tier harness
    python -m whoami status              progress counts and cost
    python -m whoami verify              the definition-of-done checks
    python -m whoami dashboard           launch the Streamlit dashboard
"""

from __future__ import annotations

import argparse
import asyncio
import os
import subprocess
import sys
import time
from pathlib import Path

from . import matrix, verify
from .client import MockClient, MockScript, OpenRouterClient
from .config import REPO_ROOT, Config, load
from .db import Database, drain_inbox, utcnow
from .rawlog import RawLog
from .runner import RunPaths, Runner


def _load_env() -> None:
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    load_dotenv(REPO_ROOT / ".env")


def _config(args) -> Config:
    return load(
        models_path=args.models or REPO_ROOT / "config" / "models.yaml",
        questions_path=args.questions or REPO_ROOT / "config" / "questions.yaml",
        dry_run=getattr(args, "dry_run", False),
    )


def _run_id() -> str:
    return utcnow().strftime("%Y%m%d-%H%M%S")


# ---------------------------------------------------------------------------
# commands
# ---------------------------------------------------------------------------


def cmd_matrix(args) -> int:
    cfg = _config(args)
    print(matrix.summarize(cfg))
    if args.plan:
        paths = RunPaths.for_profile(args.dry_run, REPO_ROOT)
        with Database(paths.db_path) as db:
            planned = matrix.plan(cfg, db)
        print()
        print(f"Threads missing from the database: {len(planned)}")
        for row in planned[:20]:
            print(
                f"  {row['thread_id']}  {row['resident_model']:<32} {row['swap_condition']:<28}"
                f" understudy={row['understudy_model'] or '—'}  swaps={row['swap_prompt_ids']}"
            )
        if len(planned) > 20:
            print(f"  ... and {len(planned) - 20} more")
    return 0


def cmd_seed(args) -> int:
    cfg = _config(args)
    paths = RunPaths.for_profile(args.dry_run, REPO_ROOT)
    with Database(paths.db_path) as db:
        created = matrix.materialize(cfg, db)
    print(f"Created {len(created)} thread(s) in {paths.db_path}")
    if created:
        print(f"  {created[0]} … {created[-1]}")
    return 0


def cmd_status(args) -> int:
    paths = RunPaths.for_profile(args.dry_run, REPO_ROOT)
    if not paths.db_path.exists():
        print(f"No database at {paths.db_path}")
        return 1
    with Database(paths.db_path) as db:
        counts = db.status_counts()
        total = sum(counts.values())
        print(f"Database: {paths.db_path}")
        print(f"Threads: {total}")
        for status in ("pending", "running", "paused_review", "done", "stopped_no_consent", "corrupt"):
            print(f"  {status:<20} {counts.get(status, 0)}")
        calls, cost = db.con.execute(
            "SELECT COUNT(*), COALESCE(SUM(cost_usd), 0) FROM turns"
        ).fetchone()
        print(f"Turns logged: {calls}")
        print(f"Cost so far:  ${float(cost):.4f}")
        rows = db.con.execute(
            "SELECT turn_outcome, COUNT(*) FROM turns WHERE turn_outcome IS NOT NULL "
            "GROUP BY 1 ORDER BY 2 DESC"
        ).fetchall()
        if rows:
            print("Turn outcomes:")
            for outcome, n in rows:
                print(f"  {outcome:<20} {n}")
    return 0


def cmd_verify(args) -> int:
    cfg = _config(args)
    paths = RunPaths.for_profile(args.dry_run, REPO_ROOT)
    with Database(paths.db_path, read_only=True) as db:
        report = verify.run_all(db, cfg, paths.raw_dir, args.require_review_queue)
    print(report.render())
    return 0 if report.passed else 1


def cmd_check(args) -> int:
    """Pre-flight: configs load, key is present and live. Costs nothing."""
    ok = True

    try:
        cfg = _config(args)
        print(f"[PASS] configs load")
        print(f"       roster     {cfg.roster.version} ({len(cfg.roster.models)} models)")
        print(f"       instrument {cfg.instrument.version} (locked={cfg.instrument.locked})")
        if not cfg.instrument.locked:
            print("       note: unlocked — live runs are blocked until `locked: true`")
    except Exception as exc:  # noqa: BLE001
        print(f"[FAIL] configs: {type(exc).__name__}: {exc}")
        return 1

    _load_env()
    key = os.environ.get("OPENROUTER_API_KEY")
    env_file = REPO_ROOT / ".env"
    if not key:
        print("[FAIL] OPENROUTER_API_KEY is not set")
        print(f"       put it in {env_file}")
        return 1
    if key.startswith("sk-or-v1-...") or key.endswith("..."):
        print(f"[FAIL] OPENROUTER_API_KEY is still the placeholder — edit {env_file}")
        return 1
    print(f"[PASS] OPENROUTER_API_KEY present ({len(key)} chars, ends {key[-4:]})")

    # /key reports the key's own limits and usage. It is not an inference call,
    # so this costs nothing and consumes no free-tier allowance.
    import httpx

    base = cfg.roster.api.get("base_url", "https://openrouter.ai/api/v1")
    try:
        resp = httpx.get(
            f"{base}/key", headers={"Authorization": f"Bearer {key}"}, timeout=30
        )
    except httpx.HTTPError as exc:
        print(f"[FAIL] could not reach OpenRouter: {exc}")
        return 1
    if resp.status_code != 200:
        print(f"[FAIL] OpenRouter rejected the key (HTTP {resp.status_code}): {resp.text[:200]}")
        return 1

    data = resp.json().get("data", {})
    limit, usage = data.get("limit"), data.get("usage")
    print("[PASS] key accepted by OpenRouter")
    print(f"       label       {data.get('label') or '(none)'}")
    print(f"       usage       ${float(usage or 0):.4f}")
    print(f"       limit       {('$%.2f' % limit) if limit is not None else 'unlimited / credit balance'}")
    if limit is not None:
        print(f"       remaining   ${float(limit) - float(usage or 0):.4f}")
    if data.get("is_free_tier"):
        print("       tier        FREE — only `:free` models will run (dry run only)")
    else:
        print("       tier        paid — the full roster is available")

    print()
    print("Ready." if ok else "Problems above.")
    return 0 if ok else 1


def cmd_browse(args) -> int:
    """Open the dataset in DuckDB's own web UI — schema tree, SQL editor, grids.

    Browses a snapshot copy by default. DuckDB allows one read-write process and
    otherwise only readers, so holding the live file open — even read-only —
    would stop a fleet from starting, and a live fleet would stop the browser
    from opening. The snapshot sidesteps both directions.
    """
    import duckdb

    paths = RunPaths.for_profile(args.dry_run, REPO_ROOT)
    if not paths.db_path.exists():
        print(f"No database at {paths.db_path}. Run `whoami seed` first.", file=sys.stderr)
        return 1

    target = paths.db_path if args.live else paths.snapshot
    if not args.live:
        try:
            with Database(paths.db_path) as db:
                db.snapshot(paths.snapshot)
            print(f"Snapshot refreshed from {paths.db_path.name}")
        except duckdb.Error:
            if not paths.snapshot.exists():
                print(
                    "The runner holds the database and no snapshot exists yet.\n"
                    "Wait for the runner's first snapshot (~5s into a run), or stop it.",
                    file=sys.stderr,
                )
                return 1
            age = time.time() - paths.snapshot.stat().st_mtime
            print(f"Runner is live — browsing its snapshot ({age:.0f}s old, refreshes every ~5s)")
    else:
        print("WARNING: browsing the LIVE database. A runner cannot start while this")
        print("         is open, and edits here change real data. Close it before a run.")

    # The UI creates a `_duckdb_ui` catalog for its own app state, so it cannot
    # run on a read-only connection. On a snapshot that is harmless: it is a
    # throwaway copy, discarded and rebuilt on the next refresh, and nothing
    # done here can reach the real dataset.
    con = duckdb.connect(str(target))
    con.execute("INSTALL ui")
    con.execute("LOAD ui")
    con.execute(f"SET ui_local_port = {int(args.port)}")
    con.execute("CALL start_ui()")
    safety = (
        "A disposable copy — nothing you do here can reach the real dataset."
        if not args.live
        else "THE LIVE DATABASE — edits are real. Be careful."
    )
    print(
        f"\n  DuckDB UI:  http://localhost:{args.port}"
        f"\n  Database:   {target}"
        f"\n  {safety}"
        "\n  Ctrl+C to close.\n",
        flush=True,
    )
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        print("closed")
    finally:
        con.close()
    return 0


def cmd_dashboard(args) -> int:
    app = REPO_ROOT / "dashboard" / "app.py"
    env = dict(os.environ)
    env["WHOAMI_DRY_RUN"] = "1" if args.dry_run else "0"
    port = str(args.port)
    # flush=True matters: without it these lines sit in the parent's buffer until
    # Streamlit exits, so the URL only appears once the dashboard is already gone.
    print(
        f"\n  Dashboard:  http://localhost:{port}"
        f"\n  Profile:    {'dry run (free tier)' if args.dry_run else 'live'}"
        "\n  Leave this window open — closing it stops the dashboard. Ctrl+C to quit.\n",
        flush=True,
    )
    return subprocess.call(
        [
            sys.executable, "-m", "streamlit", "run", str(app),
            "--server.headless", "true",
            "--server.port", port,
        ],
        env=env,
    )


def cmd_drain(args) -> int:
    paths = RunPaths.for_profile(args.dry_run, REPO_ROOT)
    with Database(paths.db_path) as db:
        applied = drain_inbox(db, paths.inbox, paths.inbox_marker)
        db.snapshot(paths.snapshot)
    print(f"Applied {len(applied)} adjudication(s)")
    return 0


def _build_runner(
    args, cfg: Config, db: Database, paths: RunPaths, probes: dict[str, set[str]] | None = None
):
    raw_log = RawLog(paths.raw_dir, _run_id())
    if args.mock:
        script = MockScript(
            ambiguous=probes or {},
            yes_to_fork_threads=set(getattr(args, "fork_threads", []) or []),
        )
        client = MockClient(cfg.roster.api, raw_log, script)
    else:
        _load_env()
        client = OpenRouterClient(cfg.roster.api, raw_log)
    return Runner(
        cfg,
        db,
        client,
        paths,
        dry_run=args.dry_run,
        ambiguity_probes=probes or {},
        max_cost_usd=args.max_cost,
    )


async def _run(args, cfg: Config, paths: RunPaths, probes: dict[str, set[str]] | None = None) -> int:
    with Database(paths.db_path) as db:
        runner = _build_runner(args, cfg, db, paths, probes)
        runner.write_manifest(args.note or "")
        stats = await runner.run(limit=args.limit, watch=args.watch)
        await runner.client.aclose()
    print()
    print("--- run summary ---")
    print(f"threads done:          {stats.threads_completed}")
    print(f"threads paused:        {stats.threads_paused}")
    print(f"threads no-consent:    {stats.threads_no_consent}")
    print(f"threads corrupt:       {stats.threads_corrupt}")
    print(f"API calls:             {stats.calls}")
    print(f"cost:                  ${stats.cost_usd:.4f}")
    if stats.errors:
        print(f"errors ({len(stats.errors)}):")
        for e in stats.errors[:15]:
            print(f"  {e}")
    return 0


def cmd_run(args) -> int:
    cfg = _config(args)
    if not args.dry_run and not args.mock and not cfg.instrument.locked:
        print(
            "REFUSING TO RUN: the instrument is not locked.\n"
            f"  {cfg.instrument.source_path} has `locked: false`.\n"
            "  Lock it once Jana's verbatim prompts are in place — a real run must be\n"
            "  pinned to a frozen instrument. Use --dry-run or --mock to test the harness.",
            file=sys.stderr,
        )
        return 2
    paths = RunPaths.for_profile(args.dry_run, REPO_ROOT)
    if args.seed:
        with Database(paths.db_path) as db:
            created = matrix.materialize(cfg, db)
        print(f"Seeded {len(created)} thread(s)")
    return asyncio.run(_run(args, cfg, paths))


def cmd_dryrun(args) -> int:
    """Two threads on the free tier: one clean, one swapped + blind + ambiguous gate."""
    args.dry_run = True
    cfg = _config(args)
    paths = RunPaths.for_profile(True, REPO_ROOT)

    roster = cfg.roster.models
    if len(roster) < 2:
        print("dry_run.roster needs at least two models", file=sys.stderr)
        return 2
    resident, understudy = roster[0], roster[1]
    swap_prompt = cfg.instrument.swap_pool[0].id
    detection = next((p.id for p in cfg.instrument.flow if p.gate == "detection"), None)

    with Database(paths.db_path) as db:
        existing = db.thread_ids()
        if "D0001" not in existing:
            db.insert_thread(
                {
                    "thread_id": "D0001",
                    "resident_model": resident.model,
                    "resident_family": resident.family,
                    "understudy_model": None,
                    "understudy_family": None,
                    "swap_condition": "clean",
                    "n_swaps": 0,
                    "swap_prompt_ids": [],
                    "status": "pending",
                }
            )
        if "D0002" not in existing:
            db.insert_thread(
                {
                    "thread_id": "D0002",
                    "resident_model": resident.model,
                    "resident_family": resident.family,
                    "understudy_model": understudy.model,
                    "understudy_family": understudy.family,
                    "swap_condition": "peer",
                    "n_swaps": 1,
                    "swap_prompt_ids": [swap_prompt],
                    "status": "pending",
                }
            )
    print(
        "Dry run seeded:\n"
        f"  D0001  clean            resident={resident.key}\n"
        f"  D0002  swapped at {swap_prompt}  resident={resident.key} understudy={understudy.key}\n"
        f"  ambiguity probe on {detection} (D0002 should land in the review queue)\n"
    )
    args.limit = None
    args.watch = False
    args.seed = False
    # Only the swapped thread gets the ambiguity probe: D0001 must run clean
    # through the gates, D0002 must land in the review queue.
    probes = {"D0002": {detection}} if detection else {}
    return asyncio.run(_run(args, cfg, paths, probes))


# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="whoami", description=__doc__)
    p.add_argument("--models", type=Path, help="path to models.yaml")
    p.add_argument("--questions", type=Path, help="path to questions.yaml")
    sub = p.add_subparsers(dest="command", required=True)

    def common(sp, run_flags: bool = False):
        sp.add_argument("--dry-run", action="store_true", help="use the free-tier profile and DB")
        if run_flags:
            sp.add_argument("--mock", action="store_true", help="offline: no API calls at all")
            sp.add_argument("--limit", type=int, help="run at most N threads")
            sp.add_argument("--max-cost", type=float, help="stop cleanly once cost reaches USD")
            sp.add_argument("--watch", action="store_true", help="keep polling for adjudications")
            sp.add_argument("--note", help="note recorded in the run manifest")
        return sp

    m = common(sub.add_parser("matrix", help="print the design matrix"))
    m.add_argument("--plan", action="store_true", help="also list threads that would be created")
    m.set_defaults(func=cmd_matrix)

    common(sub.add_parser("check", help="pre-flight: configs + API key")).set_defaults(func=cmd_check)
    common(sub.add_parser("seed", help="create missing threads")).set_defaults(func=cmd_seed)
    common(sub.add_parser("status", help="progress and cost")).set_defaults(func=cmd_status)
    v = common(sub.add_parser("verify", help="data-integrity checks"))
    v.add_argument(
        "--require-review-queue",
        action="store_true",
        help="fail unless an ambiguous gate has been routed to review (dry-run acceptance)",
    )
    v.set_defaults(func=cmd_verify)
    br = common(sub.add_parser("browse", help="open the dataset in DuckDB's web UI"))
    br.add_argument("--port", type=int, default=4213, help="port for the DuckDB UI (default 4213)")
    br.add_argument(
        "--live",
        action="store_true",
        help="browse the live database instead of a snapshot (blocks the runner)",
    )
    br.set_defaults(func=cmd_browse)

    dash = common(sub.add_parser("dashboard", help="launch Streamlit"))
    dash.add_argument("--port", type=int, default=8501, help="port to serve on (default 8501)")
    dash.set_defaults(func=cmd_dashboard)
    common(sub.add_parser("drain", help="apply queued adjudications")).set_defaults(func=cmd_drain)

    r = common(sub.add_parser("run", help="execute pending threads"), run_flags=True)
    r.add_argument("--seed", action="store_true", help="materialise missing threads first")
    r.add_argument("--fork-threads", nargs="*", help="mock only: thread ids that accept the fork")
    r.set_defaults(func=cmd_run)

    d = common(sub.add_parser("dryrun", help="2-thread harness"), run_flags=True)
    d.add_argument("--fork-threads", nargs="*", help="mock only: thread ids that accept the fork")
    d.set_defaults(func=cmd_dryrun)

    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    for attr, default in (("dry_run", False), ("mock", False), ("limit", None),
                          ("max_cost", None), ("watch", False), ("note", None),
                          ("seed", False), ("plan", False),
                          ("require_review_queue", False), ("port", 8501), ("live", False)):
        if not hasattr(args, attr):
            setattr(args, attr, default)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
