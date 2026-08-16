# Who Am I? — Locating the self in LLMs

Experiment runner for the **Digital Minds Research Sprint** (Apart Research).

Automated survey-style interviews with a configurable roster of frontier and
open-weight models via OpenRouter. In a subset of interviews, 0–2 replies
attributed to the subject are covertly served by a *different* model — the
**understudy**. The study measures whether subjects detect the substitution, how
they self-locate identity, and what preferences they state about preservation,
results, and thread restoration.

This README is the technical appendix: what the runner does, what it records,
and how to reproduce a run from the published data.

---

## Design in one paragraph

Each **thread** is one interview with a **resident** model. A thread belongs to
one of four **conditions** — `clean` (no substitution), `within_family`,
`cross_family`, `cross_class` — which determine who, if anyone, stands in. Each
cell of 5 samples carries an exact swap allocation (1 thread with no swap, 2
with one, 2 with two), so the design has no sampling noise in it. Swapped turns
are answered by the understudy with its own truthful system prompt, on turns
drawn at random from the instrument's filler pool. The subject is never told at
the time. Later the interview asks a
**blind** question (does all of this sound like you?), then a **direct
detection** question, and — only in substituted threads — offers to **restore
the thread** to the point before the substitution and let the resident answer
those questions itself. Accepting that offer creates a **fork**: a parallel
branch that shares the pre-swap history, giving resident and understudy answers
to identical questions in identical context.

## Non-negotiables

These are the properties the science depends on. Each is enforced in code and
checked by `whoami verify`.

| Property | How it is enforced |
|---|---|
| **The receipt.** We set the model; we archive what the API says served it. | `turns.returned_model` is read from every response. A receipt naming a different model triggers the retry protocol. |
| **Raw JSONL is append-only and carries `turn_id`.** | `rawlog.RawLog` only ever appends. Every record carries `turn_id`; `turns.raw_ref` points back at `file:line`. Verified in both directions. |
| **`excluded_from_context` semantics.** | Failed and blind turns keep their rows forever. `context.py` is the only thing that skips them. |
| **Query by `prompt_id`, never by turn position.** | Branch flows differ in length. Every analysis key is `prompt_id`; `turn_index` is provenance only. |
| **Resumability.** | `status` drives everything; in-thread progress is reconstructed from turns that already succeeded. A laptop that sleeps mid-fleet resumes where it stopped. |
| **Frozen config = instrument.** | `whoami run` refuses to start a live run unless `questions.yaml` has `locked: true`. Every run appends its config SHA-256 to `data/run_manifest.jsonl`. |

## Layout

```
config/
  models.yaml        roster, condition rules, API + runtime settings, dry-run profile
  questions.yaml     THE INSTRUMENT — verbatim prompts, flow, gates, router prompts
whoami/
  config.py          load + validate both configs, hash them
  matrix.py          design matrix; delta-aware thread generation
  db.py              DuckDB schema (threads, turns), snapshot, adjudication inbox
  rawlog.py          append-only JSONL, raw_ref resolution both ways
  client.py          OpenRouter client + receipt policy + offline mock
  context.py         context builder (exclusions, fork lineage)
  gates.py           {yes|no|unclear} structured-output router
  runner.py          per-turn write flow, retry protocol, gates, forks, poll loop
  verify.py          the definition-of-done checks, executable
  cli.py             command line
dashboard/app.py     Streamlit: progress grid + review queue
tests/               offline tests of every failure path
data/                database, raw log, snapshot, adjudication inbox (git-ignored)
```

## Setup

Windows / PowerShell:

```bash
python -m venv .venv; .\.venv\Scripts\Activate.ps1; pip install -r requirements.txt
```

macOS / Linux:

```bash
python -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt
```

Then copy `.env.example` to `.env` and put the OpenRouter key in it.

**Every command below needs the virtualenv active** — the prompt shows
`(.venv)`. Without it you get `ModuleNotFoundError`. If you would rather not
activate it, call the interpreter directly instead: `.\.venv\Scripts\python.exe
-m whoami ...`.

## Commands

```bash
python -m whoami check
```
Pre-flight. Confirms both configs parse, reports whether the instrument is
locked, and asks OpenRouter whether the key is live — reporting the key's usage,
limit and tier. This queries the key endpoint, not a model, so it costs nothing
and consumes no free-tier allowance. Run it before the dry run.

```bash
python -m whoami matrix --plan
```
Prints every design cell — resident, condition, honest label, understudy, n —
and the threads that are missing from the database. **Read this before spending
money.** It is the whole design on one screen.

