# How the dashboard build works

This document explains what `scripts/build.py` does, step by step, and what the main
functions are for. It is meant for whoever maintains the dashboard. For setting it up,
see `README.md`; for changing what is shown, see the comments in `config.yml`.

---

## The big picture

The dashboard is a single static web page. Nothing runs on a server. Instead, once a day
GitHub Actions runs `build.py`, which:

1. reads `config.yml`,
2. fetches fresh data from a set of public sources,
3. puts all of it into one JSON object,
4. pastes that JSON into `template/index.html` and saves the result as `site/index.html`.

The page's own JavaScript then draws everything from that embedded data when someone opens it.

```
                 config.yml
                     │
                     ▼
┌──────────────── build.py ────────────────┐
│ 1  New papers ........ Europe PMC         │
│ 2  News feeds ........ RSS/Atom feeds     │──► data (one JSON object)
│ 3  Group papers ...... OpenAlex + PubMed  │            │
│ 4  Right column ...... Protein Spotlight, │            ▼
│                        ChEMBL + UniProt,  │   template/index.html
│                        Our World in Data, │            │
│                        RCSB PDB-101       │            ▼
│ 5  Coming up ......... DDSA, Science      │   site/index.html  (+ data.json, img/)
│                        Cluster, DDEA,     │
│                        TeSS, conference   │
│                        websites           │
│ 6  Write the page                         │
└───────────────────────────────────────────┘            │
                                                         ▼
                                                   GitHub Pages
```

### Files

| File | What it is |
|---|---|
| `config.yml` | Everything you are expected to change: searches, feeds, journals, links, ORCIDs |
| `scripts/build.py` | The build script described here |
| `template/index.html` | The page design (HTML, CSS and JavaScript). Contains the placeholder `/*__DATA__*/null` where the data goes |
| `sample_data.json` | Saved example data, used with `--sample` to build the page without internet |
| `.github/workflows/build.yml` | Tells GitHub when to run the build (daily, on every change, or by hand) and publishes `site/` |
| `site/` | The output. Created by the build; not stored in the repository |

### Running it

```bash
python scripts/build.py                            # live build: fetch everything
python scripts/build.py --quiet                    # same, but only print the summary
python scripts/build.py --sample sample_data.json  # use saved data, no internet needed
python scripts/build.py --out somewhere/else       # write the page to another folder
```

---

## Design principles

These explain most of the choices in the code.

- **One failing source never breaks the page.** Every source is fetched inside its own
  `try/except`. If it fails, that box shows "could not be read" and the rest of the page
  is built as normal.
- **Everything is recorded.** Each source adds a line to a status list (`STATUS`), via
  `ok()` or `fail()`. That list is printed at the end of the build and shown in the page
  footer ("3 of 18 sources had problems · show details").
- **Polite to other people's servers.** All requests identify the script (`UA`), wait and
  retry when a server says it is busy, and pause briefly between article-page lookups.
- **Free sources only.** Every source used is public and free. Only OpenAlex needs a
  (free) API key, which is read from the `OPENALEX_API_KEY` environment variable (a GitHub
  secret) or a local `.openalex_key` file, never from `config.yml`.
- **Keys never leak.** `redact()` masks `api_key=…` in every error message before it is
  printed or stored, because error messages end up on the public page.
- **Copyrighted images are never copied.** News thumbnails and Protein Spotlight images
  are shown straight from the publisher's site. Only openly licensed images (Our World in
  Data, PDB-101) are downloaded and stored with the page.

---

## Step by step

### Step 0: Setup (`main`)

`main()` reads the command-line options, loads `config.yml` with PyYAML, and starts a
`base` dictionary with the things that don't need fetching: group name, quick links,
journal lists and the build time.

With `--sample`, it loads `sample_data.json` instead of fetching and jumps to step 5.
Otherwise it runs steps 1–5 below.

### Step 1: New papers (`fetch_papers`, `fetch_tab`)

