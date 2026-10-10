// @ts-check
/* The quieter chapters: stats count up on entry, timeline thumbnails open the flight, galleries get
 * index dots, video chapters play while on screen, and headings rise into place as they arrive. */

/** Run `fn(el)` once, the first time each element is at least `threshold` visible. */
function onceVisible(/** @type {Element[]} */ els, /** @type {(el: any) => void} */ fn, threshold = 0) {
  const obs = new IntersectionObserver(
    (entries) =>
      entries.forEach((e) => {
        if (!e.isIntersecting) return;
        obs.unobserve(e.target);
        fn(e.target);
      }),
    { threshold, rootMargin: "0px 0px -6% 0px" },
  );
  els.forEach((el) => obs.observe(el));
}

/** STATS: "$5.3M", "1,700", "47" count up from zero in 900 ms, keeping their prefix, suffix and format.
 * The count is aria-hidden; a visually hidden copy keeps the real figure for screen readers and copying.
 * Each figure's length (separators count less) lets CSS shrink a long one to its column (--n) instead of
 * breaking it. */
function stats(/** @type {HTMLElement} */ sec) {
  for (const el of $$(".v-stat__value", sec)) {
    const t = el.firstChild?.nodeType === 3 ? el.firstChild.data.trim() : "";
    if (t && !/\s/.test(t)) el.style.setProperty("--n", String(t.length - 0.6 * (t.match(/[,.]/g) || []).length));
  }
  if (reduced) return;
  const items = $$(".v-stat__value", sec).flatMap((el) => {
    const node = el.firstChild;
    const m = node?.nodeType === 3 && /^(\D*?)(\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)(.*)$/s.exec(node.data);
    if (!m) return [];
    const dec = (m[2].split(".")[1] || "").length;
    const fmt = (/** @type {number} */ x) =>
      m[1] + x.toLocaleString("en-US", { minimumFractionDigits: dec, maximumFractionDigits: dec, useGrouping: m[2].includes(",") }) + m[3];
    const span = h("span", { class: "v-count", "aria-hidden": "true", text: node.data });
    node.replaceWith(h("span", { class: "v-sr", text: node.data }), span);
    return [{ el, span, fmt, to: parseFloat(m[2].replace(/,/g, "")) }];
  });
  const below = items.filter((it) => it.el.getBoundingClientRect().top > innerHeight);
  below.forEach((it) => (it.span.style.minWidth = `${it.span.offsetWidth}px`));
  below.forEach((it) => (it.span.textContent = it.fmt(0)));
  let next = 0; // stats arriving together start 110 ms apart
  onceVisible(below.map((it) => it.el), (el) => {
    const it = below.find((x) => x.el === el);
    const wait = Math.max(0, next - performance.now());
    next = performance.now() + wait + 110;
    window.setTimeout(() => tween(900, (t) => (it.span.textContent = it.fmt(it.to * easeOut(t)))), wait);
  }, 0.6);
}

/** A capture in the sheet, with a way into the explore chapter when it covers the vantage. */
function openCapture(/** @type {any} */ v, /** @type {number} */ i, /** @type {HTMLElement} */ from) {
  const c = v.captures[i];
  const path = pickSrc(c.img, Math.min(innerWidth, 900) * Math.min(devicePixelRatio || 1, 2));
  const img = h("img", { class: "v-dlg__img", alt: c.img.alt, width: c.img.w, height: c.img.h });
  img.style.backgroundColor = c.img.color;
  assetURL(path)
    .then((u) => ((img.src = u), img.decode()))
    .catch(() => {})
    .finally(() => releaseAsset(path));
  const ex = explorers.find((x) => x.has(v.id));
  const go = () => {
    closeSheet();
    ex?.show(v.id, i);
  };
  openSheet({
    kicker: v.name,
    title: c.label,
    from,
    wide: true,
    node: h("figure", { class: "v-dlg__fig" }, [
      img,
      c.note && h("figcaption", { text: c.note }),
      ex && h("button", { class: "v-btn v-btn--text v-btn--accent", type: "button", onclick: go }, ["Explore from this flight", icon("arrow")]),
    ]),
  });
}

