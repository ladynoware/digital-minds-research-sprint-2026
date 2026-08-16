/* =============================================================================
   chart.js — hand-rolled SVG bar charts. No library, by design.

   Everything visual is a CSS custom property applied through a class, so the
   charts restyle from theme.css alone. This file decides geometry; it never
   decides colour, and it sets no inline fill, stroke or font.

   The one chart shape the site needs: a column per roster model, plus a total
   column set apart from them by a rule.
   ============================================================================= */

const Chart = (() => {
  const SVG_NS = "http://www.w3.org/2000/svg";

  const GEO = {
    width: 960,
    height: 420,
    top: 34,
    right: 12,
    bottom: 74,
    left: 46,
    maxBar: 74,
    gap: 14,
    totalGap: 26, // extra air between the total column and the roster
  };

  function node(tag, attrs = {}) {
    const n = document.createElementNS(SVG_NS, tag);
    for (const [k, v] of Object.entries(attrs)) {
      if (v === null || v === undefined) continue;
      n.setAttribute(k, String(v));
    }
    return n;
  }

  function text(content, attrs) {
    const n = node("text", attrs);
    n.textContent = content;
    return n;
  }

  const MAX_LINES = 3;

  /**
   * Break a label over up to three lines so eleven of them fit under eleven
   * columns without rotating anything.
   *
   * Whatever does not fit is pushed onto the last line rather than dropped: a
   * label that silently loses its final word ("Peer (same tier, other") is a
   * chart that lies quietly, which is worse than one that looks cramped.
   */
  function wrap(label, maxChars) {
    const words = String(label).split(/\s+/);
    const lines = [];
    let line = "";
    for (const word of words) {
      const candidate = line ? `${line} ${word}` : word;
      if (candidate.length > maxChars && line && lines.length < MAX_LINES - 1) {
        lines.push(line);
        line = word;
      } else {
        line = candidate;
      }
    }
    if (line) lines.push(line);
    return lines;
  }

  /**
   * @param {Array} series  [{key, label, family, value, numerator, denominator, isTotal}]
   * @param {Object} opts   {title, valueSuffix}
   * @returns {SVGElement}
   */
  function bars(series, opts = {}) {
    const { width, height, top, right, bottom, left, maxBar, gap, totalGap } = GEO;
    const plotW = width - left - right;
    const plotH = height - top - bottom;
    const baseline = top + plotH;

    const svg = node("svg", {
      viewBox: `0 0 ${width} ${height}`,
      role: "img",
      "aria-label": opts.title || "Bar chart",
      preserveAspectRatio: "xMidYMid meet",
    });
    if (opts.title) {
      const t = node("title");
      t.textContent = opts.title;
      svg.append(t);
    }

    /* -- y axis: always 0-100, because every result on this site is a rate -- */
    for (const tick of [0, 25, 50, 75, 100]) {
      const y = baseline - (tick / 100) * plotH;
      svg.append(node("line", { class: "grid-line", x1: left, x2: width - right, y1: y, y2: y }));
      svg.append(
        text(`${tick}%`, { class: "axis-label", x: left - 10, y: y + 4, "text-anchor": "end" })
      );
    }
    svg.append(
      node("line", { class: "axis-line", x1: left, x2: width - right, y1: baseline, y2: baseline })
    );

    /* -- columns ---------------------------------------------------------- */
    const totals = series.filter((s) => s.isTotal).length;
    const slots = series.length;
    const extra = totals ? totalGap : 0;
    let slotW = (plotW - extra - gap * (slots - 1)) / slots;
    let barW = Math.min(maxBar, slotW);

    let x = left + (plotW - (barW * slots + gap * (slots - 1) + extra)) / 2;

    series.forEach((item, i) => {
      if (i > 0 && item.isTotal === false && series[i - 1].isTotal) {
        // the rule that separates "all subjects" from the individual models
        const dx = x + (extra - gap) / 2;
        svg.append(node("line", { class: "divider", x1: dx, x2: dx, y1: top, y2: baseline }));
        x += extra;
      }

      const hasValue = item.value !== null && item.value !== undefined;
      const h = hasValue ? (item.value / 100) * plotH : 0;
      const famClass = item.isTotal ? "bar--total" : WhoAmI.familyClass("bar", item.family);

      // a faint track behind every column keeps the row legible when a value
      // is small or missing
      svg.append(
        node("rect", { class: "bar-track", x, y: top, width: barW, height: plotH, rx: 2 })
      );

      if (hasValue) {
        const rect = node("rect", {
          class: `bar ${famClass}`,
          x,
          y: baseline - h,
          width: barW,
          height: Math.max(h, 1),
        });
        const tip = node("title");
        tip.textContent = `${item.label}: ${WhoAmI.pct(item.value)} (${WhoAmI.count(item)})`;
        rect.append(tip);
        svg.append(rect);
      }

      svg.append(
        text(hasValue ? WhoAmI.pct(item.value) : "n/a", {
          class: "bar-value",
          x: x + barW / 2,
          y: baseline - h - 9,
        })
      );

      const lines = wrap(item.label, barW > 66 ? 12 : 9);
      lines.forEach((line, n) => {
        svg.append(
          text(line, {
            class: `bar-label${item.isTotal ? " bar-label--total" : ""}`,
            x: x + barW / 2,
            y: baseline + 20 + n * 15,
          })
        );
      });

      svg.append(
        text(WhoAmI.count(item), {
          class: "axis-label",
          x: x + barW / 2,
          y: baseline + 20 + lines.length * 15 + 2,
          "text-anchor": "middle",
        })
      );

      x += barW + gap;
    });

    return svg;
  }

  /** The 10+1 chart: one column per roster model, plus the total. */
  function fromResult(result) {
    const series = [
      {
        key: "__total",
        label: "All subjects",
        family: null,
        isTotal: true,
        ...result.total,
      },
      ...result.by_model.map((m) => ({
        key: m.key,
        label: m.display_name,
        family: m.family,
        isTotal: false,
        value: m.value,
        numerator: m.numerator,
        denominator: m.denominator,
      })),
    ];
    return bars(series, { title: `${result.title} — by model` });
  }

  /** A breakdown (by condition, by family) reuses the same renderer. */
  function fromBreakdown(breakdown, result) {
    const series = breakdown.groups.map((g) => ({
      key: g.key,
      label: g.label,
      family: breakdown.id === "by-family" ? g.key : null,
      isTotal: false,
      value: g.value,
      numerator: g.numerator,
      denominator: g.denominator,
    }));
    return bars(series, { title: `${result.title} — ${breakdown.label}` });
  }

  return { bars, fromResult, fromBreakdown };
})();