Fills the two "New papers" widgets (Biomarkers & proteomics, Metabolism & treatment).
Each tab in `config.yml` becomes one search in **Europe PMC**, a free literature database
that contains all of PubMed plus preprints.

For each tab:

1. **`build_query()`** turns the tab's settings into a Europe PMC search string:
   - every group in `all_of` becomes `(term OR term OR …)`, searched in title and abstract
     (or title only, if `fields: [TITLE]`),
   - the groups are joined with `AND`,
   - `none_of` terms are added as `AND NOT (...)`,
   - it is limited to PubMed records and preprints (`SRC:MED OR SRC:PPR`) and to the
     last `window_days` days (`FIRST_PDATE:[from TO today]`),
   - for `journals: key` tabs, a `JOURNAL:"…" OR …` clause with all journals from the two
     journal lists is added.
2. **`epmc_search()`** sends the search and returns the raw records, newest first.
3. If a key-journals search returns nothing (which can happen if Europe PMC spells a
   journal name differently), it searches again without the journal clause and filters
   locally instead.
4. Each record is converted by **`to_paper()`** into a small, uniform dictionary: title,
   short author list, journal, date, preprint yes/no, review yes/no, journal tier
   (`top`, `key` or none) and a link (the DOI if there is one).
5. Duplicates (same DOI or title) and corrections/errata are dropped, and the list is
   sorted by date and cut to `limit`.

The tier is what powers the "Key journals" filter button and the "Top tier" badge on the
page. It is looked up with **`norm_journal()`**, which lower-cases journal names so that
"Diabetes Care" and "diabetes care." match.

### Step 2: News feeds (`fetch_news`)

Fills the "Science & research news" widget, one tab per feed in `config.yml`.

For each feed:

1. The feed is downloaded and parsed with **feedparser**, which understands RSS and Atom.
2. **Images.** Feeds put images in many different places. **`raw_item_images()`** scans
   the raw feed text for each item and takes the first image it finds in, in order:
   `media:content`, `media:thumbnail`, `webfeeds:featuredImage` (eLife), an image
   `enclosure` (Knowable), or an `<img>` inside the article text (Retraction Watch).
   Emoji, tracking pixels and avatars are skipped (`JUNK_IMG`).
3. **Title filter.** Items whose title matches a pattern in `exclude_titles` are removed
   (for example `^STAT\+:` for paywalled STAT+ stories).
4. **Article pages.** If the feed has `fetch_images: true` (Nature, Science) or an
   `exclude_types`/`include_types` filter, the script opens each item's article page with
   **`page_info()`**. That one request gives two things:
   - the page's preview image (`og:image` or `twitter:image`, the image used for link
     previews on social media), and
   - the article type (e.g. "News", "Correspondence"), read from the page's metadata.
   Items of an excluded type are dropped. To make sure the tab still fills up, the script
   reads up to three times `limit` items from the feed and stops opening pages once it has
   kept `limit` items.
5. **Logo.** The feed's own logo (or the `logo:` from `config.yml`) is stored with the tab.
   The page shows it when an item has no image. If there is no logo either, it shows the
   source name on a tinted tile.

### Step 3: Group publications (`fetch_group_pubs`)

Fills "Latest publication" and "Papers per year". It uses two sources and merges them,
because each catches papers the other misses:

| | OpenAlex | PubMed (via Europe PMC) |
|---|---|---|
| Speed | New papers appear within days of the DOI being registered | New papers appear when PubMed has indexed them, which can take weeks |
| Coverage | Also journals that PubMed doesn't index | Biomedicine only |
| Searched by | ORCID | ORCID, plus the optional `extra_query` (e.g. a name search) |

What happens:

1. **OpenAlex** (`openalex_api_key`, `openalex_group_works`, `from_openalex`). One search for
   all ORCIDs in `config.yml`, limited to `type` article or review and to journals (so
   preprints, conference abstracts, book chapters, editorials and letters are excluded).
   The API key is added as the `api_key` parameter. Results come in pages of 200; the
   script follows the pages until it has them all. If OpenAlex rejects the combined filter
   (HTTP 400), it retries with the ORCID filter only and applies the type and journal rules
   itself. If OpenAlex refuses the request (HTTP 401/402/403/409, usually a missing or wrong
   key), the build says so and continues with PubMed only.
