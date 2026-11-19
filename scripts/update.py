#!/usr/bin/env python3
"""Gather news feeds, journal tables of contents and recent scholarship
into site/data/feed.json. Run by GitHub Actions twice a day; safe to run
locally too:  pip install feedparser requests && python scripts/update.py
"""
import html
import json
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import feedparser
import requests

ROOT = Path(__file__).resolve().parent.parent
CONFIG = json.loads((ROOT / "sources.json").read_text())
OUT = ROOT / "site" / "data" / "feed.json"
NOW = datetime.now(timezone.utc)

# Contact email for Crossref/OpenAlex. A CONTACT_EMAIL secret overrides sources.json.
EMAIL = os.environ.get("CONTACT_EMAIL") or CONFIG.get("contact_email", "")
if not EMAIL or EMAIL.endswith("example.com"):
    EMAIL = ""
OPENALEX_KEY = os.environ.get("OPENALEX_API_KEY", "").strip()

API_UA = f"TwiceDaily/1.0 (personal news digest{'; mailto:' + EMAIL if EMAIL else ''})"
# Many news sites turn away unknown robots, so feeds are fetched the way
# ordinary feed readers fetch them.
FEED_HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; TwiceDaily/1.0; personal RSS reader)",
    "Accept": "application/rss+xml, application/atom+xml, application/xml;q=0.9, text/xml;q=0.9, text/html;q=0.8, */*;q=0.5",
    "Accept-Language": "en-US,en;q=0.9",
}
SESSION = requests.Session()
SESSION.headers["User-Agent"] = API_UA

# How far back each section looks, and how many items it keeps.
WINDOWS = {"economy": (4, 45), "politics": (3, 45), "left": (10, 70), "world": (14, 70)}
PER_JOURNAL = 5
SCHOLARSHIP_MAX = 40

failed = []


def clean(text, limit=320):
    """Strip tags/entities and trim to a readable teaser."""
    if not text:
        return ""
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)
    text = re.sub(r"\s+", " ", text).strip()
    text = re.sub(r"^(Abstract|ABSTRACT)[:.]?\s*", "", text)
    if len(text) > limit:
        text = text[:limit].rsplit(" ", 1)[0].rstrip(",.;:") + "…"
    return text


def iso(dt):
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if dt else None


def get(url, headers=None, **params):
    r = SESSION.get(url, params=params or None, timeout=25, headers=headers)
    r.raise_for_status()
    return r


def get_patiently(url, tries=5, **params):
    """For the scholarly APIs: on 429 (too many requests) or a server
    hiccup, wait as long as asked (or back off) and try again."""
    for attempt in range(tries):
        r = SESSION.get(url, params=params or None, timeout=40)
        if r.status_code not in (429, 500, 502, 503, 504) or attempt == tries - 1:
            r.raise_for_status()
            return r
        wait = r.headers.get("Retry-After")
        wait = float(wait) if wait and wait.replace(".", "", 1).isdigit() else 2 * (2 ** attempt)
        time.sleep(min(wait, 60))


# ---------- RSS / Atom ----------

FEED_LINK = re.compile(
    r'<link[^>]+type=["\']application/(?:rss|atom)\+xml["\'][^>]*>', re.I)
HREF = re.compile(r'href=["\']([^"\']+)["\']', re.I)
COMMON_PATHS = ("/feed/", "/feed", "/rss", "/rss.xml", "/feed.xml", "/index.xml", "/atom.xml")


def try_feed(url, problems=None):
    """Return (parsed feed with entries or None, response or None)."""
    try:
        r = SESSION.get(url, timeout=25, headers=FEED_HEADERS)
    except Exception as e:  # noqa: BLE001
        if problems is not None:
            problems.append(type(e).__name__)
        return None, None
    if r.status_code >= 400:
        if problems is not None:
            problems.append(f"HTTP {r.status_code}")
        return None, None
    feed = feedparser.parse(r.content)
    return (feed if feed.entries else None), r


