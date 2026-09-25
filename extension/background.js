// Walks through chapter pages one by one: open page -> click the site's Download button -> save -> next.
// State lives in chrome.storage so it survives the service worker being put to sleep.

const SITE = "https://www.selfstudys.com";
const BOOKS = {
  "10-maths":   { cls: "Class 10", subject: "Mathematics", path: "/books/ncert-solution/english/10th/class-10-mathematics/766" },
  "10-science": { cls: "Class 10", subject: "Science",     path: "/books/ncert-solution/english/10th/class-10-science/782" },
  "9-maths":    { cls: "Class 9",  subject: "Mathematics", path: "/books/ncert-solution/english/9th/class-9-mathematics/785" },
  "9-science":  { cls: "Class 9",  subject: "Science",     path: "/books/ncert-solution/english/9th/class-9-science/797" },
};
const GAP_MS = [8000, 15000];     // pause between chapters
const DOWNLOAD_START_MS = 15000;  // after clicking, how long before we assume the site is showing a prompt
const PROMPT_WAIT_MS = 5 * 60000; // how long to wait for you to answer a prompt in the tab

// ---------------- state ----------------
async function getState() {
  const { state } = await chrome.storage.local.get("state");
  return state || { queue: [], pos: 0, running: false, tabId: null, phase: "idle", message: "Ready.", done: {} };
}
async function setState(patch) {
  const s = { ...(await getState()), ...patch };
  await chrome.storage.local.set({ state: s });
  return s;
}
const say = (message, extra = {}) => setState({ message, ...extra });

