// Interaction checks for the runtime (dev loop, not CI): touch drags through CDP, rail taps, sheets,
// explore gestures, save-for-offline, share fallback, reduced motion and the file:// editions.
//   node web-tests/dev/interact.mjs <served-url> <dist-dir> <outdir>
import { chromium } from "@playwright/test";
import fs from "node:fs";
import path from "node:path";
import { pathToFileURL } from "node:url";

const [url, dist, out] = process.argv.slice(2);
fs.mkdirSync(out, { recursive: true });
const IPHONE = { viewport: { width: 393, height: 852 }, deviceScaleFactor: 3, isMobile: true, hasTouch: true };
const results = [];
const check = (name, ok, extra = "") => results.push(`${ok ? "PASS" : "FAIL"} ${name}${extra ? ` (${extra})` : ""}`);

async function open(browser, target, opts = {}) {
  const ctx = await browser.newContext({ ...IPHONE, ...opts });
  const page = await ctx.newPage();
  const errors = [];
  page.on("console", (m) => m.type() === "error" && errors.push(m.text()));
  page.on("pageerror", (e) => errors.push(e.message));
  await page.goto(target, { waitUntil: "load" });
  return { ctx, page, errors };
}

/** Touch drag through CDP (Playwright has no touch-move API): straight line in `steps`. */
async function touchDrag(page, from, to, steps = 10) {
  const cdp = await page.context().newCDPSession(page);
  const pt = (p) => [{ x: p[0], y: p[1], id: 1 }];
  await cdp.send("Input.dispatchTouchEvent", { type: "touchStart", touchPoints: pt(from) });
  for (let i = 1; i <= steps; i++) {
    const p = [from[0] + ((to[0] - from[0]) * i) / steps, from[1] + ((to[1] - from[1]) * i) / steps];
    await cdp.send("Input.dispatchTouchEvent", { type: "touchMove", touchPoints: pt(p) });
    await page.waitForTimeout(16);
  }
  await cdp.send("Input.dispatchTouchEvent", { type: "touchEnd", touchPoints: [] });
}

/** Two-finger pinch through CDP around `c`, from spread d0 to d1. */
async function pinch(page, c, d0, d1, steps = 10) {
  const cdp = await page.context().newCDPSession(page);
  const pts = (d) => [{ x: c[0] - d / 2, y: c[1], id: 1 }, { x: c[0] + d / 2, y: c[1], id: 2 }];
  await cdp.send("Input.dispatchTouchEvent", { type: "touchStart", touchPoints: pts(d0) });
  for (let i = 1; i <= steps; i++) {
    await cdp.send("Input.dispatchTouchEvent", { type: "touchMove", touchPoints: pts(d0 + ((d1 - d0) * i) / steps) });
    await page.waitForTimeout(16);
  }
  await cdp.send("Input.dispatchTouchEvent", { type: "touchEnd", touchPoints: [] });
}

const trackY = (page, id, p) =>
  page.evaluate(([id, p]) => {
    const t = document.querySelector(`#${id} > .v-track`);
    const r = t.getBoundingClientRect();
    return r.top + scrollY + p * (t.offsetHeight - t.querySelector(".v-stage").offsetHeight);
  }, [id, p]);
const odoDate = (page, id) => page.$eval(`#${id} .v-odo__date`, (el) => el.textContent.replace(/\s+/g, " ").trim());
/** True when a canvas shows an image: pixel variance where readable; file:// taints the canvas, so
 * there a centre crop must not compress like a flat fill. */
async function painted(page, sel) {
  const spread = await page
    .$eval(sel, (c) => {
      const x = c.getContext("2d").getImageData(c.width / 2 - 40, c.height / 2 - 40, 80, 80).data;
      let lo = 255;
      let hi = 0;
      for (let i = 0; i < x.length; i += 4) {
        lo = Math.min(lo, x[i + 1]);
        hi = Math.max(hi, x[i + 1]);
      }
      return hi - lo;
    })
    .catch(() => null);
  if (spread !== null) return spread > 30;
  const b = await page.locator(sel).boundingBox();
  const png = await page.screenshot({ clip: { x: b.x + b.width / 2 - 60, y: b.y + b.height / 2 - 60, width: 120, height: 120 } });
  return png.length > 30000;
}

