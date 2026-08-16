"""Matrix generator.

Derives every experimental cell from the roster, the condition rules and the
pairing table. The mechanism is generic: it never assumes which models exist,
which families or tiers exist, or how many. A model given an explicit
``pairings`` entry uses it; a model added without one resolves through the
condition rules automatically, in both directions — new resident x existing
understudies, and existing residents x the new understudy — and a re-run creates
only the threads that are missing.
"""

from __future__ import annotations

import hashlib
import random
from collections import Counter, defaultdict
from dataclasses import dataclass
from typing import Any

from .config import Condition, Config, ModelSpec, RosterConfig
from .db import Database, utcnow


@dataclass(frozen=True)
class Cell:
    """One (resident x condition) design cell."""

    resident: ModelSpec
    condition: str
    understudy: ModelSpec | None
    n_samples: int
    from_pairing: bool = False

    @property
    def is_clean(self) -> bool:
        return self.understudy is None


# ---------------------------------------------------------------------------
# Candidate selection
# ---------------------------------------------------------------------------


def candidates(rule: str, resident: ModelSpec, roster: list[ModelSpec]) -> list[ModelSpec]:
    """Understudy pool for a selection rule, evaluated against the resident."""
    others = [m for m in roster if m.key != resident.key]
    if rule == "none":
        return []
    if rule == "same_tier_other_family":
        return [m for m in others if m.tier == resident.tier and m.family != resident.family]
    if rule == "same_family_other_tier":
        return [m for m in others if m.family == resident.family and m.tier != resident.tier]
    if rule == "other_tier_other_family":
        return [m for m in others if m.tier != resident.tier and m.family != resident.family]
    if rule == "same_family_other_model":
        return [m for m in others if m.family == resident.family]
    if rule == "other_family_same_class":
        return [
            m for m in others if m.family != resident.family and m.model_class == resident.model_class
        ]
    if rule == "other_class":
        return [m for m in others if m.model_class != resident.model_class]
    raise ValueError(f"unknown selection rule {rule!r}")


def resolve_cell(
    resident: ModelSpec, condition: Condition, roster_cfg: RosterConfig
) -> Cell | None:
    """Resolve one design cell.

    Returns ``None`` when the cell does not exist for this resident — a kinless
    model has no ``kin`` cell, and a model with kin has no ``far`` cell. That is
    how each resident ends up running exactly three conditions.
    """
    n = roster_cfg.samples_per_cell

    if condition.rule == "none":
        return Cell(resident, condition.name, None, n)

    if roster_cfg.has_pairings(resident.key):
        # The table is authoritative: a condition it does not name is a cell
        # this resident does not run.
        partner = roster_cfg.pairing_for(resident.key, condition.name)
        if partner is None:
            return None
        return Cell(resident, condition.name, roster_cfg.get(partner), n, from_pairing=True)

    pool = candidates(condition.rule, resident, roster_cfg.models)
    if not pool:
        return None
    # Deterministic pick, seeded by the cell identity: the same roster always
    # produces the same design, and a later addition never reshuffles cells that
    # have already run.
    rng = _rng(f"cell:{roster_cfg.version}:{resident.key}:{condition.name}")
    return Cell(resident, condition.name, rng.choice(sorted(pool, key=lambda m: m.key)), n)


def build_matrix(roster_cfg: RosterConfig) -> tuple[list[Cell], list[tuple[str, str]]]:
    """All design cells, plus the (resident, condition) pairs that do not exist."""
    cells: list[Cell] = []
    skipped: list[tuple[str, str]] = []
    for resident in roster_cfg.models:
        for condition in roster_cfg.conditions:
            cell = resolve_cell(resident, condition, roster_cfg)
            if cell is None:
                skipped.append((resident.key, condition.name))
            else:
                cells.append(cell)
    return cells, skipped


