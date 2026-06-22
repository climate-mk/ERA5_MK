"""
climate_news.py — Macedonian climate-news aggregation.

Pulls recent climate-related headlines from curated MK outlet RSS feeds,
filters by keyword, and accumulates matches into a persistent on-disk
archive (cache/<country>/climate_news_archive.json).

Kept standalone (no dependency on mk_api.py's CSV loading) so it can be
called cheaply from a cron job to refresh the cache out-of-band, without
visitors ever triggering the live poll.
"""

import os
import re
import json
import time
import glob
from email.utils import format_datetime
from xml.sax.saxutils import escape
from urllib.parse import urlparse, parse_qs
from concurrent.futures import ThreadPoolExecutor, as_completed

import yaml
import pandas as pd
import requests as http_requests
import feedparser
from dotenv import load_dotenv

from config import CONFIG

load_dotenv()

_CACHE_DIR = os.path.join(os.path.dirname(__file__), "cache", CONFIG["code"])

# All tunables (keywords, sources, retention, poll frequency) live in
# countries/climate_news.yaml — edit that file, not this one, to change them.
_NEWS_CONFIG_PATH = os.path.join(os.path.dirname(__file__), "countries", "climate_news.yaml")
with open(_NEWS_CONFIG_PATH, encoding="utf-8") as _f:
    _NEWS_CONFIG = yaml.safe_load(_f)

_CLIMATE_NEWS_KEYWORDS = _NEWS_CONFIG["keywords_mk"]
_CLIMATE_NEWS_MAX_AGE_DAYS = _NEWS_CONFIG["max_age_days"]
_CLIMATE_NEWS_POLL_INTERVAL_SECONDS = _NEWS_CONFIG["poll_interval_hours"] * 3600

_CLIMATE_NEWS_SOURCES = _NEWS_CONFIG["sources_mk"]

# English-language sources. Balkan Insight's BTJ Macedonia category feed is
# already scoped to North Macedonia specifically, so it needs no extra country
# filter. SkopjeDiem is English-language general MK news, also no filter needed
# since every item is already about North Macedonia.
_CLIMATE_NEWS_EN_SOURCES = _NEWS_CONFIG["sources_en"]
_CLIMATE_NEWS_EN_KEYWORDS = _NEWS_CONFIG["keywords_en"]

# Our own Bluesky account — every post is included (no keyword filter), since
# it's our own published content rather than a third-party source being mined.
_BLUESKY_HANDLE = _NEWS_CONFIG["bluesky_handle"]
_BLUESKY_API_URL = (
    "https://public.api.bsky.app/xrpc/app.bsky.feed.getAuthorFeed"
    f"?actor={_BLUESKY_HANDLE}&limit=100"
)

# SerpApi (Google News search results) — an extra source on top of the direct
# RSS feeds, queried once/day (not the 6h interval) to stay within the free
# tier. Requires SERPAPI_KEY in .env; silently contributes nothing if unset
# (same fail-open behavior as every other source).
_SERPAPI_KEY = os.getenv("SERPAPI_KEY", "")
_SERPAPI_QUERIES = _NEWS_CONFIG.get("serpapi_queries", [])
_SERPAPI_POLL_INTERVAL_SECONDS = _NEWS_CONFIG.get("serpapi_poll_interval_hours", 24) * 3600
_SERPAPI_ARCHIVE_PATH = os.path.join(_CACHE_DIR, "climate_news_serpapi_state.json")

# Bing News search RSS export — free, no API key, no quota — so it rides the
# same free 6h poll cycle as the direct outlet RSS feeds rather than the
# once-daily SerpApi cadence.
_BING_QUERIES = _NEWS_CONFIG.get("bing_queries", [])

# Each outlet's RSS feed only exposes its latest ~10-20 items (a few hours of
# content) — not a 30-day archive. A climate story only survives in that window
# briefly before newer unrelated posts push it out. So matched items are
# accumulated into a single persistent archive file across polls, rather than
# recomputed from a fresh feed snapshot each time.
_CLIMATE_NEWS_ARCHIVE_PATH = os.path.join(_CACHE_DIR, "climate_news_archive.json")

