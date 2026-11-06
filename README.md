# Wewer Albrechtsen Group dashboard

A daily start page for the [Wewer Albrechtsen group](https://alepilhen.github.io/WewerAlbrechtsen_homepage/)
(Clinical Biochemistry, Bispebjerg Hospital). It collects new papers in the group's research
areas, science news, upcoming courses and conferences, and the group's own publications in one
place, and rebuilds itself every morning.

**Live page:** <https://alepilhen.github.io/WewerAlbrechtsen_homepage/>

![The dashboard, shown with example data](docs/img/dashboard.png)

## What it shows

**The dashboard** (`index.html`) has three columns:

| Left: the group | Middle: reading | Right: daily and monthly picks |
|---|---|---|
| The group's latest publication | Science news: Nature, Science, eLife Magazine, Retraction Watch | Protein Spotlight (SIB) |
| Courses and conferences, with registration deadlines | New papers in the group's fields, in tabs (e.g. plasma proteomics, glucagon, incretin drugs, MASLD/MASH, clinical trials), with filters for preprints, reviews and key journals | A data insight from Our World in Data |
| The group's papers per year | | PDB-101's Molecule of the Month |
| Quick links to databases, societies and funders | | |

A search box at the top sends a gene, protein or topic straight to PubMed, UniProt or
Open Targets.

**The publications page** (`publications.html`) lists all of the group's papers since the PI's
first paper as last author. It can be searched (titles, abstracts, authors and journals) and
filtered by year, journal, paper type, group member and author position, and research section.
The list can be downloaded as a spreadsheet (CSV) or for a reference manager (BibTeX).

![The publications page, shown with example data](docs/img/publications.png)

## How it works

The page is a static website: there is no server or database. Once a day, GitHub Actions
runs a Python script that fetches fresh data from public sources, puts it into two HTML
templates, and publishes the result on GitHub Pages. Everything needed for that is free.

```
config.yml ─┐                     ┌─ Europe PMC        (new papers; PubMed cross-check)
template/  ─┼─► scripts/build.py ─┼─ OpenAlex, ORCID   (group publications)
            │         │           ├─ RSS feeds         (news)
            │         │           ├─ Protein Spotlight, Our World in Data, PDB-101
            │         │           └─ DDSA, DDEA, TeSS, … (courses and conferences)
            │         ▼
            │   site/index.html, site/publications.html
            └──────────► GitHub Pages
```

If a source can't be reached, the rest of the page is still built, and a status line at the
bottom of the page shows which source failed.

## Data sources

| Source | Used for |
|---|---|
| [Europe PMC](https://europepmc.org/) (REST API) | New papers and preprints in the group's fields; the group's papers in PubMed, with abstracts |
| [OpenAlex](https://openalex.org/) (API, free key) | The group's papers, found by ORCID, usually within days of publication |
| [ORCID](https://orcid.org/) (public API) | Group members' names, to catch papers wrongly linked to a member |
| Nature, Science, eLife, Retraction Watch (RSS) | News |
| [Protein Spotlight](https://www.proteinspotlight.org/) (SIB) | Monthly protein story |
| [Our World in Data](https://ourworldindata.org/data-insights) | Latest data insight, with chart |
| [RCSB PDB-101](https://pdb101.rcsb.org/motm/motm-by-date) | Molecule of the Month, with image |
| [DDSA](https://ddsa.dk/), [DDEA](https://ddeacademy.dk/), [NNF Science Cluster](https://sciencecluster.dk/), [ELIXIR TeSS](https://tess.elixir-europe.org/) and conference websites | Courses, events and conferences |
| [ChEMBL](https://www.ebi.ac.uk/chembl/) and [UniProt](https://www.uniprot.org/) | "Target of the day" (optional, off by default) |

Web pages are only read where the site's `robots.txt` allows it. Openly licensed images (the
data-insight chart and the Molecule of the Month) are stored with the page; news thumbnails are
shown from the publishers' own sites and never copied.

## Repository layout

```
.github/workflows/build.yml   daily build and publish (GitHub Actions)
config.yml                    everything that can be adjusted: searches, journals, feeds,
                              group members, sections, calendar sources, links
scripts/build.py              fetches the data and writes the pages
template/index.html           the dashboard (HTML, CSS and JavaScript)
template/publications.html    the publications page
sample_data.json              example data, for building without internet access
example.html                  the dashboard built from the example data
docs/HOW_TO_SET_UP.md         step-by-step set-up and configuration guide
docs/HOW_IT_WORKS.md          how the build script works, step by step
```

Each build writes these files to `site/` (not stored in the repository):

| File | Contents |
|---|---|
| `index.html`, `data.json` | The dashboard, and the data it shows |
| `publications.html`, `publications.json` | The publications page, and the group's papers as JSON |
| `publications_report.txt` | Group papers that were left out, and why |
| `events_report.txt` | Every event the calendar considered, and why it was shown or left out |

## Getting started

To see the layout, open `example.html` in a browser; nothing needs to be installed.

To build the page on your own computer (Python 3.10 or newer):

```bash
python -m venv .venv
source .venv/bin/activate                          # Windows (Git Bash): source .venv/Scripts/activate
pip install -r requirements.txt

python scripts/build.py --sample sample_data.json  # example data, no internet needed
python scripts/build.py                            # live data
```

Then open `site/index.html`. Add `--quiet` to only see a summary in the terminal.

To publish your own copy on GitHub Pages, add the group's ORCIDs and an OpenAlex key, and
adjust the searches, follow **[docs/HOW_TO_SET_UP.md](docs/HOW_TO_SET_UP.md)**.

## Good to know

- **The page is public.** Anyone with the link can see a GitHub Pages site, so `config.yml`
  should not contain anything internal. API keys go in GitHub secrets, never in the repository.
- **Paper relevance is keyword-based.** The searches catch most relevant papers, but not all,
  and let some noise through. They are adjusted in `config.yml`.
- **The group's papers depend on ORCID links.** A paper is only found if at least one member's
  ORCID is linked to it in OpenAlex or PubMed. OpenAlex sometimes mixes up people with similar
  names; the build checks for this and lists the affected papers in `publications_report.txt`.
- **Scheduled builds pause after 60 days** without changes to the repository (a GitHub rule).
  Any small commit restarts them.

## Documentation

- [docs/HOW_TO_SET_UP.md](docs/HOW_TO_SET_UP.md): putting it online, adding publications,
  adjusting the content, keeping it running
- [docs/HOW_IT_WORKS.md](docs/HOW_IT_WORKS.md): what the build script does, step by step, and
  troubleshooting
- The comments in [`config.yml`](config.yml): what each setting does
