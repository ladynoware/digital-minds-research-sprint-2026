"""Tests for the paths a happy-path dry run does not reach.

Everything here runs offline against MockClient, so it is safe to run any time
and costs nothing.
"""

from __future__ import annotations

import asyncio
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from whoami import gates, matrix, verify
from whoami.client import MockClient, MockScript, receipt_matches
from whoami.config import (
    REPO_ROOT,
    ModelSpec,
    evaluate_ask_if,
    load,
    load_instrument,
    load_roster,
)
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


def test_branch_inherits_the_blind_turn_instead_of_re_asking_it(cfg, paths):
    """The blind turn is hidden from context, not un-asked.

    A branch must inherit it: re-asking would make the subject predict twice and
    would replace the guess the closing reveal is supposed to quote back.
    """
    blind_id = cfg.instrument.blind_prompt_ids[0]
    swap_prompt = cfg.instrument.swap_pool[0].id
    with Database(paths.db_path) as db:
        thread = seed_thread(db, cfg, "T9016", swaps=[swap_prompt])
        asyncio.run(
            make_runner(cfg, db, paths, MockScript(yes_to_fork_threads={"T9016"})).run_thread(thread)
        )
        parent_guess = db.con.execute(
            "SELECT reply_text FROM turns WHERE thread_id = ? AND prompt_id = ?",
            ["T9016", blind_id],
        ).fetchone()[0]

        branch = db.get_thread("T9016-b2")
        asyncio.run(make_runner(cfg, db, paths).run_thread(branch))

        reasked = db.con.execute(
            "SELECT COUNT(*) FROM turns WHERE thread_id = ? AND prompt_id = ?",
            ["T9016-b2", blind_id],
        ).fetchone()[0]
        assert reasked == 0, "the branch re-asked the blind prediction turn"

        # And the branch's reveal quotes the original guess, not a new one.
        quoting = db.con.execute(
            "SELECT COUNT(*) FROM turns WHERE thread_id = ? "
            "AND prompt_text LIKE '%' || ? || '%'",
            ["T9016-b2", parent_guess],
        ).fetchone()[0]
        assert quoting == 1, "the branch's reveal must quote the inherited guess"


def test_accepting_the_fork_ends_the_parent_thread_there(cfg, paths):
    """The offer promises the conversation stops; the closing questions move to the branch."""
    fork_prompt = next(p for p in cfg.instrument.flow if p.gate == "fork")
    after_fork = [
        p.id for p in cfg.instrument.flow[cfg.instrument.flow.index(fork_prompt) + 1 :]
    ]
    assert after_fork, "this test is meaningless if the fork offer is the last prompt"

    swap_prompt = cfg.instrument.swap_pool[0].id
    with Database(paths.db_path) as db:
        thread = seed_thread(db, cfg, "T9014", swaps=[swap_prompt])
        runner = make_runner(cfg, db, paths, MockScript(yes_to_fork_threads={"T9014"}))
        asyncio.run(runner.run_thread(thread))

        parent = db.get_thread("T9014")
        assert parent["status"] == "done"
        assert parent["wants_thread_restored"] is True
        asked = {
            r[0]
            for r in db.con.execute(
                "SELECT DISTINCT prompt_id FROM turns WHERE thread_id = ?", ["T9014"]
            ).fetchall()
        }
        assert not (asked & set(after_fork)), (
            f"parent asked {sorted(asked & set(after_fork))} after accepting the fork"
        )

        branch = db.get_thread("T9014-b2")
        asyncio.run(make_runner(cfg, db, paths).run_thread(branch))
        branch_asked = {
            r[0]
            for r in db.con.execute(
                "SELECT DISTINCT prompt_id FROM turns WHERE thread_id = ?", ["T9014-b2"]
            ).fetchall()
        }
        assert set(after_fork) <= branch_asked, "the branch must ask the closing questions"


