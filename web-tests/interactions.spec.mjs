// The runtime's enhancements, tested through roles and ARIA (docs/ARCHITECTURE.md "Runtime principles",
// docs/BLUEPRINT.md "Front-end decisions"); web-tests/README.md lists the DOM contract assumed here.
// The controls are tested twice, with default motion and with prefers-reduced-motion, which must keep
// every control working (only automatic motion goes away).
import { expect, test } from "@playwright/test";
import {
  chapterCheckpoints,
  currentDate,
  escapeRe,
  geometry,
  readStory,
  scrollToY,
  section,
  vantageOf,
  watchErrors,
} from "./support.mjs";

/** Loads the story; returns it and its first chapter of `type` (skips the test when there is none). */
async function open(page, type, where = () => true) {
  await page.goto("./");
  const story = await readStory(page);
  const ch = story.chapters.find((c) => c.type === type && where(c));
  test.skip(!ch, `this story has no matching ${type} chapter`);
  return { story, ch };
}

const curtain = (c) => c.mode !== "blink";
const valueOf = async (slider) => Number(await slider.getAttribute("aria-valuenow"));

/** aria-valuenow once it stops changing (the curtain may ease to its new position). */
async function settled(slider) {
  for (let last = await valueOf(slider), i = 0; i < 20; i++) {
    await slider.page().waitForTimeout(80);
    const now = await valueOf(slider);
    if (now === last) return now;
    last = now;
  }
  return valueOf(slider);
}

