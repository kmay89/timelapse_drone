/* Vantage story service worker (template; `vantage build` injects CONFIG).
 *
 * - install: precache the core set (page, fonts, brand, icons, one JPEG per image, posters)
 * - message {type: "vantage:save"}: cache every remaining file ("Save for offline"), posting
 *   {type: "vantage:progress", done, total, bytes, totalBytes} and finally {type: "vantage:saved"};
 *   {type: "vantage:status"} answers {type: "vantage:status", cached, total}
 * - index.html: network first (3 s), cache fallback; everything else: cache first
 * - Range requests (iOS <video>) are answered with 206 slices of the cached file
 * - offline image misses fall back to any cached variant of the same image (another width/format)
 * The page only registers this worker over http(s); it never runs from file://.
 */
"use strict";

const CONFIG = /*@config*/ { cache: "vantage-dev", core: ["./"], assets: [] } /*@end*/;
const PREFIX = CONFIG.cache.replace(/-[^-]+$/, "-");
const SCOPE = self.registration.scope;
const url = (path) => new URL(path, SCOPE).href;
const ASSETS = new Map(CONFIG.assets.map(([path, bytes]) => [url(path), bytes]));
const IMG_VARIANT = /-(\d+)\.(avif|webp|jpg)$/;

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches
      .open(CONFIG.cache)
      .then((cache) => cache.addAll(CONFIG.core.map((p) => new Request(url(p), { cache: "reload" }))))
      .then(() => self.skipWaiting()),
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches
      .keys()
      .then((keys) => Promise.all(keys.filter((k) => k.startsWith(PREFIX) && k !== CONFIG.cache).map((k) => caches.delete(k))))
      .then(() => self.clients.claim()),
  );
});

self.addEventListener("message", (event) => {
  const type = event.data && event.data.type;
  const reply = (msg) => event.source && event.source.postMessage(msg);
  if (type === "vantage:save") event.waitUntil(saveAll(reply));
  if (type === "vantage:status") {
    event.waitUntil(
      caches.open(CONFIG.cache).then(async (cache) => {
        const cached = (await cache.keys()).filter((r) => ASSETS.has(r.url)).length;
        reply({ type: "vantage:status", cached, total: ASSETS.size });
      }),
    );
  }
});

async function saveAll(reply) {
  const cache = await caches.open(CONFIG.cache);
  const totalBytes = [...ASSETS.values()].reduce((a, b) => a + b, 0);
  let done = 0;
  let bytes = 0;
  for (const [href, size] of ASSETS) {
    if (!(await cache.match(href))) {
      try {
        const res = await fetch(href, { cache: "reload" });
        if (res.ok) await cache.put(href, res);
      } catch (err) {
        reply({ type: "vantage:error", url: href, message: String(err) });
        return;
      }
    }
    done += 1;
    bytes += size;
    reply({ type: "vantage:progress", done, total: ASSETS.size, bytes, totalBytes });
  }
  reply({ type: "vantage:saved", total: ASSETS.size, totalBytes });
}

self.addEventListener("fetch", (event) => {
  const req = event.request;
  if (req.method !== "GET" || !req.url.startsWith(SCOPE)) return;
  const href = req.url.split("#")[0].split("?")[0];
  if (req.mode === "navigate" || href === url("./") || href === url("index.html")) {
    event.respondWith(networkFirst(req));
  } else if (req.headers.has("range")) {
    event.respondWith(ranged(req, href));
  } else {
    event.respondWith(cacheFirst(req, href));
  }
});

async function networkFirst(req) {
  const cache = await caches.open(CONFIG.cache);
  try {
    const res = await Promise.race([
      fetch(req),
      new Promise((_, reject) => setTimeout(() => reject(new Error("timeout")), 3000)),
    ]);
    if (res.ok) await cache.put(url("index.html"), res.clone());
    return res;
  } catch (err) {
    return (await cache.match(url("index.html"))) || (await cache.match(url("./"))) || Response.error();
  }
}

async function cacheFirst(req, href) {
  const cache = await caches.open(CONFIG.cache);
  const hit = await cache.match(href);
  if (hit) return hit;
  try {
    const res = await fetch(req);
    if (res.ok && res.status === 200 && ASSETS.has(href)) await cache.put(href, res.clone());
    return res;
  } catch (err) {
    return (await anyVariant(cache, href)) || Response.error();
  }
}

/* Another cached width/format of the same image, largest JPEG first. */
async function anyVariant(cache, href) {
  const m = IMG_VARIANT.exec(href);
  if (!m) return undefined;
  const stem = href.slice(0, m.index);
  const rank = { jpg: 0, webp: 1, avif: 2 };
  const candidates = [...ASSETS.keys()]
    .map((u) => [u, IMG_VARIANT.exec(u)])
    .filter(([u, v]) => v && u.slice(0, v.index) === stem && u !== href)
    .sort(([, a], [, b]) => rank[a[2]] - rank[b[2]] || Number(b[1]) - Number(a[1]));
  for (const [u] of candidates) {
    const hit = await cache.match(u);
    if (hit) return hit;
  }
  return undefined;
}

async function ranged(req, href) {
  const cache = await caches.open(CONFIG.cache);
  const hit = await cache.match(href);
  if (!hit) return fetch(req);
  const blob = await hit.blob();
  const size = blob.size;
  const m = /^bytes=(\d*)-(\d*)$/.exec((req.headers.get("range") || "").trim());
  if (!m || (m[1] === "" && m[2] === "")) return new Response(blob, { status: 200, headers: hit.headers });
  let start;
  let end;
  if (m[1] === "") {
    start = Math.max(size - Number(m[2]), 0);
    end = size - 1;
  } else {
    start = Number(m[1]);
    end = m[2] === "" ? size - 1 : Math.min(Number(m[2]), size - 1);
  }
  if (start >= size || start > end) {
    return new Response(null, { status: 416, headers: { "Content-Range": `bytes */${size}` } });
  }
  return new Response(blob.slice(start, end + 1), {
    status: 206,
    statusText: "Partial Content",
    headers: {
      "Content-Type": hit.headers.get("Content-Type") || "video/mp4",
      "Content-Range": `bytes ${start}-${end}/${size}`,
      "Content-Length": String(end - start + 1),
      "Accept-Ranges": "bytes",
    },
  });
}
