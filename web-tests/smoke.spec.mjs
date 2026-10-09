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

  test("stats: figures fill their rows evenly and never run into the next column", async ({ page }) => {
    await page.goto("./");
    const story = await readStory(page);
    const chs = story.chapters.filter((c) => c.type === "stats" && c.items.length > 1);
    test.skip(!chs.length, "this story has no stats chapter with two or more figures");
    const width = page.viewportSize().width;
    for (const w of [width, 768]) {
      await page.setViewportSize({ width: w, height: page.viewportSize().height });
      for (const ch of chs) {
        const list = section(page, ch).getByRole("list").last(); // after any list in the lede
        await list.scrollIntoViewIfNeeded();
        await page.waitForTimeout(300); // let the figures count up and the layout settle
        const { rows, overflow } = await list.evaluate((ul) => {
          // Layout boxes (offset*), not painted ones: arrival reveals slide figures in with a transform.
          const cells = [...ul.children].map((li) => {
            const r = li.getBoundingClientRect();
            let right = r.left;
            for (const n of li.querySelectorAll("*")) right = Math.max(right, n.getBoundingClientRect().right);
            return { top: li.offsetTop, left: li.offsetLeft, right: li.offsetLeft + li.offsetWidth, over: right - r.right };
          });
          const tops = [...new Set(cells.map((c) => c.top))];
          const [left, right] = [ul.offsetLeft, ul.offsetLeft + ul.offsetWidth];
          // A row is full when its cells reach from the list's left edge to its right edge.
          const rows = tops.map((t) => {
            const row = cells.filter((c) => c.top === t);
            return Math.min(...row.map((c) => c.left)) - left < 2 && right - Math.max(...row.map((c) => c.right)) < 2;
          });
          return { rows, overflow: cells.map((c) => Math.round(c.over)).filter((o) => o > 0) };
        });
        expect(rows.every(Boolean), `${ch.id} at ${w}px: a row that doesn't reach across (${JSON.stringify(rows)})`).toBe(true);
        expect(overflow, `${ch.id} at ${w}px: figures wider than their column`).toEqual([]);
      }
    }
  });

  test("chapter checkpoint screenshots", async ({ page }, testInfo) => {
    await page.goto("./");
    const story = await readStory(page);
    await chapterCheckpoints(page, testInfo, story, "js");
  });
});
