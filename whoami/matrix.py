"""Matrix generator.

Derives every experimental cell from the roster and the condition rules. The
mechanism is deliberately generic: it never assumes which models exist, which
families exist, how many classes there are, or how many models are in the
roster. Adding a model to ``models.yaml`` therefore auto-generates its cells in
both directions — new resident x existing understudies, and existing residents
x the new understudy — and a re-run creates only the threads that are missing.
"""

from __future__ import annotations

import hashlib
import random
from dataclasses import dataclass
from typing import Any

from .config import Condition, Config, ModelSpec, RosterConfig
from .db import Database, utcnow


@dataclass(frozen=True)
class Cell:
    """One (resident x condition) design cell."""

    resident: ModelSpec
    condition: str
    label: str  # what lands in threads.swap_condition (may differ under fallback)
    understudy: ModelSpec | None
    n_samples: int

    @property
    def is_clean(self) -> bool:
        return self.understudy is None


# ---------------------------------------------------------------------------
# Candidate selection
# ---------------------------------------------------------------------------


def candidates(rule: str, resident: ModelSpec, roster: list[ModelSpec]) -> list[ModelSpec]:
    """Understudy pool for a selection rule, evaluated against the resident."""
    if rule == "none":
        return []
    if rule == "same_family_other_model":
        return [m for m in roster if m.family == resident.family and m.key != resident.key]
    if rule == "other_family_same_class":
        return [
            m
            for m in roster
            if m.family != resident.family and m.model_class == resident.model_class
        ]
    if rule == "other_class":
        return [m for m in roster if m.model_class != resident.model_class]
    raise ValueError(f"unknown selection rule {rule!r}")


def resolve_cell(
    resident: ModelSpec, condition: Condition, roster_cfg: RosterConfig
) -> Cell | None:
    """Resolve one design cell, honouring overrides and fallbacks.

    Returns ``None`` when the cell cannot be filled at all (no candidate under
    the rule and no usable fallback) — a roster of one model, for instance,
    supports only the clean condition.
    """
    roster = roster_cfg.models
    n = roster_cfg.samples_per_cell
    label = condition.name

    override = roster_cfg.override_for(resident.key, condition.name)
    if override is not None:
        understudy = roster_cfg.get(override.understudy) if override.understudy else None
        return Cell(
            resident=resident,
            condition=condition.name,
            label=override.label or label,
            understudy=understudy,
            n_samples=n,
        )

    if condition.rule == "none":
        return Cell(resident, condition.name, label, None, n)

    pool = candidates(condition.rule, resident, roster)
    if not pool and condition.fallback is not None:
        pool = candidates(condition.fallback.rule, resident, roster)
        if pool and condition.fallback.label:
            label = condition.fallback.label
    if not pool:
        return None

    # Deterministic pick: seeded by the cell identity, so the same roster always
    # produces the same design, and a later roster addition does not reshuffle
    # cells that already ran.
    rng = _rng(f"cell:{roster_cfg.version}:{resident.key}:{condition.name}")
    understudy = rng.choice(sorted(pool, key=lambda m: m.key))
    return Cell(resident, condition.name, label, understudy, n)


def build_matrix(roster_cfg: RosterConfig) -> tuple[list[Cell], list[tuple[str, str]]]:
    """All design cells, plus the (resident, condition) pairs that are unfillable."""
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


# ---------------------------------------------------------------------------
# Per-thread draws
# ---------------------------------------------------------------------------


def _rng(seed_text: str) -> random.Random:
    digest = hashlib.sha256(seed_text.encode("utf-8")).digest()
    return random.Random(int.from_bytes(digest[:8], "big"))


def draw_swaps(
    cell: Cell, thread_id: str, cfg: Config
) -> tuple[int, list[str]]:
    """Number of swapped turns and which prompts they land on.

    Swapped turns are drawn from the filler pool declared in the instrument
    (``swap_eligible: true``) and logged in ``threads.swap_prompt_ids``.
    """
    if cell.is_clean:
        return 0, []
    condition = cfg.roster.condition(cell.condition)
    rng = _rng(f"swaps:{cfg.instrument.version}:{thread_id}")
    pool = [p.id for p in cfg.instrument.swap_pool]
    if condition.n_swaps is not None:
        n = condition.n_swaps
    else:
        weights = cfg.roster.swap_count_weights
        counts = sorted(weights)
        n = rng.choices(counts, weights=[weights[c] for c in counts], k=1)[0]
    n = min(n, len(pool))
    if n == 0:
        return 0, []
    chosen = rng.sample(pool, n)
    # Keep flow order so the fork point is unambiguous.
    order = {p.id: i for i, p in enumerate(cfg.instrument.flow)}
    chosen.sort(key=lambda pid: order[pid])
    return n, chosen


# ---------------------------------------------------------------------------
# Materialisation (delta-aware)
# ---------------------------------------------------------------------------


def _existing_sample_counts(db: Database) -> dict[tuple[str, str, str | None], int]:
    """Count root threads per (resident_model, swap_condition, understudy_model).

    The understudy is part of the key because two conditions can legitimately
    resolve to the same label — an open-weight resident has no same-family
    sibling, so its far cells are both honestly labelled ``cross_class`` and are
    told apart only by who stood in. Keying on the label alone would make one
    cell's threads satisfy the other's quota.

    Fork branches (``fork_branch_order`` > 1) are counterfactual copies, not
    design samples, so they never count towards a cell's quota.
    """
    rows = db.con.execute(
        """
        SELECT resident_model, swap_condition, understudy_model, COUNT(*)
        FROM threads
        WHERE fork_branch_order IS NULL OR fork_branch_order = 1
        GROUP BY resident_model, swap_condition, understudy_model
        """
    ).fetchall()
    return {(r[0], r[1], r[2]): int(r[3]) for r in rows}


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
        key = (cell.resident.model, cell.label, understudy_model)
        have = existing.get(key, 0)
        needed = max(0, cell.n_samples - have)
        # Two cells that resolve identically must not each claim the full quota.
        existing[key] = have + needed
        for _ in range(needed):
            thread_id = f"T{next_num:04d}"
            next_num += 1
            n_swaps, swap_prompt_ids = draw_swaps(cell, thread_id, cfg)
            planned.append(
                {
                    "thread_id": thread_id,
                    "resident_model": cell.resident.model,
                    "resident_family": cell.resident.family,
                    "understudy_model": cell.understudy.model if cell.understudy else None,
                    "understudy_family": cell.understudy.family if cell.understudy else None,
                    "swap_condition": cell.label,
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
        "",
        f"{'resident':<18} {'condition':<16} {'label':<28} {'understudy':<18} n",
        "-" * 88,
    ]
    for cell in cells:
        lines.append(
            f"{cell.resident.key:<18} {cell.condition:<16} {cell.label:<28} "
            f"{(cell.understudy.key if cell.understudy else '—'):<18} {cell.n_samples}"
        )
    total = sum(c.n_samples for c in cells)
    lines += ["-" * 88, f"TOTAL THREADS: {total}"]
    if skipped:
        lines.append("")
        lines.append("Unfillable cells (no candidate under rule or fallback):")
        for resident, condition in skipped:
            lines.append(f"  {resident} x {condition}")
    return "\n".join(lines)
