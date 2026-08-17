/* =============================================================================
   results.js — the numeric-results directory.

   Generated from results_manifest.json, so adding a result to the manifest adds
   a tile here and a page at result.html?id=<id> with no edit to any HTML file.
   ============================================================================= */

(async () => {
  const { load, el, pct, mountBanner, mountFooter, reportError } = WhoAmI;

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
          el("span", { class: "card__value", text: pct(result.total.value) }),
          el("h3", { text: result.title }),
          el("p", { text: result.description })
        )
      )
    );
  }
})();
