// @ts-check
/* The scroll engine: one requestAnimationFrame loop for the whole page, woken by passive scroll/resize
 * listeners, IntersectionObserver activations, pointer input and tweens; it sleeps when nothing moves.
 * Each frame reads first (every active component's measure()), then writes (update()), so the loop never
 * interleaves layout reads with style writes. A component whose update() returns true asks for another
 * frame (an easing in progress). */

/** @typedef {{measure?(): any, update(m: any, now: number): boolean | void, enter?(): void, leave?(): void, warm?(on: boolean): void, resize?(): void}} Component */

/** @type {Set<Component>} */
const actives = new Set();
/** Components that just left: they get one last update (so fast flings land on the end state). */
/** @type {Set<Component>} */
const leaving = new Set();
/** @type {Set<Component>} */
const sized = new Set();
/** @type {Set<(now: number) => boolean | void>} */
const tasks = new Set();
let frameId = 0;

function kick() {
  if (!frameId) frameId = requestAnimationFrame(frame);
}

function frame(/** @type {number} */ now) {
  frameId = 0;
  const list = [...actives, ...leaving];
  const measured = list.map((c) => (c.measure ? c.measure() : null));
  let more = false;
  list.forEach((c, k) => {
    if (c.update(measured[k], now)) more = true;
  });
  leaving.clear();
  for (const task of [...tasks]) if (!task(now)) tasks.delete(task);
  if (more || tasks.size) kick();
}

/** Run `fn(now)` every frame until it returns falsy. */
function every(/** @type {(now: number) => boolean | void} */ fn) {
  tasks.add(fn);
  kick();
}

/** Ease 0→1 over `ms` (instantly under reduced motion unless `force`); resolves when done. */
function tween(/** @type {number} */ ms, /** @type {(t: number) => void} */ step, force = false) {
  return new Promise((done) => {
    let t0 = 0;
    every((now) => {
      t0 = t0 || now;
      const t = reduced && !force ? 1 : clamp((now - t0) / ms);
      step(t);
      if (t >= 1) done(undefined);
      return t < 1;
    });
  });
}

/* Activation: a component is updated every frame while `el` intersects the viewport, and warmed
 * (images decoded ahead) while it is within 1.5 viewports; it is cooled down beyond 3. */
const io = (/** @type {string} */ margin, /** @type {(c: Component, on: boolean) => void} */ fn) => {
  const map = new Map();
  const obs = new IntersectionObserver(
    (entries) => entries.forEach((e) => fn(map.get(e.target), e.isIntersecting)),
    { rootMargin: margin },
  );
  return (/** @type {Element} */ el, /** @type {Component} */ c) => {
    map.set(el, c);
    obs.observe(el);
  };
};
const watchActive = io("0px", (c, on) => {
  if (on) {
    actives.add(c);
    c.enter?.();
  } else if (actives.delete(c)) {
    leaving.add(c);
    c.leave?.();
  }
  kick();
});
const watchWarm = io("150% 0px", (c, on) => on && c.warm?.(true));
const watchCold = io("300% 0px", (c, on) => !on && c.warm?.(false));

/** Register a component: updated while `el` is on screen, warmed near it, resized with the window. */
function mount(/** @type {Element} */ el, /** @type {Component} */ c) {
  watchActive(el, c);
  if (c.warm) {
    watchWarm(el, c);
    watchCold(el, c);
  }
  if (c.resize) {
    sized.add(c);
    c.resize();
  }
}

let resizeTimer = 0;
let lastWidth = innerWidth;
addEventListener("scroll", kick, passive);
addEventListener(
  "resize",
  () => {
    // iOS fires resize when the toolbar collapses; stages are sized in svh, so only width matters.
    clearTimeout(resizeTimer);
    resizeTimer = window.setTimeout(() => {
      if (innerWidth === lastWidth && !matchMedia("(pointer: fine)").matches) return;
      lastWidth = innerWidth;
      sized.forEach((c) => c.resize?.());
      kick();
    }, 120);
  },
  passive,
);

/** Progress through a pinned track: 0 when its top meets the viewport top, 1 when its bottom meets the stage bottom. */
function pinProgress(/** @type {Element} */ track, /** @type {number} */ trackH, /** @type {number} */ stageH) {
  return clamp(-track.getBoundingClientRect().top / Math.max(1, trackH - stageH));
}

/** Document scroll position at which `track` shows progress p. */
function pinScrollY(/** @type {Element} */ track, /** @type {number} */ trackH, /** @type {number} */ stageH, /** @type {number} */ p) {
  return track.getBoundingClientRect().top + scrollY + p * (trackH - stageH);
}

const scrollToY = (/** @type {number} */ y, smoothly = true) =>
  scrollTo({ top: Math.round(y), behavior: smoothly && !reduced ? "smooth" : "auto" });

/**
 * Horizontal drags on an element whose touch-action is pan-y: vertical swipes stay native scrolling;
 * travel > 8px and > 1.7× the vertical captures the pointer. Taps (no travel) go to `tap`.
 * @param {HTMLElement} el
 * @param {{start?(e: PointerEvent): void, move(e: PointerEvent, dx: number): void, end?(e: PointerEvent): void, tap?(e: PointerEvent): void, down?(e: PointerEvent): boolean | void}} cb
 */
function hdrag(el, cb) {
  /** @type {{id: number, x: number, y: number, drag: boolean} | null} */
  let s = null;
  el.addEventListener("pointerdown", (e) => {
    if (s || e.button > 0 || cb.down?.(e) === false) return;
    s = { id: e.pointerId, x: e.clientX, y: e.clientY, drag: false };
  });
  el.addEventListener("pointermove", (e) => {
    if (!s || e.pointerId !== s.id) return;
    const dx = e.clientX - s.x;
    const dy = e.clientY - s.y;
    if (!s.drag && Math.abs(dx) > 8 && Math.abs(dx) > 1.7 * Math.abs(dy)) {
      s.drag = true;
      el.setPointerCapture(e.pointerId);
      cb.start?.(e);
    }
    if (s.drag) cb.move(e, dx);
  });
  const up = (/** @type {PointerEvent} */ e) => {
    if (!s || e.pointerId !== s.id) return;
    if (s.drag) cb.end?.(e);
    else if (e.type === "pointerup" && Math.hypot(e.clientX - s.x, e.clientY - s.y) < 8) cb.tap?.(e);
    s = null;
  };
  el.addEventListener("pointerup", up);
  el.addEventListener("pointercancel", up);
}
