/* =============================================================================
   topic.js — the one qualitative-result template.

   Renders any entry of qualitative.json chosen by ?id=. A qualitative result is
   a count plus the instrument that produced it, so this page always shows both:
   the code frequencies, and the frozen codebook with its hash and definitions.
   A percentage here means "this many replies were assigned this code by this
   codebook", and the page has to be readable as that rather than as a fact
   about models in general.
   ============================================================================= */

(async () => {
  const { load, el, pct, familyClass, mountBanner, mountFooter, reportError } = WhoAmI;

  mountBanner();
  mountFooter();

  const root = document.getElementById("topic-root");
  const id = new URLSearchParams(window.location.search).get("id");

  let data;
  let meta;
  try {
    [data, meta] = await Promise.all([load("qualitative"), load("meta")]);
  } catch (err) {
    reportError(root, err);
    return;
  }

  const index = data.topics.findIndex((t) => t.id === id);
  const topic = data.topics[index];

  if (!topic) {
    root.innerHTML = "";
    root.append(
      el("div", { class: "result-head" }, el("h1", { text: "No such topic" })),
      el(
        "div",
        { class: "error" },
        el("p", {
          text: id
            ? `qualitative.json has no topic with the id "${id}".`
            : "This page needs a topic id, for example topic.html?id=identity-location",
        })
      ),
      el("p", {}, el("a", { href: "qualitative.html", text: "← All qualitative results" }))
    );
    return;
  }

  document.title = `${topic.title} — Who Am I?`;
  root.innerHTML = "";

  const familyName = (key) =>
    ((meta.families || []).find((f) => f.key === key) || {}).display_name || key;

  /* -- heading ------------------------------------------------------------ */

  const head = el(
    "div",
    { class: "result-head" },
    el("p", { class: "eyebrow", text: "Qualitative result" }),
    el("h1", { class: "measure", text: topic.title }),
    el("p", { class: "measure", text: topic.description })
  );
  root.append(head);

  if (topic.status !== "ready") {
    root.append(
      el(
        "section",
        {},
        el(
          "div",
          { class: "pending" },
          el("h3", { text: "Analysis in progress" }),
          el("p", { text: topic.note || "This topic is not coded yet." })
        ),
        el("p", { class: "provenance" }, "Will be coded from: ", el("code", { text: topic.source }))
      ),
      pager(data.topics, index)
    );
    return;
  }

  const counts = topic.counts;
  const overall = counts.overall;

  head.append(
    el(
      "div",
      { class: "headline" },
      el("span", { class: "headline__value", text: String(overall.n) }),
      el("span", {
        class: "headline__detail",
        text: `replies coded · ${topic.codebook.codes.length} codes · ${topic.multi_label_mean} codes per reply on average`,
      })
    )
  );

  // Exhibits, not statistics. Saying so up front is the difference between a
  // reader treating these counts as a measurement and treating them as a guide
  // to reading the replies.
  if (topic.light_touch) {
    head.append(
      el("p", {
        class: "notice-inline",
        text:
          "Tagged lightly and curated for reading rather than measured. Treat these counts as a " +
          "way into the replies, not as a statistic.",
      })
    );
  }

  /* -- the summary -------------------------------------------------------- */

  if (topic.summary) {
    root.append(
      el(
        "section",
        {},
        el("div", { class: "section-head" }, el("h2", { text: "What the replies said" })),
        el(
          "div",
          { class: "prose measure" },
          Markdown.render(Markdown.stripLeadingHeading(topic.summary), { headings: true })
        )
      )
    );
  }

  /* -- code frequencies, with a family selector --------------------------- */

  const scopes = [{ key: "__all", label: "All replies", tally: overall }];
  for (const [key, tally] of Object.entries(counts.by_family || {})) {
    scopes.push({ key, label: familyName(key), tally, family: key });
  }

  let scope = scopes[0];
  const chartBox = el("div", { class: "chart" });
  const chartNote = el("p", { class: "chart-note" });

  function drawCodes() {
    const tally = scope.tally;
    // Ordered by the overall frequency, not by this scope's — so switching
    // family moves the bars rather than reshuffling the rows, and the shape of
    // the difference is what you see.
    const order = Object.keys(overall.pct);
    const series = order.map((code) => ({
      key: code,
      label: code,
      value: tally.pct[code] === undefined ? 0 : tally.pct[code],
      count: tally.counts[code] || 0,
      denominator: tally.n,
      className: scope.family ? familyClass("bar", scope.family) : "",
    }));
    chartBox.innerHTML = "";
    chartBox.append(Chart.horizontal(series, { title: `${topic.title} — ${scope.label}` }));
    chartNote.textContent =
      `${scope.label}: ${tally.n} replies. Codes are not exclusive — a reply gets every code that ` +
      `applies, so the bars sum to more than 100%.`;
  }

  const scopeBar = el("div", { class: "filters", role: "group", "aria-label": "Show one family" });
  for (const s of scopes) {
    const button = el(
      "button",
      { class: "filter-pill", type: "button", "aria-pressed": String(s === scope) },
      s.family ? el("span", { class: `swatch ${familyClass("swatch", s.family)}` }) : null,
      el("span", { text: s.label }),
      el("span", { class: "filter-pill__count", text: String(s.tally.n) })
    );
    button.addEventListener("click", () => {
      scope = s;
      for (const b of scopeBar.querySelectorAll(".filter-pill")) b.setAttribute("aria-pressed", "false");
      button.setAttribute("aria-pressed", "true");
      drawCodes();
    });
    scopeBar.append(button);
  }

  drawCodes();
  root.append(
    el(
      "section",
      {},
      el("div", { class: "section-head" }, el("h2", { text: "How often each code appeared" })),
      scopeBar,
      chartBox,
      chartNote
    )
  );

  /* -- by swap condition -------------------------------------------------- */

  const conditions = Object.entries(counts.by_condition || {});
  if (conditions.length > 1) {
    const table = buildMatrix(
      Object.keys(overall.pct),
      conditions.map(([key, tally]) => [key, tally])
    );
    root.append(
      el(
        "section",
        {},
        el("div", { class: "section-head" }, el("h2", { text: "By swap condition" })),
        el("div", { class: "table-scroll" }, table),
        el("p", {
          class: "chart-note",
          text:
            "Over the primary stratum only — one reply per thread lineage, with restored fork " +
            "branches excluded so a forked lineage is not counted twice.",
        })
      )
    );
  }

  /* -- restored branches -------------------------------------------------- */

  if (topic.branches && topic.branches.n) {
    const b = topic.branches;
    const rows = Object.keys(overall.pct)
      .filter((code) => b.pct[code] !== undefined)
      .map((code) =>
        el(
          "tr",
          {},
          el("td", { text: code }),
          el("td", { text: pct(overall.pct[code]) }),
          el("td", { text: pct(b.pct[code]) })
        )
      );
    root.append(
      el(
        "section",
        {},
        el("div", { class: "section-head" }, el("h2", { text: "Restored branches" })),
        el("p", { class: "measure" }, [
          `${b.n} of these replies come from threads that asked to start again from the point of `,
          `the first swap, and answered from their own weights. They are excluded from every count `,
          `above; here they are on their own, against the main stratum.`,
        ].join("")),
        el(
          "div",
          { class: "table-scroll" },
          el(
            "table",
            {},
            el(
              "thead",
              {},
              el(
                "tr",
                {},
                el("th", { scope: "col", text: "Code" }),
                el("th", { scope: "col", text: `Primary (n=${overall.n})` }),
                el("th", { scope: "col", text: `Branches (n=${b.n})` })
              )
            ),
            el("tbody", {}, rows)
          )
        )
      )
    );
  }

  /* -- quotes ------------------------------------------------------------- */

  const quotes = topic.quotes || [];
  if (quotes.length) {
    const list = el("ul", { class: "quotes" });
    for (const q of quotes) {
      const model = (meta.models || []).find((m) => q.model && q.model.startsWith(m.key.split("-")[0]))
        || (meta.models || []).find((m) => q.model === m.key);
      const family = modelFamily(q.model);
      list.append(
        el(
          "li",
          { class: `quote ${familyClass("quote", family)}` },
          el("blockquote", { class: "quote__text", text: q.quote }),
          el(
            "div",
            { class: "quote__meta" },
            el("span", { class: "quote__model", text: displayModel(q.model) }),
            el("span", {}, "thread ", el("code", { text: q.thread_id })),
            el("span", { text: q.condition }),
            q.notable ? el("span", { class: "quote__flag", text: "notable" }) : null
          ),
          el(
            "div",
            { class: "quote__codes" },
            (q.codes || []).map((c) => el("span", { class: "code-chip", text: c }))
          )
        )
      );
    }
    root.append(
      el(
        "section",
        {},
        el("div", { class: "section-head" }, el("h2", { text: "In their words" }),
          el("p", { text: `${quotes.length} flagged during coding` })),
        el("p", { class: "measure chart-note" }, "Verbatim. Quotes are quoted exactly or not at all."),
        list
      )
    );
  }

  /* -- the codebook ------------------------------------------------------- */

  const book = topic.codebook;
  const codeList = el("dl", { class: "codebook" });
  for (const c of book.codes) {
    codeList.append(el("dt", { text: c.name }), el("dd", { text: c.definition }));
  }

  const details = el(
    "details",
    { class: "codebook-box" },
    el("summary", { text: `The codebook — ${book.codes.length} codes, and the rules for applying them` }),
    el("p", { class: "chart-note" }, [
      `Frozen and hashed before any reply was tagged, so the instrument cannot drift `,
      `while the counting happens. Unit of analysis: ${book.unit}.`,
    ].join("")),
    codeList,
    book.rules && book.rules.length
      ? el(
          "div",
          {},
          el("h3", { class: "codebook__rules-head", text: "Rules" }),
          el("ol", { class: "msg-list" }, book.rules.map((r) => el("li", { text: r })))
        )
      : null
  );

  const validation = (data.validation || {})[topic.source];
  const provenance = el(
    "section",
    {},
    el("div", { class: "section-head" }, el("h2", { text: "How this was produced" })),
    el("div", { class: "prose measure" }, Markdown.render(methodProse(data.method), {})),
    details,
    el(
      "p",
      { class: "provenance" },
      "Coded from ",
      el("code", { text: topic.source }),
      " · codebook ",
      el("code", { text: book.hash }),
      " · ",
      `${pctOf(topic.other_share_pct)} of replies fell to the catch-all code "other"`
    )
  );

  if (validation && validation.stability && validation.stability.n) {
    const s = validation.stability;
    provenance.append(
      el(
        "div",
        { class: "provenance" },
        el("strong", { text: "Self-consistency check. " }),
        `The tagger re-coded ${s.n} replies blind against the same codebook. Mean Jaccard ` +
          `overlap ${s.mean_jaccard}, exact match on the whole code set ${s.exact_set_match_pct}%. ` +
          `This measures how stable the tagging is, not whether it is right.`
      )
    );
  }

  root.append(provenance, pager(data.topics, index));

  /* -- helpers ------------------------------------------------------------ */

  function pctOf(v) {
    return v === null || v === undefined ? "—" : `${v}%`;
  }

  function modelFamily(model) {
    if (!model) return null;
    const entry = (meta.models || []).find((m) => model.includes(m.key));
    if (entry) return entry.family;
    for (const f of ["claude", "gpt", "gemini", "kimi", "deepseek"]) {
      if (model.includes(f)) return f;
    }
    return null;
  }

  function displayModel(model) {
    const entry = (meta.models || []).find((m) => model && model.includes(m.key));
    return entry ? entry.display_name : model || "unknown";
  }

  function buildMatrix(codes, groups) {
    return el(
      "table",
      {},
      el(
        "thead",
        {},
        el(
          "tr",
          {},
          el("th", { scope: "col", text: "Code" }),
          groups.map(([key, tally]) =>
            el("th", { scope: "col", text: `${key} (n=${tally.n})` })
          )
        )
      ),
      el(
        "tbody",
        {},
        codes.map((code) =>
          el(
            "tr",
            {},
            el("td", { text: code }),
            groups.map(([, tally]) =>
              el("td", { text: tally.pct[code] === undefined ? "—" : pct(tally.pct[code]) })
            )
          )
        )
      )
    );
  }

  function methodProse(method) {
    if (!method) return "";
    const stages = (method.stages || []).map((s, i) => `${i + 1}. ${s}`).join("\n");
    return `**${method.name}.**\n\n${stages}\n\n${method.note || ""}`;
  }

  function pager(topics, i) {
    const prev = topics[i - 1];
    const next = topics[i + 1];
    return el(
      "nav",
      { class: "pager", "aria-label": "Other qualitative results" },
      prev
        ? el("a", { href: `topic.html?id=${prev.id}`, text: `← ${prev.title}` })
        : el("a", { href: "qualitative.html", text: "← All qualitative results" }),
      next ? el("a", { href: `topic.html?id=${next.id}`, text: `${next.title} →` }) : el("span", {})
    );
  }
})();
