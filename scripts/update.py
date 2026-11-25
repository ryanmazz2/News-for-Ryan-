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
WINDOWS = {"economy": (4, 60), "politics": (3, 60), "mixed": (5, 400), "essays": (21, 60), "releases": (14, 40), "papers": (180, 40), "reviews": (90, 40), "artnow": (60, 30),
           "podcasts": (45, 30), "videos": (30, 30), "local": (5, 50), "hog": (3, 200)}
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


def try_feed(url, problems=None, headers=None):
    """Return (parsed feed with entries or None, response or None)."""
    try:
        r = SESSION.get(url, timeout=25, headers=headers or FEED_HEADERS)
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


def load_feed(url, allow_google=True, headers=None):
    """Fetch a feed. If the address is a web page, or is wrong, look for
    the site's feed: first in the page's <link rel=alternate> tags, then
    at the usual feed paths."""
    problems = []
    feed, resp = try_feed(url, problems, headers)
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


def itunes_feed(src):
    """Find a podcast's official feed in Apple's public directory."""
    d = get_patiently("https://itunes.apple.com/search", tries=3, term=src["itunes"], media="podcast", entity="podcast", limit=8).json()
    want = src.get("match", src["name"]).lower()
    for r in d.get("results", []):
        if want in (r.get("collectionName") or "").lower() and r.get("feedUrl"):
            return r["feedUrl"]
    raise ValueError("not found in Apple's podcast directory")


def crossref_prefix_items(section, src):
    d = get_patiently(f"https://api.crossref.org/prefixes/{src['crossref_prefix']}/works", tries=4,
                      sort="published", order="desc", rows=20, **({"mailto": EMAIL} if EMAIL else {})).json()
    out = []
    for w in d.get("message", {}).get("items", []):
        title = clean((w.get("title") or [""])[0], 240)
        if not title:
            continue
        authors = [" ".join(filter(None, [a.get("given"), a.get("family")])) for a in w.get("author", [])][:4]
        out.append({"title": title, "url": w.get("URL"), "source": src["name"], "date": iso(crossref_date(w)),
                    "authors": [a for a in authors if a], "summary": clean(w.get("abstract")), "kind": src.get("kind", "independent")})
    return out


def fetch_rss(section, src):
    if src.get("crossref_prefix"):
        try:
            return crossref_prefix_items(section, src)
        except Exception as e:  # noqa: BLE001
            failed.append({"source": src["name"], "section": section, "error": str(e).split(" for url")[0][:120]})
            return []
    try:
        if src.get("itunes"):
            src = {**src, "url": itunes_feed(src)}
        feed = load_feed(src["url"], allow_google=not src.get("no_google"),
                         headers={"User-Agent": API_UA, "Accept": "application/rss+xml, application/xml, text/xml"} if src.get("official") else None)
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
            "kind": src.get("kind", "newsroom"),
        }
        tags = [str(t.get("term", "")).lower() for t in (e.get("tags") or []) if t.get("term")]
        if tags:
            item["tags"] = tags[:12]
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
    if section in ("local", "essays", "releases", "artnow"):
        items = cap_per_source(merge_similar(items), 8 if section == "local" else 10)
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
    for name, fn in (("Art Institute of Chicago", aic), ("Cleveland Museum of Art", cleveland)):
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


# ---------- Sorting the news: Economy, Politics, or not news at all ----------

ECON_LABELS = {"economy", "economics", "business", "money", "finance", "markets", "labor", "labour", "work",
               "workers", "unions", "jobs", "employment", "housing", "inequality", "wealth", "poverty", "taxes",
               "trade", "wages", "personal finance", "real estate", "banking", "economic policy", "corporations",
               "working people", "workers' rights", "labor rights", "cost of living", "monopoly", "antitrust"}
POL_LABELS = {"politics", "elections", "election", "congress", "government", "courts", "supreme court", "justice",
              "immigration", "foreign policy", "world", "international", "us news", "national", "democracy",
              "war", "military", "civil rights", "criminal justice", "policing", "law", "white house", "voting",
              "middle east", "europe", "africa", "asia", "latin america", "climate", "environment", "health", "education"}
DROP_LABELS = {"sport", "sports", "culture", "arts", "art", "books", "book review", "film", "movies", "music",
               "television", "tv", "reviews", "review", "lifestyle", "food", "recipes", "travel", "style", "fashion",
               "games", "gaming", "puzzles", "crossword", "horoscopes", "celebrity", "entertainment", "podcast",
               "podcasts", "video", "videos", "poetry", "fiction", "obituaries", "photos", "photography", "humor",
               "comedy", "advice", "shopping", "deals", "sponsored", "events", "newsletters", "quiz"}
ECON_PATH = re.compile(r"/(economy|economics|business|money|markets|finance|labor|labour|work|jobs|housing|real-estate)(/|-|$)", re.I)
POL_PATH = re.compile(r"/(politics|elections?|congress|government|world|us-news|national|courts|justice|immigration)(/|-|$)", re.I)
DROP_PATH = re.compile(r"/(sports?|culture|arts?|books|film|movies|music|tv|television|reviews?|lifestyle|food|recipes|"
                       r"travel|style|fashion|games|puzzles|crosswords?|horoscopes?|celebrity|entertainment|podcasts?|"
                       r"videos?|gallery|galleries|photos?|in-pictures|programmes|program|shopping|deals|sponsored|quiz)(/|-|$)", re.I)

SPORTS = re.compile(r"\b(NFL|NBA|WNBA|MLB|NHL|MLS|NCAA|Premier League|Champions League|La Liga|Serie A|Bundesliga|"
                    r"World Cup|Super Bowl|Stanley Cup|World Series|playoffs?|quarterback|touchdown|home run|"
                    r"Olympics?|Olympian|cricket|rugby|tennis|Wimbledon|golf|PGA|Formula (1|One)|F1|Grand Prix|NASCAR|"
                    r"boxing|UFC|wrestling|coach(es|ed)?|transfer window|striker|midfielder|goalkeeper|batting|innings)\b", re.I)
