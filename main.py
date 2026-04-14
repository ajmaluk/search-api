import asyncio
import hashlib
import time
import re
from urllib.parse import quote_plus, urljoin, urlparse

import httpx
from bs4 import BeautifulSoup
from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware

try:
    import trafilatura
    HAS_TRAFILATURA = True
except ImportError:
    HAS_TRAFILATURA = False

app = FastAPI(title="Free AI Search API", version="2.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ─── In-memory cache ──────────────────────────────────────────────
_cache: dict = {}
CACHE_TTL = 300  # 5 minutes

def cache_get(key: str):
    entry = _cache.get(key)
    if entry and time.time() - entry["ts"] < CACHE_TTL:
        return entry["data"]
    return None

def cache_set(key: str, data):
    _cache[key] = {"data": data, "ts": time.time()}

def cache_key(*args) -> str:
    return hashlib.md5("|".join(str(a) for a in args).encode()).hexdigest()

# ─── User-agent rotation ──────────────────────────────────────────
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/123.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/122.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:124.0) Gecko/20100101 Firefox/124.0",
]
_ua_index = 0

def next_ua() -> str:
    global _ua_index
    ua = USER_AGENTS[_ua_index % len(USER_AGENTS)]
    _ua_index += 1
    return ua

def get_headers():
    return {
        "User-Agent": next_ua(),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate",
        "DNT": "1",
        "Connection": "keep-alive",
    }

# ─── Search engines ───────────────────────────────────────────────

async def search_duckduckgo(query: str, max_results: int = 8) -> list:
    url = "https://html.duckduckgo.com/html/"
    params = {"q": query, "b": "", "kl": "us-en"}
    try:
        async with httpx.AsyncClient(timeout=12, follow_redirects=True) as client:
            r = await client.post(url, data=params, headers=get_headers())
        soup = BeautifulSoup(r.text, "html.parser")
        results = []
        for a in soup.select(".result__a"):
            href = a.get("href", "")
            # DDG wraps links, extract real URL
            if "uddg=" in href:
                from urllib.parse import unquote, parse_qs
                qs = parse_qs(urlparse(href).query)
                href = unquote(qs.get("uddg", [""])[0])
            title = a.get_text(strip=True)
            snippet_el = a.find_parent(".result")
            snippet = ""
            if snippet_el:
                snip = snippet_el.select_one(".result__snippet")
                snippet = snip.get_text(strip=True) if snip else ""
            if href and href.startswith("http"):
                results.append({"title": title, "url": href, "snippet": snippet, "source": "duckduckgo"})
        return results[:max_results]
    except Exception as e:
        return []


async def search_brave(query: str, max_results: int = 8) -> list:
    """Brave Search (no API key needed for HTML endpoint)"""
    url = f"https://search.brave.com/search?q={quote_plus(query)}&source=web"
    try:
        async with httpx.AsyncClient(timeout=12, follow_redirects=True) as client:
            r = await client.get(url, headers=get_headers())
        soup = BeautifulSoup(r.text, "html.parser")
        results = []
        for item in soup.select(".snippet"):
            a = item.select_one("a.heading-serpresult")
            if not a:
                a = item.select_one("a")
            if not a:
                continue
            href = a.get("href", "")
            title = a.get_text(strip=True)
            snip_el = item.select_one(".snippet-description")
            snippet = snip_el.get_text(strip=True) if snip_el else ""
            if href and href.startswith("http"):
                results.append({"title": title, "url": href, "snippet": snippet, "source": "brave"})
        return results[:max_results]
    except Exception:
        return []


async def search_mojeek(query: str, max_results: int = 8) -> list:
    """Mojeek – independent search engine, no rate limiting"""
    url = f"https://www.mojeek.com/search?q={quote_plus(query)}"
    try:
        async with httpx.AsyncClient(timeout=12, follow_redirects=True) as client:
            r = await client.get(url, headers=get_headers())
        soup = BeautifulSoup(r.text, "html.parser")
        results = []
        for li in soup.select("ul.results-standard li"):
            a = li.select_one("a.title")
            if not a:
                continue
            href = a.get("href", "")
            title = a.get_text(strip=True)
            snip_el = li.select_one("p.s")
            snippet = snip_el.get_text(strip=True) if snip_el else ""
            if href and href.startswith("http"):
                results.append({"title": title, "url": href, "snippet": snippet, "source": "mojeek"})
        return results[:max_results]
    except Exception:
        return []