2. **PubMed via Europe PMC.** One search for the same ORCIDs (`AUTHORID:"…"`), plus
   `extra_query` if set, limited to PubMed records (`SRC:MED`). Editorials, comments,
   letters, errata and similar are dropped using the PubMed publication type
   (`EPMC_SKIP_TYPES`); reviews are kept.
3. **Merge.** Papers are matched by DOI (**`norm_doi()`** removes `https://doi.org/` and
   lower-cases it), or by title if there is no DOI (**`paper_key()`**). For papers found in
   both, the earliest date is kept and both sources are recorded. OpenAlex's version is
   preferred for the display text because it has full journal names.
4. **Report.** The terminal shows how many papers were in both sources, only in OpenAlex and
   only in PubMed, and lists recent papers that are "not yet in PubMed".
5. **Latest publication** is the paper with the newest date that is not in the future
   (journals sometimes date a print issue ahead). The page shows which sources found it.
6. **Papers per year** counts the merged papers by year, for the last eight years.

If there are no ORCIDs in `config.yml`, or both sources fail, **`example_group_pubs()`**
returns a clearly marked example instead.

### Step 4: Right column

Each box has its own function. All of them return `None` if switched off or failing, and
the page then hides the box or shows "Not available".

**Protein Spotlight** (`fetch_spotlight`): only if `daily.spotlight_feed` is set.

1. Finds the newest issue from the Protein Spotlight feed. If the feed can't be read, it
   reads the issue list on the Protein Spotlight website and takes the highest issue number.
2. Opens the article page and reads its preview image, its date ("October 2026"), its first
   real paragraph (**`first_paragraph()`** skips menus and captions) and the UniProt
   accession numbers it links to (matched with the official accession pattern, `UNIPROT_ACC`).
3. Looks up the name and species of up to three featured proteins in UniProt.

**Target of the day** (`fetch_target_of_day`): only if `daily.target_of_the_day` is true.