JUNK = re.compile(r"\b(quiz|crossword|wordle|horoscopes?|recipes?|best .{0,40} (to buy|deals?)|deals? of the|"
                  r"gift guide|shopping|sponsored|giveaway|in pictures|photos? of the|watch:|listen:|podcast:|"
                  r"newsletter|sign up|celebrit\w*|red carpet|box office|album review|film review|tv review|"
                  r"what to watch|streaming guide|weekend reads)\b", re.I)

# Fallback when a feed gives no labels: the headline itself must be about the economy.
ECON = re.compile(
    r"\b(econom\w*|inflation|wages?|paychecks?|jobs report|unemploy\w*|jobless|labou?r market|layoffs?|"
    r"minimum wage|unions?|unioni[sz]\w*|union drive|strik(e|es|ers|ing)|walkouts?|"
    r"rents?|renters|tenants?|evictions?|housing (costs?|crisis|market|prices)|mortgage rates?|home prices|"
    r"tariffs?|trade war|federal reserve|fed(?! up)|interest rates?|rates? (steady|unchanged)|rate (cut|hike)s?|recession|gdp|"
    r"stock market|stocks|wall street|dow|s&p 500|profits?|billionaires?|wealth tax|wealth gap|inequality|poverty|"
    r"prices|price gouging|cost of living|grocery|tax (cuts?|bill|credits?|breaks?)|budget|debt ceiling|deficit|"
    r"private equity|antitrust|monopol\w*|ceo pay|pensions?|social security|medicaid|snap benefits|food stamps|"
    r"supply chains?|oil prices|gas prices|bankrupt\w*|amazon|walmart|starbucks|uber|tesla)\b", re.I)
NOT_LABOR_STRIKE = re.compile(r"(air|drone|missile|military|israeli|russian|us|u\.s\.) ?strikes?|strikes? (on|against|in) ", re.I)
PARTY = re.compile(r"\blabou?r (party|government|leader|leadership|mps?|ministers?|conference|left|right)\b", re.I)


def classify(item):
    """'economy', 'politics' or 'drop'."""
    title = item["title"]
    if SPORTS.search(title) or JUNK.search(title):
        return "drop"
    labels = set(item.get("tags") or [])
    path = item.get("url", "")
    if labels & DROP_LABELS or DROP_PATH.search(path):
        return "drop"
    if labels & ECON_LABELS or ECON_PATH.search(path):
        return "economy"
    if labels & POL_LABELS or POL_PATH.search(path):
        return "politics"
    t = PARTY.sub("", title)
    hits = ECON.findall(t)
    if hits and not (len(hits) == 1 and NOT_LABOR_STRIKE.search(t)):
        return "economy"
    return "politics"


STOP = set("""a an the and or but of to in on at for from by with as is are was were be been it its this that these those
after before over under into about amid against says say said new how why what who will would could can may
than more most not no his her their our your he she they we you i us up out off just now first last week day""".split())


PHRASES = [(r"walk(ed|s|ing)? (off|out)( the job)?|walkouts?", "strike"), (r"\bfive\b", "5"), (r"\bfour\b", "4"),
           (r"\bthree\b", "3"), (r"\btwo\b", "2"), (r"\bu\.s\.|\bunited states\b", "us"), (r"\bgov'?t\b", "government")]
SYN = {"approv": "pass", "approve": "pass", "clear": "pass", "advance": "pass", "leav": "hold", "leave": "hold",
       "keep": "hold", "kept": "hold", "unchang": "steady", "unchanged": "steady", "launch": "start", "begin": "start",
       "began": "start", "kick": "start", "fund": "fund", "funding": "fund", "spend": "fund", "spending": "fund",
       "kill": "kill", "dead": "kill", "die": "kill", "death": "kill", "slay": "kill", "strik": "strike",
       "cut": "cut", "slash": "cut", "rais": "raise", "hik": "raise", "hike": "raise", "boost": "raise"}
GENERIC = set("""trump biden harris president government house senate congress court state states us federal officials
official report reports plan plans bill law sign order executive year years people group news live update updates
deal country world national city leader leaders party week month today""".split())


def _stem(w):
    for suf in ("ies", "ing", "ed", "es", "s"):
        if len(w) > len(suf) + 3 and w.endswith(suf):
            w = w[: -len(suf)] + ("y" if suf == "ies" else "")
            break
    return SYN.get(w, w)


def _tokens(title):
    t = title.lower().replace("’", "'").replace("'s", "")
    for pat, rep_ in PHRASES:
        t = re.sub(pat, rep_, t)
    words = re.findall(r"[a-z0-9]+", t)
    return {_stem(w) for w in words if (len(w) > 2 or w.isdigit()) and w not in STOP}


def merge_similar(items, days=3):
    """Group stories about the same event. Items must already be sorted newest
    first; the first of each group is kept and the rest are listed under
    'also' so the page can say who else covered it."""
    kept, toks = [], []
    for it in items:
        t = _tokens(it["title"])
        dt = it.get("date") or ""
        dup = None
        for i, (k, kt) in enumerate(zip(kept, toks)):
            if not t or not kt:
                continue
            shared = t & kt
            inter = len(shared)
            jacc = inter / len(t | kt)
            contain = inter / min(len(t), len(kt))
            specific = len(shared - GENERIC)
            if specific >= 2 and (jacc >= 0.5 or contain >= 0.6 or (specific >= 3 and contain >= 0.45)):
                if dt and k.get("date") and abs((datetime.fromisoformat(dt.replace("Z", "+00:00")) - datetime.fromisoformat(k["date"].replace("Z", "+00:00"))).days) > days:
                    continue
                dup = i
                break
        if dup is None:
            kept.append(it)
            toks.append(t)
        else:
            k = kept[dup]
            also = k.setdefault("also", [])
            if it["source"] != k["source"] and it["source"] not in also and len(also) < 6:
                also.append(it["source"])
            toks[dup] = toks[dup] | t
    return kept


