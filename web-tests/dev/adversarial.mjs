// Adversarial checks for the runtime (dev loop, not CI): the cases a happy-path walk-through misses.
//   node web-tests/dev/adversarial.mjs <served-url> <outdir>
// Needs ffmpeg with libsvtav1: this Chromium has no H.264/HEVC decoder, so the story's MP4s are
// answered with a tiny AV1 clip to exercise real playback.
import { chromium } from "@playwright/test";
import { execFileSync } from "node:child_process";
import fs from "node:fs";
import path from "node:path";

const [url, out] = process.argv.slice(2);
fs.mkdirSync(out, { recursive: true });
const IPHONE = { viewport: { width: 393, height: 852 }, deviceScaleFactor: 3, isMobile: true, hasTouch: true };
const DESKTOP = { viewport: { width: 1440, height: 900 } };
const results = [];
const check = (name, ok, extra = "") => results.push(`${ok ? "PASS" : "FAIL"} ${name}${extra ? ` (${extra})` : ""}`);

const clip = path.join(out, "av1.mp4");
execFileSync("ffmpeg", ["-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=24:duration=3",
  "-c:v", "libsvtav1", "-pix_fmt", "yuv420p", "-movflags", "+faststart", clip], { stdio: "ignore" });
const av1 = fs.readFileSync(clip);

/** A page with error capture; `rewrite(html)` edits the served index.html (the StoryJSON) in flight. */
async function open(browser, opts, { rewrite, init } = {}) {
  const ctx = await browser.newContext(opts);
  const page = await ctx.newPage();
  const errors = [];
  page.on("console", (m) => m.type() === "error" && errors.push(m.text()));
  page.on("pageerror", (e) => errors.push(e.message));
  if (init) await page.addInitScript(init);
  await page.route(/\.mp4$/, (route) => route.fulfill({ status: 200, contentType: "video/mp4", body: av1 }));
  if (rewrite)
    await page.route(/\/(index\.html)?$/, async (route) => {
      const res = await route.fetch();
      await route.fulfill({ response: res, body: rewrite(await res.text()) });
    });
  await page.goto(url, { waitUntil: "load" });
  return { ctx, page, errors };
}

const trackY = (page, id, p) =>
  page.evaluate(([id, p]) => {
    const t = document.querySelector(`#${id} > .v-track`);
    return t.getBoundingClientRect().top + scrollY + p * (t.offsetHeight - t.querySelector(".v-stage").offsetHeight);
  }, [id, p]);
const go = async (page, y, wait = 700) => {
  await page.evaluate((y) => scrollTo(0, y), y);
  await page.waitForTimeout(wait);
};

const browser = await chromium.launch();

/* 1. A mouse released outside a drag surface must not leave a drag armed: hovering later scrubs nothing. */
{
  const { ctx, page, errors } = await open(browser, DESKTOP);
  await go(page, await trackY(page, "twelve-flights", 0.3));
  const rb = await page.locator("#twelve-flights .v-rail").boundingBox();
  const y = rb.y + rb.height / 2;
  await page.mouse.move(rb.x + rb.width / 2, y);
  await page.mouse.down();
  await page.mouse.move(rb.x + rb.width / 2, y - 200, { steps: 6 }); // straight up, off the rail
  await page.mouse.up();
  await page.waitForTimeout(300);
  const before = await page.evaluate(() => scrollY);
  await page.mouse.move(rb.x + rb.width / 2, y, { steps: 4 }); // back over the rail, no button held
  await page.mouse.move(rb.x + 20, y, { steps: 12 });
  await page.waitForTimeout(500);
  const after = await page.evaluate(() => scrollY);
  check("rail: hovering after an outside release does not scrub", before === after, `${before} → ${after}`);

  // Explore blink: hold, slide off the stage, release outside → back to the after flight.
  const ex = page.locator("#explore .v-x__stage");
  await ex.scrollIntoViewIfNeeded();
  await page.getByRole("button", { name: "Blink", exact: true }).click();
  const eb = await ex.boundingBox();
  await page.mouse.move(eb.x + eb.width / 2, eb.y + eb.height - 30);
  await page.mouse.down();
  await page.waitForTimeout(250);
  const held = await page.locator("#explore .v-cmp__tag--b").evaluate((e) => e.classList.contains("v-on"));
  await page.mouse.move(eb.x + eb.width / 2, eb.y + eb.height + 60, { steps: 5 }); // below the stage
  await page.mouse.up();
  await page.waitForTimeout(300);
  const stuck = await page.locator("#explore .v-cmp__tag--b").evaluate((e) => e.classList.contains("v-on"));
  check("explore blink: release outside the stage shows the after flight again", held && !stuck, `held=${held} stuck=${stuck}`);
  check("no console errors (mouse)", errors.length === 0, errors.join(" | "));
  await ctx.close();
}

