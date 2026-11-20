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
WINDOWS = {"economy": (4, 60), "politics": (3, 60), "independent": (7, 200), "artnow": (60, 30),
           "podcasts": (45, 30), "videos": (30, 30), "local": (5, 50)}
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


def gnews_url(query):
    return ("https://news.google.com/rss/search?q=" + requests.utils.quote(query)
            + "&hl=en-US&gl=US&ceid=US:en")


def tidy_google(feed):
    for e in feed.entries:
        # Google appends " - Publication Name" to each headline.
        src = (e.get("source") or {}).get("title")
        if src and e.get("title", "").endswith(" - " + src):
            e["title"] = e["title"][: -len(" - " + src)]
        e["summary"] = ""   # Google's summary is just the headline again
    feed["via_google"] = True
    return feed


def google_news(host, days=14):
    """Last resort: the site's recent articles as indexed by Google News."""
    domain = host.removeprefix("www.")
    feed, _ = try_feed(gnews_url(f"site:{domain} when:{days}d"))
    return tidy_google(feed) if feed else None


def load_feed(url, allow_google=True):
    """Fetch a feed. If the address is a web page, or is wrong, look for
    the site's feed: first in the page's <link rel=alternate> tags, then
    at the usual feed paths."""
    problems = []
    feed, resp = try_feed(url, problems)
    if feed:
        return tidy_google(feed) if "news.google.com" in url else feed
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
    feed = google_news(parts.netloc) if allow_google else None
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
        feed = load_feed(src["url"], allow_google=not src.get("no_google"))
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
        gsrc = (e.get("source") or {}).get("title") if feed.get("via_google") and src.get("search") else None
        item = {
            "title": title,
            "url": link,
            "source": gsrc or src["name"],
            "date": iso(dt),
            "summary": clean(e.get("summary") or e.get("description")),
        }
        img = entry_image(e)
        if img:
            item["image"] = img
        for enc in e.get("enclosures") or []:
            if str(enc.get("type", "")).startswith("audio") and enc.get("href"):
                item["audio"] = enc["href"]
                break
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
    """Lead image of a Wikipedia article, using the MediaWiki API's
    free-licence filter, so only Commons images (no fair-use files) come back.
    Asks for a 640px rendering, which also turns SVG drawings into PNGs."""
    try:
        d = get_patiently(
            "https://en.wikipedia.org/w/api.php", tries=3,
            action="query", format="json", formatversion=2, redirects=1, titles=title,
            prop="pageimages|info", piprop="thumbnail|name", pithumbsize=640,
            pilicense="free", inprop="url",
        ).json()
    except Exception:  # noqa: BLE001
        return None
    pages = ((d or {}).get("query") or {}).get("pages") or []
    if not pages or pages[0].get("missing"):
        return None
    page = pages[0]
    thumb = (page.get("thumbnail") or {}).get("source")
    name = page.get("pageimage")
    if not thumb or "/wikipedia/commons/" not in thumb:
        return None
    return {
        "src": thumb,
        "credit": "https://commons.wikimedia.org/wiki/File:" + requests.utils.quote(name) if name else None,
        "page": page.get("fullurl"),
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


# ---------- Independent outlets, sorted into Economy or Politics ----------

ECON = re.compile(
    r"\b(econom\w*|wages?|paychecks?|salar\w+|inflation|prices?|cost of living|jobs?|jobless|unemploy\w*|"
    r"labou?r|workers?|workplace|unions?|unioni[sz]\w*|strik\w*|walkouts?|layoffs?|"
    r"housing|rents?|renters?|tenants?|landlords?|evictions?|mortgages?|"
    r"tax(es|ation)?|budgets?|debts?|deficit|austerity|privati[sz]\w*|"
    r"tariffs?|trade|markets?|stocks?|banks?|banking|financ\w*|investors?|"
    r"federal reserve|the fed|interest rates?|central bank|recession|gdp|growth|"
    r"profits?|corporat\w*|billionaires?|wealth\w*|inequality|poverty|minimum wage|"
    r"capital|capitalis\w*|neoliberal\w*|supply chains?|energy prices|oil prices|"
    r"amazon|walmart|starbucks|tesla|uber|private equity|wall street|crypto\w*|"
    r"food prices|grocer\w*|health ?care costs?|medicaid|social security|pensions?|benefits)\b", re.I)


PARTY = re.compile(r"\blabou?r (party|government|leader|leadership|mps?|ministers?|conference|left|right)\b|\b(capital city|the capital)\b", re.I)


def is_economic(item):
    title = PARTY.sub("", item["title"])
    body = PARTY.sub("", item.get("summary") or "")
    title_hits = len(ECON.findall(title))
    body_hits = len(ECON.findall(body))
    return title_hits >= 1 or body_hits >= 2


def merge_independent(sections):
    """Read every independent outlet once, then file each article under
    Economy or Politics by what it's about, mixed in with the wire and
    mainstream reporting and sorted by time."""
    items = build_rss_section("independent", CONFIG["rss"].get("independent", []))
    econ = [i for i in items if is_economic(i)]
    pol = [i for i in items if not is_economic(i)]
    for key, extra, keep in (("economy", econ, 110), ("politics", pol, 130)):
        merged, seen = [], set()
        for it in sorted(sections.get(key, []) + extra, key=lambda i: i["date"] or "", reverse=True):
            k = re.sub(r"\W+", "", it["title"].lower())[:80]
            if k not in seen:
                seen.add(k)
                merged.append(it)
        sections[key] = merged[:keep]
    print(f"independent: {len(econ)} to economy, {len(pol)} to politics")


# ---------- Strike tracker ----------

STRIKE_WORDS = re.compile(r"\b(strik(e|es|ers|ing)|walk ?outs?|walked out|picket(s|ing|ers)?|lockouts?|locked out|work stoppages?|sick-?outs?)\b", re.I)
NOT_LABOR = re.compile(r"air ?strikes?|drone strikes?|missile strikes?|strikes? (down|back|deal|a balance|a chord|out)|struck down|lucky strike|strike zone|first strike|preemptive|military strike|striker\b.*\b(goal|club|signs?)|strikes? again", re.I)


def build_strikes(sections):
    searches = [{"name": "Google News", "url": gnews_url(q + " when:7d"), "search": True, "no_google": True}
                for q in CONFIG.get("strike_searches", [])]
    pool = [i for src in searches for i in fetch_rss("strikes", src)]
    pool += [i for k in ("economy", "politics", "local") for i in sections.get(k, [])]
    cutoff = NOW - timedelta(days=7)
    seen, out = set(), []
    for it in sorted(pool, key=lambda i: i["date"] or "", reverse=True):
        t = it["title"]
        if not STRIKE_WORDS.search(t) or NOT_LABOR.search(t):
            continue
        if it["date"] and datetime.fromisoformat(it["date"].replace("Z", "+00:00")) < cutoff:
            continue
        key = re.sub(r"\W+", "", t.lower())[:70]
        if key in seen:
            continue
        seen.add(key)
        out.append({k: it[k] for k in ("title", "url", "source", "date") if k in it})
    return out[:40]


def build_local():
    srcs = list(CONFIG["rss"].get("local", []))
    srcs += [{"name": "Google News", "url": gnews_url(q + " when:5d"), "search": True, "no_google": True}
             for q in CONFIG.get("local_searches", [])]
    return build_rss_section("local", srcs)


# ---------- Economy in numbers (FRED, no key needed) ----------

DASHBOARD = [
    ("UNRATE", "Unemployment rate", "%", "level"),
    ("CPIAUCSL", "Inflation over the past year", "%", "yoy"),
    ("LES1252881600Q", "Median weekly pay, adjusted for inflation", "1982–84 dollars", "level"),
    ("PRS85006173", "Workers’ share of business income", "index, 2017 = 100", "level"),
    ("WFRBST01134", "Share of wealth held by the richest 1%", "%", "level"),
    ("WFRBSB50215", "Share of wealth held by the bottom half", "%", "level"),
]


def fred_series(sid, mode):
    start = (NOW - timedelta(days=365 * 12)).date().isoformat()
    text = get_patiently("https://fred.stlouisfed.org/graph/fredgraph.csv", tries=3, id=sid, cosd=start).text
    rows = []
    for line in text.strip().splitlines()[1:]:
        parts = line.split(",")
        if len(parts) >= 2 and parts[1] not in (".", ""):
            try:
                rows.append((parts[0], float(parts[1])))
            except ValueError:
                pass
    if mode == "yoy":
        rows = [(d, round((v / rows[i - 12][1] - 1) * 100, 2)) for i, (d, v) in enumerate(rows) if i >= 12]
    return rows[-120:]


def build_dashboard():
    out = []
    for sid, label, unit, mode in DASHBOARD:
        try:
            pts = fred_series(sid, mode)
        except Exception as e:  # noqa: BLE001
            failed.append({"source": f"FRED {sid}", "section": "economy in numbers", "error": str(e).split(" for url")[0][:100]})
            continue
        if len(pts) < 2:
            continue
        # change over roughly one year, whatever the series frequency
        per_year = 12 if len(pts) > 60 else 4
        back = pts[-1 - per_year] if len(pts) > per_year else pts[0]
        out.append({"id": sid, "label": label, "unit": unit, "latest": pts[-1][1], "date": pts[-1][0],
                    "change": round(pts[-1][1] - back[1], 2), "since": back[0], "points": pts,
                    "url": f"https://fred.stlouisfed.org/series/{sid}"})
        time.sleep(0.3)
    print(f"dashboard: {len(out)} series")
    return out


# ---------- Text of the week: check the links still work ----------

def check_texts():
    path = ROOT / "site" / "data" / "texts.json"
    if not path.exists():
        return []
    broken = []
    for t in json.loads(path.read_text()):
        try:
            r = SESSION.get(t["url"], timeout=20, headers=FEED_HEADERS, stream=True)
            r.close()
            if r.status_code >= 400:
                broken.append(t["title"])
        except Exception:  # noqa: BLE001
            broken.append(t["title"])
    return broken


# ---------- Back issues ----------

def save_back_issue(payload):
    """Keep a slim copy of each edition (headlines only) in site/archive,
    which the workflow commits so the history survives between runs."""
    arch = ROOT / "site" / "archive"
    arch.mkdir(parents=True, exist_ok=True)
    keep = ("economy", "politics", "local", "strikes", "journals", "scholarship")
    slim = {"generated_at": payload["generated_at"], "sections": {
        k: [{f: it[f] for f in ("title", "url", "source", "date", "authors") if f in it} for it in payload["sections"].get(k, [])[:25]]
        for k in keep}}
    name = NOW.strftime("%Y-%m-%d-%H%M") + ".json"
    (arch / name).write_text(json.dumps(slim, ensure_ascii=False, separators=(",", ":")))
    # keep a year of editions
    files = sorted(f for f in arch.glob("20*.json"))
    for old in files[:-730]:
        old.unlink()
    files = sorted(f.name for f in arch.glob("20*.json"))
    (arch / "index.json").write_text(json.dumps(list(reversed(files))))
    print(f"back issues: {len(files)} saved")


def main():
    sections = {}
    for name, sources in CONFIG["rss"].items():
        if name in ("local", "independent"):
            continue
        sections[name] = build_rss_section(name, sources)
        print(f"{name}: {len(sections[name])} items")

    merge_independent(sections)

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

    sections["local"] = build_local()
    print(f"local: {len(sections['local'])} items")
    sections["strikes"] = build_strikes(sections)
    print(f"strikes: {len(sections['strikes'])} items")
    sections["museums"] = gather_museums()
    images = gather_wiki_images()

    dashboard = build_dashboard()
    broken_texts = check_texts()

    payload = {"generated_at": iso(NOW), "sections": sections, "images": images,
               "dashboard": dashboard, "broken_texts": broken_texts,
               "local_place": CONFIG.get("local_place", "Local"), "failed": failed}

    # If everything failed (network outage), keep the last good file.
    if not any(v for k, v in sections.items() if k != "museums") and OUT.exists():
        print("Nothing fetched; keeping previous feed.json", file=sys.stderr)
        return
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=1))
    save_back_issue(payload)
    if failed:
        print(f"{len(failed)} sources failed:", *[f"  {f['source']}: {f['error']}" for f in failed], sep="\n")


if __name__ == "__main__":
    main()
