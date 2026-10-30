"""Build the research group dashboard.

Fetches new papers (Europe PMC), news (RSS), the group's own publications
(OpenAlex, cross-checked against PubMed via Europe PMC) and the boxes in the
right column (Protein Spotlight, target of the day, Our World in Data insight,
PDB Molecule of the Month) and the "Coming up" calendar (DDSA, NNF Science Cluster,
DDEA, ELIXIR TeSS and conference websites), then writes site/index.html.

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
# Coming up: courses, events and conferences
# ====================================================================

# ---------- Reading dates from ordinary text ----------

_MONTH_NAMES = {
    1: ["january", "jan", "januar"], 2: ["february", "feb", "februar"], 3: ["march", "mar", "marts"],
    4: ["april", "apr"], 5: ["may", "maj"], 6: ["june", "jun", "juni"], 7: ["july", "jul", "juli"],
    8: ["august", "aug"], 9: ["september", "sept", "sep"], 10: ["october", "oct", "oktober", "okt"],
    11: ["november", "nov"], 12: ["december", "dec"],
}
MONTH_NUM = {name: n for n, names in _MONTH_NAMES.items() for name in names}
_M = "(" + "|".join(sorted(MONTH_NUM, key=len, reverse=True)) + r")(?![a-zæøå])\.?"
_D = r"(\d{1,2})"
_Y = r"(20\d\d)"
_WEEKDAYS = re.compile(r"\b(monday|tuesday|wednesday|thursday|friday|saturday|sunday|mon|tues?|wed|thu(?:rs?)?|fri|sat|sun|"
                       r"mandag|tirsdag|onsdag|torsdag|fredag|lørdag|søndag)\b\.?,?", re.I)

# Most specific first; each match "uses up" its text so it isn't matched again as a single date.
DATE_PATTERNS = [
    # Dates written with numbers (day.month.year, as on Danish sites): 05.11.2026 - 06.11.2026
    ("num-num", re.compile(r"\b(\d{1,2})[./-](\d{1,2})[./-](20\d\d)\s*-\s*(\d{1,2})[./-](\d{1,2})[./-](20\d\d)\b")),
    # 05.-06.11.2026
    ("numd-num", re.compile(r"\b(\d{1,2})\.?\s*-\s*(\d{1,2})[./](\d{1,2})[./](20\d\d)\b")),
    # 04.10.2026  /  04/10/2026
    ("num", re.compile(r"\b(\d{1,2})[./-](\d{1,2})[./-](20\d\d)\b")),
    # 30 May 2029 - 2 June 2029  /  30 December - 2 January 2027
    ("dm-dm", re.compile(rf"\b{_D}\s*{_M}\s*{_Y}?\s*-\s*{_D}\s*{_M}\s*{_Y}?", re.I)),
    # 16 - 19 June 2027  /  07-11 September 2026
    ("d-dm", re.compile(rf"\b{_D}\s*-\s*{_D}\s*{_M}\s*{_Y}?", re.I)),
    # May 30 - June 2 2029
    ("md-md", re.compile(rf"\b{_M}\s*{_D}\s*-\s*{_M}\s*{_D}\b\s*{_Y}?", re.I)),
    # June 18-21 2027  /  June16-19 2029
    ("md-d", re.compile(rf"\b{_M}\s*{_D}\s*-\s*{_D}\b\s*{_Y}?", re.I)),
    # 2026-11-05
    ("iso", re.compile(r"\b(20\d\d)-(\d{2})-(\d{2})\b")),
    # 8 December 2026
    ("dm", re.compile(rf"\b{_D}\s*{_M}\s*{_Y}?", re.I)),
    # December 8 2026
    ("md", re.compile(rf"\b{_M}\s*{_D}\b\s*{_Y}?", re.I)),
]


def normalise_for_dates(text: str) -> str:
    """Make date wordings uniform: drop weekday names and commas, one kind of dash, 'to' -> '-'."""
    t = _WEEKDAYS.sub(" ", text)
    t = re.sub(r"[\u2010-\u2015\u2212]", "-", t)
    t = re.sub(r"(\d{1,2}[./-]\d{1,2}[./-]20\d\d)\s*(?:-|,|kl\.?|at)?\s*\d{1,2}[:.]\d{2}(?![./]?\d)(?:\s*(?:CET|CEST|h))?", r"\1", t)
    t = re.sub(r"\s+(?:to|until|till|through|thru|til)\s+", " - ", t, flags=re.I)
    t = re.sub(r"(\d)(?:st|nd|rd|th)\b", r"\1", t)
    t = t.replace(",", " ")
    return re.sub(r"\s+", " ", t)


def _mk(y, m, d):
    try:
        return dt.date(int(y), int(m), int(d))
    except (TypeError, ValueError):
        return None


def find_dates(text: str, default_year: int | None = None) -> list[dict]:
    """All dates and date ranges in a text, in reading order.

    Each result: {"start", "end", "pos", "has_year"}. Dates without a year get
    `default_year`; if there is none, they are skipped.
    """
    t = normalise_for_dates(text)
    used = [False] * (len(t) + 1)
    found = []
    for kind, rx in DATE_PATTERNS:
        for m in rx.finditer(t):
            if any(used[m.start():m.end()]):
                continue
            g = m.groups()
            if kind == "num-num":
                s, e, has_year = _mk(g[2], g[1], g[0]), _mk(g[5], g[4], g[3]), True
            elif kind == "numd-num":
                s, e, has_year = _mk(g[3], g[2], g[0]), _mk(g[3], g[2], g[1]), True
            elif kind == "num":
                s = e = _mk(g[2], g[1], g[0])
                has_year = True
            elif kind == "dm-dm":
                d1, m1, y1, d2, m2, y2 = g
                y2 = y2 or y1 or default_year
                y1 = y1 or (int(y2) - 1 if y2 and MONTH_NUM[m1.lower()] > MONTH_NUM[m2.lower()] else y2)
                s, e, has_year = _mk(y1, MONTH_NUM[m1.lower()], d1), _mk(y2, MONTH_NUM[m2.lower()], d2), bool(g[2] or g[5])
            elif kind == "d-dm":
                d1, d2, mo, y = g
                y = y or default_year
                s, e, has_year = _mk(y, MONTH_NUM[mo.lower()], d1), _mk(y, MONTH_NUM[mo.lower()], d2), bool(g[3])
            elif kind == "md-md":
                m1, d1, m2, d2, y = g
                y = y or default_year
                y1 = int(y) - 1 if y and MONTH_NUM[m1.lower()] > MONTH_NUM[m2.lower()] else y
                s, e, has_year = _mk(y1, MONTH_NUM[m1.lower()], d1), _mk(y, MONTH_NUM[m2.lower()], d2), bool(g[4])
            elif kind == "md-d":
                mo, d1, d2, y = g
                y = y or default_year
                s, e, has_year = _mk(y, MONTH_NUM[mo.lower()], d1), _mk(y, MONTH_NUM[mo.lower()], d2), bool(g[3])
            elif kind == "iso":
                s = e = _mk(*g)
                has_year = True
            elif kind == "dm":
                d1, mo, y = g
                s = e = _mk(y or default_year, MONTH_NUM[mo.lower()], d1)
                has_year = bool(y)
            else:  # md
                mo, d1, y = g
                s = e = _mk(y or default_year, MONTH_NUM[mo.lower()], d1)
                has_year = bool(y)
            if not s or not e or e < s or (e - s).days > 60:
                continue
            for i in range(m.start(), m.end()):
                used[i] = True
            found.append({"start": s, "end": e, "pos": m.start(), "endpos": m.end(), "has_year": has_year})
    return sorted(found, key=lambda x: x["pos"])


# ---------- Registration and deadlines ----------

DEADLINE_RX = re.compile(
    r"\b(application deadline|deadline for applications?|abstract (?:submission )?deadline|deadline for abstracts?|"
    r"abstracts? (?:due|by)|registration deadline|early[- ]?(?:bird )?registration(?: deadline)?|registration closes|"
    r"register (?:by|before)|sign[- ]?up (?:by|before)|apply (?:by|before)|enrol(?:l)?ment deadline|"
    r"applications? (?:close|closes|closing)|closing date|last day (?:of|for) (?:registration|sign[- ]?up|applications?)|"
    r"deadline|tilmeldingsfrist|ansøgningsfrist|sidste frist|frist)", re.I)
# Pages that say registration is over: such events are not shown
CLOSED_RX = re.compile(r"registration (?:is |has )?(?:now )?(?:closed|ended)|applications? (?:are |is |have )?(?:now )?closed|"
                       r"fully booked|sold out|no (?:more|available) (?:places|seats|spots)|waiting list only|"
                       r"(?<!if the )(?:course|event|workshop) is (?:now )?full\b|tilmelding(?:en)? er lukket|fuldt booket|udsolgt", re.I)
REG_APPLY = re.compile(r"\b(by application|apply (?:by|before|here|now|for (?:a |the )?(?:place|participation|admission))|"
                       r"application (?:deadline|form)|applications? (?:open|close|must)|selected (?:by|from) (?:the )?"
                       r"(?:abstract|application)s?|ansøg)", re.I)
REG_OPEN = re.compile(r"registration (?:is )?(?:now )?open", re.I)
REG_NONE = re.compile(r"no (?:registration|sign[- ]?up)|registration is not required|open to all|free and open", re.I)
REG_REQUIRED = re.compile(r"\b(register|registration|sign[- ]?up|signup|enrol|tilmeld)", re.I)
FREE_RX = re.compile(r"\b(free of charge|no (?:application |registration )?fees?|registration is free|free|gratis)\b", re.I)
ONLINE_RX = re.compile(r"\b(online|virtual|zoom|webinar|livestream|teams meeting)\b", re.I)


def deadline_kind(phrase: str) -> str:
    p = phrase.lower()
    if "abstract" in p:
        return "Abstracts"
    if "early" in p:
        return "Early registration"
    if "appl" in p or "ansøg" in p or "closing" in p:
        return "Application"
    return "Sign-up"


def registration_info(text: str, event_start: dt.date | None) -> dict:
    """Deadline (if one is stated) and the kind of registration, read from free text."""
    t = normalise_for_dates(text)
    year = event_start.year if event_start else TODAY.year
    deadlines = []
    for m in DEADLINE_RX.finditer(t):
        window = t[m.end(): m.end() + 90]
        dates = find_dates(window, default_year=year)
        if not dates:
            continue
        d = dates[0]["start"]
        if not dates[0]["has_year"] and event_start and d > event_start:
            d = d.replace(year=d.year - 1)  # "19 January" before a June congress is January of the same season
        if event_start and d > event_start:
            continue  # a "deadline" after the event starts is something else
        deadlines.append((d, deadline_kind(m.group(1)), (m.group(0) + window[:dates[0]["endpos"]]).strip()))
    info = {"deadline": "", "deadline_label": "", "reg": "", "deadline_text": ""}
    if deadlines:
        recent = sorted(x for x in deadlines if x[0] >= TODAY - dt.timedelta(days=7))
        d, label, quote = recent[0] if recent else max(deadlines)
        info["deadline"], info["deadline_label"], info["deadline_text"] = d.isoformat(), label, quote
    if CLOSED_RX.search(t):
        info["reg"] = "closed"
    elif REG_NONE.search(t):
        info["reg"] = "none"
    elif REG_OPEN.search(t):
        info["reg"] = "open"
    elif REG_APPLY.search(t):
        info["reg"] = "application"
    elif REG_REQUIRED.search(t):
        info["reg"] = "required"
    return info


EVENT_DATE_LABEL = re.compile(r"\b(event date|date and time|date & time|dates?|when|time and place|dato|tidspunkt)\s*:?", re.I)


def labelled_event_date(text: str) -> dict | None:
    """The date right after a label such as "Event date" or "Date:" (more reliable than guessing)."""
    t = normalise_for_dates(text)
    for m in EVENT_DATE_LABEL.finditer(t):
        dates = find_dates(t[m.end(): m.end() + 80])
        if dates and dates[0]["has_year"] and dates[0]["pos"] <= 15:
            return dates[0]
    return None


def keyword_regex(words: list[str], stems: bool = False) -> re.Pattern | None:
    """Keywords from config.yml -> one regular expression. 'metabol*' matches metabolism,
    metabolic...; words in CAPITALS (like AI) must match exactly, including case.
    With stems=True every word also matches longer forms ("protein" -> "proteins")."""
    parts = []
    for w in words or []:
        w = str(w).strip()
        if not w:
            continue
        body = re.escape(w.rstrip("*")).replace(r"\ ", r"\s+") + (r"\w*" if w.endswith("*") or stems else "")
        parts.append(rf"(?-i:\b{body}\b)" if w.isupper() else rf"\b{body}\b")
    return re.compile("|".join(parts), re.I) if parts else None


# ---------- Reading web pages politely ----------

class RobotsBlocked(RuntimeError):
    pass


_ROBOTS: dict[str, object] = {}


def allowed_by_robots(url: str) -> bool:
    """Respect the site's robots.txt (the rules sites set for automated readers)."""
    from urllib import robotparser
    from urllib.parse import urlsplit
    parts = urlsplit(url)
    base = f"{parts.scheme}://{parts.netloc}"
    if base not in _ROBOTS:
        rp = robotparser.RobotFileParser()
        try:
            r = requests.get(base + "/robots.txt", headers=UA, timeout=15)
            if r.status_code in (401, 403):
                rp.disallow_all = True
            elif r.status_code >= 400:
                rp.parse([])
            else:
                rp.parse(r.text.splitlines())
        except Exception:
            rp.parse([])
        _ROBOTS[base] = rp
    return _ROBOTS[base].can_fetch(UA["User-Agent"], url)


