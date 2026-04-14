"""
AI Search API v4.0 - Ultra Minimal & Robust
✅ Scraping with proper encoding
✅ Working search engines
✅ Financial data support
"""
import asyncio
import hashlib
import time
import re
import json
import logging
from urllib.parse import quote_plus, urlparse, parse_qs, unquote
from typing import Optional, List, Dict, Any
from datetime import datetime

import httpx
from bs4 import BeautifulSoup
from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger(__name__)

try:
    import trafilatura
    HAS_TRAFILATURA = True
except ImportError:
    HAS_TRAFILATURA = False
    logger.warning("trafilatura not installed")

app = FastAPI(title="AI Search API", version="4.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

# ─── Cache ─────────────────────────────────────────────────────────
_cache: Dict = {}
CACHE_TTL = 300

def _cache_key(*args) -> str:
    return hashlib.md5("|".join(str(a) for a in args).encode()).hexdigest()

def _cache_get(key: str):
    entry = _cache.get(key)
    if entry and (time.time() - entry["ts"]) < CACHE_TTL:
        return entry["data"]
    return None

def _cache_set(key: str, data):
    _cache[key] = {"data": data, "ts": time.time()}

# ─── HTTP Client - ROBUST ENCODING ────────────────────────────────
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36"

async def fetch_html(url: str, post_data: dict = None, timeout: float = 15.0) -> Optional[str]:
    """Fetch URL and return properly decoded HTML string"""
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate",  # No 'br' for better compatibility
        "DNT": "1",
    }
    
    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True, http2=False) as client:
            if post_data:
                response = await client.post(url, data=post_data, headers=headers)
            else:
                response = await client.get(url, headers=headers)
            
            if response.status_code != 200:
                return None
            
            # 🔥 CRITICAL FIX: Force proper UTF-8 decoding
            # First try to get encoding from response headers
            content_type = response.headers.get("content-type", "")
            if "charset=" in content_type:
                encoding = content_type.split("charset=")[-1].strip().lower()
            else:
                encoding = "utf-8"
            
            try:
                return response.content.decode(encoding, errors="replace")
            except:
                return response.text  # Fallback
                
    except Exception as e:
        logger.warning(f"Fetch error for {url[:60]}: {type(e).__name__}")
        return None

# ─── Search Engines ───────────────────────────────────────────────

async def search_duckduckgo(query: str, limit: int = 8) -> List[Dict]:
    """DuckDuckGo HTML search"""
    html = await fetch_html("https://html.duckduckgo.com/html/", {"q": query, "kl": "us-en"})
    if not html:
        return []
    
    soup = BeautifulSoup(html, "html.parser")
    results = []
    
    for link in soup.select("a.result__a, .result__a, .links_main a"):
        href = link.get("href", "")
        if "uddg=" in href:
            qs = parse_qs(urlparse(href).query)
            href = unquote(qs.get("uddg", [""])[0])
        
        if not href.startswith(("http://", "https://")):
            continue
        
        title = link.get_text(strip=True)
        if len(title) < 5:
            continue
        
        snippet = ""
        parent = link.find_parent("div", class_=re.compile("result"))
        if parent:
            snip_el = parent.select_one(".result__snippet")
            if snip_el:
                snippet = snip_el.get_text(strip=True)
        
        results.append({
            "title": title,
            "url": href,
            "snippet": snippet[:300],
            "source": "duckduckgo"
        })
        
        if len(results) >= limit:
            break
    
    return results

async def search_hackernews(query: str, limit: int = 8) -> List[Dict]:
    """Hacker News via Algolia API"""
    url = f"https://hn.algolia.com/api/v1/search"
    params = {"query": query, "tags": "story", "hitsPerPage": limit}
    
    try:
        async with httpx.AsyncClient(timeout=12) as client:
            response = await client.get(url, params=params)
            if response.status_code != 200:
                return []
            
            data = response.json()
            results = []
            
            for hit in data.get("hits", []):
                title = hit.get("title", "")
                if not title:
                    continue
                
                url = hit.get("url") or f"https://news.ycombinator.com/item?id={hit.get('objectID')}"
                
                results.append({
                    "title": title,
                    "url": url,
                    "snippet": f"▲{hit.get('points', 0)} 💬{hit.get('num_comments', 0)}",
                    "source": "hackernews"
                })
            
            return results[:limit]
            
    except Exception as e:
        logger.warning(f"HN error: {e}")
        return []