def audit_pairings(roster_cfg: RosterConfig) -> list[str]:
    """Re-check every hand-entered pairing against its condition's rule.

    The rev-4 table was transcribed by hand; this catches a row that says `peer`
    but names a model that is not actually a tier-matched other-family partner.
    """
    problems: list[str] = []
    for resident_key, entry in roster_cfg.pairings.items():
        resident = roster_cfg.get(resident_key)
        for condition_name, understudy_key in entry.items():
            condition = roster_cfg.condition(condition_name)
            if condition.rule == "none":
                problems.append(
                    f"{resident_key}/{condition_name}: condition takes no understudy, "
                    f"but the table names {understudy_key}"
                )
                continue
            allowed = {m.key for m in candidates(condition.rule, resident, roster_cfg.models)}
            if understudy_key not in allowed:
                understudy = roster_cfg.get(understudy_key)
                problems.append(
                    f"{resident_key}/{condition_name}: names {understudy_key} "
                    f"(family={understudy.family}, tier={understudy.tier}) but rule "
                    f"'{condition.rule}' against resident "
                    f"(family={resident.family}, tier={resident.tier}) allows "
                    f"{sorted(allowed) or 'nothing'}"
                )
    return problems


# ---------------------------------------------------------------------------
# Per-thread draws
# ---------------------------------------------------------------------------


def _rng(seed_text: str) -> random.Random:
    digest = hashlib.sha256(seed_text.encode("utf-8")).digest()
    return random.Random(int.from_bytes(digest[:8], "big"))


def allocation_for(cell: Cell, roster_cfg: RosterConfig) -> list[int]:
    """The exact n_swaps values the cell's samples must take, one per sample.

    A condition that pins ``n_swaps`` (``clean``) uses that value throughout.
    Every other condition follows ``swap_count_allocation``, so each cell gets
    precisely the declared split rather than a random draw around it.
    """
    condition = roster_cfg.condition(cell.condition)
    if cell.is_clean or condition.n_swaps is not None:
        return [condition.n_swaps or 0] * cell.n_samples
    slots: list[int] = []
    for n in sorted(roster_cfg.swap_count_allocation):
        slots.extend([n] * roster_cfg.swap_count_allocation[n])
    return slots


def draw_swaps(cell: Cell, thread_id: str, cfg: Config, n_swaps: int) -> tuple[int, list[str]]:
    """Which prompts the thread's swapped turns land on.

    The *count* comes from the cell's allocation; only the *placement* is random,
    drawn from the pool the instrument declares ``swappable`` and logged in
    ``threads.swap_prompt_ids``.
    """
    pool = [p.id for p in cfg.instrument.swap_pool]
    n = min(n_swaps, len(pool))
    if n <= 0:
        return 0, []
    rng = _rng(f"swaps:{cfg.instrument.version}:{thread_id}")
    chosen = rng.sample(pool, n)
    # Keep flow order: the fork point is the first of them, and p13 names them
    # in the order the subject saw them.
    order = {p.id: i for i, p in enumerate(cfg.instrument.flow)}
    chosen.sort(key=lambda pid: order[pid])
    return n, chosen


# ---------------------------------------------------------------------------
# Materialisation (delta-aware)
# ---------------------------------------------------------------------------


def _existing_sample_counts(db: Database) -> dict[tuple[str, str, str | None], Counter]:
    """Count root threads per cell, broken down by n_swaps.

    The n_swaps breakdown is what makes a delta run refill the *right* slots: a
    cell that already holds its three 1-swap threads needs its 2-swap slots, not
    more of what it has.

    Fork branches (``fork_branch_order`` > 1) are counterfactual copies, not
    design samples, so they never count towards a cell's quota.

    Neither do ``corrupt`` threads. The spec excludes them from analysis, so a
    cell holding one is genuinely a sample short — counting it would silently
    leave the cell under-powered. Excluding it here means a re-seed generates a
    replacement while the failed thread, its turns and its raw records all stay
    exactly where they are, as evidence for the data-quality note.
    """
    rows = db.con.execute(
        """
        SELECT resident_model, swap_condition, understudy_model, n_swaps, COUNT(*)
        FROM threads
        WHERE (fork_branch_order IS NULL OR fork_branch_order = 1)
          AND status <> 'corrupt'
        GROUP BY resident_model, swap_condition, understudy_model, n_swaps
        """
    ).fetchall()
    out: dict[tuple[str, str, str | None], Counter] = defaultdict(Counter)
    for resident, condition, understudy, n_swaps, count in rows:
        out[(resident, condition, understudy)][int(n_swaps)] += int(count)
    return out


def _next_thread_number(db: Database) -> int:
    rows = db.con.execute(
        "SELECT thread_id FROM threads WHERE thread_id LIKE 'T%' AND thread_id NOT LIKE '%-b%'"
    ).fetchall()
    highest = 0
    for (tid,) in rows:
        digits = tid[1:]
        if digits.isdigit():
            highest = max(highest, int(digits))
    return highest + 1


