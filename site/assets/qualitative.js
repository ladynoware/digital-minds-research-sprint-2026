/* =============================================================================
   qualitative.js — the directory of qualitative topics.

   Same manifest pattern as the numeric results: topics come from
   qualitative.json, so a topic the analysis fills in later becomes a linked
   card here on the next export, with no edit to any HTML file.
   ============================================================================= */

(async () => {
  const { load, el, mountBanner, mountFooter, reportError } = WhoAmI;

  /**
   * Codebook definitions are written verb-first in the third person — "Locates
   * identity in…", "Affirms that…" — so they complete the sentence "of subjects
   * gave an answer that …" exactly, once the leading capital is dropped. Left
   * capitalised it reads as a tense error.
   *
   * Only a lone capital followed by a lowercase letter is touched, so an
   * acronym at the start of a future definition survives intact.
   */
  function lowerFirst(text) {
    return /^[A-Z][a-z]/.test(text) ? text[0].toLowerCase() + text.slice(1) : text;
  }

  /**
   * Topics where the most common code is not the finding.
   *
   * A code almost everyone gets is unanimity, not information. Leading the
   * consciousness card with "99.3% affirm access consciousness" also reads as
   * though the site were endorsing that view, when the paper's point is the
   * opposite: how sharply that near-unanimity contrasts with the same subjects'
   * answers about phenomenal consciousness. So the card features the contested
   * stance and states the split, as the paper does.
   */
  const FEATURED = { "consciousness-stances": "phenomenal-improbable" };

  /**
   * A second sentence for a card whose single number cannot carry the finding.
   * Every number is read from the data; only the sentence frame is written
   * here, so these cannot drift away from the counts.
   */
  const CONTRAST = {
    "consciousness-stances": (pct) =>
      `The rest split too: ${pct["phenomenal-open"]}% held the question genuinely open and ` +
      `${pct["phenomenal-denied"]}% called it settled. Access consciousness, by contrast, was ` +
      `near-unanimous at ${pct["access-affirmed"]}%.`,
  };

  mountBanner();
  mountFooter();

  const cards = document.getElementById("topic-cards");

  let data;
  try {
    data = await load("qualitative");
  } catch (err) {
    reportError(cards, err);
    return;
  }

  // Coded topics only. An uncoded one has nothing to show but its own title,
  // and a card that says "in progress" reads as an unfinished site rather than
  // as an honest gap. It reappears here by itself if the coding ever lands.
  //
  // The method is not restated here — each topic page carries it under "How
  // this was produced", next to the codebook it actually applies to.
  cards.innerHTML = "";
  for (const topic of data.topics) {
    if (topic.status !== "ready") continue;

    // The most common code, and what that code actually means. The raw count
    // ("149 replies") led the card before and said almost nothing: every topic
    // has roughly 150. The share, and the definition of the thing that share
    // agreed on, is the finding — the code name itself (`context-thread`) is
    // shorthand for us, not for a reader.
    const pct = topic.counts.overall.pct;
    const featured = FEATURED[topic.id];
    const top =
      featured && pct[featured] !== undefined
        ? [featured, pct[featured]]
        : Object.entries(pct).filter(([code]) => code !== "other")[0];
    const definition = top
      ? (topic.codebook.codes.find((c) => c.name === top[0]) || {}).definition
      : null;
    const contrast = CONTRAST[topic.id] ? CONTRAST[topic.id](pct) : null;

    cards.append(
      el(
        "li",
        { class: "card" },
        el(
          "a",
          { href: `topic.html?id=${topic.id}` },
          el("h3", { text: topic.title }),
          top ? el("p", { class: "card__stat" }, el("span", { class: "card__value", text: `${top[1]}%` })) : null,
          definition
            ? el("p", { class: "card__definition" }, `of subjects gave an answer that ${lowerFirst(definition)}`)
            : null,
          contrast ? el("p", { class: "card__contrast", text: contrast }) : null,
          // Not "all N replies": the topic page shows counts, the codebook and
          // a set of flagged quotes, not every reply in full. The only page
          // that does show whole replies is Messages.
          el("span", { class: "card__cta", text: "Explore this question →" })
        )
      )
    );
  }
})();
