// @ts-check
/* SCRUB: one vantage flown through time as you scroll. A sticky 100svh stage holds a canvas that
 * cross-fades decoded captures (BLUEPRINT 5.3: each capture holds still for `hold` of its scroll
 * segment, then dissolves with a smoothstep), eased camera moves between the steps' focus, cards for
 * the steps, pinned hotspots, a date odometer and a calendar rail with ticks at the real dates. */

const NIGHT = getComputedStyle(root).getPropertyValue("--v-night").trim() || "#000";

/** @returns {Component | null} */
function scrub(/** @type {HTMLElement} */ sec, /** @type {any} */ ch) {
  const v = VANTAGES[ch.vantage];
  /** @type {number[]} */
  const seq = []; // capture indices with a real image (lite editions keep a few)
  for (let i = ch.from; i <= ch.to; i++) if (!isPlaceholder(v.captures[i].img)) seq.push(i);
  if (seq.length < 2) return null;
  const caps = seq.map((i) => v.captures[i]);
  const imgs = caps.map((c) => c.img);
  const days = caps.map((c) => day(c.date));
  const n = seq.length;
  const hold = clamp(ch.hold ?? 0.55, 0, 0.9);
  const V = n - 1 + hold; // every capture, the last included, gets a still hold
  const posOf = (/** @type {number} */ c) => Math.max(0, seq.filter((i) => i <= c).length - 1);
  let prev = ch.from;
  const steps = ch.steps
    .map((/** @type {any} */ s) => ({ ...s, k: posOf((prev = s.capture ?? prev)) }))
    .sort((/** @type {any} */ a, /** @type {any} */ b) => a.k - b.k);
  const wins = holdWindows(steps.map((/** @type {any} */ s) => s.k), hold);
  const inRange = (/** @type {any} */ hs, /** @type {number} */ k) => hs.from <= seq[k] && seq[k] <= hs.to;

  const canvas = h("canvas", { class: "v-canvas", role: "img", "aria-label": `${v.name}, ${caps[0].label}` });
  const ctx = canvas.getContext("2d", { alpha: false });
  ctx.imageSmoothingQuality = "high";
  const odo = new Odometer(pad(n));
  const cards = new Cards(steps.map((/** @type {any} */ s) => s.html));
  const layer = h("div", { class: "v-hs-layer" });
  const pins = new Pins(layer, ch.hotspots);
  const ticks = h("div", { class: "v-rail__ticks" });
  const fill = h("div", { class: "v-rail__fill" });
  const knob = h("div", { class: "v-rail__knob" });
  const rail = h(
    "div",
    { class: "v-rail", role: "slider", tabindex: "0", "aria-label": "Flight date", "aria-valuemin": "1", "aria-valuemax": String(n) },
    [h("div", { class: "v-rail__track" }, [ticks, fill, knob])],
  );
  const where = dateTicks(ticks, caps);
  const tickEls = $$(".v-tick:not(.v-tick--year)", ticks);
  odo.set(caps[0].label, 0, "");
  const live = liveRegion();
  const stage = h("div", { class: "v-stage" }, [canvas, h("div", { class: "v-scrim" }), layer, odo.el, cards.el, rail, live]);
  const track = h("div", { class: "v-track", style: `--len:${Math.max(ch.scrollVh || 75 * n, 200)}` }, [stage]);
  sec.append(track);
  sec.classList.add("v-live", "v-pinned");

  let W = 0;
  let H = 0;
  let s = 1;
  let trackH = 0;
  let railW = 0;
  let need = 0;
  let top = 0;
  let bottom = 0;
  /** @type {number[][]} */
  let cams = [];
  /** @type {DOMRect[]} */
  let cardBoxes = [];
  let dirty = true;
  let drawn = "";
  let cur = -1;
  let range = "";
  let railF = -1;
  let shown = 0; // reduced motion: the capture faded to, from `faded`, starting at fadeT0
  let faded = 0;
  let fadeT0 = 0;
  const store = new ImageStore(imgs, () => {
    dirty = true;
    kick();
  });
  const camAt = (/** @type {number} */ x) => (steps.length ? keyed(wins, cams, x, mixCam) : homeCam(v, W, H));
  const yFor = (/** @type {number} */ x) => pinScrollY(track, trackH, H, clamp(x / V));
  const holdOf = (/** @type {number} */ k) => k + hold / 2;
  const nearest = (/** @type {number} */ d) =>
    days.reduce((best, x, k) => (Math.abs(x - d) < Math.abs(days[best] - d) ? k : best), 0);
  /** Rail fraction → scroll position (inverting the rail's date interpolation). */
  const xForDate = (/** @type {number} */ d) => {
    const i = days.findIndex((x, k) => k < n - 1 && d < days[k + 1]);
    if (i < 0) return holdOf(n - 1);
    return i + hold + unsmooth((d - days[i]) / (days[i + 1] - days[i])) * (1 - hold);
  };
  const dateAt = (/** @type {PointerEvent} */ e) => {
    const box = ticks.getBoundingClientRect();
    return lerp(days[0], days[n - 1], clamp((e.clientX - box.left) / box.width));
  };

  hdrag(rail, {
    move: (e) => scrollToY(yFor(xForDate(dateAt(e))), false),
    end: (e) => scrollToY(yFor(holdOf(nearest(dateAt(e))))),
    tap: (e) => scrollToY(yFor(holdOf(nearest(dateAt(e))))),
  });
  rail.addEventListener("keydown", (/** @type {KeyboardEvent} */ e) => {
    const to = { ArrowRight: cur + 1, ArrowUp: cur + 1, ArrowLeft: cur - 1, ArrowDown: cur - 1, Home: 0, End: n - 1 }[e.key];
    if (to === undefined) return;
    e.preventDefault();
    scrollToY(yFor(holdOf(clamp(to, 0, n - 1))));
  });

  return {
    resize() {
      W = stage.clientWidth;
      H = stage.clientHeight;
      trackH = track.offsetHeight;
      railW = ticks.clientWidth;
      s = fitCanvas(canvas, W, H);
      pins.measure();
      cams = steps.map((/** @type {any} */ st) => st.focus || homeCam(v, W, H));
      const widest = Math.max(...[homeCam(v, W, H), ...cams].map((c) => frameRect(W, H, v.aspect, c).w));
      need = decodeWidth(imgs[0], widest, s, v.aspect);
      const box = stage.getBoundingClientRect();
      top = odo.date.getBoundingClientRect().bottom - box.top + 8;
      bottom = box.bottom - rail.getBoundingClientRect().top + 8;
      cardBoxes = cards.items.map((it) => {
        const r = it.el.getBoundingClientRect();
        return new DOMRect(r.left - box.left - 8, r.top - box.top - 8, r.width + 16, r.height + 16);
      });
      dirty = true;
      range = "";
      railF = -1;
    },
    warm(on) {
      const k = Math.round(pinProgress(track, trackH, H) * (n - 1)); // arriving from below starts at the end
      if (on) store.want([k, k + 1, k - 1], need);
      else store.clear();
    },
    measure: () => pinProgress(track, trackH, H),
    update(p, now) {
      const x = p * V;
      const i = Math.min(Math.floor(x), n - 1);
      let lo = i;
      let hi = Math.min(i + 1, n - 1);
      let a = i === n - 1 ? 0 : smooth(clamp((x - i - hold) / (1 - hold)));
      let more = false;
      if (reduced) {
        const target = a >= 0.5 ? hi : lo;
        if (target !== shown) {
          faded = shown;
          shown = target;
          fadeT0 = now;
        }
        lo = faded;
        hi = shown;
        a = clamp((now - fadeT0) / 200);
        more = a < 1;
      }
      const k = a >= 0.5 ? hi : lo;
      const cam = camAt(x);
      const r = frameRect(W, H, v.aspect, cam);

      if (`${lo},${hi}` !== range) {
        range = `${lo},${hi}`;
        store.want([lo, hi, hi + 1, lo - 1, hi + 2], need);
      }
      const key = `${lo},${hi},${a.toFixed(3)},${r.x.toFixed(1)},${r.y.toFixed(1)},${r.w.toFixed(1)},${lqipGen}`;
      if (dirty || key !== drawn) {
        drawn = key;
        dirty = false;
        ctx.globalAlpha = 1;
        ctx.fillStyle = imgs[k].color || NIGHT;
        ctx.fillRect(0, 0, canvas.width, canvas.height);
        paint(ctx, store.get(lo) || lqip(imgs[lo]), r, W, H, s);
        if (hi !== lo) paint(ctx, store.get(hi) || lqip(imgs[hi]), r, W, H, s, a);
      }

      const alphas = cardAlphas(wins, x).map((o) => (reduced ? +(o >= 0.5) : o));
      cards.set(alphas);
      const shade = String(1 - Math.max(0, ...alphas));
      if (odo.note.style.opacity !== shade) odo.note.style.opacity = shade;
      const covered = cardBoxes.filter((_, j) => alphas[j] > 0.1);
      pins.place(r, W, H, (hs, px) => {
        const py = r.y + hs.y * r.h;
        if (covered.some((b) => px > b.left && px < b.right && py > b.top && py < b.bottom)) return 0;
        return (inRange(hs, lo) ? 1 - a : 0) + (inRange(hs, hi) && hi !== lo ? a : 0);
      }, top, bottom);

      const f = where(caps[lo].date) + (where(caps[hi].date) - where(caps[lo].date)) * a;
      if (f !== railF) {
        railF = f;
        knob.style.transform = `translate3d(${(f * railW).toFixed(1)}px,0,0)`;
        fill.style.transform = `scaleX(${f.toFixed(4)})`;
      }
      if (k !== cur) {
        const dir = k < cur ? -1 : 1;
        cur = k;
        const step = steps.find((/** @type {any} */ st) => st.k === k);
        odo.set(caps[k].label, k, step ? "" : caps[k].note || "", dir);
        tickEls.forEach((t, j) => t.classList.toggle("v-past", j <= k));
        rail.setAttribute("aria-valuenow", String(k + 1));
        rail.setAttribute("aria-valuetext", caps[k].label);
        const text = `${v.name}, ${caps[k].label}: ${step ? plain(step.html) : caps[k].note || ""}`;
        canvas.setAttribute("aria-label", text);
        live.say(text);
      }
      return more;
    },
  };
}