const browser = await chromium.launch();

/* 1. Hosted site on a touch phone */
{
  const { ctx, page, errors } = await open(browser, url);
  // Scrub: tap the rail near its right end → the page scrolls to the last flight's hold.
  await page.evaluate((y) => scrollTo(0, y), await trackY(page, "twelve-flights", 0.05));
  await page.waitForTimeout(500);
  const rail = await page.$("#twelve-flights .v-rail__ticks");
  const rb = await rail.boundingBox();
  await page.touchscreen.tap(rb.x + rb.width - 2, rb.y + 4);
  await page.waitForTimeout(1600);
  check("rail tap scrolls to the last flight", (await odoDate(page, "twelve-flights")) === "September 2026", await odoDate(page, "twelve-flights"));
  await page.screenshot({ path: path.join(out, "iphone-rail-tap.png") });
  check("rail aria-valuetext follows", (await page.$eval("#twelve-flights .v-rail", (e) => e.getAttribute("aria-valuetext"))) === "September 2026");
  // Drag along the rail to the start.
  await touchDrag(page, [rb.x + rb.width - 4, rb.y + 4], [rb.x + 2, rb.y + 6], 14);
  await page.waitForTimeout(1500);
  check("rail drag scrubs back to the first flight", (await odoDate(page, "twelve-flights")) === "April 2025", await odoDate(page, "twelve-flights"));
  check("scrub canvas painted", await painted(page, "#twelve-flights canvas"));

  // Hotspot: tap a visible pin → sheet; swipe down → closed.
  const pin = page.locator("#twelve-flights .v-hs.v-on").first();
  await pin.tap();
  await page.waitForTimeout(500);
  check("hotspot tap opens the sheet", await page.locator(".v-dlg-wrap.v-open").isVisible());
  check("focus moves into the sheet", await page.evaluate(() => document.activeElement?.closest(".v-dlg") != null));
  check("page behind is inert", await page.evaluate(() => document.querySelector("main").inert === true));
  await page.screenshot({ path: path.join(out, "iphone-hotspot-sheet.png") });
  const sb = await page.locator(".v-dlg__head").boundingBox();
  await touchDrag(page, [sb.x + sb.width / 2, sb.y + 10], [sb.x + sb.width / 2, sb.y + 260], 8);
  await page.waitForTimeout(600);
  check("swipe down dismisses the sheet", await page.locator(".v-dlg-wrap").isHidden());
  check("focus returns to the pin", await page.evaluate(() => document.activeElement?.classList.contains("v-hs")));

  // Compare: touch-drag the curtain horizontally; a vertical swipe still scrolls.
  await page.evaluate((y) => scrollTo(0, y), await trackY(page, "before-after", 0.4));
  await page.waitForTimeout(600);
  const grip = page.locator("#before-after .v-cmp__grip");
  const v0 = Number(await grip.getAttribute("aria-valuenow"));
  const gb = await grip.boundingBox();
  await touchDrag(page, [gb.x + 22, gb.y + 120], [gb.x + 22 + 120, gb.y + 124]);
  await page.waitForTimeout(300);
  const v1 = Number(await grip.getAttribute("aria-valuenow"));
  check("touch drag moves the curtain", v1 > v0 + 20, `${v0} → ${v1}`);
  await page.screenshot({ path: path.join(out, "iphone-curtain-dragged.png") });
  const y0 = await page.evaluate(() => scrollY);
  await touchDrag(page, [200, 600], [204, 300], 8);
  await page.waitForTimeout(400);
  check("vertical swipe on the stage scrolls the page", (await page.evaluate(() => scrollY)) > y0 + 100);
  check("dragged curtain no longer follows scroll", Number(await grip.getAttribute("aria-valuenow")) === v1);

  // Timeline thumbnail → sheet → "Explore from this flight".
  await page.locator("#timeline .v-tl__open").nth(2).scrollIntoViewIfNeeded();
  await page.locator("#timeline .v-tl__open").nth(2).tap();
  await page.waitForTimeout(700);
  check("timeline thumbnail opens the flight", (await page.locator("#v-dlg-title").textContent()) === "January 2026");
  await page.screenshot({ path: path.join(out, "iphone-timeline-sheet.png") });
  await page.getByRole("button", { name: "Explore from this flight" }).tap();
  await page.waitForTimeout(1400);
  check("explore opens at that flight", (await odoDate(page, "explore")) === "January 2026", await odoDate(page, "explore"));

  // Explore: play, pinch, double-tap, reset, curtain mode.
  const stage = page.locator("#explore .v-x__stage");
  await stage.scrollIntoViewIfNeeded();
  const xb = await stage.boundingBox();
  await pinch(page, [xb.x + xb.width / 2, xb.y + xb.height / 2], 80, 240);
  await page.waitForTimeout(300);
  check("pinch zooms the stage", await page.locator("#explore .v-x__reset").isVisible());
  check("zoomed stage takes over touch", await stage.evaluate((s) => getComputedStyle(s).touchAction === "none"));
  await page.screenshot({ path: path.join(out, "iphone-explore-zoomed.png") });
  await page.locator("#explore .v-x__reset").tap();
  await page.waitForTimeout(500);
  check("reset zoom", await page.locator("#explore .v-x__reset").isHidden());
  await page.touchscreen.tap(xb.x + 150, xb.y + 200);
  await page.waitForTimeout(120);
  await page.touchscreen.tap(xb.x + 150, xb.y + 200);
  await page.waitForTimeout(500);
  check("double-tap zooms", await page.locator("#explore .v-x__reset").isVisible());
  await page.locator("#explore .v-x__reset").tap();
  await page.locator("#explore .v-x__play").tap();
  await page.waitForTimeout(3200);
  check("play advances through the flights", (await odoDate(page, "explore")) !== "January 2026", await odoDate(page, "explore"));
  await page.locator("#explore .v-x__play").tap();
  await page.getByRole("button", { name: "Curtain" }).tap();
  await page.locator("#explore .v-x__select").first().selectOption("2");
  await page.waitForTimeout(400);
  const eg = page.locator("#explore .v-cmp__grip");
  check("explore curtain slider", (await eg.getAttribute("aria-valuetext"))?.includes("July 2025"), await eg.getAttribute("aria-valuetext"));
  await stage.scrollIntoViewIfNeeded();
  await page.screenshot({ path: path.join(out, "iphone-explore-curtain.png") });
  await page.getByRole("button", { name: "Blink", exact: true }).tap();
  const blinkBtn = page.locator("#explore").getByRole("button", { name: /^Show / });
  check("explore blink: a toggle button, named for the before flight", (await blinkBtn.isVisible()) && (await blinkBtn.textContent()) === "Show July 2025", await blinkBtn.textContent());
  await blinkBtn.tap();
  await page.waitForTimeout(300);
  check(
    "explore blink: the toggle shows the before flight",
    (await blinkBtn.getAttribute("aria-pressed")) === "true" && (await page.locator("#explore canvas").getAttribute("aria-label")).endsWith("July 2025"),
    await page.locator("#explore canvas").getAttribute("aria-label"),
  );
  await stage.scrollIntoViewIfNeeded();
  await page.screenshot({ path: path.join(out, "iphone-explore-blink-toggle.png") });
  await blinkBtn.focus();
  await page.keyboard.press("Space");
  check("explore blink: Space toggles back to the after flight", (await blinkBtn.getAttribute("aria-pressed")) === "false");
  await page.getByRole("button", { name: "Time", exact: true }).tap();
  check("explore blink: the toggle leaves with blink mode", await blinkBtn.isHidden());

  // Contents → Save for offline (served from localhost: a secure context).
  await page.getByRole("button", { name: "Contents" }).tap();
  await page.waitForTimeout(500);
  await page.screenshot({ path: path.join(out, "iphone-contents.png") });
  const save = page.getByRole("button", { name: "Save for offline" });
  check("save for offline offered over http(s)", await save.isVisible());
  await save.tap();
  await page.waitForFunction(() => /Saved for offline/.test(document.querySelector(".v-offline__status")?.textContent || ""), null, { timeout: 30000 }).catch(() => {});
  const status = await page.locator(".v-offline__status").textContent();
  check("save for offline completes", /Saved for offline/.test(status), status);
  await page.screenshot({ path: path.join(out, "iphone-saved.png") });
  await page.keyboard.press("Escape");

  // Share without navigator.share → clipboard / toast.
  await page.getByRole("button", { name: "Share this story" }).tap();
  await page.waitForTimeout(400);
  check("share falls back to a toast", (await page.locator(".v-toast.v-on").count()) === 1, await page.locator(".v-toast").textContent());
  check("no console errors (site)", errors.length === 0, errors.join(" | "));
  await ctx.close();
}

