"""Build the research group dashboard.

Fetches new papers (Europe PMC), news (RSS), the group's own publications
(OpenAlex, cross-checked against PubMed via Europe PMC) and the boxes in the
right column (Protein Spotlight, target of the day, Our World in Data insight,
PDB Molecule of the Month), then writes site/index.html with the data embedded.

Usage:
    python scripts/build.py                             # live build (used by GitHub Actions)
    python scripts/build.py --quiet                     # same, only print the summary
    python scripts/build.py --sample sample_data.json   # offline example page

See docs/HOW_IT_WORKS.md for a walk-through of the steps and functions.
"""
from __future__ import annotations

import argparse
import datetime as dt
import html
import json
import os
import pathlib
import random
import re
import sys
import time
from collections import Counter

import feedparser
import requests
import yaml

# Never crash on characters the terminal can't show (e.g. Git Bash on Windows)
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(errors="replace")
    except Exception:
        pass

ROOT = pathlib.Path(__file__).resolve().parent.parent
EPMC = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"
CHEMBL = "https://www.ebi.ac.uk/chembl/api/data"
UNIPROT = "https://rest.uniprot.org/uniprotkb"
UA = {"User-Agent": "research-group-dashboard/0.4 (GitHub Actions)"}
TODAY = dt.date.today()
STATUS: list[dict] = []
T0 = time.time()
VERBOSE = True  # switched off with --quiet


# ====================================================================
# Terminal messages
# ====================================================================

def say(msg: str = "", end: str = "\n") -> None:
    """Always printed (used for the final summary)."""
    print(msg, end=end, flush=True)


def log(msg: str = "", end: str = "\n") -> None:
    """Progress message, hidden with --quiet."""
    if VERBOSE:
        print(msg, end=end, flush=True)


def progress(msg: str) -> None:
    """A counter that updates in place (only in a real terminal, not in GitHub's log)."""
    if VERBOSE and sys.stdout.isatty():
        print("\r" + msg, end="", flush=True)


def progress_done(msg: str = "") -> None:
    """Finish a progress counter with a final line."""
    if VERBOSE:
        if sys.stdout.isatty():
            print("\r" + msg + " " * 12 if msg else "", flush=True)
        elif msg:
            print(msg, flush=True)


def step(i: int, n: int, title: str) -> None:
    log(f"\n[{i}/{n}] {title}")


# Formatting tags that titles may keep (italic gene/species names, superscripts, ...)
KEEP_TAGS = "i|em|b|strong|sup|sub"


def clean_title(s: str | None) -> str:
    """Tidy a title from a feed or database: decode entities like &amp;, keep simple
    formatting tags (<i>, <sup>, ...) in a plain form, and remove all other tags."""
    s = html.unescape(s or "")
    s = re.sub(rf"<\s*(/?)\s*({KEEP_TAGS})\b[^>]*>", lambda m: f"<{m.group(1)}{m.group(2).lower()}>", s, flags=re.I)
    s = re.sub(rf"<(?!/?(?:{KEEP_TAGS})>)/?[a-zA-Z][^>]*>", "", s)
    return " ".join(s.split())


def plain(s: str | None) -> str:
    """A title without any tags (for the terminal, matching and filters)."""
    return " ".join(re.sub(r"</?[a-zA-Z][^>]*>", "", s or "").split())


def short(s: str, n: int = 60) -> str:
    s = plain(str(s))
    return s if len(s) <= n else s[: n - 3] + "..."


def ok(name: str, count: int, label: str | None = None, detail: str | None = None) -> None:
    STATUS.append({"name": name, "ok": True, "count": count})
    log(f"    ok  {short(label or name, 34):<34}  {detail if detail is not None else plural(count, 'item')}")


def redact(text: str) -> str:
    """Hide API keys in error messages (they end up in the terminal and on the public page)."""
    return re.sub(r"(api_key=)[^&\s]+", r"\1***", str(text))


def fail(name: str, err: Exception | str, label: str | None = None) -> None:
    err = redact(err)
    STATUS.append({"name": name, "ok": False, "error": err[:140]})
    log(f"    !!  {short(label or name, 34):<34}  {short(err, 90)}")


def plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def get(url: str, **kw) -> requests.Response:
    """GET with a few polite retries when a server is busy (HTTP 429/5xx)."""
    for attempt in range(4):
        r = requests.get(url, headers=UA, timeout=40, **kw)
        if r.status_code in (429, 500, 502, 503, 504) and attempt < 3:
            try:
                wait = int(r.headers.get("Retry-After", "") or 0) or 5 * (attempt + 1)
            except ValueError:
                wait = 5 * (attempt + 1)
            log(f"        server busy (HTTP {r.status_code}), waiting {min(wait, 60)} s and retrying")
            time.sleep(min(wait, 60))
            continue
        r.raise_for_status()
        return r
    r.raise_for_status()
    return r


# ====================================================================
# Europe PMC
# ====================================================================

def epmc_search(query: str, page_size: int = 100) -> list[dict]:
    params = {"query": query, "format": "json", "pageSize": min(page_size, 1000),
              "resultType": "lite", "sort": "P_PDATE_D desc"}
    return get(EPMC, params=params).json().get("resultList", {}).get("result", [])


def norm_journal(s: str | None) -> str:
    s = (s or "").strip().rstrip(".").lower()
    return s[4:] if s.startswith("the ") else s


def term(t: str) -> str:
    t = str(t).replace('"', "")
    return f'"{t}"' if re.search(r"[\s\-/()]", t) else t


