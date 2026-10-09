// Design-loop screenshots of the runtime (not a test): key states of every chapter on an iPhone 15 Pro
// and a 1440×900 desktop, plus console errors.
//   node web-tests/dev/shoot.mjs <url> <outdir> [--only=iphone|desktop] [--reduced]
import { chromium } from "@playwright/test";
import fs from "node:fs";
import path from "node:path";

const [url, out, ...flags] = process.argv.slice(2);
const only = flags.find((f) => f.startsWith("--only="))?.slice(7);
const reduced = flags.includes("--reduced");
const DEVICES = {
  iphone: { viewport: { width: 393, height: 852 }, deviceScaleFactor: 3, isMobile: true, hasTouch: true },
  desktop: { viewport: { width: 1440, height: 900 }, deviceScaleFactor: 1 },
};
fs.mkdirSync(out, { recursive: true });

/** Page-space geometry of a chapter's pinned track (or the section). */
const geo = (page, id) =>
  page.evaluate((id) => {
    const sec = document.getElementById(id);
    const el = sec.querySelector(":scope > .v-track") || sec;
    const r = el.getBoundingClientRect();
    const stage = el.querySelector(".v-stage");
    return { top: r.top + scrollY, height: r.height, stageH: stage ? stage.offsetHeight : innerHeight, secTop: sec.getBoundingClientRect().top + scrollY };
  }, id);

const settle = async (page, ms = 700) => {
  await page.waitForTimeout(ms);
  await page.evaluate(() => document.fonts.ready);
};

const browser = await chromium.launch();
const errors = [];
for (const [name, opts] of Object.entries(DEVICES)) {
  if (only && only !== name) continue;
  const ctx = await browser.newContext({ ...opts, reducedMotion: reduced ? "reduce" : "no-preference" });
  const page = await ctx.newPage();
  page.on("console", (m) => m.type() === "error" && errors.push(`${name} console: ${m.text()}`));
  page.on("pageerror", (e) => errors.push(`${name} pageerror: ${e.message}`));
  const shot = (label) => page.screenshot({ path: path.join(out, `${name}-${label}.png`) });
  await page.goto(url, { waitUntil: "load" });
  const story = await page.evaluate(() => JSON.parse(document.getElementById("vantage-story").textContent));

  await settle(page, 2600); // hero reveal + scroll cue
  await shot("00-hero");
  for (const [i, ch] of story.chapters.entries()) {
    const n = String(i).padStart(2, "0");
    const g = await geo(page, ch.id);
    const at = async (p, label, wait) => {
      await page.evaluate((y) => scrollTo(0, y), g.top + p * (g.height - g.stageH));
      await settle(page, wait);
      await shot(`${n}-${ch.id}-${label}`);
    };
    if (ch.type === "hero") {
      await page.evaluate((y) => scrollTo(0, y), g.secTop + g.height - opts.viewport.height * 0.6);
      await settle(page);
      await shot(`${n}-${ch.id}-standfirst`);
    } else if (ch.type === "scrub") {
      await page.evaluate((y) => scrollTo(0, y), g.secTop);
      await settle(page);
      await shot(`${n}-${ch.id}-head`);
      for (const p of [0, 0.25, 0.5, 0.75, 1]) await at(p, `p${p * 100}`, 900);
    } else if (ch.type === "compare") {
      for (const p of [0, 0.5, 1]) await at(p, `p${p * 100}`, 1100);
    } else {
      await page.evaluate((y) => scrollTo(0, y), g.secTop);
      await settle(page, 1200);
      await shot(`${n}-${ch.id}`);
      if (g.height > opts.viewport.height * 1.3) {
        await page.evaluate((y) => scrollTo(0, y), g.secTop + opts.viewport.height * 0.85);
        await settle(page, 1200);
        await shot(`${n}-${ch.id}-b`);
      }
    }
  }
  await ctx.close();
}
await browser.close();
console.log(errors.length ? errors.join("\n") : "no console errors");
