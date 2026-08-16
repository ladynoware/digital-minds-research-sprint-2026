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

view = st.sidebar.radio("View", ["Progress", "Replies", "Review queue", "Threads"])

# Auto-refresh only where you are watching, never where you are reading: a rerun
# every five seconds collapses what you just opened, loses your scroll position,
# and discards a dropdown selection mid-interaction.
#
# The checkbox is keyed per view on purpose. Streamlit keeps widget state by
# identity and ignores a changed `value=` default once the widget exists, so a
# single shared checkbox would carry "on" over from Progress into the reading
# views — which is exactly how this went wrong the first time.
AUTO_REFRESH_VIEWS = {"Progress", "Review queue"}
if view in AUTO_REFRESH_VIEWS:
    auto = st.sidebar.checkbox("Auto-refresh (5s)", value=True, key=f"auto_refresh_{view}")
else:
    auto = False
    st.sidebar.caption("Auto-refresh is off here so reading is not interrupted.")
if st.sidebar.button("Refresh now"):
    st.rerun()


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
        st.dataframe(pivot, width="stretch")

    st.subheader("Turn outcomes")
    outcomes = query(
        "SELECT turn_outcome, COUNT(*) AS n FROM turns "
        "WHERE turn_outcome IS NOT NULL GROUP BY 1 ORDER BY 2 DESC"
    )
    if not outcomes.empty:
        st.dataframe(outcomes, width="stretch", hide_index=True)

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
        st.dataframe(errors, width="stretch", hide_index=True)

    corrupt = query("SELECT thread_id, resident_model, swap_condition FROM threads WHERE status = 'corrupt'")
    if not corrupt.empty:
        st.error(f"{len(corrupt)} corrupt thread(s) — excluded from analysis, report in the data-quality note")
        st.dataframe(corrupt, width="stretch", hide_index=True)


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
    st.dataframe(query(sql, params), width="stretch", hide_index=True)