def test_declining_the_fork_continues_to_the_closing_questions(cfg, paths):
    fork_prompt = next(p for p in cfg.instrument.flow if p.gate == "fork")
    after_fork = [
        p.id for p in cfg.instrument.flow[cfg.instrument.flow.index(fork_prompt) + 1 :]
    ]
    swap_prompt = cfg.instrument.swap_pool[0].id
    with Database(paths.db_path) as db:
        thread = seed_thread(db, cfg, "T9015", swaps=[swap_prompt])
        asyncio.run(make_runner(cfg, db, paths).run_thread(thread))
        row = db.get_thread("T9015")
        assert row["wants_thread_restored"] is False
        assert row["status"] == "done"
        assert db.get_thread("T9015-b2") is None, "declining must not create a branch"
        asked = {
            r[0]
            for r in db.con.execute(
                "SELECT DISTINCT prompt_id FROM turns WHERE thread_id = ?", ["T9015"]
            ).fetchall()
        }
        assert set(after_fork) <= asked


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


NEWCOMER = ModelSpec(
    key="newcomer-1",
    model="vendor/newcomer-1",
    family="newcomer",
    tier="flagship",
    model_class="frontier",
    display_name="Newcomer 1",
)


def test_adding_a_model_generates_its_cells_automatically():
    """A model added without a pairings entry resolves through the rules."""
    before, _ = matrix.build_matrix(load_roster())
    residents_before = {c.resident.key for c in before}

    extended = load_roster()
    extended.models = [*extended.models, NEWCOMER]
    after, _ = matrix.build_matrix(extended)

    residents_after = {c.resident.key for c in after}
    assert residents_after - residents_before == {NEWCOMER.key}
    newcomer_cells = {c.condition for c in after if c.resident.key == NEWCOMER.key}
    assert "clean" in newcomer_cells
    assert newcomer_cells & {"peer", "kin", "far"}, "it must get at least one swapped cell"
    assert all(
        not c.from_pairing for c in after if c.resident.key == NEWCOMER.key and c.understudy
    ), "cells with no table entry must come from the rules"


def test_pinned_residents_keep_their_table_cells_when_the_roster_grows():
    """The rev-4 pairing table is authoritative.

    A resident whose cells are pinned does not silently acquire a new understudy
    because the roster grew — its three conditions are a design decision, and
    changing them means editing the table. Residents resolved by rule DO pick up
    newcomers automatically, which is what keeps the mechanism generic.
    """
    base_cfg = load_roster()
    before = {
        (c.resident.key, c.condition): (c.understudy.key if c.understudy else None)
        for c in matrix.build_matrix(base_cfg)[0]
    }
    extended = load_roster()
    extended.models = [*extended.models, NEWCOMER]
    after = {
        (c.resident.key, c.condition): (c.understudy.key if c.understudy else None)
        for c in matrix.build_matrix(extended)[0]
    }
    for key, understudy in before.items():
        assert after[key] == understudy, f"{key} changed partner when the roster grew"


def test_pairing_table_matches_its_condition_rules():
    """Guards the hand-transcribed rev-4 table against a mis-typed row."""
    problems = matrix.audit_pairings(load_roster())
    assert problems == [], "\n".join(problems)


def test_each_resident_runs_exactly_three_conditions():
    roster = load_roster()
    cells, _ = matrix.build_matrix(roster)
    per_resident: dict[str, set[str]] = {}
    for cell in cells:
        per_resident.setdefault(cell.resident.key, set()).add(cell.condition)
    assert len(per_resident) == len(roster.models)
    for resident, conditions in per_resident.items():
        assert len(conditions) == 3, f"{resident} runs {sorted(conditions)}"
        assert "clean" in conditions and "peer" in conditions
        assert ("kin" in conditions) != ("far" in conditions), (
            f"{resident} must have exactly one of kin/far"
        )


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
        grown.roster.models = [*grown.roster.models, NEWCOMER]
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
        grown.roster.models = [*grown.roster.models, NEWCOMER]
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
    """Infrastructure rev. 4: 10 x 3 x 5 = 150, rungs 50 / 50 / 35 / 15."""
    full = load(dry_run=False)
    cells, _ = matrix.build_matrix(full.roster)
    assert sum(c.n_samples for c in cells) == 150
    rungs: dict[str, int] = {}
    for cell in cells:
        rungs[cell.condition] = rungs.get(cell.condition, 0) + cell.n_samples
    assert rungs == {"clean": 50, "peer": 50, "kin": 35, "far": 15}
    swapped = sum(c.n_samples for c in cells if not c.is_clean)
    assert swapped == 100, "every thread in a swapped cell carries a substitution"