// ---------------- chapter lists ----------------
function titleFrom(slug) {
  return slug.replace(/-/g, " ").replace(/^chapter \d+\s*/i, "").replace(/\b\w/g, c => c.toUpperCase()).trim();
}
async function chaptersOf(key) {
  const b = BOOKS[key];
  const html = await (await fetch(SITE + b.path)).text();
  const slug = b.path.split("/").slice(-2)[0];
  const re = new RegExp(`href="(/advance-pdf-viewer/ncert-solution/english/[^"]*/${slug}/([^/"]+)/(\\d+))"`, "g");
  const seen = new Set(), out = [];
  for (const m of html.matchAll(re)) {
    if (seen.has(m[3])) continue;
    seen.add(m[3]);
    const n = String(out.length + 1).padStart(2, "0");
    const safe = titleFrom(m[2]).replace(/[<>:"/\\|?*]+/g, "");
    out.push({ id: m[3], url: SITE + m[1], book: key, file: `selfstudys/${b.cls}/${b.subject}/${n} - ${safe}.pdf`, title: `${b.cls} ${b.subject} · ${n} ${titleFrom(m[2])}` });
  }
  return out;
}

// ---------------- the loop ----------------
async function start(keys) {
  await say("Reading chapter lists…", { running: true, phase: "loading" });
  let queue = [];
  try {
    for (const k of keys) queue = queue.concat(await chaptersOf(k));
  } catch (e) {
    return say("Could not read the chapter lists: " + e.message, { running: false, phase: "idle" });
  }
  const { done } = await getState();
  queue = queue.filter(c => !done[c.id]);
  if (!queue.length) return say("Everything selected is already downloaded.", { running: false, phase: "idle" });
  const tab = await chrome.tabs.create({ url: "about:blank", active: true });
  await setState({ queue, pos: 0, tabId: tab.id, running: true });
  openCurrent();
}

async function openCurrent() {
  const s = await getState();
  if (!s.running) return;
  if (s.pos >= s.queue.length) return say(`Finished: ${s.queue.length} chapter(s) downloaded.`, { running: false, phase: "idle" });
  const ch = s.queue[s.pos];
  await say(`Opening ${ch.title}  (${s.pos + 1}/${s.queue.length})`, { phase: "opening" });
  try {
    await chrome.tabs.update(s.tabId, { url: ch.url, active: true });
  } catch {
    return say("The downloader tab was closed. Press Start to continue.", { running: false, phase: "idle" });
  }
}

chrome.tabs.onUpdated.addListener(async (tabId, info) => {
  const s = await getState();
  if (!s.running || tabId !== s.tabId || info.status !== "complete" || s.phase !== "opening") return;
  // mark as waiting before clicking: the file can start downloading while the page script is still running
  await setState({ phase: "awaiting-download", clickedAt: Date.now() + 25000, promptShown: false, reclicks: 0, lastCheck: 0 });
  let result;
  try {
    [{ result }] = await chrome.scripting.executeScript({ target: { tabId }, func: clickDownloadWhenReady });
  } catch (e) { result = "error: " + e.message; }
  const now = await getState();
  if (now.phase !== "awaiting-download") return;  // the download already started (or finished) meanwhile
  if (result === "clicked" || result === "clicked-after-class-confirm") {
    await setState({ clickedAt: Date.now() });
    chrome.alarms.create("check", { delayInMinutes: 0.5 });
    setTimeout(checkWaiting, DOWNLOAD_START_MS + 500);
  } else if (result === "no-button") {
    await say("The Download button is not showing on this chapter — the site's download limit is probably reached. " +
              "Press Resume later (or log in on the site) to continue.", { running: false, phase: "paused" });
  } else {
    await say("Could not click Download: " + result, { running: false, phase: "paused" });
  }
});

// runs inside the chapter page: click Download; if the site's "Just to Confirm! Your Selected Class" popup
// appears, press its Save (keeps the class already on your profile), then click Download again, because
// Save only closes the popup and does not start the download.
function clickDownloadWhenReady() {
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  const visible = el => el && el.offsetParent !== null;
  const button = () => document.querySelector(".downloadPdfBtn:not(.hideThis)");
  return (async () => {
    const t0 = Date.now();
    while (!visible(button())) {
      if (Date.now() - t0 > 20000) return "no-button";
      await sleep(500);
    }
    button().click();
    for (let i = 0; i < 8; i++) {  // give the site up to 4 s to decide whether to show its class popup
      await sleep(500);
      const popup = document.querySelector("#confirmClassPopup.show");
      if (popup) {
        const save = popup.querySelector('.confirmClassPopupBtnClick[data-value="yes"]');
        if (!save) return "clicked";
        save.click();
        await sleep(2500);
        if (visible(button())) button().click();
        return "clicked-after-class-confirm";
      }
    }
    return "clicked";
  })();
}

// runs inside the chapter page: after you have answered some other prompt, press Download again
function reclickIfNoPopup() {
  const open = [...document.querySelectorAll(".modal.show")].some(m => m.offsetParent !== null);
  if (open) return "popup-open";
  const b = document.querySelector(".downloadPdfBtn:not(.hideThis)");
  if (b && b.offsetParent !== null) { b.click(); return "reclicked"; }
  return "no-button";
}

async function checkWaiting() {
  const s = await getState();
  if (!s.running || s.phase !== "awaiting-download") return;
  if (Date.now() - (s.lastCheck || 0) < 6000) return;  // timer and alarm can both fire; run one check at a time
  await setState({ lastCheck: Date.now() });
  const waited = Date.now() - s.clickedAt;
  if (waited > PROMPT_WAIT_MS) {
    return say("No download started — if the site asked for phone verification or said the limit is reached, " +
               "try again later with Resume.", { running: false, phase: "paused" });
  }
  if (waited > DOWNLOAD_START_MS) {
    let r = "";
    try { [{ result: r }] = await chrome.scripting.executeScript({ target: { tabId: s.tabId }, func: reclickIfNoPopup }); } catch {}
    if (r === "popup-open" && !s.promptShown) {
      await say("The site is asking something in the tab (login / verification). Please answer it there — " +
                "the downloader clicks Download again by itself when the popup closes.", { promptShown: true });
    } else if (r === "reclicked") {
      await setState({ clickedAt: Date.now() - DOWNLOAD_START_MS / 2, reclicks: (s.reclicks || 0) + 1 });
      if ((s.reclicks || 0) >= 4) {
        return say("Clicked Download several times but no file came. The site may have hit its limit — " +
                   "check the tab, then press Resume.", { running: false, phase: "paused" });
      }
    }
  }
  chrome.alarms.create("check", { delayInMinutes: 0.5 });
  setTimeout(checkWaiting, 8000);
}
chrome.alarms.onAlarm.addListener(a => { if (a.name === "check") checkWaiting(); });

// give each PDF its class/subject/chapter name
chrome.downloads.onDeterminingFilename.addListener((item, suggest) => {
  getState().then(s => {
    const ch = s.queue[s.pos];
    const ours = s.running && s.phase === "awaiting-download" && ch && /selfstudys\.com/.test(item.url + item.finalUrl + (item.referrer || ""));
    if (!ours) return suggest();
    setState({ phase: "downloading", downloadId: item.id });
    suggest({ filename: ch.file, conflictAction: "overwrite" });
  });
  return true; // we answer asynchronously
});

chrome.downloads.onChanged.addListener(async delta => {
  const s = await getState();
  if (!s.running || delta.id !== s.downloadId || !delta.state) return;
  if (delta.state.current === "complete") {
    const ch = s.queue[s.pos];
    const done = { ...s.done, [ch.id]: ch.file };
    await setState({ done, pos: s.pos + 1, phase: "gap", downloadId: null });
    const gap = GAP_MS[0] + Math.random() * (GAP_MS[1] - GAP_MS[0]);
    await say(`Saved ${ch.title}. Next in ${Math.round(gap / 1000)} s…`);
    setTimeout(openCurrent, gap);
  } else if (delta.state.current === "interrupted") {
    await say("The download was interrupted. Press Resume to retry this chapter.", { running: false, phase: "paused" });
  }
});

// ---------------- popup commands ----------------
chrome.runtime.onMessage.addListener((msg, _sender, reply) => {
  (async () => {
    if (msg.cmd === "start") await start(msg.books);
    else if (msg.cmd === "pause") await say("Paused.", { running: false, phase: "paused" });
    else if (msg.cmd === "resume") {
      const s = await getState();
      if (!s.queue.length || s.pos >= s.queue.length) await say("Nothing to resume — press Start.");
      else {
        let tabId = s.tabId;
        try { await chrome.tabs.get(tabId); } catch { tabId = (await chrome.tabs.create({ url: "about:blank" })).id; }
        await setState({ running: true, tabId });
        openCurrent();
      }
    } else if (msg.cmd === "reset") await chrome.storage.local.set({ state: { queue: [], pos: 0, running: false, tabId: null, phase: "idle", message: "Cleared. Ready.", done: {} } });
    reply(await getState());
  })();
  return true;
});