1. Downloads the list of approved drugs (`max_phase = 4`) and their mechanisms of action from
   **ChEMBL** (**`chembl_all()`** follows ChEMBL's pages of 1,000 records).
2. Groups the mechanisms by target, so each target has a list of approved drugs.
3. Picks one target for today. The targets are shuffled once in a fixed order, and the
   day number decides the position, so the choice changes daily but is the same for
   everyone all day.
4. Skips targets that aren't a single human protein, then looks the protein up in
   **UniProt** (**`describe_uniprot()`**: gene, protein name, first sentences of its function,
   and links to UniProt, Open Targets and Protein Atlas).
5. Adds the drugs' approved indications from ChEMBL.
6. If ChEMBL can't be reached, it uses `FALLBACK_TARGETS`, a fixed list of well-known drug
   targets, and shows only the UniProt information.

**Our World in Data** (`fetch_owid`): reads the newest Data Insight from the OWID feed, with
its chart image and first paragraph. The chart is downloaded with **`save_image()`** into
`site/img/`.

**PDB-101 Molecule of the Month** (`fetch_motm`): reads the newest item from the PDB-101
feed, then opens the article page for its preview image, which is also saved to `site/img/`.

### Step 5: Coming up (`fetch_events`)

Fills the "Coming up" calendar in the left column with courses, events and conferences. Each
source has its own function, and they all return events in the same form (`new_event()`):
title, link, start and end date, time, place, online yes/no, source, kind
(`course`, `event` or `conference`), registration deadline and its kind, registration type and cost.

**The sources**

| Function | Source | How it is read |
|---|---|---|
| `ev_ddsa()` | Danish Data Science Academy | DDSA's public events API (a standard WordPress events plugin). Keeps the categories in `config.yml` (DDSA-organised and -funded events) plus PhD courses with one of the listed tags **and** a topic word in the title or description. When DDSA only re-posts an event, the dashboard links to the original page, and for the sites in `follow_links_to` the deadline is read from that page |
| `ev_science_cluster()` | NNF Science Cluster | The site's list of event pages (`events-sitemap.xml`). Pages changed in the last year are opened; the start and end come from the page's "Date" facts and the deadline from "Application Deadline". Only events whose title or summary matches a keyword are kept |
| `ev_ddea()` | Danish Diabetes and Endocrine Academy | The events overview page and each event page. The event date comes from the "Event date" field (`labelled_event_date`), the deadline from the "Deadline" field. Skipped automatically if the site's robots.txt forbids automated reading |
| `ev_tess()` | ELIXIR TeSS | One search per keyword in TeSS's events API. Kept: courses and workshops, in the Nordics or online (or anywhere, for the `trusted_providers` EMBL-EBI and EMBL), not invitation-only, and with a keyword in the title or topic tags. For trusted providers the application deadline is read from the course page |
| `ev_conferences()` | Conference websites | See below |

**Conference series.** Each conference in `config.yml` has one address:
- an address with `{year}` (e.g. `easd-{year}`) is tried for this year and next year. A page that
  doesn't exist yet (404), or that redirects to a page without the year, is skipped;
- an address without `{year}` is a page that lists the next meeting(s); every date range on it
  becomes one edition.

Only date ranges of at most 7 days count as meetings (`max_days`), which filters out things like
"abstract submission opens …" or a mistyped range. `one_day: true` allows single-day meetings.

**Reading dates from text** (`find_dates`). Pages write dates in many ways. `normalise_for_dates()`
first removes weekday names and commas, turns every kind of dash and the word "to" into "-",
and removes ordinal endings ("1st"). `DATE_PATTERNS` then matches, from most to least specific:
"30 May - 2 June 2029", "16-19 June 2027", "May 30 - June 2 2029", "June 18-21 2027",
"2026-11-05", "8 December 2026" and "December 8 2026", and dates written with numbers as on
Danish sites ("05.11.2026", "04/10/2026", "05.-06.11.2026", always read as day.month.year).
Times after a date ("05.11.2026 - 09:00") are ignored. English and Danish month names are
understood. A date without a year gets the year of the page (from its address or title).

**Registration** (`registration_info`). Looks for deadline phrases ("application deadline",
"register before", "abstract submission deadline", "tilmeldingsfrist", …) followed by a date.
A deadline without a year is placed before the event ("19 January" before a June congress).
If several deadlines are found, the next one that hasn't passed is shown. Without a date, the
text decides the label: "by application", "registration open", "sign-up required" or
"no sign-up needed". Wordings like "registration closed", "fully booked" or "waiting list only"
mark the event as closed (`CLOSED_RX`). `_outside_deadlines()` makes sure a deadline date is never
mistaken for the event's own date.

**Keywords** (`keyword_regex`). `metabol*` matches every word starting with "metabol"; words in
capitals (`AI`) must match exactly, so "AI" doesn't match "air" or "said".

**Robots.txt** (`allowed_by_robots`, `fetch_page`). Pages that are read as web pages rather than
through an API are only fetched if the site's robots.txt allows automated reading.

**Combining** (in `fetch_events`):
1. The same event from two sources (same link, or same title and start date) is merged first
   (`merge_duplicates`): the sources are combined ("DDSA + DDEA"), the original page's link is
   kept, and missing details such as the deadline are filled in from either copy. This happens
   before the rules, so a deadline found on DDEA also removes DDSA's copy of the event.
2. Then these are removed, in this order:
   - events that have ended;
   - events matching the `hide` list in `config.yml`;
   - events whose registration is closed;
   - events whose deadline passed more than `hide_days_after_deadline` (7) days ago;
   - conferences more than `conference_window_days` (365) ahead;
   - courses and events more than `window_days` (120) ahead, **unless** their deadline falls
     within those 120 days (so a course next spring shows while you can still apply);
   - courses and events (not conferences) with no known deadline that start within
     `hide_days_before_start_without_deadline` (10) days.
3. The rest is sorted by start date.
4. `write_event_report()` writes `events_report.txt` next to the page: every event considered,
   the deadline it found (with the sentence it came from) and why anything was left out. Open
   `<your page address>/events_report.txt` to check it.

The page works out the registration label itself ("Apply by 15 Nov", "Deadline in 5 days",
"Deadline passed 2 days ago"), using the viewer's date. That way the label is correct over a
weekend, when the page isn't rebuilt.

### Step 6: Writing the page (`render`)

1. Reads `template/index.html`.
2. Converts all data to JSON and replaces the placeholder `/*__DATA__*/null` with it.
   (Any `</` in the data is escaped so it can't accidentally close the `<script>` tag.)
3. Wraps it in a minimal HTML document and writes `site/index.html`.
4. Also writes `site/data.json`, the same data in readable form, which is handy for
   checking what the build fetched.

Finally `main()` prints the summary: build time, and every source that had a problem.

---

## Helper functions

| Function | What it does |
|---|---|
| `redact(text)` | Masks API keys in messages before they are printed or stored |
| `get(url, …)` | Every request goes through this. Adds the `User-Agent`, waits 40 s at most, and retries up to three times when a server answers "busy" (HTTP 429 or 5xx), respecting the server's `Retry-After` |
| `log()`, `say()` | Print a message. `log` is hidden with `--quiet`; `say` always prints (used for the summary) |
| `progress()`, `progress_done()` | Counters that update in place in a terminal (e.g. "page 7/15"). In GitHub's log, where that doesn't work, only the final line is printed |
| `step()` | Prints a step heading such as `[2/5] News feeds` |
| `ok()`, `fail()` | Record a source's result in `STATUS` and print the `ok` / `!!` line |
| `short()`, `plural()` | Shorten long text for the terminal; write "1 paper" / "2 papers" |
| `short_authors()` | Turns a long author list into "First et al." or "First, Second, Third, …, Last" |
| `entry_date()` | The publication date of a feed item as `YYYY-MM-DD` |
| `term()` | Puts quotes around search terms that contain spaces or hyphens |
| `rel()` | Shows file paths relative to the current folder in messages |
| `find_dates()`, `normalise_for_dates()` | Find dates and date ranges in ordinary text (see step 5) |
| `registration_info()` | Deadline and registration type from ordinary text (see step 5) |
| `fetch_page()`, `allowed_by_robots()` | Fetch a web page, but only if the site's robots.txt allows it |
| `html_text()`, `page_title()`, `page_summary()` | Turn a web page into plain text; read its title and short description |

---

## The data passed to the page

`site/data.json` shows the full structure. In outline:

```json
{
  "group":         { "name": "...", "subtitle": "..." },
  "generated":     "2026-10-22T04:17+00:00",
  "links":         [ { "group": "Literature", "items": [ { "name", "url", "note" } ] } ],
  "journals":      { "top_tier": [...], "key_field": [...] },
  "paper_widgets": [ { "title": "Biomarkers & proteomics",
                       "tabs": [ { "label", "window_days", "key_only",
                                   "items": [ { "title", "authors", "journal", "date",
                                                "preprint", "review", "tier", "url" } ] } ] } ],
  "news":          [ { "label": "Nature", "logo": "...",
                       "items": [ { "title", "url", "source", "date", "image", "type" } ] } ],
  "group_pubs":    { "example": false,
                     "latest": { "title", "authors", "journal", "date", "url", "sources" },
                     "per_year": [ { "year": 2026, "n": 21 } ] },
  "events":        { "items": [ { "title", "url", "start", "end", "time", "where", "online", "source",
                                "kind", "deadline", "deadline_label", "reg", "cost" } ],
                     "show": 6, "urgent_days": 14, "hide_days_after_deadline": 7 },
  "daily":         { "spotlight": {...}, "target": {...}, "owid": {...}, "motm": {...},
                     "spotlight_enabled": true, "target_enabled": false },
  "status":        [ { "name": "News · Nature", "ok": true, "count": 15 } ]
}
```

A tab or box that failed has an `"error"` field instead of (or as well as) its items.

`sample_data.json` has the same structure, so you can edit it to preview design changes
without fetching anything.

---

## Common changes

**Add a news feed.** Add an entry under `news:` in `config.yml` with `label`, `url` and
`limit`. Add `fetch_images: true` if the feed has no images.

**Add a conference.** Add a line under `events: conferences:` in `config.yml` with its name and
the address of its yearly page (with `{year}`) or of its "future meetings" page. Run the build and
check the conference's line in the terminal.

**Add or change a paper tab.** Edit `paper_widgets:` in `config.yml`. The comments at the top
of that section explain `all_of`, `none_of`, `fields`, `journals` and `window_days`.
Europe PMC's search syntax is documented at <https://europepmc.org/searchsyntax>.

**Add a box to the right column.**
1. In `build.py`, write a `fetch_something(cfg)` function that returns a small dictionary,
   or `None` if it fails. Call `ok()` or `fail()` so it appears in the status list. The
   Molecule of the Month function (`fetch_motm`) is the simplest model to copy.
2. Add it to the `daily = {...}` block in `main()`.
3. In `template/index.html`, add a widget in the right-hand `<section>`, plus a JavaScript
   function that fills it from `DASH.daily.something` (copy `motm()`), and call it at the
   bottom of the script.

**Change the design.** Everything visual is in `template/index.html`. Colours are defined
once, at the top of the `<style>` block, for light and dark mode. To try changes quickly, use
`python scripts/build.py --sample sample_data.json` and refresh `site/index.html` in the
browser.

---

## Troubleshooting

| Symptom | Likely cause | What to do |
|---|---|---|
| `!!  … HTTP 429` | The source is rate-limiting | Usually temporary; the next daily build will work. The script already retries three times |
| A paper tab is always empty | The search is too narrow, or journal names don't match | Widen `window_days`, add synonyms, or check the journal abbreviation in PubMed's NLM Catalog |
| A paper tab is very noisy | The search is too broad | Add `none_of` terms, or switch the tab to `journals: key` |
| `!!  OpenAlex  HTTP 401` | No OpenAlex API key, or a wrong one | Add the `OPENALEX_API_KEY` secret (see the README) |
| `!!  <conference>  no page found for …` | The society moved its pages | Find the new address and update `config.yml` |
| `!!  <conference>  page found, but no dates recognised` | The dates are written in an unusual way, or are shown as an image | Check the page; if needed, add the format to `DATE_PATTERNS` |
| `!!  DDEA  … does not allow automated reading` | The site's robots.txt forbids it | Nothing to fix: DDEA events still appear when DDSA lists them |
| An irrelevant course in Coming up | A broad DDSA tag or TeSS keyword | Check `events_report.txt`, then remove the tag or keyword, add an `exclude_words` entry, or add it to `hide` in `config.yml` |
| A passed deadline is still shown | The deadline wasn't recognised | Look the event up in `events_report.txt` ("no deadline found") and check how its page writes the deadline |
| A recent group paper is missing | Not in OpenAlex or PubMed yet, or the member's ORCID isn't linked to it | Check the paper's page in OpenAlex; ask the member to link the paper in their ORCID record |
| News images missing | The publisher blocks automated page requests, or changed its pages | The page falls back to the logo or name tile; nothing to fix unless it's permanent |
| `could not read the article type` | Nature changed where pages state the article type | The filter then keeps all items; update `TYPE_PATTERNS` in `build.py` |
| The whole page didn't update | The GitHub Action didn't run | Check the Actions tab. GitHub pauses daily runs after 60 days without changes to the repository; any edit restarts them |