def fetch_page(url: str) -> tuple[str, str]:
    """(html, final url) of a page, if robots.txt allows reading it."""
    if not allowed_by_robots(url):
        raise RobotsBlocked(f"{url.split('/')[2]} does not allow automated reading (robots.txt)")
    r = get(url)
    return r.text, r.url


def html_text(page: str) -> str:
    page = re.sub(r"<(script|style|noscript|svg)\b.*?</\1>", " ", page, flags=re.S | re.I)
    page = re.sub(r"<br\s*/?>|</(p|div|li|h\d|tr|td|section)>", "\n", page, flags=re.I)
    text = html.unescape(re.sub(r"<[^>]+>", " ", page))
    return re.sub(r"[ \t\r\f\v]+", " ", text)


def page_title(page: str) -> str:
    m = (re.search(r'<meta[^>]+property="og:title"[^>]+content="([^"]+)"', page, re.I)
         or re.search(r"<h1[^>]*>(.*?)</h1>", page, re.S | re.I)
         or re.search(r"<title>(.*?)</title>", page, re.S | re.I))
    return clean_title(re.split(r"\s[|–-]\s", html.unescape(re.sub(r"<[^>]+>", "", m.group(1))))[0]) if m else ""


def page_summary(page: str) -> str:
    m = re.search(r'<meta[^>]+(?:property|name)="(?:og:description|description)"[^>]+content="([^"]+)"', page, re.I)
    return html.unescape(m.group(1)) if m else ""


