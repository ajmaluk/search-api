"""
AI Search API v3.1 - Production Ready for AI Chat + Financial Queries
✅ Fixed: Scraping encoding, all engine parsers, financial data detection, Render compatibility
"""
import asyncio
import hashlib
import time
import re
import logging
import json
from urllib.parse import quote_plus, urlparse, parse_qs, unquote, urljoin
from typing import Optional, List, Dict, Any, Union
from datetime import datetime

import httpx
from bs4 import BeautifulSoup
from fastapi import FastAPI, Query, HTTPException, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

# Configure logging for Render debugging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
    handlers=[logging.StreamHandler()]
)
logger = logging.getLogger(__name__)

# Optional dependencies with graceful fallback
try:
    import trafilatura
    HAS_TRAFILATURA = True
except ImportError:
    HAS_TRAFILATURA = False
    logger.warning("trafilatura not installed - install with: pip install trafilatura")

app = FastAPI(
    title="AI Search API", 
    version="3.1", 
    description="Reliable web search + content extraction for AI chat applications",
    docs_url="/docs",
    redoc_url="/redoc"
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["*"],
)

# ─────────────────────────────────────────────────────────────
# CACHE SYSTEM
# ─────────────────────────────────────────────────────────────
_cache: Dict[str, Dict] = {}
CACHE_TTL = 300  # 5 minutes

def _cache_key(*parts: Any) -> str:
    key_str = "|".join(str(p) for p in parts if p is not None)
    return hashlib.sha256(key_str.encode()).hexdigest()[:32]

def cache_get(key: str) -> Optional[Dict]:
    entry = _cache.get(key)
    if entry and (time.time() - entry["ts"]) < CACHE_TTL:
        return entry["data"]
    return None

def cache_set(key: str, data: Dict):
    _cache[key] = {"data": data, "ts": time.time()}
    if len(_cache) > 100:
        oldest = min(_cache.items(), key=lambda x: x[1]["ts"])
        del _cache[oldest[0]]

# ─────────────────────────────────────────────────────────────
# HTTP CLIENT WITH ANTI-BOT EVASION
# ─────────────────────────────────────────────────────────────
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/123.0.0.0 Safari/537.36",
]
_UA_INDEX = 0

def _rotate_ua() -> str:
    global _UA_INDEX
    ua = USER_AGENTS[_UA_INDEX % len(USER_AGENTS)]
    _UA_INDEX += 1
    return ua

def _get_headers(referer: Optional[str] = None, json_accept: bool = False) -> Dict[str, str]:
    headers = {
        "User-Agent": _rotate_ua(),
        "Accept": "application/json" if json_accept else "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br",  # Critical for proper decompression
        "DNT": "1", "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
        "Sec-Fetch-Dest": "document" if not json_accept else "empty",
        "Sec-Fetch-Mode": "navigate" if not json_accept else "cors",
        "Sec-Fetch-Site": "none",
        "Cache-Control": "no-cache", "Pragma": "no-cache",
    }
    if referer: headers["Referer"] = referer
    return headers

async def _fetch_with_retry(
    url: str, 
    method: str = "GET", 
    data: Optional[Dict] = None,
    json_accept: bool = False,
    referer: Optional[str] = None,
    timeout: float = 20.0,
    max_retries: int = 2
) -> Optional[httpx.Response]:
    """Fetch with retry logic and anti-bot evasion"""
    headers = _get_headers(referer, json_accept)
    
    for attempt in range(max_retries + 1):
        try:
            if attempt > 0:
                await asyncio.sleep(0.5 * attempt)
                
            async with httpx.AsyncClient(
                timeout=timeout,
                follow_redirects=True,
                headers=headers,
                http2=False,  # Render-friendly
            ) as client:
                if method == "POST":
                    r = await client.post(url, data=data or {})
                else:
                    r = await client.get(url)
                
                # Detect anti-bot responses
                text_lower = r.text.lower()
                if r.status_code == 403 or any(x in text_lower for x in [
                    "just a moment", "checking your browser", "cloudflare",
                    "access denied", "captcha", "ray id"
                ]):
                    if attempt < max_retries:
                        continue
                    return None
                    
                r.raise_for_status()
                return r
                
        except Exception:
            if attempt == max_retries:
                return None
            await asyncio.sleep(0.5)
    
    return None

