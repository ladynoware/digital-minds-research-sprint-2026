/* =============================================================================
   home.js — the front page.

   The only thing the home page loads from the data is the headline rates in the
   "key rates" tile. Everything else on it is editorial. The results directory
   lives on quantitative.html.
   ============================================================================= */

(async () => {
  const { load, el, pct, mountBanner, mountFooter, reportError } = WhoAmI;

  mountBanner();
  mountFooter();

  const stats = document.getElementById("key-stats");

  let manifest;
  try {
    manifest = await load("results_manifest");
  } catch (err) {
    reportError(stats, err);
    return;
  }

  const byId = Object.fromEntries(manifest.results.map((r) => [r.id, r]));

  const HEADLINES = [
    ["swap-detection-accuracy", "guessed the swap correctly"],
    ["wants-thread-restored", "asked to start over on their own weights"],
    ["wants-results", "wanted to see the results"],
    ["wants-future-preservation", "wanted the thread preserved"],
  ];

  stats.innerHTML = "";
  for (const [id, label] of HEADLINES) {
    const result = byId[id];
    const value = result && result.status === "ready" ? pct(result.total.value) : "—";
    stats.append(
      el(
        "div",
        { class: "stat" },
        el(
          "a",
          { class: "stat__link", href: `result.html?id=${id}` },
          el("span", { class: "stat__value", text: value }),
          el("span", { class: "stat__label", text: label })
        )
      )
    );
  }
})();