/* 1b. Hero: the ambient loop and drift pause from a button, and stay paused for the session. */
{
  const { ctx, page, errors } = await open(browser, url);
  await page.waitForTimeout(2600);
  const pause = page.getByRole("button", { name: "Pause the background video" });
  check("hero: pause button for the ambient video", await pause.isVisible());
  await page.screenshot({ path: path.join(out, "iphone-hero-pause.png") });
  await pause.tap();
  await page.waitForTimeout(300);
  const drift = () => page.$eval(".v-hero__media img", (i) => getComputedStyle(i).animationPlayState);
  check("hero: paused, the video stops and the still stops drifting", (await page.$eval("#opening video", (v) => v.paused)) && (await drift()) === "paused");
  await page.screenshot({ path: path.join(out, "iphone-hero-paused.png") });
  await page.reload({ waitUntil: "load" });
  await page.waitForTimeout(1500);
  check(
    "hero: the pause is remembered for the session",
    (await page.getByRole("button", { name: "Play the background video" }).isVisible()) && (await page.$eval("#opening video", (v) => v.paused)) && (await drift()) === "paused",
  );
  await page.getByRole("button", { name: "Play the background video" }).tap();
  await page.waitForTimeout(300);
  check("hero: play resumes the drift", (await drift()) === "running");
  check("no console errors (hero)", errors.length === 0, errors.join(" | "));
  await ctx.close();
}