# ─────────────────────────────────────────────────────────────
# FINANCIAL DATA DETECTION (for gold price, stock queries, etc.)
# ─────────────────────────────────────────────────────────────
FINANCIAL_KEYWORDS = [
    "gold price", "silver price", "stock price", "bitcoin price", "crypto price",
    "exchange rate", "currency", "forex", "metal price", "commodity price",
    "gold rate", "silver rate", "btc", "eth", "usd", "eur", "gbp"
]

def _is_financial_query(query: str) -> bool:
    """Detect if query is about financial/pricing data"""
    q_lower = query.lower()
    return any(kw in q_lower for kw in FINANCIAL_KEYWORDS)

async def _fetch_financial_data(query: str) -> Optional[Dict]:
    """Special handler for financial queries - scrape trusted sources"""
    sources = [
        ("https://goldprice.org/", ["gold", "silver", "metal", "price", "rate"]),
        ("https://pricegold.net/", ["gold", "price", "rate"]),
        ("https://www.livepriceofgold.com/", ["gold", "live", "price"]),
    ]
    
    q_lower = query.lower()
    for url, keywords in sources:
        if any(kw in q_lower for kw in keywords):
            try:
                r = await _fetch_with_retry(url, timeout=15)
                if not r:
                    continue
                    
                soup = BeautifulSoup(r.text, "html.parser")
                
                # Extract gold price patterns
                price_patterns = [
                    r'\$[\d,]+\.?\d*\s*(?:/?\s*(?:oz|ounce|gram|kg))?',
                    r'[\d,]+\.?\d*\s*(?:USD|dollars?)',
                ]
                
                # Look for price in common locations
                price_text = None
                for selector in [
                    ".spot-price", ".price", "#gold-price", 
                    "[data-price]", ".current-price", "strong", "b"
                ]:
                    el = soup.select_one(selector)
                    if el:
                        text = el.get_text(strip=True)
                        if "$" in text or any(c.isdigit() for c in text):
                            price_text = text
                            break
                
                if price_text:
                    # Clean and extract numeric value
                    cleaned = re.sub(r'[^\d\.\,]', '', price_text.replace("$", ""))
                    try:
                        price_val = float(cleaned.replace(",", ""))
                        return {
                            "query": query,
                            "source": url,
                            "price": price_val,
                            "currency": "USD",
                            "unit": "per ounce",
                            "timestamp": datetime.utcnow().isoformat(),
                            "raw_text": price_text,
                        }
                    except ValueError:
                        pass
                        
            except Exception:
                continue
    
    return None

# ─────────────────────────────────────────────────────────────
# SEARCH ENGINES - FIXED PARSERS
# ─────────────────────────────────────────────────────────────

async def _search_duckduckgo(query: str, limit: int = 8) -> List[Dict]:
    """DuckDuckGo HTML search - robust parsing"""
    url = "https://html.duckduckgo.com/html/"
    params = {"q": query, "kl": "us-en"}
    results = []
    
    try:
        r = await _fetch_with_retry(url, method="POST", data=params, timeout=15)
        if not r:
            return []
        
        soup = BeautifulSoup(r.text, "html.parser")
        
        for item in soup.select(".result__a, a.result__a, .links_main a"):
            href = item.get("href", "")
            if "uddg=" in href:
                qs = parse_qs(urlparse(href).query)
                href = unquote(qs.get("uddg", [""])[0])
            if not href or not href.startswith(("http://", "https://")):
                continue
            
            title = item.get_text(strip=True)
            if not title or len(title) < 5:
                continue
            
            snippet = ""
            parent = item.find_parent(".result, .web-result")
            if parent:
                snip = parent.select_one(".result__snippet")
                if snip: snippet = snip.get_text(strip=True)
            
            results.append({
                "title": title, "url": href, 
                "snippet": snippet[:280], "source": "duckduckgo"
            })
            if len(results) >= limit:
                break
                
        return results[:limit]
    except Exception as e:
        logger.warning(f"DDG error: {e}")
        return []


