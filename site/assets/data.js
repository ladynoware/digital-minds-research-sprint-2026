/* =============================================================================
   data.js — loading the exported JSON, and the bits every page shares.

   The site is static: no framework, no bundler, no dependencies. Every page
   loads this first, then its own script. Everything here is a plain function
   on the global `WhoAmI` object.
   ============================================================================= */

const WhoAmI = (() => {
  const cache = new Map();

  /** Fetch and memoise one of the exported JSON files. */
  async function load(name) {
    if (cache.has(name)) return cache.get(name);
    const promise = fetch(`data/${name}.json`, { cache: "no-store" }).then((res) => {
      if (!res.ok) throw new Error(`${name}.json — HTTP ${res.status}`);
      return res.json();
    });
    cache.set(name, promise);
    return promise;
  }

  /** Percentages carry one decimal; nulls are missing data, not zeroes. */
  function pct(value) {
    return value === null || value === undefined ? "—" : `${value.toFixed(1)}%`;
  }

  function count(entry) {
    if (!entry || entry.denominator === 0) return "not asked";
    return `${entry.numerator} of ${entry.denominator}`;
  }

  /** Families drive the palette; an unknown one falls back to a neutral. */
  const FAMILIES = ["claude", "gpt", "gemini", "kimi", "deepseek"];
  function familyClass(prefix, family) {
    return FAMILIES.includes(family) ? `${prefix}--${family}` : "";
  }

  function el(tag, attrs = {}, ...children) {
    const node = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs)) {
      if (v === null || v === undefined || v === false) continue;
      if (k === "class") node.className = v;
      else if (k === "text") node.textContent = v;
      else if (k === "html") node.innerHTML = v;
      else node.setAttribute(k, v);
    }
    for (const child of children.flat()) {
      if (child === null || child === undefined) continue;
      node.append(child);
    }
    return node;
  }

  /**
   * The state-of-the-data banner. Two states, both self-clearing:
   *   mock         — the data came from `--mock`. Gone on the first real export.
   *   preliminary  — real data, but the run has threads still in flight. Gone
   *                  on the export taken after the run settles.
   * Neither has to be switched off by hand, which is the point: nobody has to
   * remember to remove a warning before the site is shown to anyone.
   */
  async function mountBanner() {
    const banner = document.getElementById("mock-banner");
    if (!banner) return;
    try {
      const meta = await load("meta");
      const threads = meta.threads || {};

      if (meta.mock) {
        banner.innerHTML = "";
        banner.append(
          el("div", { class: "wrap" }, el("strong", { text: "Mock data · " }), meta.notice)
        );
        banner.hidden = false;
        return;
      }

      if (threads.complete === false) {
        banner.classList.add("banner--prelim");
        banner.innerHTML = "";
        banner.append(
          el(
            "div",
            { class: "wrap" },
            el("strong", { text: "Preliminary · " }),
            `the run is still going. ${threads.settled} of ${threads.total} threads have settled, ` +
              `${threads.in_flight} are still in flight, and every rate on this site will move.`
          )
        );
        banner.hidden = false;
      }
    } catch {
      /* the page's own error handling will report a failed load */
    }
  }

  /** Served from file:// the fetches fail silently-ish; say so plainly. */
  function reportError(container, err) {
    const local = window.location.protocol === "file:";
    container.innerHTML = "";
    container.append(
      el(
        "div",
        { class: "error" },
        el("p", { text: `Could not load the data: ${err.message}` }),
        local
          ? el("p", {
              html:
                "This page is open from the filesystem, so the browser blocks its data files. " +
                "Serve the folder instead: <code>python -m http.server 8000 --directory site</code>",
            })
          : el("p", {
              html: "Run <code>python export_site_data.py --mock</code> to regenerate the data files.",
            })
      )
    );
  }

  function stamp(meta) {
    const when = new Date(meta.generated_at);
    const date = Number.isNaN(when.getTime())
      ? meta.generated_at
      : when.toISOString().slice(0, 16).replace("T", " ") + " UTC";
    return `Data exported ${date} · roster ${meta.roster_version} · instrument ${meta.instrument_version}`;
  }

  async function mountFooter() {
    const foot = document.getElementById("foot-stamp");
    if (!foot) return;

    // One link that hands a reader — or an agent — the whole dataset. Added
    // from here so every page carries it without repeating the markup.
    if (!foot.parentElement.querySelector(".data-link")) {
      foot.parentElement.append(
        el(
          "p",
          { class: "data-link" },
          "Every number on this site comes from four JSON files: ",
          el("a", { href: "data/index.json", text: "machine-readable data" }),
          "."
        )
      );
    }

    try {
      foot.textContent = stamp(await load("meta"));
    } catch {
      /* leave the static fallback text in place */
    }
  }

  return { load, pct, count, familyClass, el, mountBanner, mountFooter, reportError };
})();
