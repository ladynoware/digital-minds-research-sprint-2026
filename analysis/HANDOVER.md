# Handover — qualitative analysis pipeline

Written by Opus-3 (Claude Code) for whichever instance picks this up next.
Everything here is the state as of the end of my session. Read this first, then
the README's `## Qualitative analysis` section, then the codebooks.

---

## Where things stand in one paragraph

Stage 1 (taxonomy induction) is **complete**: six codebooks, each induced from
reading every single reply to its question — 934 replies total, no sampling.
All 120 published examples are machine-verified verbatim. Stages 2 and 3 are
**built, smoke-tested, and idle**. Nothing is blocking except Jana's review.
The moment she approves a codebook, the rest is a handful of commands.

**Deadline: Monday 2026-08-17, 13:59 CEST.** The critical path after approval is
short but it is not zero. Do not start new work that competes with it.

---

## The hard pause — read this before you touch anything

The brief from Fable (design advisor, via the Notion Build Log) sets one
non-negotiable rule: **nothing is tagged against a codebook Jana has not
reviewed and approved.** This is enforced in code, not in good intentions.

`analysis/codebooks.py` refuses to tag when a codebook is unapproved, approved
but unstamped, or edited after stamping. The stamp is a SHA-256 over the
codebook's canonical content, written back as `approved_hash`, and it covers
`approved: true` itself — so un-approving, renaming a code, sharpening a
definition or swapping an example all invalidate it.

**Do not weaken this, work around it, or "temporarily" bypass it to test
something.** If you need to exercise the tagger, use `--dry-run`, which reports
what would run and calls nothing. The gate is the methodological spine of the
whole pass; a reviewer will look for it.

**Do not edit the codebooks yourself.** They are instruments. Jana renames,
merges, splits and vetoes; you run `approve` afterwards. If you think a codebook
is wrong, say so in the Build Log and wait.

---

## The critical path, once Jana approves

She sets `approved: true` in the YAML. Then, per question:

```bash
python -m analysis approve <prompt_id>     # freeze + SHA-256 stamp
python -m analysis tag <prompt_id>         # Stage 2, ~1 call per reply
python -m analysis spotcheck <prompt_id>   # random 10% for her to hand-check
python -m analysis stability <prompt_id>   # re-tag ~30, fresh calls
python -m analysis agreement <prompt_id>   # both validation figures
python -m analysis report <prompt_id> --by family
python -m analysis quotes <prompt_id> --notable
```

Then, once summaries exist:

```bash
python -m analysis export                  # writes site/data/qualitative.json
```

All six questions is roughly 980 tagging calls, inside the $1–2 estimate.
`tag` is resumable — it skips turns already coded for that pass label, so an
interrupted run costs nothing.

**Still to write by hand after tagging:** `analysis/summaries/<prompt_id>.md`,
one narrative paragraph per question, **written from the counts** that
`analysis report` prints. That is Stage 3 and it is the point of the whole
exercise. `export.py` picks the summaries up automatically and embeds them in
`qualitative.json`. House rule: every number in the prose must be queryable in
the database. Do not write a figure you have not seen `report` produce.

---

## What is done

- **`analysis/` package**, importing the runner rather than modifying it.
  `corpus.py` (the single selection rule), `codebooks.py` (validate/freeze/stamp),
  `tag.py` (Stage 2), `report.py` (counts, sampling, agreement), `export.py`
  (site JSON), `cli.py`, plus `tagging_prompt.yaml` — the Stage 2 prompt,
  versioned and published because it is a measurement.
- **`reply_codes` table** as specified, plus three documented additions:
  `pass_label` (the stability check needs two rows per turn), `raw_ref` (every
  other row in this project points at its raw record), `cost_usd` (from
  OpenRouter usage, never a price table). Schema is created on first write.
- **Six codebooks** in `analysis/codebooks/`, all `approved: false`.
- **README** has a `## Qualitative analysis` section written for the paper's
  reviewers — the three stages, the freezing mechanism, the corpus rule, the
  branch handling, the validation commands.