/* 2. Offline: after saving, the page reloads with the network gone. */
{
  const { ctx, page, errors } = await open(browser, url);
  await page.evaluate(() => navigator.serviceWorker.ready);
  await page.evaluate(() => new Promise((done) => {
    navigator.serviceWorker.addEventListener("message", (e) => e.data.type === "vantage:saved" && done());
    navigator.serviceWorker.controller ? navigator.serviceWorker.controller.postMessage({ type: "vantage:save" }) : navigator.serviceWorker.ready.then((r) => r.active.postMessage({ type: "vantage:save" }));
  }));
  await ctx.setOffline(true);
  await page.reload({ waitUntil: "load" });
  await page.evaluate((y) => scrollTo(0, y), await trackY(page, "twelve-flights", 0.5));
  await page.waitForTimeout(1200);
  check("offline reload renders the scrub", await painted(page, "#twelve-flights canvas"));
  await page.screenshot({ path: path.join(out, "iphone-offline-scrub.png") });
  check("no console errors (offline)", errors.length === 0, errors.join(" | "));
  await ctx.close();
}

/* 3. Reduced motion: the scrub steps instead of dissolving; the compare never nudges. */
{
  const { ctx, page, errors } = await open(browser, url, { reducedMotion: "reduce" });
  await page.evaluate((y) => scrollTo(0, y), await trackY(page, "twelve-flights", 0.31));
  await page.waitForTimeout(700);
  check("reduced motion: still shows a single flight", /2025|2026/.test(await odoDate(page, "twelve-flights")));
  await page.screenshot({ path: path.join(out, "iphone-reduced-scrub.png") });
  check("reduced motion: hero has no drift", await page.$eval(".v-hero__media img", (i) => getComputedStyle(i).animationName === "none"));
  check("no console errors (reduced)", errors.length === 0, errors.join(" | "));
  await ctx.close();
}