async def search_wikipedia(query: str, limit: int = 3) -> List[Dict]:
    """Wikipedia API search"""
    url = "https://en.wikipedia.org/w/api.php"
    params = {
        "action": "query",
        "list": "search",
        "srsearch": query,
        "srlimit": limit,
        "format": "json",
        "origin": "*"
    }
    headers = {"User-Agent": "AI-Search-API/4.0"}
    
    try:
        async with httpx.AsyncClient(timeout=12) as client:
            response = await client.get(url, params=params, headers=headers)
            if response.status_code != 200:
                return []
            
            data = response.json()
            results = []
            
            for item in data.get("query", {}).get("search", []):
                title = item.get("title", "")
                if not title:
                    continue
                
                snippet = BeautifulSoup(item.get("snippet", ""), "html.parser").get_text(strip=True)
                
                results.append({
                    "title": title,
                    "url": f"https://en.wikipedia.org/wiki/{title.replace(' ', '_')}",
                    "snippet": snippet[:300],
                    "source": "wikipedia"
                })
            
            return results
            
    except Exception as e:
        logger.warning(f"Wikipedia error: {e}")
        return []

# ─── Content Scraping - FIXED ─────────────────────────────────────

async def scrape_content(url: str, max_chars: int = 3000) -> Dict[str, Any]:
    """Extract main content with proper encoding"""
    
    # Validate URL
    if not url.startswith(("http://", "https://")):
        return {"url": url, "content": "", "method": "invalid", "ok": False}
    
    # Skip known anti-bot domains
    parsed = urlparse(url)
    blocked = ["cloudflare", "akamai", "incapsula", "perimeterx", "captcha"]
    if any(b in parsed.netloc.lower() for b in blocked):
        return {"url": url, "content": "", "method": "blocked", "ok": False}
    
    # Fetch HTML
    html = await fetch_html(url, timeout=20.0)
    if not html:
        return {"url": url, "content": "", "method": "fetch_failed", "ok": False}
    
    # Check for anti-bot pages
    html_lower = html.lower()
    if any(x in html_lower for x in ["just a moment", "checking your browser", "access denied", "captcha"]):
        return {"url": url, "content": "", "method": "antibot", "ok": False}
    
    # Try trafilatura first
    if HAS_TRAFILATURA:
        try:
            content = trafilatura.extract(html, include_comments=False, favor_precision=True)
            if content and len(content.strip()) > 100:
                return {
                    "url": url,
                    "content": content[:max_chars],
                    "method": "trafilatura",
                    "ok": True,
                    "char_count": len(content)
                }
        except:
            pass
    
    # BeautifulSoup fallback
    try:
        soup = BeautifulSoup(html, "html.parser")
        
        # Remove non-content elements
        for tag in soup(["script", "style", "nav", "footer", "header", "aside", "iframe", "form"]):
            tag.decompose()
        
        # Find main content
        container = None
        for selector in ["article", "main", '[role="main"]', "#content", ".content", ".post-content", "body"]:
            el = soup.select_one(selector)
            if el and len(el.get_text(strip=True)) > 150:
                container = el
                break
        
        if not container:
            container = soup
        
        # Extract paragraphs
        paragraphs = []
        for p in container.find_all("p"):
            text = p.get_text(strip=True)
            if len(text) > 60 and not text.startswith(("©", "Privacy", "Terms", "Cookie")):
                paragraphs.append(text)
        
        if paragraphs:
            content = "\n\n".join(paragraphs)
        else:
            content = container.get_text(separator="\n", strip=True)
        
        # Clean up
        content = re.sub(r'\n{3,}', '\n\n', content).strip()
        content = re.sub(r'[ \t]+', ' ', content)
        
        if len(content) < 50:
            return {"url": url, "content": "", "method": "empty", "ok": False}
        
        return {
            "url": url,
            "content": content[:max_chars],
            "method": "beautifulsoup",
            "ok": True,
            "char_count": len(content)
        }
        
    except Exception as e:
        return {"url": url, "content": "", "method": "error", "ok": False, "error": str(e)[:100]}

