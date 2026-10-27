// Mio's browser bridge. Keeps a native-messaging port to the local host (bin/mio-browser-host), runs the
// actions Mio asks for, and answers the popup. Nothing leaves this PC.

const HOST = "com.mio.assistant";
const DEFAULTS = {
  readPages: true,
  allowActions: true, // clicking, typing, scrolling
  blocked: ["bank", "sparkasse", "volksbank", "paypal.", "accounts.google.", "login", "signin", "sign-in",
            "password", "bitwarden", "1password", "lastpass", "proton.me/pass", "checkout"],
  log: [],
};
// clicks on these ask first (Mio speaks the question, you answer yes/no)
const DANGER = "buy|purchase|order|pay|checkout|subscribe|delete|remove|send|post|submit|confirm|transfer|" +
  "unsubscribe|install|kaufen|bestellen|zahlen|bezahlen|löschen|senden|abonnieren";

let port = null;
let hello = false;
const statusWaiters = new Map();

function connect() {
  if (port) return;
  try {
    port = chrome.runtime.connectNative(HOST);
  } catch (e) {
    port = null;
    return;
  }
  port.onMessage.addListener(onHostMessage);
  port.onDisconnect.addListener(() => {
    port = null;
    hello = false;
    setTimeout(connect, 5000);
  });
}

chrome.runtime.onStartup.addListener(connect);
chrome.runtime.onInstalled.addListener(async () => {
  chrome.alarms.create("keepalive", { periodInMinutes: 0.5 });
  const { blocked } = await chrome.storage.local.get("blocked");
  if (!blocked) await chrome.storage.local.set({ blocked: DEFAULTS.blocked }); // so the popup can show + edit them
  connect();
});
chrome.alarms.onAlarm.addListener(() => connect()); // wakes the worker; an open port keeps it alive
connect();

async function settings() {
  return { ...DEFAULTS, ...(await chrome.storage.local.get(Object.keys(DEFAULTS))) };
}

async function logAction(text) {
  const { log } = await settings();
  log.unshift({ t: Date.now(), text });
  await chrome.storage.local.set({ log: log.slice(0, 25) });
}

function isBlocked(url, blocked) {
  const u = (url || "").toLowerCase();
  return blocked.find((b) => b && u.includes(b.toLowerCase()));
}

async function onHostMessage(msg) {
  if (msg.type === "hello") {
    hello = true;
    return;
  }
  if (msg.type === "status") {
    statusWaiters.get(msg.rid)?.(msg);
    statusWaiters.delete(msg.rid);
    return;
  }
  if (msg.id === undefined) return;
  try {
    const result = await run(msg.action, msg.args || {});
    port?.postMessage({ id: msg.id, result });
  } catch (e) {
    port?.postMessage({ id: msg.id, error: String(e.message || e) });
  }
}

// ── helpers ───────────────────────────────────────────────────────────────────
async function activeTab() {
  const [tab] = await chrome.tabs.query({ active: true, lastFocusedWindow: true });
  if (!tab) throw new Error("no browser tab is open");
  return tab;
}

async function inject(tab, func, args = []) {
  const s = await settings();
  const hit = isBlocked(tab.url, s.blocked);
  if (hit) throw new Error(`Mio stays out of this site (blocked: "${hit}" — change it in the extension popup)`);
  if (/^(chrome|brave|edge|about|chrome-extension|devtools):/.test(tab.url || "")) {
    throw new Error("that's a browser settings page, Mio can't read or touch those");
  }
  const [res] = await chrome.scripting.executeScript({ target: { tabId: tab.id }, func, args });
  return res?.result;
}

const isWatch = (t) => /youtube\.com\/(watch|shorts)|youtu\.be\//.test(t.url || "");

async function youtubeTab() {
  const tab = await activeTab().catch(() => null);
  if (tab && isWatch(tab)) return tab;
  const all = (await chrome.tabs.query({})).filter(isWatch);
  if (!all.length) throw new Error("no YouTube video is open");
  return all.find((t) => t.audible) || all.sort((a, b) => (b.lastAccessed || 0) - (a.lastAccessed || 0))[0];
}

function matchTabs(tabs, query) {
  const q = (query || "").toLowerCase().trim();
  return tabs.filter((t) => (t.title || "").toLowerCase().includes(q) || (t.url || "").toLowerCase().includes(q));
}

