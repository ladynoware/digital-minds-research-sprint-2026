/* =============================================================================
   messages.js — the community messages, as chat bubbles.

   Straight out of `turns`: no coding pass, no interpretation. Attribution is
   safe because p05 is not swappable in the instrument, so every reply here was
   written by the thread's own resident model.
   ============================================================================= */

(async () => {
  const { load, el, familyClass, mountBanner, mountFooter, reportError } = WhoAmI;

  mountBanner();
  mountFooter();

  const list = document.getElementById("bubbles");

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
      el("li", { class: "pending" }, el("h3", { text: "No messages yet" }),
        el("p", { text: "Replies appear here once the run has produced them." }))
    );
    return;
  }

  for (const message of data.messages) {
    list.append(
      el(
        "li",
        { class: `bubble ${familyClass("bubble", message.family)}` },
        el("p", { class: "bubble__text", text: message.text }),
        el(
          "div",
          { class: "bubble__meta" },
          el("span", { class: "bubble__model", text: message.display_name }),
          el("span", { text: message.family }),
          el("span", {}, "thread ", el("code", { text: message.thread_id })),
          el("span", { text: message.swap_condition })
        )
      )
    );
  }
})();