async def search_news_hn(query: str, max_results: int = 8) -> list:
    """Hacker News Algolia API – completely free, no key"""
    url = f"https://hn.algolia.com/api/v1/search?query={quote_plus(query)}&tags=story&hitsPerPage={max_results}"
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(url)
        data = r.json()
        results = []
        for hit in data.get("hits", []):
            url_ = hit.get("url") or f"https://news.ycombinator.com/item?id={hit.get('objectID')}"
            results.append({
                "title": hit.get("title", ""),
                "url": url_,
                "snippet": f"Points: {hit.get('points',0)} | Comments: {hit.get('num_comments',0)}",
                "source": "hackernews",
            })
        return results
    except Exception:
        return []


async def search_wikipedia(query: str, max_results: int = 3) -> list:
    """Wikipedia REST API – completely free"""
    url = f"https://en.wikipedia.org/w/api.php"
    params = {
        "action": "query", "list": "search", "srsearch": query,
        "srlimit": max_results, "format": "json", "utf8": 1,
    }
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(url, params=params)
        data = r.json()
        results = []
        for item in data.get("query", {}).get("search", []):
            title = item["title"]
            snippet = BeautifulSoup(item.get("snippet", ""), "html.parser").get_text()
            wiki_url = f"https://en.wikipedia.org/wiki/{title.replace(' ', '_')}"
            results.append({"title": title, "url": wiki_url, "snippet": snippet, "source": "wikipedia"})
        return results
    except Exception:
        return []


async def search_reddit(query: str, max_results: int = 5) -> list:
    """Reddit JSON search – no auth needed"""
    url = f"https://www.reddit.com/search.json?q={quote_plus(query)}&sort=relevance&limit={max_results}"
    try:
        async with httpx.AsyncClient(timeout=10, follow_redirects=True) as client:
            r = await client.get(url, headers={"User-Agent": "search-api/2.0"})
        data = r.json()
        results = []
        for post in data.get("data", {}).get("children", []):
            d = post["data"]
            results.append({
                "title": d.get("title", ""),
                "url": f"https://reddit.com{d.get('permalink','')}",
                "snippet": d.get("selftext", "")[:300],
                "source": "reddit",
            })
        return results
    except Exception:
        return []

# ─── Content extraction ───────────────────────────────────────────

async def scrape_url(url: str, max_chars: int = 4000) -> dict:
    try:
        async with httpx.AsyncClient(
            timeout=15, follow_redirects=True,
            headers=get_headers()
        ) as client:
            r = await client.get(url)

        html = r.text

        # Try trafilatura first (best quality)
        if HAS_TRAFILATURA:
            text = trafilatura.extract(
                html,
                include_comments=False,
                include_tables=True,
                no_fallback=False,
            )
            if text and len(text) > 100:
                return {"url": url, "content": text[:max_chars], "method": "trafilatura", "ok": True}

        # Fallback: BeautifulSoup paragraph extraction
        soup = BeautifulSoup(html, "html.parser")
        for tag in soup(["script", "style", "noscript", "nav", "footer", "header", "aside"]):
            tag.decompose()

        # Try article/main first
        main = soup.find("article") or soup.find("main") or soup.find(id=re.compile(r"content|main|article", re.I))
        container = main if main else soup

        paragraphs = [p.get_text(strip=True) for p in container.find_all("p") if len(p.get_text(strip=True)) > 40]
        text = "\n\n".join(paragraphs)

        if len(text) < 100:
            text = soup.get_text(separator="\n", strip=True)
            text = re.sub(r'\n{3,}', '\n\n', text)

        return {"url": url, "content": text[:max_chars], "method": "beautifulsoup", "ok": True}

    except Exception as e:
        return {"url": url, "content": "", "method": "failed", "ok": False, "error": str(e)}


def deduplicate(results: list) -> list:
    seen_domains = set()
    seen_titles = set()
    out = []
    for r in results:
        domain = urlparse(r["url"]).netloc
        title_key = r["title"].lower()[:60]
        if domain not in seen_domains and title_key not in seen_titles:
            seen_domains.add(domain)
            seen_titles.add(title_key)
            out.append(r)
    return out

# ─── API Endpoints ────────────────────────────────────────────────

@app.get("/")
def root():
    return {
        "name": "Free AI Search API",
        "version": "2.0",
        "endpoints": {
            "/search": "Multi-engine web search + content extraction",
            "/quick":  "Fast search (results only, no scraping)",
            "/scrape": "Scrape a single URL",
            "/news":   "Hacker News search",
            "/wiki":   "Wikipedia search",
            "/reddit": "Reddit search",
        },
    }


