# Wewer Albrechtsen Group dashboard

A start page for the group: new papers in the group's fields, science news, the group's
latest publication, a calendar of courses and conferences, and daily boxes (Protein
Spotlight, a data insight from Our World in Data, and PDB-101's Molecule of the Month).
A second page lists all of the group's publications, with search and filters.

It is a static web page. Once a day, GitHub Actions fetches fresh data and republishes
the page on GitHub Pages. There is no server to run and it costs nothing.

```
config.yml ─┐                     ┌─ Europe PMC        (new papers; PubMed cross-check)
template/  ─┼─► scripts/build.py ─┼─ OpenAlex          (group publications)
            │         │           ├─ RSS feeds         (news)
            │         │           ├─ Protein Spotlight, Our World in Data, PDB-101
            │         │           └─ DDSA, DDEA, TeSS, … (courses and conferences)
            │         ▼
            │   site/index.html, site/publications.html
            └──────────► GitHub Pages
```

How the build works, step by step, is described in [`docs/HOW_IT_WORKS.md`](docs/HOW_IT_WORKS.md).

---

## 1. Look at the example first (optional)

Open `example.html` in your browser. It shows the layout with example data;
no installation is needed.

## 2. Put it on GitHub (about 15 minutes)

You need a free GitHub account. Ideally create a GitHub *organisation* for the group,
so the dashboard doesn't depend on one person's account.

1. **Create a repository.** On github.com click **New repository**. Give it a name such as
   `dashboard`, choose **Public**, and click **Create repository**.
2. **Upload the files.** On the new repository's page click **uploading an existing file**.
   Drag in *everything inside* the `research-dashboard` folder, including the hidden
   `.github` folder. On a Mac, press `Cmd + Shift + .` in Finder to show hidden folders.
   Click **Commit changes**.
   - Check that the file `.github/workflows/build.yml` now appears in the repository.
     If it doesn't, create it via **Add file → Create new file**, type
     `.github/workflows/build.yml` as the name, and paste in the file's contents.
3. **Turn on GitHub Pages.** Go to **Settings → Pages**. Under *Build and deployment → Source*,
   choose **GitHub Actions**.
4. **Run the first build.** Go to the **Actions** tab. If asked, click
   **I understand my workflows, go ahead and enable them**. Click
   **Build and publish dashboard → Run workflow → Run workflow**.
   It takes 1–3 minutes, and a green tick means it worked.
5. **Open the page.** It's at `https://<your-account>.github.io/<repository-name>/`.
   The link is also shown under **Settings → Pages**.
6. **Share it.** Send the link to the group and suggest setting it as their browser's start page.

From now on the page rebuilds every morning at 04:17 UTC (06:17 Copenhagen time in summer,
05:17 in winter), and whenever you change a file. To change the time, edit the `cron:` line
in `.github/workflows/build.yml`.

## 3. Add the group's publications

Open `config.yml` on GitHub, click the pencil icon, and fill in these settings:

```yaml
# at the top of the file: the address of the published page
site_url: https://alepilhen.github.io/WewerAlbrechtsen_homepage/

group:
  # The PI. Sets the start of the list: the first paper with this person as last author
  pi_orcid: "0000-0003-4230-5753"
  # Group members. Their papers are searched for and their names shown in colour
  orcids:
    - "0000-0002-1234-5678"
    - "0000-0001-8765-4321"
  # Optional. A paper belongs to a section when the lead is one of the last two authors
  sections:
    - {name: "Glucagon", lead: "0000-0002-1234-5678"}
  # Optional. DOIs of papers to leave out (e.g. a correction notice listed as an article)
  exclude_papers: []
```

Click **Commit changes**. The page rebuilds within a few minutes. The "Latest publication"
box and the chart switch from example to real data, and an **All publications →** link
appears, leading to the publications page.

**The publications page** lists every paper since the PI's first last-author paper. You can
search titles and abstracts, filter by year, journal, type, group member (and their author
position: first, last or one of the last two) and section, sort the list, and download what
you see as a spreadsheet (CSV) or for a reference manager (BibTeX). The list is also saved as
`publications.json` next to the page.

**Where the papers come from.** They are found in two places and merged: **OpenAlex**, which
picks up new papers within days of publication, and **PubMed** (via Europe PMC), as a
cross-check and the main source of abstracts. Journal articles and reviews are included, and
JoVE papers are included with the label "Methods". Preprints, conference abstracts,
editorials and letters are not. A paper is only found if a member's ORCID is linked to it; if
papers are missing, add a name search for PubMed as well, e.g.
`extra_query: 'AUTH:"Wewer Albrechtsen NJ"'`.

**If a source is down.** When OpenAlex or PubMed fails, the build keeps the papers from the
previously published list (read from `site_url`), so nothing disappears from the page for a day.

### Give the build an OpenAlex key (free, 5 minutes)

OpenAlex requires a free API key. Without one, the OpenAlex search fails with
`HTTP 401` and only PubMed is used, so very recent papers are missed.

1. Create a free account at [openalex.org](https://openalex.org) and copy your key from
   [openalex.org/settings/api](https://openalex.org/settings/api).
2. **For GitHub:** in the repository, go to **Settings → Secrets and variables → Actions →
   New repository secret**. Name it `OPENALEX_API_KEY`, paste the key, and click **Add secret**.
   The workflow passes it to the build. Secrets are never shown in logs or on the page.
3. **On your own computer:** either run `export OPENALEX_API_KEY=your-key` in the terminal
   before building, or save the key in a file called `.openalex_key` in the project folder.
   That file is listed in `.gitignore`, so it is never uploaded.

Never put the key in `config.yml`: the repository is public.

The free key allows 100,000 "credits" a day. One build uses about 10–30, so the limit
won't be a problem.

## 4. Adjust what the page shows

Everything is in `config.yml`:

| To change | Edit |
|---|---|
| Paper categories and search terms | `paper_widgets:` (instructions are at the top of that section) |
| Which journals count as "key" and "top tier" | `journals:` |
| How far back to look for papers | `window_days:` (overall, or per tab) |
| News feeds | `news:` (any RSS or Atom feed) |
| Quick links | `links:` |
| Group members, sections and papers to leave out | `group:` |
| Calendar ("Coming up"): sources, keywords, conferences, time windows | `events:` |
| Turn off a daily box | `daily:` (remove the line or set it to `false`) |

**Tips for the paper searches**
- A tab with `journals: key` only shows papers from the two journal lists. The tabs for
  incretin drugs, MASLD/MASH, murine models and clinical trials are set up this way because
  they would otherwise be flooded.
- If a tab is noisy, add terms to `none_of:`, make the terms more specific, or switch it to
  `journals: key`.
- If a tab is empty, widen `window_days` or add synonyms.
- The filter buttons (All · Preprints · Reviews · Key journals) work automatically for every tab.

Design changes (colours, layout) go in `template/index.html` (dashboard) and
`template/publications.html` (publications page).

## 5. Check that it keeps working

- **Feed status:** the line at the bottom of the page shows whether every source updated.
  Click it for details. If one source fails, the rest of the page still works.
- **Keep-alive:** GitHub pauses scheduled builds after 60 days without any change to the
  repository, and sends an email when it does. Any small edit, e.g. to `config.yml`,
  restarts it. You can also re-enable it in the Actions tab.
- **Manual rebuild:** Actions tab → Build and publish dashboard → Run workflow.

## Good to know

- **The page is public.** Free GitHub Pages sites can be seen by anyone with the link. Don't
  put unpublished or internal information in `config.yml`. If that's needed, the `site/`
  folder can instead be hosted on a hospital or university server.
- **Target of the day** rotates through human proteins targeted by approved drugs, using
  ChEMBL (the drug database behind Open Targets' drug data) and UniProt. All of its text
  comes straight from those databases. If ChEMBL is unreachable, it falls back to a fixed
  list of well-known drug targets.
- **Images.** The data-insight chart and the molecule-of-the-month image are openly licensed,
  so they are downloaded at build time and stored with the page. News thumbnails are copyrighted
  by the publishers, so they are shown straight from the publishers' sites and never copied.
  Retraction Watch and some eLife items have images in their feeds. For Nature and Science, the
  build opens each article page and uses the preview image it declares (`fetch_images: true` in
  `config.yml`), which adds 10–20 seconds to the build. When an item has no image, the feed's logo
  is shown, or the source name if there is no logo.
- **Coming up** reads DDSA's events API, ELIXIR TeSS's search API, and the event pages of the NNF
  Science Cluster, DDEA and the conference websites in `config.yml`. Pages are only read if the
  site's robots.txt allows it. Conference dates are read from the societies' own pages each day,
  so you only add a conference once; if a society moves its pages, the build says so.
- **Relevance** is keyword-based. It catches most relevant papers but not all, and lets
  some noise through.

## Run it on your own computer (optional)

Requires Python 3.10 or newer. Use a virtual environment so the packages stay separate
from the rest of your Python setup:

```bash
python -m venv .venv
source .venv/Scripts/activate      # Windows (Git Bash); on macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt

python scripts/build.py                            # live data
python scripts/build.py --sample sample_data.json  # example data, no internet needed
```

Then open `site/index.html` in your browser (in Git Bash: `start site/index.html`).

While it runs, the script prints what it is doing, step by step:

```
[1/5] New papers (Europe PMC)
  Biomarkers & proteomics
    ok  Plasma proteomics                   23 papers (21 days, all journals)
    ...
[4/5] Daily boxes
    ..  downloading approved drugs and their targets from ChEMBL (can take a minute)
    ok  Target of the day                   PPARG (4 approved drugs)
...
Done in 48 s. All sources updated.
```

Lines starting with `!!` are sources that failed; the rest of the page is still built.
Add `--quiet` (or `-q`) to hide these messages and only see the summary at the end:

```bash
python scripts/build.py --quiet
```
The same messages appear in the GitHub Actions log (Actions tab → the latest run → build).
If a server is busy, the script waits and retries a few times before giving up.
