import asyncio
import hashlib
import time
import re
import logging
from urllib.parse import quote_plus, urlparse, parse_qs, unquote
from typing import Optional, List, Dict, Any

import httpx
from bs4 import BeautifulSoup
from fastapi import FastAPI, Query, HTTPException
from fastapi.middleware.cors import CORSMiddleware

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler()]
)
logger = logging.getLogger(__name__)

try:
    import trafilatura
    HAS_TRAFILATURA = True
except ImportError:
    HAS_TRAFILATURA = False
    logger.warning("trafilatura not installed - install with: pip install trafilatura")

app = FastAPI(title="AI Search API", version="2.2", description="Web search API for AI chat applications")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ─── Cache ──────────────────────────────────────────────
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

# ─── Headers & UA Rotation ──────────────────────────────
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:124.0) Gecko/20100101 Firefox/124.0",
]
_ua_idx = 0

def next_ua() -> str:
    global _ua_idx
    ua = USER_AGENTS[_ua_idx % len(USER_AGENTS)]
    _ua_idx += 1
    return ua

def get_headers(referer: Optional[str] = None) -> dict:
    h = {
        "User-Agent": next_ua(),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br",  # Important: request compression
        "DNT": "1", "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
        "Sec-Fetch-Dest": "document", "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none", "Sec-Fetch-User": "?1",
    }
    if referer: h["Referer"] = referer
    return h

# ─── Search Functions ───────────────────────────────────

async def search_duckduckgo(query: str, max_results: int = 8) -> list:
    """DuckDuckGo HTML search - updated selectors + redirect handling"""
    url = "https://html.duckduckgo.com/html/"
    params = {"q": query, "kl": "us-en"}
    results = []
    
    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
            r = await client.post(url, data=params, headers=get_headers())
            r.raise_for_status()
        
        soup = BeautifulSoup(r.text, "html.parser")
        
        # Multiple selector patterns for resilience
        selectors = [
            ".result__a", "a.result__a", ".links_main a", 
            "div.web-result a", ".result__title a"
        ]
        
        for selector in selectors:
            for item in soup.select(selector):
                href = item.get("href", "")
                # Unwrap DDG redirect
                if "uddg=" in href:
                    qs = parse_qs(urlparse(href).query)
                    href = unquote(qs.get("uddg", [""])[0])
                if not href or href.startswith(("/html", "https://duckduckgo.com", "javascript:")):
                    continue
                
                title = item.get_text(strip=True)
                if not title or len(title) < 3: continue
                
                snippet = ""
                parent = item.find_parent(".result, div.result, .web-result, .result__body")
                if parent:
                    snip = parent.select_one(".result__snippet, .result__desc, .result__extras")
                    if snip: snippet = snip.get_text(strip=True)
                
                results.append({"title": title, "url": href, "snippet": snippet[:300], "source": "duckduckgo"})
                if len(results) >= max_results: break
            if results: break  # If we found results with this selector, stop
            
        return results[:max_results]
    except Exception as e:
        logger.warning(f"DDG error: {type(e).__name__}: {e}")
        return []

async def search_brave(query: str, max_results: int = 8) -> list:
    """Brave Search - multiple selector fallbacks"""
    url = f"https://search.brave.com/search?q={quote_plus(query)}&source=web"
    results = []
    
    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
            r = await client.get(url, headers=get_headers("https://search.brave.com"))
            r.raise_for_status()
        
        # Check for anti-bot
        if "Just a moment" in r.text or "checking your browser" in r.text.lower():
            logger.warning("Brave returned anti-bot page")
            return []
        
        soup = BeautifulSoup(r.text, "html.parser")
        
        # Try multiple result patterns
        for item in soup.select(".snippet, .result, [data-snippet], .search-result, div.pdbl"):
            a = item.select_one("a.heading-serpresult, a.result-header, a.title, a[href^='http']")
            if not a: continue
            href = a.get("href", "")
            if not href or not href.startswith("http"): continue
            
            title = a.get_text(strip=True)
            if not title or len(title) < 3: continue
            
            snip = item.select_one(".snippet-description, .result-desc, [data-snippet], .result-snippet")
            snippet = snip.get_text(strip=True) if snip else ""
            
            results.append({"title": title, "url": href, "snippet": snippet[:300], "source": "brave"})
            if len(results) >= max_results: break
        return results[:max_results]
    except Exception as e:
        logger.warning(f"Brave error: {type(e).__name__}: {e}")
        return []

