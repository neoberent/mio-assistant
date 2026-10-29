/**
 * @name Mio
 * @author weev
 * @version 1.0.0
 * @description Lets Mio, your desktop assistant, play songs, playlists and your liked songs by voice.
 */

/* Riff plugins can't listen for connections, so this long-polls Mio's bridge on localhost
   (assistant/riff.py): GET /mio/poll waits for a command, POST /mio/result answers it.
   Everything is done through riff's own buttons and track rows, the same as a click. */

const BASE = 'http://127.0.0.1:__PORT__/mio';
const TOKEN = '__TOKEN__';
let running = true;
const abort = new AbortController();

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const $ = (sel) => document.querySelector(sel);
const rows = (list) => [...document.querySelectorAll(`${list} .track-row`)];
const norm = (s) => String(s || '').toLowerCase().normalize('NFKD').replace(/[̀-ͯ]/g, '')
  .replace(/[^\p{L}\p{N}]+/gu, ' ').trim();
const trackInfo = (t) => (t ? { id: t.id || '', title: t.title || '', artist: t.artist || '' } : null);

// a MutationObserver as well as a timer: timers get throttled while riff is in the background
function waitFor(fn, ms = 12000) {
  return new Promise((resolve) => {
    let done = false;
    const finish = (v) => { if (done) return; done = true; obs.disconnect(); clearInterval(iv); clearTimeout(to); resolve(v); };
    const check = () => { try { const v = fn(); if (v) finish(v); } catch (e) {} };
    const obs = new MutationObserver(check);
    obs.observe(document.body, { childList: true, subtree: true, attributes: true });
    const iv = setInterval(check, 150);
    const to = setTimeout(() => finish(null), ms);
    check();
  });
}

const playlists = () => { try { return JSON.parse(localStorage.getItem('devsize_playlists') || '[]'); } catch (e) { return []; } };
const favorites = () => { try { return JSON.parse(localStorage.getItem('devsize_favorites') || '[]'); } catch (e) { return []; } };
const isLiked = (t) => !!t && favorites().some((f) => (f.id && f.id === t.id) || (norm(f.title) === norm(t.title) && norm(f.artist) === norm(t.artist)));

// clicks a track row and waits until riff really plays that track
async function playRow(row) {
  const id = row.dataset.trackId;
  row.click();
  const t = await waitFor(() => { const c = Riff.getTrack(); return c && (!id || c.id === id) && c; }, 15000);
  if (!t) throw new Error('the song didn\'t start');
  return trackInfo(t);
}

// fresh rows in a list (rows that were there before are tagged, riff replaces them when it re-renders)
// (stale = fine to fall back to the rows already there, for views riff doesn't re-render on every visit)
async function freshRows(list, ms = 15000, stale = false) {
  rows(list).forEach((r) => { r.dataset.mioOld = '1'; });
  const r = await waitFor(() => { const r = rows(list).filter((x) => !x.dataset.mioOld); return r.length && r; }, stale ? 3000 : ms);
  return r || (stale && rows(list).length ? rows(list) : null);
}

function bestRow(found, query) {
  const words = norm(query).split(' ').filter(Boolean);
  let best = found[0], top = -1;
  found.slice(0, 10).forEach((r) => {
    const text = ` ${norm(r.dataset.trackTitle)} ${norm(r.dataset.trackArtist)} `;
    const score = words.filter((w) => text.includes(` ${w}`)).length;
    if (score > top) { top = score; best = r; }  // ties keep riff's own order
  });
  return best;
}

async function search(query) {
  const input = $('#search-input');
  if (!input) throw new Error('riff\'s search box is missing');
  const wait = freshRows('#search-tracks-list');
  input.value = query;
  input.dispatchEvent(new Event('input', { bubbles: true }));
  input.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true }));
  const found = await wait;
  if (!found) throw new Error(`no songs found for "${query}"`);
  return found;
}

async function setShuffle(on) {
  const btn = $('#btn-shuffle');
  if (btn && btn.classList.contains('active') !== on) btn.click();
}

async function playList(list, shuffle) {
  const found = await freshRows(list, 15000, true);
  if (!found) return null;
  if (shuffle) await setShuffle(true);
  return playRow(shuffle ? found[Math.floor(Math.random() * Math.min(found.length, 20))] : found[0]);
}