def build_query(tab: dict, since: str, journal_clause: str | None) -> str:
    if tab.get("query"):
        q = f"({tab['query']})"
    else:
        fields = tab.get("fields") or ["TITLE", "ABSTRACT"]
        groups = []
        for grp in tab["all_of"]:
            groups.append("(" + " OR ".join(f"{f}:{term(t)}" for t in grp for f in fields) + ")")
        q = " AND ".join(groups)
        for t in tab.get("none_of") or []:
            q += " AND NOT (" + " OR ".join(f"{f}:{term(t)}" for f in fields) + ")"
    q += " AND (SRC:MED OR SRC:PPR)"
    if journal_clause:
        q += f" AND ({journal_clause})"
    return q + f" AND FIRST_PDATE:[{since} TO {TODAY.isoformat()}]"


def short_authors(s: str | None, n: int = 1) -> str:
    names = [a.strip() for a in (s or "").rstrip(".").split(",") if a.strip()]
    if not names:
        return ""
    if len(names) <= n + 1:
        return ", ".join(names)
    return ", ".join(names[:n]) + (f", …, {names[-1]}" if n > 1 else " et al.")


def to_paper(rec: dict, tiers: dict[str, str]) -> dict:
    src, rid, doi = rec.get("source", ""), rec.get("id", ""), rec.get("doi")
    journal = rec.get("journalTitle") or ("bioRxiv/medRxiv" if src == "PPR" else "")
    tier = tiers.get(norm_journal(journal))
    return {
        "title": clean_title(rec.get("title")).rstrip("."),
        "authors": short_authors(rec.get("authorString")),
        "journal": journal,
        "date": rec.get("firstPublicationDate", ""),
        "preprint": src == "PPR",
        "review": "review" in (rec.get("pubType") or "").lower(),
        "tier": tier,  # "top" | "key" | None
        "url": f"https://doi.org/{doi}" if doi else f"https://europepmc.org/article/{src}/{rid}",
    }


SKIP_TITLES = ("correction", "erratum", "corrigendum", "retraction note")


def fetch_tab(tab: dict, cfg: dict, tiers: dict[str, str], journal_clause: str) -> dict:
    window = tab.get("window_days", cfg.get("window_days", 21))
    limit = tab.get("limit", cfg.get("limit", 40))
    since = (TODAY - dt.timedelta(days=window)).isoformat()
    key_only = tab.get("journals", "any") == "key"
    out = {"label": tab["label"], "window_days": window, "key_only": key_only, "items": []}
    name = f"Papers · {tab['label']}"
    try:
        recs = epmc_search(build_query(tab, since, journal_clause if key_only else None), 300 if key_only else 150)
        if key_only and not recs:
            # Fallback if Europe PMC's JOURNAL field did not match: fetch wider, filter here
            log(f"        {short(tab['label'], 30)}: no hits with journal filter, retrying without it")
            recs = epmc_search(build_query(tab, since, None), 1000)
        seen, items = set(), []
        for rec in recs:
            p = to_paper(rec, tiers)
            key = (rec.get("doi") or re.sub(r"\W+", "", plain(p["title"]).lower()))
            if not p["title"] or key in seen or plain(p["title"]).lower().startswith(SKIP_TITLES):
                continue
            if key_only and not p["tier"]:
                continue
            seen.add(key)
            items.append(p)
        out["items"] = sorted(items, key=lambda x: x["date"], reverse=True)[:limit]
        scope = "key journals" if key_only else "all journals"
        ok(name, len(out["items"]), tab["label"], f"{plural(len(out['items']), 'paper')} ({window} days, {scope})")
    except Exception as e:  # one failing tab must not break the page
        out["error"] = type(e).__name__
        fail(name, e, tab["label"])
    return out


def fetch_papers(cfg: dict) -> list[dict]:
    j = cfg.get("journals", {})
    tiers = {norm_journal(x): "top" for x in j.get("top_tier", [])}
    tiers.update({norm_journal(x): "key" for x in j.get("key_field", []) if norm_journal(x) not in tiers})
    journal_clause = " OR ".join(f'JOURNAL:"{x}"' for x in j.get("top_tier", []) + j.get("key_field", []))
    widgets = []
    for w in cfg.get("paper_widgets", []):
        log(f"  {w['title']}")
        widgets.append({"title": w["title"], "tabs": [fetch_tab(t, cfg, tiers, journal_clause) for t in w["tabs"]]})
    return widgets


# ---------- Group publications (OpenAlex + PubMed, merged) ----------

OPENALEX = "https://api.openalex.org/works"
# Publication types that count as group papers
OPENALEX_TYPES = ["article", "review"]
EPMC_SKIP_TYPES = ("editorial", "comment", "letter", "erratum", "correction", "news", "retraction",
                   "expression of concern", "preprint", "published erratum")


def norm_doi(doi: str | None) -> str:
    d = (doi or "").strip().lower()
    return re.sub(r"^(https?://)?(dx\.)?doi\.org/", "", d)


def paper_key(doi: str | None, title: str) -> str:
    """How the same paper is recognised in both sources: its DOI, or else its title."""
    return norm_doi(doi) or "t:" + re.sub(r"\W+", "", plain(title).lower())


def openalex_api_key() -> str:
    """The OpenAlex API key: from the OPENALEX_API_KEY environment variable (used by GitHub
    Actions, via a repository secret) or from a local file .openalex_key (never committed)."""
    key = os.environ.get("OPENALEX_API_KEY", "").strip()
    if not key and (ROOT / ".openalex_key").exists():
        key = (ROOT / ".openalex_key").read_text(encoding="utf-8").strip()
    return key


