// @ts-check
/* Chrome: a thin reading-progress bar, floating Share and Contents buttons, the draft badge, and
 * "Save for offline" (hosted edition over https only: registers the service worker named by
 * <html data-sw>, asks it to cache every file with progress, then requests persistent storage). */

const shareURL = story.meta.url || (/^https?:$/.test(location.protocol) ? location.href.split("#")[0] : "");
const canSave = !!root.dataset.sw && "serviceWorker" in navigator && isSecureContext && /^https?:$/.test(location.protocol);
const MB = (/** @type {number} */ b) => `${(b / 1048576).toFixed(b < 1048576 * 10 ? 1 : 0)} MB`;

async function share() {
  try {
    if (navigator.share) await navigator.share({ title: story.meta.title, url: shareURL });
    else {
      await navigator.clipboard.writeText(shareURL);
      toast("Link copied");
    }
  } catch (err) {
    if (/** @type {Error} */ (err).name !== "AbortError") toast(shareURL);
  }
}

/* Save for offline: one status line + meter, kept across openings of the contents sheet. */
const saveStatus = h("p", { class: "v-offline__status v-meta", text: "Keep the whole story on this device for airplane mode." });
const saveMeter = h("progress", { class: "v-meter", max: "1", value: "0", hidden: true });
const saveBtn = h("button", { class: "v-btn v-btn--pill", type: "button" }, [icon("save"), "Save for offline"]);
const offline = h("div", { class: "v-offline" }, [saveBtn, saveMeter, saveStatus]);

async function saved() {
  saveMeter.hidden = true;
  saveBtn.disabled = true;
  const persisted = await navigator.storage?.persist?.().catch(() => false);
  const est = await navigator.storage?.estimate?.().catch(() => null);
  saveStatus.textContent = `Saved for offline${est?.usage ? `, ${MB(est.usage)} on this device` : ""}.${persisted ? "" : " Open it now and then so the browser keeps it."}`;
}

if (canSave) {
  addEventListener("load", () => navigator.serviceWorker.register(/** @type {string} */ (root.dataset.sw)).catch(() => {}));
  navigator.serviceWorker.addEventListener("message", (e) => {
    const m = e.data || {};
    if (m.type === "vantage:progress") {
      saveMeter.hidden = false;
      saveMeter.value = m.totalBytes ? m.bytes / m.totalBytes : m.done / m.total;
      saveStatus.textContent = `Saving… ${MB(m.bytes)} of ${MB(m.totalBytes)}`;
    } else if (m.type === "vantage:saved" || (m.type === "vantage:status" && m.total && m.cached >= m.total)) saved();
    else if (m.type === "vantage:error") {
      saveBtn.disabled = false;
      saveMeter.hidden = true;
      saveStatus.textContent = "Couldn’t save everything. Check the connection and try again.";
    }
  });
  saveBtn.addEventListener("click", async () => {
    saveBtn.disabled = true;
    saveStatus.textContent = "Preparing…";
    (await navigator.serviceWorker.ready).active?.postMessage({ type: "vantage:save" });
  });
}

function contents(/** @type {HTMLElement} */ from) {
  const mark = innerHeight / 3;
  const secs = story.chapters.map((/** @type {any} */ c) => doc.getElementById(c.id));
  const here = secs.reduce((k, el, i) => (el && el.getBoundingClientRect().top <= mark ? i : k), 0);
  const list = h("ol", { class: "v-toc" }, story.chapters.map((/** @type {any} */ c, /** @type {number} */ k) => {
    const sec = secs[k];
    if (!sec) return null;
    const a = h("a", { href: `#${c.id}`, "aria-current": k === here ? "true" : null }, [
      h("span", { class: "v-label", text: pad(k + 1) }),
      h("span", { text: c.type === "hero" ? "Opening" : c.title || c.kicker || c.type }),
    ]);
    a.addEventListener("click", (/** @type {Event} */ e) => {
      e.preventDefault();
      closeSheet();
      sec.scrollIntoView({ behavior: reduced ? "auto" : "smooth" });
    });
    return h("li", {}, [a]);
  }));
  if (canSave) navigator.serviceWorker.ready.then((reg) => reg.active?.postMessage({ type: "vantage:status" }));
  openSheet({
    kicker: story.brand.name,
    title: story.meta.title,
    from,
    node: h("div", {}, [
      list,
      canSave && offline,
      story.meta.simulated && h("p", { class: "v-meta v-toc__note", text: "The imagery in this story is simulated." }),
    ]),
  });
}

/** Mount the chrome and keep the progress bar in step with the page. */
function chrome() {
  const fillEl = h("span");
  const menu = h("button", { class: "v-btn v-btn--glass", type: "button", "aria-label": "Contents", "aria-haspopup": "dialog" }, [icon("menu")]);
  menu.addEventListener("click", () => contents(menu));
  doc.body.append(
    h("div", { class: "v-progress", "aria-hidden": "true" }, [fillEl]),
    h("nav", { class: "v-chrome", "aria-label": "Story" }, [
      story.meta.draft && h("span", { class: "v-badge v-chrome__badge", text: "Preview" }),
      shareURL && h("button", { class: "v-btn v-btn--glass", type: "button", "aria-label": "Share this story", onclick: share }, [icon("share")]),
      menu,
    ]),
    sheetWrap,
    toastEl,
    liveRegion,
  );
  let last = -1;
  actives.add({
    measure: () => scrollY / Math.max(1, root.scrollHeight - innerHeight),
    update(p) {
      const f = Math.round(clamp(p) * 1000) / 1000;
      if (f !== last) fillEl.style.transform = `scaleX(${(last = f)})`;
    },
  });
  kick();
}