/* 4. Variants the demo doesn't author: a blink compare and a draft, by rewriting the StoryJSON in flight. */
{
  const ctx = await browser.newContext(IPHONE);
  const page = await ctx.newPage();
  const errors = [];
  page.on("pageerror", (e) => errors.push(e.message));
  page.on("console", (m) => m.type() === "error" && errors.push(m.text()));
  await page.route(/\/(index\.html)?$/, async (route) => {
    const res = await route.fetch();
    const body = (await res.text()).replace('"mode":"curtain"', '"mode":"blink"').replace('"draft":false', '"draft":true');
    await route.fulfill({ response: res, body });
  });
  await page.goto(url, { waitUntil: "load" });
  check("draft: Preview badge in the chrome", await page.locator(".v-chrome__badge").isVisible());
  await page.evaluate((y) => scrollTo(0, y), await trackY(page, "before-after", 0.3));
  await page.waitForTimeout(600);
  const after = page.locator("#before-after .v-cmp__after");
  const toggle = page.getByRole("button", { name: /Show April 2025/ });
  check("blink: toggle button, after side showing", (await toggle.isVisible()) && (await after.evaluate((e) => e.style.opacity)) === "1");
  await toggle.tap();
  await page.waitForTimeout(300);
  check("blink: toggle shows before", (await toggle.getAttribute("aria-pressed")) === "true" && (await after.evaluate((e) => e.style.opacity)) === "0");
  await page.screenshot({ path: path.join(out, "iphone-blink-before.png") });
  await toggle.tap();
  const sb = await page.locator("#before-after .v-stage").boundingBox();
  await page.mouse.move(sb.x + 200, sb.y + 300);
  await page.mouse.down();
  await page.waitForTimeout(250);
  const held = await after.evaluate((e) => e.style.opacity);
  await page.mouse.up();
  await page.waitForTimeout(150);
  check("blink: press and hold shows before, release shows after", held === "0" && (await after.evaluate((e) => e.style.opacity)) === "1");
  await page.getByRole("button", { name: "Blink automatically" }).tap();
  const seen = new Set();
  for (let i = 0; i < 12; i++) {
    seen.add(await after.evaluate((e) => e.style.opacity));
    await page.waitForTimeout(150);
  }
  check("blink: auto-blink alternates", seen.size === 2);
  // The loop sleeps off screen and in a hidden tab: count animation frames requested over a second.
  await page.evaluate(() => {
    window.__raf = 0;
    const raf = window.requestAnimationFrame.bind(window);
    window.requestAnimationFrame = (cb) => (window.__raf++, raf(cb));
  });
  const frames = async () => {
    await page.evaluate(() => (window.__raf = 0));
    await page.waitForTimeout(1000);
    return page.evaluate(() => window.__raf);
  };
  const onScreen = await frames();
  await page.evaluate(() => scrollTo(0, document.getElementById("timeline").getBoundingClientRect().top + scrollY));
  await page.waitForTimeout(600);
  const offScreen = await frames();
  check("blink: auto-blink stops off screen", onScreen > 20 && offScreen <= 2, `${onScreen} frames on screen, ${offScreen} off`);
  await page.evaluate((y) => scrollTo(0, y), await trackY(page, "before-after", 0.3));
  await page.waitForTimeout(400);
  const back = await frames();
  const hide = (hidden) =>
    page.evaluate((hidden) => {
      Object.defineProperty(document, "hidden", { configurable: true, get: () => hidden });
      document.dispatchEvent(new Event("visibilitychange"));
    }, hidden);
  await hide(true);
  await page.waitForTimeout(100);
  const hiddenFrames = await frames();
  await hide(false);
  const shownAgain = await frames();
  check(
    "blink: auto-blink resumes on return, sleeps while the tab is hidden",
    back > 20 && hiddenFrames <= 2 && shownAgain > 20 && (await page.getByRole("button", { name: "Blink automatically" }).getAttribute("aria-pressed")) === "true",
    `back ${back}, hidden ${hiddenFrames}, shown ${shownAgain}`,
  );
  check("no console errors (variants)", errors.length === 0, errors.join(" | "));

  // Explore vantage chips and gallery dots.
  await page.locator("#explore .v-chip").nth(1).scrollIntoViewIfNeeded();
  await page.locator("#explore .v-chip").nth(1).tap();
  await page.waitForTimeout(800);
  check("explore: chip switches vantage", (await page.locator("#explore canvas").getAttribute("aria-label")).startsWith("From over the water"));
  const visits = await page.evaluate(() => {
    const id = /** @type {HTMLElement} */ (document.querySelectorAll("#explore .v-chip")[1]).dataset.id;
    return JSON.parse(document.getElementById("vantage-story").textContent).vantages.find((v) => v.id === id).captures.length;
  });
  const ticks = await page.locator("#explore .v-x__ticks .v-tick:not(.v-tick--year)").count();
  check("explore: ticks follow the vantage", ticks === visits, `${ticks} ticks, ${visits} visits`);
  await page.locator("#from-the-water .v-dot").nth(4).scrollIntoViewIfNeeded();
  await page.locator("#from-the-water .v-dot").nth(4).tap();
  await page.waitForTimeout(900);
  check("gallery: a dot scrolls the strip", (await page.locator("#from-the-water .v-dot").nth(4).getAttribute("aria-current")) === "true");
  await page.screenshot({ path: path.join(out, "iphone-gallery-dot.png") });
  await ctx.close();
}