def time_of(s: str) -> str:
    m = re.search(r"\b(\d{1,2}:\d{2})", s or "")
    return "" if not m or m.group(1) in ("00:00", "0:00") else m.group(1)


def new_event(**kw) -> dict:
    base = {"title": "", "url": "", "alt_urls": [], "start": "", "end": "", "time": "", "where": "",
            "online": False, "source": "", "kind": "event", "deadline": "", "deadline_label": "",
            "deadline_text": "", "reg": "", "cost": ""}
    base.update(kw)
    if not base["end"]:
        base["end"] = base["start"]
    return base


# Everything the calendar considered, and why it was shown or left out (written to events_report.txt)
EVENT_REPORT: list[dict] = []


def left_out(source: str, title: str, reason: str, url: str = "", start: str = "") -> None:
    EVENT_REPORT.append({"status": "left out", "reason": reason, "source": source, "title": plain(clean_title(title)),
                         "start": start, "url": url, "deadline": "", "deadline_text": ""})


def _domain(url: str) -> str:
    return re.sub(r"^www\.", "", (url or "").split("/")[2].lower()) if "//" in (url or "") else ""


def _matches_domain(url: str, domains: list[str]) -> bool:
    d = _domain(url)
    return any(d == x.lower() or d.endswith("." + x.lower()) for x in domains or [])


