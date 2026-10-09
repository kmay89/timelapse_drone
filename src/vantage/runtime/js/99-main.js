// @ts-check
/* Boot: chrome first, then each chapter's enhancer. A chapter whose enhancer fails keeps its static,
 * pre-rendered form; the rest of the story still upgrades. */

/** @type {Record<string, (sec: HTMLElement, ch: any) => Component | null | void>} */
const ENHANCE = { hero, scrub, compare, stats, timeline, gallery, video: videoChapter, explore };

chrome();
for (const ch of story.chapters) {
  const sec = doc.getElementById(ch.id);
  const fn = ENHANCE[ch.type];
  if (!sec || !fn) continue;
  try {
    const c = fn(sec, ch);
    if (c) mount($(":scope > .v-track", sec) || $(".v-x", sec) || sec, c);
  } catch (err) {
    sec.classList.remove("v-live", "v-pinned");
    console.warn(`vantage: chapter ${ch.id} stays static`, err);
  }
}
reveals();
root.classList.add("v-ready");