def cap_per_source(items, cap):
    counts, out = {}, []
    for it in items:
        n = counts.get(it["source"], 0)
        if n < cap:
            counts[it["source"]] = n + 1
            out.append(it)
    return out


def beat_patterns():
    return {name: re.compile(r"\b(" + "|".join(re.escape(w) for w in words) + r")\b", re.I)
            for name, words in (CONFIG.get("beats") or {}).items()}


def rank(items):
    """Tag each story with your beats and give it a score for the 'top stories'
    view: more outlets covering it, a newsroom source, a beat you follow and
    freshness all count. The list itself stays newest first."""
    pats = beat_patterns()
    for it in items:
        text = it["title"] + " " + (it.get("summary") or "")
        beats = [n for n, rx in pats.items() if rx.search(text)]
        if beats:
            it["beats"] = beats[:3]
        try:
            hours = max(0.0, (NOW - datetime.fromisoformat(it["date"].replace("Z", "+00:00"))).total_seconds() / 3600)
        except Exception:  # noqa: BLE001
            hours = 24.0
        score = (1 + 0.9 * len(it.get("also", []))) * (1.15 if it.get("kind", "newsroom") == "newsroom" else 1.0)
        score *= 1.25 if beats else 1.0
        score *= 0.5 ** (hours / 16)
        it["score"] = round(score, 4)
    return items


def assemble_news(sections):
    """Single-subject feeds keep their tab (minus junk); outlets that cover
    everything are sorted article by article. Then every tab is de-duplicated,
    ordered by time, and capped so no outlet can flood it."""
    mixed = build_rss_section("mixed", CONFIG["rss"].get("mixed", []))
    pools = {"economy": [], "politics": []}
    dropped = 0
    for key in ("economy", "politics"):
        for it in sections.get(key, []):
            labels, path = set(it.get("tags") or []), it.get("url", "")
            if SPORTS.search(it["title"]) or JUNK.search(it["title"]) or labels & DROP_LABELS or DROP_PATH.search(path):
                dropped += 1
                continue
            pools[key].append(it)
    for it in mixed:
        where = classify(it)
        if where == "drop":
            dropped += 1
        else:
            pools[where].append(it)
    for key, keep in (("economy", 120), ("politics", 140)):
        merged, seen = [], set()
        for it in sorted(pools[key], key=lambda i: i["date"] or "", reverse=True):
            k = re.sub(r"\W+", "", it["title"].lower())[:80]
            if k not in seen:
                seen.add(k)
                it.pop("tags", None)
                merged.append(it)
        sections[key] = rank(cap_per_source(merge_similar(merged), 6))[:keep]
    print(f"news: {len(sections['economy'])} economy, {len(sections['politics'])} politics, {dropped} non-news dropped")


# ---------- Hog watch: right-wing outlets, kept in their own tab ----------

def hog_watch(items):
    """Same clean-up as the news tabs (no sports or junk, one item per story,
    at most six per outlet), but never mixed with anything else."""
    kept = [i for i in items if not (SPORTS.search(i["title"]) or JUNK.search(i["title"])
                                     or set(i.get("tags") or []) & DROP_LABELS or DROP_PATH.search(i.get("url", "")))]
    kept.sort(key=lambda i: i["date"] or "", reverse=True)
    out = cap_per_source(merge_similar(kept), 6)[:90]
    for i in out:
        i.pop("tags", None)
    print(f"hog watch: {len(out)} items")
    return out


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
    return cap_per_source(merge_similar(out, days=5), 4)[:40]


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


def fred_series(sid, mode, start=None):
    start = start or (NOW - timedelta(days=365 * 12)).date().isoformat()
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
    if mode == "all":
        return rows
    return rows[-120:]


GLOBAL_PRICES = [
    ("POILBREUSDM", "Brent crude oil", "dollars per barrel", "level"),
    ("PFOODINDEXM", "Global food prices", "index, 2016 = 100", "level"),
    ("PWHEAMTUSDM", "Wheat", "dollars per metric ton", "level"),
    ("PCOPPUSDM", "Copper", "dollars per metric ton", "level"),
    ("RTWEXBGS", "Strength of the US dollar", "index, Jan 2006 = 100", "level"),
]


def build_dashboard(series_list=None, label="economy in numbers"):
    out = []
    for sid, label_, unit, mode in (series_list or DASHBOARD):
        label_txt = label_
        try:
            pts = fred_series(sid, mode)
        except Exception as e:  # noqa: BLE001
            failed.append({"source": f"FRED {sid}", "section": label, "error": str(e).split(" for url")[0][:100]})
            continue
        if len(pts) < 2:
            continue
        # change over roughly one year, whatever the series frequency
        per_year = 12 if len(pts) > 60 else 4
        back = pts[-1 - per_year] if len(pts) > per_year else pts[0]
        out.append({"id": sid, "label": label_txt, "unit": unit, "latest": pts[-1][1], "date": pts[-1][0],
                    "change": round(pts[-1][1] - back[1], 2), "since": back[0], "points": pts,
                    "url": f"https://fred.stlouisfed.org/series/{sid}"})
        time.sleep(0.3)
    print(f"{label}: {len(out)} series")
    return out


# ---------- Profits vs. wages (FRED, back to 1947) ----------

