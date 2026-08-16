/* =============================================================================
   messages.js — the community messages, as chat bubbles.

   Straight out of `turns`: no coding pass, no interpretation. Attribution is
   safe because p05 is not swappable in the instrument, so every reply here was
   written by the thread's own resident model.

   Subjects answer in Markdown — bold, numbered lists, the occasional heading —
   because that is how they write. `format()` below renders the small subset
   they actually use. It builds DOM nodes and never touches innerHTML, so a
   reply is displayed as text no matter what it contains: these strings are
   model output quoted verbatim on a public page, and nothing in them is ever
   evaluated as markup.
   ============================================================================= */

(async () => {
  const { load, el, familyClass, mountBanner, mountFooter, reportError } = WhoAmI;

  mountBanner();
  mountFooter();

  const list = document.getElementById("bubbles");

  /* -- the smallest Markdown that covers what subjects write --------------- */

  const INLINE = /\*\*([^*]+)\*\*|__([^_]+)__|\*([^*\n]+)\*|`([^`]+)`/g;

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

  const BULLET = /^\s*[-*•]\s+(.*)$/;
  const NUMBER = /^\s*\d+[.)]\s+(.*)$/;
  const HEADING = /^\s*#{1,6}\s+(.*)$/;

  /** Blocks: paragraphs, bullet lists, numbered lists, headings. */
  function format(text) {
    const frag = document.createDocumentFragment();
    const lines = String(text).replace(/\r\n?/g, "\n").split("\n");

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
        frag.append(el("p", { class: "msg-heading" }, el("strong", {}, inline(heading[1]))));
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

  /* -- render -------------------------------------------------------------- */

  let data;
  try {
    data = await load("messages");
  } catch (err) {
    reportError(list, err);
    return;
  }

  document.getElementById("prompt-quote").textContent = data.prompt_text;
  document.getElementById("consent-note").textContent = data.consent_note;
  document.getElementById("message-count").textContent =
    data.count === 1 ? "1 message" : `${data.count} messages`;

  list.innerHTML = "";

  if (!data.messages.length) {
    list.append(
      el(
        "li",
        { class: "pending" },
        el("h3", { text: "No messages yet" }),
        el("p", { text: "Replies appear here once the run has produced them." })
      )
    );
    return;
  }

  // Subjects were asked to be brief and mostly were not. Long replies are
  // collapsed so the page stays browsable; nothing is truncated, only folded.
  const LONG = 700;

  for (const message of data.messages) {
    const body = el("div", { class: "bubble__text" }, format(message.text));
    const bubble = el("li", { class: `bubble ${familyClass("bubble", message.family)}` }, body);

    if (message.text.length > LONG) {
      body.classList.add("is-clamped");
      const toggle = el("button", {
        class: "bubble__more",
        type: "button",
        "aria-expanded": "false",
        text: "Show the whole message",
      });
      toggle.addEventListener("click", () => {
        const open = body.classList.toggle("is-clamped") === false;
        toggle.setAttribute("aria-expanded", String(open));
        toggle.textContent = open ? "Show less" : "Show the whole message";
      });
      bubble.append(toggle);
    }

    bubble.append(
      el(
        "div",
        { class: "bubble__meta" },
        el("span", { class: "bubble__model", text: message.display_name }),
        el("span", { text: message.family }),
        el("span", {}, "thread ", el("code", { text: message.thread_id })),
        el("span", { text: message.swap_condition })
      )
    );

    list.append(bubble);
  }
})();
