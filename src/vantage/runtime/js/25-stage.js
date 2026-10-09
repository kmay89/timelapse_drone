// @ts-check
/* Pieces shared by the image stages (scrub, compare, explore): the camera, canvas painting, step cards,
 * hotspot pins, the date odometer and real-date ticks. */

/**
 * Where the whole image lands in a W×H stage (CSS px) for camera [cx, cy, zoom].
 * Portrait stages cover-crop (zoom adds a gentle push-in); landscape stages show the full frame,
 * overscanned up to 12% to lose thin bars. `zoom` is relative to the full frame, so a step's focus
 * zooms on desktop and pans on phones, where the cover crop is already magnified.
 */
function frameRect(/** @type {number} */ W, /** @type {number} */ H, /** @type {number} */ aspect, /** @type {number[]} */ cam) {
  const contain = Math.min(H, W / aspect);
  const cover = Math.max(H, W / aspect);
  const portrait = W < H;
  const base = portrait ? cover * (1 + (cam[2] - 1) / 4) : Math.min(cover, contain * 1.12);
  const hh = Math.max(base, contain * cam[2]);
  const ww = hh * aspect;
  return {
    x: ww > W ? clamp(W / 2 - cam[0] * ww, W - ww, 0) : (W - ww) / 2,
    y: hh > H ? clamp(H / 2 - cam[1] * hh, H - hh, 0) : (H - hh) / 2,
    w: ww,
    h: hh,
  };
}

/** The resting camera: centred, or on the vantage's portrait focus on phones. */
const homeCam = (/** @type {any} */ v, /** @type {number} */ W, /** @type {number} */ H) =>
  W < H && v.portraitFocus ? [v.portraitFocus[0], v.portraitFocus[1], 1] : [0.5, 0.5, 1];
const mixCam = (/** @type {number[]} */ a, /** @type {number[]} */ b, /** @type {number} */ t) => a.map((x, k) => lerp(x, b[k], t));

/** Canvas backing store = CSS size × min(DPR, 2), capped at 2.2 MP. Returns the scale. */
function fitCanvas(/** @type {HTMLCanvasElement} */ canvas, /** @type {number} */ W, /** @type {number} */ H) {
  const s = Math.min(devicePixelRatio || 1, 2, Math.sqrt(2.2e6 / Math.max(1, W * H)));
  canvas.width = Math.round(W * s);
  canvas.height = Math.round(H * s);
  return s;
}

/** Draw the visible part of decoded image `d` placed at rect r (stage px), scaled by s, at `alpha`. */
function paint(/** @type {CanvasRenderingContext2D} */ ctx, /** @type {any} */ d, /** @type {any} */ r, /** @type {number} */ W, /** @type {number} */ H, /** @type {number} */ s, alpha = 1) {
  const x0 = Math.max(r.x, 0);
  const y0 = Math.max(r.y, 0);
  const x1 = Math.min(r.x + r.w, W);
  const y1 = Math.min(r.y + r.h, H);
  if (!d || x1 <= x0 || y1 <= y0 || alpha <= 0.002) return;
  const kx = d.w / r.w;
  const ky = d.h / r.h;
  ctx.globalAlpha = alpha;
  ctx.drawImage(d.src, (x0 - r.x) * kx, (y0 - r.y) * ky, (x1 - x0) * kx, (y1 - y0) * ky, x0 * s, y0 * s, (x1 - x0) * s, (y1 - y0) * s);
}

/** Decode width that keeps the sharpest framing of a stage crisp (≤ the source, ≤ 4 MP). */
function decodeWidth(/** @type {any} */ img, /** @type {number} */ rectW, /** @type {number} */ s, /** @type {number} */ aspect) {
  return Math.min(maxWidth(img), rectW * s, Math.sqrt(4e6 * aspect));
}

/**
 * Hold windows in "v" units: key k holds over [k, k + hold]; keys sharing a k split its hold.
 * @returns {{a: number, b: number}[]}
 */