async def search_mojeek(query: str, max_results: int = 8) -> list:
    """Mojeek - independent search engine"""
    url = f"https://www.mojeek.com/search?q={quote_plus(query)}"
    results = []
    
    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
            r = await client.get(url, headers=get_headers("https://www.mojeek.com"))
            r.raise_for_status()
        
        soup = BeautifulSoup(r.text, "html.parser")
        
        for li in soup.select("ul.results-standard li, div.results li, .result, .result-standard"):
            a = li.select_one("a.title, h2 a, .result-title a, a[href^='http']")
            if not a: continue
            href = a.get("href", "")
            if not href or not href.startswith("http"): continue
            
            title = a.get_text(strip=True)
            if not title or len(title) < 3: continue
            
            snip = li.select_one("p.s, .result-desc, .snippet, .result-snippet")
            snippet = snip.get_text(strip=True) if snip else ""
            
            results.append({"title": title, "url": href, "snippet": snippet[:300], "source": "mojeek"})
            if len(results) >= max_results: break
        return results[:max_results]
    except Exception as e:
        logger.warning(f"Mojeek error: {type(e).__name__}: {e}")
        return []

async def search_hn(query: str, max_results: int = 8) -> list:
    """Hacker News via Algolia API - most reliable"""
    url = f"https://hn.algolia.com/api/v1/search?query={quote_plus(query)}&tags=story&hitsPerPage={max_results}"
    try:
        async with httpx.AsyncClient(timeout=12) as client:
            r = await client.get(url, headers={"User-Agent": "search-api/2.2"})
            r.raise_for_status()
        data = r.json()
        results = []
        for hit in data.get("hits", []):
            title = hit.get("title", "")
            if not title or len(title) < 3: continue
            url_ = hit.get("url") or f"https://news.ycombinator.com/item?id={hit.get('objectID')}"
            results.append({
                "title": title, "url": url_,
                "snippet": f"▲{hit.get('points',0)} 💬{hit.get('num_comments',0)}",
                "source": "hackernews"
            })
        return results[:max_results]
    except Exception as e:
        logger.warning(f"HN error: {type(e).__name__}: {e}")
        return []

async def search_wikipedia(query: str, max_results: int = 3) -> list:
    """Wikipedia API - official endpoint with proper UA"""
    url = "https://en.wikipedia.org/w/api.php"
    params = {
        "action": "query", "list": "search", "srsearch": query, 
        "srlimit": max_results, "format": "json", "origin": "*"
    }
    headers = {"User-Agent": "search-api/2.2 (AI chat integration)"}
    try:
        async with httpx.AsyncClient(timeout=12) as client:
            r = await client.get(url, params=params, headers=headers)
            r.raise_for_status()
        data = r.json()
        results = []
        for item in data.get("query", {}).get("search", []):
            title = item.get("title", "")
            if not title: continue
            snippet = BeautifulSoup(item.get("snippet", ""), "html.parser").get_text(strip=True)
            results.append({
                "title": title,
                "url": f"https://en.wikipedia.org/wiki/{title.replace(' ', '_')}",
                "snippet": snippet[:300],
                "source": "wikipedia"
            })
        return results[:max_results]
    except Exception as e:
        logger.warning(f"Wikipedia error: {type(e).__name__}: {e}")
        return []

async def search_reddit(query: str, max_results: int = 5) -> list:
    """Reddit JSON API - requires proper UA to avoid 429"""
    url = f"https://www.reddit.com/search.json?q={quote_plus(query)}&sort=relevance&limit={max_results}"
    headers = {"User-Agent": "search-api/2.2 by u/ai_search_bot (contact: api@example.com)", "Accept": "application/json"}
    try:
        async with httpx.AsyncClient(timeout=12, follow_redirects=True) as client:
            r = await client.get(url, headers=headers)
            if r.status_code == 429:
                logger.warning("Reddit rate limited")
                return []
            r.raise_for_status()
        data = r.json()
        results = []
        for post in data.get("data", {}).get("children", []):
            d = post["data"]
            title, permalink = d.get("title", ""), d.get("permalink", "")
            if not title or not permalink or len(title) < 3: continue
            # Get text content
            text = d.get("selftext", "")
            if not text and d.get("preview", {}).get("images"):
                text = "Image post"
            results.append({
                "title": title,
                "url": f"https://reddit.com{permalink}",
                "snippet": text[:300] if text else "",
                "source": "reddit"
            })
        return results[:max_results]
    except Exception as e:
        logger.warning(f"Reddit error: {type(e).__name__}: {e}")
        return []

# ─── Content Scraping (FIXED: encoding/gzip handling) ──

