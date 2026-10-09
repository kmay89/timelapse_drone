// @ts-check
/* One modal bottom sheet (hotspot details, the contents, photo views) and a toast. The sheet traps
 * focus by making the rest of the page inert, closes on Esc, the close button, the backdrop or a swipe
 * down, and hands focus back to whatever opened it. */

const sheetTitle = h("h2", { class: "v-dlg__title", id: "v-dlg-title" });
const sheetKicker = h("p", { class: "v-dlg__kicker v-label" });
const sheetBody = h("div", { class: "v-dlg__body" });
const sheetClose = h("button", { class: "v-btn v-dlg__close", type: "button", "aria-label": "Close" }, [icon("close")]);
const sheet = h("div", { class: "v-dlg", role: "dialog", "aria-modal": "true", "aria-labelledby": "v-dlg-title" }, [
  h("div", { class: "v-dlg__grab", "aria-hidden": "true" }),
  h("header", { class: "v-dlg__head" }, [h("div", {}, [sheetKicker, sheetTitle]), sheetClose]),
  sheetBody,
]);
const sheetBack = h("div", { class: "v-dlg-back" });
const sheetWrap = h("div", { class: "v-dlg-wrap", hidden: true }, [sheetBack, sheet]);
/** @type {HTMLElement | null} */
let sheetFrom = null;
let sheetTimer = 0;

/** @param {{kicker?: string, title: string, html?: string, node?: Node, from?: HTMLElement | null, wide?: boolean}} o */
function openSheet(o) {
  clearTimeout(sheetTimer);
  sheetFrom = o.from || /** @type {HTMLElement} */ (doc.activeElement);
  sheetKicker.textContent = o.kicker || "";
  sheetTitle.textContent = o.title;
  sheetBody.replaceChildren();
  if (o.html) sheetBody.innerHTML = o.html;
  if (o.node) sheetBody.append(o.node);
  sheet.classList.toggle("v-dlg--wide", !!o.wide);
  sheetWrap.hidden = false;
  for (const el of doc.body.children) if (el !== sheetWrap && el !== liveRegion && el !== toastEl) el.inert = true;
  sheet.style.transform = "";
  requestAnimationFrame(() => {
    sheet.classList.toggle("v-fit", sheetBody.scrollHeight <= sheetBody.clientHeight);
    sheetWrap.classList.add("v-open");
    sheetClose.focus({ preventScroll: true });
  });
}

function closeSheet() {
  if (sheetWrap.hidden) return;
  sheetWrap.classList.remove("v-open");
  for (const el of doc.body.children) el.inert = false;
  sheet.style.transform = "";
  sheetFrom?.focus({ preventScroll: true });
  sheetTimer = window.setTimeout(() => (sheetWrap.hidden = true), reduced ? 0 : 320);
}

sheetClose.addEventListener("click", closeSheet);
sheetBack.addEventListener("click", closeSheet);
doc.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && !sheetWrap.hidden) closeSheet();
});

/* Swipe down to dismiss: from the header anywhere, from the body when it isn't scrolled. */
{
  /** @type {{id: number, y: number, t: number} | null} */
  let drag = null;
  sheet.addEventListener("pointerdown", (e) => {
    const inBody = sheetBody.contains(/** @type {Node} */ (e.target));
    if ((inBody && (sheetBody.scrollTop > 0 || !sheet.classList.contains("v-fit"))) || e.button > 0) return;
    if (/** @type {HTMLElement} */ (e.target).closest("button, a, input, select")) return;
    drag = { id: e.pointerId, y: e.clientY, t: e.timeStamp };
    sheet.setPointerCapture(e.pointerId);
    sheet.classList.add("v-drag");
  });
  sheet.addEventListener("pointermove", (e) => {
    if (drag && e.pointerId === drag.id) sheet.style.transform = `translate3d(0,${Math.max(0, e.clientY - drag.y)}px,0)`;
  });
  const end = (/** @type {PointerEvent} */ e) => {
    if (!drag || e.pointerId !== drag.id) return;
    const dy = e.clientY - drag.y;
    sheet.classList.remove("v-drag");
    if (dy > 90 || dy / Math.max(1, e.timeStamp - drag.t) > 0.6) closeSheet();
    else sheet.style.transform = "";
    drag = null;
  };
  sheet.addEventListener("pointerup", end);
  sheet.addEventListener("pointercancel", end);
}

const toastEl = h("div", { class: "v-toast", role: "status" });
let toastTimer = 0;
function toast(/** @type {string} */ text) {
  toastEl.textContent = text;
  toastEl.classList.add("v-on");
  clearTimeout(toastTimer);
  toastTimer = window.setTimeout(() => toastEl.classList.remove("v-on"), 2800);
}
