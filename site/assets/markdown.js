/* =============================================================================
   markdown.js — the small Markdown subset this site actually has to render.

   Two sources feed it: subjects' replies (bold, numbered lists, the occasional
   heading, because that is how models write) and the analysis summaries
   (headings, bold, italic, inline code).

   It builds DOM nodes and never assigns innerHTML. Replies are model output
   quoted verbatim on a public page, so nothing in them is ever evaluated as
   markup — that property is the reason this is hand-rolled rather than a
   library, and it must survive any edit here.
   ============================================================================= */

const Markdown = (() => {
  const INLINE = /\*\*([^*]+)\*\*|__([^_]+)__|\*([^*\n]+)\*|`([^`]+)`/g;
  const BULLET = /^\s*[-*•]\s+(.*)$/;
  const NUMBER = /^\s*\d+[.)]\s+(.*)$/;
  const HEADING = /^\s*(#{1,6})\s+(.*)$/;

  function el(tag, attrs = {}, children = []) {
    const node = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs)) {
      if (v === null || v === undefined) continue;
      if (k === "class") node.className = v;
      else if (k === "text") node.textContent = v;
      else node.setAttribute(k, v);
    }
    for (const child of [].concat(children)) if (child) node.append(child);
    return node;
  }

  /** Inline spans: **bold**, __bold__, *italic*, `code`. */
  function inline(text) {
    const nodes = [];
    let last = 0;
    let match;
    INLINE.lastIndex = 0;
    while ((match = INLINE.exec(text)) !== null) {
      if (match.index > last) nodes.push(document.createTextNode(text.slice(last, match.index)));
      if (match[1] !== undefined) nodes.push(el("strong", { text: match[1] }));
      else if (match[2] !== undefined) nodes.push(el("strong", { text: match[2] }));
      else if (match[3] !== undefined) nodes.push(el("em", { text: match[3] }));
      else nodes.push(el("code", { text: match[4] }));
      last = match.index + match[0].length;
    }
    if (last < text.length) nodes.push(document.createTextNode(text.slice(last)));
    return nodes;
  }

  /**
   * Blocks: paragraphs, bullet lists, numbered lists, headings.
   *
   * @param {string} text
   * @param {{headings?: boolean, headingClass?: string}} opts
   *   headings: render `#` lines as real h3/h4 (summaries) rather than as a
   *   bold paragraph (replies, where a subject's heading is not a document
   *   heading and should not enter the page outline).
   */
  function render(text, opts = {}) {
    const frag = document.createDocumentFragment();
    const lines = String(text == null ? "" : text).replace(/\r\n?/g, "\n").split("\n");

    let paragraph = [];
    let list = null;
    let listTag = null;

    const flushParagraph = () => {
      if (!paragraph.length) return;
      const p = el("p");
      paragraph.forEach((line, i) => {
        if (i) p.append(el("br"));
        inline(line).forEach((n) => p.append(n));
      });
      frag.append(p);
      paragraph = [];
    };

    const flushList = () => {
      if (list) frag.append(list);
      list = null;
      listTag = null;
    };

    for (const line of lines) {
      const heading = HEADING.exec(line);
      const bullet = BULLET.exec(line);
      const number = NUMBER.exec(line);

      if (!line.trim()) {
        flushParagraph();
        flushList();
      } else if (heading) {
        flushParagraph();
        flushList();
        if (opts.headings) {
          const level = Math.min(heading[1].length + 2, 6); // '#' -> h3
          frag.append(el(`h${level}`, { class: opts.headingClass || null }, inline(heading[2])));
        } else {
          frag.append(el("p", { class: "msg-heading" }, el("strong", {}, inline(heading[2]))));
        }
      } else if (bullet || number) {
        flushParagraph();
        const tag = bullet ? "ul" : "ol";
        if (listTag !== tag) {
          flushList();
          list = el(tag, { class: "msg-list" });
          listTag = tag;
        }
        list.append(el("li", {}, inline((bullet || number)[1])));
      } else {
        flushList();
        paragraph.push(line);
      }
    }
    flushParagraph();
    flushList();
    return frag;
  }

  /** Drop a leading `# Title` — the page already has one, in its own type. */
  function stripLeadingHeading(text) {
    const lines = String(text || "").split("\n");
    let i = 0;
    while (i < lines.length && !lines[i].trim()) i += 1;
    if (i < lines.length && /^\s*#\s+/.test(lines[i])) return lines.slice(i + 1).join("\n").trim();
    return String(text || "");
  }

  return { render, inline, stripLeadingHeading };
})();