async def scrape_url(url: str, max_chars: int = 4000) -> dict:
    """Extract main content with proper encoding/compression handling"""
    parsed = urlparse(url)
    if parsed.scheme not in ["http", "https"]:
        return {"url": url, "content": "", "method": "invalid", "ok": False}
    
    # Skip known anti-bot/CDN domains
    if any(x in parsed.netloc.lower() for x in ["cloudflare", "akamai", "incapsula", "perimeterx", "botprotect"]):
        return {"url": url, "content": "", "method": "blocked", "ok": False}
    
    try:
        # httpx automatically handles gzip/br decoding when Accept-Encoding is set
        async with httpx.AsyncClient(
            timeout=20, 
            follow_redirects=True, 
            headers=get_headers(url)
        ) as client:
            r = await client.get(url)
            r.raise_for_status()
        
        # CRITICAL: Get text with proper encoding detection
        # httpx .text uses response.encoding which is auto-detected from headers/charset
        html = r.text
        
        # Detect anti-bot pages (case-insensitive)
        html_lower = html.lower()
        if any(x in html_lower for x in [
            "just a moment", "checking your browser", "cloudflare", 
            "access denied", "permission denied", "captcha", "ray id"
        ]):
            return {"url": url, "content": "", "method": "antibot", "ok": False}
        
        # Try trafilatura first (best quality, handles encoding internally)
        if HAS_TRAFILATURA:
            try:
                text = trafilatura.extract(
                    html, 
                    include_comments=False, 
                    include_tables=True, 
                    favor_precision=True,
                    no_fallback=False
                )
                if text and len(text.strip()) > 100:
                    return {"url": url, "content": text[:max_chars], "method": "trafilatura", "ok": True}
            except Exception as e:
                logger.debug(f"Trafilatura failed for {url}: {e}")
        
        # BeautifulSoup fallback with encoding-safe parsing
        soup = BeautifulSoup(html, "html.parser")
        
        # Remove non-content elements
        for tag in soup(["script", "style", "noscript", "nav", "footer", "header", "aside", "iframe", "form"]):
            tag.decompose()
        
        # Find main content container (multiple strategies)
        container = None
        for selector in [
            "article", "main", "[role='main']", "#content", ".content", 
            "[itemprop='articleBody']", ".post-content", ".entry-content",
            ".article-body", "#main-content", ".main-content"
        ]:
            container = soup.select_one(selector)
            if container and len(container.get_text(strip=True)) > 100:
                break
        
        if not container:
            container = soup
        
        # Extract meaningful paragraphs only
        paragraphs = []
        for p in container.find_all("p"):
            text = p.get_text(strip=True)
            if len(text) > 50 and not text.startswith(("©", "Privacy", "Terms", "Cookie")):
                paragraphs.append(text)
        
        # Fallback to all text if no good paragraphs
        if not paragraphs:
            text = container.get_text(separator="\n", strip=True)
            # Clean up whitespace
            text = re.sub(r'\n{3,}', '\n\n', text)
            text = re.sub(r'[ \t]+', ' ', text)
        else:
            text = "\n\n".join(paragraphs)
        
        if not text.strip() or len(text.strip()) < 50:
            return {"url": url, "content": "", "method": "empty", "ok": False}
            
        return {"url": url, "content": text[:max_chars], "method": "bs4", "ok": True}
        
    except httpx.HTTPStatusError as e:
        logger.warning(f"Scrape HTTP {e.response.status_code} for {url}")
        return {"url": url, "content": "", "method": f"http_{e.response.status_code}", "ok": False}
    except httpx.RequestError as e:
        logger.warning(f"Scrape request error for {url}: {type(e).__name__}")
        return {"url": url, "content": "", "method": "request_error", "ok": False}
    except Exception as e:
        logger.warning(f"Scrape error for {url}: {type(e).__name__}: {str(e)[:100]}")
        return {"url": url, "content": "", "method": "failed", "ok": False, "error": str(e)[:100]}

# ─── Utilities ──────────────────────────────────────────

def deduplicate(results: List[Dict]) -> List[Dict]:
    """Remove duplicates by domain + normalized title"""
    seen, out = set(), []
    for r in results:
        if not isinstance(r, dict) or not r.get("url") or not r.get("title"): 
            continue
        domain = urlparse(r["url"]).netloc.lower()
        title_norm = re.sub(r'[^a-z0-9]', '', r["title"].lower())[:40]
        key = f"{domain}:{title_norm}"
        if key not in seen:
            seen.add(key)
            out.append(r)
    return out

def safe_results(results: Any, max_results: int) -> List[Dict]:
    """Ensure we always return a proper list of result dicts"""
    if not isinstance(results, list):
        logger.error(f"Unexpected results type: {type(results)}")
        return []
    return [r for r in results if isinstance(r, dict) and r.get("url") and r.get("title")][:max_results]

# ─── API Endpoints ──────────────────────────────────────