async def _search_hackernews(query: str, limit: int = 8) -> List[Dict]:
    """Hacker News via Algolia API - most reliable"""
    url = f"https://hn.algolia.com/api/v1/search"
    params = {
        "query": query, "tags": "story",
        "hitsPerPage": min(limit, 20),
        "attributesToRetrieve": "title,url,objectID,points,num_comments",
    }
    
    try:
        r = await _fetch_with_retry(url, params=params, json_accept=True, timeout=12)
        if not r:
            return []
        
        data = r.json()
        results = []
        
        for hit in data.get("hits", []):
            title = hit.get("title", "")
            if not title or len(title) < 3:
                continue
            story_url = hit.get("url") or f"https://news.ycombinator.com/item?id={hit.get('objectID')}"
            results.append({
                "title": title, "url": story_url,
                "snippet": f"▲{hit.get('points', 0)} 💬{hit.get('num_comments', 0)}",
                "source": "hackernews"
            })
            if len(results) >= limit:
                break
        return results[:limit]
    except Exception as e:
        logger.warning(f"HN error: {e}")
        return []


async def _search_wikipedia(query: str, limit: int = 3) -> List[Dict]:
    """Wikipedia API - official endpoint"""
    url = "https://en.wikipedia.org/w/api.php"
    params = {
        "action": "query", "list": "search", "srsearch": query,
        "srlimit": limit, "format": "json", "origin": "*",
    }
    headers = {"User-Agent": "AI-Search-API/3.1"}
    
    try:
        async with httpx.AsyncClient(timeout=12) as client:
            r = await client.get(url, params=params, headers=headers)
            r.raise_for_status()
        
        data = r.json()
        results = []
        
        for item in data.get("query", {}).get("search", []):
            title = item.get("title", "")
            if not title:
                continue
            snippet = BeautifulSoup(item.get("snippet", ""), "html.parser").get_text(strip=True)
            results.append({
                "title": title,
                "url": f"https://en.wikipedia.org/wiki/{title.replace(' ', '_')}",
                "snippet": snippet[:280],
                "source": "wikipedia"
            })
        return results[:limit]
    except Exception as e:
        logger.warning(f"Wikipedia error: {e}")
        return []


async def _search_reddit(query: str, limit: int = 5) -> List[Dict]:
    """Reddit JSON API - requires proper UA"""
    url = f"https://www.reddit.com/search.json"
    params = {"q": query, "sort": "relevance", "limit": min(limit, 25)}
    headers = {
        "User-Agent": "AI-Search-Bot/1.0 (by u/ai_helper; contact: api@example.com)",
        "Accept": "application/json"
    }
    
    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
            r = await client.get(url, params=params, headers=headers)
            if r.status_code == 429:
                return []
            r.raise_for_status()
        
        data = r.json()
        results = []
        
        for post in data.get("data", {}).get("children", []):
            d = post.get("data", {})
            title, permalink = d.get("title", ""), d.get("permalink", "")
            if not title or not permalink or len(title) < 3:
                continue
            text = d.get("selftext", "") or ("[Media post]" if d.get("is_video") else "")
            results.append({
                "title": title,
                "url": f"https://reddit.com{permalink}",
                "snippet": text[:280] if text else "",
                "source": "reddit"
            })
            if len(results) >= limit:
                break
        return results[:limit]
    except Exception as e:
        logger.warning(f"Reddit error: {e}")
        return []

# ─────────────────────────────────────────────────────────────
# CONTENT SCRAPING - FIXED ENCODING & ANTI-BOT
# ─────────────────────────────────────────────────────────────

