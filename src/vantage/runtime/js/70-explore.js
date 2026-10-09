// @ts-check
/* EXPLORE: the "time machine". Pick a vantage, drag a date scrubber whose ticks sit at the real flight
 * dates (it dissolves continuously while dragging and settles on a flight when released), play the
 * flights in order, or compare any two flights with a curtain or a blink. Pinch, double-tap and drag
 * zoom and pan inside the stage only; a vertical swipe still scrolls the page until you zoom in. */

/** @type {{has(id: string): boolean, show(id: string, i: number): void}[]} */
const explorers = [];

/** @returns {Component | null} */
function explore(/** @type {HTMLElement} */ sec, /** @type {any} */ ch) {
  const real = (/** @type {any} */ vt) => vt.captures.map((/** @type {any} */ _, /** @type {number} */ i) => i).filter((/** @type {number} */ i) => !isPlaceholder(vt.captures[i].img));
  const ids = ch.vantages.filter((/** @type {string} */ id) => VANTAGES[id] && real(VANTAGES[id]).length > 1);
  if (!ids.length) return null;

  const canvas = h("canvas", { class: "v-canvas", role: "img" });
  const ctx = canvas.getContext("2d", { alpha: false });
  ctx.imageSmoothingQuality = "high";
  const reset = h("button", { class: "v-btn v-x__reset", type: "button", "aria-label": "Reset zoom", hidden: true }, [icon("reset")]);
  const tags = [0, 1].map((k) => h("span", { class: `v-cmp__tag v-label v-cmp__tag--${k ? "a" : "b"}` }));
  const grip = h("div", { class: "v-cmp__grip", role: "slider", tabindex: "0", "aria-label": "Divider", "aria-valuemin": "0", "aria-valuemax": "100" }, [icon("grip")]);
  const handle = h("div", { class: "v-cmp__handle" }, [h("span", { class: "v-cmp__rule" }), grip]);
  const stage = h("div", { class: "v-x__stage" }, [canvas, h("div", { class: "v-scrim" }), handle, ...tags, reset]);
  const play = h("button", { class: "v-btn v-x__play", type: "button", "aria-label": "Play the flights in order" }, [icon("play")]);
  const input = h("input", { class: "v-x__input", type: "range", min: "0", step: "1", "aria-label": "Flight date" });
  const ticks = h("div", { class: "v-x__ticks", "aria-hidden": "true" });
  const selects = ["Before", "After"].map((t) => h("select", { class: "v-x__select", "aria-label": `${t} flight` }));
  const modes = { single: "Time", curtain: "Curtain", blink: "Blink" };
  const modeBtns = Object.entries(modes).map(([m, t]) => h("button", { class: "v-seg__btn", type: "button", "data-mode": m, text: t }));
  const chips = ids.map((id) => h("button", { class: "v-chip", type: "button", "data-id": id, text: VANTAGES[id].name }));
  const odo = new Odometer("");
  stage.append(odo.el);
  const ui = h("div", { class: "v-x" }, [
    ids.length > 1 && h("div", { class: "v-x__chips", role: "group", "aria-label": "Vantage point" }, chips),
    stage,
    h("div", { class: "v-x__bar" }, [
      h("div", { class: "v-x__time" }, [play, h("div", { class: "v-x__range" }, [ticks, input])]),
      h("div", { class: "v-x__pick" }, selects.map((s, k) => h("label", {}, [h("span", { class: "v-label", text: k ? "After" : "Before" }), s]))),
      h("div", { class: "v-seg", role: "group", "aria-label": "Mode" }, modeBtns),
    ]),
  ]);
  const head = $(".v-head", sec);
  if (head) head.after(ui);
  else $(".v-flow", sec).prepend(ui);
  sec.classList.add("v-live");

  let v = VANTAGES[ids[0]];
  /** @type {number[]} */
  let seq = [];
  /** @type {any[]} */
  let caps = [];
  /** @type {number[]} */
  let days = [];
  /** @type {ImageStore} */
  let store;
  let where = (/** @type {string} */ _) => 0;
  let mode = "single";
  let u = 0; // continuous position in seq (single mode)
  let pick = [0, 0];
  let split = 0.5;
  let showA = false;
  let cx = 0.5;
  let cy = 0.5;
  let z = 1;
  let W = 0;
  let H = 0;
  let s = 1;
  let bw = 0;
  let bh = 0;
  let need = 0;
  let dirty = true;
  let warm = false;
  let playing = false;
  let playT0 = 0;
  let cur = -1;
  const rect = () => place(W, H, bw * z, bh * z, cx, cy);
  const redraw = () => {
    dirty = true;
    kick();
  };

  function setVantage(/** @type {string} */ id) {
    v = VANTAGES[id];
    seq = real(v);
    caps = seq.map((i) => v.captures[i]);
    days = caps.map((c) => day(c.date));
    store?.clear();
    store = new ImageStore(caps.map((c) => c.img), redraw);
    ticks.replaceChildren();
    where = dateTicks(ticks, caps);
    input.max = String(days[days.length - 1] - days[0]);
    for (const sel of selects) sel.replaceChildren(...caps.map((c, k) => h("option", { value: String(k), text: c.label })));
    pick = [0, seq.length - 1];
    selects.forEach((sel, k) => (sel.value = String(pick[k])));
    odo.total = pad(seq.length);
    odo.label = "";
    chips.forEach((c) => c.setAttribute("aria-pressed", String(c.dataset.id === id)));
    canvas.setAttribute("aria-label", v.name);
    ui.style.setProperty("--ar", String(v.aspect));
    u = seq.length - 1;
    cx = cy = 0.5;
    z = 1;
    cur = -1;
    if (W) sizes();
    sync();
  }

  function sizes() {
    W = stage.clientWidth;
    H = stage.clientHeight;
    s = fitCanvas(canvas, W, H);
    const home = homeCam(v, W, H);
    const r = frameRect(W, H, v.aspect, home);
    [bw, bh, cx, cy] = [r.w, r.h, home[0], home[1]];
    need = decodeWidth(caps[0].img, bw * 2.5, s, v.aspect);
    redraw();
  }

  /** Re-centre after a pan/zoom so the stored centre is the clamped one. */
  function settle() {
    const r = rect();
    cx = (W / 2 - r.x) / r.w;
    cy = (H / 2 - r.y) / r.h;
    reset.hidden = z < 1.01;
    stage.classList.toggle("v-zoomed", z > 1.01);
    redraw();
  }

  function zoomAt(/** @type {number} */ z1, /** @type {number} */ px, /** @type {number} */ py, anchor = rect()) {
    const ax = (px - anchor.x) / anchor.w;
    const ay = (py - anchor.y) / anchor.h;
    z = clamp(z1, 1, 5);
    cx = (W / 2 - px) / (bw * z) + ax;
    cy = (H / 2 - py) / (bh * z) + ay;
    settle();
  }

  /** Position (continuous seq index) for a date: linear between the flights around it. */
  const uForDay = (/** @type {number} */ d) => {
    const i = days.findIndex((x, k) => k === days.length - 1 || d < days[k + 1]);
    return i === days.length - 1 ? i : i + clamp((d - days[i]) / (days[i + 1] - days[i]));
  };
  const goTo = (/** @type {number} */ k) => {
    const u0 = u;
    tween(320, (t) => {
      u = lerp(u0, k, easeOut(t));
      syncInput();
    });
  };

  /** Mirror state into the controls: the scrubber (every play frame), then mode buttons. */
  function syncInput() {
    input.value = String(Math.round(lerp(days[Math.floor(u)], days[Math.ceil(u)], u % 1) - days[0]));
    input.setAttribute("aria-valuetext", caps[Math.round(u)].label);
    redraw();
  }
  function sync() {
    syncInput();
    ui.dataset.mode = mode;
    modeBtns.forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.mode === mode)));
  }

  function setPlaying(/** @type {boolean} */ on) {
    playing = on && mode === "single";
    play.replaceChildren(icon(playing ? "pause" : "play"));
    play.setAttribute("aria-label", playing ? "Pause" : "Play the flights in order");
    if (playing) {
      if (u >= seq.length - 1) u = 0;
      u = Math.floor(u);
      playT0 = 0;
      kick();
    }
  }

  /* Controls */
  chips.forEach((c) => c.addEventListener("click", () => setVantage(c.dataset.id)));
  modeBtns.forEach((b) =>
    b.addEventListener("click", () => {
      mode = b.dataset.mode;
      setPlaying(false);
      sync();
    }),
  );
  selects.forEach((sel, k) =>
    sel.addEventListener("change", () => {
      pick[k] = +sel.value;
      redraw();
    }),
  );
  play.addEventListener("click", () => setPlaying(!playing));
  input.addEventListener("input", () => {
    setPlaying(false);
    u = uForDay(days[0] + +input.value);
    redraw();
  });
  input.addEventListener("change", () => goTo(Math.round(u)));
  input.addEventListener("keydown", (/** @type {KeyboardEvent} */ e) => {
    const k = Math.round(u);
    const to = { ArrowRight: k + 1, ArrowUp: k + 1, ArrowLeft: k - 1, ArrowDown: k - 1, Home: 0, End: seq.length - 1, PageUp: k + 3, PageDown: k - 3 }[e.key];
    if (to === undefined) return;
    e.preventDefault();
    setPlaying(false);
    goTo(clamp(to, 0, seq.length - 1));
  });
  reset.addEventListener("click", () => {
    const [z0, x0, y0] = [z, cx, cy];
    const home = homeCam(v, W, H);
    tween(300, (t) => {
      z = lerp(z0, 1, easeOut(t));
      cx = lerp(x0, home[0], easeOut(t));
      cy = lerp(y0, home[1], easeOut(t));
      settle();
    });
  });
  grip.addEventListener("keydown", (/** @type {KeyboardEvent} */ e) => {
    const d = { ArrowLeft: -0.02, ArrowDown: -0.02, ArrowRight: 0.02, ArrowUp: 0.02, PageDown: -0.1, PageUp: 0.1 }[e.key];
    if (d === undefined && e.key !== "Home" && e.key !== "End") return;
    e.preventDefault();
    split = e.key === "Home" ? 0 : e.key === "End" ? 1 : clamp(split + /** @type {number} */ (d));
    redraw();
  });

  /* Gestures inside the stage: pinch / drag-pan / curtain drag / blink hold / double-tap zoom. */
  /** @type {Map<number, {x: number, y: number, x0: number, y0: number}>} */
  const pts = new Map();
  /** @type {any} */
  let g = null;
  let box = new DOMRect();
  let holdTimer = 0;
  let lastTap = { t: 0, x: 0, y: 0 };
  const local = (/** @type {{x: number, y: number}} */ p) => [p.x - box.left, p.y - box.top];
  stage.addEventListener("pointerdown", (/** @type {PointerEvent} */ e) => {
    if (/** @type {HTMLElement} */ (e.target).closest("button") || e.button > 0) return;
    box = stage.getBoundingClientRect();
    pts.set(e.pointerId, { x: e.clientX, y: e.clientY, x0: e.clientX, y0: e.clientY });
    if (pts.size === 2) {
      const [a, b] = [...pts.values()];
      for (const id of pts.keys()) stage.setPointerCapture(id);
      clearTimeout(holdTimer);
      g = { kind: "pinch", d0: Math.hypot(a.x - b.x, a.y - b.y), z0: z, anchor: rect(), mid: local({ x: (a.x + b.x) / 2, y: (a.y + b.y) / 2 }) };
    } else if (pts.size === 1) {
      g = { kind: "pending" };
      if (mode === "blink") holdTimer = window.setTimeout(() => ((showA = true), redraw()), 120);
    }
  });
  stage.addEventListener("pointermove", (/** @type {PointerEvent} */ e) => {
    const p = pts.get(e.pointerId);
    if (!p || !g) return;
    p.x = e.clientX;
    p.y = e.clientY;
    if (g.kind === "pinch" && pts.size === 2) {
      const [a, b] = [...pts.values()];
      const [mx, my] = local({ x: (a.x + b.x) / 2, y: (a.y + b.y) / 2 });
      const r0 = g.anchor;
      const shifted = { x: r0.x + mx - g.mid[0], y: r0.y + my - g.mid[1], w: r0.w, h: r0.h };
      zoomAt((g.z0 * Math.hypot(a.x - b.x, a.y - b.y)) / g.d0, mx, my, shifted);
      return;
    }
    const dx = p.x - p.x0;
    const dy = p.y - p.y0;
    if (g.kind === "pending" && Math.hypot(dx, dy) > 8 && (z > 1.01 || Math.abs(dx) > 1.7 * Math.abs(dy))) {
      clearTimeout(holdTimer);
      stage.setPointerCapture(e.pointerId);
      const nearHandle = Math.abs(local(p)[0] - dx - split * W) < 36;
      g = mode === "curtain" && (z <= 1.01 || nearHandle) ? { kind: "split" } : { kind: "pan", cx, cy, r: rect() };
    }
    if (g.kind === "split") {
      split = clamp(local(p)[0] / W);
      redraw();
    } else if (g.kind === "pan") {
      cx = g.cx - dx / g.r.w;
      cy = g.cy - dy / g.r.h;
      settle();
    }
  });
  const up = (/** @type {PointerEvent} */ e) => {
    const p = pts.get(e.pointerId);
    if (!p) return;
    pts.delete(e.pointerId);
    clearTimeout(holdTimer);
    if (showA) {
      showA = false;
      redraw();
    }
    if (g?.kind === "pending" && e.type === "pointerup") {
      const [x, y] = local(p);
      if (e.timeStamp - lastTap.t < 320 && Math.hypot(x - lastTap.x, y - lastTap.y) < 30) {
        const z0 = z;
        tween(280, (t) => zoomAt(lerp(z0, z0 > 1.2 ? 1 : 2.5, easeOut(t)), x, y));
        lastTap.t = 0;
      } else lastTap = { t: e.timeStamp, x, y };
    }
    if (!pts.size) g = null;
  };
  stage.addEventListener("pointerup", up);
  stage.addEventListener("pointercancel", up);
  stage.addEventListener(
    "wheel",
    (/** @type {WheelEvent} */ e) => {
      if (!e.ctrlKey) return; // trackpad pinch; plain wheel keeps scrolling the page
      e.preventDefault();
      box = stage.getBoundingClientRect();
      zoomAt(z * Math.exp(-e.deltaY / 120), e.clientX - box.left, e.clientY - box.top);
    },
    { passive: false },
  );

  function draw() {
    const r = rect();
    ctx.globalAlpha = 1;
    ctx.fillStyle = NIGHT;
    ctx.fillRect(0, 0, canvas.width, canvas.height);
    const img = (/** @type {number} */ k) => store.get(k) || lqip(caps[k].img);
    if (mode === "single") {
      const lo = Math.floor(u);
      const hi = Math.ceil(u);
      paint(ctx, img(lo), r, W, H, s);
      if (hi !== lo) paint(ctx, img(hi), r, W, H, s, u - lo);
    } else if (mode === "blink") paint(ctx, img(pick[showA ? 0 : 1]), r, W, H, s);
    else {
      const X = split * W;
      paint(ctx, img(pick[0]), r, W, H, s);
      ctx.save();
      ctx.beginPath();
      ctx.rect(X * s, 0, canvas.width, canvas.height);
      ctx.clip();
      paint(ctx, img(pick[1]), r, W, H, s);
      ctx.restore();
      handle.style.transform = `translate3d(${X.toFixed(1)}px,0,0)`;
      grip.setAttribute("aria-valuenow", String(Math.round(split * 100)));
      grip.setAttribute("aria-valuetext", `${Math.round((1 - split) * 100)}% ${caps[pick[1]].label}, ${Math.round(split * 100)}% ${caps[pick[0]].label}`);
    }
    tags.forEach((t, k) => {
      t.replaceChildren(h("b", { text: k ? "After" : "Before" }), caps[pick[k]].label);
      t.classList.toggle("v-on", mode === "curtain" || (mode === "blink" && showA === !k));
    });
  }

  const api = {
    has: (/** @type {string} */ id) => ids.includes(id),
    show(/** @type {string} */ id, /** @type {number} */ i) {
      if (v.id !== id) setVantage(id);
      mode = "single";
      u = Math.max(0, seq.filter((x) => x <= i).length - 1);
      sync();
      stage.scrollIntoView({ behavior: reduced ? "auto" : "smooth", block: "center" });
    },
  };
  explorers.push(api);
  setVantage(ids[0]);

  return {
    resize: sizes,
    warm(on) {
      warm = on;
      if (!on) store.clear();
      redraw();
    },
    update(_, now) {
      if (playing) {
        playT0 = playT0 || now;
        const t = now - playT0;
        const base = Math.floor(u + 1e-6);
        u = base + (reduced ? +(t >= 1400) : smooth(clamp((t - 900) / 500)));
        if (t >= 1400) {
          u = base + 1;
          playT0 = now;
          if (u >= seq.length - 1) setPlaying(false);
        }
        syncInput();
      }
      const k = Math.round(u);
      if (warm) store.want(mode === "single" ? [Math.floor(u), Math.ceil(u), k + 1, k - 1, k + 2] : pick, need);
      const shownK = mode === "single" ? k : pick[mode === "blink" && !showA ? 1 : 0];
      if (shownK !== cur) {
        const dir = shownK < cur ? -1 : 1;
        cur = shownK;
        odo.set(caps[shownK].label, shownK, "", dir);
      }
      if (dirty) {
        dirty = false;
        draw();
      }
      return playing;
    },
  };
}