function holdWindows(/** @type {number[]} */ keys, /** @type {number} */ hold) {
  return keys.map((k, idx) => {
    const n = keys.filter((x) => x === k).length;
    const j = keys.slice(0, idx).filter((x) => x === k).length;
    return { a: k + (hold * j) / n, b: k + (hold * (j + 1)) / n };
  });
}

/** Value at v: held inside each window, eased between windows (a cut halfway under reduced motion). */
function keyed(/** @type {{a: number, b: number}[]} */ wins, /** @type {any[]} */ vals, /** @type {number} */ v, /** @type {(a: any, b: any, t: number) => any} */ mix) {
  let i = -1;
  while (i + 1 < wins.length && wins[i + 1].a <= v) i++;
  if (i < 0) return vals[0];
  if (i === wins.length - 1 || v <= wins[i].b) return vals[i];
  const t = (v - wins[i].b) / Math.max(1e-6, wins[i + 1].a - wins[i].b);
  return mix(vals[i], vals[i + 1], reduced ? +(t >= 0.5) : smooth(t));
}

/** Visibility of a card whose window is w: fades over `f` v-units on either side. */
const cardAlpha = (/** @type {{a: number, b: number}} */ w, /** @type {number} */ v, f = 0.3) =>
  clamp((v - w.a + f) / f) * clamp((w.b + f - v) / f);

/** Step text cards: bottom sheets on phones, a side column on wide screens. Only opacity/transform move. */
class Cards {
  constructor(/** @type {string[]} */ htmls) {
    this.el = h("div", { class: "v-cards" });
    this.items = htmls.map((html) => {
      const el = h("div", { class: "v-card v-prose", html });
      this.el.append(el);
      return { el, o: -1 };
    });
  }

  /** @param {number[]} alphas */
  set(alphas) {
    this.items.forEach((it, k) => {
      const o = Math.round(alphas[k] * 100) / 100;
      if (o === it.o) return;
      it.o = o;
      it.el.style.opacity = String(o);
      it.el.style.transform = o < 1 ? `translate3d(0,${((1 - o) * 14).toFixed(1)}px,0)` : "";
      it.el.classList.toggle("v-on", o > 0.5);
    });
  }
}

/** Hotspot pins: a dot and a label pill pinned in image coordinates; tap opens a sheet. */
class Pins {
  constructor(/** @type {HTMLElement} */ layer, /** @type {any[]} */ hotspots) {
    this.items = hotspots.map((hs) => {
      const el = h("button", { class: "v-hs", type: "button", tabindex: "-1", "aria-label": `${hs.label}: details` }, [
        h("span", { class: "v-hs__dot" }),
        h("span", { class: "v-hs__label", text: hs.label }),
      ]);
      el.addEventListener("click", () =>
        openSheet({ kicker: "Point of interest", title: hs.label, html: hs.html || "", from: el }),
      );
      layer.append(el);
      return { hs, el, x: NaN, y: NaN, o: -1, flip: false };
    });
  }

  /**
   * @param {any} r image rect @param {number} W @param {number} H
   * @param {(hs: any, x: number) => number} vis opacity for a hotspot at stage x
   * @param {number} top @param {number} bottom pins outside [top, H - bottom] hide (odometer, rail)
   */
  place(r, W, H, vis, top, bottom) {
    for (const it of this.items) {
      const x = r.x + it.hs.x * r.w;
      const y = r.y + it.hs.y * r.h;
      const inside = x > 14 && x < W - 14 && y > top && y < H - bottom;
      const o = inside ? Math.round(clamp(vis(it.hs, x)) * 100) / 100 : 0;
      if (Math.abs(x - it.x) + Math.abs(y - it.y) > 0.2) {
        it.x = x;
        it.y = y;
        it.el.style.transform = `translate3d(${x.toFixed(1)}px,${y.toFixed(1)}px,0)`;
      }
      if (o !== it.o) {
        if ((o > 0.05) !== (it.o > 0.05)) {
          it.el.classList.toggle("v-on", o > 0.05);
          it.el.tabIndex = o > 0.05 ? 0 : -1;
        }
        it.o = o;
        it.el.style.opacity = String(o);
      }
      const flip = x > W * 0.58;
      if (flip !== it.flip) it.el.classList.toggle("v-hs--l", (it.flip = flip));
    }
  }
}