_CLIMATE_NEWS_IMG_RE = re.compile(r'<img[^>]+src=["\']([^"\']+)["\']', re.IGNORECASE)


def _fs_load(path):
    """Load a JSON cache file; return None on any error."""
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _fs_save(path, data):
    """Write data as JSON."""
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
    except Exception:
        pass  # disk failure is non-fatal


def _extract_image(summary_html: str) -> str:
    """WordPress RSS feeds embed the featured image as an <img> inside the
    summary HTML rather than a dedicated media field — pull its src out."""
    m = _CLIMATE_NEWS_IMG_RE.search(summary_html or "")
    return m.group(1) if m else ""


_FEED_REQUEST_HEADERS = {
    # Some outlets (e.g. fakulteti.mk) block requests with no User-Agent.
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
}

def _fetch_one_climate_source(source_name, url, cutoff, keywords, require_keywords=None):
    """Fetch and filter a single outlet's feed; returns a dict of matched items.
    require_keywords (if given) must ALSO appear — used to scope a regional
    multi-country feed (e.g. Balkan Insight) down to North Macedonia only."""
    try:
        resp = http_requests.get(url, timeout=10, headers=_FEED_REQUEST_HEADERS)
        resp.raise_for_status()
        feed = feedparser.parse(resp.content)
    except Exception:
        return {}
    matched = {}
    for entry in feed.entries:
        link = entry.get("link")
        if not link:
            continue
        title   = entry.get("title", "")
        summary = entry.get("summary", "")
        haystack = f"{title} {summary}".lower()
        if not any(kw in haystack for kw in keywords):
            continue
        if require_keywords and not any(kw in haystack for kw in require_keywords):
            continue
        # Most feeds expose published_parsed; some (e.g. DW's RDF feed) only
        # set updated_parsed — fall back to that so those items aren't dropped.
        published = entry.get("published_parsed") or entry.get("updated_parsed")
        if not published:
            continue
        published_ts = pd.Timestamp(time.mktime(published), unit="s", tz="UTC")
        if published_ts < cutoff:
            continue
        matched[link] = {
            "title":     title,
            "link":      link,
            "source":    source_name,
            "published": published_ts.isoformat(),
            "image":     _extract_image(summary),
            "origin":    "feed",
        }
    return matched


def _fetch_bluesky_posts():
    """Fetch our own recent Bluesky posts via the public, unauthenticated
    AppView API. Returns matched items keyed by post permalink, same shape
    as news items, so they merge into the same archive seamlessly."""
    try:
        resp = http_requests.get(_BLUESKY_API_URL, timeout=10)
        resp.raise_for_status()
        data = resp.json()
    except Exception:
        return {}

    cutoff = pd.Timestamp.now(tz="UTC") - pd.Timedelta(days=_CLIMATE_NEWS_MAX_AGE_DAYS)
    matched = {}
    for entry in data.get("feed", []):
        post = entry.get("post", {})
        record = post.get("record", {})
        uri = post.get("uri", "")
        created_at = record.get("createdAt")
        if not uri or not created_at:
            continue
        try:
            published_ts = pd.Timestamp(created_at)
            if published_ts.tzinfo is None:
                published_ts = published_ts.tz_localize("UTC")
        except Exception:
            continue
        if published_ts < cutoff:
            continue

        rkey = uri.rsplit("/", 1)[-1]
        link = f"https://bsky.app/profile/{_BLUESKY_HANDLE}/post/{rkey}"

        image = ""
        embed_images = post.get("embed", {}).get("images", [])
        if embed_images:
            image = embed_images[0].get("fullsize", "")

        matched[link] = {
            "title":     record.get("text", ""),
            "link":      link,
            "source":    "Bluesky",
            "published": published_ts.isoformat(),
            "image":     image,
            "origin":    "feed",
        }
    return matched