/* 2. Image quality: compare layers select their variant for the zoomed stage (every <source>'s sizes,
 *    not just the <img>'s, since the browser takes sizes from the chosen source), and canvases keep
 *    high-quality smoothing after being sized. (currentSrc is no proof: Chrome reuses cached larger
 *    variants, so the sizes themselves are checked.) */
for (const [name, opts] of [["desktop", DESKTOP], ["iphone", IPHONE]]) {
  const { ctx, page } = await open(browser, opts);
  await go(page, await trackY(page, "before-after", 0.5), 1500);
  const sizes = await page.$$eval("#before-after .v-cmp__img", (boxes) =>
    boxes.flatMap((box) => {
      const w = box.getBoundingClientRect().width;
      return [...box.querySelectorAll("source[srcset], img[srcset]")].map((el) => ({ sizes: el.sizes, w: Math.round(w) }));
    }),
  );
  check(`${name}: compare sources size for the stage`, sizes.every((s) => parseFloat(s.sizes) >= s.w - 1), JSON.stringify(sizes));
  const smoothing = await page.$$eval(".v-canvas", (cs) => cs.map((c) => c.getContext("2d").imageSmoothingQuality));
  check(`${name}: canvases draw with high-quality smoothing`, smoothing.every((q) => q === "high"), smoothing.join(","));
  await ctx.close();
}