def build_shares():
    try:
        gdp = dict(fred_series("GDP", "all", "1947-01-01"))
        cp = fred_series("CP", "all", "1947-01-01")
        try:
            coe = fred_series("COE", "all", "1947-01-01")
        except Exception:  # noqa: BLE001
            coe = []
    except Exception as e:  # noqa: BLE001
        failed.append({"source": "FRED profits and wages", "section": "profits vs. wages", "error": str(e).split(" for url")[0][:100]})
        return None
    share = lambda rows: [(d, round(v / gdp[d] * 100, 2)) for d, v in rows if gdp.get(d)]
    out = {"profits": share(cp), "wages": share(coe)}
    if not out["wages"]:
        try:   # annual fallback: compensation's share of gross domestic income
            out["wages"] = [(d, round(v, 2)) for d, v in fred_series("W270RE1A156NBEA", "all", "1947-01-01")]
            out["wages_note"] = "share of gross domestic income"
        except Exception:  # noqa: BLE001
            pass
    print(f"profits vs. wages: {len(out['profits'])} and {len(out['wages'])} points")
    return out


# ---------- World map and the debt squeeze (World Bank) ----------

MAP_INDICATORS = {
    "NY.GDP.MKTP.KD.ZG": "GDP growth, %",
    "FP.CPI.TOTL.ZG": "Inflation, %",
    "SL.UEM.TOTL.ZS": "Unemployment, %",
    "SI.POV.GINI": "Inequality (Gini index)",
}


def wb_all(code, countries="all", first=None):
    d = get_patiently(f"https://api.worldbank.org/v2/country/{countries}/indicator/{code}", tries=3,
                      format="json", per_page=20000, date=f"{first or NOW.year - 12}:{NOW.year}").json()
    return d[1] if isinstance(d, list) and len(d) > 1 and d[1] else []


def build_world_map():
    out = {}
    for code, label in MAP_INDICATORS.items():
        try:
            rows = wb_all(code)
        except Exception as e:  # noqa: BLE001
            failed.append({"source": f"World Bank {code}", "section": "world map", "error": str(e).split(" for url")[0][:100]})
            continue
        latest = {}
        for o in rows:
            cc, v = o.get("countryiso3code"), o.get("value")
            if cc and v is not None and (cc not in latest or o["date"] > latest[cc][1]):
                latest[cc] = (round(v, 1), o["date"], (o.get("country") or {}).get("value", cc))
        out[code] = {"label": label, "values": latest}
        time.sleep(0.3)
    print(f"world map: {len(out)} indicators")
    return out or None


DEBT_GROUPS = {"LIC": "Low-income countries", "LMC": "Lower-middle-income countries"}
DEBT_COUNTRIES = ["PAK", "EGY", "KEN", "GHA", "ZMB", "LKA", "ARG", "NGA", "ETH", "TUN", "BGD", "COL"]


def build_debt():
    out = {"groups": {}, "countries": []}
    try:
        rows = wb_all("DT.TDS.DECT.EX.ZS", ";".join(DEBT_GROUPS), 2000)
        for o in rows:
            if o.get("value") is not None:
                out["groups"].setdefault(o["countryiso3code"], []).append((o["date"], round(o["value"], 1)))
        for k in out["groups"]:
            out["groups"][k].sort()
        out["group_names"] = DEBT_GROUPS
    except Exception as e:  # noqa: BLE001
        failed.append({"source": "World Bank debt service", "section": "debt squeeze", "error": str(e).split(" for url")[0][:100]})
    try:
        rows = wb_all("GC.XPN.INTP.RV.ZS", ";".join(DEBT_COUNTRIES), NOW.year - 8)
        latest = {}
        for o in rows:
            cc, v = o.get("countryiso3code"), o.get("value")
            if v is not None and (cc not in latest or o["date"] > latest[cc]["year"]):
                latest[cc] = {"code": cc, "name": (o.get("country") or {}).get("value", cc), "value": round(v, 1), "year": o["date"]}
        out["countries"] = sorted(latest.values(), key=lambda x: -x["value"])
    except Exception as e:  # noqa: BLE001
        failed.append({"source": "World Bank interest payments", "section": "debt squeeze", "error": str(e).split(" for url")[0][:100]})
    print(f"debt squeeze: {len(out['groups'])} groups, {len(out['countries'])} countries")
    return out if (out["groups"] or out["countries"]) else None


# ---------- Text of the week: check the links still work ----------

def check_texts():
    data = ROOT / "site" / "data"
    texts = []
    for name in ("texts.json", "library.json"):
        if (data / name).exists():
            d = json.loads((data / name).read_text())
            texts += d if isinstance(d, list) else [t for sch in d.get("schools", []) for t in sch["texts"] if t.get("url")]
    broken = []
    seen = set()
    for t in texts:
        if t["url"] in seen:
            continue
        seen.add(t["url"])
        try:
            r = SESSION.get(t["url"], timeout=20, headers=FEED_HEADERS, stream=True)
            r.close()
            if r.status_code >= 400:
                broken.append(t["title"])
        except Exception:  # noqa: BLE001
            broken.append(t["title"])
    return broken


# ---------- World economy (World Bank, no key) ----------

