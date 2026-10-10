// The runtime's enhancements, tested through roles and ARIA (docs/ARCHITECTURE.md "Runtime principles",
// docs/BLUEPRINT.md "Front-end decisions"); web-tests/README.md lists the DOM contract assumed here.
// The controls are tested twice, with default motion and with prefers-reduced-motion, which must keep
// every control working (only automatic motion goes away).
import { expect, test } from "@playwright/test";
import {
  chapterCheckpoints,
  currentDate,
  effectiveOpacity,
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

/**
 * One tap (a touch on phones, a mouse click elsewhere) on `area`, at a spot on the far side of the
 * curtain where nothing (a pin, a card, a button) lies on top of the picture: WCAG 2.2 SC 2.5.7 asks
 * that what a drag does also works without one. Returns the slider value the tap points at.
 */
async function tapAcross(page, area, slider) {
  const box = await area.boundingBox();
  const grip = await slider.boundingBox();
  const y = grip.y + grip.height / 2;
  for (const f of (await settled(slider)) > 50 ? [0.2, 0.3, 0.12, 0.4] : [0.8, 0.7, 0.88, 0.6]) {
    const x = box.x + f * box.width;
    const clear = await slider.evaluate((el, [x, y]) => {
      const hit = document.elementFromPoint(x, y);
      return !!hit && (hit.contains(el) || hit.getAttribute("role") === "img");
    }, [x, y]);
    if (!clear) continue;
    if (test.info().project.use.hasTouch) await page.touchscreen.tap(x, y);
    else await page.mouse.click(x, y);
    return f * 100;
  }
  throw new Error("no clear spot on the picture to tap");
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

  test("scrub: a step card's links are in the tab order exactly while the card shows", async ({ page }) => {
    const { ch } = await open(page, "scrub", (c) => c.steps.some((s) => /<a\s/.test(s.html || "")));
    const sec = section(page, ch);
    const { top, height, vh } = await geometry(sec);
    let showing = 0;
    for (let k = 0; k <= 48; k++) {
      await scrollToY(page, top + (k / 48) * (height - vh));
      const links = await sec.getByRole("link").evaluateAll((els) =>
        els.filter((a) => onScreen(a)).map((a) => {
          let o = 1;
          for (let n = a; n instanceof Element; n = n.parentElement) o *= Number(getComputedStyle(n).opacity);
          return { name: a.getAttribute("aria-label") || a.textContent, o, tabbable: a.tabIndex >= 0 };
        }),
      );
      for (const l of links) {
        showing += l.o > 0.5;
        expect(l.tabbable, `"${l.name}" at opacity ${l.o.toFixed(2)}`).toBe(l.o > 0.5);
      }
    }
    expect(showing, "a step card with a link came into view").toBeGreaterThan(0);
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

  test("compare: a tap on the picture moves the curtain there", async ({ page }) => {
    const { ch } = await open(page, "compare", curtain);
    const sec = section(page, ch);
    const slider = sec.getByRole("slider").first();
    await expect(slider).toBeVisible();
    await slider.scrollIntoViewIfNeeded();
    await expect(slider).toHaveAttribute("aria-valuenow", /\d/);
    const want = await tapAcross(page, sec, slider); // the curtain spans the section's width
    await expect.poll(async () => Math.abs((await settled(slider)) - want)).toBeLessThanOrEqual(3);
  });

  test("compare: scrolling through the steps sweeps the curtain", async ({ page }) => {
    const { ch } = await open(page, "compare", (c) => curtain(c) && c.steps.filter((s) => s.split != null).length > 1);
    const splits = ch.steps.filter((s) => s.split != null).map((s) => s.split * 100);
    const sec = section(page, ch);
    const slider = sec.getByRole("slider").first();
    await expect(slider).toBeVisible();
    const { top, height, vh } = await geometry(sec);
    await scrollToY(page, top + 1);
    await expect(slider).toHaveAttribute("aria-valuenow", /\d/); // a missing value would read as 0
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

  test("explore: blink has a toggle button for keyboards and screen readers", async ({ page }) => {
    const { story, ch } = await open(page, "explore");
    const sec = section(page, ch);
    const mode = sec.getByRole("button", { name: "Blink", exact: true });
    test.skip(!(await mode.count()), "this explore chapter has no two-flight modes");
    await mode.scrollIntoViewIfNeeded();
    await mode.click();
    const v = story.vantages.find((x) => x.id === ch.vantages[0]);
    const toggle = sec.getByRole("button", { name: /^Show / });
    await expect(toggle).toBeVisible();
    await expect(toggle).toHaveAttribute("aria-pressed", "false");
    await toggle.focus();
    await page.keyboard.press("Space");
    await expect(toggle).toHaveAttribute("aria-pressed", "true");
    const first = v.captures.find((c) => c.img.sources.length || c.img.fallback !== c.img.lqip); // not a placeholder
    await expect(sec.getByRole("img", { name: new RegExp(`^${escapeRe(v.name)}, `) })).toHaveAttribute("aria-label", new RegExp(`${escapeRe(first.label)}$`));
    await page.keyboard.press("Enter");
    await expect(toggle).toHaveAttribute("aria-pressed", "false");
    await sec.getByRole("button", { name: "Curtain", exact: true }).click();
    await expect(toggle).toBeHidden();
  });

  test("explore: in Curtain mode a tap on the picture moves the divider there", async ({ page }) => {
    const { story, ch } = await open(page, "explore");
    const sec = section(page, ch);
    const mode = sec.getByRole("button", { name: "Curtain", exact: true });
    test.skip(!(await mode.count()), "this explore chapter has no two-flight modes");
    await mode.scrollIntoViewIfNeeded();
    await mode.click();
    const v = story.vantages.find((x) => x.id === ch.vantages[0]);
    const stage = sec.getByRole("img", { name: new RegExp(`^${escapeRe(v.name)}, `) });
    const slider = sec.getByRole("slider", { name: /divider/i });
    await expect(slider).toBeVisible();
    await stage.scrollIntoViewIfNeeded();
    const want = await tapAcross(page, stage, slider);
    await expect.poll(async () => Math.abs((await settled(slider)) - want)).toBeLessThanOrEqual(3);
  });

  test("keyboard: every Tab stop can be seen (none in a faded step card or pin)", async ({ page }) => {
    await page.goto("./");
    const stops = [];
    for (let i = 0; i < 400; i++) {
      await page.keyboard.press("Tab");
      const stop = await page.evaluate(() => {
        const el = document.activeElement;
        const seen = (window.tabStops ??= new WeakSet());
        if (!el || el === document.body || seen.has(el)) return null; // past the last stop, or round again
        seen.add(el);
        return `${el.closest("section")?.id ?? "page"}: ${el.tagName.toLowerCase()} "${el.getAttribute("aria-label") || el.textContent.trim().slice(0, 40)}"`;
      });
      if (!stop) break;
      stops.push(stop);
      // A moment for entrance fades (the hero's pause button, headings rising in) to finish.
      await expect.poll(() => effectiveOpacity(page.locator(":focus")), { message: `${stop} has focus but cannot be seen`, timeout: 4_000 }).toBeGreaterThan(0.5);
    }
    expect(stops.length, "Tab reaches the story's controls").toBeGreaterThan(2);
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
    // Focus follows the jump, so the next Tab carries on from the chapter, not from the Contents button.
    await expect(section(page, target).getByRole("heading", { name: target.title, exact: true })).toBeFocused();
  });

  test("timeline: Explore from this flight moves focus to the explore chapter", async ({ page }) => {
    await page.goto("./");
    const story = await readStory(page);
    const ex = story.chapters.find((c) => c.type === "explore");
    const tl = story.chapters.find((c) => c.type === "timeline");
    const it = ex && tl?.items.find((x) => x.vantage && x.capture != null && ex.vantages.includes(x.vantage));
    test.skip(!it, "no timeline flight from a vantage the explore chapter covers");
    const label = story.vantages.find((v) => v.id === it.vantage).captures[it.capture].label;
    const thumb = section(page, tl).getByRole("button", { name: `View the ${label} flight` }).first();
    await thumb.scrollIntoViewIfNeeded();
    await thumb.click();
    const go = page.getByRole("dialog").getByRole("button", { name: "Explore from this flight" });
    test.skip(!(await go.count()), "the explore chapter leaves this vantage out");
    await go.click();
    const sec = section(page, ex);
    await expect(ex.title ? sec.getByRole("heading", { name: ex.title, exact: true }) : sec).toBeFocused();
  });

  test("stats: assistive tech reads every figure's real value, before and after it counts up", async ({ page }) => {
    const { ch } = await open(page, "stats");
    const items = section(page, ch).getByRole("list").last().getByRole("listitem"); // after any list in the lede
    await expect(items).toHaveCount(ch.items.length);
    const check = async (when) => {
      for (const [k, it] of ch.items.entries()) {
        const snap = await items.nth(k).ariaSnapshot();
        // The item's text starts with the figure, once: not a placeholder 0 or a count in flight.
        const v = escapeRe(it.value);
        const re = new RegExp(`^- listitem:(?:\\s+- text:)? "?${v}(?![\\d.,]| ${v})`);
        expect(snap, `${ch.id} item ${k} ${when}`).toMatch(re);
      }
    };
    await check("before it is scrolled to"); // figures below the fold wait to count up from zero
    await items.last().scrollIntoViewIfNeeded();
    await page.waitForTimeout(1800); // every figure has counted up (900 ms each, 110 ms apart)
    await check("after it counts up");
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

  test("compare: a first step with no text still lets the next step's card show", async ({ page }) => {
    // A step may only move the curtain ({text: "", split}). The StoryJSON is edited in flight to make one.
    const marker = "The card of the step after a step with no text";
    await page.route(/\/(index\.html)?$/, async (route) => {
      const res = await route.fetch();
      const headers = { ...res.headers() };
      delete headers["content-encoding"];
      delete headers["content-length"];
      const body = (await res.text()).replace(/(<script id="vantage-story" type="application\/json">)(.*?)(<\/script>)/s, (_, a, json, c) => {
        const story = JSON.parse(json);
        const ch = story.chapters.find((x) => x.type === "compare" && x.steps.length > 1);
        if (ch) [ch.steps[0].html, ch.steps[1].html] = ["", `<p>${marker}</p>`];
        return a + JSON.stringify(story).replace(/</g, "\\u003c") + c;
      });
      await route.fulfill({ response: res, headers, body });
    });
    const { ch } = await open(page, "compare", (c) => c.steps.length > 1 && c.steps[0].html === "");
    const sec = section(page, ch);
    const card = sec.getByText(marker);
    const { top, height, vh } = await geometry(sec);
    let most = 0;
    for (let k = 0; k <= 16 && most < 0.9; k++) {
      await scrollToY(page, top + (k / 16) * (height - vh));
      most = Math.max(most, await effectiveOpacity(card));
    }
    expect(most, "scrolling through the chapter never showed the second step's card").toBeGreaterThan(0.9);
  });

  test("chrome: Share and Contents come before the story in the tab order, as they do on screen", async ({ page }) => {
    await page.goto("./");
    const menu = page.getByRole("navigation", { name: "Story" }).getByRole("button", { name: "Contents" });
    await expect(menu).toBeVisible();
    const main = await page.getByRole("main").elementHandle();
    expect(await menu.evaluate((el, main) => !!(el.compareDocumentPosition(main) & Node.DOCUMENT_POSITION_FOLLOWING), main)).toBe(true);
  });

  test("hero: the ambient motion has a pause button, remembered for the session", async ({ page }) => {
    const { ch } = await open(page, "hero", (c) => c.vantage || c.video);
    const sec = section(page, ch);
    const pause = sec.getByRole("button", { name: /^pause the background/i });
    await expect(pause).toBeVisible();
    await pause.focus();
    await page.keyboard.press("Enter");
    const play = sec.getByRole("button", { name: /^play the background/i });
    await expect(play).toBeVisible();
    const paused = () => sec.locator("video").evaluateAll((vs) => vs.every((v) => v.paused));
    expect(await paused()).toBe(true);
    await page.reload();
    await expect(play).toBeVisible();
    await page.waitForTimeout(800); // the loop would have started by now
    expect(await paused()).toBe(true);
    await play.click();
    await expect(pause).toBeVisible();
  });

  /* Save for offline when the service worker misbehaves: it is stubbed in the page, so these run against
   * any hosted (http/https) copy. */
  const stubWorker = (page, mode) =>
    page.addInitScript((mode) => {
      const sw = navigator.serviceWorker;
      if (!sw) return;
      const post = (data, ms) => setTimeout(() => sw.dispatchEvent(new MessageEvent("message", { data })), ms);
      const active = {
        postMessage(m) {
          if (m.type !== "vantage:save" || mode !== "quota") return;
          post({ type: "vantage:progress", done: 1, total: 4, bytes: 1048576, totalBytes: 4194304 }, 20);
          post({ type: "vantage:error", url: "x", message: "QuotaExceededError: The quota has been exceeded." }, 60);
        },
      };
      sw.register = () => (mode === "refused" ? Promise.reject(new TypeError("refused")) : Promise.resolve({}));
      Object.defineProperty(sw, "ready", { get: () => (mode === "stalled" ? new Promise(() => {}) : Promise.resolve({ active })) });
    }, mode);

  const hosted = async (page) => {
    await page.goto("./");
    test.skip(!/^https?:/.test(page.url()) || !(await page.locator("html[data-sw]").count()), "not a hosted edition");
  };

  const saveSheet = async (page) => {
    if (!/^https?:/.test(page.url())) await hosted(page);
    await page.getByRole("button", { name: /chapters|contents|index/i }).first().click();
    const save = page.getByRole("dialog").getByRole("button", { name: "Save for offline" });
    await expect(page.getByRole("dialog")).toBeVisible();
    test.skip(!(await save.count()), "this browser has no service workers here, so no Save for offline");
    await expect(save).toBeEnabled();
    return save;
  };

  test("save for offline: a worker that never activates ends in a retry state", async ({ page }) => {
    await page.clock.install();
    await stubWorker(page, "stalled");
    const save = await saveSheet(page);
    await save.click();
    await expect(save).toBeDisabled();
    await page.clock.fastForward(21_000);
    await expect(page.getByRole("dialog").getByRole("button", { name: "Try again" })).toBeEnabled();
    await expect(page.getByRole("dialog").getByText(/didn’t start/).first()).toBeVisible();
  });

  for (const [mode, says] of [["refused", /can’t save a copy/], ["quota", /Not enough free space/]]) {
    test(`save for offline: ${mode === "quota" ? "a full disk" : "a refused registration"} says so and offers a retry`, async ({ page }) => {
      await stubWorker(page, mode);
      const save = await saveSheet(page);
      await save.click();
      const sheet = page.getByRole("dialog");
      await expect(sheet.getByRole("button", { name: "Try again" })).toBeEnabled();
      await expect(sheet.getByText(says).first()).toBeVisible();
      await expect(sheet.getByRole("progressbar")).toBeHidden();
    });
  }

  /* The real service worker. Chromium only: Playwright's handle on a worker is Chromium's, and WebKit's
   * worker is checked on a device (docs/DELIVERY.md). */
  const realWorker = async (page, browserName) => {
    test.skip(browserName !== "chromium", "drives the real service worker through Chromium");
    await hosted(page);
    test.skip(!(await page.evaluate(() => "serviceWorker" in navigator)), "this browser has no service workers here");
    await page.waitForFunction(() => navigator.serviceWorker.controller, null, { timeout: 30_000 }); // installed and in control
    return page.context().serviceWorkers().at(-1);
  };
  /** Every URL in this origin's CacheStorage, without its query. */
  const cached = (page) =>
    page.evaluate(async () => {
      const out = [];
      for (const name of await caches.keys()) for (const r of await (await caches.open(name)).keys()) out.push(r.url.split("?")[0]);
      return out;
    });

  test("offline cache: a first visit stores the opening picture and what the reader saw, nothing more", async ({ page, browserName }) => {
    const seen = new Set();
    page.on("request", (r) => seen.add(r.url().split("?")[0]));
    await realWorker(page, browserName);
    const story = await readStory(page);
    const hero = story.chapters[0]?.type === "hero" ? story.chapters[0] : null;
    const still = hero && vantageOf(story, hero)?.captures.at(hero.capture)?.img;
    const opening = [still?.fallback, hero?.video?.poster?.fallback].filter(Boolean).map((f) => new URL(f, page.url()).href);
    await page.waitForTimeout(500); // copies of the last responses are stored in the background
    const images = (await cached(page)).filter((u) => u.includes("/assets/img/"));
    expect(images.filter((u) => !seen.has(u) && !opening.includes(u)), "images stored that the page never asked for").toEqual([]);
  });

  test("save for offline: files the host won't serve are reported, not called saved", async ({ page, browserName }) => {
    const worker = await realWorker(page, browserName);
    // From here every download answers 404, the way a file the host refused (over its size cap) would.
    await worker.evaluate(() => {
      self.fetch = async () => new Response("", { status: 404 });
    });
    const save = await saveSheet(page);
    await save.click();
    const sheet = page.getByRole("dialog");
    await expect(sheet.getByRole("button", { name: "Try again" })).toBeEnabled();
    await expect(sheet.getByText(/Couldn’t save everything/).first()).toBeVisible();
    await expect(sheet.getByRole("button", { name: "Saved for offline" })).toHaveCount(0);
  });

  test("save for offline: an update keeps the saved copy and fetches only what changed", async ({ page, browserName }) => {
    await realWorker(page, browserName);
    const save = await saveSheet(page);
    await save.click();
    const sheet = page.getByRole("dialog");
    await expect(sheet.getByRole("button", { name: "Saved for offline" })).toBeDisabled({ timeout: 60_000 });
    // What the next deploy looks like to the saved copy: one file changed (so the device has no copy of the
    // new version) and one file is gone from the story.
    const { changed, dropped, kept } = await page.evaluate(async () => {
      const cache = await caches.open((await caches.keys()).find((k) => k.startsWith("vantage-")));
      const reqs = (await cache.keys()).filter((r) => r.url.includes("/assets/"));
      const changed = reqs.find((r) => r.url.includes("/assets/video/")) || reqs.find((r) => r.url.includes("/assets/img/"));
      await cache.delete(changed);
      const dropped = new URL("assets/img/dropped-from-the-story-640.jpg", location.href).href;
      await cache.put(dropped, new Response("old"));
      return { changed: changed.url.split("?")[0], dropped, kept: reqs.length };
    });
    await page.evaluate(() => navigator.serviceWorker.register(`${document.documentElement.dataset.sw}?next`)); // the update takes over
    await expect
      .poll(async () => {
        const urls = await cached(page);
        return { changed: urls.includes(changed), dropped: urls.includes(dropped), assets: urls.filter((u) => u.includes("/assets/")).length };
      }, { timeout: 60_000 })
      .toEqual({ changed: true, dropped: false, assets: kept });
    await expect(sheet.getByRole("button", { name: "Saved for offline" })).toBeDisabled();
    await expect(sheet.getByText(/^Saved for offline/).first()).toBeVisible();
  });

  controlTests();
});

test.describe("interactions with reduced motion", () => {
  // reducedMotion is a context option, not a fixture of its own: `use({ reducedMotion })` is silently ignored.
  test.use({ contextOptions: { reducedMotion: "reduce" } });

  test("the page sees prefers-reduced-motion", async ({ page }) => {
    await page.goto("./");
    expect(await page.evaluate(() => matchMedia("(prefers-reduced-motion: reduce)").matches)).toBe(true);
  });

  test("no errors, every chapter renders; checkpoint screenshots", async ({ page }, testInfo) => {
    const errors = watchErrors(page);
    await page.goto("./");
    const story = await readStory(page);
    for (const ch of story.chapters) await expect(section(page, ch)).toHaveCount(1);
    await chapterCheckpoints(page, testInfo, story, "reduced-motion");
    expect(errors).toEqual([]);
  });

  test("hero: no ambient motion, so no pause button, and the loop never plays", async ({ page }) => {
    const { ch } = await open(page, "hero", (c) => c.vantage || c.video);
    const sec = section(page, ch);
    await page.waitForTimeout(800);
    await expect(sec.getByRole("button", { name: /background/i })).toBeHidden();
    expect(await sec.locator("video").evaluateAll((vs) => vs.every((v) => v.paused))).toBe(true);
  });

  controlTests();
});