def deadline_from_page(url: str, start: dt.date) -> dict:
    """Open an event's own page and read its deadline / registration status from there."""
    page, _ = fetch_page(url)
    time.sleep(0.4)  # be polite to the site
    return registration_info(html_text(page), start)


# ---------- Source: Danish Data Science Academy (events API) ----------

def ev_ddsa(c: dict, horizon: dt.date, rules: dict) -> tuple[list[dict], str]:
    api = c.get("api", "https://ddsa.dk/wp-json/tribe/events/v1/events")
    keep_cats = {x.lower() for x in c.get("categories", ["DDSA-Organised Event", "DDSA-Funded Event"])}
    course_cat = c.get("course_category", "PhD Course").lower()
    course_tags = {x.lower() for x in c.get("course_tags", [])}
    topic_rx = keyword_regex(c.get("course_topic_words", []))
    follow = c.get("follow_links_to", ["ddeacademy.dk", "phdcourses.dk"])
    raw, page = [], 1
    while page <= 10:
        d = get(api, params={"start_date": TODAY.isoformat(), "end_date": horizon.isoformat(),
                             "per_page": 50, "page": page}).json()
        raw += d.get("events", [])
        if page >= int(d.get("total_pages") or 1):
            break
        page += 1
    out, why, followed = [], Counter(), 0
    for e in raw:
        title = clean_title(e.get("title"))
        cats = {x.get("name", "").lower() for x in e.get("categories") or []}
        tags = {x.get("name", "").lower() for x in e.get("tags") or []}
        is_course = course_cat in cats
        desc = html_text(e.get("description") or "")
        ddsa_url, website = e.get("url", ""), (e.get("website") or "").strip()
        # Link to the original page when DDSA only re-posts someone else's event
        link = website if website.startswith("http") and "ddsa.dk" not in _domain(website) else ddsa_url
        reason = ""
        if not (cats & keep_cats or (is_course and tags & course_tags)):
            reason = "category/tag filter"
        elif rules["exclude"] and rules["exclude"].search(f"{title} {desc[:600]}"):
            reason = "exclude word"
        elif is_course and topic_rx and not topic_rx.search(f"{title} {desc}"):
            reason = "no topic word"
        if reason:
            why[reason] += 1
            left_out("DDSA", title, reason, link, e.get("start_date", "")[:10])
            continue
        venue = e.get("venue") if isinstance(e.get("venue"), dict) else {}
        start = dt.date.fromisoformat(e["start_date"][:10])
        info = registration_info(desc, start)
        # No deadline in DDSA's text: look on the original page (e.g. DDEA, phdcourses.dk)
        if not info["deadline"] and link != ddsa_url and _matches_domain(link, follow):
            try:
                theirs = deadline_from_page(link, start)
                followed += 1
                info = {k: theirs[k] or info[k] for k in info}
                if theirs["reg"] == "closed":
                    info["reg"] = "closed"
            except Exception:
                pass
        cost = str(e.get("cost") or "")
        where_txt = " ".join(str(venue.get(k) or "") for k in ("venue", "city"))
        times = "" if e.get("all_day") else "–".join(x for x in (time_of(e.get("start_date")), time_of(e.get("end_date"))) if x)
        out.append(new_event(
            title=title, url=link, alt_urls=[ddsa_url, website],
            start=start.isoformat(), end=(e.get("end_date") or e["start_date"])[:10], time=times,
            where=venue.get("city") or venue.get("venue") or "",
            online=bool(ONLINE_RX.search(where_txt + " " + title)),
            source="DDSA", kind="course" if is_course else "event",
            cost="Free" if cost.lower().startswith("free") or cost.strip() in ("0", "0 DKK") else "",
            **info))
    notes = [f"{n} {k}" for k, n in why.most_common()]
    if followed:
        notes.append(f"deadline looked up on {plural(followed, 'original page')}")
    return out, f" ({', '.join(notes)})" if notes else ""



# ---------- Source: NNF Science Cluster (event pages listed in the sitemap) ----------