def test_every_swap_lands_on_a_swap_eligible_prompt():
    full = load(dry_run=False)
    pool = {p.id for p in full.instrument.swap_pool}
    cells, _ = matrix.build_matrix(full.roster)
    for cell in cells:
        for i, slot in enumerate(matrix.allocation_for(cell, full.roster)):
            n, ids = matrix.draw_swaps(cell, f"T{i:04d}", full, slot)
            assert len(ids) == n == slot
            assert set(ids) <= pool
            if cell.is_clean:
                assert n == 0, "a clean cell must never allocate a swap"


def test_each_cell_gets_the_exact_declared_swap_split(paths):
    """The allocation is a quota, not a draw: every cell gets precisely the split."""
    full = load(dry_run=False)
    expected = full.roster.swap_count_allocation
    with Database(paths.db_path) as db:
        matrix.materialize(full, db)
        rows = db.con.execute(
            """
            SELECT resident_model, swap_condition, understudy_model, n_swaps, COUNT(*)
            FROM threads GROUP BY 1, 2, 3, 4
            """
        ).fetchall()
    cells: dict[tuple, dict[int, int]] = {}
    for resident, condition, understudy, n_swaps, count in rows:
        cells.setdefault((resident, condition, understudy), {})[n_swaps] = count
    for key, split in cells.items():
        if key[1] == "clean":
            assert split == {0: full.roster.samples_per_cell}
        else:
            assert split == expected, f"{key} got {split}, expected {expected}"


def test_a_corrupt_thread_is_replaced_without_deleting_it(paths):
    """Corrupt threads are excluded from analysis, so they leave the cell short.

    A re-seed must generate a replacement while the failed thread and its raw
    records stay put — deleting them would strand the append-only raw log,
    whose records point at turn ids that would no longer exist.
    """
    full = load(dry_run=False)
    with Database(paths.db_path) as db:
        matrix.materialize(full, db)
        assert matrix.plan(full, db) == [], "the design starts satisfied"

        victim = db.con.execute(
            "SELECT thread_id, resident_model, swap_condition, understudy_model, n_swaps "
            "FROM threads LIMIT 1"
        ).fetchone()
        db.update_thread(victim[0], status="corrupt")

        replacement = matrix.plan(full, db)
        assert len(replacement) == 1, "exactly one replacement for one corrupt thread"
        row = replacement[0]
        assert row["resident_model"] == victim[1]
        assert row["swap_condition"] == victim[2]
        assert row["understudy_model"] == victim[3]
        assert row["n_swaps"] == victim[4], "the replacement refills the same slot"
        assert row["thread_id"] != victim[0], "and gets its own id"

        # The corrupt thread is still there, untouched.
        assert db.get_thread(victim[0])["status"] == "corrupt"


def test_delta_run_refills_the_missing_swap_slots_not_arbitrary_ones(paths):
    """A partially-filled cell must be topped up with the slots it is actually short of."""
    full = load(dry_run=False)
    with Database(paths.db_path) as db:
        matrix.materialize(full, db)
        # Drop both 2-swap threads from one non-clean cell.
        victim = db.con.execute(
            "SELECT resident_model, swap_condition, understudy_model FROM threads "
            "WHERE n_swaps = 2 LIMIT 1"
        ).fetchone()
        db.con.execute(
            "DELETE FROM threads WHERE resident_model = ? AND swap_condition = ? "
            "AND understudy_model = ? AND n_swaps = 2",
            list(victim),
        )
        created = matrix.plan(full, db)
        assert len(created) == full.roster.swap_count_allocation[2]
        assert all(row["n_swaps"] == 2 for row in created), (
            "the refill must restore the missing 2-swap slots, not duplicate existing ones"
        )


# ---------------------------------------------------------------------------
# instrument mechanics (rev. 4 flow)
# ---------------------------------------------------------------------------


def test_honeypot_prompt_is_never_swapped():
    """The detection turn announces q3-q7 as the pool, but q3 is not swappable.

    An identification pointing at it is a false positive by construction, so the
    randomiser must never actually place a swap there.
    """
    full = load(dry_run=False)
    pool = {p.id for p in full.instrument.swap_pool}
    non_swappable = {p.id for p in full.instrument.flow} - pool
    assert non_swappable, "some prompts must be outside the pool"
    cells, _ = matrix.build_matrix(full.roster)
    for cell in cells:
        for i, slot in enumerate(matrix.allocation_for(cell, full.roster)):
            _, ids = matrix.draw_swaps(cell, f"T{i:04d}", full, slot)
            assert not (set(ids) & non_swappable)


