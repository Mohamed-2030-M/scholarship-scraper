<div align="center">

<img src="assets/Project logo.jpg" alt="Scholarship Scraper project logo" width="140">

# Scholarship Scraper

**An AI-assisted tool that extracts application deadlines and international-student eligibility from Turkish scholarship announcements — with every answer verified against the source text.**

![Python](https://img.shields.io/badge/python-3.10%2B-blue)
![GitHub release](https://img.shields.io/github/v/release/Mohamed-2030-M/scholarship-scraper)
![Status](https://img.shields.io/badge/status-learning%20project-orange)

[Overview](#overview) · [Features](#features) · [How It Works](#how-it-works) · [Installation](#installation) · [Usage](#usage) · [Output](#output-format) · [Troubleshooting](#troubleshooting)

</div>

---

## Overview

Searching scholarship announcements manually is slow, especially when application deadlines and eligibility rules are buried in long pages. **Scholarship Scraper** automates that research step: it retrieves scholarship posts from the WordPress REST API of [Guncel Eğitim](https://www.guncel-egitim.org/), sends a shortened, priority-filtered version of each page to a configurable large language model (local Ollama or a free cloud API), and asks for two facts:

1. the **final application deadline**
2. whether **foreign / international students may apply** (`yes` / `no` / `unknown`), together with the sentence that proves it

Before a result is trusted, the script checks that the quoted evidence and the deadline literally appear in the original announcement. Everything is stored in one master CSV that you can open in Excel, filter, and annotate.

> **Important:** this tool supports scholarship research. It does not submit applications, and a green status does not guarantee that a scholarship is still open. Always confirm the details on the original announcement before applying.

## Table of Contents

- [Features](#features)
- [How It Works](#how-it-works)
- [Architecture](#architecture)
- [Installation](#installation)
- [AI Providers and Configuration](#ai-providers-and-configuration)
- [Usage](#usage)
- [CLI Options](#cli-options)
- [Output Format](#output-format)
- [Provider Fallback and Rate Limits](#provider-fallback-and-rate-limits)
- [Project Structure](#project-structure)
- [Version History](#version-history)
- [Troubleshooting](#troubleshooting)
- [Development and Experiments](#development-and-experiments)
- [Limitations and Responsible Use](#limitations-and-responsible-use)
- [Contributing](#contributing)
- [License](#license)

## Features

- **WordPress REST API retrieval** — posts are fetched as JSON from the site's `/wp-json/wp/v2` endpoints; no browser is needed.
- **AI-assisted extraction** — a local or cloud LLM reads the announcement and returns the deadline, eligibility, and supporting evidence.
- **Evidence verification** — quoted evidence and the deadline must literally exist in the page text, and the eligibility sentence must mention nationality, otherwise the row is flagged `Check`.
- **Multiple LLM providers** — Ollama (local), Gemini, and Groq through one OpenAI-compatible interface.
- **Per-scholarship fallback** — if the first model fails for any reason (timeout, server error, unreadable answer), the next model in the list answers that scholarship; models that stay down are skipped for the rest of the run.
- **Incremental updates** — after the first full scan, later runs only analyze new or modified posts and stop as soon as a page has nothing new.
- **Master CSV table** — one persistent table keyed by the WordPress post ID, with `Deadline Date`, `Days Left`, and `Expired` refreshed on every run.
- **Your columns are preserved** — manual columns such as `Applied?` and `My Notes` (and any extra column you add) survive every run.
- **Local answer cache** — model answers are cached per post, so re-runs are free and fast.
- **Polite by design** — a descriptive user agent, a delay between listing pages, and per-provider rate-limit intervals.

## How It Works

1. **Choose pages.** On the first run (or with `--full`) the script scans every listing page; later runs work incrementally and stop at the first page with nothing new.
2. **Resolve the category.** It looks up the `burs-basvurusu` category and filters posts by it (if the category is missing, it warns and scans all posts instead).
3. **Fetch posts.** Each post arrives as JSON with its title, link, last-modified date, and HTML content.
4. **Clean the text.** HTML is converted to plain text; scripts and styles are removed.
5. **Prioritize relevant lines.** Long pages are shortened to the model's character budget: nationality lines first, then deadline lines, then application conditions, keeping the original page order and dropping duplicates.
6. **Ask the LLM.** The model returns a JSON object with `deadline`, `deadline_evidence`, `foreigners_allowed`, and `evidence`.
7. **Verify.** The deadline and both evidence strings must appear in the full page text (compared after normalization), and the eligibility evidence must contain a nationality keyword — otherwise the row becomes `Check`.
8. **Save and summarize.** Rows are merged into the master CSV, `Days Left` / `Expired` are recomputed, and a summary lists every row that needs a human look.

<img src="assets/PIPLEINE_DESING_System Desing_Workflow.jpg" alt="Pipeline design diagram: fetch posts from the WordPress API, clean the HTML, shrink the text with priority selection, ask the LLM, verify the evidence, and save to the master CSV" width="860">

<!-- Asset note: this reference must match the exact file name in assets/ (shown truncated in Explorer). Run "dir assets" to confirm; fix this one link if your file name differs. -->

## Architecture

The scraper is **API-first**: Python does retrieval, text preparation, normalization, verification, caching, and CSV writing, while the LLM is only asked to interpret the announcement text. The source site, the model provider, and the output files are connected in one pipeline:

<img src="assets/Architecture Diagram.jpg" alt="System architecture diagram showing the scraper connecting to the Guncel Eğitim WordPress API, the configured LLM provider, and the output folder with the master CSV and cache files" width="860">

## Installation

### 1. Clone the repository

```bash
git clone https://github.com/Mohamed-2030-M/scholarship-scraper.git
cd scholarship-scraper
```

### 2. Create and activate a virtual environment

**Windows (PowerShell)**

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
```

If PowerShell refuses to activate the script for this session only:

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy RemoteSigned
.\.venv\Scripts\Activate.ps1
```

**macOS / Linux**

```bash
python3 -m venv .venv
source .venv/bin/activate
```

### 3. Install the dependencies

```bash
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

`playwright` and `rich` are only needed for the legacy v0.1 script; the current `main.py` imports `requests` and `beautifulsoup4` only.

### 4. Prepare an LLM

**Local (Ollama):**

```bash
ollama pull qwen2.5:7b
```

Make sure the Ollama service is running before you start the scraper.

**Cloud (Gemini or Groq):** no installation is needed — set the API key as shown in the next section.

## AI Providers and Configuration

Providers are presets that all speak the OpenAI-compatible `/chat/completions` API. You can override the model of any provider with `provider:model` in the `--providers` list.

| Provider | Endpoint | Default model | API key environment variable | Text budget | Min. interval between calls |
| --- | --- | --- | --- | --- | --- |
| `ollama` | `http://localhost:11434/v1` | `qwen2.5:7b` | — | 3,500 chars | none |
| `gemini` | `https://generativelanguage.googleapis.com/v1beta/openai` | `gemini-3-flash-preview` | `GEMINI_API_KEY` | 12,000 chars | 6.5 s |
| `groq` | `https://api.groq.com/openai/v1` | `llama-3.3-70b-versatile` | `GROQ_API_KEY` | 12,000 chars | 3.0 s |

The default provider list is:

```text
ollama:qwen2.5:7b,ollama:olmo-3:7b-instruct
```

Set a cloud key in the same terminal session where you run the scraper.

**Windows (PowerShell)**

```powershell
$env:GEMINI_API_KEY = "YOUR_GEMINI_API_KEY"
$env:GROQ_API_KEY  = "YOUR_GROQ_API_KEY"
```

**macOS / Linux**

```bash
export GEMINI_API_KEY="YOUR_GEMINI_API_KEY"
export GROQ_API_KEY="YOUR_GROQ_API_KEY"
```

If the key for a chosen cloud provider is missing, the script stops before the run and prints the exact PowerShell command to set it. Never commit real keys or `.env` files to Git.

Model names on free tiers change often. To list the model IDs a provider currently offers:

```bash
python main.py --list-models gemini
```

## Usage

A typical session: pick the pages and providers, let the scraper analyze the posts, then review the rows it flags in the master CSV.

<img src="assets/Workflow diagram.jpg" alt="Workflow diagram showing the end-to-end process from choosing pages and providers to reviewing the exported CSV records" width="760">

### First run — full scan of every page

```bash
python main.py
```

### Later runs — incremental updates (new or changed posts only)

```bash
python main.py
```

### Scan specific pages

```bash
python main.py --pages 3        # one page
python main.py --pages 2-5      # a range
python main.py --pages all      # everything
```

### Quick test on a few posts

```bash
python main.py --pages 1 --limit 3
```

### Force a complete re-scan

```bash
python main.py --full
```

### Ask the model again for posts that were already analyzed

```bash
python main.py --reanalyze --no-cache
```

Use `--no-cache` whenever you edit the prompt, the text selection, or the model, so stale answers are not reused.

### Choose providers and models

```bash
python main.py --providers gemini,ollama:qwen2.5:7b
```

The list is tried in order — see [Provider Fallback and Rate Limits](#provider-fallback-and-rate-limits).

Example terminal output of a run:

<img src="assets/Terminal demo.jpg" alt="Terminal demo showing the scraper printing per-scholarship progress, status, deadline, and foreigners-allowed results, followed by a summary and the saved CSV path" width="860">

> **Note:** the old `--page N` switch from v0.2 still works but is hidden from `--help`. Prefer `--pages N`.

## CLI Options

| Option | Default | Description |
| --- | --- | --- |
| `--pages SPEC` | first run: all; later: incremental | Pages to scan: `3`, `2-5`, or `all` |
| `--limit N` | — | Only process the first N posts of each page (useful for testing) |
| `--full` | off | Scan every page again instead of stopping at unchanged posts |
| `--reanalyze` | off | Ask the model again even for unchanged posts (pair with `--no-cache`) |
| `--providers LIST` | `ollama:qwen2.5:7b,ollama:olmo-3:7b-instruct` | Comma-separated providers/models, tried in order |
| `--max-chars N` | preset value (see provider table) | Characters of a page sent to the model |
| `--timeout SEC` | `180` | Seconds to wait for one model answer |
| `--no-cache` | off | Ignore saved model answers |
| `--list-models PROVIDER` | — | Print the model names a provider offers, then exit |
| `--version` | — | Print the script version and exit |

Run `python main.py --help` for the built-in reference.

## Output

All runtime files live in the `output/` folder (created automatically, ignored by Git):

```text
output/
├── scholarships_master.csv   # the persistent master table
├── llm_cache.json            # cached model answers
└── state.json                # remembers the last complete scan
```

The master CSV is written atomically (a temporary file is renamed into place) with a UTF-8 BOM so Excel opens Turkish characters correctly. If the file is locked because it is open in Excel, the script writes `scholarships_master_HHMMSS.csv` instead and tells you.

The master table is keyed by the WordPress post ID: posts are inserted once and updated in place, so your edits survive. `Days Left` and `Expired` are recomputed on every run, and your own columns (`Applied?`, `My Notes`, or any extra column you add) are preserved.

Example of the master CSV opened in a spreadsheet:

<img src="assets/CSV output example.jpg" alt="Example of the output CSV opened in a spreadsheet, showing columns for deadline, days left, foreigners allowed, evidence, status, and notes" width="860">

### Columns

| Column | Description |
| --- | --- |
| `Post ID` | WordPress post ID (the key of the master table) |
| `Scholarship Name` | Announcement title |
| `Link` | Original announcement URL |
| `Deadline` | Deadline exactly as written on the page, or `Not specified` |
| `Deadline Date` | Deadline normalized to `YYYY-MM-DD` (Turkish month names and numeric dates are understood) |
| `Days Left` | Days until `Deadline Date` from today, recomputed on every run |
| `Expired` | `yes` when the deadline has passed |
| `Foreigners Allowed` | `yes`, `no`, or `unknown` |
| `Evidence` | Sentence from the page supporting the eligibility result |
| `Deadline Evidence` | Sentence from the page supporting the deadline |
| `Status` | `OK`, `Incomplete`, `Check`, or `Failed` |
| `Notes` | What went wrong during verification or processing |
| `Model` | Provider and model that produced the answer (`(cached)` when reused) |
| `Modified` | Post modification date from the WordPress API |
| `First Seen` | When this post first entered the table |
| `Last Updated` | When this row was last (re)analyzed |
| `Applied?` / `My Notes` | Your own tracking columns, preserved between runs |

### Status meanings

| Status | Meaning |
| --- | --- |
| `OK` | Deadline and eligibility were found, and all quoted evidence passed the source-text checks |
| `Incomplete` | The page text is empty/too short, or one of the two facts is missing (`unknown` or no deadline) |
| `Check` | The model's deadline or evidence could not be verified in the page text — review the original link by hand |
| `Failed` | The post could not be analyzed (for example, every model was unavailable); it is retried on the next run |

`foreigners: unknown` means the page does not say — it never means "not allowed". `no` means the page states that only Turkish citizens (T.C. vatandaşı) may apply.

## Provider Fallback and Rate Limits

The `--providers` list is a priority list, and fallback is **per scholarship**, not per run:

- If the first model fails for any reason — a timeout, a server error, a rate limit, or an unreadable answer — that scholarship is sent to the next provider in the list.
- A provider is **disabled for the rest of the run** when it reports a permanent problem (cannot connect, HTTP 401/403/404, payment required, or a second consecutive HTTP 429) **or** when it fails 3 times in a row. You will see a `[!]` line in the terminal each time this happens.
- Each individual call is retried up to 4 times: a single HTTP 429 waits 20 seconds and retries, HTTP 400 first retries once without JSON mode (for providers without JSON mode support), and HTTP 5xx / 408 back off and retry.
- Free cloud tiers are paced by a minimum interval between calls (see the provider table) to stay inside rate limits.
- If every provider ends up disabled, the run stops, progress is saved, and re-running the same command later resumes cheaply because cached answers are reused.

So: switching models is not limited to "provider unavailable" — any failure, including a timeout, moves that one scholarship to the next model.

## Project Structure

```text
scholarship-scraper/
├── main.py                    # v1.0 pipeline: WordPress API + LLM + verification + master CSV
├── legacy/
│   └── playwright_scraper.py  # v0.1: Playwright browser scraper with keyword lists (reference)
├── check_words.py             # Debug helper: prints nationality lines from the full page text
├── list_headings.py           # Debug helper: heading frequencies across posts (writes output/headings.csv)
├── test_heading_filter.py     # Experiment: heading-based text filtering before the LLM call (WIP)
├── requirements.txt           # Python dependencies
├── assets/                    # Images used by this README
├── .gitignore                 # Ignores output/, venvs, .env, caches
└── README.md
```

<img src="assets/Folder Structure.jpg" alt="Folder structure diagram of the repository showing the source files, the legacy folder, and the runtime output folder" width="720">

## Version History

| Tag | What changed |
| --- | --- |
| [`v1.0`](https://github.com/Mohamed-2030-M/scholarship-scraper/releases/tag/v1.0) (current) | Master CSV keyed by post ID; incremental updates with early stop; `--pages` / `--full` / `--reanalyze`; `Deadline Date`, `Days Left`, `Expired`; manual columns preserved; per-scholarship fallback with 3-strike disabling; `--timeout`; `--version`; eligibility evidence must mention nationality |
| [`v0.2`](https://github.com/Mohamed-2030-M/scholarship-scraper/tree/v0.2) | WordPress REST API + LLM pipeline; per-run CSV files; Ollama/Gemini/Groq providers with fallback on permanent failure; answer cache; evidence verification; `--page` / `--limit` / `--providers` / `--no-cache` / `--list-models` |
| [`v0.1`](https://github.com/Mohamed-2030-M/scholarship-scraper/tree/v0.1) | Headless Microsoft Edge via Playwright; fixed Turkish keyword lists with a 5-step first-match eligibility classifier; label-based deadline extraction; CSV with `Eligibility` and `Matched Keyword` columns |

First full v1.0 run (145 scholarships on 13 pages, local `qwen2.5:7b`): **OK 41, Incomplete 75, Check 29, Failed 0**. `Incomplete` usually means the announcement itself does not state the information — `unknown` is the honest answer then.

## Troubleshooting

- **`cannot connect (is Ollama running?)`** — start Ollama and make sure the configured model is pulled. The provider is disabled for the rest of the run and the next provider is tried.
- **Missing API key message** — set `GEMINI_API_KEY` or `GROQ_API_KEY` in the same terminal session (see [Configuration](#ai-providers-and-configuration)).
- **The run never seems to finish** — the first run scans every page. Use `--limit` for a quick test, or press `Ctrl+C`: partial progress is saved.
- **A model name stops working** — free-tier model names change; run `python main.py --list-models PROVIDER` for the current IDs.
- **Old answers after I changed something** — use `--no-cache` (and `--reanalyze` for posts that were already analyzed). The cache key includes the post's modification date and the first model.
- **`Category 'burs-basvurusu' not found`** — the script falls back to scanning all posts and says so; check whether the site renamed the category.
- **The CSV was written to a second file** — close it in Excel and re-run; the script explains this when it happens.
- **Incremental run misbehaving** — delete `output/state.json` (or run with `--full`) to force a complete fresh scan.
- **Row marked `Check` or `Incomplete`** — open the original link and verify the deadline and eligibility manually; treat those rows as hints, not facts.

## Development and Experiments

There is no pytest suite in this repository. The helper and experiment scripts are run directly and require network access to the source site:

```bash
python check_words.py            # nationality lines in the first 3 posts of page 1
python list_headings.py 1 2 3    # common headings across pages; writes output/headings.csv
python test_heading_filter.py    # WIP: keep only focus sections + date-carrying sections for the LLM
```

## Limitations and Responsible Use

- The source site is hard-coded (`guncel-egitim.org`, category `burs-basvurusu`) and announcements are in Turkish.
- Only the announcement text itself is analyzed; external links inside a post (for example a PDF on a university site) are not followed.
- LLM output is assistance for research, not an eligibility decision — `OK` rows still deserve a glance at the source link.
- Be polite to the source website: the script already sends a descriptive user agent, waits between pages, and paces cloud calls — keep it that way.
- Do not publish API keys or personal data; `.env` files are git-ignored for a reason.

## Contributing

Issues and pull requests are welcome. When reporting a problem, include the command you ran, what you expected, what happened, and a sanitized error message — never API keys or other secrets.

## License

No license is specified for this repository yet. Unless a license is added, reuse and redistribution may be subject to copyright restrictions.

---

<div align="center">

Built to make scholarship research more organized and evidence-aware.

**[Mohamed-2030-M/scholarship-scraper](https://github.com/Mohamed-2030-M/scholarship-scraper)**

</div>