def ev_science_cluster(c: dict, horizon: dt.date) -> tuple[list[dict], str]:
    sitemap = c.get("sitemap", "https://sciencecluster.dk/events-sitemap.xml")
    rx = keyword_regex(c.get("keywords", []))
    xml, _ = fetch_page(sitemap)
    entries = re.findall(r"<loc>\s*([^<\s]+)\s*</loc>\s*<lastmod>\s*([^<\s]+)\s*</lastmod>", xml)
    recent = sorted((e for e in entries if e[1][:10] >= (TODAY - dt.timedelta(days=365)).isoformat()),
                    key=lambda e: e[1], reverse=True)[: c.get("max_pages", 25)]
    log(f"        Science Cluster: reading {plural(len(recent), 'recently updated event page')}")
    out, past, off_topic = [], 0, 0
    for i, (url, _) in enumerate(recent, 1):
        progress(f"        Science Cluster: page {i}/{len(recent)}")
        try:
            page, _ = fetch_page(url)
        except Exception:
            continue
        time.sleep(0.3)
        text = html_text(page)
        title = page_title(page)
        # The "Date" facts, e.g. "08 Mar 2027 17:30 CET" ... "12 Mar 2027 09:00 CET" (more reliable than the header)
        facts = [(m.group(1), m.group(2)) for m in
                 re.finditer(r"(\d{1,2}\s+[A-Za-z]{3,9}\.?\s+20\d\d)\s+(\d{1,2}:\d{2})\s*(?:CET|CEST)", text)
                 if "deadline" not in text[max(0, m.start() - 40): m.start()].lower()][:2]
        if facts:
            s = find_dates(facts[0][0])[0]["start"]
            e = find_dates(facts[-1][0])[0]["start"] if len(facts) > 1 else s
            when = facts[0][1]
        else:
            ds = [d for d in find_dates(text) if d["has_year"]]
            if not ds:
                continue
            s, e, when = ds[0]["start"], ds[0]["end"], ""
        if e < TODAY:
            past += 1
            continue
        about = " ".join([title, page_summary(page), first_paragraph(page)])
        if rx and not rx.search(about):
            off_topic += 1
            left_out("Science Cluster", title, "no keyword in title or summary", url, s.isoformat())
            continue
        dl = re.search(r"Application\s+Deadline\s+(\d{1,2}\s+[A-Za-z]{3,9}\.?\s+20\d\d)", text, re.I)
        info = registration_info(text, s)
        if dl:
            info.update(deadline=find_dates(dl.group(1))[0]["start"].isoformat(), deadline_label="Application",
                        deadline_text=" ".join(dl.group(0).split()))
        place = re.search(r"\b\d{4}\s+([A-ZÆØÅ][a-zæøå]+(?:\s[A-ZÆØÅ][a-zæøå]+)?)\s+Denmark", text)
        out.append(new_event(
            title=re.sub(r"^(Conference|Lecture|Course)\s*:\s*", "", title), url=url,
            start=s.isoformat(), end=e.isoformat(), time=when, where=place.group(1) if place else "",
            online=bool(ONLINE_RX.search(title)), source="Science Cluster",
            kind="conference" if title.lower().startswith("conference") else "event",
            cost="Free" if FREE_RX.search(text) else "", **{**info, "reg": info["reg"] or "application"}))
    progress_done()
    notes = [f"{off_topic} off-topic"] if off_topic else []
    notes += [f"{past} already held"] if past else []
    return out, f" ({', '.join(notes)})" if notes else ""


# ---------- Source: Danish Diabetes and Endocrine Academy (event pages) ----------

def ev_ddea(c: dict, horizon: dt.date, rules: dict) -> tuple[list[dict], str]:
    list_url = c.get("list", "https://ddeacademy.dk/events/")
    page, _ = fetch_page(list_url)  # raises RobotsBlocked if the site says no
    links = list(dict.fromkeys(re.findall(r'href="(https?://(?:www\.)?ddeacademy\.dk/events/[a-z0-9-]+/[a-z0-9-]+/?)"', page)))
    links = links[: c.get("max_pages", 20)]
    log(f"        DDEA: reading {plural(len(links), 'event page')}")
    out = []
    for i, url in enumerate(links, 1):
        progress(f"        DDEA: page {i}/{len(links)}")
        try:
            p, _ = fetch_page(url)
        except Exception:
            continue
        time.sleep(0.3)
        text = html_text(p)
        title = page_title(p)
        # The event's own date: from the "Event date" field if there is one, otherwise the
        # first date on the page that isn't part of a deadline sentence
        d = labelled_event_date(text)
        if not d:
            dates = [x for x in _outside_deadlines(text, find_dates(text)) if x["has_year"]]
            if not dates:
                left_out("DDEA", title, "no event date found", url)
                continue
            d = dates[0]
        kind_seg = url.rstrip("/").split("/")[-2]
        out.append(new_event(
            title=title, url=url, start=d["start"].isoformat(), end=d["end"].isoformat(),
            online=bool(ONLINE_RX.search(title)), source="DDEA",
            kind="course" if "course" in kind_seg else "event",
            cost="Free" if FREE_RX.search(text) else "", **registration_info(text, d["start"])))
    progress_done()
    return out, ""



# ---------- Source: ELIXIR TeSS (training register, search API) ----------

def _tess_records(d) -> list[dict]:
    """TeSS answers either with a plain list or in JSON:API form; return plain dicts."""
    if isinstance(d, list):
        return d
    out = []
    for x in d.get("data", []):
        rec = {k.replace("-", "_"): v for k, v in (x.get("attributes") or {}).items()}
        rec.setdefault("id", x.get("id"))
        out.append(rec)
    return out


