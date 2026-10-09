// @ts-check
/* Assets and decoded images.
 * Hosted and offline editions reference site-relative paths. Single-file editions carry each asset once
 * (ARCHITECTURE.md "Single-file assets"): an element with data-asset + src lends its data: URI, and a
 * <script type="application/octet-stream" data-asset> becomes a blob: URL, created lazily and revoked
 * when its last user releases it. The base64 is decoded by fetch() of a data: URL, off the main thread
 * (a chunked atob() that yields between chunks is the fallback), so a large film never stalls a frame. */

const embedded = new Map($$("[data-asset]").map((el) => [el.dataset.asset, el]));
/** @type {Map<string, {url: Promise<string>, refs: number}>} */
const blobs = new Map();

/** Bytes of a base64 string, 1 MiB of text per task. */
async function unbase64(/** @type {string} */ b64, /** @type {string} */ type) {
  const parts = [];
  for (let i = 0; i < b64.length; i += 1 << 20) {
    const bin = atob(b64.slice(i, i + (1 << 20)));
    const bytes = new Uint8Array(bin.length);
    for (let k = 0; k < bin.length; k++) bytes[k] = bin.charCodeAt(k);
    parts.push(bytes);
    await new Promise((r) => setTimeout(r));
  }
  return new Blob(parts, { type });
}

/** URL (a promise) for a site-relative asset path; pair every call with releaseAsset(path). */
function assetURL(/** @type {string} */ path) {
  const el = embedded.get(path);
  if (!el) return Promise.resolve(path);
  if (el.tagName !== "SCRIPT") return Promise.resolve(/** @type {string} */ (el.getAttribute("src")));
  let blob = blobs.get(path);
  if (!blob) {
    const type = el.dataset.type;
    const b64 = el.textContent.trim();
    const url = fetch(`data:${type};base64,${b64}`)
      .then((r) => r.blob())
      .catch(() => unbase64(b64, type))
      .then((b) => URL.createObjectURL(b));
    blobs.set(path, (blob = { url, refs: 0 }));
  }
  blob.refs++;
  return blob.url;
}

function releaseAsset(/** @type {string} */ path) {
  const blob = blobs.get(path);
  if (blob && --blob.refs <= 0) {
    blob.url.then((u) => URL.revokeObjectURL(u), () => {});
    blobs.delete(path);
  }
}

/* AVIF support, probed once with a 1×1 image; JPEG (or WebP) until it answers. */
let avif = false;
const avifReady = new Promise((done) => {
  const probe = new Image();
  probe.onload = () => done((avif = probe.width > 0));
  probe.onerror = () => done(false);
  probe.src =
    "data:image/avif;base64,AAAAIGZ0eXBhdmlmAAAAAGF2aWZtaWYxbWlhZk1BMUIAAADrbWV0YQAAAAAAAAAhaGRscgAAAAAAAAAAcGljdAAAAAAAAAAAAAAAAAAAAAAOcGl0bQAAAAAAAQAAAB5pbG9jAAAAAEQAAAEAAQAAAAEAAAETAAAAIQAAAChpaW5mAAAAAAABAAAAGmluZmUCAAAAAAEAAGF2MDFDb2xvcgAAAABqaXBycAAAAEtpcGNvAAAAFGlzcGUAAAAAAAAAAQAAAAEAAAAQcGl4aQAAAAADCAgIAAAADGF2MUOBAAwAAAAAE2NvbHJuY2x4AAEADQAGgAAAABdpcG1hAAAAAAAAAAEAAQQBAoMEAAAAKW1kYXQSAAoIGAAGiAhoNCAyEx7Hh4VZ3///4s/AAJA2IJvX7LA=";
});

/** The srcset entry just above `need` device px, best supported format first. @returns {string} */
function pickSrc(/** @type {any} */ img, /** @type {number} */ need) {
  const set = img.sources.find((/** @type {any} */ s) => s.type !== "image/avif" || avif);
  if (!set) return img.fallback;
  const sorted = [...set.srcset].sort((a, b) => a[1] - b[1]);
  return (sorted.find((e) => e[1] >= need) || sorted[sorted.length - 1])[0];
}

/** Largest width an Img offers. */
const maxWidth = (/** @type {any} */ img) =>
  Math.max(img.w, ...img.sources.flatMap((/** @type {any} */ s) => s.srcset.map((/** @type {any} */ e) => e[1])));