def build_world():
    cfg = CONFIG.get("world_economy") or {}
    countries, inds = cfg.get("countries", []), cfg.get("indicators", {})
    if not countries:
        return None
    first = NOW.year - 12
    rows = {cc: {"code": cc, "name": cc, "values": {}} for cc in countries}
    got = False
    for code in inds:
        try:
            d = get_patiently(f"https://api.worldbank.org/v2/country/{';'.join(countries)}/indicator/{code}", tries=3,
                              format="json", per_page=2000, date=f"{first}:{NOW.year}").json()
        except Exception as e:  # noqa: BLE001
            failed.append({"source": f"World Bank {code}", "section": "world economy", "error": str(e).split(" for url")[0][:100]})
            continue
        if not isinstance(d, list) or len(d) < 2 or not d[1]:
            continue
        series = {}
        for obs in d[1]:
            cc = obs.get("countryiso3code")
            if cc in rows:
                rows[cc]["name"] = (obs.get("country") or {}).get("value", cc)
                if obs.get("value") is not None:
                    series.setdefault(cc, []).append((obs["date"], round(obs["value"], 2)))
        for cc, pts in series.items():
            pts.sort()
            rows[cc]["values"][code] = {"latest": pts[-1][1], "year": pts[-1][0], "points": pts}
            got = True
        time.sleep(0.3)
    if not got:
        return None
    print(f"world economy: {len(rows)} countries")
    return {"indicators": inds, "rows": [rows[cc] for cc in countries]}


# ---------- Release calendar (BLS) ----------

def build_calendar():
    url = CONFIG.get("bls_calendar")
    if not url:
        return []
    try:
        text = SESSION.get(url, timeout=25, headers={"User-Agent": API_UA}).text
    except Exception as e:  # noqa: BLE001
        failed.append({"source": "BLS release calendar", "section": "official releases", "error": str(e)[:100]})
        return []
    events, cur = [], {}
    for line in text.replace("\r\n ", "").splitlines():
        if line.startswith("BEGIN:VEVENT"):
            cur = {}
        elif line.startswith("SUMMARY"):
            cur["title"] = line.split(":", 1)[1].strip().replace("\\,", ",")
        elif line.startswith("DTSTART"):
            v = line.split(":", 1)[1].strip()
            try:
                dt = datetime.strptime(v[:15], "%Y%m%dT%H%M%S") if "T" in v else datetime.strptime(v[:8], "%Y%m%d")
                cur["date"] = dt.strftime("%Y-%m-%dT%H:%M:%S")
            except ValueError:
                pass
        elif line.startswith("END:VEVENT") and cur.get("title") and cur.get("date"):
            events.append(cur)
    today = NOW.strftime("%Y-%m-%d")
    horizon = (NOW + timedelta(days=21)).strftime("%Y-%m-%d")
    upcoming = sorted([e for e in events if today <= e["date"][:10] <= horizon], key=lambda e: e["date"])
    if not events:
        failed.append({"source": "BLS release calendar", "section": "official releases", "error": "no events found (BLS may have refused the request)"})
    return upcoming[:20]


# ---------- Posters (DPLA: MSU's leftist and radicalism poster collections) ----------

FREE_RIGHTS = re.compile(r"(NKC|NoC-|publicdomain|/mark/1\.0|zero/1\.0)", re.I)


def build_posters():
    key = os.environ.get("DPLA_API_KEY", "").strip()
    cfg = CONFIG.get("posters") or {}
    if not key:
        failed.append({"source": "DPLA posters", "section": "posters", "error": "add a free DPLA_API_KEY secret (see README)"})
        return []
    out = []
    for coll in cfg.get("collections", []):
        page = 1
        while page <= 6:
            try:
                d = get_patiently("https://api.dp.la/v2/items", tries=3, api_key=key, page_size=100, page=page,
                                  **{"sourceResource.collection.title": f'"{coll}"'}).json()
            except Exception as e:  # noqa: BLE001
                failed.append({"source": f"DPLA: {coll}", "section": "posters", "error": str(e).split(" for url")[0][:100]})
                break
            docs = d.get("docs") or []
            for doc in docs:
                sr = doc.get("sourceResource") or {}
                rights = " ".join(sr.get("rights") if isinstance(sr.get("rights"), list) else [sr.get("rights") or ""]) + " " + str(doc.get("rights") or "")
                img = doc.get("object")
                if not img or not FREE_RIGHTS.search(rights):
                    continue
                title = sr.get("title"); title = title[0] if isinstance(title, list) else title
                desc = sr.get("description"); desc = desc[0] if isinstance(desc, list) else desc
                date = (sr.get("date") or {}); date = date[0] if isinstance(date, list) else date
                place = sr.get("spatial") or []
                out.append({
                    "title": clean(title, 160) or "Untitled poster",
                    "date": (date or {}).get("displayDate", ""),
                    "description": clean(desc, 300),
                    "place": ", ".join(p.get("name", "") for p in place[:2] if isinstance(p, dict)),
                    "image": img, "url": doc.get("isShownAt"), "collection": coll,
                })
            if len(docs) < 100:
                break
            page += 1
            time.sleep(0.3)
    print(f"posters: {len(out)} free-to-use")
    return out


# ---------- Your shelf: StoryGraph export + the canon ----------

def norm_title(t):
    t = (t or "").lower().split(":")[0]
    t = re.sub(r"\(.*?\)", "", t)
    t = re.sub(r"^(the|a|an)\s+", "", t.strip())
    return re.sub(r"[^a-z0-9]+", "", t)


def norm_full(t):
    t = re.sub(r"\(.*?\)", "", (t or "").lower())
    t = re.sub(r"^(the|a|an)\s+", "", t.strip())
    return re.sub(r"[^a-z0-9]+", "", t)


def last_name(a):
    a = (a or "").split(",")[0].strip()
    return re.sub(r"[^a-z]", "", a.split(" ")[-1].lower()) if a else ""


