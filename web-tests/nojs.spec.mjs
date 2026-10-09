// JavaScript off: how iOS Quick Look shows a downloaded .html file (Files, Mail, AirDrop). Every edition
// must still read as a complete photo essay: headline, hero, every chapter's words and pictures.
import { expect, test } from "@playwright/test";
import {
  SLUG,
  chapterCheckpoints,
  distUrl,
  effectiveOpacity,
  expectNoSidewaysScroll,
  readStory,
  scrollThrough,
  section,
  watchErrors,
} from "./support.mjs";

const EDITIONS = [
  ["site", "./"],
  ["single", distUrl(`${SLUG}.html`)],
  ["lite", distUrl(`${SLUG}-lite.html`)],
];

test.use({ javaScriptEnabled: false });

/** Visible at full strength once centred in the viewport (no reveal left waiting for a script). */
async function expectReadable(locator, text) {
  if (text) await expect(locator).toHaveText(text);
  await locator.evaluate((el) => el.scrollIntoView({ block: "center" }));
  await expect(locator).toBeVisible();
  await expect.poll(() => effectiveOpacity(locator), { message: "text is invisible without JS" }).toBeGreaterThan(0.9);
}

for (const [edition, url] of EDITIONS) {
  test.describe(`no-JS ${edition}`, () => {
    test("reads as a photo essay: headline, hero and every chapter visible", async ({ page }) => {
      const errors = watchErrors(page);
      await page.goto(url);
      const story = await readStory(page);
      await expect(page.locator("html")).toHaveClass(/\bno-js\b/);
      // The opening hero's title is the <h1>; a story that opens otherwise has a hidden <h1> with its title.
      const lead = story.chapters[0]?.type === "hero" ? story.chapters[0] : null;
      await expect(page.locator("h1")).toHaveText(lead?.title || story.meta.title);
      for (const ch of story.chapters) {
        await expect(section(page, ch)).toBeVisible();
        if (ch.title) await expectReadable(page.locator(`#${ch.id}-title`), ch.title);
      }
      for (const prose of await page.locator(".v-text .v-prose").all()) await expectReadable(prose);
      expect(errors).toEqual([]);
    });

    test("hero image is visible and loaded", async ({ page }) => {
      await page.goto(url);
      const hero = page.locator('section[data-type="hero"] img:not(.v-logo)').first();
      await expect(hero).toBeVisible();
      await expect.poll(() => hero.evaluate((i) => i.complete && i.naturalWidth > 0)).toBe(true);
    });

    test("every image has alt text; figures' images are described", async ({ page }) => {
      await page.goto(url);
      const missing = await page.locator("img:not([alt])").evaluateAll((els) => els.map((i) => i.src.slice(0, 80)));
      expect(missing, "<img> without an alt attribute").toEqual([]);
      const blank = await page
        .locator("figure img")
        .evaluateAll((els) => els.filter((i) => !i.alt.trim()).map((i) => i.src.slice(0, 80)));
      expect(blank, "figure images with empty alt").toEqual([]);
    });

    test("images on the way load; no sideways scrolling", async ({ page }) => {
      await page.goto(url);
      expect(await scrollThrough(page), "images that never loaded").toEqual([]);
      await expectNoSidewaysScroll(page);
    });

    test("chapter checkpoint screenshots", async ({ page }, testInfo) => {
      await page.goto(url);
      await chapterCheckpoints(page, testInfo, await readStory(page), `nojs-${edition}`);
    });
  });
}
