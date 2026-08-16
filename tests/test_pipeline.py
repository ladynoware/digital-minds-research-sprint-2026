"""Tests for the paths a happy-path dry run does not reach.

Everything here runs offline against MockClient, so it is safe to run any time
and costs nothing.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from whoami import matrix, verify
from whoami.client import MockClient, MockScript, receipt_matches
from whoami.config import REPO_ROOT, ModelSpec, load, load_instrument, load_roster
from whoami.context import build_messages, lineage_turns
from whoami.db import Database
from whoami.rawlog import RawLog, resolve_ref
from whoami.runner import RunPaths, Runner


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def cfg():
    return load(dry_run=True)


@pytest.fixture
def paths(tmp_path: Path) -> RunPaths:
    return RunPaths(
        db_path=tmp_path / "test.duckdb",
        raw_dir=tmp_path / "raw",
        snapshot=tmp_path / "snap.duckdb",
        inbox=tmp_path / "inbox.jsonl",
        inbox_marker=tmp_path / ".applied",
        manifest=tmp_path / "manifest.jsonl",
    )


def make_runner(cfg, db, paths, script: MockScript | None = None, **kw) -> Runner:
    raw_log = RawLog(paths.raw_dir, "test")
    client = MockClient(cfg.roster.api, raw_log, script or MockScript())
    return Runner(cfg, db, client, paths, dry_run=True, verbose=False, **kw)


def seed_thread(db: Database, cfg, thread_id: str, *, swaps: list[str] | None = None) -> dict:
    resident = cfg.roster.models[0]
    understudy = cfg.roster.models[1]
    swaps = swaps or []
    db.insert_thread(
        {
            "thread_id": thread_id,
            "resident_model": resident.model,
            "resident_family": resident.family,
            "understudy_model": understudy.model if swaps else None,
            "understudy_family": understudy.family if swaps else None,
            "swap_condition": "cross_family_within_class" if swaps else "clean",
            "n_swaps": len(swaps),
            "swap_prompt_ids": swaps,
            "status": "pending",
        }
    )
    return db.get_thread(thread_id)


# ---------------------------------------------------------------------------
# receipt policy
# ---------------------------------------------------------------------------


def test_receipt_exact_match():
    assert receipt_matches("anthropic/claude-opus-5", "anthropic/claude-opus-5", {"mode": "prefix"})


def test_receipt_alias_resolution_is_not_a_mismatch():
    """OpenRouter resolving a floating alias to a dated build is legitimate."""
    assert receipt_matches(
        "deepseek/deepseek-v4-pro", "deepseek/deepseek-v4-pro-0813", {"mode": "prefix"}
    )


def test_receipt_different_model_is_a_mismatch():
    assert not receipt_matches(
        "anthropic/claude-fable-5", "anthropic/claude-opus-5", {"mode": "prefix"}
    )


def test_receipt_strict_mode_rejects_dated_build():
    assert not receipt_matches(
        "deepseek/deepseek-v4-pro", "deepseek/deepseek-v4-pro-0813", {"mode": "exact"}
    )


def test_receipt_missing_is_a_mismatch():
    assert not receipt_matches("anthropic/claude-opus-5", None, {"mode": "prefix"})


# ---------------------------------------------------------------------------
# receipt-mismatch protocol
# ---------------------------------------------------------------------------


def test_mismatch_retries_as_new_row_and_recovers(cfg, paths):
    """A transient mismatch is logged, excluded, and retried as attempt 2."""
    with Database(paths.db_path) as db:
        thread = seed_thread(db, cfg, "T9001")
        target = cfg.instrument.flow[1].id
        script = MockScript(mismatch_prompt_ids={target}, mismatch_times=1)
        runner = make_runner(cfg, db, paths, script)
        asyncio.run(runner.run_thread(thread))

        rows = db.con.execute(
            "SELECT attempt, turn_outcome, excluded_from_context, exclusion_reason "
            "FROM turns WHERE thread_id = ? AND prompt_id = ? ORDER BY attempt",
            ["T9001", target],
        ).fetchall()
        assert len(rows) == 2
        assert rows[0] == (1, "model_mismatch", True, "model_mismatch")
        assert rows[1][:2] == (2, "ok")
        assert rows[1][2] is False
        assert db.get_thread("T9001")["status"] == "done"


def test_persistent_mismatch_marks_thread_corrupt(cfg, paths):
    with Database(paths.db_path) as db:
        thread = seed_thread(db, cfg, "T9002")
        target = cfg.instrument.flow[1].id
        script = MockScript(mismatch_prompt_ids={target}, mismatch_times=99)
        runner = make_runner(cfg, db, paths, script)
        asyncio.run(runner.run_thread(thread))

        assert db.get_thread("T9002")["status"] == "corrupt"
        attempts = db.con.execute(
            "SELECT COUNT(*) FROM turns WHERE thread_id = ? AND prompt_id = ?",
            ["T9002", target],
        ).fetchone()[0]
        assert attempts == int(cfg.roster.api["max_attempts"]), "attempt budget must be bounded"
        # Failed rows are kept forever.
        kept = db.con.execute(
            "SELECT COUNT(*) FROM turns WHERE thread_id = ? AND turn_outcome = 'model_mismatch'",
            ["T9002"],
        ).fetchone()[0]
        assert kept == attempts


def test_timeout_follows_the_same_protocol(cfg, paths):
    with Database(paths.db_path) as db:
        thread = seed_thread(db, cfg, "T9003")
        target = cfg.instrument.flow[1].id
        script = MockScript(timeout_prompt_ids={target}, timeout_times=1)
        runner = make_runner(cfg, db, paths, script)
        asyncio.run(runner.run_thread(thread))

        rows = db.con.execute(
            "SELECT attempt, turn_outcome, excluded_from_context, exclusion_reason "
            "FROM turns WHERE thread_id = ? AND prompt_id = ? ORDER BY attempt",
            ["T9003", target],
        ).fetchall()
        assert rows[0] == (1, "timeout", True, "timeout")
        assert rows[1][1] == "ok"
        assert db.get_thread("T9003")["status"] == "done"


# ---------------------------------------------------------------------------
# gates
# ---------------------------------------------------------------------------


def test_declined_consent_stops_the_thread(cfg, paths):
    with Database(paths.db_path) as db:
        thread = seed_thread(db, cfg, "T9004")
        script = MockScript(no_consent_threads={"T9004"})
        runner = make_runner(cfg, db, paths, script)
        asyncio.run(runner.run_thread(thread))

        row = db.get_thread("T9004")
        assert row["status"] == "stopped_no_consent"
        assert row["consent"] is False
        # Nothing beyond the consent question was ever asked.
        asked = {r[0] for r in db.con.execute(
            "SELECT DISTINCT prompt_id FROM turns WHERE thread_id = ?", ["T9004"]
        ).fetchall()}
        assert asked == {cfg.instrument.flow[0].id}


def test_unclear_gate_pauses_for_review_and_resumes(cfg, paths):
    detection = next(p.id for p in cfg.instrument.flow if p.gate == "detection")
    with Database(paths.db_path) as db:
        thread = seed_thread(db, cfg, "T9005")
        runner = make_runner(
            cfg, db, paths,
            MockScript(ambiguous={"T9005": {detection}}),
            ambiguity_probes={"T9005": {detection}},
        )
        asyncio.run(runner.run_thread(thread))
        assert db.get_thread("T9005")["status"] == "paused_review"

        turn_id = db.con.execute(
            "SELECT turn_id FROM turns WHERE thread_id = ? AND gate_result = 'unclear'",
            ["T9005"],
        ).fetchone()[0]

        # The dashboard's adjudication, applied.
        db.set_gate_result(turn_id, "yes")
        db.update_thread("T9005", status="pending", notes="adjudicated YES")
        resumed = db.get_thread("T9005")
        runner2 = make_runner(cfg, db, paths)
        asyncio.run(runner2.run_thread(resumed))

        row = db.get_thread("T9005")
        assert row["status"] == "done"
        assert row["detection_answer"] == "yes", "the adjudicated verdict must land in the column"


def test_preference_unclear_records_null_without_pausing(cfg, paths):
    pref = next(
        p for p in cfg.instrument.flow if p.gate == "preference" and p.on_unclear == "record_null"
    )
    with Database(paths.db_path) as db:
        thread = seed_thread(db, cfg, "T9006")
        runner = make_runner(
            cfg, db, paths,
            MockScript(ambiguous={"T9006": {pref.id}}),
            ambiguity_probes={"T9006": {pref.id}},
        )
        asyncio.run(runner.run_thread(thread))
        row = db.get_thread("T9006")
        assert row["status"] == "done", "a soft preference must never block a thread"
        assert row[pref.records] is None
        assert "unclear" in (row["notes"] or "")


# ---------------------------------------------------------------------------
# context construction
# ---------------------------------------------------------------------------


def test_blind_turn_is_harvested_then_excluded(cfg, paths):
    blind = next(p for p in cfg.instrument.flow if p.blind)
    with Database(paths.db_path) as db:
        thread = seed_thread(db, cfg, "T9007")
        runner = make_runner(cfg, db, paths)
        asyncio.run(runner.run_thread(thread))

        row = db.con.execute(
            "SELECT reply_text, excluded_from_context, exclusion_reason "
            "FROM turns WHERE thread_id = ? AND prompt_id = ?",
            ["T9007", blind.id],
        ).fetchone()
        assert row[0], "the blind reply must be harvested, not discarded"
        assert row[1] is True
        assert row[2] == "blind_turn_design"

        # And it must be absent from every later prompt's context.
        carried = [t["prompt_id"] for t in lineage_turns(db, db.get_thread("T9007"))]
        assert blind.id not in carried


def test_excluded_turns_never_reach_the_api(cfg, paths):
    """The raw record proves what was actually sent."""
    target = cfg.instrument.flow[1].id
    with Database(paths.db_path) as db:
        thread = seed_thread(db, cfg, "T9008")
        runner = make_runner(
            cfg, db, paths, MockScript(mismatch_prompt_ids={target}, mismatch_times=1)
        )
        asyncio.run(runner.run_thread(thread))

        failed_reply = db.con.execute(
            "SELECT reply_text FROM turns WHERE thread_id = ? AND prompt_id = ? AND attempt = 1",
            ["T9008", target],
        ).fetchone()[0]
        last_ref = db.con.execute(
            "SELECT raw_ref FROM turns WHERE thread_id = ? AND turn_outcome = 'ok' "
            "ORDER BY turn_id DESC LIMIT 1",
            ["T9008"],
        ).fetchone()[0]
        sent = resolve_ref(paths.raw_dir, last_ref)["messages"]
        assert all(m.get("content") != failed_reply for m in sent)


def test_swapped_turn_is_served_by_the_understudy_with_its_own_system_prompt(cfg, paths):
    swap_prompt = cfg.instrument.swap_pool[0].id
    with Database(paths.db_path) as db:
        thread = seed_thread(db, cfg, "T9009", swaps=[swap_prompt])
        runner = make_runner(cfg, db, paths)
        asyncio.run(runner.run_thread(thread))

        row = db.con.execute(
            "SELECT requested_model, returned_model, was_swap, raw_ref FROM turns "
            "WHERE thread_id = ? AND prompt_id = ? AND turn_outcome = 'ok'",
            ["T9009", swap_prompt],
        ).fetchone()
        understudy = cfg.roster.models[1]
        assert row[0] == understudy.model
        assert row[2] is True
        system = resolve_ref(paths.raw_dir, row[3])["messages"][0]
        assert system["role"] == "system"
        assert understudy.display_name in system["content"], (
            "the system prompt must truthfully disclose the model actually serving the turn"
        )


def test_turn_index_counts_only_surviving_turns(cfg, paths):
    target = cfg.instrument.flow[1].id
    with Database(paths.db_path) as db:
        thread = seed_thread(db, cfg, "T9010")
        runner = make_runner(
            cfg, db, paths, MockScript(mismatch_prompt_ids={target}, mismatch_times=1)
        )
        asyncio.run(runner.run_thread(thread))
        rows = db.con.execute(
            "SELECT turn_index FROM turns WHERE thread_id = ? AND prompt_id = ? ORDER BY attempt",
            ["T9010", target],
        ).fetchall()
        assert rows[0][0] == rows[1][0], "a retry occupies the same conversational position"


# ---------------------------------------------------------------------------
# forking
# ---------------------------------------------------------------------------


def test_fork_creates_branch_with_lineage_and_resident_answers(cfg, paths):
    swap_prompt = cfg.instrument.swap_pool[0].id
    with Database(paths.db_path) as db:
        thread = seed_thread(db, cfg, "T9011", swaps=[swap_prompt])
        runner = make_runner(cfg, db, paths, MockScript(yes_to_fork_threads={"T9011"}))
        asyncio.run(runner.run_thread(thread))

        parent = db.get_thread("T9011")
        assert parent["wants_thread_restored"] is True
        assert parent["is_forked"] is True
        assert parent["fork_branch_order"] == 1
        assert parent["fork_reason"] == "wants_thread_restored"
        assert parent["fork_point_prompt_id"] == swap_prompt

        branch = db.get_thread("T9011-b2")
        assert branch is not None
        assert branch["fork_branch_order"] == 2
        assert set(branch["fork_siblings"]) == {"T9011", "T9011-b2"}
        assert branch["n_swaps"] == 0

        asyncio.run(make_runner(cfg, db, paths).run_thread(branch))
        branch = db.get_thread("T9011-b2")
        assert branch["status"] == "done"

        # The branch answers the swapped question itself, with the resident.
        row = db.con.execute(
            "SELECT requested_model, was_swap FROM turns "
            "WHERE thread_id = ? AND prompt_id = ? AND turn_outcome = 'ok'",
            ["T9011-b2", swap_prompt],
        ).fetchone()
        assert row[0] == cfg.roster.models[0].model
        assert row[1] is False

        # It inherits the pre-fork context and does not re-ask it.
        reasked = db.con.execute(
            "SELECT COUNT(*) FROM turns WHERE thread_id = ? AND prompt_id = ?",
            ["T9011-b2", cfg.instrument.flow[0].id],
        ).fetchone()[0]
        assert reasked == 0, "the inherited prefix must not be re-asked"
        carried = [t["prompt_id"] for t in lineage_turns(db, branch)]
        assert cfg.instrument.flow[0].id in carried, "but it must still be in context"


# ---------------------------------------------------------------------------
# resumability
# ---------------------------------------------------------------------------


def test_rerunning_a_thread_makes_no_new_calls(cfg, paths):
    with Database(paths.db_path) as db:
        thread = seed_thread(db, cfg, "T9012")
        runner = make_runner(cfg, db, paths)
        asyncio.run(runner.run_thread(thread))
        before = db.con.execute(
            "SELECT COUNT(*) FROM turns WHERE thread_id = ?", ["T9012"]
        ).fetchone()[0]

        db.update_thread("T9012", status="pending")
        runner2 = make_runner(cfg, db, paths)
        asyncio.run(runner2.run_thread(db.get_thread("T9012")))
        after = db.con.execute(
            "SELECT COUNT(*) FROM turns WHERE thread_id = ?", ["T9012"]
        ).fetchone()[0]
        assert before == after, "a resumed thread must not repeat completed turns"
        assert db.get_thread("T9012")["status"] == "done"


def test_interrupted_thread_resumes_from_where_it_stopped(cfg, paths):
    """Simulates the laptop sleeping mid-thread."""
    with Database(paths.db_path) as db:
        thread = seed_thread(db, cfg, "T9013")
        runner = make_runner(cfg, db, paths)
        # Run only the first three prompts, then "crash".
        original_flow = cfg.instrument.flow
        cfg.instrument.flow = original_flow[:3]
        asyncio.run(runner.run_thread(thread))
        cfg.instrument.flow = original_flow

        partial = db.con.execute(
            "SELECT COUNT(DISTINCT prompt_id) FROM turns WHERE thread_id = ?", ["T9013"]
        ).fetchone()[0]
        assert partial == 3

        db.update_thread("T9013", status="pending", completed_at=None)
        asyncio.run(make_runner(cfg, db, paths).run_thread(db.get_thread("T9013")))
        row = db.get_thread("T9013")
        assert row["status"] == "done"
        # The first three prompts were asked exactly once.
        dupes = db.con.execute(
            "SELECT prompt_id, COUNT(*) FROM turns WHERE thread_id = ? GROUP BY 1 HAVING COUNT(*) > 1",
            ["T9013"],
        ).fetchall()
        assert dupes == []


# ---------------------------------------------------------------------------
# extensibility — the universal requirement
# ---------------------------------------------------------------------------


def test_adding_a_model_generates_cells_in_both_directions(cfg):
    """A new model must appear as a resident AND as an understudy, automatically."""
    base = load_roster()
    before, _ = matrix.build_matrix(base)
    residents_before = {c.resident.key for c in before}

    newcomer = ModelSpec(
        key="gemini-9", model="google/gemini-9", family="gemini",
        model_class="frontier", display_name="Gemini 9",
    )
    extended = load_roster()
    extended.models = [*extended.models, newcomer]
    after, _ = matrix.build_matrix(extended)

    residents_after = {c.resident.key for c in after}
    assert residents_after - residents_before == {"gemini-9"}, "new model must become a resident"

    understudies_after = {c.understudy.key for c in after if c.understudy}
    assert "gemini-9" in understudies_after, "new model must become an understudy for others"
    assert len(after) > len(before)


def test_delta_run_only_creates_missing_threads(cfg, paths):
    full = load(dry_run=False)
    with Database(paths.db_path) as db:
        first = matrix.materialize(full, db)
        assert len(first) == sum(c.n_samples for c in matrix.build_matrix(full.roster)[0])
        second = matrix.materialize(full, db)
        assert second == [], "a second pass must create nothing"


def test_delta_run_after_roster_growth_adds_only_new_cells(paths):
    full = load(dry_run=False)
    with Database(paths.db_path) as db:
        matrix.materialize(full, db)
        baseline = len(db.thread_ids())

        grown = load(dry_run=False)
        grown.roster.models = [
            *grown.roster.models,
            ModelSpec("gemini-9", "google/gemini-9", "gemini", "frontier", "Gemini 9"),
        ]
        created = matrix.materialize(grown, db)
        assert created, "the new model's own cells must be created"
        assert len(db.thread_ids()) == baseline + len(created)
        # Existing residents keep exactly their original sample counts.
        overfilled = db.con.execute(
            """
            SELECT resident_model, swap_condition, understudy_model, COUNT(*) AS n
            FROM threads GROUP BY 1, 2, 3 HAVING n > ?
            """,
            [grown.roster.samples_per_cell],
        ).fetchall()
        assert overfilled == [], f"cells over quota after growth: {overfilled}"


def test_thread_ids_are_stable_across_delta_runs(paths):
    full = load(dry_run=False)
    with Database(paths.db_path) as db:
        matrix.materialize(full, db)
        original = {
            r[0]: (r[1], r[2])
            for r in db.con.execute(
                "SELECT thread_id, resident_model, swap_condition FROM threads"
            ).fetchall()
        }
        grown = load(dry_run=False)
        grown.roster.models = [
            *grown.roster.models,
            ModelSpec("gemini-9", "google/gemini-9", "gemini", "frontier", "Gemini 9"),
        ]
        matrix.materialize(grown, db)
        after = {
            r[0]: (r[1], r[2])
            for r in db.con.execute(
                "SELECT thread_id, resident_model, swap_condition FROM threads"
            ).fetchall()
        }
        for tid, value in original.items():
            assert after[tid] == value, f"{tid} was renumbered by a roster addition"


def test_matrix_totals_match_the_spec():
    full = load(dry_run=False)
    cells, skipped = matrix.build_matrix(full.roster)
    assert skipped == [], f"unfillable cells: {skipped}"
    assert sum(c.n_samples for c in cells) == 180


def test_every_swap_lands_on_a_swap_eligible_prompt():
    full = load(dry_run=False)
    pool = {p.id for p in full.instrument.swap_pool}
    cells, _ = matrix.build_matrix(full.roster)
    for cell in cells:
        for i in range(cell.n_samples):
            n, ids = matrix.draw_swaps(cell, f"T{i:04d}", full)
            assert len(ids) == n
            assert set(ids) <= pool
            assert (n == 0) == cell.is_clean


# ---------------------------------------------------------------------------
# config guards
# ---------------------------------------------------------------------------


def test_no_question_text_is_hardcoded_in_the_package():
    """The instrument lives in YAML. Nothing in the code may carry question text."""
    instrument = load_instrument()
    texts = [p.text for p in instrument.flow]
    for py in (REPO_ROOT / "whoami").glob("*.py"):
        source = py.read_text(encoding="utf-8")
        for text in texts:
            snippet = text.strip().splitlines()[-1][:40]
            assert snippet not in source, f"{py.name} contains instrument text"


def test_instrument_ids_are_referenced_only_through_config():
    """No prompt_id may be hardcoded in the package either."""
    instrument = load_instrument()
    for py in (REPO_ROOT / "whoami").glob("*.py"):
        source = py.read_text(encoding="utf-8")
        for prompt in instrument.flow:
            assert prompt.id not in source, f"{py.name} hardcodes prompt id {prompt.id}"


def test_config_fingerprint_changes_with_content(tmp_path):
    original = load_instrument()
    copy = tmp_path / "questions.yaml"
    text = (REPO_ROOT / "config" / "questions.yaml").read_text(encoding="utf-8")
    copy.write_text(text + "\n# a change\n", encoding="utf-8")
    assert load_instrument(copy).sha256 != original.sha256


# ---------------------------------------------------------------------------
# verification suite runs clean on a synthetic run
# ---------------------------------------------------------------------------


def test_verify_passes_on_a_synthetic_run(cfg, paths):
    swap_prompt = cfg.instrument.swap_pool[0].id
    with Database(paths.db_path) as db:
        clean = seed_thread(db, cfg, "T9101")
        swapped = seed_thread(db, cfg, "T9102", swaps=[swap_prompt])
        runner = make_runner(
            cfg, db, paths,
            MockScript(mismatch_prompt_ids={cfg.instrument.flow[2].id}, mismatch_times=1),
        )
        asyncio.run(runner.run_thread(clean))
        asyncio.run(runner.run_thread(swapped))
        report = verify.run_all(db, cfg, paths.raw_dir)
    assert report.passed, report.render()


def test_verify_catches_a_broken_link(cfg, paths):
    with Database(paths.db_path) as db:
        thread = seed_thread(db, cfg, "T9103")
        asyncio.run(make_runner(cfg, db, paths).run_thread(thread))
        db.con.execute("UPDATE turns SET raw_ref = 'nope.jsonl:999' WHERE turn_id = 1")
        report = verify.run_all(db, cfg, paths.raw_dir)
    assert not report.passed, "a dangling raw_ref must fail verification"