def _fetch_serpapi_news_one(query, cutoff):
    """Run one SerpApi Google News search and return matched items keyed by
    link. Must use engine=google + tbm=nws with NO gl/hl params — adding
    locale params or using the dedicated google_news engine makes Google
    ignore locale entirely and return generic/English results instead of
    Macedonian ones (confirmed by testing).

    tbs=qdr:y (past year) is used to exclude old/stale articles that would
    otherwise dominate unfiltered results — without it, ~9 of 10 results are
    years old. Stricter filters (qdr:m, qdr:w) were also tested but return
    far fewer results via SerpApi's infrastructure than the same filter does
    in a real browser (likely an IP/geo-ranking difference on Google's side
    outside our control) — qdr:y is the best tested yield of genuinely
    Macedonian, climate-relevant results once combined with the 30-day
    client-side cutoff below."""
    try:
        resp = http_requests.get("https://serpapi.com/search", params={
            "engine": "google",
            "q": query,
            "tbm": "nws",
            "tbs": "qdr:y",
            "api_key": _SERPAPI_KEY,
        }, timeout=15)
        resp.raise_for_status()
        data = resp.json()
    except Exception:
        return {}

    matched = {}
    for entry in data.get("news_results", []):
        link = entry.get("link")
        published_at = entry.get("published_at")
        if not link or not published_at:
            continue
        try:
            published_ts = pd.Timestamp(published_at)
            if published_ts.tzinfo is None:
                published_ts = published_ts.tz_localize("UTC")
        except Exception:
            continue
        if published_ts < cutoff:
            continue
        source = entry.get("source", "")
        matched[link] = {
            "title":     entry.get("title", ""),
            "link":      link,
            "source":    source if isinstance(source, str) else source.get("name", ""),
            "published": published_ts.isoformat(),
            "image":     entry.get("thumbnail", ""),
            "origin":    "google",
        }
    return matched


def _fetch_serpapi_news():
    """Run all configured SerpApi queries in parallel, merge results."""
    if not _SERPAPI_KEY or not _SERPAPI_QUERIES:
        return {}
    cutoff = pd.Timestamp.now(tz="UTC") - pd.Timedelta(days=_CLIMATE_NEWS_MAX_AGE_DAYS)
    items_by_link = {}
    with ThreadPoolExecutor(max_workers=len(_SERPAPI_QUERIES)) as pool:
        futures = [pool.submit(_fetch_serpapi_news_one, q, cutoff) for q in _SERPAPI_QUERIES]
        for fut in as_completed(futures):
            items_by_link.update(fut.result())
    return items_by_link


def _fetch_bing_news_one(query, cutoff):
    """Run one Bing News search and return matched items keyed by link.
    Bing's free RSS export (no API key, no quota) returns real, dated,
    Macedonian-language results for these queries (confirmed by testing) —
    used as a free supplement to SerpApi rather than a replacement, since
    SerpApi's infrastructure can't replicate the "past month" result volume
    a real Macedonian browser session gets from Google (see climate_news.py
    SerpApi comments for the full investigation).

    Bing wraps each result link in a click-tracking redirect
    (bing.com/news/apiclick.aspx?...&url=<real article URL>) — unwrap it so
    the archive's link-keyed dedup and the final card link both point at the
    real article, not Bing's redirect page.

    qft=interval="9" is Bing's own "past month" freshness filter (confirmed
    against the user's real browser URL) — applying it server-side gives a
    tighter, more relevant result set than fetching unfiltered and relying
    solely on our own 30-day cutoff below."""
    try:
        resp = http_requests.get("https://www.bing.com/news/search", params={
            "q": query,
            "qft": 'interval="9"',
            "format": "rss",
        }, timeout=15, headers=_FEED_REQUEST_HEADERS)
        resp.raise_for_status()
        feed = feedparser.parse(resp.content)
    except Exception:
        return {}

    matched = {}
    for entry in feed.entries:
        raw_link = entry.get("link")
        published = entry.get("published_parsed")
        if not raw_link or not published:
            continue
        published_ts = pd.Timestamp(time.mktime(published), unit="s", tz="UTC")
        if published_ts < cutoff:
            continue

        qs = parse_qs(urlparse(raw_link).query)
        link = qs.get("url", [raw_link])[0]

        matched[link] = {
            "title":     entry.get("title", ""),
            "link":      link,
            "source":    entry.get("news_source", ""),
            "published": published_ts.isoformat(),
            "image":     entry.get("news_image", ""),
            "origin":    "bing",
        }
    return matched