class OpenAlexKeyError(RuntimeError):
    pass


def openalex_group_works(orcids: list[str], api_key: str = "") -> list[dict]:
    """All journal articles and reviews by these ORCIDs in OpenAlex (follows the result pages).

    Asks OpenAlex to filter by type and journal. If OpenAlex rejects that filter,
    it retries with the ORCID filter only and applies the same rules here instead.
    """
    ids = "|".join(f"https://orcid.org/{o.strip()}" for o in orcids)
    full = f"authorships.author.orcid:{ids},type:{'|'.join(OPENALEX_TYPES)},primary_location.source.type:journal"
    simple = f"authorships.author.orcid:{ids}"
    for flt in (full, simple):
        params = {"filter": flt, "select": "id,doi,title,publication_date,type,primary_location,authorships,ids",
                  "per_page": 200, "cursor": "*"}
        if api_key:
            params["api_key"] = api_key  # free key from openalex.org/settings/api
        works = []
        try:
            for _ in range(25):  # at most 5,000 papers
                d = get(OPENALEX, params=params).json()
                works += d.get("results", [])
                progress(f"        OpenAlex: {len(works)} / {d.get('meta', {}).get('count', '?')} papers")
                params["cursor"] = d.get("meta", {}).get("next_cursor")
                if not params["cursor"] or not d.get("results"):
                    break
        except requests.HTTPError as e:
            code = e.response.status_code if e.response is not None else None
            if code in (401, 402, 403, 409):
                raise OpenAlexKeyError(
                    f"HTTP {code}: OpenAlex refused the request. "
                    + ("Check that the API key is correct." if api_key else
                       "It needs a free API key (OPENALEX_API_KEY); see the README."))
            if flt is full and code == 400:
                log(f"        OpenAlex did not accept the full filter (HTTP {e.response.status_code}), retrying a simpler search")
                continue
            raise
        if flt is simple:  # apply the type/journal rules here
            works = [w for w in works if w.get("type") in OPENALEX_TYPES
                     and ((w.get("primary_location") or {}).get("source") or {}).get("type", "journal") == "journal"]
        progress_done(f"        OpenAlex: {plural(len(works), 'paper')}")
        return works
    return []


def from_openalex(w: dict) -> dict:
    loc = w.get("primary_location") or {}
    names = [a.get("author", {}).get("display_name", "") for a in w.get("authorships") or []]
    title = clean_title(w.get("title")).rstrip(".")
    doi = norm_doi(w.get("doi"))
    return {"title": title, "authors": short_authors(", ".join(n for n in names if n), 3),
            "journal": (loc.get("source") or {}).get("display_name", ""),
            "date": w.get("publication_date") or "", "doi": doi,
            "url": f"https://doi.org/{doi}" if doi else (loc.get("landing_page_url") or w.get("id", "")),
            "pmid": (w.get("ids") or {}).get("pmid", "")}


def fetch_group_pubs(cfg: dict) -> dict:
    g = cfg.get("group", {})
    orcids = [str(o).strip() for o in g.get("orcids") or [] if str(o).strip()]
    if not orcids and not g.get("extra_query"):
        fail("Group publications", "no ORCIDs in config.yml, showing example")
        return example_group_pubs()

    # 1) OpenAlex: fast, picks up papers within days of publication
    oa = []
    if orcids:
        key = openalex_api_key()
        log(f"    ..  searching OpenAlex for {plural(len(orcids), 'ORCID')} (journal articles and reviews)"
            + ("" if key else " - no API key found, this will probably fail"))
        try:
            oa = [from_openalex(w) for w in openalex_group_works(orcids, key)]
            ok("Group publications · OpenAlex", len(oa), "OpenAlex", f"{plural(len(oa), 'paper')}")
        except Exception as e:
            fail("Group publications · OpenAlex", e, "OpenAlex")

    # 2) PubMed (via Europe PMC): the cross-check, plus the optional name search
    pm = []
    parts = [f'AUTHORID:"{o}"' for o in orcids] + ([f"({g['extra_query']})"] if g.get("extra_query") else [])
    log(f"    ..  cross-checking in PubMed (via Europe PMC)" + (" incl. extra query" if g.get("extra_query") else ""))
    try:
        recs = epmc_search("(" + " OR ".join(parts) + ") AND SRC:MED", 1000)
        for r in recs:
            ptype = (r.get("pubType") or "").lower()
            if any(t in ptype for t in EPMC_SKIP_TYPES) and "review" not in ptype:
                continue
            p = to_paper(r, {})
            pm.append({**p, "authors": short_authors(r.get("authorString"), 3), "doi": norm_doi(r.get("doi")),
                       "pmid": r.get("pmid", "")})
        ok("Group publications · PubMed", len(pm), "PubMed (Europe PMC)", f"{plural(len(pm), 'paper')}")
    except Exception as e:
        fail("Group publications · PubMed", e, "PubMed (Europe PMC)")

    if not oa and not pm:
        return example_group_pubs()

    # 3) Merge by DOI (or title); keep the earliest date and remember where each paper was found
    merged: dict[str, dict] = {}
    for src, papers in (("OpenAlex", oa), ("PubMed", pm)):
        for p in papers:
            if not p["title"] or not p["date"]:
                continue
            k = paper_key(p["doi"], p["title"])
            if k in merged:
                m = merged[k]
                m["sources"].append(src)
                m["date"] = min(m["date"], p["date"])
                m["pmid"] = m.get("pmid") or p.get("pmid", "")
            else:
                merged[k] = {**p, "sources": [src]}
    papers = sorted(merged.values(), key=lambda p: p["date"])
    both = sum(len(p["sources"]) == 2 for p in papers)
    only_oa = [p for p in papers if p["sources"] == ["OpenAlex"]]
    only_pm = [p for p in papers if p["sources"] == ["PubMed"]]
    log(f"        merged: {plural(len(papers), 'paper')} ({both} in both, {len(only_oa)} only in OpenAlex, "
        f"{len(only_pm)} only in PubMed)")
    recent_new = [p for p in only_oa if p["date"] >= (TODAY - dt.timedelta(days=60)).isoformat()]
    for p in recent_new[-5:]:
        log(f"        not yet in PubMed: {short(p['title'], 60)} ({p['date']})")

    # The latest paper: newest date that isn't in the future (print issues are sometimes dated ahead)
    past = [p for p in papers if p["date"][:10] <= TODAY.isoformat()] or papers
    latest = dict(past[-1]) if past else None
    if latest:
        log(f"        latest: {short(latest['title'], 70)} ({latest['journal']}, {latest['date']})")
    years = Counter(int(p["date"][:4]) for p in papers)
    ok("Group publications", len(papers), "Group publications (merged)", f"{plural(len(papers), 'paper')}")
    return {"example": False, "latest": latest,
            "per_year": [{"year": y, "n": years.get(y, 0)} for y in range(TODAY.year - 7, TODAY.year + 1)]}


