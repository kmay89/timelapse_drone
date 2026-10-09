// The hosted story with JavaScript on: it loads cleanly, renders every chapter in the StoryJSON, every
// image it shows actually loads, and nothing scrolls sideways on a phone.
import { expect, test } from "@playwright/test";
import {
  chapterCheckpoints,
  escapeRe,
  expectNoSidewaysScroll,
  readStory,
  scrollThrough,
  section,
  watchErrors,
} from "./support.mjs";

test.describe("smoke", () => {
  test("renders every StoryJSON chapter in order", async ({ page }) => {
    await page.goto("./");
    const story = await readStory(page);
    await expect(page).toHaveTitle(new RegExp(`^${escapeRe(story.meta.title)}`));
    await expect(page.locator("html")).toHaveClass(/\bjs\b/);
    const ids = await page.locator("main section.v-chapter[data-type]").evaluateAll((els) => els.map((e) => e.id));
    expect(ids).toEqual(story.chapters.map((ch) => ch.id));
    for (const ch of story.chapters) {
      await expect(section(page, ch)).toHaveAttribute("data-type", ch.type);
    }
  });

  test("scrolling the whole story: no errors, every image on the way loads", async ({ page }) => {
    const errors = watchErrors(page);
    await page.goto("./");
    const broken = await scrollThrough(page);
    expect(broken, "images that never loaded").toEqual([]);
    expect(errors).toEqual([]);
  });

  test("no sideways scrolling", async ({ page }) => {
    await page.goto("./");
    await scrollThrough(page); // runtime layout settles as chapters activate
    await expectNoSidewaysScroll(page);
  });

  test("chapter checkpoint screenshots", async ({ page }, testInfo) => {
    await page.goto("./");
    const story = await readStory(page);
    await chapterCheckpoints(page, testInfo, story, "js");
  });
});
