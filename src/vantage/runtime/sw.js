/* Vantage story service worker (template; `vantage build` injects CONFIG).
 *
 * - install: precache the shell (page, fonts, brand, icons, and the opening hero's fallback JPEGs, which
 *   the page shows before this worker is in control); every other file is cached when the page first
 *   asks for it, or all at once by "Save for offline"
 * - one cache per story and scope, kept from deploy to deploy: each file is stored under
 *   `<file>?v=<content hash>`, so an update keeps every file it did not change; activate drops the rest
 * - message {type: "vantage:save"}: cache every remaining file ("Save for offline"), posting
 *   {type: "vantage:progress", done, total, bytes, totalBytes}, then {type: "vantage:saved"}, or
 *   {type: "vantage:error", message} when a file could not be fetched (offline, a 404, a full disk)
 * - {type: "vantage:status"} answers {type: "vantage:status", cached, total}; when an update changed
 *   files of a copy the reader saved, it fetches those instead (posting the save's messages)
 * - the page (./, index.html): network first (3 s), cache fallback; everything else: cache first; a
 *   network response goes to the page at once and is stored as it streams
 * - caches are named per scope, so stories sharing an origin never delete each other's
 * - Range requests (iOS <video>) are answered with 206 slices of the cached file
 * - offline image misses fall back to any cached variant of the same image (another width/format)
 * The page only registers this worker over http(s); it never runs from file://.
 */
"use strict";

const CONFIG = /*@config*/ { cache: "vantage-dev", core: ["./"], assets: [] } /*@end*/;
const SCOPE = self.registration.scope;
const CACHE = `${CONFIG.cache} ${SCOPE}`;
const url = (path) => new URL(path, SCOPE).href;
const ASSETS = new Map(CONFIG.assets.map(([path, bytes]) => [url(path), bytes]));
const KEYS = new Map(CONFIG.assets.map(([path, , sha]) => [url(path), `${url(path)}?v=${sha}`]));
const key = (href) => KEYS.get(href) || href;
const SAVED = url(".vantage-saved"); // stored once the reader has saved the whole story
const IMG_VARIANT = /-(\d+)\.(avif|webp|jpg)$/;

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches
      .open(CACHE)
      .then((cache) =>
        Promise.all(
          CONFIG.core.map(async (path) => {
            const href = url(path);
            if (KEYS.has(href) && (await cache.match(KEYS.get(href)))) return; // unchanged since the last deploy
            const res = await fetch(href, { cache: "reload" });
            if (!res.ok) throw new Error(`${res.status} ${href}`);
            await cache.put(key(href), res);
          }),
        ),
      )
      .then(() => self.skipWaiting()),
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    (async () => {
      const keys = await caches.keys();
      await Promise.all(keys.filter((k) => k.startsWith("vantage-") && k.endsWith(` ${SCOPE}`) && k !== CACHE).map((k) => caches.delete(k)));
      // Files this deploy changed or dropped.
      const live = new Set([...KEYS.values(), url("./"), SAVED]);
      const cache = await caches.open(CACHE);
      await Promise.all((await cache.keys()).filter((r) => !live.has(r.url)).map((r) => cache.delete(r)));
      await self.clients.claim();
    })(),
  );
});

self.addEventListener("message", (event) => {
  const type = event.data && event.data.type;
  const reply = (msg) => event.source && event.source.postMessage(msg);
  if (type === "vantage:save") event.waitUntil(save(reply));
  if (type === "vantage:status") event.waitUntil(status(reply));
});

let saving = null; // one save at a time
const save = (reply) => (saving = saving || saveAll(reply).finally(() => (saving = null)));

async function status(reply) {
  const cache = await caches.open(CACHE);
  const have = new Set((await cache.keys()).map((r) => r.url));
  const cached = [...KEYS.values()].filter((k) => have.has(k)).length;
  // Saved before an update changed some files: fetch just those, so the copy stays whole offline.
  if (cached < KEYS.size && have.has(SAVED)) return save(reply);
  reply({ type: "vantage:status", cached, total: KEYS.size });
}

async function saveAll(reply) {
  const cache = await caches.open(CACHE);
  const totalBytes = [...ASSETS.values()].reduce((a, b) => a + b, 0);
  let done = 0;
  let bytes = 0;
  let failed = 0;
  for (const [href, size] of ASSETS) {
    if (!(await cache.match(key(href)))) {
      try {
        const res = await fetch(href, { cache: "reload" });
        if (res.ok) await cache.put(key(href), res);
        else failed += 1; // a 404 or 5xx: the copy is not whole, so it is never called saved
      } catch (err) {
        reply({ type: "vantage:error", url: href, message: String(err) });
        return;
      }
    }
    done += 1;
    bytes += size;
    reply({ type: "vantage:progress", done, total: ASSETS.size, bytes, totalBytes });
  }
  if (failed) {
    reply({ type: "vantage:error", message: `${failed} of ${ASSETS.size} files could not be downloaded` });
    return;
  }
  await cache.put(SAVED, new Response(""));
  reply({ type: "vantage:saved", total: ASSETS.size, totalBytes });
}

self.addEventListener("fetch", (event) => {
  const req = event.request;
  if (req.method !== "GET" || !req.url.startsWith(SCOPE)) return;
  const href = req.url.split("#")[0].split("?")[0];
  const stores = [];
  const store = (put) => stores.push(put.catch(() => {}));
  const res =
    href === url("./") || href === url("index.html")
      ? networkFirst(req, store)
      : req.headers.has("range")
        ? ranged(req, href)
        : cacheFirst(req, href, store);
  event.respondWith(res);
  event.waitUntil(res.then(() => Promise.all(stores), () => {})); // copies finish after the page has its answer
});

async function networkFirst(req, store) {
  const cache = await caches.open(CACHE);
  try {
    const res = await Promise.race([
      fetch(req),
      new Promise((_, reject) => setTimeout(() => reject(new Error("timeout")), 3000)),
    ]);
    if (res.ok) store(cache.put(url("./"), res.clone()));
    return res;
  } catch (err) {
    return unredirect((await cache.match(url("./"))) || (await cache.match(key(url("index.html"))))) || Response.error();
  }
}

/* WebKit fails a navigation answered with a redirected response ("Response served by service worker
 * has redirections"); hosts that redirect index.html to ./ (Cloudflare Pages) leave one in the cache. */
function unredirect(res) {
  if (!res || !res.redirected) return res;
  return new Response(res.body, { status: res.status, statusText: res.statusText, headers: res.headers });
}

async function cacheFirst(req, href, store) {
  const cache = await caches.open(CACHE);
  const hit = await cache.match(key(href));
  if (hit) return hit;
  try {
    const res = await fetch(req);
    if (res.ok && res.status === 200 && ASSETS.has(href)) store(cache.put(key(href), res.clone()));
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
    const hit = await cache.match(key(u));
    if (hit) return hit;
  }
  return undefined;
}

async function ranged(req, href) {
  const cache = await caches.open(CACHE);
  const hit = await cache.match(key(href));
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