@app.get("/")
def root():
    return {
        "name": "AI Search API", "version": "2.2", "status": "ready",
        "usage": "For AI chat: call /search?query=...&engines=ddg,hn,wiki&scrape=true",
        "engines": ["ddg", "brave", "mojeek", "wiki", "hn", "reddit"],
        "endpoints": {
            "/search": "Multi-engine + optional scraping",
            "/quick": "Fast results only",
            "/scrape": "Single URL extraction",
            "/health": "Status check"
        }
    }

@app.get("/search")
async def search(
    query: str = Query(..., min_length=1, description="Search query"),
    max_results: int = Query(5, ge=1, le=20, description="Max results to return"),
    engines: str = Query("ddg,hn,wiki", description="Comma-separated: ddg,brave,mojeek,wiki,hn,reddit"),
    scrape: bool = Query(True, description="Extract page content for AI context"),
    max_chars: int = Query(2500, ge=500, le=6000, description="Max chars per scraped page"),
):
    """Primary endpoint for AI chat integration"""
    ck = cache_key("search", query, max_results, engines, scrape, max_chars)
    cached = cache_get(ck)
    if cached: 
        return {**cached, "cached": True}
    
    engine_list = [e.strip().lower() for e in engines.split(",") if e.strip()]
    engine_map = {
        "ddg": search_duckduckgo, "brave": search_brave, "mojeek": search_mojeek,
        "wiki": search_wikipedia, "hn": search_hn, "reddit": search_reddit,
    }
    
    # Build tasks with appropriate limits per engine
    tasks = []
    for eng in engine_list:
        if eng in engine_map:
            # Some engines work better with smaller limits
            limit = {"wiki": 3, "reddit": 5, "hn": 8}.get(eng, max_results)
            tasks.append(engine_map[eng](query, min(limit, max_results)))
    
    # Gather with exception handling
    batches = await asyncio.gather(*tasks, return_exceptions=True)
    
    # Merge results safely
    merged = []
    for batch in batches:
        if isinstance(batch, list):
            merged.extend(safe_results(batch, max_results))
        elif isinstance(batch, Exception):
            logger.warning(f"Engine task failed: {type(batch).__name__}: {batch}")
    
    merged = deduplicate(merged)[:max_results]
    
    # Scrape for AI context (only if we have results)
    if scrape and merged:
        scrape_tasks = [scrape_url(r["url"], max_chars) for r in merged]
        contents = await asyncio.gather(*scrape_tasks, return_exceptions=True)
        for i, r in enumerate(merged):
            c = contents[i] if i < len(contents) and isinstance(contents[i], dict) else {}
            r["content"] = c.get("content", "") if c.get("ok") else ""
            r["scrape_ok"] = c.get("ok", False)
            r["scrape_method"] = c.get("method", "none")
    
    # Ensure consistent response structure
    result = {
        "query": query,
        "total": len(merged),
        "results": merged,  # Always a list of dicts
        "cached": False,
    }
    cache_set(ck, result)
    return result

@app.get("/quick")
async def quick_search(
    query: str = Query(..., min_length=1),
    max_results: int = Query(10, ge=1, le=20),
):
    """Fast metadata-only search (no scraping)"""
    ck = cache_key("quick", query, max_results)
    cached = cache_get(ck)
    if cached: 
        return {**cached, "cached": True}
    
    # Use most reliable engines for quick search
    results = await asyncio.gather(
        search_duckduckgo(query, max_results),
        search_hn(query, max_results),
        return_exceptions=True,
    )
    
    merged = []
    for batch in results:
        if isinstance(batch, list):
            merged.extend(safe_results(batch, max_results))
    
    merged = deduplicate(merged)[:max_results]
    result = {"query": query, "total": len(merged), "results": merged, "cached": False}
    cache_set(ck, result)
    return result

@app.get("/scrape")
async def scrape_endpoint(
    url: str = Query(..., description="Full URL to scrape (http/https)"),
    max_chars: int = Query(4000, ge=500, le=8000),
):
    """Scrape single URL for AI context"""
    parsed = urlparse(url)
    if parsed.scheme not in ["http", "https"]:
        return {"url": url, "content": "", "ok": False, "error": "http/https required", "method": "invalid"}
    
    ck = cache_key("scrape", url, max_chars)
    cached = cache_get(ck)
    if cached: 
        return {**cached, "cached": True}
    
    result = await scrape_url(url, max_chars)
    cache_set(ck, result)
    return result

@app.get("/health")
def health():
    return {"status": "ok", "cache_entries": len(_cache), "version": "2.2"}

# For Render deployment: uvicorn main:app --host 0.0.0.0 --port $PORT