/**
 * Decode an Img at about `need` px wide: img.decode(), then an ImageBitmap at that size when the
 * browser can (the image element otherwise). Never blocks: everything is promise-driven.
 * @returns {Promise<{src: CanvasImageSource, w: number, h: number, close(): void}>}
 */
async function decodeImg(/** @type {any} */ img, /** @type {number} */ need) {
  await avifReady;
  const path = pickSrc(img, need);
  const el = new Image();
  el.decoding = "async";
  try {
    el.src = await assetURL(path);
    await el.decode();
  } finally {
    releaseAsset(path);
  }
  const w = Math.min(el.naturalWidth, Math.round(need));
  const hh = Math.round((w * el.naturalHeight) / el.naturalWidth);
  if (window.createImageBitmap) {
    const opts = w < el.naturalWidth ? { resizeWidth: w, resizeHeight: hh, resizeQuality: "high" } : {};
    try {
      const bmp = await createImageBitmap(el, /** @type {ImageBitmapOptions} */ (opts));
      return { src: bmp, w: bmp.width, h: bmp.height, close: () => bmp.close() };
    } catch {
      /* fall through to the element */
    }
  }
  return { src: el, w: el.naturalWidth, h: el.naturalHeight, close() {} };
}

/** Tiny LQIP images (data: URIs), decoded once and shared; lqipGen counts arrivals (stages redraw). */
const lqips = new Map();
let lqipGen = 0;
function lqip(/** @type {any} */ img) {
  let el = lqips.get(img.lqip);
  if (!el) {
    el = new Image();
    el.onload = () => {
      lqipGen++;
      kick();
    };
    el.src = img.lqip;
    lqips.set(img.lqip, el);
  }
  return el.complete && el.naturalWidth ? { src: el, w: el.naturalWidth, h: el.naturalHeight } : null;
}

/**
 * At most `max` decoded captures for one stage (LRU), loaded in priority order two at a time.
 * Evicted bitmaps are closed. `onload` fires whenever a new image is ready to draw.
 */
class ImageStore {
  constructor(/** @type {any[]} */ imgs, /** @type {() => void} */ onload, max = 5) {
    this.imgs = imgs;
    this.onload = onload;
    this.max = max;
    /** @type {Map<number, any>} */
    this.cache = new Map();
    this.need = 0;
    this.queue = /** @type {number[]} */ ([]);
    this.busy = 0;
  }

  /** Ready image for index `i`, or null (bumps it to most recently used). */
  get(/** @type {number} */ i) {
    const e = this.cache.get(i);
    if (!e || !e.ready) return null;
    this.cache.delete(i);
    this.cache.set(i, e);
    return e.ready;
  }

  /** Keep these indices (most important first) decoded at `need` px wide; evict the rest beyond max. */
  want(/** @type {number[]} */ order, /** @type {number} */ need) {
    if (need > this.need * 1.25) this.clear(); // the stage grew: decode sharper copies
    this.need = Math.max(this.need, need);
    order = order.filter((i, k) => i >= 0 && i < this.imgs.length && order.indexOf(i) === k).slice(0, this.max);
    const missing = order.filter((i) => !this.cache.has(i)).length;
    for (const [i, e] of [...this.cache]) {
      if (this.cache.size + missing <= this.max) break;
      if (!order.includes(i)) this.drop(i, e);
    }
    this.queue = order.filter((i) => !this.cache.has(i));
    this.pump();
  }

  pump() {
    while (this.busy < 2 && this.queue.length) {
      const i = /** @type {number} */ (this.queue.shift());
      const entry = { ready: null, dead: false }; // a failed decode stays empty (no retry storm)
      this.cache.set(i, entry);
      this.busy++;
      decodeImg(this.imgs[i], this.need)
        .then((d) => {
          if (entry.dead) d.close();
          else {
            entry.ready = d;
            this.onload();
          }
        })
        .catch(() => {})
        .finally(() => {
          this.busy--;
          this.pump();
        });
    }
  }

  drop(/** @type {number} */ i, /** @type {any} */ e) {
    e.dead = true;
    if (e.ready) e.ready.close();
    this.cache.delete(i);
  }

  clear() {
    for (const [i, e] of this.cache) this.drop(i, e);
    this.queue = [];
    this.need = 0;
  }
}
