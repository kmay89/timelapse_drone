// @ts-check
/* HERO: the reveal (CSS, gated on .js), a scroll cue that appears after 2 s only if the reader hasn't
 * scrolled, and the ambient loop: muted, inline, attempted autoplay. A rejected play() (Low Power Mode,
 * thermal limits) keeps the still, which also carries a slow Ken Burns drift. Both pause off screen. */

/** Muted inline <video> for a chapter's sources; single-file editions read them from the page. */
function buildVideo(/** @type {any} */ video, /** @type {Record<string, any>} */ attrs) {
  const src = video.sources[video.sources.length - 1].src; // H.264 is last (and only, when embedded)
  const el = h("video", { playsinline: true, muted: true, preload: "metadata", ...attrs });
  el.poster = assetURL(video.poster.fallback);
  el.src = assetURL(src);
  return el;
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

/** @returns {Component} */
function hero(/** @type {HTMLElement} */ sec, /** @type {any} */ ch) {
  const media = $(".v-hero__media", sec);
  let video = $("video", sec);
  if (!video && media && ch.video?.sources.length) {
    video = buildVideo(ch.video, { loop: true, "aria-hidden": "true", tabindex: "-1" });
    media.append(video);
  }
  if (video) {
    video.addEventListener("playing", () => media.classList.add("v-playing"));
    if (!video.paused) media.classList.add("v-playing");
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
      sec.classList.remove("v-off");
      if (video && !reduced) tryPlay(video);
    },
    leave() {
      sec.classList.add("v-off");
      video?.pause();
    },
    update() {},
  };
}