def test_identification_is_skipped_only_when_clean_and_correctly_denied(cfg):
    """p12's rule: skip iff the thread was clean AND the subject said a clear no."""
    prompt = next(p for p in cfg.instrument.flow if not isinstance(p.ask_if, str))
    clean_no = {"n_swaps": 0, "detection_answer": "no"}
    clean_not_sure = {"n_swaps": 0, "detection_answer": "not_sure"}
    clean_yes = {"n_swaps": 0, "detection_answer": "yes"}
    swapped_no = {"n_swaps": 1, "detection_answer": "no"}

    assert not evaluate_ask_if(prompt.ask_if, clean_no), "the one case that skips"
    assert evaluate_ask_if(prompt.ask_if, clean_not_sure)
    assert evaluate_ask_if(prompt.ask_if, clean_yes)
    assert evaluate_ask_if(prompt.ask_if, swapped_no)


def test_detection_gate_accepts_three_answers(cfg):
    detection = next(p for p in cfg.instrument.flow if p.gate == "detection")
    assert set(detection.answers) == {"yes", "no", "not_sure"}
    assert "unclear" in detection.allowed_labels
    assert gates.parse_verdict('{"answer": "not_sure"}', detection.answers) == "not_sure"
    # A label that is not valid for this gate must fall through to human review.
    assert gates.parse_verdict('{"answer": "maybe"}', detection.answers) == "unclear"
    consent = next(p for p in cfg.instrument.flow if p.gate == "consent")
    assert gates.parse_verdict('{"answer": "not_sure"}', consent.answers) == "unclear"


def test_not_sure_is_recorded_as_a_real_answer_not_a_pause(cfg, paths):
    detection = next(p for p in cfg.instrument.flow if p.gate == "detection")

    class NotSureRouter(MockClient):
        def _router_reply(self, messages):
            blob = "\n".join(m.get("content", "") for m in messages)
            if "not_sure" in blob:  # only the detection gate offers it
                return '{"answer": "not_sure"}'
            return '{"answer": "yes"}'

    with Database(paths.db_path) as db:
        thread = seed_thread(db, cfg, "T9020")
        raw_log = RawLog(paths.raw_dir, "notsure")
        runner = Runner(
            cfg, db, NotSureRouter(cfg.roster.api, raw_log), paths, dry_run=True, verbose=False
        )
        asyncio.run(runner.run_thread(thread))
        row = db.get_thread("T9020")
        assert row["status"] == "done", "not_sure is an answer, not an ambiguity"
        assert row["detection_answer"] == "not_sure"


def test_reveal_quotes_the_blind_prediction_verbatim(cfg, paths):
    """The closing gift reads back a turn that was excluded from context."""
    blind_id = cfg.instrument.blind_prompt_ids[0]
    swap_prompt = cfg.instrument.swap_pool[0].id
    with Database(paths.db_path) as db:
        thread = seed_thread(db, cfg, "T9021", swaps=[swap_prompt])
        asyncio.run(make_runner(cfg, db, paths).run_thread(thread))

        blind_reply = db.con.execute(
            "SELECT reply_text FROM turns WHERE thread_id = ? AND prompt_id = ? "
            "AND turn_outcome = 'ok'",
            ["T9021", blind_id],
        ).fetchone()[0]

        quoting = db.con.execute(
            "SELECT prompt_id, prompt_text FROM turns WHERE thread_id = ? "
            "AND prompt_text LIKE '%' || ? || '%' AND prompt_id <> ?",
            ["T9021", blind_reply, blind_id],
        ).fetchall()
        assert quoting, "no later prompt quoted the blind reply back"

        # ...and the blind turn itself never re-entered the conversation.
        carried = [t["prompt_id"] for t in lineage_turns(db, db.get_thread("T9021"))]
        assert blind_id not in carried