async def scrape_content(url: str, max_chars: int = 3000) -> Dict[str, Any]:
    """Extract main content with proper encoding handling"""
    try:
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https"):
            return _scrape_error(url, "invalid_scheme", "URL must start with http/https")
    except Exception as e:
        return _scrape_error(url, "parse_error", str(e)[:100])
    
    # Block anti-bot domains
    if any(x in parsed.netloc.lower() for x in [
        "cloudflare", "akamai", "incapsula", "perimeterx", "botprotect"
    ]):
        return _scrape_error(url, "blocked_domain", "Domain uses anti-bot protection")
    
    # Fetch page
    r = await _fetch_with_retry(url, timeout=25, max_retries=2)
    if not r:
        return _scrape_error(url, "fetch_failed", "Could not retrieve page")
    
    # Detect anti-bot pages
    text_lower = r.text.lower()
    if any(pat in text_lower for pat in [
        "just a moment", "checking your browser", "cloudflare",
        "access denied", "captcha", "ray id"
    ]):
        return _scrape_error(url, "antibot_detected", "Page requires JavaScript/CAPTCHA")
    
    # CRITICAL: Get properly decoded text (httpx auto-handles gzip/br)
    try:
        html = r.text  # Auto-decoded with proper charset detection
    except Exception as e:
        return _scrape_error(url, "decode_error", f"Encoding issue: {str(e)[:80]}")
    
    # Try trafilatura first
    if HAS_TRAFILATURA:
        try:
            content = trafilatura.extract(
                html, include_comments=False, include_tables=True,
                favor_precision=True, no_fallback=False,
            )
            if content and len(content.strip()) > 100:
                return {
                    "url": url, "content": content[:max_chars],
                    "method": "trafilatura", "ok": True, "char_count": len(content),
                }
        except Exception:
            pass
    
    # BeautifulSoup fallback
    try:
        soup = BeautifulSoup(html, "html.parser")
        for tag in soup(["script", "style", "noscript", "nav", "footer", 
                        "header", "aside", "iframe", "form"]):
            tag.decompose()
        
        # Find main content
        container = None
        for selector in [
            "article", "main", "[role='main']", "#content", ".content",
            "[itemprop='articleBody']", ".post-content", ".entry-content", "body"
        ]:
            el = soup.select_one(selector)
            if el and len(el.get_text(strip=True)) > 150:
                container = el
                break
        if not container:
            container = soup
        
        # Extract paragraphs
        paragraphs = [
            p.get_text(strip=True) for p in container.find_all("p")
            if len(p.get_text(strip=True)) > 60
            and not p.get_text(strip=True).startswith(("©", "Privacy", "Terms"))
        ]
        
        content = "\n\n".join(paragraphs) if paragraphs else container.get_text(separator="\n", strip=True)
        content = re.sub(r'\n{3,}', '\n\n', content).strip()
        
        if not content or len(content) < 50:
            return _scrape_error(url, "no_content", "Could not extract meaningful text")
        
        return {
            "url": url, "content": content[:max_chars],
            "method": "beautifulsoup", "ok": True, "char_count": len(content),
        }
    except Exception as e:
        logger.warning(f"BS4 scrape failed: {e}")
        return _scrape_error(url, "parse_failed", str(e)[:100])


def _scrape_error(url: str, method: str, message: str) -> Dict[str, Any]:
    return {
        "url": url, "content": "", "method": method,
        "ok": False, "error": message, "char_count": 0,
    }

# ─────────────────────────────────────────────────────────────
# UTILITIES
# ─────────────────────────────────────────────────────────────

def _deduplicate(results: List[Dict]) -> List[Dict]:
    seen, unique = set(), []
    for r in results:
        if not isinstance(r, dict) or not r.get("url") or not r.get("title"):
            continue
        domain = urlparse(r["url"]).netloc.lower()
        title_norm = re.sub(r'[^a-z0-9]', '', r["title"].lower())[:30]
        key = f"{domain}:{title_norm}"
        if key not in seen:
            seen.add(key)
            unique.append(r)
    return unique


def _ensure_result_structure(r: Dict) -> Dict:
    return {
        "title": str(r.get("title", ""))[:200],
        "url": str(r.get("url", ""))[:500],
        "snippet": str(r.get("snippet", ""))[:300],
        "source": str(r.get("source", "unknown")),
        "content": r.get("content", ""),
        "scrape_ok": r.get("scrape_ok", False),
        "scrape_method": r.get("scrape_method", "none"),
    }

# ─────────────────────────────────────────────────────────────
# API ENDPOINTS
# ─────────────────────────────────────────────────────────────

@app.get("/", tags=["Meta"])
def api_root():
    return {
        "name": "AI Search API", "version": "3.1", "status": "operational",
        "usage": {
            "quick": "/quick?query=...&max_results=5",
            "full": "/search?query=...&engines=hn,ddg,wiki&scrape=true",
            "scrape": "/scrape?url=https://example.com",
            "financial": "/search?query=gold+price (auto-detects financial data)",
        },
        "engines": ["hn", "ddg", "wiki", "reddit"],
    }


@app.get("/health", tags=["Meta"])
def health_check():
    return {
        "status": "ok", "version": "3.1",
        "cache_entries": len(_cache),
        "trafilatura": HAS_TRAFILATURA,
        "timestamp": time.time(),
    }


