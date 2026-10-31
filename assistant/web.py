"""Free web research: DuckDuckGo search → fetch pages → main-text extraction → compact extracts.

All of this runs locally; only the condensed extracts ever reach an LLM, which keeps
Claude requests to a few thousand tokens instead of whole web pages.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from urllib.parse import urlparse

import httpx

log = logging.getLogger("web")

UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/141.0 Safari/537.36")
SKIP_HOSTS = ("youtube.com", "youtu.be", "tiktok.com", "instagram.com", "facebook.com", "x.com",
              "twitter.com", "pinterest.", "linkedin.com")
URL_RE = re.compile(r"https?://[^\s<>\"'`]+")
REFUSED_DAYS = 7  # a site that refused us (403/401/429/451) is passed over for a week, not argued with


def _refused_file():
    from .config import STATE_DIR

    return STATE_DIR / "web_refused.json"


def _refused() -> dict[str, float]:
    try:
        d = json.loads(_refused_file().read_text())
    except (OSError, ValueError):
        return {}
    cut = time.time() - REFUSED_DAYS * 86400
    return {h: t for h, t in d.items() if t > cut}


def _note_refused(url: str):
    host = urlparse(url).netloc.lower()
    d = _refused()
    d[host] = time.time()
    try:
        _refused_file().write_text(json.dumps(d, indent=1))
    except OSError:
        pass


def _words(text: str, n: int) -> str:
    w = text.split()
    return " ".join(w[:n]) + (" …" if len(w) > n else "")


def _extract(html: str, url: str) -> str:
    import trafilatura

    text = trafilatura.extract(html, url=url, include_comments=False, include_tables=True,
                               favor_precision=True) or ""
    return re.sub(r"\n{3,}", "\n\n", text).strip()


async def fetch_text(client: httpx.AsyncClient, url: str, words: int) -> tuple[str, str]:
    """Returns (title, text) for one page; empty text on failure."""
    try:
        r = await client.get(url)
        r.raise_for_status()
        ctype = r.headers.get("content-type", "")
        if "html" not in ctype and "text" not in ctype:
            return url, ""
        html = r.text
    except httpx.HTTPError as e:
        log.info("fetch failed %s: %s", url, e)
        if isinstance(e, httpx.HTTPStatusError) and e.response.status_code in (401, 403, 429, 451):
            _note_refused(url)
        return url, ""
    m = re.search(r"<title[^>]*>(.*?)</title>", html, re.S | re.I)
    title = re.sub(r"\s+", " ", m.group(1)).strip()[:120] if m else urlparse(url).netloc
    text = await asyncio.to_thread(_extract, html, url)
    return title, _words(text, words)


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=httpx.Timeout(8, connect=4), follow_redirects=True,
                             headers={"User-Agent": UA, "Accept-Language": "en-US,en;q=0.8"})


# ddgs' engines fail often and independently ("No results found" for half of the real
# questions on 2026-10-21), so try a few before giving up; Wikipedia is the last resort.
# One after the other cost seconds per failing engine (2026-10-31: duckduckgo/brave/mojeek failed every time, auto
# and bing half the time), so they now all run at once and the first one with results wins.
# 2026-10-31: ddgs dropped bing; google (403) and brave (429) refuse us every time → left out.
BACKENDS = ("auto", "startpage", "yahoo", "duckduckgo", "mojeek")


def search(query: str, timeout: float = 6.0) -> list[dict]:
    import concurrent.futures as cf

    from ddgs import DDGS

    def one(backend: str) -> list[dict]:
        try:
            return DDGS().text(query, max_results=10, backend=backend) or []
        except Exception as e:
            log.info("search %s failed for %r: %s", backend, query, e)
            return []

    pool = cf.ThreadPoolExecutor(max_workers=len(BACKENDS))
    try:
        for f in cf.as_completed([pool.submit(one, b) for b in BACKENDS], timeout=timeout):
            if hits := f.result():
                return hits
    except cf.TimeoutError:
        log.info("search timed out for %r", query)
    finally:
        pool.shutdown(wait=False, cancel_futures=True)  # the slower engines finish in the background
    return []


async def _wiki_hits(client: httpx.AsyncClient, query: str) -> list[dict]:
    try:
        r = await client.get("https://en.wikipedia.org/w/api.php", params={
            "action": "query", "list": "search", "srsearch": query, "srlimit": 3, "format": "json"})
        found = r.json()["query"]["search"]
    except Exception as e:
        log.info("wikipedia search failed for %r: %s", query, e)
        return []
    return [{"href": "https://en.wikipedia.org/wiki/" + h["title"].replace(" ", "_"),
             "body": re.sub(r"<[^>]+>", "", h.get("snippet", ""))} for h in found]


async def _wiki_intro(client: httpx.AsyncClient, query: str, words: int) -> tuple[str, str, str]:
    """(title, url, text) of the best Wikipedia article's intro: a source that always answers."""
    hits = await _wiki_hits(client, query)
    if not hits:
        return "", "", ""
    title = hits[0]["href"].rsplit("/", 1)[1]
    try:
        r = await client.get("https://en.wikipedia.org/w/api.php", params={
            "action": "query", "prop": "extracts", "exintro": 1, "explaintext": 1, "redirects": 1,
            "titles": title.replace("_", " "), "format": "json"})
        page = next(iter(r.json()["query"]["pages"].values()))
        text = page.get("extract", "")
    except Exception as e:
        log.info("wikipedia intro failed for %r: %s", title, e)
        return "", "", ""
    return f"{page.get('title', title)} - Wikipedia", hits[0]["href"], _words(text, words)