/** TIMELINE: each thumbnail opens its flight. */
function timeline(/** @type {HTMLElement} */ sec, /** @type {any} */ ch) {
  const rows = $$(".v-tl", sec);
  ch.items.forEach((/** @type {any} */ it, /** @type {number} */ k) => {
    const fig = rows[k] && $(".v-tl__fig", rows[k]);
    if (!fig || !it.vantage || it.capture == null) return;
    const v = VANTAGES[it.vantage];
    const btn = h("button", { class: "v-tl__open", type: "button", "aria-label": `View the ${v.captures[it.capture].label} flight` });
    btn.addEventListener("click", () => openCapture(v, it.capture, btn));
    fig.append(btn);
  });
}

/** GALLERY: index dots under the scroll-snapping filmstrip. */
function gallery(/** @type {HTMLElement} */ sec) {
  const strip = $(".v-strip", sec);
  const items = strip ? $$(":scope > li", strip) : [];
  if (items.length < 2) return;
  let cur = 0;
  const dots = items.map((li, k) =>
    h("button", { class: "v-dot", type: "button", "aria-label": `Image ${k + 1} of ${items.length}`, onclick: () => {
      const dx = li.getBoundingClientRect().left - items[cur].getBoundingClientRect().left;
      strip.scrollBy({ left: dx, behavior: reduced ? "auto" : "smooth" });
    } }),
  );
  const mark = () => {
    const left = strip.getBoundingClientRect().left;
    const offs = items.map((li) => Math.abs(li.getBoundingClientRect().left - left - parseFloat(getComputedStyle(strip).paddingLeft)));
    cur = offs.indexOf(Math.min(...offs));
    dots.forEach((d, k) => d.setAttribute("aria-current", String(k === cur)));
  };
  let queued = false;
  strip.addEventListener("scroll", () => {
    if (queued) return;
    queued = true;
    every(() => {
      queued = false;
      mark();
    });
  }, passive);
  strip.after(h("div", { class: "v-dots", role: "group", "aria-label": "Images" }, dots));
  mark();
}

/** VIDEO: plays (muted, inline) while at least half on screen, unless the reader paused it. The browser's
 * control bar gives way to one round play/pause (the clip is a short silent loop); a tap on the picture
 * toggles too. A player the runtime built (single files) gets its blob as it nears the viewport. */
function videoChapter(/** @type {HTMLElement} */ sec, /** @type {any} */ ch) {
  let el = $("video", sec);
  const media = $(".v-clip .v-media", sec);
  const built = !el && media && ch.video.sources.length;
  if (built) {
    el = buildVideo(ch.video, { loop: ch.loop, width: ch.video.poster.w, height: ch.video.poster.h });
    media.replaceChildren(el);
  }
  if (!el) return;
  el.controls = false;
  const btn = h("button", { class: "v-btn v-round v-clip__btn", type: "button" });
  const sync = () => {
    btn.setAttribute("aria-label", `${el.paused ? "Play" : "Pause"} the video`);
    btn.replaceChildren(icon(el.paused ? "play" : "pause"));
  };
  btn.addEventListener("click", () => (el.paused ? loadVideo(el, ch.video).then(() => tryPlay(el)) : el.pause()));
  el.addEventListener("click", () => btn.click());
  el.after(btn);
  sync();
  let auto = false; // the next "pause" event is ours, not the reader's
  let userPaused = false;
  el.addEventListener("pause", () => {
    userPaused = !auto;
    auto = false;
  });
  el.addEventListener("play", () => (userPaused = false));
  for (const type of ["play", "pause"]) el.addEventListener(type, sync);
  if (built) watchWarm(el, { update() {}, warm: () => loadVideo(el, ch.video) });
  let seen = false;
  new IntersectionObserver(
    (entries) => {
      seen = entries[entries.length - 1].isIntersecting; // a fast fling can batch an arrival and a departure
      if (seen && !userPaused && !reduced) loadVideo(el, ch.video).then(() => seen && tryPlay(el));
      else if (!seen && !el.paused) {
        auto = true;
        el.pause();
      }
    },
    { threshold: 0.5 },
  ).observe(el);
}

/** Headings, quotes, stats and timeline rows rise into place the first time they arrive. */
function reveals() {
  if (reduced) return;
  const els = $$(".v-chapter:not(.v-hero) :is(.v-head, .v-quote, .v-stat, .v-tl), .v-standfirst > *");
  root.classList.add("v-reveal");
  onceVisible(els, (el) => el.classList.add("v-in"));
}
