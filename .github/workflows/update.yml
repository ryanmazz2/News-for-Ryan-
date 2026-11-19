#!/usr/bin/env python3
"""Gather news feeds, journal tables of contents and recent scholarship
into site/data/feed.json. Run by GitHub Actions twice a day; safe to run
locally too:  pip install feedparser requests && python scripts/update.py
"""
import html
import json
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

UA = f"RedWireDigest/1.0 (personal news digest; mailto:{CONFIG.get('contact_email', '')})"
SESSION = requests.Session()
SESSION.headers["User-Agent"] = UA

# How far back each section looks, and how many items it keeps.
WINDOWS = {"economy": (4, 45), "politics": (3, 45), "left": (14, 45)}
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


def get(url, **params):
    r = SESSION.get(url, params=params or None, timeout=25)
    r.raise_for_status()
    return r


# ---------- RSS / Atom ----------

def fetch_rss(section, src):
    try:
        feed = feedparser.parse(get(src["url"]).content)
        if feed.bozo and not feed.entries:
            raise ValueError("unreadable feed")
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
        data = get(
            f"https://api.crossref.org/journals/{j['issn']}/works",
            sort="published", order="desc", rows=25,
            filter=f"until-pub-date:{NOW.date().isoformat()}",
            mailto=CONFIG.get("contact_email", ""),
        ).json()
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


def fetch_openalex(term, since):
    try:
        data = get(
            "https://api.openalex.org/works",
            search=term,
            filter=f"from_publication_date:{since},to_publication_date:{NOW.date().isoformat()},type:article",
            sort="publication_date:desc",
            per_page=30,
            mailto=CONFIG.get("contact_email", ""),
        ).json()
    except Exception as e:  # noqa: BLE001
        failed.append({"source": f"OpenAlex: {term}", "section": "scholarship", "error": str(e)[:120]})
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
            "topic": term,
        })
    return out


def main():
    sections = {}
    for name, sources in CONFIG["rss"].items():
        sections[name] = build_rss_section(name, sources)
        print(f"{name}: {len(sections[name])} items")

    with ThreadPoolExecutor(4) as pool:
        journal_batches = list(pool.map(fetch_journal, CONFIG["journals"]))
    journals = [i for b in journal_batches for i in b]
    journals.sort(key=lambda i: i["date"] or "", reverse=True)
    sections["journals"] = journals
    print(f"journals: {len(journals)} items")

    sch = CONFIG.get("scholarship", {})
    since = (NOW - timedelta(days=sch.get("days_back", 120))).date().isoformat()
    journal_titles = {re.sub(r"\W+", "", i["title"].lower()) for i in journals}
    seen, scholarship = set(), []
    for term in sch.get("terms", []):
        for item in fetch_openalex(term, since):
            key = re.sub(r"\W+", "", item["title"].lower())
            if key in seen or key in journal_titles:
                continue
            seen.add(key)
            scholarship.append(item)
        time.sleep(0.3)
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