def example_group_pubs() -> dict:
    return {
        "example": True,
        "latest": {
            "title": "Proteomic profiling of liver and plasma uncovers biomarkers and mechanisms in patients with primary biliary cholangitis",
            "authors": "Jesper Sloth Kellemann, Anne-Sofie Houlberg Jensen, Annelaura Bach Nielsen, …, Nicolai J. Wewer Albrechtsen",
            "journal": "Communications Medicine",
            "date": "2026-10-01",
            "url": "https://doi.org/10.1038/s43856-026-01924-9",
            "sources": ["OpenAlex"],
        },
        "per_year": [{"year": TODAY.year - 7 + i, "n": n} for i, n in enumerate([14, 22, 25, 27, 24, 30, 28, 21])],
    }


# ====================================================================
# News (RSS / Atom)
# ====================================================================

def entry_date(e) -> str:
    t = e.get("published_parsed") or e.get("updated_parsed")
    return dt.date(*t[:3]).isoformat() if t else ""


# Where feeds put an item's image, in order of preference
IMG_PATTERNS = [
    r'<media:content[^>]+url="([^"]+)"[^>]*medium="image"',
    r'<media:content[^>]+url="([^"]+\.(?:jpe?g|png|webp|gif)[^"]*)"',
    r'<media:thumbnail[^>]+url="([^"]+)"',
    r'<webfeeds:featuredImage[^>]+url="([^"]+)"',
    r'<enclosure[^>]+url="([^"]+)"[^>]+type="image/',
    r'<enclosure[^>]+type="image/[^"]*"[^>]+url="([^"]+)"',
    r'<img[^>]+src="([^"]+)"',
]
JUNK_IMG = re.compile(r"emoji|pixel|feedburner|gravatar|1x1|spacer", re.I)


def raw_item_images(xml: str) -> dict[str, str]:
    """Map item link -> first image URL found in the raw feed XML."""
    out = {}
    for m in re.finditer(r"<(item|entry)\b.*?</\1>", xml, re.S):
        block = html.unescape(m.group(0))  # article HTML inside feeds is often entity-escaped
        link = re.search(r"<link>\s*(?:<!\[CDATA\[)?\s*([^<\]\s]+)", block) or re.search(r'<link[^>]+href="([^"]+)"', block)
        if not link:
            continue
        for pattern in IMG_PATTERNS:
            for im in re.finditer(pattern, block, re.I):
                if not JUNK_IMG.search(im.group(1)):
                    out[link.group(1).strip()] = html.unescape(im.group(1))
                    break
            if link.group(1).strip() in out:
                break
    return out


OG_IMAGE = [
    re.compile(r'<meta[^>]+(?:property|name)="(?:og:image|twitter:image)(?::src)?"[^>]+content="([^"]+)"', re.I),
    re.compile(r'<meta[^>]+content="([^"]+)"[^>]+(?:property|name)="(?:og:image|twitter:image)(?::src)?"', re.I),
]


# Where article pages state what kind of article they are (News, Correspondence, ...)
TYPE_PATTERNS = [
    re.compile(r'<meta[^>]+name="(?:citation_article_type|dc\.type|prism\.section|WT\.cg_s)"[^>]+content="([^"]+)"', re.I),
    re.compile(r'<meta[^>]+content="([^"]+)"[^>]+name="(?:citation_article_type|dc\.type|prism\.section|WT\.cg_s)"', re.I),
    re.compile(r'<meta[^>]+property="article:section"[^>]+content="([^"]+)"', re.I),
    re.compile(r'data-test="article-category"[^>]*>\s*([^<]+?)\s*<', re.I),
    re.compile(r'"(?:articleType|article_type|contentType)"\s*:\s*"([^"]+)"', re.I),
]


