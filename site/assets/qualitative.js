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

  // Coded topics only. An uncoded one has nothing to show but its own title,
  // and a card that says "in progress" reads as an unfinished site rather than
  // as an honest gap. It reappears here by itself if the coding ever lands.
  //
  // The method is not restated here — each topic page carries it under "How
  // this was produced", next to the codebook it actually applies to.
  cards.innerHTML = "";
  for (const topic of data.topics) {
    if (topic.status !== "ready") continue;

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
