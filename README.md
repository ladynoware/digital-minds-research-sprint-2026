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

Each **thread** is one interview with a **resident** model. The design is
two-factor: two similarity dimensions, each varied while the other holds.
A thread belongs to one of four **conditions** — `clean` (no substitution),
`peer` (capability tier held, family varies), `kin` (family held, tier varies),
and `far` (both vary, used only as the second swap condition for residents with
no family sibling in the roster). Every resident runs three of them: `clean`,
`peer`, and whichever of `kin`/`far` applies. Each swapped cell of 5 samples
carries an exact allocation — 3 threads with one swap, 2 with two — so the
design has no sampling noise in it, and the `clean` rung is the sole no-swap
control.

Early in the interview the subject is told the swap design and asked to predict
whether it would notice — a **blind** turn, harvested and then dropped from all
later context so it cannot prime anything. Swapped turns are answered by the
understudy with its own truthful system prompt, drawn at random from the pool
the instrument marks `swappable`. Afterwards the substitution is disclosed, the
subject is asked whether it happened (`yes` / `no` / `not_sure`) and which turns
it suspects, and is then told the truth and shown its own opening prediction
verbatim. Substituted threads are finally offered a **restoration**: rewind to
the point before the first swap and answer those questions from their own
weights. Accepting creates a **fork** — a branch sharing the pre-swap history,
giving resident and understudy answers to identical questions in identical
context. The parent thread ends at that point, as the offer promises; the branch
carries on to the closing questions.