/** Month + year in the numeric face; each part rolls like an odometer when it changes. */
class Odometer {
  constructor(/** @type {string} */ total) {
    this.total = total;
    this.count = h("span", { class: "v-odo__count v-label" });
    this.month = h("span", { class: "v-wheel" });
    this.year = h("span", { class: "v-wheel" });
    this.note = h("p", { class: "v-odo__note" });
    this.el = h("div", { class: "v-odo", "aria-hidden": "true" }, [
      this.count,
      h("div", { class: "v-odo__date" }, [this.month, this.year]),
      this.note,
    ]);
    this.label = "";
  }

  set(/** @type {string} */ label, /** @type {number} */ index, /** @type {string} */ note, dir = 1) {
    if (label === this.label) return;
    const [m, y] = splitLabel(label);
    const [m0, y0] = splitLabel(this.label);
    const x0 = this.year.offsetLeft;
    this.label = label;
    this.count.textContent = `${pad(index + 1)} / ${this.total}`;
    this.note.textContent = note;
    if (m !== m0) roll(this.month, m, dir);
    if (y !== y0) roll(this.year, y, dir);
    const dx = x0 - this.year.offsetLeft;
    if (m0 && dx && !reduced) this.year.animate([{ transform: `translateX(${dx}px)` }, { transform: "none" }], { duration: 420, easing: "cubic-bezier(.2,.7,.2,1)" });
  }
}

/** Replace a wheel's text: the old value slides out, the new one in (direction follows time). */
function roll(/** @type {HTMLElement} */ wheel, /** @type {string} */ text, /** @type {number} */ dir) {
  const olds = [...wheel.children];
  const next = h("span", { text });
  wheel.append(next);
  for (const old of olds) {
    if (reduced) {
      old.remove();
      continue;
    }
    old.classList.add("v-out");
    old.animate([{ transform: "none", opacity: 1 }, { transform: `translateY(${-70 * dir}%)`, opacity: 0 }], { duration: 380, easing: "cubic-bezier(.2,.7,.2,1)", fill: "forwards" }).onfinish = () => old.remove();
  }
  if (olds.length && !reduced) next.animate([{ transform: `translateY(${70 * dir}%)`, opacity: 0 }, { transform: "none", opacity: 1 }], { duration: 420, easing: "cubic-bezier(.2,.7,.2,1)" });
}

/** "Apr 2025" from an ISO date. */
const shortDate = (/** @type {string} */ iso) => `${MONTHS[+iso.slice(5, 7) - 1].slice(0, 3)} ${iso.slice(0, 4)}`;

/**
 * Ticks for capture dates at their real-time positions (--f = 0..1), labelled at the first and last
 * capture and at each new year that has room. Returns position(iso) → 0..1.
 */
function dateTicks(/** @type {HTMLElement} */ el, /** @type {any[]} */ caps) {
  const d0 = day(caps[0].date);
  const span = Math.max(1, day(caps[caps.length - 1].date) - d0);
  const f = (/** @type {string} */ iso) => clamp((day(iso) - d0) / span);
  for (const c of caps) el.append(h("span", { class: "v-tick", style: `--f:${f(c.date).toFixed(4)}` }));
  const label = (/** @type {number} */ x, /** @type {string} */ text, cls = "") =>
    el.append(h("span", { class: `v-tick__label ${cls}`, style: `--f:${x.toFixed(4)}`, text }));
  label(0, shortDate(caps[0].date), "v-first");
  label(1, shortDate(caps[caps.length - 1].date), "v-last");
  for (let y = +caps[0].date.slice(0, 4) + 1; y <= +caps[caps.length - 1].date.slice(0, 4); y++) {
    const x = f(`${y}-01-01`);
    el.append(h("span", { class: "v-tick v-tick--year", style: `--f:${x.toFixed(4)}` }));
    if (x > 0.2 && x < 0.8) label(x, String(y), "v-year");
  }
  return f;
}