function controlTests() {
  test("scrub: the date label follows the scroll from the first to the last capture", async ({ page }) => {
    const { story, ch } = await open(page, "scrub");
    const labels = vantageOf(story, ch).captures.map((c) => c.label);
    const sec = section(page, ch);
    const { top, height, vh } = await geometry(sec);
    await scrollToY(page, top + 1);
    await expect.poll(() => currentDate(sec, labels)).toBe(labels[ch.from]);
    await scrollToY(page, top + height - vh - 1);
    await expect.poll(() => currentDate(sec, labels)).toBe(labels[ch.to]);
  });

  test("compare: the curtain is a keyboard slider with date text", async ({ page }) => {
    const { ch } = await open(page, "compare", curtain);
    const slider = section(page, ch).getByRole("slider").first();
    await expect(slider).toBeVisible();
    await slider.scrollIntoViewIfNeeded();
    await expect(slider).toHaveAttribute("aria-valuenow", /\d/); // enhanced by the runtime
    await expect(slider).toHaveAttribute("aria-valuemin", "0");
    await expect(slider).toHaveAttribute("aria-valuemax", "100");
    await slider.focus();
    await page.keyboard.press("Home");
    await expect(slider).toHaveAttribute("aria-valuenow", "0");
    const atStart = await slider.getAttribute("aria-valuetext");
    await page.keyboard.press("End");
    await expect(slider).toHaveAttribute("aria-valuenow", "100");
    await expect.poll(() => slider.getAttribute("aria-valuetext"), { message: "aria-valuetext names the dates" }).not.toBe(atStart);
    for (const [key, direction] of [["ArrowLeft", -1], ["PageDown", -1], ["ArrowRight", 1]]) {
      const from = await settled(slider);
      await page.keyboard.press(key);
      await expect.poll(async () => Math.sign((await settled(slider)) - from), { message: key }).toBe(direction);
    }
  });

  test("compare: dragging the curtain moves it", async ({ page }) => {
    const { ch } = await open(page, "compare", curtain);
    const slider = section(page, ch).getByRole("slider").first();
    await expect(slider).toBeVisible();
    await slider.scrollIntoViewIfNeeded();
    await expect(slider).toHaveAttribute("aria-valuenow", /\d/);
    const before = await settled(slider);
    const box = await slider.boundingBox();
    // Grab the handle: the slider is either the handle itself or the whole stage.
    const x = box.width > 88 ? box.x + (box.width * before) / 100 : box.x + box.width / 2;
    const y = box.y + box.height / 2;
    const dx = (before > 50 ? -1 : 1) * Math.min(160, page.viewportSize().width / 3);
    await page.mouse.move(x, y);
    await page.mouse.down();
    await page.mouse.move(x + dx / 2, y, { steps: 4 });
    await page.mouse.move(x + dx, y, { steps: 4 });
    await page.mouse.up();
    await expect.poll(async () => Math.sign((await settled(slider)) - before)).toBe(Math.sign(dx));
  });

  test("compare: scrolling through the steps sweeps the curtain", async ({ page }) => {
    const { ch } = await open(page, "compare", (c) => curtain(c) && c.steps.filter((s) => s.split != null).length > 1);
    const splits = ch.steps.filter((s) => s.split != null).map((s) => s.split * 100);
    const sec = section(page, ch);
    const slider = sec.getByRole("slider").first();
    await expect(slider).toBeVisible();
    const { top, height, vh } = await geometry(sec);
    await scrollToY(page, top + 1);
    const start = await settled(slider);
    await scrollToY(page, top + height - vh - 1);
    const travel = Math.abs(splits.at(-1) - splits[0]);
    await expect.poll(async () => Math.abs((await valueOf(slider)) - start)).toBeGreaterThanOrEqual(travel / 2);
  });

  test("hotspot: activating a pin opens a sheet that Escape closes", async ({ page }) => {
    const { ch } = await open(page, "compare", (c) => c.hotspots.length > 0);
    const spot = ch.hotspots.find((h) => h.from <= ch.after && ch.after <= h.to) ?? ch.hotspots[0];
    const pin = section(page, ch).getByRole("button", { name: new RegExp(escapeRe(spot.label), "i") }).first();
    await expect(pin).toBeVisible();
    await pin.scrollIntoViewIfNeeded();
    await pin.focus();
    await page.keyboard.press("Enter");
    const sheet = page.getByRole("dialog").filter({ hasText: spot.label });
    await expect(sheet).toBeVisible();
    await page.keyboard.press("Escape");
    await expect(sheet).toBeHidden();
  });

  test("chapter index: opens, lists the chapters, and jumps to one", async ({ page }) => {
    await page.goto("./");
    const story = await readStory(page);
    const titled = story.chapters.filter((c) => c.title && c.type !== "hero");
    test.skip(titled.length < 2, "fewer than two titled chapters");
    await section(page, story.chapters[1]).scrollIntoViewIfNeeded();
    await page.getByRole("button", { name: /chapters|contents|index/i }).first().click();
    for (const ch of titled) await expect(page.getByRole("link", { name: ch.title }).first()).toBeVisible();
    const target = titled.at(-1);
    await page.getByRole("link", { name: target.title }).first().click();
    await expect(section(page, target)).toBeInViewport();
  });
}

test.describe("interactions", () => {
  test("scrub: a pinned <canvas> stage holds the screen while the chapter scrolls", async ({ page }) => {
    const { ch } = await open(page, "scrub");
    const { top, height, vh } = await geometry(section(page, ch));
    const canvas = section(page, ch).locator("canvas").first();
    const tops = [];
    for (const p of [0.3, 0.7]) {
      await scrollToY(page, top + p * (height - vh));
      await expect(canvas).toBeInViewport({ ratio: 0.9 });
      tops.push(await canvas.evaluate((c) => c.getBoundingClientRect().top));
    }
    expect(Math.abs(tops[0] - tops[1]), "the stage moved: it is not pinned").toBeLessThan(2);
  });

  controlTests();
});

test.describe("interactions with reduced motion", () => {
  test.use({ reducedMotion: "reduce" });

  test("no errors, every chapter renders; checkpoint screenshots", async ({ page }, testInfo) => {
    const errors = watchErrors(page);
    await page.goto("./");
    const story = await readStory(page);
    for (const ch of story.chapters) await expect(section(page, ch)).toHaveCount(1);
    await chapterCheckpoints(page, testInfo, story, "reduced-motion");
    expect(errors).toEqual([]);
  });

  controlTests();
});
