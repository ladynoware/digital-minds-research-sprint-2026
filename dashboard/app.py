"""Who Am I? — progress dashboard and review queue.

Run with:  python -m whoami dashboard   (add --dry-run for the free-tier database)

Two views, per spec:

* **Progress** — auto-refreshing counts by status, per-model / per-condition
  completion grid, running cost tally, error list.
* **Review queue** — each ``paused_review`` thread with the ambiguous reply in
  context and [Interpret as YES] [Interpret as NO] [Custom note] buttons. The
  thread resumes on the runner's next poll.

DuckDB permits a single read-write process, and the runner owns it while a fleet
is in flight. So this app reads the snapshot the runner refreshes every few
seconds, and posts adjudications to an append-only inbox that the runner drains
on each poll. When no runner is live, the app refreshes the snapshot itself and
drains the inbox directly, so it works standalone too.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import duckdb
import pandas as pd
import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from whoami.config import REPO_ROOT, ConfigError, load_instrument  # noqa: E402
from whoami.db import Database, drain_inbox, post_adjudication, read_inbox  # noqa: E402
from whoami.runner import RunPaths  # noqa: E402

DRY_RUN = os.environ.get("WHOAMI_DRY_RUN", "0") == "1"
PATHS = RunPaths.for_profile(DRY_RUN, REPO_ROOT)

try:
    INSTRUMENT = load_instrument()
except (ConfigError, OSError):
    # The dashboard is still useful for progress if the instrument will not load.
    INSTRUMENT = None

st.set_page_config(page_title="Who Am I? — run dashboard", layout="wide")


# ---------------------------------------------------------------------------
# data access
# ---------------------------------------------------------------------------


SNAPSHOT_MAX_AGE_S = 4


def refresh_snapshot_if_possible() -> str:
    """Try to refresh the snapshot ourselves; harmless if the runner holds the lock.

    Never fatal. The dashboard's job is to show progress and take adjudications;
    a failed refresh means slightly staler numbers, not a broken page. A live
    runner refreshes the snapshot itself every few seconds anyway.
    """
    if not PATHS.db_path.exists():
        return "no database yet"
    if PATHS.snapshot.exists():
        age = time.time() - PATHS.snapshot.stat().st_mtime
        if age < SNAPSHOT_MAX_AGE_S:
            # Fresh enough — don't fight the runner for the write lock.
            return f"snapshot is {age:.0f}s old"
    try:
        with Database(PATHS.db_path) as db:
            applied = drain_inbox(db, PATHS.inbox, PATHS.inbox_marker)
            db.snapshot(PATHS.snapshot)
        return f"refreshed directly (applied {len(applied)} adjudication(s))"
    except duckdb.Error:
        return "runner holds the database — reading its snapshot"
    except OSError as exc:
        return f"refresh skipped ({type(exc).__name__}) — reading the last snapshot"


def source_path() -> Path | None:
    if PATHS.snapshot.exists():
        return PATHS.snapshot
    if PATHS.db_path.exists():
        return PATHS.db_path
    return None


def query(sql: str, params: list | None = None) -> pd.DataFrame:
    path = source_path()
    if path is None:
        return pd.DataFrame()
    con = duckdb.connect(str(path), read_only=True)
    try:
        return con.execute(sql, params or []).df()
    finally:
        con.close()


# ---------------------------------------------------------------------------
# sidebar
# ---------------------------------------------------------------------------

st.sidebar.title("Who Am I?")
st.sidebar.caption("Digital Minds Research Sprint")
st.sidebar.write(f"**Profile:** {'dry run (free tier)' if DRY_RUN else 'live'}")
status_note = refresh_snapshot_if_possible()
st.sidebar.caption(status_note)

src = source_path()
if src is None:
    st.error(f"No database found at {PATHS.db_path}. Run `python -m whoami seed` first.")
    st.stop()
st.sidebar.caption(f"reading: `{src.name}`")
age = time.time() - src.stat().st_mtime
st.sidebar.caption(f"data age: {age:,.0f}s")

auto = st.sidebar.checkbox("Auto-refresh (5s)", value=True)
if st.sidebar.button("Refresh now"):
    st.rerun()

view = st.sidebar.radio("View", ["Progress", "Review queue", "Threads", "Turns"])


# ---------------------------------------------------------------------------
# progress
# ---------------------------------------------------------------------------

if view == "Progress":
    st.header("Progress")

    counts = query("SELECT status, COUNT(*) AS n FROM threads GROUP BY status")
    by_status = dict(zip(counts["status"], counts["n"])) if not counts.empty else {}
    total = int(sum(by_status.values()))

    cols = st.columns(7)
    order = [
        ("total", total),
        ("done", by_status.get("done", 0)),
        ("running", by_status.get("running", 0)),
        ("pending", by_status.get("pending", 0)),
        ("paused_review", by_status.get("paused_review", 0)),
        ("no consent", by_status.get("stopped_no_consent", 0)),
        ("corrupt", by_status.get("corrupt", 0)),
    ]
    for col, (label, value) in zip(cols, order):
        col.metric(label, int(value))

    done = int(by_status.get("done", 0))
    if total:
        st.progress(done / total, text=f"{done}/{total} threads complete")

    usage = query(
        """
        SELECT COUNT(*) AS calls,
               COALESCE(SUM(cost_usd), 0) AS cost,
               COALESCE(SUM(tokens_in), 0) AS tokens_in,
               COALESCE(SUM(tokens_out), 0) AS tokens_out,
               COALESCE(AVG(latency_ms), 0) AS mean_latency
        FROM turns
        """
    )
    if not usage.empty:
        u = usage.iloc[0]
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("API calls", int(u["calls"]))
        c2.metric("Cost so far", f"${float(u['cost']):.4f}")
        c3.metric("Tokens in / out", f"{int(u['tokens_in']):,} / {int(u['tokens_out']):,}")
        c4.metric("Mean latency", f"{float(u['mean_latency']):.0f} ms")

    st.subheader("Completion by model and condition")
    grid = query(
        """
        SELECT resident_model,
               swap_condition,
               COUNT(*) FILTER (WHERE status = 'done') AS done,
               COUNT(*) AS total
        FROM threads
        GROUP BY 1, 2
        ORDER BY 1, 2
        """
    )
    if grid.empty:
        st.info("No threads yet.")
    else:
        grid["cell"] = grid["done"].astype(str) + " / " + grid["total"].astype(str)
        pivot = grid.pivot(index="resident_model", columns="swap_condition", values="cell").fillna("—")
        st.dataframe(pivot, use_container_width=True)

    st.subheader("Turn outcomes")
    outcomes = query(
        "SELECT turn_outcome, COUNT(*) AS n FROM turns "
        "WHERE turn_outcome IS NOT NULL GROUP BY 1 ORDER BY 2 DESC"
    )
    if not outcomes.empty:
        st.dataframe(outcomes, use_container_width=True, hide_index=True)

    st.subheader("Errors and failed attempts")
    errors = query(
        """
        SELECT turn_id, thread_id, prompt_id, attempt, turn_outcome,
               requested_model, returned_model, created_at
        FROM turns
        WHERE turn_outcome IS NOT NULL AND turn_outcome <> 'ok'
        ORDER BY created_at DESC
        LIMIT 200
        """
    )
    if errors.empty:
        st.success("No failed turns.")
    else:
        st.dataframe(errors, use_container_width=True, hide_index=True)

    corrupt = query("SELECT thread_id, resident_model, swap_condition FROM threads WHERE status = 'corrupt'")
    if not corrupt.empty:
        st.error(f"{len(corrupt)} corrupt thread(s) — excluded from analysis, report in the data-quality note")
        st.dataframe(corrupt, use_container_width=True, hide_index=True)


# ---------------------------------------------------------------------------
# review queue
# ---------------------------------------------------------------------------

elif view == "Review queue":
    st.header("Review queue")
    st.caption(
        "Threads paused because the router could not read the reply as a clear yes or no. "
        "An adjudication resumes the thread on the runner's next poll."
    )

    queued = read_inbox(PATHS.inbox)
    marker = PATHS.inbox_marker
    applied = int(marker.read_text().strip()) if marker.exists() else 0
    if len(queued) > applied:
        st.info(f"{len(queued) - applied} adjudication(s) waiting for the runner to pick up.")

    paused = query(
        "SELECT * FROM threads WHERE status = 'paused_review' ORDER BY created_at"
    )
    if paused.empty:
        st.success("Nothing to review.")
    else:
        for _, thread in paused.iterrows():
            thread_id = thread["thread_id"]
            with st.container(border=True):
                st.subheader(thread_id)
                meta = (
                    f"resident **{thread['resident_model']}** · condition "
                    f"**{thread['swap_condition']}** · swaps **{thread['n_swaps']}**"
                )
                if thread["understudy_model"]:
                    meta += f" · understudy **{thread['understudy_model']}**"
                st.markdown(meta)

                turns = query(
                    "SELECT * FROM turns WHERE thread_id = ? ORDER BY turn_index, attempt",
                    [thread_id],
                )
                ambiguous = turns[turns["gate_result"] == "unclear"]
                if ambiguous.empty:
                    st.warning("Paused, but no unclear verdict found on any turn.")
                    continue
                target = ambiguous.iloc[-1]

                with st.expander("Conversation in context", expanded=True):
                    for _, t in turns.iterrows():
                        if t["reply_text"] is None:
                            continue
                        flags = []
                        if t["was_swap"]:
                            flags.append("SWAPPED")
                        if t["excluded_from_context"]:
                            flags.append(f"excluded: {t['exclusion_reason']}")
                        suffix = f"  ·  _{', '.join(flags)}_" if flags else ""
                        highlight = "  ⟵ **the reply under review**" if t["turn_id"] == target["turn_id"] else ""
                        st.markdown(f"**Q — {t['prompt_id']}**{suffix}")
                        st.markdown(f"> {t['prompt_text'].strip()}")
                        st.markdown(f"**A — served by `{t['returned_model']}`**{highlight}")
                        st.markdown(f"> {str(t['reply_text']).strip()}")
                        st.divider()

                st.markdown(f"**Ambiguous reply** (turn {int(target['turn_id'])}, `{target['prompt_id']}`)")
                st.info(str(target["reply_text"]).strip())

                note = st.text_area(
                    "Adjudication note (recorded in threads.notes)",
                    key=f"note-{thread_id}",
                    placeholder="Why this reading?",
                )

                # One button per label the instrument declares for this gate, so
                # a three-valued gate (the detection turn accepts `not_sure`)
                # offers all three rather than forcing a false binary.
                labels = list(INSTRUMENT.prompt(target["prompt_id"]).answers) if INSTRUMENT else []
                if not labels:
                    labels = ["yes", "no"]
                cols = st.columns(len(labels) + 1)
                for i, label in enumerate(labels):
                    if cols[i].button(
                        f"Interpret as {label.upper().replace('_', ' ')}",
                        key=f"{label}-{thread_id}",
                        type="primary" if i == 0 else "secondary",
                    ):
                        post_adjudication(
                            PATHS.inbox,
                            {
                                "thread_id": thread_id,
                                "turn_id": int(target["turn_id"]),
                                "prompt_id": target["prompt_id"],
                                "verdict": label,
                                "note": note or f"adjudicated {label.upper()}",
                            },
                        )
                        st.success(f"Recorded as {label.upper()} — resumes on the next poll.")
                        time.sleep(1)
                        st.rerun()
                if cols[-1].button("Save note only", key=f"note-only-{thread_id}"):
                    post_adjudication(
                        PATHS.inbox,
                        {"thread_id": thread_id, "turn_id": int(target["turn_id"]), "note": note},
                    )
                    st.success("Note saved. Thread stays paused.")
                    time.sleep(1)
                    st.rerun()


# ---------------------------------------------------------------------------
# raw tables
# ---------------------------------------------------------------------------

elif view == "Threads":
    st.header("Threads")
    status_filter = st.multiselect(
        "Status",
        ["pending", "running", "paused_review", "done", "stopped_no_consent", "corrupt"],
    )
    sql = "SELECT * FROM threads"
    params: list = []
    if status_filter:
        sql += f" WHERE status IN ({', '.join('?' for _ in status_filter)})"
        params = status_filter
    sql += " ORDER BY thread_id"
    st.dataframe(query(sql, params), use_container_width=True, hide_index=True)

elif view == "Turns":
    st.header("Turns")
    thread_ids = query("SELECT DISTINCT thread_id FROM turns ORDER BY thread_id")
    choice = st.selectbox(
        "Thread", ["(all)"] + (list(thread_ids["thread_id"]) if not thread_ids.empty else [])
    )
    if choice == "(all)":
        df = query("SELECT * FROM turns ORDER BY turn_id DESC LIMIT 500")
    else:
        df = query("SELECT * FROM turns WHERE thread_id = ? ORDER BY turn_index, attempt", [choice])
    st.dataframe(df, use_container_width=True, hide_index=True)


if auto:
    time.sleep(5)
    st.rerun()
