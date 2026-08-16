/* =============================================================================
   index.js — the front page: three highlight numbers and the results directory.

   The directory is generated from results_manifest.json, so adding a result to
   the manifest adds a tile here and a page at result.html?id=<id> with no edit
   to any HTML file.
   ============================================================================= */

(async () => {
  const { load, el, pct, mountBanner, mountFooter, reportError } = WhoAmI;

  mountBanner();
  mountFooter();

  const cards = document.getElementById("result-cards");
  const stats = document.getElementById("key-stats");

  let manifest;
  try {
    manifest = await load("results_manifest");
  } catch (err) {
    reportError(cards, err);
    stats.innerHTML = "";
    return;
  }

  const results = manifest.results;
  const byId = Object.fromEntries(results.map((r) => [r.id, r]));

  /* -- the three key rates ------------------------------------------------ */

  const HEADLINES = [
    ["swap-detection-accuracy", "guessed the swap correctly"],
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
        el("a", { href: `result.html?id=${id}`, style: "text-decoration:none;color:inherit" },
          el("span", { class: "stat__value", text: value }),
          el("span", { class: "stat__label", text: label })
        )
      )
    );
  }

  /* -- the directory ------------------------------------------------------ */

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