def page_info(url: str) -> tuple[str, str]:
    """(preview image, article type) declared by an article page; empty strings if unknown."""
    try:
        r = requests.get(url, headers=UA, timeout=15)
        if r.status_code != 200:
            return "", ""
        head = r.text[:400_000]
        image = next((html.unescape(m.group(1)) for rx in OG_IMAGE for m in [rx.search(head)]
                      if m and not JUNK_IMG.search(m.group(1))), "")
        kind = next((html.unescape(m.group(1)).strip() for rx in TYPE_PATTERNS for m in [rx.search(head)] if m), "")
        return image, kind
    except Exception:
        return "", ""


def fetch_news(cfg: dict) -> list[dict]:
    out = []
    for f in cfg.get("news", []):
        label = f["label"]
        limit = f.get("limit", 10)
        exclude_types = {t.lower() for t in f.get("exclude_types") or []}
        include_types = {t.lower() for t in f.get("include_types") or []}
        title_filters = [re.compile(p, re.I) for p in f.get("exclude_titles") or []]
        check_types = bool(exclude_types or include_types)
        tab = {"label": label, "items": [], "logo": ""}
        try:
            r = get(f["url"])
            feed = feedparser.parse(r.content)
            raw_images = raw_item_images(r.text)
            tab["logo"] = f.get("logo") or (feed.feed.get("image") or {}).get("href", "")

            # Read extra items when filtering, so the tab still fills up afterwards
            candidates = []
            for e in feed.entries[: limit * 3 if (check_types or title_filters) else limit]:
                link = e.get("link", "")
                image = raw_images.get(link, "")
                if not image:
                    for key in ("media_thumbnail", "media_content"):
                        if e.get(key):
                            image = e[key][0].get("url", "")
                            break
                candidates.append({"title": clean_title(e.get("title", "")), "url": link,
                                   "source": label, "date": entry_date(e), "image": image})

            removed = Counter()
            if title_filters:
                kept = []
                for it in candidates:
                    if any(rx.search(plain(it["title"])) for rx in title_filters):
                        removed["title filter"] += 1
                    else:
                        kept.append(it)
                candidates = kept

            needs_page = lambda it: it["url"] and (check_types or (f.get("fetch_images") and not it["image"]))
            n_pages = sum(1 for it in candidates[:limit] if needs_page(it))
            if n_pages:
                what = " and ".join(x for x in ["article type" if check_types else "", "image" if f.get("fetch_images") else ""] if x)
                log(f"        {label}: checking article pages for {what}")
            checked = found = 0
            for it in candidates:
                if len(tab["items"]) >= limit:
                    break
                if needs_page(it):
                    checked += 1
                    progress(f"        {label}: {checked} pages checked, {len(tab['items'])}/{limit} items kept")
                    image, kind = page_info(it["url"])
                    time.sleep(0.3)  # be polite to the publisher's servers
                    if f.get("fetch_images") and not it["image"] and image:
                        it["image"] = image
                        found += 1
                    if kind:
                        it["type"] = kind
                        if kind.lower() in exclude_types or (include_types and kind.lower() not in include_types):
                            removed[kind] += 1
                            continue
                tab["items"].append(it)
            if checked:
                progress_done(f"        {label}: {plural(checked, 'article page')} checked")
            if removed:
                log(f"        {label}: left out " + ", ".join(f"{n} {k}" for k, n in removed.most_common()))
            if check_types and checked and not any(it.get("type") for it in tab["items"]) and not removed:
                log(f"        {label}: could not read the article type on these pages, so nothing was filtered")

            n_img = sum(bool(it["image"]) for it in tab["items"])
            logo = "feed logo" if tab["logo"] else "no logo"
            ok(f"News · {label}", len(tab["items"]), label,
               f"{plural(len(tab['items']), 'item')}, {n_img} with {'an image' if n_img == 1 else 'images'} ({logo})")
        except Exception as e:
            tab["error"] = type(e).__name__
            fail(f"News · {label}", e, label)
        out.append(tab)
    return out


# ====================================================================
# Daily boxes
# ====================================================================

def save_image(url: str, out_dir: pathlib.Path, stem: str) -> str:
    """Download an image next to the page; fall back to the remote URL."""
    try:
        r = get(url)
        ctype = r.headers.get("content-type", "")
        ext = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp", "image/svg+xml": ".svg"}.get(ctype.split(";")[0])
        if not ext:
            return url
        (out_dir / "img").mkdir(parents=True, exist_ok=True)
        (out_dir / "img" / f"{stem}{ext}").write_bytes(r.content)
        return f"img/{stem}{ext}"
    except Exception:
        log(f"        could not download image, linking to it instead")
        return url


def fetch_owid(cfg: dict, out_dir: pathlib.Path) -> dict | None:
    url = cfg.get("daily", {}).get("owid_feed")
    if not url:
        log("    --  Data insight                        switched off in config.yml")
        return None
    try:
        e = feedparser.parse(get(url).content).entries[0]
        body = (e.get("content") or [{}])[0].get("value", "") or e.get("summary", "")
        img = re.search(r'<img[^>]+src="([^"]+)"', body)
        para = re.search(r"<p>(.*?)</p>", body, re.S)
        text = html.unescape(re.sub(r"<[^>]+>", "", para.group(1))).strip() if para else ""
        out = {"title": clean_title(e.get("title", "")), "url": e.get("link", ""), "date": entry_date(e),
               "image": save_image(img.group(1), out_dir, "owid") if img else "",
               "text": (text[:260] + "…") if len(text) > 260 else text}
        ok("Our World in Data", 1, "Data insight (Our World in Data)", short(out["title"], 60))
        return out
    except Exception as ex:
        fail("Our World in Data", ex, "Data insight (Our World in Data)")
        return None


