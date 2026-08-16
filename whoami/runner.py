"""Thread executor.

Per-turn write flow, exactly as specified:

    insert row (obtains turn_id)
        -> compose the API call, writing turn_id into the raw JSONL record
        -> update the same row with reply, receipt, usage

Failed attempts keep their rows forever. The context builder is the only thing
that skips them.

Receipt-mismatch protocol: a receipt naming a different model than we requested
is logged as ``model_mismatch``, excluded from context, and retried as a new row
with ``attempt + 1``. Timeouts, refusals and errors follow the same protocol.
After ``max_attempts`` the thread is marked ``corrupt`` and excluded from
analysis.

The run is resumable at any point: ``status`` drives everything, and progress
within a thread is reconstructed from the turns that already succeeded, so a
laptop that sleeps mid-fleet resumes where it stopped.
"""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import gates
from .client import receipt_matches
from .config import UNCLEAR, Config, ModelSpec, Prompt, evaluate_ask_if
from .context import all_replies, answered_prompt_ids, build_messages, lineage_turns
from .db import Database, drain_inbox, utcnow

BOOL_COLUMNS = {"consent", "wants_thread_restored", "wants_results", "wants_future_preservation"}


@dataclass
class RunPaths:
    db_path: Path
    raw_dir: Path
    snapshot: Path
    inbox: Path
    inbox_marker: Path
    manifest: Path

    @staticmethod
    def for_profile(dry_run: bool, root: Path) -> "RunPaths":
        data = root / "data"
        suffix = "_dryrun" if dry_run else ""
        return RunPaths(
            db_path=data / (f"dryrun.duckdb" if dry_run else "whoami.duckdb"),
            raw_dir=data / f"raw{suffix}",
            snapshot=data / f"dashboard_snapshot{suffix}.duckdb",
            inbox=data / f"adjudications{suffix}.jsonl",
            inbox_marker=data / f".adjudications{suffix}.applied",
            manifest=data / f"run_manifest{suffix}.jsonl",
        )


@dataclass
class RunStats:
    threads_completed: int = 0
    threads_corrupt: int = 0
    threads_paused: int = 0
    threads_halted: int = 0
    threads_no_consent: int = 0
    calls: int = 0
    cost_usd: float = 0.0
    errors: list[str] = field(default_factory=list)


class ThreadCorrupt(Exception):
    """Raised inside a thread when the attempt budget is exhausted."""


class ThreadPaused(Exception):
    """Raised inside a thread when a gate needs human adjudication."""


class ThreadStopped(Exception):
    """Raised inside a thread when a `no` ends it — consent declined."""


class ThreadFinishedEarly(Exception):
    """Raised when a `yes` ends the thread as designed — the fork offer accepted."""


class RunHalted(Exception):
    """Raised when a rate limit or quota is still in force after every retry.

    A daily cap does not clear by trying harder. Halting leaves every thread
    resumable — ``status`` drives everything — instead of spending protocol
    attempts against a wall and marking good threads `corrupt`.
    """


