// Shared helpers for the story specs. Every expectation is derived from the StoryJSON embedded in the
// page (<script id="vantage-story">), so the suite runs unchanged against any built story.
import { expect } from "@playwright/test";
import { execFileSync } from "node:child_process";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const here = path.dirname(fileURLToPath(import.meta.url));
export const SLUG = process.env.VANTAGE_STORY || "demo-lakeside";
const DIST = path.join(path.resolve(here, "..", process.env.VANTAGE_DIST || "dist"), SLUG);

/** file:// URL of a file under dist/<slug>/. */
export const distUrl = (rel) => pathToFileURL(path.join(DIST, rel)).href;

/** Unzips dist/<slug>/<slug>-offline.zip into `dir`; returns the file:// URL of its index.html. */
export function unzipOffline(dir) {
  fs.mkdirSync(dir, { recursive: true });
  execFileSync("python3", ["-m", "zipfile", "-e", path.join(DIST, `${SLUG}-offline.zip`), dir]);
  return pathToFileURL(path.join(dir, SLUG, "index.html")).href;
}

export const escapeRe = (s) => s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");

/** Console errors, uncaught exceptions, HTTP errors and failed requests seen by `page`. */
export function watchErrors(page) {
  const errors = [];
  page.on("console", (msg) => msg.type() === "error" && errors.push(`console: ${msg.text()}`));
  page.on("pageerror", (err) => errors.push(`pageerror: ${err.message}`));
  page.on("response", (res) => res.status() >= 400 && errors.push(`HTTP ${res.status()} ${res.url()}`));
  page.on("requestfailed", (req) => {
    const why = req.failure()?.errorText ?? "";
    // Media elements abort and re-issue range requests as a matter of course.
    if (!/abort|cancel/i.test(why)) errors.push(`request failed: ${req.url().slice(0, 160)} ${why}`);
  });
  return errors;
}

/** URLs of every request that leaves the machine (anything but file:, data: and blob:). */
export function watchNetwork(page) {
  const urls = [];
  page.on("request", (req) => /^(file|data|blob):/.test(req.url()) || urls.push(req.url()));
  return urls;
}

export const readStory = (page) =>
  page.locator("#vantage-story").evaluate((el) => JSON.parse(el.textContent));

export const section = (page, ch) => page.locator(`section#${ch.id}`);

export const vantageOf = (story, ch) => story.vantages.find((v) => v.id === ch.vantage);

/** Page-space top and height of an element, plus the viewport height. */
export const geometry = (locator) =>
  locator.evaluate((el) => ({
    top: el.getBoundingClientRect().top + scrollY,
    height: el.offsetHeight,
    vh: innerHeight,
  }));

/**
 * True once `predicate` (run in the page) holds, polling from Node: with JavaScript off the page runs
 * neither timers nor requestAnimationFrame, so page.waitForFunction() would never re-check.
 */
async function until(page, predicate, arg, timeout = 15_000) {
  for (const end = Date.now() + timeout; ; await page.waitForTimeout(100)) {
    if (await page.evaluate(predicate, arg)) return true;
    if (Date.now() > end) return false;
  }
}

/**
 * Scrolls to y, lets scroll handlers paint, and waits (up to 15 s) for the images in the viewport.
 * Also (re)defines window.onScreen(el) for the other helpers.
 */
export async function scrollToY(page, y) {
  await page.evaluate((top) => {
    window.onScreen = (el) => {
      const r = el.getBoundingClientRect();
      return r.width > 0 && r.height > 0 && r.bottom > 0 && r.right > 0 && r.top < innerHeight &&
        r.left < innerWidth && getComputedStyle(el).visibility !== "hidden";
    };
    scrollTo(0, top);
  }, Math.max(0, Math.round(y)));
  await page.waitForTimeout(120);
  await until(page, () => [...document.images].every((i) => i.complete || !onScreen(i))); // laggards: brokenImages()
}

/** On-screen images that are still loading or loaded without pixels, as their URLs. */
const brokenImages = (page) =>
  page.evaluate(() =>
    [...document.images]
      .filter((i) => onScreen(i) && !(i.complete && i.naturalWidth > 0))
      .map((i) => (i.currentSrc || i.src).slice(0, 120)),
  );

/** Scrolls the whole story top to bottom; returns every image that failed to load on the way. */
export async function scrollThrough(page) {
  const broken = new Set();
  for (let y = 0; ; ) {
    await scrollToY(page, y);
    for (const src of await brokenImages(page)) broken.add(src);
    const { height, vh } = await page.evaluate(() => ({ height: document.documentElement.scrollHeight, vh: innerHeight }));
    if (y + vh >= height) break;
    y += vh * 0.8;
  }
  return [...broken];
}

/** Fails when the page can scroll sideways (the classic phone bug). */
export async function expectNoSidewaysScroll(page) {
  const { scrollWidth, width } = await page.evaluate(() => ({
    scrollWidth: Math.max(document.documentElement.scrollWidth, document.body.scrollWidth),
    width: innerWidth,
  }));
  expect(scrollWidth, `document is ${scrollWidth}px wide in a ${width}px viewport`).toBeLessThanOrEqual(width + 1);
}

/** Lowest opacity along an element's ancestor chain: text a JS reveal never revealed reads ~0. */
export const effectiveOpacity = (locator) =>
  locator.evaluate((el) => {
    let o = 1;
    for (let n = el; n instanceof Element; n = n.parentElement) o *= Number(getComputedStyle(n).opacity);
    return o;
  });

/** Screenshot into .results/test-results/shots/<project>/<name>.jpg and attach it to the report. */
async function checkpoint(page, testInfo, name) {
  const file = path.join(testInfo.project.outputDir, "shots", testInfo.project.name, `${name}.jpg`);
  await until(page, () => document.fonts.status === "loaded");
  await page.screenshot({ path: file, type: "jpeg", quality: 80 }); // ~5× smaller than PNG at DPR 3
  await testInfo.attach(name, { path: file, contentType: "image/jpeg" });
}

/** Screenshots every chapter: its top, and for tall (pinned) chapters the middle and the end too. */
export async function chapterCheckpoints(page, testInfo, story, prefix) {
  for (const [i, ch] of story.chapters.entries()) {
    const { top, height, vh } = await geometry(section(page, ch));
    const stops = Math.min(3, Math.max(1, Math.ceil(height / vh)));
    for (let k = 0; k < stops; k++) {
      await scrollToY(page, top + (stops > 1 ? (k * (height - vh)) / (stops - 1) : 0));
      const name = `${prefix}-${String(i).padStart(2, "0")}-${ch.id}${stops > 1 ? `-${k}` : ""}`;
      await checkpoint(page, testInfo, name);
    }
  }
}

/**
 * The date a scrub chapter currently shows, as one of `labels`: the polite live region or the
 * aria-current tick when the runtime provides one, else the first on-screen element whose whole text
 * is a capture label.
 */
export const currentDate = (locator, labels) =>
  locator.evaluate((root, labels) => {
    const text = (n) => n.textContent.replace(/\s+/g, " ").trim();
    for (const n of root.querySelectorAll('[aria-live], [aria-current]:not([aria-current="false"])')) {
      const hit = labels.find((l) => text(n).includes(l));
      if (hit) return hit;
    }
    for (const n of root.querySelectorAll("*")) {
      if (labels.includes(text(n)) && onScreen(n) && Number(getComputedStyle(n).opacity) > 0.05) return text(n);
    }
    return null;
  }, labels);