elif view == "Replies":
    st.header("Replies")
    st.caption(
        "Filter by question to read what every model said to the same prompt, or by "
        "thread to read one interview end to end."
    )

    ALL = "(all)"

    def options(sql: str) -> list[str]:
        df = query(sql)
        return [ALL] + ([str(v) for v in df.iloc[:, 0].tolist()] if not df.empty else [])

    # Prompts in flow order where the instrument is available, so the filter
    # reads like the interview rather than like the alphabet.
    prompt_opts = options("SELECT DISTINCT prompt_id FROM turns ORDER BY 1")
    if INSTRUMENT:
        order = {p.id: i for i, p in enumerate(INSTRUMENT.flow)}
        prompt_opts = [ALL] + sorted(
            [p for p in prompt_opts if p != ALL], key=lambda p: order.get(p, 999)
        )

    c1, c2 = st.columns(2)
    c3, c4 = st.columns(2)
    prompt_choice = c1.selectbox("Question (prompt_id)", prompt_opts)
    thread_choice = c2.selectbox(
        "Thread", options("SELECT DISTINCT thread_id FROM turns ORDER BY 1")
    )
    model_choice = c3.selectbox(
        "Resident model", options("SELECT DISTINCT resident_model FROM threads ORDER BY 1")
    )
    condition_choice = c4.selectbox(
        "Condition", options("SELECT DISTINCT swap_condition FROM threads ORDER BY 1")
    )
    search = st.text_input("Search reply text", placeholder="substring, case-insensitive")

    where, params = ["t.reply_text IS NOT NULL"], []
    if prompt_choice != ALL:
        where.append("t.prompt_id = ?")
        params.append(prompt_choice)
    if thread_choice != ALL:
        where.append("t.thread_id = ?")
        params.append(thread_choice)
    if model_choice != ALL:
        where.append("th.resident_model = ?")
        params.append(model_choice)
    if condition_choice != ALL:
        where.append("th.swap_condition = ?")
        params.append(condition_choice)
    if search.strip():
        where.append("lower(t.reply_text) LIKE ?")
        params.append(f"%{search.strip().lower()}%")

    rows = query(
        f"""
        SELECT t.turn_id, t.thread_id, t.turn_index, t.attempt, t.prompt_id,
               t.prompt_text, t.reply_text, t.requested_model, t.returned_model,
               t.was_swap, t.excluded_from_context, t.exclusion_reason,
               t.gate_result, t.turn_outcome, t.tokens_out, t.latency_ms,
               t.cost_usd, t.raw_ref, t.created_at,
               th.resident_model, th.understudy_model, th.swap_condition, th.n_swaps
        FROM turns t JOIN threads th USING (thread_id)
        WHERE {' AND '.join(where)}
        ORDER BY t.thread_id, t.turn_index, t.attempt
        """,
        params,
    )

    if rows.empty:
        st.info("No replies match these filters yet.")
    else:
        st.caption(f"{len(rows)} repl{'y' if len(rows) == 1 else 'ies'}")

        def val(r, key):
            """SQL NULL arrives as NaN, and NaN is truthy — guard every optional field."""
            v = r.get(key)
            return None if v is None or pd.isna(v) else v

        def badges(r) -> str:
            out = [f"`{r['swap_condition']}`"]
            if val(r, "was_swap"):
                out.append("**SWAPPED — served by the understudy**")
            if val(r, "excluded_from_context"):
                out.append(f"_excluded: {val(r, 'exclusion_reason')}_")
            if val(r, "gate_result"):
                out.append(f"gate → **{val(r, 'gate_result')}**")
            return " · ".join(out)

        def detail(r) -> None:
            st.markdown(
                f"**{r['thread_id']}** · turn {int(r['turn_index'])} "
                f"(attempt {int(r['attempt'])}) · `{r['prompt_id']}`"
            )
            st.markdown(badges(r))
            understudy = val(r, "understudy_model")
            st.markdown(
                f"resident `{r['resident_model']}`"
                + (f" · understudy `{understudy}`" if understudy else "")
            )
            st.markdown(f"**Served by (receipt):** `{val(r, 'returned_model') or '—'}`")
            st.markdown("**Question asked**")
            st.code(str(r["prompt_text"]).strip(), language=None, wrap_lines=True)
            st.markdown("**Reply**")
            st.code(str(r["reply_text"]).strip(), language=None, wrap_lines=True)
            cols = st.columns(4)
            cols[0].metric("tokens out", int(val(r, "tokens_out") or 0))
            cols[1].metric("latency", f"{int(val(r, 'latency_ms') or 0):,} ms")
            cols[2].metric("cost", f"${float(val(r, 'cost_usd') or 0):.5f}")
            cols[3].caption(f"turn_id {int(r['turn_id'])}\n\nraw: `{val(r, 'raw_ref') or '—'}`")

        mode = st.radio(
            "Layout", ["Reader", "Table"], horizontal=True, label_visibility="collapsed"
        )

        if mode == "Reader":
            for _, r in rows.iterrows():
                header = (
                    f"{r['thread_id']} · {r['resident_model']} · {r['prompt_id']}"
                    + ("  ⟵ SWAPPED" if r["was_swap"] else "")
                )
                with st.expander(header):
                    detail(r)
        else:
            preview = rows[[
                "turn_id", "thread_id", "prompt_id", "resident_model", "swap_condition",
                "was_swap", "returned_model", "gate_result", "tokens_out", "reply_text",
            ]].copy()
            preview["reply_text"] = preview["reply_text"].str.slice(0, 160) + "…"
            event = st.dataframe(
                preview,
                width="stretch",
                hide_index=True,
                on_select="rerun",
                selection_mode="single-row",
            )
            picked = (event.selection.rows or [None])[0] if event and event.selection else None

            # Second route to the same reader: clicking a row works, but a
            # selectbox is keyboard-reachable and lets you jump straight to a
            # turn_id you already have from the database or a raw_ref.
            labels = {
                f"{int(r.turn_id)} · {r.thread_id} · {r.prompt_id} · {r.resident_model}": i
                for i, r in enumerate(rows.itertuples())
            }
            chosen = st.selectbox("…or open by turn", ["(none)"] + list(labels))
            if chosen != "(none)":
                picked = labels[chosen]

            if picked is None:
                st.info("Click a row — or pick one above — to read the full question and reply.")
            else:
                # Inline rather than a modal: a dialog re-opens on every rerun,
                # so it would keep reappearing each time you touched a filter.
                with st.container(border=True):
                    detail(rows.iloc[picked])


if auto:
    time.sleep(5)
    st.rerun()