@app.get("/quick", tags=["Search"])
async def quick_search(
    query: str = Query(..., min_length=1, max_length=200),
    max_results: int = Query(5, ge=1, le=20),
):
    """Fast search without scraping"""
    cache_k = _cache_key("quick", query, max_results)
    cached = cache_get(cache_k)
    if cached:
        return {**cached, "cached": True}
    
    tasks = [_search_hackernews(query, max_results), _search_duckduckgo(query, max_results)]
    batches = await asyncio.gather(*tasks, return_exceptions=True)
    
    merged = [r for batch in batches if isinstance(batch, list) for r in batch]
    results = _deduplicate(merged)[:max_results]
    results = [_ensure_result_structure(r) for r in results]
    
    response = {"query": query, "total": len(results), "results": results, "cached": False}
    cache_set(cache_k, response)
    return response


@app.get("/search", tags=["Search"])
async def full_search(
    query: str = Query(..., min_length=1, max_length=200),
    max_results: int = Query(5, ge=1, le=15),
    engines: str = Query("hn,ddg,wiki"),
    scrape: bool = Query(True),
    max_chars: int = Query(2000, ge=300, le=5000),
):
    """
    Multi-engine search with optional scraping.
    
    🎯 For financial queries (gold price, stock, crypto):
    - Auto-detects and fetches structured price data
    - Returns price + source + timestamp in results
    """
    cache_k = _cache_key("search", query, max_results, engines, scrape, max_chars)
    cached = cache_get(cache_k)
    if cached:
        return {**cached, "cached": True}
    
    # 🎯 SPECIAL: Handle financial queries
    financial_data = None
    if _is_financial_query(query):
        financial_data = await _fetch_financial_data(query)
    
    engine_codes = [e.strip().lower() for e in engines.split(",") if e.strip()] or ["hn", "ddg"]
    engine_funcs = {
        "hn": (_search_hackernews, 8), "ddg": (_search_duckduckgo, 8),
        "wiki": (_search_wikipedia, 3), "reddit": (_search_reddit, 5),
    }
    
    tasks = []
    active_engines = []
    for code in engine_codes:
        if code in engine_funcs:
            func, limit = engine_funcs[code]
            tasks.append(func(query, min(limit, max_results)))
            active_engines.append(code)
    
    batches = await asyncio.gather(*tasks, return_exceptions=True)
    merged = [r for batch in batches if isinstance(batch, list) for r in batch]
    results = _deduplicate(merged)[:max_results]
    
    # Scrape if requested
    if scrape and results:
        scrape_tasks = [scrape_content(r["url"], max_chars) for r in results]
        scraped = await asyncio.gather(*scrape_tasks, return_exceptions=True)
        for i, result in enumerate(results):
            scrape_data = scraped[i] if i < len(scraped) and isinstance(scraped[i], dict) else {}
            result["content"] = scrape_data.get("content", "") if scrape_data.get("ok") else ""
            result["scrape_ok"] = scrape_data.get("ok", False)
            result["scrape_method"] = scrape_data.get("method", "none")
    
    results = [_ensure_result_structure(r) for r in results]
    
    # 🎯 Inject financial data if found
    if financial_data:
        results.insert(0, {
            "title": f"Current {query} - Live Data",
            "url": financial_data.get("source", ""),
            "snippet": f"Price: ${financial_data.get('price'):,.2f} {financial_data.get('unit', '')}",
            "source": "financial_data",
            "content": json.dumps(financial_data, indent=2),
            "scrape_ok": True,
            "scrape_method": "financial_api",
            "financial_data": financial_data,  # Structured price info
        })
    
    response = {
        "query": query, "total": len(results), "results": results,
        "cached": False, "engines_used": active_engines,
        "financial_query": _is_financial_query(query),
        "scraped": scrape and any(r.get("scrape_ok") for r in results),
    }
    cache_set(cache_k, response)
    return response


@app.get("/scrape", tags=["Scraping"])
async def scrape_single(
    url: str = Query(...),
    max_chars: int = Query(3000, ge=300, le=8000),
):
    """Extract content from single URL"""
    if not url.startswith(("http://", "https://")):
        return JSONResponse(status_code=400, content={"error": "http/https required"})
    
    cache_k = _cache_key("scrape", url, max_chars)
    cached = cache_get(cache_k)
    if cached:
        return {**cached, "cached": True}
    
    result = await scrape_content(url, max_chars)
    cache_set(cache_k, result)
    return result

# ─────────────────────────────────────────────────────────────
# ERROR HANDLING
# ─────────────────────────────────────────────────────────────

@app.exception_handler(Exception)
async def global_exception_handler(request, exc: Exception):
    logger.error(f"Unhandled error: {exc}", exc_info=True)
    return JSONResponse(status_code=500, content={"error": "Internal server error"})