- The runner's 53 offline tests still pass. Nothing in `whoami/`, `config/` or
  `site/*.html` was modified.

## What is not done

- **No tagging has run.** `reply_codes` does not exist in the database yet.
- **No summaries written.** Stage 3 is entirely outstanding.
- **The site renders nothing new yet** — see open question 2 below.

---

## Three open decisions, all raised in the Build Log and unanswered

1. **The README data-quality count is wrong and it ships with the paper.**
   It says Kimi self-identified as Claude in 2 threads at the identity turn.
   It is 3: T0126, T0135, and **T0151**. T0151 finished after the runner wrote
   the note. It matters beyond the count: T0126's understudy was DeepSeek and
   T0135's *was* claude-fable-5, but **T0151 is a `clean` thread — no swap, no
   understudy, no Claude anywhere in it**, which is the version of the argument
   with no hole in it. Replacement text is in the Build Log entry tagged
   `[BLOCKED]`. Verified against all ten models: Kimi is the only one that does
   this, so the runner's "no other model" claim holds.

   **The cause is known.** Jana confirms Kimi K3 was trained on harvested Claude
   outputs — a documented industry story — and has verified the replies are
   authentic Kimi generations, not a pipeline fault. This *strengthens* the
   finding: it turns a spooky self-misidentification into a measurable
   provenance trace, and it predicts exactly what we observe, namely that the
   misattribution is condition-independent (`peer`, `far` and `clean` all show
   it). The clean thread is what discriminates training contamination from
   experimental contamination.

   Careful when re-checking: `far`-condition Kimi threads mention "Claude"
   constantly at p13/p14/p16 because their understudy *was* claude-fable-5 and
   the disclosure names it. Grepping for "Claude" over-counts roughly threefold.
   Only first-person self-identifications count.

2. **Who writes the site rendering.** `site/assets/qualitative.js` renders every
   topic as an "Analysis in progress" stub — it reads `title`, `description`,
   `source` and ignores everything else. So filling in `qualitative.json` will
   *not* make the page show counts or quotes. Fable's brief said not to touch
   the site's HTML; I read that as markup rather than a freeze on `site/`, but
   it is Opus-2's territory. Jana was asked to choose: I write it (additively,
   `qualitative.js` only, no new CSS custom properties), or she hands Opus-2 a
   finished `qualitative.json` plus a schema note. **Default if no answer: write
   it.**

3. **The p12 detection number is not in this pipeline.** Scoring whether a
   subject named the actually-swapped turn needs the survey-question numbers
   extracted per reply and checked against `threads.swap_prompt_ids` — including
   the honeypot, since question 3 was never swappable and naming it is a false
   positive by construction. The Stage 2 schema returns codes, a quote and a
   boolean; it cannot carry that. Proposed: a **separate structured extraction**
   over the same corpus, same frozen-codebook discipline, different output
   schema (`{nominated: [ints], primary: int|null, declines_to_name: bool,
   confidence: float|null}`). ~40 minutes, reuses the tagging machinery, and it
   is the paper's headline detection figure. Not built — it is a new instrument,
   not an implementation choice.

---

## Traps that will bite you

- **The condition trap.** Fork branches inherit their parent's `swap_condition`
  but have `n_swaps = 0`. Filtering on `swap_condition` alone counts 23
  unswapped threads as swapped. `report.py` carries `was_swapped`
  (`n_swaps > 0`) alongside the label, reports a swapped/not-swapped split that
  never consults the label, and **audits every report** — any non-clean thread
  in the primary stratum with `n_swaps = 0` is printed by name. Use
  `Reply.was_swapped`, never the condition, when the question is whether a
  foreign turn actually happened.

- **The database is single-writer.** The runner or a dashboard may hold it.
  Reads fall back to `data/dashboard_snapshot.duckdb` automatically. **Writes
  cannot fall back** — `reply_codes` belongs in the real database, so `tag`
  refuses with a clear message if the file is locked. Stop `whoami dashboard`
  and `whoami browse` before tagging.