// ── actions ───────────────────────────────────────────────────────────────────
async function run(action, a) {
  const s = await settings();
  switch (action) {
    case "page": {
      if (!s.readPages) throw new Error("reading pages is turned off in the Mio extension");
      const tab = await activeTab();
      const page = await inject(tab, extractPage);
      await logAction(`Read “${(tab.title || "").slice(0, 50)}”`);
      return page;
    }
    case "tabs":
      return tabsAction(a);
    case "video": {
      const tab = await youtubeTab();
      const r = await inject(tab, videoControl, [a.op || "info", a.value ?? null]);
      if (a.op && a.op !== "info") await logAction(`YouTube: ${a.op}${a.value != null ? " " + a.value : ""}`);
      return r;
    }
    case "scroll": {
      if (!s.allowActions) throw new Error("clicking and scrolling are turned off in the Mio extension");
      const tab = await activeTab();
      return inject(tab, (dir) => {
        const h = window.innerHeight * 0.8;
        const to = { down: [0, h], up: [0, -h] }[dir];
        if (to) window.scrollBy({ left: 0, top: to[1], behavior: "smooth" });
        else window.scrollTo({ top: dir === "top" ? 0 : document.body.scrollHeight, behavior: "smooth" });
        return { scrolled: dir };
      }, [a.direction || "down"]);
    }
    case "click": {
      if (!s.allowActions) throw new Error("clicking is turned off in the Mio extension");
      // results are often drawn a moment after the page loads: keep looking for up to `wait` seconds
      const until = Date.now() + Math.min(Number(a.wait) || 0, 15) * 1000;
      let r;
      for (;;) {
        const tab = await activeTab();
        r = await inject(tab, findAndClick, [a.target || "", !!a.confirmed, DANGER]).catch((e) => {
          if (Date.now() >= until || /stays out|settings page/.test(String(e.message))) throw e;  // else still loading
          return { error: String(e.message || e) };
        });
        if (!r?.error || Date.now() >= until) break;
        await new Promise((res) => setTimeout(res, 400));
      }
      if (r?.clicked) await logAction(`Clicked “${r.clicked.slice(0, 50)}”`);
      return r;
    }
    case "type": {
      if (!s.allowActions) throw new Error("typing is turned off in the Mio extension");
      const tab = await activeTab();
      const r = await inject(tab, typeText, [a.text || "", !!a.submit, !!a.confirmed]);
      if (r?.typed) await logAction(`Typed into ${r.field}${r.submitted ? " and sent it" : ""}`);
      return r;
    }
    case "history": {
      const days = Math.min(Math.max(a.days || 7, 1), 90);
      const items = await chrome.history.search({ text: a.query || "", startTime: Date.now() - days * 864e5,
                                                  maxResults: 12 });
      return items.filter((i) => !isBlocked(i.url, s.blocked))
        .map((i) => {
          const d = new Date(i.lastVisitTime);  // local time, "2026-10-18 13:42"
          const pad = (n) => String(n).padStart(2, "0");
          const last = `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
          return { title: i.title, url: i.url, last };
        });
    }
    case "open": {
      const tab = await chrome.tabs.create({ url: a.url });
      await logAction(`Opened ${new URL(a.url).host || "a new tab"}`);
      return { opened: tab.url || a.url };
    }
    case "wait": {
      // until the page Mio just opened is the active tab and has loaded ("expect" = part of its URL)
      const until = Date.now() + Math.min(a.timeout || 10000, 20000);
      const want = (a.expect || "").toLowerCase();
      const plain = (u) => { try { return decodeURIComponent(u.replace(/\+/g, " ")).toLowerCase(); } catch { return u.toLowerCase(); } };
      while (Date.now() < until) {
        const tab = await activeTab().catch(() => null);
        const url = plain(tab?.url || tab?.pendingUrl || "");
        if (tab && tab.status === "complete" && (!want || url.includes(want))) return { url: tab.url, title: tab.title };
        await new Promise((r) => setTimeout(r, 250));
      }
      throw new Error("the page didn't finish loading");
    }
    case "nav": {
      const tab = await activeTab();
      if (a.to === "back") await chrome.tabs.goBack(tab.id);
      else if (a.to === "forward") await chrome.tabs.goForward(tab.id);
      else await chrome.tabs.reload(tab.id);
      await logAction(a.to === "back" ? "Went back" : a.to === "forward" ? "Went forward" : "Reloaded the page");
      return { nav: a.to };
    }
    default:
      throw new Error(`unknown action ${action}`);
  }
}

async function tabsAction(a) {
  const tabs = await chrome.tabs.query({});
  switch (a.op) {
    case "list":
      return tabs.slice(0, 40).map((t) => ({ title: (t.title || "").slice(0, 70), host: hostOf(t.url),
                                            active: t.active, audible: !!t.audible }));
    case "focus": {
      const hit = matchTabs(tabs, a.query).sort((x, y) => (y.lastAccessed || 0) - (x.lastAccessed || 0))[0];
      if (!hit) return { found: 0 };
      await chrome.tabs.update(hit.id, { active: true });
      await chrome.windows.update(hit.windowId, { focused: true });
      await logAction(`Switched to “${(hit.title || "").slice(0, 50)}”`);
      return { focused: hit.title };
    }
    case "close": {
      const hits = !a.query || /^(this|current|this tab)$/i.test(a.query) ? [await activeTab()] : matchTabs(tabs, a.query);
      if (!hits.length) return { closed: 0 };
      if (hits.length > 3 && !a.confirmed) return { needs_confirm: `close ${hits.length} tabs matching “${a.query}”` };
      await chrome.tabs.remove(hits.map((t) => t.id));
      await logAction(`Closed ${hits.length} tab${hits.length > 1 ? "s" : ""}`);
      return { closed: hits.length };
    }
    case "reopen": {
      const s = await chrome.sessions.restore();
      await logAction("Reopened a closed tab");
      return { reopened: s?.tab?.title || s?.window?.tabs?.[0]?.title || "a tab" };
    }
    default:
      throw new Error(`unknown tabs op ${a.op}`);
  }
}

function hostOf(url) {
  try { return new URL(url).host; } catch { return ""; }
}

// ── injected into the page (must be self-contained) ───────────────────────────
function extractPage() {
  const sel = String(window.getSelection() || "").trim();
  const yt = /youtube\.com\/(watch|shorts)/.test(location.href);
  const root = document.querySelector("article") || document.querySelector("main") || document.body;
  const clone = root.cloneNode(true);
  clone.querySelectorAll("script,style,noscript,nav,footer,aside,iframe,svg,button,form,[aria-hidden=true]")
    .forEach((e) => e.remove());
  let text = (clone.innerText || clone.textContent || "").replace(/[ \t]+/g, " ").replace(/\n\s*\n+/g, "\n").trim();
  const desc = yt ? "" : document.querySelector('meta[name="description"],meta[property="og:description"]')?.content || "";
  if (yt) {
    const ch = document.querySelector("ytd-channel-name a, #owner #channel-name a")?.textContent?.trim();
    const d = ["ytd-watch-metadata #description-inline-expander", "ytd-text-inline-expander",
               "ytd-watch-metadata #description", "#description yt-attributed-string", "#description"]
      .map((q) => document.querySelector(q)?.innerText?.trim()).find((x) => x && x.length > 20) || "";
    text = `YouTube video by ${ch || "?"}.\nDescription: ${d.slice(0, 3000) || "(not loaded)"}`;
  }
  return { title: document.title, url: location.href, description: desc.slice(0, 300),
           selection: sel.slice(0, 3000), text: text.slice(0, 12000), youtube: yt };
}

function videoControl(op, value) {
  const v = document.querySelector("video");
  if (!v) return { error: "no video on this page" };
  const fmt = (s) => `${Math.floor(s / 60)}:${String(Math.floor(s % 60)).padStart(2, "0")}`;
  const click = (sel) => { const b = document.querySelector(sel); if (b) b.click(); return !!b; };
  switch (op) {
    case "play": v.play(); break;
    case "pause": v.pause(); break;
    case "toggle": v.paused ? v.play() : v.pause(); break;
    case "mute": v.muted = true; break;
    case "unmute": v.muted = false; break;
    case "speed": v.playbackRate = Math.min(Math.max(Number(value) || 1, 0.25), 4); break;
    case "seek": v.currentTime = Math.max(0, v.currentTime + Number(value || 0)); break;
    case "seek_to": v.currentTime = Math.max(0, Number(value || 0)); break;
    case "next":
      if (!click(".ytp-next-button")) return { error: "there's no next video button" };
      break;
    case "skip_ad":
      if (!click(".ytp-skip-ad-button, .ytp-ad-skip-button, .ytp-ad-skip-button-modern, .ytp-ad-skip-button-slot button")) {
        const ad = document.querySelector(".ad-showing");
        return { error: ad ? "this ad can't be skipped yet" : "there's no ad playing" };
      }
      break;
    case "info": break;
    default: return { error: `unknown video op ${op}` };
  }
  const ch = document.querySelector("ytd-channel-name a, #owner #channel-name a")?.textContent?.trim();
  const title = document.querySelector("h1.ytd-watch-metadata, h1 yt-formatted-string")?.textContent?.trim();
  return { op, title: title || document.title, channel: ch || "", url: location.href, time: fmt(v.currentTime),
           duration: isFinite(v.duration) ? fmt(v.duration) : "live", paused: v.paused, speed: v.playbackRate,
           ad: !!document.querySelector(".ad-showing") };
}

function findAndClick(target, confirmed, danger) {
  const t = target.toLowerCase().trim();
  const visible = (el) => {
    const r = el.getBoundingClientRect();
    return r.width > 2 && r.height > 2 && getComputedStyle(el).visibility !== "hidden";
  };
  const labelOf = (el) => (el.innerText || el.value || el.getAttribute("aria-label") || el.title || "")
    .replace(/\s+/g, " ").trim();
  const ordinals = { first: 0, "1st": 0, second: 1, "2nd": 1, third: 2, "3rd": 2, fourth: 3, "4th": 3, fifth: 4,
                     "5th": 4, last: -1 };
  let el = null;
  const m = t.match(/^(?:the )?(first|1st|second|2nd|third|3rd|fourth|4th|fifth|5th|last)?\s*(?:search )?(result|video|link)s?$/);
  if (m) {
    // YouTube first (old and new layouts), then Google, DuckDuckGo, Brave Search, Bing, anything with headings
    const sels = ["ytd-video-renderer a#video-title", "yt-lockup-view-model a.yt-lockup-metadata-view-model__title",
                  "ytd-rich-item-renderer a#video-title-link", "ytd-compact-video-renderer a#thumbnail",
                  "yt-lockup-view-model a[href*='/watch']:has(h3)", "#search a:has(h3)",
                  "a[data-testid=result-title-a]", "#results .snippet a[href]:has(.title)", "#b_results h2 a",
                  // Reddit (search and feeds), GitHub search
                  "a[data-testid=post-title-text]", "a[data-testid=post-title]", "shreddit-post a[slot=title]",
                  "a[href*='/comments/']:not([href*='#'])", "[data-testid=results-list] .search-title a",
                  "main a:has(h2), main a:has(h3)", "article a:has(h2)"];
    const onYoutube = /(^|\.)youtube\.com$/.test(location.hostname);
    // ads, Shorts shelves and playlists aren't "the first video"
    const real = (a) => !a.closest("ytd-ad-slot-renderer, ytd-in-feed-ad-layout-renderer, ytd-promoted-video-renderer, " +
                                    "ytd-reel-shelf-renderer, ytm-shorts-lockup-view-model, [data-text-ad], .ads-ad") &&
      (!onYoutube || /\/watch\?v=/.test(a.href || ""));
    let list = [];
    for (const sel of sels) {
      try { list = [...document.querySelectorAll(sel)].filter((a) => visible(a) && real(a)); } catch { list = []; }
      if (list.length) break;
    }
    if (!list.length) {
      // any other site: links with a real title in the main part of the page, in reading order
      const root = document.querySelector("main, [role=main], #content, #main") || document.body;
      list = [...root.querySelectorAll("a[href]")].filter((a) => visible(a) && real(a) &&
        !a.closest("nav, header, footer, aside, [role=navigation], [role=banner]") &&
        labelOf(a).length >= 15 && !/^(javascript:|#)/.test(a.getAttribute("href") || ""));
    }
    // one post or video is often linked twice (thumbnail + title): count it once
    const seen = new Set();
    list = list.filter((a) => { const k = a.href.split("#")[0]; if (seen.has(k)) return false; seen.add(k); return true; });
    const i = ordinals[m[1] || "first"];
    el = list.at(i) || null;
  } else {
    const cands = [...document.querySelectorAll("a,button,[role=button],[role=link],[role=tab],[role=menuitem]," +
                                                "input[type=submit],input[type=button],summary,label")].filter(visible);
    let best = 0;
    for (const c of cands) {
      const l = labelOf(c).toLowerCase();
      if (!l) continue;
      const s = l === t ? 4 : l.startsWith(t) ? 3 : l.includes(t) ? 2 : t.includes(l) && l.length > 3 ? 1 : 0;
      if (s > best || (s === best && s && el && labelOf(c).length < labelOf(el).length)) { best = s; el = c; }
    }
  }
  if (!el) return { error: `couldn't find “${target}” on the page` };
  const label = labelOf(el).slice(0, 120) || el.href || "that";
  if (!confirmed && new RegExp(`\\b(${danger})\\b`, "i").test(label)) return { needs_confirm: label };
  el.scrollIntoView({ block: "center" });
  el.click();
  return { clicked: label, href: el.href || "" };
}

function typeText(text, submit, confirmed) {
  const visible = (el) => { const r = el.getBoundingClientRect(); return r.width > 2 && r.height > 2; };
  const fieldOk = (e) => e && (e.isContentEditable || /^(input|textarea)$/i.test(e.tagName)) &&
    !/^(password|hidden|file|checkbox|radio|submit|button)$/i.test(e.type || "");
  let el = document.activeElement;
  if (!fieldOk(el)) {
    el = [...document.querySelectorAll("input[type=search],input[name=q],input[name=search_query],input[name=k]," +
                                       "input[type=text],textarea,[contenteditable=true]")].find((e) => visible(e) && fieldOk(e));
  }
  if (!el) return { error: "there's no text field to type into" };
  if ((el.type || "").toLowerCase() === "password") return { error: "Mio never types into password fields" };
  const searchy = el.type === "search" || /^(q|search|search_query|query|k|s)$/i.test(el.name || "") ||
    el.getAttribute("role") === "searchbox" || !!el.closest("form[role=search], [role=search]");
  if (submit && !searchy && !confirmed) return { needs_confirm: `send the form on ${location.host}` };
  el.focus();
  if (el.isContentEditable) {
    document.execCommand("insertText", false, text);
  } else {
    const proto = el.tagName === "TEXTAREA" ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
    Object.getOwnPropertyDescriptor(proto, "value").set.call(el, text);  // so React/Vue notice
    el.dispatchEvent(new Event("input", { bubbles: true }));
    el.dispatchEvent(new Event("change", { bubbles: true }));
  }
  if (submit) {
    const form = el.form;
    if (form?.requestSubmit) form.requestSubmit();
    else for (const type of ["keydown", "keypress", "keyup"]) {
      el.dispatchEvent(new KeyboardEvent(type, { key: "Enter", code: "Enter", keyCode: 13, which: 13, bubbles: true }));
    }
  }
  return { typed: text.length, field: el.name || el.placeholder || el.getAttribute("aria-label") || el.tagName.toLowerCase(),
           submitted: !!submit };
}

// ── popup ─────────────────────────────────────────────────────────────────────
chrome.runtime.onMessage.addListener((msg, _sender, reply) => {
  if (msg.type === "status") {
    if (!port || !hello) {
      connect();
      reply({ bridge: false });
      return false;
    }
    const rid = Math.random().toString(36).slice(2);
    const timer = setTimeout(() => { statusWaiters.delete(rid); reply({ bridge: true, mio: false }); }, 1500);
    statusWaiters.set(rid, (m) => { clearTimeout(timer); reply({ bridge: true, mio: m.mio, state: m.state }); });
    port.postMessage({ type: "status", rid });
    return true; // async reply
  }
  if (msg.type === "ask") {
    if (!port) { reply({ ok: false }); return false; }
    port.postMessage({ type: "ask", text: msg.text });
    logAction(`Asked: “${msg.text.slice(0, 50)}”`);
    reply({ ok: true });
    return false;
  }
  return false;
});