const actions = {
  async status() {
    const a = Riff.audio();
    const t = Riff.getTrack();
    return {
      track: trackInfo(t), playing: Riff.isPlaying(), liked: isLiked(t),
      time: a && Number.isFinite(a.currentTime) ? Math.round(a.currentTime) : 0,
      duration: a && Number.isFinite(a.duration) ? Math.round(a.duration) : 0,
      shuffle: !!($('#btn-shuffle') && $('#btn-shuffle').classList.contains('active'))
    };
  },
  async play({ query }) {
    const found = await search(query);
    const row = bestRow(found, query);
    return { ...(await playRow(row)), others: found.filter((r) => r !== row).slice(0, 3).map((r) => `${r.dataset.trackTitle} (${r.dataset.trackArtist})`) };
  },
  async search({ query }) {
    const found = await search(query);
    return { results: found.slice(0, 5).map((r) => `${r.dataset.trackTitle} (${r.dataset.trackArtist})`) };
  },
  async playlists() {
    return { playlists: playlists().map((p) => ({ id: p.id, name: p.name || 'untitled', count: (p.tracks || []).length })) };
  },
  async playlist({ id, play = true, shuffle = false }) {
    const pl = playlists().find((p) => p.id === id);
    if (!pl) throw new Error('that playlist is gone');
    const item = document.querySelector(`.playlist-item[data-playlist-id="${CSS.escape(id)}"]`);
    if (!item) throw new Error('riff isn\'t showing its playlists');
    const filter = $('#playlist-search-input');
    if (filter) filter.value = '';
    const wait = play ? playList('#playlist-tracks-list', shuffle) : null;
    item.click();
    const track = play ? await wait : null;
    if (play && !track) throw new Error(`"${pl.name}" has no songs`);
    return { name: pl.name, count: (pl.tracks || []).length, track };
  },
  async liked({ shuffle = false }) {
    if (!favorites().length) throw new Error('you don\'t have any liked songs yet');
    const wait = playList('#favorites-tracks-list', shuffle);
    $('#nav-favorites').click();
    const track = await wait;
    if (!track) throw new Error('the liked songs didn\'t load');
    return { count: favorites().length, track };
  },
  async wave() {
    const before = Riff.getTrack();
    $('#nav-discover').click();
    const btn = await waitFor(() => $('#btn-play-my-wave'), 5000);
    if (!btn) throw new Error('My Wave isn\'t there');
    btn.click();
    const t = await waitFor(() => { const c = Riff.getTrack(); return c && c !== before && Riff.isPlaying() && c; }, 20000);
    if (!t) throw new Error('My Wave didn\'t start');
    return trackInfo(t);
  },
  async control({ op }) {
    const click = (sel) => { const b = $(sel); if (!b) throw new Error(`riff's ${sel} button is missing`); b.click(); };
    const before = Riff.getTrack();
    if (op === 'pause') { if (Riff.isPlaying()) click('#btn-play-pause'); }
    else if (op === 'resume') { if (!Riff.isPlaying()) click('#btn-play-pause'); }
    else if (op === 'toggle') click('#btn-play-pause');
    else if (op === 'next' || op === 'previous') {
      click(op === 'next' ? '#btn-next' : '#btn-prev');
      await waitFor(() => Riff.getTrack() !== before, 8000);
    }
    else if (op === 'like' || op === 'unlike') {
      if (!before) throw new Error('nothing is playing');
      if (isLiked(before) !== (op === 'like')) click('#btn-toggle-fav');
    }
    else if (op === 'shuffle_on' || op === 'shuffle_off') await setShuffle(op === 'shuffle_on');
    else throw new Error(`unknown control ${op}`);
    await sleep(250);
    return actions.status();
  }
};

// Mio-side timeouts are 25 s; nothing here may wait forever, and a watchdog restarts a loop that went quiet.
// window.__mio shows what the loop is doing (handy from DevTools).
const status = window.__mio = { state: 'starting', since: Date.now(), gen: 0, last: '' };
const mark = (state) => { status.state = state; status.since = Date.now(); };
const withTimeout = (p, ms, what) => Promise.race([p, new Promise((_, no) => setTimeout(() => no(new Error(`${what} took too long`)), ms))]);

async function loop(gen) {
  let backoff = 1000;
  while (running && gen === status.gen) {
    let cmd;
    try {
      mark('polling');
      const stopPoll = new AbortController();
      const onStop = () => stopPoll.abort();
      abort.signal.addEventListener('abort', onStop);
      const t = setTimeout(onStop, 35000);  // Mio answers every 25 s, even with nothing to do
      try {
        const res = await fetch(`${BASE}/poll?token=${TOKEN}`, { cache: 'no-store', signal: stopPoll.signal });
        if (res.status === 403) { mark('bad token'); Riff.toast('Mio: the plugin\'s token is outdated, run scripts/install_riff.sh again', 'error'); return; }
        cmd = await res.json();
      } finally { clearTimeout(t); abort.signal.removeEventListener('abort', onStop); }
      backoff = 1000;
    } catch (e) {  // Mio isn't running: try again quietly
      if (!running || gen !== status.gen) return;
      mark('waiting for Mio');
      await sleep(backoff);
      backoff = Math.min(backoff * 2, 10000);
      continue;
    }
    if (!cmd || !cmd.id) continue;
    let answer;
    try {
      mark(`running ${cmd.action}`);
      const fn = actions[cmd.action];
      if (!fn) throw new Error(`unknown action ${cmd.action}`);
      answer = { id: cmd.id, result: await withTimeout(fn(cmd.args || {}), 22000, cmd.action) };
    } catch (e) {
      answer = { id: cmd.id, error: e && e.message ? e.message : String(e) };
    }
    status.last = `${cmd.action}: ${answer.error || 'ok'}`;
    let body;
    try { body = JSON.stringify(answer); } catch (e) { body = JSON.stringify({ id: cmd.id, error: String(e) }); }
    fetch(`${BASE}/result?token=${TOKEN}`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body }).catch(() => {});
  }
}
const restart = () => { status.gen++; loop(status.gen).catch((e) => { status.last = `loop crashed: ${e}`; mark('crashed'); }); };
const watchdog = setInterval(() => {
  if (running && (status.state === 'crashed' || Date.now() - status.since > 60000)) { status.last = `watchdog restart (${status.state})`; restart(); }
}, 15000);
restart();

return { stop() { running = false; clearInterval(watchdog); abort.abort(); } };