# ─── Financial Data ──────────────────────────────────────────────

FINANCIAL_KEYWORDS = ["gold", "silver", "stock", "bitcoin", "crypto", "price", "rate", "usd", "eur"]

def is_financial_query(query: str) -> bool:
    q = query.lower()
    return any(kw in q for kw in FINANCIAL_KEYWORDS)

async def fetch_gold_price() -> Optional[Dict]:
    """Fetch current gold price from goldprice.org"""
    html = await fetch_html("https://goldprice.org/", timeout=15.0)
    if not html:
        return None
    
    try:
        soup = BeautifulSoup(html, "html.parser")
        
        # Look for price in common locations
        for selector in [".spot-price", ".price", "#gold-price", '[data-price]', "strong", "b"]:
            el = soup.select_one(selector)
            if el:
                text = el.get_text(strip=True)
                if "$" in text:
                    # Extract number
                    import re
                    numbers = re.findall(r'[\d,]+\.?\d*', text.replace(",", ""))
                    if numbers:
                        price = float(numbers[0].replace(",", ""))
                        return {
                            "query": "gold price",
                            "source": "https://goldprice.org/",
                            "price": price,
                            "currency": "USD",
                            "unit": "per ounce",
                            "timestamp": datetime.utcnow().isoformat(),
                            "raw_text": text
                        }
    except:
        pass
    
    return None

# ─── Utilities ──────────────────────────────────────────────────

def deduplicate_results(results: List[Dict]) -> List[Dict]:
    seen = set()
    unique = []
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

def safe_result(r: Dict) -> Dict:
    return {
        "title": str(r.get("title", ""))[:200],
        "url": str(r.get("url", ""))[:500],
        "snippet": str(r.get("snippet", ""))[:300],
        "source": str(r.get("source", "unknown")),
        "content": r.get("content", ""),
        "scrape_ok": r.get("scrape_ok", False),
        "scrape_method": r.get("scrape_method", "none")
    }

# ─── API Endpoints ──────────────────────────────────────────────

@app.get("/")
def root():
    return {
        "name": "AI Search API",
        "version": "4.0",
        "status": "operational",
        "endpoints": {
            "/search": "Full search with optional scraping",
            "/quick": "Fast metadata-only search",
            "/scrape": "Scrape single URL",
            "/health": "Health check"
        }
    }

@app.get("/health")
def health():
    return {
        "status": "ok",
        "version": "4.0",
        "cache_entries": len(_cache),
        "trafilatura": HAS_TRAFILATURA,
        "timestamp": time.time()
    }

@app.get("/quick")
async def quick_search(
    query: str = Query(..., min_length=1, max_length=200),
    max_results: int = Query(5, ge=1, le=20)
):
    """Fast search without content scraping"""
    
    # Clamp max_results
    max_results = min(max(1, max_results), 20)
    
    cache_key = _cache_key("quick", query, max_results)
    cached = _cache_get(cache_key)
    if cached:
        return {**cached, "cached": True}
    
    # Run searches in parallel
    tasks = [
        search_hackernews(query, max_results),
        search_duckduckgo(query, max_results),
        search_wikipedia(query, min(3, max_results))
    ]
    
    batches = await asyncio.gather(*tasks, return_exceptions=True)
    
    # Merge results
    merged = []
    for batch in batches:
        if isinstance(batch, list):
            merged.extend(batch)
    
    results = deduplicate_results(merged)[:max_results]
    results = [safe_result(r) for r in results]
    
    response = {
        "query": query,
        "total": len(results),
        "results": results,
        "cached": False
    }
    
    _cache_set(cache_key, response)
    return response