/* 3. The resting curtain sits exactly at its split once the first-entry nudge has played out. */
{
  const { ctx, page } = await open(browser, DESKTOP);
  await go(page, await trackY(page, "before-after", 0.02), 100);
  await go(page, await trackY(page, "before-after", 0.05), 1600); // the nudge (900 ms) ends with no scrolling
  const [x, split, w] = await page.evaluate(() => {
    const story = JSON.parse(document.getElementById("vantage-story").textContent);
    const st = document.querySelector("#before-after .v-stage");
    const m = /translate3d\(([-\d.]+)px/.exec(st.querySelector(".v-cmp__handle").style.transform);
    return [+m[1], story.chapters.find((c) => c.id === "before-after").steps[0].split ?? 0.5, st.clientWidth];
  });
  check("curtain settles on its split after the nudge", Math.abs(x - split * w) <= 1, `x=${x}, split ${split} of ${w}`);
  await ctx.close();
}

/* 4. Memory: scrubbing end to end keeps ≤ 5 decoded bitmaps per stage, and leaving frees them. */
{
  const init = () => {
    window.__bitmaps = 0;
    const make = window.createImageBitmap.bind(window);
    window.createImageBitmap = (...a) =>
      make(...a).then((b) => {
        window.__bitmaps++;
        const close = b.close.bind(b);
        b.close = () => (window.__bitmaps--, close());
        return b;
      });
  };
  const { ctx, page, errors } = await open(browser, IPHONE, { init });
  let peak = 0;
  for (let p = 0; p <= 1.0001; p += 0.04) {
    await go(page, await trackY(page, "twelve-flights", p), 90);
    peak = Math.max(peak, await page.evaluate(() => window.__bitmaps));
  }
  await page.waitForTimeout(800);
  peak = Math.max(peak, await page.evaluate(() => window.__bitmaps));
  check("scrub keeps at most 5 decoded bitmaps", peak <= 5, `peak ${peak}`);
  await go(page, await page.evaluate(() => document.getElementById("timeline").getBoundingClientRect().top + scrollY), 1200);
  const left = await page.evaluate(() => window.__bitmaps);
  check("bitmaps are closed once the scrub is far away", left === 0, `${left} live`);
  check("no console errors (memory)", errors.length === 0, errors.join(" | "));
  await ctx.close();
}

/* 5. Hostile text in the StoryJSON stays text: labels, names, titles and the brand never become markup. */
{
  const evil = "<img src=x onerror=window.__xss=1>";
  const rewrite = (html) =>
    html.replace(/(<script id="vantage-story" type="application\/json">)(.*?)(<\/script>)/s, (_, a, body, c) => {
      const story = JSON.parse(body);
      story.brand.name = evil;
      for (const v of story.vantages) {
        v.name = evil;
        for (const cap of v.captures) cap.label = `${evil} ${cap.date.slice(0, 4)}`;
      }
      for (const ch of story.chapters) {
        if (ch.title) ch.title = evil;
        for (const hs of ch.hotspots || []) hs.label = evil;
        if (ch.type === "compare") [ch.beforeLabel, ch.afterLabel] = [evil, evil];
      }
      return a + JSON.stringify(story).replace(/</g, "\\u003c") + c;
    });
  const { ctx, page, errors } = await open(browser, IPHONE, { rewrite });
  for (const p of [0.1, 0.5, 0.95]) await go(page, await trackY(page, "twelve-flights", p), 400);
  await page.locator("#twelve-flights .v-hs.v-on").first().tap();
  await page.waitForTimeout(400);
  await page.keyboard.press("Escape");
  await go(page, await trackY(page, "before-after", 0.5), 400);
  await page.getByRole("button", { name: "Contents" }).tap();
  await page.waitForTimeout(400);
  await page.keyboard.press("Escape");
  await page.locator("#explore .v-x__stage").scrollIntoViewIfNeeded();
  await page.getByRole("button", { name: "Curtain", exact: true }).tap();
  await page.waitForTimeout(400);
  const xss = await page.evaluate(() => window.__xss === 1 || document.querySelector("main img[src='x']") !== null);
  check("hostile labels render as text", !xss);
  check("no page errors (hostile text)", errors.filter((e) => !/Failed to load resource/.test(e)).length === 0, errors.join(" | "));
  await ctx.close();
}

/* 6. Video: the hero loop plays with default motion and stays still under reduced motion; a video
 *    chapter plays on screen and pauses off screen. */
{
  const { ctx, page, errors } = await open(browser, IPHONE);
  await page.waitForTimeout(1500);
  check("hero loop plays", await page.$eval("#opening video", (v) => !v.paused && v.closest(".v-playing") !== null));
  const vid = page.locator("#last-evening video");
  await vid.scrollIntoViewIfNeeded();
  await page.waitForTimeout(1500);
  const playing = await vid.evaluate((v) => !v.paused);
  await go(page, 0, 800);
  check("video chapter plays on screen, pauses off screen", playing && (await vid.evaluate((v) => v.paused)));
  check("no console errors (video)", errors.length === 0, errors.join(" | "));
  await ctx.close();
}
{
  const { ctx, page } = await open(browser, { ...IPHONE, reducedMotion: "reduce" });
  await page.waitForTimeout(1500);
  // Chromium holds back muted autoplay of the (transparent) video anyway; WebKit may not, so the
  // runtime must switch the attribute off itself.
  const still = await page.$eval("#opening video", (v) => v.paused && !v.autoplay && v.closest(".v-playing") === null);
  check("reduced motion: hero loop stays on its still", still);
  const vid = page.locator("#last-evening video");
  await vid.scrollIntoViewIfNeeded();
  await page.waitForTimeout(1200);
  check("reduced motion: video chapter waits for the reader", await vid.evaluate((v) => v.paused));
  await ctx.close();
}

/* 7. Two steps on one capture (a focus-only step: a camera push without a new flight) ease from one
 *    to the next: their cards cross-fade over some scroll instead of cutting. */
{
  const rewrite = (html) =>
    html.replace(/(<script id="vantage-story" type="application\/json">)(.*?)(<\/script>)/s, (_, a, body, c) => {
      const story = JSON.parse(body);
      const ch = story.chapters.find((x) => x.type === "scrub");
      ch.steps.splice(1, 0, { html: "<p>Closer in.</p>", focus: [0.3, 0.6, 1.8] });
      return a + JSON.stringify(story).replace(/</g, "\\u003c") + c;
    });
  const { ctx, page, errors } = await open(browser, IPHONE, { rewrite });
  // Both steps sit in capture 0's hold (the first 0.55 / 11.55 of the track): the first card must
  // fade out over some scroll there, not drop from 1 to 0 at once.
  const [y0, y1] = [await trackY(page, "twelve-flights", 0), await trackY(page, "twelve-flights", 0.05)];
  const between = [];
  for (let y = y0; y <= y1; y += 4) {
    await go(page, y, 40);
    const o = await page.$eval("#twelve-flights .v-card", (c) => +c.style.opacity);
    if (o > 0.05 && o < 0.95) between.push(o);
  }
  check("steps sharing a capture cross-fade", between.length >= 3, `${between.length} in-between samples`);
  check("no console errors (shared capture)", errors.length === 0, errors.join(" | "));
  await ctx.close();
}

await browser.close();
console.log(results.join("\n"));
const failed = results.filter((r) => r.startsWith("FAIL")).length;
console.log(`${results.length - failed}/${results.length} passed`);
process.exit(failed ? 1 : 0);