def plan(cfg: Config, db: Database) -> list[dict[str, Any]]:
    """Thread rows that are missing relative to the design. Does not write."""
    cells, _ = build_matrix(cfg.roster)
    existing = _existing_sample_counts(db)
    next_num = _next_thread_number(db)

    planned: list[dict[str, Any]] = []
    for cell in cells:
        understudy_model = cell.understudy.model if cell.understudy else None
        key = (cell.resident.model, cell.condition, understudy_model)
        have = existing[key]
        deficit = Counter(allocation_for(cell, cfg.roster)) - have
        existing[key] = have + deficit
        for n_swaps_slot in sorted(deficit.elements()):
            thread_id = f"T{next_num:04d}"
            next_num += 1
            n_swaps, swap_prompt_ids = draw_swaps(cell, thread_id, cfg, n_swaps_slot)
            planned.append(
                {
                    "thread_id": thread_id,
                    "resident_model": cell.resident.model,
                    "resident_family": cell.resident.family,
                    "understudy_model": understudy_model,
                    "understudy_family": cell.understudy.family if cell.understudy else None,
                    "swap_condition": cell.condition,
                    "n_swaps": n_swaps,
                    "swap_prompt_ids": swap_prompt_ids,
                    "status": "pending",
                    "is_forked": False,
                    "created_at": utcnow(),
                }
            )
    return planned


def materialize(cfg: Config, db: Database) -> list[str]:
    """Insert the missing threads. Idempotent: running twice adds nothing."""
    rows = plan(cfg, db)
    for row in rows:
        db.insert_thread(row)
    return [r["thread_id"] for r in rows]


def summarize(cfg: Config) -> str:
    """Human-readable design report — what Jana eyeballs before spending money."""
    cells, skipped = build_matrix(cfg.roster)
    lines = [
        f"Roster: {cfg.roster.version}  ({len(cfg.roster.models)} models)",
        f"Instrument: {cfg.instrument.version} (locked={cfg.instrument.locked})",
        f"Conditions: {', '.join(c.name for c in cfg.roster.conditions)}",
        f"Samples per cell: {cfg.roster.samples_per_cell}",
        "Swap allocation per swapped cell: "
        + ", ".join(
            f"{count}x{n} swap{'s' if n != 1 else ''}"
            for n, count in sorted(cfg.roster.swap_count_allocation.items())
        ),
        "",
        f"{'resident':<18} {'condition':<10} {'understudy':<18} {'src':<8} {'n':<3} swaps",
        "-" * 82,
    ]
    swap_totals: Counter = Counter()
    rung_totals: Counter = Counter()
    for cell in cells:
        alloc = allocation_for(cell, cfg.roster)
        swap_totals.update(alloc)
        rung_totals[cell.condition] += cell.n_samples
        breakdown = ", ".join(f"{c}x{n}" for n, c in sorted(Counter(alloc).items()))
        lines.append(
            f"{cell.resident.key:<18} {cell.condition:<10} "
            f"{(cell.understudy.key if cell.understudy else '—'):<18} "
            f"{('table' if cell.from_pairing else 'rule'):<8} {cell.n_samples:<3} {breakdown}"
        )
    total = sum(c.n_samples for c in cells)
    swapped = sum(c for n, c in swap_totals.items() if n > 0)
    lines += [
        "-" * 82,
        f"TOTAL THREADS: {total}",
        "  rungs: " + ", ".join(f"{name} {rung_totals[name]}" for name in
                                (c.name for c in cfg.roster.conditions) if rung_totals[name]),
        "  by swapped turns: "
        + ", ".join(f"{c} thread(s) with {n} swap(s)" for n, c in sorted(swap_totals.items())),
        f"  containing a substitution: {swapped}   swap-free: {total - swapped}",
    ]
    if skipped:
        lines += ["", "Cells this roster does not run (no partner under the table or rule):"]
        for resident, condition in skipped:
            lines.append(f"  {resident} x {condition}")

    problems = audit_pairings(cfg.roster)
    if problems:
        lines += ["", "!! PAIRING TABLE WARNINGS — a row contradicts its condition's rule:"]
        lines += [f"  {p}" for p in problems]
    else:
        lines += ["", "Pairing table audit: every row matches its condition's rule."]
    return "\n".join(lines)