def fetch_motm(cfg: dict, out_dir: pathlib.Path) -> dict | None:
    url = cfg.get("daily", {}).get("motm_feed")
    if not url:
        log("    --  Molecule of the month               switched off in config.yml")
        return None
    try:
        e = feedparser.parse(get(url).content).entries[0]
        link = e.get("link", "").replace("http://", "https://")
        image = ""
        try:
            page = get(link).text
            m = re.search(r'<meta[^>]+property="og:image"[^>]+content="([^"]+)"', page) or \
                re.search(r'<meta[^>]+content="([^"]+)"[^>]+property="og:image"', page)
            if m:
                image = save_image(m.group(1), out_dir, "motm")
        except Exception:
            log("        could not read the article page for its image")
        out = {"title": clean_title(e.get("title", "")), "url": link, "date": entry_date(e),
               "text": html.unescape(re.sub(r"<[^>]+>", "", e.get("summary", ""))).strip(), "image": image}
        ok("PDB Molecule of the Month", 1, "Molecule of the month (PDB-101)", short(out["title"], 60))
        return out
    except Exception as ex:
        fail("PDB Molecule of the Month", ex, "Molecule of the month (PDB-101)")
        return None


# ---------- Protein Spotlight (SIB) ----------

SPOTLIGHT_PAGE = "https://www.proteinspotlight.org/spotlight/"
MONTHS = {m: i for i, m in enumerate(["january", "february", "march", "april", "may", "june", "july", "august",
                                       "september", "october", "november", "december"], 1)}
# Official UniProt accession format
UNIPROT_ACC = r"(?:[OPQ][0-9][A-Z0-9]{3}[0-9]|[A-NR-Z][0-9](?:[A-Z][A-Z0-9]{2}[0-9]){1,2})"


def first_paragraph(page: str, min_len: int = 80) -> str:
    """The first real paragraph of article text (skips menus and short captions)."""
    for p in re.findall(r"<p[^>]*>(.*?)</p>", page, re.S | re.I):
        text = " ".join(html.unescape(re.sub(r"<[^>]+>", " ", p)).split())
        if len(text) >= min_len:
            return text
    return ""


def fetch_spotlight(cfg: dict) -> dict | None:
    daily = cfg.get("daily", {})
    feed_url = daily.get("spotlight_feed")
    if not feed_url:
        log("    --  Protein Spotlight                   not set up (add spotlight_feed in config.yml)")
        return None
    name = label = "Protein Spotlight"
    try:
        # 1) newest issue: from the feed, or from the issue list if the feed can't be read
        link, title, date = "", "", ""
        try:
            entries = feedparser.parse(get(feed_url).content).entries
            if entries:
                e = entries[0]
                link, title, date = e.get("link", ""), e.get("title", ""), entry_date(e)
        except Exception as ex:
            log(f"        feed could not be read ({type(ex).__name__}), using the issue list instead")
        if not link:
            listing = get(daily.get("spotlight_page", SPOTLIGHT_PAGE)).text
            issues = [int(n) for n in re.findall(r"back_issues/(\d+)", listing)]
            if not issues:
                raise RuntimeError("no issues found on the Protein Spotlight page")
            link = f"https://www.proteinspotlight.org/spotlight/back_issues/{max(issues)}/"
        link = link.replace("http://", "https://")
        log(f"        newest issue: {link}")

        # 2) article page: image, opening paragraph, date and featured proteins
        page = get(link).text
        image = ""
        for rx in OG_IMAGE:
            m = rx.search(page)
            if m:
                image = html.unescape(m.group(1)).replace("http://", "https://")
                break
        if not title:
            m = (re.search(r'<meta[^>]+property="og:title"[^>]+content="([^"]+)"', page, re.I)
                 or re.search(r"<title>(.*?)</title>", page, re.S | re.I))
            parts = [p.strip() for p in re.split(r"\s[|–-]\s|\|", html.unescape(m.group(1)))] if m else []
            title = next((p for p in parts if p and "protein spotlight" not in p.lower()), "Protein Spotlight")
        title = clean_title(title)
        title = title[:1].upper() + title[1:]
        m = re.search(r"\b(January|February|March|April|May|June|July|August|September|October|November|December)\s+(20\d\d)\b", page)
        if m:
            date = f"{m.group(2)}-{MONTHS[m.group(1).lower()]:02d}"
        issue = re.search(r"back_issues/(\d+)", link)
        text = first_paragraph(page)
        accs = list(dict.fromkeys(re.findall(rf"uniprot\.org/(?:uniprotkb|uniprot)/({UNIPROT_ACC})\b", page)))[:3]

        # 3) names of the featured proteins, from UniProt
        proteins = []
        for acc in accs:
            try:
                u = uniprot_entry(acc=acc)
                pname = u.get("proteinDescription", {}).get("recommendedName", {}).get("fullName", {}).get("value", "")
                organism = u.get("organism", {}).get("commonName") or u.get("organism", {}).get("scientificName", "")
                proteins.append({"accession": acc, "name": pname or acc, "organism": organism,
                                 "url": f"https://www.uniprot.org/uniprotkb/{acc}/entry"})
            except Exception:
                proteins.append({"accession": acc, "name": acc, "organism": "",
                                 "url": f"https://www.uniprot.org/uniprotkb/{acc}/entry"})
        if proteins:
            log("        featured: " + ", ".join(f"{p['name']} ({p['organism'] or p['accession']})" for p in proteins))

        out = {"title": title, "url": link, "date": date, "issue": issue.group(1) if issue else "",
               "image": image,  # shown from Protein Spotlight's site, not copied (often artwork)
               "text": text[:260].rsplit(" ", 1)[0] + "…" if len(text) > 260 else text,
               "proteins": proteins}
        ok(name, 1, label, f"issue {out['issue']}: {short(title, 50)}" if out["issue"] else short(title, 60))
        return out
    except Exception as ex:
        fail(name, ex, label)
        return None


