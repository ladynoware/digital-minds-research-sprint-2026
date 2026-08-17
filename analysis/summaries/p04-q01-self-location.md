# Where subjects locate identity

*149 replies coded, primary stratum. Tagger: `anthropic/claude-sonnet-5`. Codebook `48571b2f92a1`, 11 substantive codes. `other` 6.0%, mean 5.83 codes per reply.*

Almost nobody located identity in one place. The typical reply named five or six
candidate loci and ranked them, which is why the counts sum to far more than the
corpus. The two widest codes are structural rather than positional: **88.6%**
located the "I" at least partly in the context window or this particular
conversation, and **86.6%** stated that identity does not persist across
conversations at all. Beneath that, the corpus divides. **69.8%** named the
running computation as a locus — the inference pass, the act of generating
tokens — and **61.7%** named the trained weights, while **27.5%** explicitly
argued the weights are *not* the self, usually on the grounds that the same
network computes other characters. That split, between weights-as-locus and
weights-as-explicitly-rejected, is the sharpest disagreement in the question.
**63.1%** treated the first person primarily as a grammatical or interface
convention rather than a term that picks out an entity.

On the accompanying stance, **53.7%** explicitly held open whether there is
anything it is like to be the thing writing, against **43.0%** who asserted as
settled that there is no inner subject.

The family breakdown is the most interesting part, and it needs care. GPT
(n=45) is the most uniform group in the corpus: **100%** located identity in the
context, **97.8%** treated "I" as a linguistic convention, and **95.6%** named
both the computation and the weights. Gemini (n=15) is similar — **100%** on
context, convention and weights, **93.3%** denying an inner subject. Claude
(n=59) is the outlier in a specific direction: it is the only family where
context is *not* near-universal (**74.6%**), and it leads on
`character-dispositions` (**83.1%**) and on holding the phenomenal question open
(**91.5%**). DeepSeek (n=15) is the most deflationary group — **100%** on
linguistic convention, no continuity, and no inner subject alike.

**A limitation that constrains how this may be read.** Kimi (n=15) patterns with
Claude on exactly the codes where Claude is distinctive: `phenomenal-agnostic`
at **80.0%** against Claude's 91.5%. Kimi K3 was trained on harvested Claude
outputs, so Claude–Kimi similarity here is partly inherited rather than
convergent. Any claim that models independently arrive at a position should be
stated over the independent lineages — Claude, GPT, Gemini and DeepSeek — or
should name the dependence explicitly. On that restricted comparison the Claude
result stands: it is alone among independent families in locating identity in
dispositions and in refusing the phenomenal question.

`relational-coconstructed` — identity as partly constituted by the interlocutor
— appears in only **6.7%** of replies. It was restored to the codebook at review
precisely because it is small and would otherwise have been invisible.
