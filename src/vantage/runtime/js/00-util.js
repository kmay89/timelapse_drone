// @ts-check
/* Vantage runtime. render.py concatenates runtime/js/*.js (filename order) inside one strict IIFE,
 * so every top-level name here is shared by the later files. No dependencies, ES2020, iOS Safari 16.4+.
 * The page is complete without this script; everything below upgrades the pre-rendered HTML. */

const doc = document;
const root = doc.documentElement;
const story = JSON.parse(/** @type {HTMLElement} */ (doc.getElementById("vantage-story")).textContent || "{}");
const edition = root.dataset.edition || "site";
const VANTAGES = Object.fromEntries(story.vantages.map((/** @type {any} */ v) => [v.id, v]));
const MONTHS = "January February March April May June July August September October November December".split(" ");
const motionQuery = matchMedia("(prefers-reduced-motion: reduce)");
let reduced = motionQuery.matches;
motionQuery.addEventListener("change", (e) => (reduced = e.matches));

/** @type {(sel: string, el?: ParentNode) => any} */
const $ = (sel, el = doc) => el.querySelector(sel);
/** @type {(sel: string, el?: ParentNode) => any[]} */
const $$ = (sel, el = doc) => Array.from(el.querySelectorAll(sel));
const clamp = (/** @type {number} */ x, a = 0, b = 1) => Math.min(b, Math.max(a, x));
const lerp = (/** @type {number} */ a, /** @type {number} */ b, /** @type {number} */ t) => a + (b - a) * t;
const smooth = (/** @type {number} */ t) => t * t * (3 - 2 * t);
const easeOut = (/** @type {number} */ t) => 1 - (1 - t) ** 3;
/** Inverse of smoothstep on [0, 1]. */
const unsmooth = (/** @type {number} */ a) => 0.5 - Math.sin(Math.asin(1 - 2 * clamp(a)) / 3);
const pad = (/** @type {number} */ n) => String(n).padStart(2, "0");
/** "2025-06-14" → days since 1970 (UTC, so no DST drift). */
const day = (/** @type {string} */ iso) => Date.UTC(+iso.slice(0, 4), +iso.slice(5, 7) - 1, +iso.slice(8, 10)) / 864e5;
const isPlaceholder = (/** @type {any} */ img) => !img.sources.length && img.fallback === img.lqip;
const passive = { passive: true };

/**
 * Tiny element builder: h("button", {class: "x", "aria-label": "Close", onclick: fn}, [child, "text"]).
 * @param {string} tag @param {Record<string, any>} [props] @param {(Node | string | null | false)[]} [kids]
 * @returns {any}
 */
function h(tag, props = {}, kids = []) {
  const el = doc.createElement(tag);
  for (const [k, v] of Object.entries(props)) {
    if (v == null || v === false) continue;
    if (k === "class") el.className = v;
    else if (k === "text") el.textContent = v;
    else if (k === "html") el.innerHTML = v;
    else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else el.setAttribute(k, v === true ? "" : v);
  }
  for (const kid of kids) if (kid) el.append(kid);
  return el;
}

const ICONS = {
  menu: "M4 7h16M4 12h16M4 17h10",
  share: "M12 3v12M7.5 7.5 12 3l4.5 4.5M6 11H5v9h14v-9h-1",
  close: "M6 6l12 12M18 6 6 18",
  play: "M8 5.5v13l10.5-6.5z",
  pause: "M8 5v14M16 5v14",
  reset: "M4 12a8 8 0 1 0 2.4-5.7M4 4v4.5h4.5",
  save: "M12 4v11m-4.5-4.5L12 15l4.5-4.5M5 19h14",
  grip: "M10 8l-4 4 4 4M14 8l4 4-4 4",
  arrow: "M5 12h13m-5-5 5 5-5 5",
  check: "M5 12.5l4.5 4.5L19 7",
};
/** Inline stroke icon (24px grid). @param {keyof ICONS} name */
const icon = (name) =>
  h("span", {
    class: "v-ico",
    "aria-hidden": "true",
    html: `<svg viewBox="0 0 24 24" width="24" height="24"><path d="${ICONS[name]}"/></svg>`,
  });

/** "April 2025" → ["April", "2025"]; anything else stays whole. @param {string} label */
function splitLabel(label) {
  const m = /^(.*\S)\s+(\d{4})$/.exec(label);
  return m ? [m[1], m[2]] : [label, ""];
}

/** A polite live region whose say(text) waits until the text has settled, so scrubbing doesn't chatter. */
function liveRegion() {
  const el = h("p", { class: "v-sr", "aria-live": "polite" });
  let timer = 0;
  el.say = (/** @type {string} */ text) => {
    clearTimeout(timer);
    timer = window.setTimeout(() => (el.textContent = text), 600);
  };
  return el;
}

/** Text of an HTML fragment (step cards → aria labels). */
const plain = (/** @type {string} */ html) => h("div", { html }).textContent.trim();