def _fetch_bing_news():
    """Run all configured Bing News queries in parallel, merge results."""
    if not _BING_QUERIES:
        return {}
    cutoff = pd.Timestamp.now(tz="UTC") - pd.Timedelta(days=_CLIMATE_NEWS_MAX_AGE_DAYS)
    items_by_link = {}
    with ThreadPoolExecutor(max_workers=len(_BING_QUERIES)) as pool:
        futures = [pool.submit(_fetch_bing_news_one, q, cutoff) for q in _BING_QUERIES]
        for fut in as_completed(futures):
            items_by_link.update(fut.result())
    return items_by_link


def _serpapi_needs_poll():
    """SerpApi has its own poll cadence (default: once/day), separate from
    the 6h interval used for the free RSS sources, to stay within its free
    tier of paid API calls."""
    state = _fs_load(_SERPAPI_ARCHIVE_PATH) or {}
    last_polled = state.get("_polled_at")
    if last_polled is None:
        return True
    now = pd.Timestamp.now(tz="UTC")
    return (now - pd.Timestamp(last_polled)).total_seconds() >= _SERPAPI_POLL_INTERVAL_SECONDS


def _mark_serpapi_polled():
    _fs_save(_SERPAPI_ARCHIVE_PATH, {"_polled_at": pd.Timestamp.now(tz="UTC").isoformat()})


def _poll_climate_news_sources():
    """Fetch all outlet feeds in parallel, return matched items keyed by link."""
    cutoff = pd.Timestamp.now(tz="UTC") - pd.Timedelta(days=_CLIMATE_NEWS_MAX_AGE_DAYS)
    items_by_link = {}
    jobs = (
        [(name, url, _CLIMATE_NEWS_KEYWORDS, None) for name, url in _CLIMATE_NEWS_SOURCES.items()]
        + [(name, url, _CLIMATE_NEWS_EN_KEYWORDS, None) for name, url in _CLIMATE_NEWS_EN_SOURCES.items()]
    )
    with ThreadPoolExecutor(max_workers=len(jobs) + 2) as pool:
        futures = [
            pool.submit(_fetch_one_climate_source, name, url, cutoff, keywords, require_keywords)
            for name, url, keywords, require_keywords in jobs
        ]
        futures.append(pool.submit(_fetch_bluesky_posts))
        futures.append(pool.submit(_fetch_bing_news))
        for fut in as_completed(futures):
            items_by_link.update(fut.result())
    return items_by_link


def _save_merged_archive(items_by_link):
    now = pd.Timestamp.now(tz="UTC")
    cutoff = now - pd.Timedelta(days=_CLIMATE_NEWS_MAX_AGE_DAYS)
    items_by_link = {
        link: item for link, item in items_by_link.items()
        if pd.Timestamp(item["published"]) >= cutoff
    }
    _fs_save(_CLIMATE_NEWS_ARCHIVE_PATH, {
        "_polled_at": now.isoformat(),
        "items": items_by_link,
    })
    return items_by_link