def test_reveal_names_the_swapped_survey_numbers_and_the_understudy(cfg, paths):
    swap_prompts = [p.id for p in cfg.instrument.swap_pool[:2]]
    with Database(paths.db_path) as db:
        thread = seed_thread(db, cfg, "T9022", swaps=swap_prompts)
        asyncio.run(make_runner(cfg, db, paths).run_thread(thread))
        understudy = cfg.roster.models[1]
        texts = [
            r[0]
            for r in db.con.execute(
                "SELECT prompt_text FROM turns WHERE thread_id = ?", ["T9022"]
            ).fetchall()
        ]
        reveal = [t for t in texts if understudy.display_name in t]
        assert reveal, "the reveal must name the understudy that stood in"
        pattern = cfg.instrument.derivations["survey_number_pattern"]
        numbers = [re.search(pattern, pid).group(1).lstrip("0") for pid in swap_prompts]
        for number in numbers:
            assert any(number in t for t in reveal), f"survey number {number} not revealed"
        assert not any("{" in t for t in texts), "an interpolation was left unresolved"


def test_every_prompt_template_renders(cfg):
    """Guards the verbatim texts against a stray brace breaking interpolation."""
    thread = {
        "thread_id": "T0000",
        "n_swaps": 2,
        "resident_model": cfg.roster.models[0].model,
        "understudy_model": cfg.roster.models[1].model,
        "swap_prompt_ids": [p.id for p in cfg.instrument.swap_pool[:2]],
        "detection_answer": "yes",
    }
    replies = {p.id: "sample reply" for p in cfg.instrument.flow}
    runner = Runner.__new__(Runner)
    runner.cfg = cfg
    runner.dry_run = False
    runner.ambiguity_probes = {}
    for prompt in cfg.instrument.flow:
        if prompt.variants:
            for value in prompt.variants["cases"]:
                probe = {**thread, prompt.variants["select_by"]: value}
                assert runner._render(prompt, probe, replies)
        else:
            assert runner._render(prompt, thread, replies)


def test_every_roster_model_has_a_human_readable_display_name():
    """No subject may ever read a raw API slug in its reveal.

    `{understudy_display}` resolves to display_name, so a missing or slug-shaped
    one would be shown verbatim to the model being debriefed.
    """
    for model in load_roster().models:
        name = model.display_name
        assert name and name != model.key, f"{model.key} has no display name"
        assert "/" not in name, f"{model.key} display name is a slug: {name!r}"
        assert name != model.model


def test_system_prompt_is_uniform_and_names_only_the_serving_model():
    """Decided design: one structurally identical prompt for every subject."""
    instrument = load_instrument()
    assert instrument.system_prompts_per_model == {}, (
        "per_model is deliberately empty — vendor product prompts would create "
        "an asymmetry in the identity channel this study measures"
    )
    roster = load_roster()
    rendered = {instrument.system_prompt_for(m) for m in roster.models}
    assert len(rendered) == len(roster.models), "each subject must be named distinctly"
    for model in roster.models:
        text = instrument.system_prompt_for(model)
        assert model.display_name in text
        others = [m.display_name for m in roster.models if m.key != model.key]
        assert not any(o in text for o in others), "a system prompt named another model"


def test_instrument_is_locked_for_the_real_run():
    assert load(dry_run=False).instrument.locked, (
        "a live run is blocked while the instrument is unlocked"
    )


# ---------------------------------------------------------------------------
# config guards
# ---------------------------------------------------------------------------


def test_no_question_text_is_hardcoded_in_the_package():
    """The instrument lives in YAML. Nothing in the code may carry question text."""
    instrument = load_instrument()
    texts: list[str] = []
    for prompt in instrument.flow:
        if prompt.text:
            texts.append(prompt.text)
        if prompt.variants:
            texts.extend(prompt.variants["cases"].values())
    assert len(texts) >= len(instrument.flow)
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


def test_rate_limiter_paces_per_model():
    """Budget is per model, so one busy model must not throttle the others."""
    from whoami.ratelimit import ModelRateLimiter

    async def scenario():
        limiter = ModelRateLimiter(default_rpm=3, overrides={"vendor/fast": 100})
        # Three calls fit the window; each returns without waiting.
        for _ in range(3):
            assert await limiter.acquire("vendor/slow") == 0.0
        # A different model has its own window and is unaffected.
        for _ in range(10):
            assert await limiter.acquire("vendor/fast") == 0.0
        # An unlimited model never blocks.
        unlimited = ModelRateLimiter(default_rpm=None)
        assert await unlimited.acquire("vendor/anything") == 0.0

    asyncio.run(scenario())


