/* =============================================================================
   result.js — the one numeric-result template.

   Renders any entry of results_manifest.json chosen by ?id=. Handles both
   states an entry can be in: `ready` (charts and a table) and `pending` (a
   holding card). Nothing here knows what any particular result *means*.
   ============================================================================= */

(async () => {
  const { load, el, pct, count, familyClass, mountBanner, mountFooter, reportError } = WhoAmI;

  mountBanner();
  mountFooter();

  const root = document.getElementById("result-root");
  const id = new URLSearchParams(window.location.search).get("id");

  let manifest;
  try {
    manifest = await load("results_manifest");
  } catch (err) {
    reportError(root, err);
    return;
  }

  const index = manifest.results.findIndex((r) => r.id === id);
  const result = manifest.results[index];

  if (!result) {
    root.innerHTML = "";
    root.append(
      el("div", { class: "result-head" }, el("h1", { text: "No such result" })),
      el(
        "div",
        { class: "error" },
        el("p", {
          text: id
            ? `The manifest has no result with the id "${id}".`
            : "This page needs a result id, for example result.html?id=consent-rate",
        })
      ),
      el("p", {}, el("a", { href: "results.html", text: "← All results" }))
    );
    return;
  }

  document.title = `${result.title} — Who Am I?`;
  root.innerHTML = "";

  /* -- heading ------------------------------------------------------------ */

  const head = el(
    "div",
    { class: "result-head" },
    el("p", { class: "eyebrow", text: "Quantitative result" }),
    el("h1", { class: "measure", text: result.title }),
    el("p", { class: "measure", text: result.description })
  );

  if (result.status === "ready") {
    head.append(
      el(
        "div",
        { class: "headline" },
        el("span", { class: "headline__value", text: pct(result.total.value) }),
        el("span", {
          class: "headline__detail",
          text: `of all subjects · ${count(result.total)}`,
        })
      )
    );
  }
  root.append(head);

  /* -- pending ------------------------------------------------------------ */

  if (result.status !== "ready") {
    root.append(
      el(
        "section",
        {},
        el(
          "div",
          { class: "pending" },
          el("h3", { text: "Analysis in progress" }),
          el("p", { text: result.note || "This result is not ready yet." })
        ),
        el(
          "p",
          { class: "provenance" },
          "Will be computed from: ",
          el("code", { text: result.source })
        )
      ),
      pager(manifest.results, index)
    );
    return;
  }

  /* -- the 10+1 chart ----------------------------------------------------- */

  root.append(
    el(
      "section",
      {},
      el("div", { class: "section-head" }, el("h2", { text: "By model" })),
      el("div", { class: "chart" }, Chart.fromResult(result)),
      el("p", {
        class: "chart-note",
        text:
          "Grouped by the thread's resident model — the subject being interviewed — including " +
          "threads in which some turns were served by an understudy.",
      })
    )
  );

  /* -- other readings of the same data ------------------------------------ */

  if (result.context && result.context.length) {
    root.append(
      el(
        "section",
        {},
        el("div", { class: "section-head" }, el("h2", { text: "Read against" })),
        el(
          "dl",
          { class: "context-list" },
          result.context.map((item) =>
            el(
              "div",
              { class: "context-item" },
              el(
                "dt",
                {},
                el("span", { class: "context-item__value", text: item.value }),
                el("span", { class: "context-item__label", text: item.label })
              ),
              el("dd", { text: item.note })
            )
          )
        )
      )
    );
  }

  /* -- breakdowns --------------------------------------------------------- */

  for (const breakdown of result.breakdowns || []) {
    root.append(
      el(
        "section",
        {},
        el("div", { class: "section-head" }, el("h2", { text: breakdown.label })),
        el("div", { class: "chart" }, Chart.fromBreakdown(breakdown, result)),
        breakdown.note ? el("p", { class: "chart-note", text: breakdown.note }) : null
      )
    );
  }

  /* -- the numbers, as numbers -------------------------------------------- */

  const rows = result.by_model.map((m) =>
    el(
      "tr",
      {},
      el(
        "td",
        {},
        el("span", { class: `swatch ${familyClass("swatch", m.family)}` }),
        m.display_name
      ),
      el("td", { text: m.family }),
      el("td", { text: m.tier }),
      el("td", { text: pct(m.value) }),
      el("td", { text: m.denominator === 0 ? "—" : `${m.numerator} / ${m.denominator}` })
    )
  );

  root.append(
    el(
      "section",
      {},
      el("div", { class: "section-head" }, el("h2", { text: "The numbers" })),
      el(
        "table",
        {},
        el("caption", { text: "Rate per model, with the counts behind it." }),
        el(
          "thead",
          {},
          el(
            "tr",
            {},
            el("th", { scope: "col", text: "Model" }),
            el("th", { scope: "col", text: "Family" }),
            el("th", { scope: "col", text: "Tier" }),
            el("th", { scope: "col", text: "Rate" }),
            el("th", { scope: "col", text: "Count" })
          )
        ),
        el("tbody", {}, rows),
        el(
          "tfoot",
          {},
          el(
            "tr",
            {},
            el("td", { text: "All subjects" }),
            el("td", {}),
            el("td", {}),
            el("td", { text: pct(result.total.value) }),
            el("td", { text: count(result.total) })
          )
        )
      ),
      el("p", { class: "provenance" }, "Source: ", el("code", { text: result.source }))
    ),
    pager(manifest.results, index)
  );

  /* -- prev / next -------------------------------------------------------- */

  // Steps between the results the directory actually lists, so prev/next cannot
  // walk into one that is hidden there.
  function pager(results, i) {
    const listed = results.filter((r) => r.status === "ready");
    const here = listed.findIndex((r) => r.id === results[i].id);
    const prev = here > 0 ? listed[here - 1] : null;
    const next = here >= 0 ? listed[here + 1] : null;
    return el(
      "nav",
      { class: "pager", "aria-label": "Other results" },
      prev
        ? el("a", { href: `result.html?id=${prev.id}`, text: `← ${prev.title}` })
        : el("a", { href: "results.html", text: "← All results" }),
      next ? el("a", { href: `result.html?id=${next.id}`, text: `${next.title} →` }) : el("span", {})
    );
  }
})();