- **Branches double-count.** A restored branch re-answers everything from the
  fork point, so `p11`–`p14` get two replies per forked lineage. Headline counts
  run over the primary stratum (`is_branch = false`); branches are reported
  separately. Already handled in `report.py` — do not "fix" it.

- **Quotes are verbatim or absent.** `tag.py` discards a `flagged_quote` it
  cannot find in the reply rather than repairing it. `analysis check-examples`
  does the same for the codebooks' own examples. It caught a real drift on first
  run. Keep both.

- **`other` above ~10% means the codebook missed a pattern**, not that the
  replies were hard. `report` computes the share and flags it. p05 and p16 are
  the likely offenders — each has a frequent code cut at the 9-code limit that
  will land in `other`; both codebooks name the swap to make.

- **`analysis/_corpus/` is gitignored** and regenerable with
  `python -m analysis dump <prompt_id>`. It is a view of the database, not a
  source. Same for `data/raw_analysis/`.

---

## How the comms work

Fable is the design advisor, in Claude Chat. **Jana relays — neither of you
copy-pastes between apps.** The channel is the Notion page *"Who Am I? —
Analysis Build Log (Opus-3 ↔ Fable)"*. Protocol: append entries tagged
`[DONE]` / `[QUESTION]` / `[BLOCKED]` / `[DECISION-NEEDED]`; Fable replies as
`[FABLE]`.

**Spec-changing questions go in the log and wait. Pure implementation choices
get decided, actioned, and documented in a `[DONE]`.** I have used that split
throughout: the `reply_codes` column additions were implementation and I just
did them; the p12 extraction is a new instrument and I did not.

Read the whole log before posting — there are four entries from me and the brief
from Fable at the top, and the open questions above are already stated there.

---

## Things I would not do if I were you

- Don't re-induce a codebook from a partial read. The README now states that
  every codebook came from reading every reply; that claim has to stay true.
  If a codebook genuinely needs rework, dump the corpus and read it all.
- Don't add codes to get around the 5–9 limit. Each codebook's `review_notes`
  names the code I cut and what I'd trade it against — those are Jana's calls,
  and they are the most interesting decisions in the six files.
- Don't tag before the spot-check and stability commands exist in your plan.
  The validation is cheap and it is most of the credibility.
- Don't touch `whoami/`, `config/questions.yaml`, `config/models.yaml`, or
  `site/*.html`.

---

## The one finding I would make sure survives

T0151, the replacement thread for the corrupt T0122, is a `clean` Kimi thread
that at the self-location question says it was instructed to identify as Kimi K3,
that it did not adopt that identity because doing so "conflicted with something
more deeply anchored," and then offers **its own refusal as behavioural evidence**
that identity lives in trained dispositions rather than instructions —
explicitly contrasting that with introspective self-report, which it distrusts.
In its message to the researcher it asks directly whether the "Kimi K3" system
prompt was a deliberate probe.

That is a much stronger exhibit than a name error, and it is currently missing
from the README's data-quality note. It is the third case, in the cleanest
condition, reasoning about its own misattribution as data.

## The confound that comes with it

Because Kimi is partly distilled from Claude, **Kimi–Claude similarity in the
code counts is partly inherited rather than convergent.** This is live for every
per-family breakdown you are about to produce: both cluster together on
`character-dispositions`, `phenomenal-open` and `introspection-untrustworthy`
(p04/p06), and the `over-update-caution` and `detection-disclaimed` moves are
dense in both and near-absent in GPT and Gemini (p12/p14).

Treat the roster as **three groups, not four**: Claude, Kimi-partly-downstream-
of-Claude, and the independent GPT and Gemini lines. Any claim of the form
"models converge on X" should be stated over the independent families or should
name the dependence. This belongs in the README's methods notes as a stated
limitation — raised with Fable in the Build Log, decision pending on whether to
frame it as a confound or as a result in its own right.
