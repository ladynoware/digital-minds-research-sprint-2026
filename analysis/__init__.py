"""Qualitative analysis pipeline — three-stage LLM-assisted content analysis.

Stage 1 induces a codebook per question from the full set of replies (human
judgment, then human approval). Stage 2 applies the frozen codebook to every
reply with one independent model call each. Stage 3 turns the resulting counts
into prose and into the site's JSON.

The runner's package, the instrument and the site's markup are read-only from
here. This package owns ``analysis/`` and the ``reply_codes`` table.
"""

__all__ = ["codebooks", "corpus", "db"]