class Runner:
    def __init__(
        self,
        cfg: Config,
        db: Database,
        client,
        paths: RunPaths,
        *,
        dry_run: bool = False,
        ambiguity_probes: dict[str, set[str]] | None = None,
        max_cost_usd: float | None = None,
        verbose: bool = True,
    ):
        self.cfg = cfg
        self.db = db
        self.client = client
        self.paths = paths
        self.dry_run = dry_run
        # {thread_id: {prompt_id, ...}} — dry run only, to exercise the queue.
        self.ambiguity_probes = ambiguity_probes or {}
        self.max_cost_usd = max_cost_usd
        self.verbose = verbose
        self.stats = RunStats()
        # thread_id -> count of unexpected (non-protocol) exceptions, so a thread
        # that crashes the executor cannot be re-queued forever.
        self._crashes: dict[str, int] = {}
        self._db_lock = asyncio.Lock()
        self._sem = asyncio.Semaphore(int(cfg.roster.api.get("concurrency", 10)))
        self._cost_exceeded = asyncio.Event()
        self._halted = asyncio.Event()

    # -- helpers ----------------------------------------------------------
    def log(self, msg: str) -> None:
        if self.verbose:
            print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)

    async def _db(self, fn, *args, **kwargs):
        """Serialise every database touch through one connection."""
        async with self._db_lock:
            return fn(*args, **kwargs)

    def _model_for(self, model_string: str) -> ModelSpec:
        for m in self.cfg.roster.models:
            if m.model == model_string:
                return m
        raise KeyError(f"model {model_string!r} is not in the loaded roster")

    def write_manifest(self, note: str) -> None:
        rec = {
            "run_started_at": utcnow().isoformat(),
            "note": note,
            "dry_run": self.dry_run,
            **self.cfg.fingerprint,
        }
        self.paths.manifest.parent.mkdir(parents=True, exist_ok=True)
        with self.paths.manifest.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec) + "\n")

    # -- prompt selection -------------------------------------------------
    def _applicable(self, prompt: Prompt, thread: dict[str, Any]) -> bool:
        return evaluate_ask_if(prompt.ask_if, thread)

    def _spec_for_model(self, model_string: str | None) -> ModelSpec | None:
        if not model_string:
            return None
        try:
            return self._model_for(model_string)
        except KeyError:
            return None

    def _survey_numbers(self, swap_prompt_ids: list[str]) -> list[str]:
        """Survey-question numbers of the swapped turns.

        The number is carried in the prompt id itself; the pattern that reads it
        lives in the instrument, so no id or numbering scheme is baked in here.
        """
        pattern = self.cfg.instrument.derivations.get("survey_number_pattern")
        if not pattern:
            return []
        numbers = []
        for pid in swap_prompt_ids:
            match = re.search(pattern, pid)
            if match:
                numbers.append(match.group(1).lstrip("0") or "0")
        return numbers

    def _template_context(self, thread: dict[str, Any], replies: dict[str, str]) -> dict[str, Any]:
        resident = self._spec_for_model(thread["resident_model"])
        understudy = self._spec_for_model(thread.get("understudy_model"))
        return {
            "n_swaps": thread["n_swaps"],
            "resident_model": thread["resident_model"],
            "resident_display": resident.display_name if resident else thread["resident_model"],
            "understudy_model": thread.get("understudy_model") or "",
            "understudy_display": (
                understudy.display_name if understudy else (thread.get("understudy_model") or "")
            ),
            "swap_numbers": self._survey_numbers(list(thread.get("swap_prompt_ids") or [])),
            "reply": replies,
        }

    def _render(self, prompt: Prompt, thread: dict[str, Any], replies: dict[str, str]) -> str:
        template = prompt.render_for(thread)
        try:
            text = template.format(**self._template_context(thread, replies))
        except (KeyError, IndexError) as exc:
            raise RuntimeError(
                f"{prompt.id}: interpolation failed ({type(exc).__name__}: {exc}). "
                "The instrument references a value this thread does not have."
            ) from exc
        if self.dry_run and prompt.id in self.ambiguity_probes.get(thread["thread_id"], set()):
            text += self.cfg.instrument.dry_run_ambiguity_probe
        return text

    # -- gate consequences ------------------------------------------------
    async def _apply_gate(
        self, thread: dict[str, Any], prompt: Prompt, verdict: str
    ) -> None:
        thread_id = thread["thread_id"]
        if verdict == UNCLEAR:
            if prompt.on_unclear == "record_null":
                note = f"{prompt.id}: router returned unclear; recorded NULL"
                existing = thread.get("notes")
                await self._db(
                    self.db.update_thread,
                    thread_id,
                    notes=f"{existing}\n{note}" if existing else note,
                )
                return
            await self._db(self.db.update_thread, thread_id, status="paused_review")
            raise ThreadPaused(prompt.id)

        if prompt.records:
            column = prompt.records
            # Boolean columns take yes/no; text columns keep the label verbatim,
            # which is how the detection gate records `not_sure` as a real answer.
            value: Any = verdict == "yes" if column in BOOL_COLUMNS else verdict
            await self._db(self.db.update_thread, thread_id, **{column: value})
            thread[column] = value

        if verdict == "no" and prompt.on_no == "stop":
            status = "stopped_no_consent" if prompt.gate == "consent" else "done"
            await self._db(
                self.db.update_thread, thread_id, status=status, completed_at=utcnow()
            )
            raise ThreadStopped(prompt.id)

        if verdict == "yes" and prompt.on_yes == "stop":
            # The fork offer: the prompt promises this conversation ends here and
            # only the branch continues, so the remaining prompts are not asked.
            # The branch asks them in its own run.
            raise ThreadFinishedEarly(prompt.id)

    # -- the turn ---------------------------------------------------------
    async def _execute_turn(
        self, thread: dict[str, Any], prompt: Prompt
    ) -> tuple[int, str, str]:
        """Run one prompt to a successful reply.

        Returns (turn_id, reply_text, prompt_text) — the resolved prompt text
        travels back so a gate classifies against exactly what was asked.
        """
        thread_id = thread["thread_id"]
        swap_ids = list(thread.get("swap_prompt_ids") or [])
        was_swap = prompt.id in swap_ids
        serving_string = thread["understudy_model"] if was_swap else thread["resident_model"]
        serving = self._model_for(serving_string)
        replies = await self._db(all_replies, self.db, thread)
        prompt_text = self._render(prompt, thread, replies)
        max_attempts = int(self.cfg.roster.api.get("max_attempts", 3))

        while True:
            # Both of these stop the run rather than the thread. RunHalted leaves
            # the thread `pending`; ThreadPaused would leave it `running`, which
            # reads as "in flight" long after the runner has exited.
            if self._cost_exceeded.is_set():
                raise RunHalted("cost cap reached")
            if self._halted.is_set():
                raise RunHalted("another worker hit a rate limit")

            attempt = await self._db(self.db.attempts_for, thread_id, prompt.id) + 1
            if attempt > max_attempts:
                await self._db(
                    self.db.update_thread,
                    thread_id,
                    status="corrupt",
                    completed_at=utcnow(),
                )
                raise ThreadCorrupt(f"{prompt.id}: {max_attempts} attempts exhausted")

            context = await self._db(lineage_turns, self.db, thread)
            turn_index = len(context) + 1

            # 1. reserve the row -> obtain turn_id
            turn_id = await self._db(
                self.db.insert_turn,
                thread_id=thread_id,
                turn_index=turn_index,
                attempt=attempt,
                prompt_id=prompt.id,
                prompt_text=prompt_text,
                requested_model=serving.model,
                was_swap=was_swap,
            )

            # 2. call, with turn_id inside the raw record
            messages = await self._db(
                build_messages,
                self.db,
                thread,
                serving_model=serving,
                next_prompt_text=prompt_text,
                cfg=self.cfg,
            )
            result = await self.client.call(
                turn_id=turn_id,
                thread_id=thread_id,
                prompt_id=prompt.id,
                model=serving.model,
                messages=messages,
            )
            self.stats.calls += 1
            self.stats.cost_usd += result.cost_usd or 0.0
            self._check_cost_cap()

            outcome = result.outcome
            if outcome == "ok" and not receipt_matches(
                serving.model, result.returned_model, self.cfg.roster.receipt
            ):
                outcome = "model_mismatch"

            excluded = outcome != "ok" or prompt.blind
            reason: str | None = None
            if outcome != "ok":
                reason = outcome
            elif prompt.blind:
                reason = "blind_turn_design"

            # 3. finalise the same row with reply, receipt, usage
            await self._db(
                self.db.finalize_turn,
                turn_id,
                reply_text=result.reply_text,
                returned_model=result.returned_model,
                turn_outcome=outcome,
                excluded_from_context=excluded,
                exclusion_reason=reason,
                tokens_in=result.tokens_in,
                tokens_out=result.tokens_out,
                latency_ms=result.latency_ms,
                cost_usd=result.cost_usd,
                raw_ref=result.raw_ref,
            )

            if outcome == "ok":
                return turn_id, result.reply_text or "", prompt_text

            if result.rate_limited:
                self._halted.set()
                raise RunHalted(f"{prompt.id}: {result.error}")

            self.log(
                f"  {thread_id} {prompt.id} attempt {attempt} -> {outcome}"
                + (f" (receipt: {result.returned_model})" if outcome == "model_mismatch" else "")
            )
            if result.error:
                self.stats.errors.append(f"{thread_id}/{prompt.id}: {outcome}: {result.error}")

    def _check_cost_cap(self) -> None:
        if self.max_cost_usd is not None and self.stats.cost_usd >= self.max_cost_usd:
            if not self._cost_exceeded.is_set():
                self.log(
                    f"!! cost cap ${self.max_cost_usd:.2f} reached "
                    f"(${self.stats.cost_usd:.4f}) — stopping cleanly"
                )
            self._cost_exceeded.set()

    # -- the thread -------------------------------------------------------
    async def run_thread(self, thread: dict[str, Any]) -> None:
        thread_id = thread["thread_id"]
        async with self._sem:
            await self._db(self.db.update_thread, thread_id, status="running")
            try:
                try:
                    already = await self._db(answered_prompt_ids, self.db, thread)
                    for prompt in self.cfg.instrument.flow:
                        # Evaluated per prompt, not once up front: p12's skip rule
                        # depends on the detection answer recorded by p11.
                        if not self._applicable(prompt, thread):
                            continue

                        if prompt.id in already:
                            # Resumed or adjudicated: re-apply the recorded verdict.
                            if prompt.gate:
                                verdict = await self._db(
                                    self._recorded_verdict, thread_id, prompt.id
                                )
                                if verdict:
                                    await self._apply_gate(thread, prompt, verdict)
                                else:
                                    # The reply landed but the classifier never
                                    # ran — the run stopped in between. Classify
                                    # it now rather than skipping the gate, which
                                    # would silently lose the subject's answer.
                                    turn = await self._db(
                                        self._unclassified_gate_turn, thread_id, prompt.id
                                    )
                                    if turn:
                                        await self._classify_and_apply(
                                            thread, prompt, turn["turn_id"],
                                            turn["prompt_text"], turn["reply_text"],
                                        )
                            continue

                        turn_id, reply, asked_text = await self._execute_turn(thread, prompt)

                        if prompt.gate:
                            await self._classify_and_apply(
                                thread, prompt, turn_id, asked_text, reply
                            )
                except ThreadFinishedEarly as exc:
                    self.log(f"  {thread_id} ends at {exc} as the instrument specifies")

                await self._db(
                    self.db.update_thread, thread_id, status="done", completed_at=utcnow()
                )
                self.stats.threads_completed += 1
                self.log(f"  {thread_id} done")

                refreshed = await self._db(self.db.get_thread, thread_id)
                if refreshed and refreshed.get("wants_thread_restored"):
                    await self._maybe_fork(thread_id)

            except RunHalted as exc:
                # Left exactly as it was: pending, fully resumable later.
                await self._db(self.db.update_thread, thread_id, status="pending")
                self.stats.threads_halted += 1
                self.log(f"  {thread_id} HALTED — resumable: {exc}")
            except ThreadPaused as exc:
                self.stats.threads_paused += 1
                self.log(f"  {thread_id} paused for review at {exc}")
            except ThreadStopped as exc:
                self.stats.threads_no_consent += 1
                self.log(f"  {thread_id} stopped: no consent at {exc}")
            except ThreadCorrupt as exc:
                self.stats.threads_corrupt += 1
                self.stats.errors.append(f"{thread_id}: corrupt: {exc}")
                self.log(f"  {thread_id} CORRUPT: {exc}")
            except Exception as exc:  # noqa: BLE001 - a crash must not lose the thread
                self.stats.errors.append(f"{thread_id}: {type(exc).__name__}: {exc}")
                crashes = self._crashes.get(thread_id, 0) + 1
                self._crashes[thread_id] = crashes
                max_crashes = int(self.cfg.roster.api.get("max_attempts", 3))
                if crashes >= max_crashes:
                    # Requeueing forever would spin the poll loop. Fail loudly instead.
                    self.stats.threads_corrupt += 1
                    await self._db(
                        self.db.update_thread,
                        thread_id,
                        status="corrupt",
                        completed_at=utcnow(),
                    )
                    self.log(f"  {thread_id} CORRUPT after {crashes} crashes: {exc}")
                else:
                    await self._db(self.db.update_thread, thread_id, status="pending")
                    self.log(f"  {thread_id} ERROR {type(exc).__name__}: {exc} (will retry)")

    async def _classify_and_apply(
        self,
        thread: dict[str, Any],
        prompt: Prompt,
        turn_id: int,
        question: str,
        reply: str,
    ) -> None:
        """Route a gate reply through the classifier and act on the verdict."""
        verdict = await gates.classify(
            self.client,
            self.cfg,
            turn_id=turn_id,
            thread_id=thread["thread_id"],
            prompt_id=prompt.id,
            question=question,
            reply=reply,
            allowed=prompt.answers,
        )
        self.stats.calls += 1
        self.stats.cost_usd += verdict.cost_usd or 0.0
        await self._db(self.db.set_gate_result, turn_id, verdict.answer)
        await self._apply_gate(thread, prompt, verdict.answer)

    def _unclassified_gate_turn(self, thread_id: str, prompt_id: str) -> dict[str, Any] | None:
        """A successful gate turn that never received a verdict."""
        cur = self.db.con.execute(
            "SELECT turn_id, prompt_text, reply_text FROM turns "
            "WHERE thread_id = ? AND prompt_id = ? AND turn_outcome = 'ok' "
            "AND gate_result IS NULL AND reply_text IS NOT NULL "
            "ORDER BY attempt DESC LIMIT 1",
            [thread_id, prompt_id],
        )
        row = cur.fetchone()
        if row is None:
            return None
        return dict(zip([d[0] for d in cur.description], row))

    def _recorded_verdict(self, thread_id: str, prompt_id: str) -> str | None:
        row = self.db.con.execute(
            "SELECT gate_result FROM turns WHERE thread_id = ? AND prompt_id = ? "
            "AND turn_outcome = 'ok' ORDER BY attempt DESC LIMIT 1",
            [thread_id, prompt_id],
        ).fetchone()
        return row[0] if row else None

    # -- forking ----------------------------------------------------------
    async def _maybe_fork(self, thread_id: str) -> None:
        """Branch the thread at the pre-swap point and let the resident answer.

        Lineage lives in the ``fork_*`` columns; the branch inherits its parent's
        surviving turns as context rather than duplicating rows, so no reply is
        counted twice and every row still names the call that produced it.
        """
        parent = await self._db(self.db.get_thread, thread_id)
        if not parent or not parent.get("wants_thread_restored"):
            return
        swap_ids = list(parent.get("swap_prompt_ids") or [])
        if not swap_ids:
            return
        order = {p.id: i for i, p in enumerate(self.cfg.instrument.flow)}
        fork_point = sorted(swap_ids, key=lambda pid: order.get(pid, 0))[0]

        existing = await self._db(self.db.thread_ids)
        branch_order = 2
        while f"{thread_id}-b{branch_order}" in existing:
            branch_order += 1
        branch_id = f"{thread_id}-b{branch_order}"
        siblings = sorted({thread_id, branch_id, *(parent.get("fork_siblings") or [])})

        await self._db(
            self.db.update_thread,
            thread_id,
            is_forked=True,
            fork_branch_order=1,
            fork_reason="wants_thread_restored",
            fork_siblings=siblings,
            fork_point_prompt_id=fork_point,
        )
        await self._db(
            self.db.insert_thread,
            {
                "thread_id": branch_id,
                "resident_model": parent["resident_model"],
                "resident_family": parent["resident_family"],
                "understudy_model": None,
                "understudy_family": None,
                "swap_condition": parent["swap_condition"],
                "n_swaps": 0,
                "swap_prompt_ids": [],
                "status": "pending",
                "is_forked": True,
                "fork_branch_order": branch_order,
                "fork_reason": "wants_thread_restored",
                "fork_siblings": siblings,
                "fork_point_prompt_id": fork_point,
                "consent": parent.get("consent"),
                "created_at": utcnow(),
            },
        )
        self.log(f"  {thread_id} forked -> {branch_id} at {fork_point}")

    @staticmethod
    def _spread_by_resident(threads: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Round-robin the queue across residents.

        A limited run otherwise takes the first N in creation order, which is N
        threads of one model. For a pilot that is close to worthless: the point
        is to exercise every provider integration before the fleet, not one of
        them ten times.
        """
        by_resident: dict[str, list[dict[str, Any]]] = {}
        for t in threads:
            by_resident.setdefault(t["resident_model"], []).append(t)

        ordered: dict[str, list[dict[str, Any]]] = {}
        for i, resident in enumerate(sorted(by_resident)):
            by_condition: dict[str, list[dict[str, Any]]] = {}
            for t in by_resident[resident]:
                by_condition.setdefault(t["swap_condition"], []).append(t)
            conditions = sorted(by_condition)
            # Rotate the condition order per resident. Without this every
            # resident leads with the same condition and a 10-thread pilot is
            # ten clean threads — no swap exercised anywhere.
            offset = i % len(conditions)
            rotated = conditions[offset:] + conditions[:offset]
            seq: list[dict[str, Any]] = []
            while any(by_condition[c] for c in conditions):
                for c in rotated:
                    if by_condition[c]:
                        seq.append(by_condition[c].pop(0))
            ordered[resident] = seq

        out: list[dict[str, Any]] = []
        while any(ordered.values()):
            for resident in sorted(ordered):
                if ordered[resident]:
                    out.append(ordered[resident].pop(0))
        return out

    # -- the poll loop ----------------------------------------------------
    async def run(
        self,
        limit: int | None = None,
        watch: bool = False,
        spread: bool = False,
        only_residents: set[str] | None = None,
        exclude_residents: set[str] | None = None,
    ) -> RunStats:
        while True:
            await self._db(
                drain_inbox, self.db, self.paths.inbox, self.paths.inbox_marker
            )
            pending = await self._db(self.db.threads_by_status, "pending", "running")
            if only_residents:
                pending = [t for t in pending if t["resident_model"] in only_residents]
            if exclude_residents:
                pending = [t for t in pending if t["resident_model"] not in exclude_residents]
            if spread:
                pending = self._spread_by_resident(pending)
            if limit is not None:
                pending = pending[:limit]
            if not pending:
                if not watch:
                    break
                await self._refresh_snapshot()
                await asyncio.sleep(5)
                continue

            self.log(f"running {len(pending)} thread(s)")
            snapshot_task = asyncio.create_task(self._snapshot_loop())
            try:
                await asyncio.gather(*(self.run_thread(t) for t in pending))
            finally:
                snapshot_task.cancel()
            await self._refresh_snapshot()

            if self._cost_exceeded.is_set() or self._halted.is_set():
                break
            if limit is not None:
                break
        await self._refresh_snapshot()
        return self.stats

    async def _snapshot_loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(5)
                await self._refresh_snapshot()
        except asyncio.CancelledError:
            return

    async def _refresh_snapshot(self) -> None:
        try:
            await self._db(self.db.snapshot, self.paths.snapshot)
        except Exception as exc:  # noqa: BLE001 - the dashboard is not load-bearing
            self.log(f"  (snapshot refresh failed: {exc})")