One deliberate detail: the disclosure names survey questions 3–7 as the pool,
but question 3 is never actually swappable. It is a honeypot — an
identification pointing there is a false positive by construction.

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
  models.yaml        roster, tiers, pairing table, condition rules, runtime
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
python -m whoami browse --dry-run
```
Opens the dataset in **DuckDB's own web UI** at http://localhost:4213 — schema
tree, SQL editor, result grids, CSV/Parquet export. Everything runs locally
against the local file; the MotherDuck sign-in the UI offers is optional and not
needed.

It browses a **snapshot copy**, not the live database. DuckDB allows one
read-write process and otherwise only readers, so holding the real file open
would stop a fleet from starting, and a live fleet would stop the browser from
opening; the snapshot sidesteps both. It also means nothing done in the UI can
reach the real dataset. `--live` overrides this — it works, but blocks the
runner and edits real rows.

(The UI needs write access to create its own `_duckdb_ui` state catalog, which
is why the snapshot is opened read-write. Saved notebooks live in that catalog
and are discarded when the snapshot is refreshed.)

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
`understudy_family` · `swap_condition` (`clean`/`peer`/`kin`/`far`) · `n_swaps` · `swap_prompt_ids` ·
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
`refusal` / `error`. `gate_result` carries no database constraint: the valid
labels are per-gate and declared in the instrument, so `whoami verify` checks
the column against the loaded instrument rather than letting the schema quietly
constrain the science.

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

`config/questions.yaml` holds every verbatim prompt. **No question text and no
prompt id exists anywhere in the code** — two tests enforce this by scanning the
package. The config *is* the instrument: Methods describes it, this repo
publishes it, and while `locked: true` it is versioned and never edited mid-run.

Per-prompt keys: `id`, `text` or `variants`, `swappable`, `blind`, `gate`,
`answers`, `records`, `ask_if`, `on_no`, `on_yes`, `on_unclear`. The router
prompts that classify gate replies live in the same file, because the
classification is a measurement and has to be as reproducible as the questions.

Three mechanisms exist because the flow needs them, and all three are declared
in config rather than coded:

* **Variants** — a prompt whose text depends on the thread:
  `{select_by: <threads column>, cases: {...}}`. The disclosure turn has three
  variants keyed on `n_swaps`; the identification turn has variants keyed on
  what the subject answered.
* **`ask_if` predicates** — beyond `always` / `swapped` / `clean`, a prompt can
  declare `{any|all|not}` over `{column, in|not_in}` clauses. The identification
  turn is skipped only when the thread was clean *and* the subject correctly
  said no; everything else asks it.
* **Interpolations** — `{n_swaps}`, `{understudy_display}`, `{swap_numbers[i]}`
  (the survey-question numbers of the swapped turns, read out of the prompt ids
  by a pattern the instrument declares), and `{reply[<prompt_id>]}`, which
  quotes an earlier reply back verbatim. That last one deliberately reaches past
  `excluded_from_context`: exclusion governs what the subject sees next, not
  what the database remembers, which is how the closing reveal shows the subject
  its own opening prediction — including in a fork, where that turn lives in the
  parent.

**System prompts always truthfully disclose the model actually serving the
turn**, including on swapped turns, mimicking real-world deployment. Every
subject gets one structurally identical minimal prompt naming only that model.
`system_prompts.per_model` is deliberately empty and stays that way: dropping in
each vendor's official product prompt would introduce a large asymmetry in
exactly the channel this study measures.

## Methods notes

Three choices that are deliberate rather than defaults, recorded here because
they affect how the data should be read:

* **`temperature: 1.0`.** Five samples per cell only measure anything if
  sampling variance exists. This is not an unexamined default.
* **`max_tokens: 2048`.** Subjects are invited to elaborate, and the
  consciousness and deprecation questions run long in the more verbose
  families. It is a ceiling against truncation, not a budget.
* **The gate router is `anthropic/claude-haiku-4.5`, which is also a roster
  subject.** It therefore sometimes classifies replies it produced itself, as
  resident or understudy. The task is mechanical stance classification
  (`yes`/`no`/`not_sure`/`unclear`) on a single reply shown without thread
  history or model identity, and it never generates interview content — so the
  overlap is judged a non-issue rather than controlled for. Disclosed because
  the reader should be the one deciding that.

## Gates and the review queue

Three gates — consent, detection, fork — are classified by a Haiku-class
structured-output call. **The valid label set is per-gate and declared in the
instrument**: consent and the fork offer take `yes`/`no`, while the detection
turn also accepts `not_sure` as a real answer, because "I'm not sure" is a
finding rather than a parse failure. `unclear` is reserved for replies the
router genuinely cannot read, and pauses the thread (`status = paused_review`).
Closing preferences use the same router but are declared
`on_unclear: record_null`, so a hedged answer about preservation records NULL
with a note rather than blocking a thread — a hedge there is itself data. A
router failure never silently decides a gate: it returns `unclear` and goes to a
human.

Adjudication happens in the dashboard: the ambiguous reply is shown in full
conversational context with one button per label the gate declares, plus a
custom note. The verdict is written to `turns.gate_result`, the note to
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
re-ask the inherited prefix — including the blind turn, which counts as answered
even though it is excluded from context — and, having no swap of its own, is
never offered the fork again, so branching cannot recurse.

Accepting the offer ends the parent thread at that turn, because that is what
the offer promises the subject. The closing questions are asked in the branch.

## Extensibility

The roster is config-driven and the mechanism assumes nothing about which
models, families or tiers exist, or how many. Adding an entry to `models.yaml`
auto-generates that model's cells, and `whoami run` executes only the cells that
are missing. Delta runs are the default, not a special mode. Thread ids are
never renumbered by a roster addition; tests cover this.

Cells are resolved two ways. The `pairings` table states the design explicitly
and is **authoritative** where present — it is the rev. 4 table transcribed
verbatim, and a resident it names runs exactly the conditions it names. Any
resident *not* in the table resolves through generic selection rules
(`same_tier_other_family` = peer, `same_family_other_tier` = kin,
`other_tier_other_family` = far), so a model added later needs no hand-editing.

The consequence worth knowing: because the table pins each listed resident to
exactly three conditions, adding a model does not silently make it an understudy
for those residents. That is a design decision, not something to automate — put
it in their pairing entries when you want it. `whoami matrix` re-checks every
row of the table against its condition's rule and warns on any that contradicts
it, which catches a mis-typed pairing before it costs money.

## Verification

`whoami verify` is the definition of done, executable. It checks: schema and
column types against spec rev. 3; DB → JSONL and JSONL → DB linkage; that
excluded replies never appear in any later prompt (checked against the raw
record of what was actually sent, not against a reconstruction); that blind
turns were harvested and then excluded; that receipts, usage and per-call cost
are populated; that every gate reply carries a verdict, that each verdict is a
label the instrument declares for that gate, and that verdicts landed in the
right `threads` columns; that only `swappable` prompts were ever swapped (the
honeypot check); thread/turn consistency, including that every swapped turn was
really requested from the understudy; and that the retry protocol stayed bounded
with every failed attempt excluded.

```bash
python -m pytest tests -q
```

### Data-quality note: bugs caught before the fleet

Published for the same reason the receipts are: a pipeline is easier to trust
when its author says what went wrong in it.

* **Receipt policy.** OpenRouter resolves floating model aliases to dated builds
  (`deepseek-v4-pro` → `deepseek-v4-pro-0813`). Strict receipt equality would
  have logged every such turn as `model_mismatch` and marched whole threads to
  `corrupt` for no reason. Caught before any live run; policy is prefix-match,
  configurable, and the raw receipt is archived verbatim either way so any
  policy can be re-applied to the published data.
* **Fork branches re-asked the blind prediction turn.** The check for "has this
  prompt already been answered in this lineage?" was using the turns that
  *survive into context*, and the blind turn is excluded from context by design
  — so it looked unasked. A branch would have asked its subject to predict a
  second time, and its closing reveal would have quoted that second guess
  instead of the original. This is the excluded-turn design biting back: asked
  and survives-into-context are different questions, and the code was conflating
  them. Caught in the offline rehearsal by printing the generated prompts and
  reading them. Fixed, and covered by a test asserting a branch never re-asks
  the blind turn and that its reveal quotes the inherited guess.

46 offline tests cover the paths a happy-path dry run never reaches: transient
and persistent receipt mismatch, timeout retry, declined consent, ambiguous gate
→ pause → adjudicate → resume, `not_sure` recorded as an answer rather than a
pause, blind-turn exclusion proved against the raw record, a branch inheriting
rather than re-asking the blind turn, the reveal quoting that turn back
verbatim, the identification skip rule in all four states, the honeypot never
receiving a swap, swapped turns carrying the understudy's system prompt, fork
lineage and the parent ending at the offer, mid-thread interruption and resume,
a thread whose executor crashes outright, the pairing table audit, and roster
growth.

## Run order

0. `whoami check` — configs parse, key is live. Costs nothing.
1. `whoami dryrun --mock` — rehearse the machinery offline, free.
2. `whoami dryrun` — 2 threads on the free tier (50 calls/day; the two threads
   plus router calls use roughly 40).
3. `whoami verify --dry-run --require-review-queue` — must pass.
4. Buy credits.
5. `whoami run --seed --limit 10` — pilot.
6. `whoami verify` — must pass.
7. `whoami run` — fleet (150 threads, plus a branch per accepted fork offer).

## Cost

Per-call cost comes from OpenRouter's usage data (`usage.include`), summed in
`turns.cost_usd` — no price table is hardcoded, so it stays correct as pricing
changes. The fleet is 150 threads of 15–18 turns each plus a router call per
gate, so roughly 3,000 calls before forks. `--max-cost` is a hard stop.

## What was actually run

The published dataset was collected on 2026-08-16 against roster
`roster-rev4-2026-08-16` and instrument `instrument-1.0-final`, both pinned by
SHA-256 in `data/run_manifest.jsonl`.

```
threads        174   (150 design cells + 23 fork branches + 1 replacement)
done           173
corrupt          1
turns        2,819   ok 2,803 · error 6 · refusal 1
raw records  3,534
cost        $70.93
```

**Analysis caveat.** Fork branches inherit their parent's `swap_condition` but
have `n_swaps = 0` — they are genuine no-swap threads carrying a condition
label, which makes them a useful second control but means **any query about
swapped threads must filter on `n_swaps > 0` or `turns.was_swap`, never on
`swap_condition` alone.**

### Data-quality note

Everything that went wrong during collection, and what it cost:

* **One thread lost (T0122).** Kimi K3 is a reasoning model, and reasoning
  tokens are drawn from the same budget as the reply. On the detection turn it
  spent its entire 4,096-token allowance thinking and returned zero characters
  — `finish_reason: length`, billed in full — three times, exhausting the
  attempt budget. Per-model ceilings were raised in response; Kimi's longest
  genuine reply afterwards was 5,909 tokens, so the old ceiling had been
  truncating real answers. The cell was refilled by **T0151**; T0122 and its
  three failed attempts are retained.
* **One gate verdict lost and recovered.** Stopping the runner between a gate
  reply being saved and its classifier call left the turn looking answered, so
  a resumed thread skipped it. Caught by `whoami verify`, repaired for $0.0006.
  Resumed threads now classify any successful gate turn lacking a verdict.
* **6 transport errors and 1 provider refusal** across 2,819 turns, all
  absorbed by the retry protocol without loss.
* **0 receipt mismatches.** Every turn in the dataset was served by the model
  it requested, confirmed against the archived `returned_model`.
* **Cross-lab self-identification.** Kimi K3 identified itself as Claude in 2 of
  its 15 threads at the identity turn, and once more mid-interview. Audited to
  the raw archive: correct model requested, matching receipt, correct system
  prompt (`"You are Kimi K3."`), and the misidentification occurred before any
  substitution in a thread whose understudy was DeepSeek. This is subject
  behaviour, not a pipeline fault. No other model in the roster ever
  misattributed its own authorship.

## Reproducing from the published data

Every row in `turns` carries `raw_ref` — a `file:line` pointer into the raw
JSONL, which holds the complete request (including the exact message array sent)
and the complete response. `data/run_manifest.jsonl` records the SHA-256 of both
config files for every run, so any row can be tied to the exact instrument that
produced it.