def google_news(host, days=14):
    """Last resort: the site's recent articles as indexed by Google News."""
    domain = host.removeprefix("www.")
    feed, _ = try_feed(
        "https://news.google.com/rss/search?q="
        + requests.utils.quote(f"site:{domain} when:{days}d")
        + "&hl=en-US&gl=US&ceid=US:en"
    )
    if not feed:
        return None
    for e in feed.entries:
        # Google appends " - Publication Name" to each headline.
        src = (e.get("source") or {}).get("title")
        if src and e.get("title", "").endswith(" - " + src):
            e["title"] = e["title"][: -len(" - " + src)]
        e["summary"] = ""   # Google's summary is just the headline again
    feed["via_google"] = True
    return feed


def load_feed(url):
    """Fetch a feed. If the address is a web page, or is wrong, look for
    the site's feed: first in the page's <link rel=alternate> tags, then
    at the usual feed paths."""
    problems = []
    feed, resp = try_feed(url, problems)
    if feed:
        return feed
    from urllib.parse import urljoin, urlsplit
    parts = urlsplit(url)
    home = f"{parts.scheme}://{parts.netloc}/"
    candidates = []
    for page in ([resp] if resp is not None else []) + ([try_feed(home, problems)[1]] if url.rstrip("/") + "/" != home else []):
        if page is None:
            continue
        for tag in FEED_LINK.findall(page.text[:200000]):
            m = HREF.search(tag)
            if m:
                candidates.append(urljoin(page.url, html.unescape(m.group(1))))
    candidates += [home.rstrip("/") + p for p in COMMON_PATHS]
    seen = {url}
    for c in candidates:
        if c in seen:
            continue
        seen.add(c)
        feed, _ = try_feed(c)
        if feed:
            print(f"  found feed for {url}: {c}")
            return feed
    feed = google_news(parts.netloc)
    if feed:
        print(f"  using Google News for {url} (site said: {', '.join(problems) or 'no feed'})")
        return feed
    detail = ", ".join(dict.fromkeys(problems)) or "no feed on the page"
    raise ValueError(f"site refused or has no feed ({detail}), and Google News had nothing recent")


def fetch_rss(section, src):
    try:
        feed = load_feed(src["url"])
    except Exception as e:  # noqa: BLE001
        failed.append({"source": src["name"], "section": section, "error": str(e)[:120]})
        return []
    items = []
    for e in feed.entries:
        t = e.get("published_parsed") or e.get("updated_parsed")
        dt = datetime.fromtimestamp(time.mktime(t), timezone.utc) if t else None
        link = e.get("link")
        title = clean(e.get("title"), 220)
        if not (link and title):
            continue
        items.append({
            "title": title,
            "url": link,
            "source": src["name"],
            "date": iso(dt),
            "summary": clean(e.get("summary") or e.get("description")),
        })
    return items


def build_rss_section(section, sources):
    days, keep = WINDOWS.get(section, (7, 40))
    cutoff = NOW - timedelta(days=days)
    with ThreadPoolExecutor(8) as pool:
        batches = list(pool.map(lambda s: fetch_rss(section, s), sources))
    seen, items = set(), []
    for item in (i for b in batches for i in b):
        key = re.sub(r"\W+", "", item["title"].lower())[:80]
        if key in seen:
            continue
        if item["date"] and datetime.fromisoformat(item["date"].replace("Z", "+00:00")) < cutoff:
            continue
        seen.add(key)
        items.append(item)
    items.sort(key=lambda i: i["date"] or "", reverse=True)
    return items[:keep]


# ---------- Journals via Crossref ----------

def crossref_date(w):
    for k in ("published-online", "published-print", "published", "issued", "created"):
        parts = (w.get(k) or {}).get("date-parts", [[None]])[0]
        if parts and parts[0]:
            y, m, d = (parts + [1, 1])[:3]
            try:
                return datetime(int(y), int(m or 1), int(d or 1), tzinfo=timezone.utc)
            except ValueError:
                continue
    return None