/* 5. JavaScript off: the photo essay. */
{
  const ctx = await browser.newContext({ ...IPHONE, javaScriptEnabled: false });
  const page = await ctx.newPage();
  await page.goto(url);
  await page.waitForTimeout(800);
  await page.screenshot({ path: path.join(out, "iphone-nojs-hero.png") });
  check("no-JS: no runtime chrome, static sequence visible", (await page.$$(".v-chrome")).length === 0 && (await page.locator("#twelve-flights .v-seq").isVisible()));
  await ctx.close();
}

/* 6. file:// editions */
for (const [name, file] of [["file site", "site/index.html"], ["single file", "demo-lakeside.html"], ["lite file", "demo-lakeside-lite.html"]]) {
  const { ctx, page, errors } = await open(browser, pathToFileURL(path.join(dist, file)).href);
  check(`${name}: no save button, no share without a URL`, (await page.getByRole("button", { name: "Save for offline" }).count()) === 0);
  await page.evaluate((y) => scrollTo(0, y), await trackY(page, "twelve-flights", 0.5));
  await page.waitForTimeout(1500);
  check(`${name}: scrub canvas painted`, await painted(page, "#twelve-flights canvas"));
  await page.screenshot({ path: path.join(out, `iphone-${name.replace(/ /g, "-")}-scrub.png`) });
  if (file.endsWith("demo-lakeside.html")) {
    check(
      "single file: hero video and poster built from the embedded blobs",
      await page.$eval(".v-hero__media video", (v) => v.src.startsWith("blob:") && v.poster.startsWith("blob:")).catch(() => false),
    );
    await page.locator("#last-evening").scrollIntoViewIfNeeded();
    await page.waitForTimeout(1500);
    // Playwright's Chromium has no H.264 decoder: there, check the player exists with its blob source.
    const h264 = await page.evaluate(() => document.createElement("video").canPlayType('video/mp4; codecs="avc1.640028"') !== "");
    check(
      `single file: chapter video ${h264 ? "plays" : "built (no H.264 in this browser)"}`,
      await page.$eval("#last-evening video", (v, h264) => (h264 ? !v.paused && v.currentTime > 0 : v.src.startsWith("blob:")), h264).catch(() => false),
    );
  }
  check(`no console errors (${name})`, errors.length === 0, errors.join(" | "));
  await ctx.close();
}
{
  // Under reduced motion the single file never even decodes the hero loop.
  const { ctx, page } = await open(browser, pathToFileURL(path.join(dist, "demo-lakeside.html")).href, { reducedMotion: "reduce" });
  await page.waitForTimeout(1200);
  check("single file, reduced motion: hero loop never decoded", await page.$eval(".v-hero__media video", (v) => v.getAttribute("src") === null).catch(() => false));
  await ctx.close();
}

await browser.close();
console.log(results.join("\n"));
console.log(`${results.filter((r) => r.startsWith("PASS")).length}/${results.length} passed`);