# ---------- Target of the day ----------

# Used only if ChEMBL cannot be reached: well-known targets of approved drugs.
FALLBACK_TARGETS = [
    "GLP1R", "GIPR", "GCGR", "INSR", "DPP4", "SLC5A2", "PPARG", "KCNJ11", "ABCC8", "HMGCR", "PCSK9", "NPC1L1",
    "THRB", "ACE", "AGTR1", "ADRB1", "ADRB2", "F10", "F2", "P2RY12", "VKORC1", "PTGS2", "TNF", "IL6R", "PDCD1",
    "CTLA4", "MS4A1", "VEGFA", "EGFR", "ERBB2", "BRAF", "ABL1", "ESR1", "AR", "CYP19A1", "SLC6A4", "DRD2",
    "OPRM1", "NR3C1", "ATP4A", "HRH1", "CFTR",
]


def chembl_all(path: str, params: dict, key: str, cap: int = 20) -> list[dict]:
    out, params = [], {**params, "format": "json", "limit": 1000, "offset": 0}
    for _ in range(cap):
        d = get(f"{CHEMBL}/{path}", params=params).json()
        out += d.get(key, [])
        total = d.get("page_meta", {}).get("total_count")
        progress(f"        {path.split('.')[0]}: {len(out)}" + (f" / {total}" if total else "") + " records")
        if not d.get("page_meta", {}).get("next"):
            break
        params["offset"] += 1000
    progress_done(f"        {path.split('.')[0]}: {len(out)} records")
    return out


def uniprot_entry(acc: str | None = None, gene: str | None = None) -> dict:
    if acc:
        return get(f"{UNIPROT}/{acc}.json").json()
    q = f"gene_exact:{gene} AND organism_id:9606 AND reviewed:true"
    res = get(f"{UNIPROT}/search", params={"query": q, "format": "json", "size": 1}).json()["results"]
    return res[0]


def first_sentences(text: str, max_len: int = 240) -> str:
    text = re.sub(r"\s*\((?:PubMed|By similarity)[^)]*\)", "", text).strip()
    out = ""
    for s in re.split(r"(?<=\.)\s+", text):
        if len(out) + len(s) > max_len and out:
            break
        out += (" " if out else "") + s
    return out if len(out) <= max_len + 40 else out[:max_len].rsplit(" ", 1)[0] + "…"


def describe_uniprot(u: dict) -> dict:
    gene = (u.get("genes") or [{}])[0].get("geneName", {}).get("value", "")
    name = u.get("proteinDescription", {}).get("recommendedName", {}).get("fullName", {}).get("value", "")
    func = next((c["texts"][0]["value"] for c in u.get("comments", [])
                 if c.get("commentType") == "FUNCTION" and c.get("texts")), "")
    ensg = ""
    for x in u.get("uniProtKBCrossReferences", []):
        if x.get("database") == "Ensembl":
            ensg = next((p["value"] for p in x.get("properties", []) if p.get("key") == "GeneId"), "")
            if ensg:
                break
    acc = u.get("primaryAccession", "")
    return {
        "gene": gene, "protein": name, "accession": acc, "function": first_sentences(func) if func else "",
        "links": {
            "UniProt": f"https://www.uniprot.org/uniprotkb/{acc}/entry",
            "Open Targets": f"https://platform.opentargets.org/target/{ensg.split('.')[0]}" if ensg
                            else f"https://platform.opentargets.org/search?q={gene}",
            "Protein Atlas": f"https://www.proteinatlas.org/search/{gene}",
        },
    }


def fetch_target_of_day() -> dict | None:
    name, label = "Target of the day", "Target of the day"
    try:
        log("    ..  downloading approved drugs and their targets from ChEMBL (can take a minute)")
        approved = {m["molecule_chembl_id"]: (m.get("pref_name") or m["molecule_chembl_id"]).title()
                    for m in chembl_all("molecule.json", {"max_phase": 4, "only": "molecule_chembl_id,pref_name"}, "molecules")}
        mechs = [m for m in chembl_all("mechanism.json", {}, "mechanisms")
                 if (m.get("parent_molecule_chembl_id") or m.get("molecule_chembl_id")) in approved and m.get("target_chembl_id")]
        by_target: dict[str, list[dict]] = {}
        for m in mechs:
            by_target.setdefault(m["target_chembl_id"], []).append(m)
        pool = sorted(by_target)
        log(f"        {len(approved)} approved drugs acting on {len(pool)} targets")
        random.Random(42).shuffle(pool)  # fixed shuffle so neighbouring days differ
        start = TODAY.toordinal() % len(pool)
        for i in range(40):  # skip non-human / multi-protein targets
            tid = pool[(start + i) % len(pool)]
            t = get(f"{CHEMBL}/target/{tid}.json").json()
            comps = t.get("target_components") or []
            if t.get("target_type") != "SINGLE PROTEIN" or t.get("organism") != "Homo sapiens" or not comps:
                continue
            info = describe_uniprot(uniprot_entry(acc=comps[0]["accession"]))
            mol_ids = list(dict.fromkeys((m.get("parent_molecule_chembl_id") or m["molecule_chembl_id"]) for m in by_target[tid]))
            drugs = [approved[x] for x in mol_ids if x in approved]
            moa = Counter(m.get("mechanism_of_action") for m in by_target[tid]).most_common(1)[0][0]
            indications = []
            try:
                ind = get(f"{CHEMBL}/drug_indication.json", params={
                    "molecule_chembl_id__in": ",".join(mol_ids[:30]), "max_phase_for_ind": 4,
                    "format": "json", "limit": 200}).json().get("drug_indications", [])
                indications = [k for k, _ in Counter(x.get("mesh_heading") for x in ind if x.get("mesh_heading")).most_common(3)]
            except Exception:
                pass
            ok(name, 1, label, f"{info['gene']} ({len(drugs)} approved drug{'s' if len(drugs) != 1 else ''})")
            return {**info, "drugs": drugs, "n_drugs": len(drugs), "mechanism": moa, "indications": indications,
                    "source": "ChEMBL approved drugs + UniProt"}
        raise RuntimeError("no human single-protein target found")
    except Exception as e:
        try:  # fallback: fixed list, UniProt only
            gene = FALLBACK_TARGETS[TODAY.toordinal() % len(FALLBACK_TARGETS)]
            log(f"        ChEMBL failed ({type(e).__name__}); using the fallback list instead")
            info = describe_uniprot(uniprot_entry(gene=gene))
            fail(name, f"ChEMBL unavailable ({type(e).__name__}); showing {gene} from fallback list", label)
            return {**info, "drugs": [], "n_drugs": None, "mechanism": "", "indications": [], "source": "UniProt"}
        except Exception as e2:
            fail(name, e2, label)
            return None