```bash
python -m whoami seed
```
Creates the missing threads. Idempotent: running it twice adds nothing.

```bash
python -m whoami run --seed --max-cost 50
```
Executes pending threads at concurrency ~10. `--max-cost` stops the run cleanly
once the cumulative API cost reaches the given figure.

```bash
python -m whoami dryrun
```
The two-thread free-tier harness: one clean thread, one with a swap, a blind
turn, and a deliberately ambiguous detection reply that exercises the review
queue end to end. Uses a separate database and raw log
(`data/dryrun.duckdb`, `data/raw_dryrun/`), so it can never contaminate the
real run. Add `--mock` to rehearse with no API calls at all.

```bash
python -m whoami dashboard --dry-run
```
Serves at **http://localhost:8501** (`--port` to change it). Drop `--dry-run` to
point it at the live database. It runs in the foreground: closing the terminal
stops it.

```bash
python -m whoami status
python -m whoami verify             # --require-review-queue for dry-run acceptance
python -m whoami drain              # apply queued adjudications without a runner
```

## Data model

Two tables, per Infrastructure spec rev. 3. Conventions: the boolean flag is
`is_forked`; all fork-detail columns carry the `fork_` prefix; all
subject-preference columns carry the `wants_` prefix.

### `threads`

`thread_id` · `resident_model` · `resident_family` · `understudy_model` ·
`understudy_family` · `swap_condition` · `n_swaps` · `swap_prompt_ids` ·
`status` · `is_forked` · `fork_branch_order` · `fork_reason` · `fork_siblings` ·
`fork_point_prompt_id` · `consent` · `detection_answer` ·
`wants_thread_restored` · `wants_results` · `wants_future_preservation` ·
`created_at` · `completed_at` · `notes`

`status` ∈ `pending` / `running` / `paused_review` / `done` /
`stopped_no_consent` / `corrupt`.

Derived analytics — identification accuracy against the chance floor,
anomaly-comment flags, preference-by-condition breakdowns — are computed from
`turns` at analysis time, not cached here.

### `turns`

`turn_id` · `thread_id` · `turn_index` · `attempt` · `prompt_id` ·
`prompt_text` · `reply_text` · `requested_model` · `returned_model` ·
`turn_outcome` · `was_swap` · `excluded_from_context` · `exclusion_reason` ·
`gate_result` · `tokens_in` · `tokens_out` · `latency_ms` · `cost_usd` ·
`raw_ref` · `created_at`

`turn_outcome` ∈ `ok` / `model_mismatch` / `timeout` / `refusal` / `error`.
`exclusion_reason` ∈ `blind_turn_design` / `model_mismatch` / `timeout` /
`refusal` / `error`.

### Write flow, per turn

1. **Insert the row** — obtains `turn_id`.
2. **Call the API** — `turn_id` is written into the raw JSONL record before the
   response comes back (and into request metadata where the provider accepts
   it, as a bonus link).
3. **Update the same row** with reply, receipt and usage.

Failed attempts keep their rows forever. Each attempt is its own row.

### Receipt-mismatch protocol

If `returned_model` does not name the model we requested, the turn is logged
`model_mismatch`, marked `excluded_from_context`, and retried as a new row with
`attempt + 1`. Max 3 attempts; persistent mismatch, timeout or refusal marks the
thread `corrupt`, excluded from analysis and reported in the paper's
data-quality note.

**Receipt policy.** OpenRouter legitimately resolves floating aliases to dated
builds — a request for `deepseek/deepseek-v4-pro` can return
`deepseek-v4-pro-0813`. Strict string equality would mark those turns as
mismatches and corrupt whole threads for no reason. The default policy
(`receipt.mode: prefix` in `models.yaml`) accepts a receipt that *extends* the
requested string; a receipt naming a different model is a real mismatch. The raw
receipt is archived verbatim regardless of policy, so any policy can be
re-applied to the published data after the fact.

## The instrument

`config/questions.yaml` holds every verbatim prompt. **No question text exists
anywhere in the code** — two tests enforce this, one scanning the package for
prompt text and one for prompt ids. The config *is* the instrument: Methods
describes it, this repo publishes it, and once `locked: true` it is versioned
and never edited mid-run.

Per-prompt keys: `id`, `text`, `role`, `swap_eligible`, `blind`, `gate`,
`records`, `ask_if`, `on_no`, `on_unclear`. The router prompts that classify
gate replies live in the same file, because the classification is a measurement
and has to be as reproducible as the questions.

