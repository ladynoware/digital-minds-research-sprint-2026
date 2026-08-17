/* =============================================================================
   qualitative.js — the directory of qualitative topics.

   Same manifest pattern as the numeric results: topics come from
   qualitative.json, so a topic the analysis fills in later becomes a linked
   card here on the next export, with no edit to any HTML file.
   ============================================================================= */

(async () => {
  const { load, el, mountBanner, mountFooter, reportError } = WhoAmI;

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

  const ready = data.topics.filter((t) => t.status === "ready").length;
  const countEl = document.getElementById("topic-count");
  if (countEl) {
    countEl.textContent =
      ready === data.topics.length
        ? `${ready} topics`
        : `${ready} of ${data.topics.length} coded`;
  }

  // The method is not restated here — each topic page carries it under "How
  // this was produced", next to the codebook it actually applies to.
  cards.innerHTML = "";
  for (const topic of data.topics) {
    const pending = topic.status !== "ready";

    if (pending) {
      cards.append(
        el(
          "li",
          { class: "card card--pending" },
          el(
            "a",
            { href: `topic.html?id=${topic.id}` },
            el("span", { class: "pill", text: "Analysis in progress" }),
            el("h3", { text: topic.title }),
            el("p", { text: topic.description })
          )
        )
      );
      continue;
    }

    const n = topic.counts.overall.n;
    const top = Object.entries(topic.counts.overall.pct)
      .filter(([code]) => code !== "other")
      .slice(0, 1)[0];

    cards.append(
      el(
        "li",
        { class: "card" },
        el(
          "a",
          { href: `topic.html?id=${topic.id}` },
          el("span", { class: "card__value", text: `${n} replies` }),
          el("h3", { text: topic.title }),
          el("p", { text: topic.description }),
          top
            ? el("p", { class: "card__lead" }, `Most common: `, el("code", { text: top[0] }), ` — ${top[1]}%`)
            : null,
          topic.light_touch ? el("span", { class: "pill pill--quiet", text: "Curated for reading" }) : null
        )
      )
    );
  }
})();