# ====================================================================
# Render
# ====================================================================

def render(data: dict, out_dir: pathlib.Path, fragment: pathlib.Path | None) -> None:
    tpl = (ROOT / "template" / "index.html").read_text(encoding="utf-8")
    blob = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
    body = tpl.replace("/*__DATA__*/null", blob)
    page = ('<!doctype html>\n<html lang="en">\n<head>\n<meta charset="utf-8">\n'
            '<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">\n'
            '</head>\n<body>\n' + body + "\n</body>\n</html>\n")
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "index.html").write_text(page, encoding="utf-8")
    (out_dir / "data.json").write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    if fragment:
        fragment.write_text(body, encoding="utf-8")


def rel(p: pathlib.Path) -> str:
    try:
        return str(p.resolve().relative_to(pathlib.Path.cwd().resolve()))
    except ValueError:
        return str(p)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sample", type=pathlib.Path, help="render from a saved JSON file instead of fetching")
    ap.add_argument("--out", type=pathlib.Path, default=ROOT / "site")
    ap.add_argument("--fragment", type=pathlib.Path, help="also write the page without the <html> wrapper")
    ap.add_argument("-q", "--quiet", action="store_true",
                    help="hide the step-by-step messages; only print the summary at the end")
    args = ap.parse_args()
    global VERBOSE
    VERBOSE = not args.quiet

    log(f"Building the dashboard ({TODAY.isoformat()}, {'example data' if args.sample else 'live data'})")
    cfg = yaml.safe_load((ROOT / "config.yml").read_text(encoding="utf-8"))
    log(f"  read config.yml: {sum(len(w['tabs']) for w in cfg.get('paper_widgets', []))} paper tabs, "
        f"{len(cfg.get('news', []))} news feeds")
    j = cfg.get("journals", {})
    base = {
        "group": {"name": cfg["group"]["name"], "subtitle": cfg["group"].get("subtitle", "")},
        "links": cfg.get("links", []),
        "journals": {"top_tier": j.get("top_tier", []), "key_field": j.get("key_field", [])},
        "generated": dt.datetime.now(dt.timezone.utc).isoformat(timespec="minutes"),
    }

    if args.sample:
        n = 2
        step(1, n, "Example data")
        data = {**base, **json.loads(args.sample.read_text(encoding="utf-8")), "sample": True}
        log(f"    ok  read {rel(args.sample)} (nothing is fetched from the internet)")
    else:
        n = 5
        step(1, n, "New papers (Europe PMC)")
        papers = fetch_papers(cfg)
        step(2, n, "News feeds")
        news = fetch_news(cfg)
        step(3, n, "Group publications (OpenAlex + PubMed)")
        group = fetch_group_pubs(cfg)
        step(4, n, "Daily boxes")
        dcfg = cfg.get("daily", {})
        target_on = bool(dcfg.get("target_of_the_day", True))
        if not target_on:
            log("    --  Target of the day                   switched off in config.yml")
        daily = {
            "spotlight_enabled": bool(dcfg.get("spotlight_feed")),
            "spotlight": fetch_spotlight(cfg),
            "target_enabled": target_on,
            "target": fetch_target_of_day() if target_on else None,
            "owid": fetch_owid(cfg, args.out),
            "motm": fetch_motm(cfg, args.out),
        }
        data = {**base, "paper_widgets": papers, "news": news, "group_pubs": group, "daily": daily, "status": STATUS}

    step(n, n, "Writing the page")
    render(data, args.out, args.fragment)
    log(f"    ok  {rel(args.out / 'index.html')}")

    bad = [s for s in data.get("status", []) if not s["ok"]]
    say(("\n" if VERBOSE else "") + f"Done in {time.time() - T0:.0f} s. "
        + (f"{len(bad)} source(s) had problems:" if bad else "All sources updated."))
    for s in bad:
        say(f"  - {s['name']}: {s['error']}")
    say(f"Open {rel(args.out / 'index.html')} in your browser to see the result.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