@app.get("/search")
async def full_search(
    query: str = Query(..., min_length=1, max_length=200),
    max_results: int = Query(5, ge=1, le=15),
    engines: str = Query("hn,ddg,wiki"),
    scrape: bool = Query(True),
    max_chars: int = Query(2000, ge=300, le=5000)
):
    """Full search with optional content scraping"""
    
    # Clamp max_results
    max_results = min(max(1, max_results), 15)
    
    cache_key = _cache_key("search", query, max_results, engines, scrape, max_chars)
    cached = _cache_get(cache_key)
    if cached:
        return {**cached, "cached": True}
    
    # Parse engines
    engine_list = [e.strip().lower() for e in engines.split(",") if e.strip()]
    if not engine_list:
        engine_list = ["hn", "ddg", "wiki"]
    
    engine_map = {
        "hn": search_hackernews,
        "ddg": search_duckduckgo,
        "wiki": search_wikipedia
    }
    
    # Build tasks
    tasks = []
    active_engines = []
    for eng in engine_list:
        if eng in engine_map:
            limit = min(8 if eng != "wiki" else 3, max_results)
            tasks.append(engine_map[eng](query, limit))
            active_engines.append(eng)
    
    # Execute searches
    batches = await asyncio.gather(*tasks, return_exceptions=True)
    
    # Merge results
    merged = []
    for batch in batches:
        if isinstance(batch, list):
            merged.extend(batch)
    
    results = deduplicate_results(merged)[:max_results]
    
    # Check for financial data
    financial_data = None
    if is_financial_query(query) and "gold" in query.lower():
        financial_data = await fetch_gold_price()
    
    # Scrape content if requested
    if scrape and results:
        scrape_tasks = [scrape_content(r["url"], max_chars) for r in results]
        scraped = await asyncio.gather(*scrape_tasks, return_exceptions=True)
        
        for i, r in enumerate(results):
            s = scraped[i] if i < len(scraped) and isinstance(scraped[i], dict) else {}
            r["content"] = s.get("content", "") if s.get("ok") else ""
            r["scrape_ok"] = s.get("ok", False)
            r["scrape_method"] = s.get("method", "none")
    
    results = [safe_result(r) for r in results]
    
    # Add financial data if found
    if financial_data:
        results.insert(0, {
            "title": f"Current Gold Price - Live",
            "url": financial_data.get("source", ""),
            "snippet": f"${financial_data.get('price', 0):,.2f} {financial_data.get('unit', '')}",
            "source": "financial_data",
            "content": json.dumps(financial_data, indent=2),
            "scrape_ok": True,
            "scrape_method": "financial_api",
            "financial_data": financial_data
        })
    
    response = {
        "query": query,
        "total": len(results),
        "results": results,
        "cached": False,
        "engines_used": active_engines,
        "scraped": scrape and any(r.get("scrape_ok") for r in results)
    }
    
    _cache_set(cache_key, response)
    return response

@app.get("/scrape")
async def scrape_single(
    url: str = Query(..., description="URL to scrape"),
    max_chars: int = Query(3000, ge=300, le=8000)
):
    """Scrape a single URL"""
    
    if not url.startswith(("http://", "https://")):
        return JSONResponse(status_code=400, content={"error": "URL must start with http:// or https://"})
    
    cache_key = _cache_key("scrape", url, max_chars)
    cached = _cache_get(cache_key)
    if cached:
        return {**cached, "cached": True}
    
    result = await scrape_content(url, max_chars)
    _cache_set(cache_key, result)
    return result

# Error handler
@app.exception_handler(Exception)
async def error_handler(request, exc):
    logger.error(f"Unhandled error: {exc}", exc_info=True)
    return JSONResponse(status_code=500, content={"error": "Internal server error"})