def _labels(v) -> list[str]:
    out = []
    for x in v or []:
        out.append(x.get("preferred_label") or x.get("label") or x.get("title") or "" if isinstance(x, dict) else str(x))
    return [x for x in out if x]


def ev_tess(c: dict, horizon: dt.date, rules: dict) -> tuple[list[dict], str]:
    api = c.get("api", "https://tess.elixir-europe.org/events.json")
    keywords = c.get("keywords", [])
    rx = keyword_regex(keywords, stems=True)  # "protein" also matches "proteins"
    nordic = {x.lower() for x in c.get("in_person_countries", ["Denmark", "Sweden", "Norway", "Finland", "Iceland"])}
    trusted = c.get("trusted_providers", ["ebi.ac.uk", "embl.org"])
    found: dict[str, dict] = {}
    for kw in keywords:
        for rec in _tess_records(get(api, params={"q": kw}).json()):
            found.setdefault(str(rec.get("id") or rec.get("url")), rec)
        time.sleep(0.2)
    why, looked_up = Counter(), 0
    out = []
    for e in found.values():
        title = clean_title(e.get("title"))
        url = e.get("url") or ""
        try:
            s = dt.date.fromisoformat(str(e.get("start"))[:10])
            en = dt.date.fromisoformat(str(e.get("end") or e.get("start"))[:10])
        except ValueError:
            why["no date"] += 1
            continue
        types = [str(t).lower() for t in e.get("event_types") or []]
        online_v = e.get("online")
        online = online_v is True or str(online_v).lower() in ("true", "online", "hybrid")
        country = str(e.get("country") or "").strip().lower()
        elig = [str(x).lower() for x in e.get("eligibility") or []]
        tags = " ".join(_labels(e.get("keywords")) + _labels(e.get("scientific_topics")))
        is_trusted = _matches_domain(url, trusted)
        reason = ""
        if en < TODAY or s > horizon:
            reason = "outside the time window"
        elif types and not any("course" in t or "workshop" in t for t in types):
            reason = "not a course"
        elif not online and country not in nordic and not is_trusted:
            reason = "in person outside the Nordics"
        elif any("invitation" in x for x in elig):
            reason = "by invitation only"
        elif rx and not rx.search(f"{title} {tags}"):
            reason = "weak match"
        elif rules["exclude"] and rules["exclude"].search(title):
            reason = "exclude word"
        if reason:
            why[reason] += 1
            if reason not in ("outside the time window",):
                left_out("TeSS", title, reason, url, s.isoformat())
            continue
        info = registration_info(html_text(str(e.get("description") or "")), s)
        # EMBL-EBI / EMBL courses: the application deadline is on the course page, not in TeSS
        if not info["deadline"] and is_trusted and url:
            try:
                theirs = deadline_from_page(url, s)
                looked_up += 1
                info = {k: theirs[k] or info[k] for k in info}
                if theirs["reg"] == "closed":
                    info["reg"] = "closed"
            except Exception:
                pass
        if not info["reg"]:
            info["reg"] = ("application" if any("interest" in x for x in elig)
                           else "required" if any("first_come" in x for x in elig) else "")
        basis, value = str(e.get("cost_basis") or "").lower(), e.get("cost_value")
        out.append(new_event(
            title=title, url=url, start=s.isoformat(), end=en.isoformat(),
            time=time_of(str(e.get("start"))), where=e.get("city") or ("" if online else e.get("country") or ""),
            online=online, source="TeSS", kind="course",
            cost="Free" if basis.startswith("free") else (f"Fee {value:g}" if isinstance(value, (int, float)) and value else ""),
            **info))
    notes = [f"{n} {k}" for k, n in why.most_common()]
    if looked_up:
        notes.append(f"deadline looked up on {plural(looked_up, 'course page')}")
    return out, f" ({len(found)} found: " + ", ".join(notes) + ")" if notes else ""



# ---------- Source: conference series (yearly pages or "future meetings" pages) ----------

def _year_on_page(page: str, text: str) -> int | None:
    """The year a "next meeting" page is about: from its title, or the first year near the top."""
    m = re.search(r"\b(20\d\d)\b", page_title(page)) or re.search(r"\b(20\d\d)\b", text[:4000])
    return int(m.group(1)) if m else None


def _outside_deadlines(text: str, dates: list[dict]) -> list[dict]:
    """Leave out dates that belong to a deadline sentence ("Abstract deadline 19 January")."""
    t = normalise_for_dates(text)
    spans = []
    for m in DEADLINE_RX.finditer(t):
        own = find_dates(t[m.end(): m.end() + 90], default_year=TODAY.year)
        if own:
            spans.append((m.start(), m.end() + own[0]["endpos"]))
    return [d for d in dates if not any(a <= d["pos"] < b for a, b in spans)]