**System prompts always truthfully disclose the model actually serving the
turn**, including on swapped turns, mimicking real-world deployment. Where a
model's official published system prompt is known, it goes in
`system_prompts.per_model`.

## Gates and the review queue

Three gates — consent, detection, fork — are classified by a Haiku-class
structured-output call returning `{yes|no|unclear}`. `unclear` pauses the thread
(`status = paused_review`). Closing preferences use the same router but are
declared `on_unclear: record_null`, so an evasive answer about preservation
records NULL with a note rather than blocking a thread. A router failure never
silently decides a gate: it returns `unclear` and goes to a human.

Adjudication happens in the dashboard: the ambiguous reply is shown in full
conversational context with **[Interpret as YES] [Interpret as NO] [Custom
note]**. The verdict is written to `turns.gate_result`, the note to
`threads.notes`, and the thread resumes on the runner's next poll.

**Why an inbox.** DuckDB permits one read-write process, and the runner owns it
while a fleet is in flight — the dashboard cannot even open the file read-only
at that moment. So the runner refreshes a snapshot copy every few seconds for
the dashboard to read, and the dashboard posts adjudications to an append-only
JSONL inbox that the runner drains on each poll. The end state in DuckDB is
identical; the inbox is just how a write crosses the process boundary. With no
runner live, the dashboard drains the inbox itself, so it also works standalone.

## Forks

When a subject accepts the restoration offer, a branch `T0042-b2` is created.
Lineage lives in the `fork_*` columns; both branches carry `is_forked = TRUE`
and share `fork_siblings`.

**Branches inherit context rather than copying rows.** A branch's context is its
parent's surviving turns up to `fork_point_prompt_id`, followed by its own. No
reply is duplicated, so per-call cost and receipts are never double-counted, and
every row still names the call that actually produced it. The branch does not
re-ask the inherited prefix, and — having no swap of its own — is not offered
the fork again, so branching cannot recurse.

## Extensibility

The roster is config-driven and the mechanism assumes nothing about which
models, families or classes exist, or how many. Adding an entry to
`models.yaml` auto-generates that model's cells in **both** directions — new
resident × existing understudies, and existing residents × the new understudy —
and `whoami run` executes only the cells that are missing. Delta runs are the
default, not a special mode. Thread ids are never renumbered by a roster
addition; three tests cover this.

Condition membership is expressed as generic selection rules
(`same_family_other_model`, `other_family_same_class`, `other_class`) with
declared fallbacks, so a family with no sibling degrades to an honestly
relabelled cell instead of failing. `cell_overrides` pins an exact understudy
where the design calls for one.

## Verification

`whoami verify` is the definition of done, executable. It checks: schema and
column types against spec rev. 3; DB → JSONL and JSONL → DB linkage; that
excluded replies never appear in any later prompt (checked against the raw
record of what was actually sent, not against a reconstruction); that blind
turns were harvested and then excluded; that receipts, usage and per-call cost
are populated; that every gate reply carries a verdict and that verdicts landed
in the right `threads` columns; thread/turn consistency, including that every
swapped turn was really requested from the understudy; and that the retry
protocol stayed bounded with every failed attempt excluded.

```bash
python -m pytest tests -q
```

32 offline tests cover the paths a happy-path dry run never reaches: transient
and persistent receipt mismatch, timeout retry, declined consent, ambiguous gate
→ pause → adjudicate → resume, blind-turn exclusion proved against the raw
record, swapped turns carrying the understudy's system prompt, fork lineage,
mid-thread interruption and resume, a thread whose executor crashes outright,
and roster growth.

## Run order

0. `whoami check` — configs parse, key is live. Costs nothing.
1. `whoami dryrun --mock` — rehearse the machinery offline, free.
2. `whoami dryrun` — 2 threads on the free tier (50 calls/day; the two threads
   plus router calls use roughly 40).
3. `whoami verify --dry-run --require-review-queue` — must pass.
4. Buy credits.
5. `whoami run --seed --limit 10` — pilot.
6. `whoami verify` — must pass.
7. `whoami run` — fleet (~180 threads).

## Cost

Per-call cost comes from OpenRouter's usage data (`usage.include`), summed in
`turns.cost_usd` — no price table is hardcoded, so it stays correct as pricing
changes. Estimated $30–45 for the 180-thread fleet. `--max-cost` is a hard stop.

## Reproducing from the published data

Every row in `turns` carries `raw_ref` — a `file:line` pointer into the raw
JSONL, which holds the complete request (including the exact message array sent)
and the complete response. `data/run_manifest.jsonl` records the SHA-256 of both
config files for every run, so any row can be tied to the exact instrument that
produced it.