def fetch_journal(j):
    try:
        params = dict(sort="published", order="desc", rows=25,
                      filter=f"until-pub-date:{NOW.date().isoformat()}")
        if EMAIL:
            params["mailto"] = EMAIL
        data = get_patiently(f"https://api.crossref.org/journals/{j['issn']}/works", **params).json()
    except Exception as e:  # noqa: BLE001
        failed.append({"source": j["name"], "section": "journals", "error": str(e)[:120]})
        return []
    out = []
    for w in data.get("message", {}).get("items", []):
        title = clean((w.get("title") or [""])[0], 240)
        if not title or w.get("type") not in (None, "journal-article"):
            continue
        # Skip housekeeping entries that journals register alongside articles.
        if re.match(r"^(front matter|back matter|issue information|editorial board|table of contents|books received|erratum|correction)", title, re.I):
            continue
        authors = [" ".join(filter(None, [a.get("given"), a.get("family")])) for a in w.get("author", [])][:4]
        out.append({
            "title": title,
            "url": w.get("URL"),
            "source": j["name"],
            "date": iso(crossref_date(w)),
            "authors": [a for a in authors if a],
            "summary": clean(w.get("abstract")),
        })
        if len(out) >= PER_JOURNAL:
            break
    return out


# ---------- Wider scholarship via OpenAlex ----------

def rebuild_abstract(inv):
    if not inv:
        return ""
    words = sorted((pos, w) for w, ps in inv.items() for pos in ps)
    return " ".join(w for _, w in words)


def fetch_openalex(terms, since):
    """One request covering every search term, joined with OR."""
    query = " OR ".join(f'"{t}"' if " " in t else t for t in terms)
    params = dict(
        search=query,
        filter=f"from_publication_date:{since},to_publication_date:{NOW.date().isoformat()},type:article",
        sort="publication_date:desc",
        per_page=100,
    )
    if OPENALEX_KEY:
        params["api_key"] = OPENALEX_KEY
    if EMAIL:
        params["mailto"] = EMAIL
    try:
        data = get_patiently("https://api.openalex.org/works", tries=3, **params).json()
    except Exception as e:  # noqa: BLE001
        msg = str(e).split(" for url")[0][:120]
        if not OPENALEX_KEY:
            msg += " (add a free OpenAlex API key: see README)"
        failed.append({"source": "OpenAlex", "section": "scholarship", "error": msg})
        return []
    out = []
    for w in data.get("results", []):
        loc = (w.get("primary_location") or {})
        venue = ((loc.get("source") or {}).get("display_name")) or "Preprint or other venue"
        url = w.get("doi") or loc.get("landing_page_url") or w.get("id")
        title = clean(w.get("display_name") or w.get("title"), 240)
        if not title or not url:
            continue
        out.append({
            "title": title,
            "url": url,
            "source": venue,
            "date": (w.get("publication_date") or "") + "T00:00:00Z",
            "authors": [a["author"]["display_name"] for a in w.get("authorships", [])[:4] if a.get("author")],
            "summary": clean(rebuild_abstract(w.get("abstract_inverted_index"))),
            "cited_by": w.get("cited_by_count", 0),
        })
    return out


def main():
    sections = {}
    for name, sources in CONFIG["rss"].items():
        sections[name] = build_rss_section(name, sources)
        print(f"{name}: {len(sections[name])} items")

    # Crossref allows one list request at a time (three a second with an email).
    journals = []
    for j in CONFIG["journals"]:
        journals += fetch_journal(j)
        time.sleep(0.4 if EMAIL else 1.1)
    journals.sort(key=lambda i: i["date"] or "", reverse=True)
    sections["journals"] = journals
    print(f"journals: {len(journals)} items")

    sch = CONFIG.get("scholarship", {})
    since = (NOW - timedelta(days=sch.get("days_back", 120))).date().isoformat()
    journal_titles = {re.sub(r"\W+", "", i["title"].lower()) for i in journals}
    seen, scholarship = set(), []
    for item in fetch_openalex(sch.get("terms", []), since):
        key = re.sub(r"\W+", "", item["title"].lower())
        if key in seen or key in journal_titles:
            continue
        seen.add(key)
        scholarship.append(item)
    scholarship.sort(key=lambda i: i["date"] or "", reverse=True)
    sections["scholarship"] = scholarship[:SCHOLARSHIP_MAX]
    print(f"scholarship: {len(sections['scholarship'])} items")

    payload = {"generated_at": iso(NOW), "sections": sections, "failed": failed}

    # If everything failed (network outage), keep the last good file.
    if not any(sections.values()) and OUT.exists():
        print("Nothing fetched; keeping previous feed.json", file=sys.stderr)
        return
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=1))
    if failed:
        print(f"{len(failed)} sources failed:", *[f"  {f['source']}: {f['error']}" for f in failed], sep="\n")


if __name__ == "__main__":
    main()
