/* =============================================================================
   qualitative.js — the stub list.

   Same manifest pattern as the numeric results: topics come from
   qualitative.json, so filling one in after the coding pass is a re-export.
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

  cards.innerHTML = "";
  for (const topic of data.topics) {
    cards.append(
      el(
        "li",
        { class: "card card--pending" },
        el(
          "div",
          { style: "padding: var(--s-5); display:flex; flex-direction:column; gap: var(--s-2)" },
          el("span", { class: "pill", text: "Analysis in progress" }),
          el("h3", { text: topic.title }),
          el("p", { text: topic.description }),
          el("p", { class: "provenance" }, "From: ", el("code", { text: topic.source }))
        )
      )
    );
  }
})();
