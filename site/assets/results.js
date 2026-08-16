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

  const results = manifest.results;
  const ready = results.filter((r) => r.status === "ready").length;
  document.getElementById("results-count").textContent =
    `${ready} of ${results.length} ready · the rest are being coded`;

  cards.innerHTML = "";
  for (const result of results) {
    const pending = result.status !== "ready";
    cards.append(
      el(
        "li",
        { class: `card${pending ? " card--pending" : ""}` },
        el(
          "a",
          { href: `result.html?id=${result.id}` },
          pending
            ? el("span", { class: "pill", text: "Analysis in progress" })
            : el("span", { class: "card__value", text: pct(result.total.value) }),
          el("h3", { text: result.title }),
          el("p", { text: result.description })
        )
      )
    );
  }
})();
