// @ts-check
/* HERO: the reveal (CSS, gated on .js), a scroll cue that appears after 2 s only if the reader hasn't
 * scrolled, and the ambient loop: muted, inline, attempted autoplay. A rejected play() (Low Power Mode,
 * thermal limits) keeps the still, which also carries a slow Ken Burns drift. Both pause off screen, and
 * a pause button stops both for the rest of the session (WCAG 2.2.2). Under reduced motion there is
 * neither, so no button either, and the loop is never even decoded. */

/** Muted inline <video> for a single-file edition; loadVideo() gives it its blob when first needed. */
function buildVideo(/** @type {any} */ video, /** @type {Record<string, any>} */ attrs) {
  const el = h("video", { playsinline: true, muted: true, preload: "metadata", ...attrs });
  assetURL(video.poster.fallback).then((u) => (el.poster = u), () => {});
  return el;
}

/** @type {WeakMap<HTMLVideoElement, Promise<void>>} */
const videoLoads = new WeakMap();
/** A built video's blob, decoded once (H.264 is the last source, and the only one in single files). */
function loadVideo(/** @type {HTMLVideoElement} */ el, /** @type {any} */ video) {
  let p = videoLoads.get(el);
  if (!p) videoLoads.set(el, (p = assetURL(video.sources[video.sources.length - 1].src).then((u) => void (el.src = u))));
  return p;
}

/** Play muted and inline; resolves false when the browser refuses (the poster stays). */
function tryPlay(/** @type {HTMLVideoElement} */ el) {
  el.muted = true;
  el.playsInline = true;
  return el.play().then(
    () => true,
    () => false,
  );
}

/** Per-tab memory (sessionStorage), silent where storage is blocked. */
const session = (/** @type {string} */ key, /** @type {string | null} */ value = null) => {
  try {
    if (value === null) return sessionStorage.getItem(key);
    sessionStorage.setItem(key, value);
  } catch {
    /* private mode, blocked storage: forget */
  }
  return null;
};

/** @returns {Component} */
function hero(/** @type {HTMLElement} */ sec, /** @type {any} */ ch) {
  const media = $(".v-hero__media", sec);
  /** @type {HTMLVideoElement | null} */
  let video = $("video", sec);
  const built = !video && media && ch.video?.sources.length;
  if (built) {
    video = buildVideo(ch.video, { loop: true, "aria-hidden": "true", tabindex: "-1" });
    media.append(video);
  }
  const vid = /** @type {HTMLVideoElement} */ (video);
  if (video) {
    video.addEventListener("playing", () => media.classList.add("v-playing"));
    if (!video.paused) media.classList.add("v-playing");
  }
  const key = `vantage:${story.meta.slug}:still`;
  let still = session(key) === "1";
  let shown = false;
  const play = () => {
    if (!video || reduced || still || !shown) return;
    (built ? loadVideo(vid, ch.video) : Promise.resolve()).then(() => shown && !still && !reduced && tryPlay(vid));
  };
  motionQuery.addEventListener("change", () => (reduced ? video?.pause() : play()));

  if (media) {
    const what = video ? "video" : "motion";
    const btn = h("button", { class: "v-btn v-hero__pause", type: "button" });
    const setStill = (/** @type {boolean} */ s) => {
      still = s;
      session(key, s ? "1" : "0");
      sec.classList.toggle("v-still", s);
      btn.setAttribute("aria-label", `${s ? "Play" : "Pause"} the background ${what}`);
      btn.replaceChildren(icon(s ? "play" : "pause"));
      if (s) video?.pause();
      else play();
    };
    btn.addEventListener("click", () => setStill(!still));
    $(".v-hero__stage", sec).append(btn);
    setStill(still);
  }

  window.setTimeout(() => scrollY < 8 && sec.classList.add("v-cue"), 2000);
  const hideCue = () => {
    if (scrollY < 8) return;
    sec.classList.remove("v-cue");
    removeEventListener("scroll", hideCue);
  };
  addEventListener("scroll", hideCue, passive);
  return {
    enter() {
      shown = true;
      sec.classList.remove("v-off");
      play();
    },
    leave() {
      shown = false;
      sec.classList.add("v-off");
      video?.pause();
    },
    update() {},
  };
}
