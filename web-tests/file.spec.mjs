// Opening the deliverables straight from disk (file://), the way a desktop recipient does: the site's
// index.html, the unzipped offline package and both single files. No errors, every chapter, every image
// on the way, and nothing fetched from the network (no service worker, no CDN, no JSON fetch).
import { expect, test } from "@playwright/test";
import { SLUG, distUrl, readStory, scrollThrough, section, unzipOffline, watchErrors, watchNetwork } from "./support.mjs";

const EDITIONS = [
  ["site index.html", "site", () => distUrl("site/index.html")],
  ["offline zip", "offline", (testInfo) => unzipOffline(testInfo.outputPath("offline"))],
  ["single file", "single", () => distUrl(`${SLUG}.html`)],
  ["lite single file", "lite", () => distUrl(`${SLUG}-lite.html`)],
];

for (const [name, edition, locate] of EDITIONS) {
  test(`file:// ${name}: loads with zero errors and no network`, async ({ page }, testInfo) => {
    const errors = watchErrors(page);
    const network = watchNetwork(page);
    await page.goto(locate(testInfo));
    await expect(page.locator("html")).toHaveAttribute("data-edition", edition);
    await expect(page.locator("html")).toHaveClass(/\bjs\b/);
    const story = await readStory(page);
    for (const ch of story.chapters) await expect(section(page, ch)).toHaveCount(1);
    expect(await scrollThrough(page), "images that never loaded").toEqual([]);
    expect(errors).toEqual([]);
    expect(network, "requests that left the machine").toEqual([]);
  });
}