def ev_conferences(series: list[dict], horizon: dt.date) -> list[dict]:
    out = []
    for c in series:
        name, url, one_day = c["name"], c["url"], bool(c.get("one_day"))
        max_days = int(c.get("max_days", 7))
        label = short(name, 34)
        try:
            found, pages_seen = [], 0
            if "{year}" in url:
                # One page per year: try this year's and next year's
                for y in (TODAY.year, TODAY.year + 1):
                    u = url.replace("{year}", str(y))
                    try:
                        page, final = fetch_page(u)
                    except requests.HTTPError:
                        continue  # not published yet
                    if str(y) not in final:
                        continue  # redirected to a general page
                    pages_seen += 1
                    text = html_text(page)
                    ok_dates = [d for d in _outside_deadlines(text, find_dates(text, default_year=y)) if d["start"].year == y
                                and (d["end"] - d["start"]).days <= max_days and (one_day or d["end"] > d["start"])]
                    if ok_dates:
                        found.append((y, ok_dates[0], text, u))
            else:
                # One page listing the next meeting(s)
                page, _ = fetch_page(url)
                pages_seen += 1
                text = html_text(page)
                year_hint = _year_on_page(page, text)
                seen_years = set()
                for d in _outside_deadlines(text, find_dates(text, default_year=year_hint)):
                    y = d["start"].year
                    if (y in seen_years or (d["end"] - d["start"]).days > max_days
                            or (not one_day and d["end"] == d["start"])):
                        continue
                    seen_years.add(y)
                    found.append((y, d, text, url))
            upcoming = [f for f in found if f[1]["end"] >= TODAY]
            if not upcoming:
                if not pages_seen:
                    msg = f"no page found for {TODAY.year} or {TODAY.year + 1} (check the address in config.yml)"
                elif not found:
                    msg = "page found, but no dates recognised"
                else:
                    msg = "only past meetings found; next year's page isn't published yet"
                fail(f"Conference · {name}", msg, label)
                continue
            shown = []
            for y, d, text, u in upcoming:
                info = registration_info(text, d["start"]) if len(upcoming) == 1 or "{year}" in url else \
                    {"deadline": "", "deadline_label": "", "reg": ""}
                title = name if str(y) in name else f"{name} {y}"
                out.append(new_event(title=title, url=u, start=d["start"].isoformat(), end=d["end"].isoformat(),
                                     source="Conference", kind="conference", **info))
                rng = d["start"].strftime("%d %b %Y") if d["start"] == d["end"] else \
                    f"{d['start'].strftime('%d %b')}–{d['end'].strftime('%d %b %Y')}"
                shown.append(rng + (f", {info['deadline_label'].lower()} by {info['deadline']}" if info["deadline"] else ""))
            ok(f"Conference · {name}", len(upcoming), label, "; ".join(shown))
        except RobotsBlocked as e:
            fail(f"Conference · {name}", e, label)
        except Exception as e:
            fail(f"Conference · {name}", e, label)
    return out


# ---------- Putting it together ----------

def _norm_url(u: str) -> str:
    return re.sub(r"^https?://(www\.)?", "", (u or "").strip().lower()).rstrip("/")


def merge_duplicates(events: list[dict]) -> list[dict]:
    """The same event from two sources (e.g. a DDEA course also listed by DDSA) becomes one entry.
    Matched by link or by title + start date; missing details are filled in from either copy."""
    merged: list[dict] = []
    for e in events:
        urls = {_norm_url(u) for u in [e["url"], *e.get("alt_urls", [])] if u}
        key = (re.sub(r"\W+", "", plain(e["title"]).lower())[:40], e["start"])
        twin = next((m for m in merged if urls & m["_urls"] or key == m["_key"]), None)
        if not twin:
            merged.append({**e, "_urls": urls, "_key": key})
            continue
        twin["_urls"] |= urls
        if e["source"] not in twin["source"]:
            twin["source"] += f" + {e['source']}"
        if "ddsa.dk" in twin["url"] and "ddsa.dk" not in e["url"]:
            twin["url"] = e["url"]  # prefer the original page over DDSA's copy
        for f in ("deadline", "deadline_label", "deadline_text", "cost", "where", "time"):
            twin[f] = twin[f] or e[f]
        if e["reg"] == "closed" or not twin["reg"]:
            twin["reg"] = e["reg"] or twin["reg"]
    return [{k: v for k, v in m.items() if not k.startswith("_")} for m in merged]