async def research(query: str, pages: int = 3, words_per_page: int = 400, deadline: float = 3.0) -> str:
    """Search, then read the top pages for at most `deadline` seconds: a slow site never holds up the answer
    (its search snippet is used instead), and the snippets of the other hits come along — they usually hold
    the answer to a quick question already. Sites that refused us lately go to the back of the line, and
    Wikipedia's intro is read alongside, so a blocked page is replaced by a real one, not just a snippet."""
    hits = await asyncio.to_thread(search, query)
    if not hits:
        async with _client() as client:
            hits = await _wiki_hits(client, query)
    if not hits:
        return "web search failed: no results from any search engine"
    refused = _refused()
    urls, seen = [], set()
    for h in sorted(hits, key=lambda h: urlparse(h["href"]).netloc.lower() in refused):  # stable: order kept
        host = urlparse(h["href"]).netloc.lower()
        if any(s in host for s in SKIP_HOSTS) or host in seen:
            continue
        seen.add(host)  # one page per site → more diverse sources
        urls.append(h)
        if len(urls) >= pages + 2:  # a couple of spares for pages that fail
            break
    if skipped := [u["href"] for u in urls if urlparse(u["href"]).netloc.lower() in refused]:
        log.info("research %r: %d site(s) refused us lately, read last", query, len(skipped))
    has_wiki = any("wikipedia.org" in urlparse(h["href"]).netloc for h in urls)
    async with _client() as client:
        tasks = [asyncio.create_task(fetch_text(client, h["href"], words_per_page))
                 if urlparse(h["href"]).netloc.lower() not in refused else None for h in urls]
        wiki = None if has_wiki else asyncio.create_task(_wiki_intro(client, query, words_per_page))
        live = [t for t in tasks + [wiki] if t]
        _, slow = await asyncio.wait(live, timeout=deadline)
        for t in slow:
            t.cancel()
        results = []
        for t in tasks:
            try:
                results.append(t.result() if t and t.done() and not t.cancelled() else ("", ""))
            except Exception:  # noqa: BLE001 - a failed page just falls back to its snippet
                results.append(("", ""))
        try:
            wiki_page = wiki.result() if wiki and wiki.done() and not wiki.cancelled() else ("", "", "")
        except Exception:  # noqa: BLE001
            wiki_page = ("", "", "")
        if slow:
            log.info("research %r: %d slow page(s) skipped after %.0fs", query, len(slow), deadline)
    full = [(title or urlparse(h["href"]).netloc, h["href"], text)
            for h, (title, text) in zip(urls, results) if len(text.split()) > 40]
    if wiki_page[2] and len(wiki_page[2].split()) > 20:
        full.append(wiki_page)
    picked = full[:pages]
    used = {u for _, u, _ in picked}
    for h in urls:  # not enough readable pages: fill up with search snippets
        if len(picked) >= pages:
            break
        if h["href"] not in used and (body := _words(h.get("body", ""), 80)):
            picked.append((urlparse(h["href"]).netloc, h["href"], body))
            used.add(h["href"])
    out = [f"[{i}] {t} — {u}\n{b}" for i, (t, u, b) in enumerate(picked, 1)]
    snippets = [f"- {urlparse(h['href']).netloc}: {_words(h.get('body', ''), 45)}" for h in hits
                if h.get("href") not in used and h.get("body")][:5]
    if not out and not snippets:
        return "no usable results"
    more = ("\n\nMore search snippets:\n" + "\n".join(snippets)) if snippets else ""
    return f"Web results for '{query}' — cite sources by site name:\n\n" + "\n\n".join(out) + more


async def read_url(url: str, words: int = 2200) -> str:
    async with _client() as client:
        title, text = await fetch_text(client, url, words)
    if not text:
        return f"couldn't read {url} (blocked, not HTML, or needs JavaScript)"
    return f"{title} ({urlparse(url).netloc})\n{text}"


def first_url(text: str) -> str | None:
    m = URL_RE.search(text or "")
    return m.group(0).rstrip(").,;]") if m else None