@app.get("/search")
async def search(
    query: str = Query(..., description="Search query"),
    max_results: int = Query(5, ge=1, le=20),
    engines: str = Query("ddg,brave,mojeek", description="Comma-separated: ddg,brave,mojeek,wiki,hn,reddit"),
    scrape: bool = Query(True, description="Extract page content"),
    max_chars: int = Query(3000, ge=500, le=8000),
):
    ck = cache_key("search", query, max_results, engines, scrape)
    cached = cache_get(ck)
    if cached:
        return {**cached, "cached": True}

    engine_list = [e.strip() for e in engines.split(",")]

    tasks = []
    if "ddg"    in engine_list: tasks.append(search_duckduckgo(query, max_results))
    if "brave"  in engine_list: tasks.append(search_brave(query, max_results))
    if "mojeek" in engine_list: tasks.append(search_mojeek(query, max_results))
    if "wiki"   in engine_list: tasks.append(search_wikipedia(query, 3))
    if "hn"     in engine_list: tasks.append(search_news_hn(query, max_results))
    if "reddit" in engine_list: tasks.append(search_reddit(query, 5))

    all_results_nested = await asyncio.gather(*tasks, return_exceptions=True)

    # Merge and deduplicate
    merged = []
    for batch in all_results_nested:
        if isinstance(batch, list):
            merged.extend(batch)

    merged = deduplicate(merged)[:max_results]

    # Scrape content
    if scrape and merged:
        scrape_tasks = [scrape_url(r["url"], max_chars) for r in merged]
        contents = await asyncio.gather(*scrape_tasks)
        for i, r in enumerate(merged):
            r["content"] = contents[i].get("content", "")
            r["scrape_ok"] = contents[i].get("ok", False)

    result = {
        "query": query,
        "total": len(merged),
        "results": merged,
        "cached": False,
    }
    cache_set(ck, result)
    return result


@app.get("/quick")
async def quick_search(
    query: str = Query(...),
    max_results: int = Query(10, ge=1, le=20),
):
    """Fast search without page scraping"""
    ck = cache_key("quick", query, max_results)
    cached = cache_get(ck)
    if cached:
        return {**cached, "cached": True}

    results_nested = await asyncio.gather(
        search_duckduckgo(query, max_results),
        search_brave(query, max_results),
        return_exceptions=True,
    )
    merged = []
    for batch in results_nested:
        if isinstance(batch, list):
            merged.extend(batch)

    merged = deduplicate(merged)[:max_results]
    result = {"query": query, "total": len(merged), "results": merged, "cached": False}
    cache_set(ck, result)
    return result


@app.get("/scrape")
async def scrape_endpoint(
    url: str = Query(..., description="Full URL to scrape"),
    max_chars: int = Query(5000, ge=500, le=10000),
):
    ck = cache_key("scrape", url)
    cached = cache_get(ck)
    if cached:
        return {**cached, "cached": True}
    result = await scrape_url(url, max_chars)
    cache_set(ck, result)
    return result


@app.get("/news")
async def news_search(
    query: str = Query(...),
    max_results: int = Query(10, ge=1, le=30),
):
    ck = cache_key("news", query, max_results)
    cached = cache_get(ck)
    if cached:
        return {**cached, "cached": True}
    results = await search_news_hn(query, max_results)
    result = {"query": query, "total": len(results), "results": results}
    cache_set(ck, result)
    return result


@app.get("/wiki")
async def wiki_search(
    query: str = Query(...),
    max_results: int = Query(5, ge=1, le=10),
    full_content: bool = Query(False),
):
    ck = cache_key("wiki", query, max_results, full_content)
    cached = cache_get(ck)
    if cached:
        return {**cached, "cached": True}
    results = await search_wikipedia(query, max_results)
    if full_content and results:
        tasks = [scrape_url(r["url"], 6000) for r in results]
        contents = await asyncio.gather(*tasks)
        for i, r in enumerate(results):
            r["content"] = contents[i].get("content", "")
    result = {"query": query, "total": len(results), "results": results}
    cache_set(ck, result)
    return result


@app.get("/reddit")
async def reddit_search(
    query: str = Query(...),
    max_results: int = Query(10, ge=1, le=25),
):
    ck = cache_key("reddit", query, max_results)
    cached = cache_get(ck)
    if cached:
        return {**cached, "cached": True}
    results = await search_reddit(query, max_results)
    result = {"query": query, "total": len(results), "results": results}
    cache_set(ck, result)
    return result


@app.get("/health")
def health():
    return {"status": "ok", "cache_entries": len(_cache)}
