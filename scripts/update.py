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
WINDOWS = {"economy": (4, 45), "politics": (3, 45), "left": (10, 70), "world": (14, 70), "artnow": (60, 30)}
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


IMG_SRC = re.compile(r'<img[^>]+src=["\']([^"\']+)["\']', re.I)


def entry_image(e):
    """The picture a feed attaches to an item, if any."""
    for key in ("media_thumbnail", "media_content"):
        for m in e.get(key) or []:
            url = m.get("url")
            if url and (m.get("medium") in (None, "image") or re.search(r"\.(jpe?g|png|webp|gif)", url, re.I)):
                return url
    for enc in e.get("enclosures") or []:
        if str(enc.get("type", "")).startswith("image") and enc.get("href"):
            return enc["href"]
    for blob in [*(c.get("value", "") for c in e.get("content") or []), e.get("summary", "")]:
        m = IMG_SRC.search(blob or "")
        if m and not m.group(1).startswith("data:"):
            return html.unescape(m.group(1))
    return None


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
        item = {
            "title": title,
            "url": link,
            "source": src["name"],
            "date": iso(dt),
            "summary": clean(e.get("summary") or e.get("description")),
        }
        img = entry_image(e)
        if img:
            item["image"] = img
        items.append(item)
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


# ---------- Pictures from Wikipedia (heroes and artwork of the day) ----------

def wiki_image(title):
    """Lead image of a Wikipedia article, but only if it lives on Wikimedia
    Commons (freely licensed). Fair-use images on English Wikipedia are skipped."""
    url = "https://en.wikipedia.org/api/rest_v1/page/summary/" + requests.utils.quote(title.replace(" ", "_"), safe="")
    try:
        d = get_patiently(url, tries=3).json()
    except Exception:  # noqa: BLE001
        return None
    orig = d.get("originalimage") or {}
    thumb = (d.get("thumbnail") or {}).get("source", "")
    src = orig.get("source", "")
    if "/wikipedia/commons/" not in src:
        return None
    # Ask for a 640px copy rather than a huge original.
    if orig.get("width", 0) > 900 and re.search(r"/\d+px-", thumb):
        src = re.sub(r"/\d+px-", "/640px-", thumb)
    filename = requests.utils.unquote(orig["source"].rsplit("/", 1)[-1])
    return {
        "src": src,
        "credit": "https://commons.wikimedia.org/wiki/File:" + requests.utils.quote(filename),
        "page": (d.get("content_urls") or {}).get("desktop", {}).get("page"),
    }


def gather_wiki_images():
    data = ROOT / "site" / "data"
    people = json.loads((data / "figures.json").read_text())
    art = json.loads((data / "art.json").read_text())
    jobs = [("figures", p["name"], p.get("wiki") or p["name"]) for p in people]
    jobs += [("art", w["title"], w["wiki"]) for w in art if not w.get("noimage")]
    out = {"figures": {}, "art": {}}
    def run(job):
        kind, key, title = job
        return kind, key, wiki_image(title)
    with ThreadPoolExecutor(4) as pool:
        for kind, key, img in pool.map(run, jobs):
            if img:
                out[kind][key] = img
    print(f"wikipedia images: {len(out['figures'])} heroes, {len(out['art'])} artworks")
    return out


# ---------- Public-domain art from three museums ----------

def aic(term):
    d = get_patiently(
        "https://api.artic.edu/api/v1/artworks/search", tries=3, q=term, limit=15,
        fields="id,title,artist_title,artist_display,date_display,image_id,is_public_domain",
        **{"query[term][is_public_domain]": "true"},
    ).json()
    for w in d.get("data", []):
        if w.get("image_id") and w.get("is_public_domain"):
            yield {
                "title": clean(w["title"], 140), "artist": w.get("artist_title") or clean(w.get("artist_display"), 80),
                "date": w.get("date_display") or "", "museum": "Art Institute of Chicago",
                "image": f"https://www.artic.edu/iiif/2/{w['image_id']}/full/400,/0/default.jpg",
                "url": f"https://www.artic.edu/artworks/{w['id']}",
            }


def met(term):
    ids = (get_patiently("https://collectionapi.metmuseum.org/public/collection/v1/search", tries=3,
                         q=term, hasImages="true").json().get("objectIDs") or [])[:8]
    for i in ids:
        try:
            w = get_patiently(f"https://collectionapi.metmuseum.org/public/collection/v1/objects/{i}", tries=2).json()
        except Exception:  # noqa: BLE001
            continue
        if w.get("isPublicDomain") and w.get("primaryImageSmall"):
            yield {
                "title": clean(w.get("title"), 140), "artist": w.get("artistDisplayName") or "",
                "date": w.get("objectDate") or "", "museum": "The Met",
                "image": w["primaryImageSmall"], "url": w.get("objectURL"),
            }
        time.sleep(0.1)


def cleveland(term):
    d = get_patiently("https://openaccess-api.clevelandart.org/api/artworks/", tries=3,
                      q=term, cc0=1, has_image=1, limit=10).json()
    for w in d.get("data", []):
        img = ((w.get("images") or {}).get("web") or {}).get("url")
        if img:
            creators = w.get("creators") or []
            yield {
                "title": clean(w.get("title"), 140),
                "artist": clean(creators[0].get("description"), 80) if creators else "",
                "date": w.get("creation_date") or "", "museum": "Cleveland Museum of Art",
                "image": img, "url": w.get("url"),
            }


def gather_museums():
    cfg = CONFIG.get("museums", {})
    pool, seen = [], set()
    for name, fn in (("Art Institute of Chicago", aic), ("The Met", met), ("Cleveland Museum of Art", cleveland)):
        got = 0
        for term in cfg.get("terms", []):
            try:
                for w in fn(term):
                    key = (w["title"].lower(), w["museum"])
                    if w["title"] and key not in seen:
                        seen.add(key)
                        w["term"] = term
                        pool.append(w)
                        got += 1
            except Exception as e:  # noqa: BLE001
                failed.append({"source": f"{name} ({term})", "section": "museums", "error": str(e).split(" for url")[0][:100]})
                break   # one failure usually means the museum is refusing; don't keep knocking
            time.sleep(0.3)
        print(f"  {name}: {got} works")
    # A different mix each edition, spread across museums and search terms.
    import random
    rng = random.Random(NOW.strftime("%Y%m%d%H"))
    rng.shuffle(pool)
    picked, per_term = [], {}
    for w in pool:
        if per_term.get(w["term"], 0) < 2:
            picked.append(w)
            per_term[w["term"]] = per_term.get(w["term"], 0) + 1
        if len(picked) >= cfg.get("show", 12):
            break
    print(f"museums: {len(picked)} shown from {len(pool)} found")
    return picked


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

    sections["museums"] = gather_museums()
    images = gather_wiki_images()

    payload = {"generated_at": iso(NOW), "sections": sections, "images": images, "failed": failed}

    # If everything failed (network outage), keep the last good file.
    if not any(v for k, v in sections.items() if k != "museums") and OUT.exists():
        print("Nothing fetched; keeping previous feed.json", file=sys.stderr)
        return
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=1))
    if failed:
        print(f"{len(failed)} sources failed:", *[f"  {f['source']}: {f['error']}" for f in failed], sep="\n")


if __name__ == "__main__":
    main()
