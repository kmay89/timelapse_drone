// @ts-check
/* COMPARE: two aligned captures in a sticky stage. "curtain": the after layer is clipped at the split;
 * scrolling sweeps the split through the steps' values until the reader drags or taps (then the reader
 * owns it until the chapter is left). "blink": press and hold to see before, release for after. The
 * pictures are the pre-rendered <picture> elements, moved into the stage, so nothing is fetched twice. */

/** @returns {Component | null} */
function compare(/** @type {HTMLElement} */ sec, /** @type {any} */ ch) {
  const v = VANTAGES[ch.vantage];
  const sides = [v.captures[ch.before], v.captures[ch.after]];
  const pics = $$(".v-pair .v-media", sec).map((m) => m.firstElementChild);
  if (sides.some((c) => isPlaceholder(c.img)) || pics.length !== 2) return null;
  const blink = ch.mode === "blink";
  const labels = [ch.beforeLabel, ch.afterLabel];
  const steps = ch.steps.length ? ch.steps : [{ html: "" }];
  const hold = 0.5;
  const V = steps.length - 1 + hold;
  const wins = holdWindows(steps.map((/** @type {any} */ _, /** @type {number} */ k) => k), hold);
  let last = 0.5;
  const splits = steps.map((/** @type {any} */ st) => (last = st.split ?? last));

  const layers = pics.map((pic, k) => {
    const img = pic.tagName === "IMG" ? pic : pic.querySelector("img");
    img.loading = "eager";
    const box = h("div", { class: "v-cmp__img" }, [pic]);
    // The browser takes `sizes` from the chosen <source>, so all of them follow the stage.
    const sized = [img, ...$$("source", pic)];
    return { el: h("div", { class: `v-cmp__layer${k ? " v-cmp__after" : ""}` }, [box]), box, sized };
  });
  const tags = sides.map((c, k) =>
    h("span", { class: `v-cmp__tag v-label v-cmp__tag--${k ? "a" : "b"}` }, [
      h("b", { text: k ? "After" : "Before" }),
      h("time", { datetime: c.date, text: labels[k] }),
    ]),
  );
  const cards = new Cards(steps.map((/** @type {any} */ st) => st.html));
  const layer = h("div", { class: "v-hs-layer" });
  const pins = new Pins(layer, ch.hotspots, (hs, x) => {
    const img = layers[+!(blink ? before : x < clamp(split + nudge) * W)].sized[0]; // the side the pin is on
    return img.naturalWidth ? crop({ src: img, w: img.naturalWidth, h: img.naturalHeight }, hs.x, hs.y) : null;
  });
  const stage = h("div", { class: `v-stage v-cmp${blink ? " v-cmp--blink" : ""}` }, [
    ...layers.map((l) => l.el),
    h("div", { class: "v-scrim" }),
    layer,
    cards.el,
  ]);
  const track = h("div", { class: "v-track", style: `--len:${100 + 90 * steps.length}` }, [stage]);
  sec.append(track);
  sec.classList.add("v-live", "v-pinned");

  let W = 0;
  let H = 0;
  let trackH = 0;
  let w0 = 1;
  /** @type {number[][]} */
  let cams = [];
  let split = splits[0];
  let manual = false;
  let nudged = false;
  let nudge = 0;
  let before = false; // blink: showing the before side
  let drawn = "";
  let valueNow = -1;
  let tagW = [0, 0];
  let top = 0; // pins hide above this line (the date tags)
  let onScreen = false;
  let blinkLoop = () => {};
  const after = layers[1].el;

  /* Curtain: handle (1px rule + 44pt grip, a role=slider), dates riding on either side. The slider has
   * its value from the start, before the chapter's first frame. */
  const valueText = (/** @type {number} */ n) => `${100 - n}% ${labels[1]}, ${n}% ${labels[0]}`;
  const n0 = Math.round(clamp(splits[0]) * 100);
  const grip = h("div", {
    class: "v-cmp__grip",
    role: "slider",
    tabindex: "0",
    "aria-label": `Divider between ${labels[0]} and ${labels[1]}`,
    "aria-valuemin": "0",
    "aria-valuemax": "100",
    "aria-valuenow": String(n0),
    "aria-valuetext": valueText(n0),
  }, [icon("grip")]);
  const handle = h("div", { class: "v-cmp__handle" }, [h("span", { class: "v-cmp__rule" }), ...tags, grip]);
  const setSplit = (/** @type {number} */ x) => {
    split = clamp(x);
    manual = true;
    kick();
  };
  const at = (/** @type {PointerEvent} */ e) => setSplit((e.clientX - stage.getBoundingClientRect().left) / W);

  if (!blink) {
    stage.append(handle);
    hdrag(stage, {
      down: (e) => !/** @type {HTMLElement} */ (e.target).closest(".v-hs, .v-card"),
      move: at,
      tap: at, // no drag-only control (WCAG 2.5.7)
    });
    grip.addEventListener("keydown", (/** @type {KeyboardEvent} */ e) => {
      const d = { ArrowLeft: -0.02, ArrowDown: -0.02, ArrowRight: 0.02, ArrowUp: 0.02, PageDown: -0.1, PageUp: 0.1 }[e.key];
      const to = e.key === "Home" ? 0 : e.key === "End" ? 1 : d === undefined ? null : split + d;
      if (to === null) return;
      e.preventDefault();
      setSplit(to);
    });
  } else {
    /* Blink: press and hold anywhere, or the toggle; optional auto-blink (off by default). */
    const toggle = h("button", { class: "v-btn v-btn--text", type: "button", "aria-pressed": "false", text: `Show ${labels[0]}` });
    const auto = h("button", { class: "v-btn", type: "button", "aria-label": "Blink automatically", "aria-pressed": "false" }, [icon("play")]);
    const show = (/** @type {boolean} */ b) => {
      before = b;
      toggle.setAttribute("aria-pressed", String(b));
      kick();
    };
    let timer = 0;
    let held = false;
    let blinking = false;
    stage.addEventListener("pointerdown", (e) => {
      if (/** @type {HTMLElement} */ (e.target).closest("button, .v-card")) return;
      timer = window.setTimeout(() => show((held = true)), 90);
    });
    for (const type of ["pointerup", "pointercancel", "pointerleave"])
      stage.addEventListener(type, () => {
        clearTimeout(timer);
        if (held) show((held = false));
      });
    toggle.addEventListener("click", () => show(!before));
    /* The auto-blink loop runs only while the chapter is on screen and the tab visible (the frame loop
     * sleeps otherwise); the reader's choice stands, so it picks up again on return. */
    let looping = false;
    blinkLoop = () => {
      if (looping || !blinking || reduced || !onScreen || doc.hidden) return;
      looping = true;
      let t0 = 0;
      every((now) => {
        if (!blinking || reduced || !onScreen || doc.hidden) return (looping = false);
        t0 = t0 || now;
        const b = Math.floor((now - t0) / 700) % 2 === 1;
        if (b !== before) show(b);
        return true;
      });
    };
    doc.addEventListener("visibilitychange", blinkLoop);
    auto.addEventListener("click", () => {
      blinking = !blinking;
      auto.setAttribute("aria-pressed", String(blinking));
      auto.replaceChildren(icon(blinking ? "pause" : "play"));
      blinkLoop();
    });
    stage.append(h("div", { class: "v-cmp__bar" }, [toggle, !reduced && auto]), tags[0], tags[1]);
  }

  const vis = (/** @type {any} */ hs, /** @type {number} */ k) => +(hs.from <= k && k <= hs.to);
  return {
    resize() {
      W = stage.clientWidth;
      H = stage.clientHeight;
      trackH = track.offsetHeight;
      cams = steps.map((/** @type {any} */ st) => st.focus || homeCam(v, W, H));
      const home = frameRect(W, H, v.aspect, [0.5, 0.5, 1]);
      w0 = home.w;
      const widest = Math.max(...cams.map((c) => frameRect(W, H, v.aspect, c).w));
      for (const l of layers) {
        l.box.style.width = `${home.w}px`;
        l.box.style.height = `${home.h}px`;
        for (const el of l.sized) el.sizes = `${Math.ceil(widest)}px`;
      }
      tagW = tags.map((t) => t.offsetWidth);
      pins.measure();
      const box = stage.getBoundingClientRect();
      cards.measure(box);
      top = Math.max(...tags.map((t) => t.getBoundingClientRect().bottom)) - box.top + 8;
      drawn = "";
    },
    enter() {
      manual = false;
      onScreen = true;
      blinkLoop();
    },
    leave() {
      onScreen = false;
    },
    measure: () => pinProgress(track, trackH, H),
    update(p) {
      const x = p * V;
      if (!manual) split = keyed(wins, splits, x, lerp);
      if (!nudged && !blink && p > 0.002 && !reduced) {
        nudged = true;
        const dir = split > 0.85 ? -1 : 1;
        tween(900, (t) => {
          nudge = t < 1 ? 0.08 * dir * Math.sin(Math.PI * t) : 0;
          kick(); // tweens run after the updates: one more frame draws this value (the last one included)
        });
      }
      const r = frameRect(W, H, v.aspect, steps.length ? keyed(wins, cams, x, mixCam) : homeCam(v, W, H));
      const X = clamp(split + nudge) * W;
      const key = `${r.x.toFixed(1)},${r.y.toFixed(1)},${r.w.toFixed(1)},${X.toFixed(1)},${before}`;
      if (key !== drawn) {
        drawn = key;
        const tf = `translate3d(${r.x.toFixed(1)}px,${r.y.toFixed(1)}px,0) scale(${(r.w / w0).toFixed(4)})`;
        for (const l of layers) l.box.style.transform = tf;
        if (blink) {
          after.style.opacity = before ? "0" : "1";
          tags[0].classList.toggle("v-on", before);
          tags[1].classList.toggle("v-on", !before);
        } else {
          after.style.clipPath = `inset(0 0 0 ${X.toFixed(1)}px)`;
          handle.style.transform = `translate3d(${X.toFixed(1)}px,0,0)`;
          tags[0].style.opacity = String(clamp((X - 18 - tagW[0]) / 20));
          tags[1].style.opacity = String(clamp((W - X - 18 - tagW[1]) / 20));
        }
      }
      const now = Math.round(clamp(split) * 100);
      if (!blink && now !== valueNow) {
        valueNow = now;
        grip.setAttribute("aria-valuenow", String(now));
        grip.setAttribute("aria-valuetext", valueText(now));
      }
      cards.set(cardAlphas(wins, x)); // a step without text has an empty card, never shown
      const side = (/** @type {number} */ px) => px < X;
      const off = (/** @type {number} */ px) => (blink ? 1 : clamp((Math.abs(px - X) - 14) / 22)); // clear of the rule
      pins.place(r, W, H, (hs, px) => vis(hs, (blink ? before : side(px)) ? ch.before : ch.after) * off(px), top, 24, cards, blink ? undefined : side);
      return false;
    },
  };
}