def fetch_events(cfg: dict) -> dict | None:
    ec = cfg.get("events")
    if not ec:
        log("    --  Coming up                           not set up (no events: section in config.yml)")
        return None
    window = int(ec.get("window_days", 120))
    conf_window = int(ec.get("conference_window_days", 365))
    grace_days = int(ec.get("hide_days_after_deadline", 7))
    no_deadline_days = int(ec.get("hide_days_before_start_without_deadline", 10))
    horizon = TODAY + dt.timedelta(days=window)
    conf_horizon = TODAY + dt.timedelta(days=conf_window)
    rules = {"exclude": keyword_regex(ec.get("exclude_words", []))}
    hide_list = [str(x).lower() for x in ec.get("hide") or [] if str(x).strip()]

    # 1) Collect from every source. Sources look up to a year ahead, because a course far ahead
    #    is still shown while its application deadline is near (see the window rule below).
    collected: list[dict] = []
    sources = [("DDSA", "ddsa", ev_ddsa), ("Science Cluster", "science_cluster", ev_science_cluster),
               ("DDEA", "ddea", ev_ddea), ("TeSS", "tess", ev_tess)]
    for label, key, fn in sources:
        c = ec.get(key)
        if not c or c.get("enabled") is False:
            continue
        try:
            if fn is ev_science_cluster:
                items, note = fn(c, conf_horizon)
            else:
                items, note = fn(c, conf_horizon, rules)
            ok(f"Events · {label}", len(items), label, f"{plural(len(items), 'event')}{note}")
            collected += items
        except RobotsBlocked as e:
            fail(f"Events · {label}", e, label)
        except Exception as e:
            fail(f"Events · {label}", e, label)
    if ec.get("conferences"):
        log("        conference series:")
        collected += ev_conferences(ec["conferences"], conf_horizon)

    # 2) Merge duplicates first, so a deadline found by one source applies to the other copy too
    candidates = merge_duplicates(collected)

    # 3) The rules
    grace = dt.timedelta(days=grace_days)
    kept, dropped = [], Counter()
    for e in candidates:
        s, en = dt.date.fromisoformat(e["start"]), dt.date.fromisoformat(e["end"])
        dl = dt.date.fromisoformat(e["deadline"]) if e["deadline"] else None
        is_conf = e["kind"] == "conference"
        if en < TODAY:
            reason = "ended"
        elif any(h in plain(e["title"]).lower() or h in e["url"].lower() for h in hide_list):
            reason = "on the hide list"
        elif e["reg"] == "closed":
            reason = "registration closed"
        elif dl and dl < TODAY - grace:
            reason = f"deadline passed more than {grace_days} days ago"
        elif is_conf and s > conf_horizon:
            reason = "more than 12 months ahead" if conf_window == 365 else "too far ahead"
        elif not is_conf and s > horizon and not (dl and dl <= horizon):
            reason = "too far ahead"  # a far-off course is only shown while its deadline is near
        elif not is_conf and not dl and (s - TODAY).days < no_deadline_days:
            reason = f"starts within {no_deadline_days} days and no deadline found"
        else:
            reason = ""
        if reason:
            dropped[reason] += 1
            EVENT_REPORT.append({"status": "left out", "reason": reason, **{k: e[k] for k in
                                 ("source", "title", "start", "url", "deadline", "deadline_text")}})
        else:
            kept.append(e)
            EVENT_REPORT.append({"status": "shown", "reason": "", **{k: e[k] for k in
                                 ("source", "title", "start", "url", "deadline", "deadline_text")}})

    items = sorted(({k: v for k, v in e.items() if k not in ("alt_urls", "deadline_text")} for e in kept),
                   key=lambda x: (x["start"], x["title"]))
    urgent_days = int(ec.get("urgent_days", 14))
    urgent = sum(1 for e in items if e["deadline"] and 0 <= (dt.date.fromisoformat(e["deadline"]) - TODAY).days <= urgent_days)
    log(f"        Coming up: {plural(len(items), 'event')}, {urgent} with a deadline within {urgent_days} days")
    for reason, n in dropped.most_common():
        log(f"          left out: {n} {reason}")
    return {"items": items, "show": int(ec.get("show", 6)), "urgent_days": urgent_days,
            "hide_days_after_deadline": grace_days, "hide_days_before_start_without_deadline": no_deadline_days}


def write_event_report(path: pathlib.Path) -> None:
    """A plain-text overview of every event considered, published next to the page."""
    lines = [f"Coming up - event report, built {dt.datetime.now().strftime('%Y-%m-%d %H:%M')}",
             "Every event the build looked at, with the deadline it found and why anything was left out.", ""]
    for status in ("shown", "left out"):
        rows = [r for r in EVENT_REPORT if r["status"] == status]
        lines.append(f"=== {status.upper()} ({len(rows)}) ===")
        for r in sorted(rows, key=lambda r: (r["reason"], r["start"] or "9999", r["title"])):
            dl = f"deadline {r['deadline']} (\"{r['deadline_text']}\")" if r["deadline"] else "no deadline found"
            why = f"  [{r['reason']}]" if r["reason"] else ""
            lines.append(f"{r['start'] or '----------'}  {r['source']:<18} {short(r['title'], 70)}{why}")
            if status == "shown" or r["deadline"]:
                lines.append(f"{'':12}{dl}")
            if r["url"]:
                lines.append(f"{'':12}{r['url']}")
        lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")


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
        n = 6
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
        step(5, n, "Coming up (courses, events and conferences)")
        events = fetch_events(cfg)
        data = {**base, "paper_widgets": papers, "news": news, "group_pubs": group, "daily": daily,
                "events": events, "status": STATUS}

    step(n, n, "Writing the page")
    render(data, args.out, args.fragment)
    log(f"    ok  {rel(args.out / 'index.html')}")
    if EVENT_REPORT:
        write_event_report(args.out / "events_report.txt")
        log(f"    ok  {rel(args.out / 'events_report.txt')} (what the calendar showed and left out, and why)")

    bad = [s for s in data.get("status", []) if not s["ok"]]
    say(("\n" if VERBOSE else "") + f"Done in {time.time() - T0:.0f} s. "
        + (f"{len(bad)} source(s) had problems:" if bad else "All sources updated."))
    for s in bad:
        say(f"  - {s['name']}: {s['error']}")
    say(f"Open {rel(args.out / 'index.html')} in your browser to see the result.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