def build_shelf():
    import csv
    books_dir = ROOT / "books"
    files = sorted(books_dir.glob("*.csv")) if books_dir.exists() else []
    canon_path = ROOT / "site" / "data" / "canon.json"
    if not files:
        return None
    path = books_dir / "storygraph.csv" if (books_dir / "storygraph.csv").exists() else files[-1]
    with open(path, newline="", encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    def isbn(r):
        v = re.sub(r"[^0-9Xx]", "", r.get("ISBN/UID") or "")
        return v if len(v) in (10, 13) else None
    shelf = {"current": [], "stats": {}, "recs": [], "canon_read": 0, "canon_total": 0}
    read, toread, current = [], [], []
    for r in rows:
        st = (r.get("Read Status") or "").strip()
        b = {"title": (r.get("Title") or "").strip(), "author": (r.get("Authors") or "").split(",")[0].strip(),
             "isbn": isbn(r), "date": (r.get("Last Date Read") or "").strip()}
        {"read": read, "to-read": toread, "currently-reading": current}.get(st, []).append(b)
    shelf["current"] = current
    years = {}
    for b in read:
        y = b["date"][:4]
        if y.isdigit():
            years[y] = years.get(y, 0) + 1
    authors = {}
    for b in read:
        if b["author"]:
            authors[b["author"]] = authors.get(b["author"], 0) + 1
    shelf["stats"] = {"read": len(read), "to_read": len(toread), "this_year": years.get(str(NOW.year), 0),
                      "by_year": dict(sorted(years.items())[-8:]),
                      "top_authors": sorted(authors.items(), key=lambda x: -x[1])[:6]}
    if canon_path.exists():
        canon = json.loads(canon_path.read_text())
        key = lambda t, a: (norm_title(t), last_name(a))
        read_t = {norm_title(b["title"]) for b in read}
        read_k = {key(b["title"], b["author"]) for b in read}
        cur_t = {norm_title(b["title"]) for b in current}
        to_t = {norm_title(b["title"]) for b in toread}
        read_authors = {last_name(b["author"]) for b in read}
        read_full = {norm_full(b["title"]) for b in read}
        def is_read(w):
            nf, nt, la_w = norm_full(w["title"]), norm_title(w["title"]), last_name(w["author"])
            if any(f == nf or (len(nf) > 10 and f.startswith(nf)) for f in read_full):
                return True
            return any(t == nt and la == la_w for t, la in read_k)
        read_works = [w for w in canon if is_read(w)]
        topic_weight = {}
        for w in read_works:
            for tp in w.get("topics", []):
                topic_weight[tp] = topic_weight.get(tp, 0) + 1
        title_of = {norm_full(b["title"]): b["title"] for b in read + current}
        recs = []
        for w in canon:
            nt = norm_title(w["title"])
            if is_read(w) or nt in cur_t:
                continue
            score, because = 0.0, []
            for a in w.get("after", []):
                na = norm_full(a)
                hit = next((t for t in title_of if t == na or (len(na) > 5 and t.startswith(na))), None)
                if hit:
                    score += 4
                    because.append(title_of[hit])
            score += min(3, sum(min(1.0, topic_weight.get(tp, 0) / 3) for tp in w.get("topics", [])))
            if last_name(w["author"]) in read_authors:
                score += 1.5
            on_list = nt in to_t
            if on_list:
                score += 1.5
            recs.append({**{k: w[k] for k in ("title", "author", "year", "shelf", "why", "venue") if k in w},
                         "score": round(score, 2), "because": because[:2], "on_list": on_list})
        recs.sort(key=lambda r: -r["score"])
        per_shelf = {}
        for r in recs:
            per_shelf.setdefault(r["shelf"], [])
            if len(per_shelf[r["shelf"]]) < 8:
                per_shelf[r["shelf"]].append(r)
        shelf["recs"] = per_shelf
        shelf["canon_read"], shelf["canon_total"] = len(read_works), len(canon)
        lib_path = ROOT / "site" / "data" / "library.json"
        if lib_path.exists():
            lib = json.loads(lib_path.read_text())
            def lib_read(t):
                return is_read(t) or any(is_read({**t, "title": a}) for a in t.get("aka", []))
            shelf["library_read"] = [t["title"] for sch in lib.get("schools", []) for t in sch["texts"] if lib_read(t)]
            to_full = {norm_full(b["title"]) for b in toread}
            shelf["library_toread"] = [t["title"] for sch in lib.get("schools", []) for t in sch["texts"]
                                       if t["title"] not in shelf["library_read"]
                                       and any(norm_full(x) in to_full or norm_title(x) in to_t for x in [t["title"], *t.get("aka", [])])]
        shelf["canon_read_by_shelf"] = {s: sum(1 for w in read_works if w["shelf"] == s) for s in {w["shelf"] for w in canon}}
        shelf["canon_total_by_shelf"] = {s: sum(1 for w in canon if w["shelf"] == s) for s in {w["shelf"] for w in canon}}
    print(f"shelf: {len(current)} current, {len(read)} read, {shelf['canon_read']}/{shelf['canon_total']} of the canon")
    return shelf


# ---------- Scholars you follow and debates you track (OpenAlex) ----------

def _oa(path, **params):
    if OPENALEX_KEY:
        params["api_key"] = OPENALEX_KEY
    if EMAIL:
        params["mailto"] = EMAIL
    return get_patiently("https://api.openalex.org/" + path, tries=3, **params).json()


def _fold(t):
    import unicodedata
    return re.sub(r"[^a-z ]", "", unicodedata.normalize("NFKD", t).encode("ascii", "ignore").decode().lower()).strip()


def _work_item(w, **extra):
    loc = (w.get("primary_location") or {})
    venue = ((loc.get("source") or {}).get("display_name")) or "Preprint or other venue"
    return {"title": clean(w.get("display_name"), 240), "url": w.get("doi") or loc.get("landing_page_url") or w.get("id"),
            "source": venue, "date": (w.get("publication_date") or "") + "T00:00:00Z",
            "authors": [a["author"]["display_name"] for a in w.get("authorships", [])[:4] if a.get("author")],
            "summary": clean(rebuild_abstract(w.get("abstract_inverted_index"))), **extra}


def build_scholars():
    names = CONFIG.get("scholars") or []
    if not names:
        return []
    ids = {}
    for n in names:
        try:
            res = _oa("authors", search=n, per_page=10).get("results", [])
        except Exception as e:  # noqa: BLE001
            failed.append({"source": f"OpenAlex: {n}", "section": "scholars", "error": str(e).split(" for url")[0][:80]})
            continue
        exact = [a for a in res if _fold(a.get("display_name", "")) == _fold(n)] or res[:1]
        if exact:
            best = max(exact, key=lambda a: a.get("cited_by_count", 0))
            ids[best["id"].rsplit("/", 1)[-1]] = n
        time.sleep(0.2)
    if not ids:
        return []
    since = (NOW - timedelta(days=540)).date().isoformat()
    try:
        res = _oa("works", filter=f"authorships.author.id:{'|'.join(ids)},from_publication_date:{since},to_publication_date:{NOW.date().isoformat()}",
                  sort="publication_date:desc", per_page=60).get("results", [])
    except Exception as e:  # noqa: BLE001
        failed.append({"source": "OpenAlex: scholars", "section": "scholars", "error": str(e).split(" for url")[0][:80]})
        return []
    out, seen = [], set()
    for w in res:
        who = [ids[a["author"]["id"].rsplit("/", 1)[-1]] for a in w.get("authorships", []) if a.get("author") and a["author"].get("id", "").rsplit("/", 1)[-1] in ids]
        key = _fold(w.get("display_name") or "")[:80]
        if who and key and key not in seen:
            seen.add(key)
            out.append(_work_item(w, scholar=who[0]))
    print(f"scholars: {len(ids)} found, {len(out)} new works")
    return out[:40]


def build_debates():
    out = []
    since = (NOW - timedelta(days=365)).date().isoformat()
    for d in CONFIG.get("debates") or []:
        try:
            res = _oa("works", search=d["title"], per_page=15).get("results", [])
            want = _fold(d["title"])[:30]
            cands = [w for w in res if _fold(w.get("display_name") or "").startswith(want[:20])
                     and any(_fold(d["author"]) in _fold(a["author"]["display_name"]) for a in w.get("authorships", []) if a.get("author"))]
            if not cands:
                failed.append({"source": f"Debate: {d['label']}", "section": "debates", "error": "couldn't find the work on OpenAlex"})
                continue
            target = max(cands, key=lambda w: w.get("cited_by_count", 0))
            wid = target["id"].rsplit("/", 1)[-1]
            cites = _oa("works", filter=f"cites:{wid},from_publication_date:{since},to_publication_date:{NOW.date().isoformat()}",
                        sort="publication_date:desc", per_page=6).get("results", [])
            out.append({"label": d["label"], "cited_by": target.get("cited_by_count", 0),
                        "items": [_work_item(w) for w in cites if w.get("display_name")]})
        except Exception as e:  # noqa: BLE001
            failed.append({"source": f"Debate: {d['label']}", "section": "debates", "error": str(e).split(" for url")[0][:80]})
        time.sleep(0.3)
    print(f"debates: {len(out)} tracked")
    return out


# ---------- Markets & indicators board (FRED, no key) ----------

INDICATORS = [
    ("currencies", "DEXUSEU", "Euro", "dollars per euro", "d"),
    ("currencies", "DEXUSUK", "British pound", "dollars per pound", "d"),
    ("currencies", "DEXJPUS", "Japanese yen", "yen per dollar", "d"),
    ("currencies", "DEXCHUS", "Chinese yuan", "yuan per dollar", "d"),
    ("currencies", "DEXINUS", "Indian rupee", "rupees per dollar", "d"),
    ("currencies", "DEXBZUS", "Brazilian real", "reais per dollar", "d"),
    ("currencies", "DEXMXUS", "Mexican peso", "pesos per dollar", "d"),
    ("currencies", "RTWEXBGS", "Dollar against all trading partners", "index, Jan 2006 = 100", "m"),
    ("commodities", "POILBREUSDM", "Brent crude oil", "dollars per barrel", "m"),
    ("commodities", "MHHNGSP", "US natural gas (Henry Hub)", "dollars per million BTU", "m"),
    ("commodities", "PFOODINDEXM", "Global food prices", "index, 2016 = 100", "m"),
    ("commodities", "PWHEAMTUSDM", "Wheat", "dollars per metric ton", "m"),
    ("commodities", "PMAIZMTUSDM", "Corn", "dollars per metric ton", "m"),
    ("commodities", "PCOPPUSDM", "Copper", "dollars per metric ton", "m"),
    ("markets", "SP500", "S&P 500 stock index", "index", "d"),
    ("markets", "DGS10", "10-year Treasury yield", "%", "d"),
    ("markets", "T10Y2Y", "Yield curve (10-year minus 2-year)", "points", "d"),
    ("markets", "FEDFUNDS", "Federal funds rate", "%", "m"),
    ("markets", "MORTGAGE30US", "30-year mortgage rate", "%", "w"),
    ("us economy", "A191RL1Q225SBEA", "Real GDP growth (annual rate)", "%", "q"),
    ("us economy", "UNRATE", "Unemployment rate", "%", "m"),
    ("us economy", "CPIAUCSL", "Consumer prices", "index", "m"),
    ("us economy", "PAYEMS", "Jobs (nonfarm payrolls)", "thousands", "m"),
    ("us economy", "CSUSHPINSA", "Home prices (Case-Shiller)", "index", "m"),
    ("inequality", "WFRBST01134", "Wealth share of the top 1%", "%", "q"),
    ("inequality", "WFRBSB50215", "Wealth share of the bottom 50%", "%", "q"),
    ("inequality", "GINIALLRF", "US income inequality (Gini)", "index", "a"),
    ("inequality", "LES1252881600Q", "Median weekly pay, adjusted for inflation", "1982–84 dollars", "q"),
]


def build_indicators():
    out = []
    for group, sid, label, unit, freq in INDICATORS:
        years = 3 if freq == "d" else 6 if freq in ("w", "m") else 15
        try:
            pts = fred_series(sid, "all", (NOW - timedelta(days=365 * years)).date().isoformat())
        except Exception as e:  # noqa: BLE001
            failed.append({"source": f"FRED {sid}", "section": "markets & indicators", "error": str(e).split(" for url")[0][:80]})
            continue
        if len(pts) < 3:
            continue
        last_d, last_v = pts[-1]
        ld = datetime.fromisoformat(last_d)
        def ago(days):
            target = (ld - timedelta(days=days)).date().isoformat()
            older = [v for d, v in pts if d <= target]
            return older[-1] if older else None
        m1, y1 = ago(30), ago(365)
        step = max(1, len(pts) // 60)
        spark = pts[::step][-60:]
        if spark[-1] != pts[-1]:
            spark.append(pts[-1])
        out.append({"group": group, "id": sid, "label": label, "unit": unit, "date": last_d, "latest": last_v,
                    "m1": None if m1 is None or freq in ("q", "a") else round(last_v - m1, 4),
                    "y1": None if y1 is None else round(last_v - y1, 4),
                    "y1pct": None if not y1 else round((last_v / y1 - 1) * 100, 2),
                    "spark": [v for _, v in spark], "url": f"https://fred.stlouisfed.org/series/{sid}"})
        time.sleep(0.25)
    print(f"indicators: {len(out)} of {len(INDICATORS)}")
    return out


# ---------- Back issues ----------

def save_back_issue(payload):
    """Keep a slim copy of each edition (headlines only) in site/archive,
    which the workflow commits so the history survives between runs."""
    arch = ROOT / "site" / "archive"
    arch.mkdir(parents=True, exist_ok=True)
    keep = ("economy", "politics", "essays", "local", "hog", "strikes", "journals", "papers", "scholarship")
    slim = {"generated_at": payload["generated_at"], "sections": {
        k: [{f: it[f] for f in ("title", "url", "source", "date", "authors", "kind") if f in it} for it in payload["sections"].get(k, [])[:25]]
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


def safe(name, fn, default=None):
    """Run one part of the edition; if it breaks, report it and carry on."""
    try:
        return fn()
    except Exception as e:  # noqa: BLE001
        import traceback
        traceback.print_exc()
        failed.append({"source": name, "section": "updater", "error": f"{type(e).__name__}: {str(e)[:100]}"})
        return default


def main():
    sections = {}
    for name, sources in CONFIG["rss"].items():
        if name in ("local", "mixed"):
            continue
        sections[name] = build_rss_section(name, sources)
        print(f"{name}: {len(sections[name])} items")

    safe("news sorting", lambda: assemble_news(sections))

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

    sections["local"] = [{k: v for k, v in i.items() if k != "tags"} for i in safe("local news", build_local, [])]
    sections["hog"] = safe("hog watch", lambda: hog_watch(sections.get("hog", [])), [])
    print(f"local: {len(sections['local'])} items")
    sections["strikes"] = safe("strike tracker", lambda: build_strikes(sections), [])
    print(f"strikes: {len(sections['strikes'])} items")
    sections["museums"] = safe("museums", gather_museums, [])
    images = safe("wikipedia images", gather_wiki_images, {"figures": {}, "art": {}})
    images["angel"] = safe("Angelus Novus image", lambda: wiki_image("Angelus Novus"))

    for items in sections.values():
        for i in items:
            i.pop("tags", None)
    world = safe("world economy", build_world)
    calendar = safe("release calendar", build_calendar, [])
    posters = safe("posters", build_posters, [])
    shelf = safe("your shelf", build_shelf)
    dashboard = safe("economy in numbers", build_dashboard, [])
    indicators = safe("markets & indicators", build_indicators, [])
    sections["scholars"] = safe("scholars", build_scholars, [])
    debates = safe("debates", build_debates, [])
    prices = safe("global prices", lambda: build_dashboard(GLOBAL_PRICES, "global prices"), [])
    shares = safe("profits vs. wages", build_shares)
    world_map = safe("world map", build_world_map)
    debt = safe("debt squeeze", build_debt)
    broken_texts = safe("text links", check_texts, [])

    payload = {"generated_at": iso(NOW), "sections": sections, "images": images,
               "dashboard": dashboard, "broken_texts": broken_texts,
               "world": world, "calendar": calendar, "shelf": shelf,
               "prices": prices, "shares": shares, "debt": debt, "indicators": indicators, "debates": debates,
               "local_place": CONFIG.get("local_place", "Local"), "failed": failed}

    # If everything failed (network outage), keep the last good file.
    if not any(v for k, v in sections.items() if k != "museums") and OUT.exists():
        print("Nothing fetched; keeping previous feed.json", file=sys.stderr)
        return
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=1))
    # posters go in their own file: the page picks a poster of the day from the whole set
    (OUT.parent / "posters.json").write_text(json.dumps(posters, ensure_ascii=False))
    # the map data is large-ish and only needed on one page
    (OUT.parent / "worldmap.json").write_text(json.dumps(world_map or {}, ensure_ascii=False, separators=(",", ":")))
    safe("back issues", lambda: save_back_issue(payload))
    if failed:
        print(f"{len(failed)} sources failed:", *[f"  {f['source']}: {f['error']}" for f in failed], sep="\n")


if __name__ == "__main__":
    main()
