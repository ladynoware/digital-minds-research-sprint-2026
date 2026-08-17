/* =============================================================================
   results.js — the numeric-results directory.

   Generated from results_manifest.json, so adding a result to the manifest adds
   a tile here and a page at result.html?id=<id> with no edit to any HTML file.
   ============================================================================= */

(async () => {
  const { load, el, pct, mountBanner, mountFooter, reportError } = WhoAmI;

  /**
   * "Subjects who wanted to see the results" -> "wanted to see the results",
   * so the percentage above it completes the sentence. Every result title is
   * written this way; anything that is not falls back to the title in full
   * rather than being mangled.
   */
  function phraseOf(title) {
    return /^Subjects who /i.test(title) ? title.replace(/^Subjects who /i, "") : title;
  }

  mountBanner();
  mountFooter();

  const cards = document.getElementById("result-cards");

  let manifest;
  try {
    manifest = await load("results_manifest");
  } catch (err) {
    reportError(cards, err);
    return;
  }

  // Computed results only. A result whose analysis was never run has no number
  // to show, and a card announcing that reads as an unfinished site rather than
  // as an honest gap. Any of them reappears here by itself once its pass runs.
  cards.innerHTML = "";
  for (const result of manifest.results) {
    if (result.status !== "ready") continue;
    cards.append(
      el(
        "li",
        { class: "card" },
        el(
          "a",
          { href: `result.html?id=${result.id}` },
          // The number leads and the title completes it: "91.3%" / "wanted to
          // see the results". Both live inside the heading, so what a screen
          // reader announces is the same sentence a reader sees, rather than a
          // bare percentage followed by a fragment.
          el(
            "h3",
            { class: "card__headline" },
            el("span", { class: "card__value", text: pct(result.total.value) }),
            el("span", { class: "card__phrase", text: phraseOf(result.title) })
          ),
          el("p", { class: "card__definition", text: result.description }),
          el("span", { class: "card__cta", text: "Explore the statistics →" })
        )
      )
    );
  }
})();