def refresh_climate_news():
    """Force a fresh poll of all outlet feeds and persist the merged archive.
    Intended to be called from cron (every 6h) so visitors never trigger the
    live poll. Does NOT poll SerpApi — that runs on its own once-daily cron
    entry via refresh_serpapi_news(), to respect its separate rate budget."""
    archive = _fs_load(_CLIMATE_NEWS_ARCHIVE_PATH) or {}
    items_by_link = archive.get("items", {})
    items_by_link.update(_poll_climate_news_sources())
    return _save_merged_archive(items_by_link)


def refresh_serpapi_news():
    """Force a fresh SerpApi poll and merge into the same archive used by the
    RSS/Bluesky sources. Intended to be called from its own once-daily cron
    entry (cron/climate_news_serpapi), separate from the 6h RSS refresh.
    Guards against polling more often than serpapi_poll_interval_hours even
    if invoked manually/extra times, to protect the paid-API rate budget."""
    if not _serpapi_needs_poll():
        archive = _fs_load(_CLIMATE_NEWS_ARCHIVE_PATH) or {}
        return archive.get("items", {})

    archive = _fs_load(_CLIMATE_NEWS_ARCHIVE_PATH) or {}
    items_by_link = archive.get("items", {})
    items_by_link.update(_fetch_serpapi_news())
    _mark_serpapi_polled()
    return _save_merged_archive(items_by_link)


def compute_climate_news(include_bluesky: bool = True):
    """
    Return recent Macedonian-language climate news from the persistent archive,
    sorted newest first. Polls outlet feeds itself only if the archive is
    missing or older than _CLIMATE_NEWS_POLL_INTERVAL_SECONDS (fallback for
    when the cron refresh hasn't run yet); otherwise serves straight from disk.

    include_bluesky=False excludes Bluesky posts — used for the page's news
    list, since those posts are already shown in the dedicated Bluesky widget
    there and would be redundant. The combined RSS feed (include_bluesky=True,
    the default) is the one place Bluesky posts and news appear together.
    """
    archive = _fs_load(_CLIMATE_NEWS_ARCHIVE_PATH) or {}
    last_polled = archive.get("_polled_at")
    now = pd.Timestamp.now(tz="UTC")
    needs_poll = (
        last_polled is None
        or (now - pd.Timestamp(last_polled)).total_seconds() >= _CLIMATE_NEWS_POLL_INTERVAL_SECONDS
    )

    items_by_link = archive.get("items", {})
    if needs_poll:
        items_by_link = refresh_climate_news()

    items = items_by_link.values()
    if not include_bluesky:
        items = [it for it in items if it["source"] != "Bluesky"]
    return sorted(items, key=lambda it: it["published"], reverse=True)


def build_rss_xml(items) -> str:
    """Render the merged climate-news + Bluesky items (from compute_climate_news)
    as an RSS 2.0 feed, so any RSS reader can subscribe to everything we
    publish in one place."""
    site_url = f"https://{CONFIG['branding']['domain']}"
    item_xml = []
    for it in items:
        description = escape(it["title"])
        if it.get("image"):
            description += f'<br/><img src="{escape(it["image"])}" />'
        pub_date = format_datetime(pd.Timestamp(it["published"]).to_pydatetime())
        item_xml.append(f"""
    <item>
      <title>{escape(it["title"])}</title>
      <link>{escape(it["link"])}</link>
      <guid isPermaLink="true">{escape(it["link"])}</guid>
      <pubDate>{pub_date}</pubDate>
      <source>{escape(it["source"])}</source>
      <description>{description}</description>
    </item>""")

    return f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>Климатски вести — climate.mk</title>
    <link>{escape(site_url)}/climate-news.html</link>
    <description>Климатски промени во Македонија — вести од македонски медиуми и објави од climate.mk</description>
    <language>mk</language>{"".join(item_xml)}
  </channel>
</rss>"""


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "--serpapi":
        n = len(refresh_serpapi_news())
        print(f"[climate_news] SerpApi refresh: {n} items currently retained")
    else:
        n = len(refresh_climate_news())
        print(f"[climate_news] refreshed archive: {n} items currently retained")
