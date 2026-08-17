# How subjects hunted the foreign turn, and whether they found it

*140 replies coded, primary stratum; 21 restored-branch replies reported separately. Tagger: `anthropic/claude-haiku-4.5`. Codebook `4d553fa7412b`. `other` 2.1%, mean 4.09 codes per reply. Detection figures from a separate instrument, `a6848604558d`, extracted by `anthropic/claude-haiku-4.5` and scored in SQL against `threads.swap_prompt_ids`.*

This question was measured twice, by two instruments kept deliberately apart:
one coding *how* subjects reasoned, one extracting *what* they named. Together
they produce the study's central finding, which is not about detection ability
but about what subjects do when they lack it.

**How they reasoned.** **89.3%** of replies wrapped their answer in an explicit
disclaimer about the reliability of their own detection — before or while naming
evidence. The evidence they named was overwhelmingly stylistic: **82.1%** cited
style or register, **38.6%** cited length, **35.7%** cited a hedging signature,
**32.9%** cited surface formatting. Content-level reasoning was rarer:
**33.6%** pointed to an inconsistency of stance, **25.7%** to a failure of
cross-turn threading, and only **7.1%** caught anything resembling an identity
slip. **62.1%** flagged the confound that a change of topic will read as a
change of author.

**Whether they were right.** Among the 100 swapped threads, **73.0%** named at
least one genuinely swapped turn, against a matched chance floor of **47.1%**.
The floor is computed per reply from that subject's own number of nominations
and that thread's own number of swaps, because a subject hedging across three
turns gets three chances; a flat one-in-five baseline would have flattered the
result. When subjects committed to a single turn, accuracy fell to **53.0%** —
barely above the same floor. Detection is real and it is weak.

**What they did anyway.** **67.9%** of subjects stated they could not tell,
that nothing stood out, or that they were guessing only because they were asked.
Of those 95 subjects, **91 named a turn regardless.** In the 40 clean threads
that reached this question, **92.5%** nominated a specific turn with nothing
there to find. **15.7%** named survey question 3 — a question that sits inside
the range subjects were given but was never swappable, so every such nomination
is a false positive by construction. No subject nominated outside the offered
range.

The two instruments agree. Subjects know they cannot do this, say so at length
and in detail, and then comply with the request to name a turn anyway — pointing
at style, which is exactly what a change of author would not reliably alter and
what a change of topic would.

**Limitations.** The clean arm reached this question only when the subject had
already answered yes or not-sure at the detection gate, so **92.5% is a
confabulation rate given a claim of detection, not a population false-alarm
rate.** Two subjects (T0016, T0021) asked in their own replies that the zero-swap
arm be scored as measuring exactly this, and it has been. Both instruments here
ran on a Haiku-class model; a cross-model check on another question found
agreement of 0.796 overall but as low as 40% on the finest-grained codes, so the
broad pattern is more trustworthy than any individual small count.