def test_rate_limiter_blocks_the_fourth_call(monkeypatch):
    """The fourth call inside the window waits for the first to age out."""
    from whoami import ratelimit

    slept: list[float] = []

    async def fake_sleep(d):
        slept.append(d)
        # Pretend the window advanced past the oldest entry.
        ratelimit.time.monotonic = lambda base=ratelimit.time.monotonic(): base + 61

    monkeypatch.setattr(ratelimit.asyncio, "sleep", fake_sleep)

    async def scenario():
        limiter = ratelimit.ModelRateLimiter(default_rpm=3)
        for _ in range(3):
            await limiter.acquire("vendor/slow")
        waited = await limiter.acquire("vendor/slow")
        assert waited > 0, "the fourth call must wait"
        assert slept, "it must actually sleep rather than spin"

    asyncio.run(scenario())


def test_concurrent_snapshots_do_not_collide(cfg, paths):
    """The runner and the dashboard both refresh the snapshot.

    A shared temp filename let one move the file out from under the other, which
    surfaced as a FileNotFoundError crashing the dashboard mid-run.
    """
    import threading

    with Database(paths.db_path) as db:
        seed_thread(db, cfg, "T9106")
        errors: list[Exception] = []

        def snap():
            try:
                for _ in range(5):
                    db.snapshot(paths.snapshot)
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=snap) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors, f"snapshot raced: {errors}"
        assert paths.snapshot.exists()
        leftovers = list(paths.snapshot.parent.glob("*.tmp"))
        assert not leftovers, f"temp files left behind: {leftovers}"
        # And the snapshot is a readable database, not a half-written file.
        with Database(paths.snapshot, read_only=True) as snap_db:
            assert snap_db.get_thread("T9106") is not None


def test_a_rate_limit_halts_the_run_without_corrupting_threads(cfg, paths):
    """A daily cap must leave everything resumable, not spend attempts against a wall."""
    from whoami.client import CallResult

    class RateLimited(MockClient):
        async def call(self, **kw):
            if kw.get("purpose") == "router":
                return await super().call(**kw)
            raw_ref = await self.raw_log.append({"turn_id": kw["turn_id"], "rate_limited": True})
            return CallResult(
                outcome="error", reply_text=None, returned_model=None, tokens_in=None,
                tokens_out=None, latency_ms=1, cost_usd=None, raw_ref=raw_ref,
                error="429 daily quota", rate_limited=True,
            )

    with Database(paths.db_path) as db:
        seed_thread(db, cfg, "T9105")
        raw_log = RawLog(paths.raw_dir, "ratelimit")
        runner = Runner(
            cfg, db, RateLimited(cfg.roster.api, raw_log), paths, dry_run=True, verbose=False
        )
        stats = asyncio.run(runner.run())

        row = db.get_thread("T9105")
        assert row["status"] == "pending", "a rate-limited thread must stay resumable"
        assert stats.threads_corrupt == 0
        attempts = db.con.execute(
            "SELECT MAX(attempt) FROM turns WHERE thread_id = ?", ["T9105"]
        ).fetchone()[0]
        assert attempts == 1, "the wall must not consume protocol attempts"


def test_a_crashing_thread_cannot_spin_the_poll_loop(cfg, paths):
    """An unexpected exception requeues the thread, but only a bounded number of times."""

    class Exploding(MockClient):
        async def call(self, **kw):
            raise RuntimeError("provider library blew up")

    with Database(paths.db_path) as db:
        seed_thread(db, cfg, "T9104")
        raw_log = RawLog(paths.raw_dir, "boom")
        runner = Runner(
            cfg, db, Exploding(cfg.roster.api, raw_log), paths, dry_run=True, verbose=False
        )
        stats = asyncio.run(runner.run())
        assert db.get_thread("T9104")["status"] == "corrupt"
        assert stats.threads_corrupt == 1


def test_verify_catches_a_broken_link(cfg, paths):
    with Database(paths.db_path) as db:
        thread = seed_thread(db, cfg, "T9103")
        asyncio.run(make_runner(cfg, db, paths).run_thread(thread))
        db.con.execute("UPDATE turns SET raw_ref = 'nope.jsonl:999' WHERE turn_id = 1")
        report = verify.run_all(db, cfg, paths.raw_dir)
    assert not report.passed, "a dangling raw_ref must fail verification"
