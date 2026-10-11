    """Scholarship scraper - Part 1 (full pipeline).

    Opens one listing page of guncel-egitim.org/burslar/burs-basvurusu in

    headless Microsoft Edge (Playwright, sync API), collects the scholarship

    card links, visits every detail page, extracts deadline + eligibility

    (with the matched keyword), writes a CSV, and prints a terminal report.

    """

    import csv

    import logging

    import random

    import re

    import time

    import unicodedata

    from datetime import datetime

    from pathlib import Path

    from urllib.parse import urlsplit, urlunsplit

    from playwright.sync_api import (

        Browser,

        Error as PlaywrightError,

        Page,

        Playwright,

        TimeoutError as PlaywrightTimeoutError,

        sync_playwright,

    )

    from rich.console import Console

    from rich.logging import RichHandler

    from rich.table import Table

    console = Console()

    logging.basicConfig(

        level=logging.INFO,

        format="%(message)s",

        datefmt="[%X]",

        handlers=[RichHandler(console=console, rich_tracebacks=True)],

    )

    logger = logging.getLogger(__name__)

    # ----------------------------------------------------------------------

    # Keyword configuration

    # ----------------------------------------------------------------------

    KeywordItem = str | re.Pattern

    FOREIGNERS_ONLY_KEYWORDS: list[KeywordItem] = [

        re.compile(r"sadece yabancı( uyruklu\w*| öğrenci\w*)"),

        re.compile(r"sadece uluslararası öğrenci\w*"),

        "türkiye cumhuriyeti vatandaşları başvuramaz",

        "türk vatandaşları başvuramaz",

    ]

    BOTH_KEYWORDS: list[KeywordItem] = [

        "yabancı uyruklu öğrenci",

        "yabancı uyruklu",

        "uluslararası öğrenci",

        "yabancı öğrenci",

        re.compile(r"yabancı\w*( uyruklu\w*)?( öğrenci\w*)? için ayrı( bir)? kota\w*"),

        "foreign students",

    ]

    TURKISH_ONLY_EXCLUSIVE_KEYWORDS: list[KeywordItem] = [

        re.compile(

            r"yabancı\w*( uyruklu\w*)?( \w+)? "

            r"(başvuramaz|başvuru yapamaz|kabul edilmez)"

        ),

        "sadece türkiye cumhuriyeti vatandaşları",

        "sadece t.c. vatandaşları",

        "sadece türk vatandaşları",

    ]

    TURKISH_GENERIC_KEYWORDS: list[KeywordItem] = [

        "türkiye cumhuriyeti vatandaşı olmak",

        "t.c. vatandaşı olmak",

        "türk vatandaşı olmak",

    ]

    DEADLINE_LABELS: list[str] = [

        "son başvuru tarihi",

        "başvuru tarihleri",

        "başvuru tarihi",

        "son tarih",

        "başvuru bitiş",

        "son başvuru",

    ]

    TURKISH_MONTHS: list[str] = [

        "ocak", "şubat", "mart", "nisan", "mayıs", "haziran",

        "temmuz", "ağustos", "eylül", "ekim", "kasım", "aralık",

    ]

    # ----------------------------------------------------------------------

    # Settings

    # ----------------------------------------------------------------------

    BASE_URL = "https://www.guncel-egitim.org"

    LIST_URL_TEMPLATE = BASE_URL + "/burslar/burs-basvurusu/page/{n}/"

    USER_AGENT = "ScholarshipScraper/1.0 (educational project)"

    SITE_HOST = "guncel-egitim.org"

    TIMEOUT_SECONDS = 120

    MAX_ATTEMPTS = 3

    RETRYABLE_STATUS_CODES = {408, 429}

    BACKOFF_MIN_SECONDS = 2

    BACKOFF_MAX_SECONDS = 5

    DETAIL_DELAY_MIN_SECONDS = 0.5

    DETAIL_DELAY_MAX_SECONDS = 1.0

    MIN_TEXT_CHARS = 300

    SEGMENT_MAX_CHARS = 120

    OUTPUT_FOLDER = "output"

    CSV_DATE_FORMAT = "%d_%m_%Y"

    CSV_TIME_SUFFIX_FORMAT = "%H%M%S"

    PARTIAL_MARKER = "_partial"

    DATETIME_FORMAT = "%Y-%m-%d %H:%M:%S"

    CSV_COLUMNS = [

        "Operation Date",

        "Page",

        "Scholarship Name",

        "Link",

        "Deadline",

        "Eligibility",

        "Matched Keyword",

        "Status",

    ]

    MISSING_DEADLINE = "Not specified"

    MISSING_ELIGIBILITY = "Unknown"

    MISSING_KEYWORD = "None"

    FAILED_LOAD = "Failed to load"

    STATUS_OK = "OK"

    STATUS_INCOMPLETE = "Incomplete"

    STATUS_FAILED = FAILED_LOAD

    CATEGORY_FOREIGNERS = "Foreigners only"

    CATEGORY_TURKISH_ONLY = "Turkish citizens only"

    CATEGORY_BOTH = "Turkish & Foreigners"

    CATEGORY_UNKNOWN = MISSING_ELIGIBILITY

    PAGE_NOT_FOUND_TEXT = "sayfa bulunamadı"

    LOAD_OK = "ok"

    LOAD_RETRYABLE = "retryable"

    LOAD_NOT_FOUND = "not_found"

    LOAD_FAILED = "failed"

    # ----------------------------------------------------------------------

    # Link-discovery constants (confirmed on the real listing page)

    # ----------------------------------------------------------------------

    LISTING_REGION_SELECTOR = "#content"

    LISTING_EXCLUDE_SELECTOR = (

        "aside, .sidebar, .widget, .widget-area, #sidebar, #secondary"

    )

    YEAR_IN_TEXT = re.compile(r"\d{4}")

    EXCLUDED_SLUGS = {

        "iletisim",

        "hakkinda",

        "hakkimizda",

        "gizlilik-politikasi",

        "cerez-politikasi",

        "kvkk",

        "feed",

        "sitemap",

    }

    # ----------------------------------------------------------------------

    # Text helpers

    # ----------------------------------------------------------------------

    def turkish_lower(text: str) -> str:

        """Lowercase with Turkish rules while preserving string length."""

        return text.replace("İ", "i").replace("I", "ı").lower()

    def compile_keywords(items: list[KeywordItem]) -> list[re.Pattern]:

        """Compile a keyword list once at startup."""

        return [

            item if isinstance(item, re.Pattern)

            else re.compile(re.escape(turkish_lower(item)))

            for item in items

        ]

    FOREIGNERS_ONLY_PATTERNS = compile_keywords(FOREIGNERS_ONLY_KEYWORDS)

    BOTH_PATTERNS = compile_keywords(BOTH_KEYWORDS)

    TURKISH_ONLY_EXCLUSIVE_PATTERNS = compile_keywords(TURKISH_ONLY_EXCLUSIVE_KEYWORDS)

    TURKISH_GENERIC_PATTERNS = compile_keywords(TURKISH_GENERIC_KEYWORDS)

    def normalize_flattened(text: str) -> str:

        """NFC + NBSP->space + collapse ALL whitespace (eligibility)."""

        text = unicodedata.normalize("NFC", text).replace("\xa0", " ")

        return re.sub(r"\s+", " ", text).strip()

    def normalize_keep_lines(text: str) -> str:

        """NFC + NBSP->space; collapse spaces/tabs but KEEP line breaks."""

        text = (

            unicodedata.normalize("NFC", text)

            .replace("\xa0", " ")

            .replace("\r\n", "\n")

        )

        return re.sub(r"[^\S\n]+", " ", text)

    # ----------------------------------------------------------------------

    # Browser core

    # ----------------------------------------------------------------------

    def launch_browser(playwright: Playwright) -> tuple[Browser, Page]:

        """Launch Microsoft Edge headless and return (browser, page)."""

        browser = playwright.chromium.launch(channel="msedge", headless=True)

        context = browser.new_context(user_agent=USER_AGENT)

        page = context.new_page()

        logger.info("Edge browser launched (headless).")

        return browser, page

    def _sleep_backoff() -> None:

        """Sleep a random backoff interval between retry attempts."""

        seconds = random.uniform(BACKOFF_MIN_SECONDS, BACKOFF_MAX_SECONDS)

        logger.info("Backing off %.1f s before the next attempt.", seconds)

        time.sleep(seconds)

    def _politeness_delay() -> None:

        """0.5-1 s delay between detail-page requests (good citizenship)."""

        time.sleep(random.uniform(DETAIL_DELAY_MIN_SECONDS, DETAIL_DELAY_MAX_SECONDS))

    def open_page(page: Page, url: str) -> tuple[str, str]:

        """Open a URL once - no retry. Returns (outcome, detail).

        The not-found marker is checked on the CONTENT region only - the

        sidebar/menu of normal pages can contain that text. body = fallback.

        """

        timeout_ms = TIMEOUT_SECONDS * 1000  # Playwright expects milliseconds!

        try:

            response = page.goto(url, timeout=timeout_ms, wait_until="domcontentloaded")

        except (PlaywrightTimeoutError, PlaywrightError) as exc:

            return LOAD_RETRYABLE, f"navigation error - {type(exc).__name__}: {exc}"

        if response is not None:

            status = response.status

            if status == 404:

                return LOAD_NOT_FOUND, "HTTP 404"

            if status >= 500 or status in RETRYABLE_STATUS_CODES:

                return LOAD_RETRYABLE, f"HTTP {status}"

            if 400 <= status < 500:

                return LOAD_FAILED, f"HTTP {status} (non-retryable)"

        try:

            region = page.query_selector("#content")

            body_text = region.inner_text() if region else page.inner_text("body")

        except PlaywrightError:

            return LOAD_RETRYABLE, "could not read page text"

        if PAGE_NOT_FOUND_TEXT in turkish_lower(body_text):

            return LOAD_NOT_FOUND, "not-found marker in page text"

        return LOAD_OK, "ok"

    def open_with_retry(page: Page, url: str, attempts: int = MAX_ATTEMPTS) -> tuple[str, str]:

        """Open a URL with up to `attempts` tries and 2-5 s backoff."""

        last_detail = "no attempts made"

        for attempt in range(1, attempts + 1):

            logger.info("Attempt %d/%d: %s", attempt, attempts, url)

            outcome, last_detail = open_page(page, url)

            if outcome != LOAD_RETRYABLE:

                return outcome, last_detail

            logger.warning("Attempt %d failed - %s", attempt, last_detail)

            if attempt < attempts:

                _sleep_backoff()

        return LOAD_FAILED, f"all {attempts} attempts exhausted - last: {last_detail}"

    # ----------------------------------------------------------------------

    # Listing page

    # ----------------------------------------------------------------------

    def prompt_page_number() -> int:

        """Ask for a listing page number; re-ask until valid."""

        while True:

            raw = console.input("Enter page number: ").strip()

            if raw.isdigit() and int(raw) >= 1:

                return int(raw)

            logger.warning("Invalid input %r - enter a positive integer (1, 2, 3, ...).", raw)

    def build_url(page_number: int) -> str:

        """Build the listing-page URL for the given page number."""

        return LIST_URL_TEMPLATE.format(n=page_number)

    def normalize_url(href: str) -> str | None:
        """Normalize an absolute HTTP(S) URL for deduplication.

        The site uses HTTPS and commonly publishes links with or without ``www``.
        Query strings and fragments are removed so one scholarship has one URL.
        """
        if not href:
            return None

        try:
            parts = urlsplit(href.strip())
            host = (parts.hostname or "").lower()
            port = parts.port
        except ValueError:
            return None

        if parts.scheme.lower() not in ("http", "https") or not host:
            return None
        if host.startswith("www."):
            host = host[4:]
        if host != SITE_HOST:
            return None
        if port not in (None, 80, 443):
            return None

        path = parts.path or "/"
        last_segment = path.rstrip("/").rsplit("/", 1)[-1]
        if "." not in last_segment and not path.endswith("/"):
            path += "/"

        return urlunsplit(("https", SITE_HOST, path, "", ""))

    def _split_anchor_regions(page: Page) -> tuple[list[dict], list[dict], list[dict]]:

        """Split body anchors into (content, widget_filtered, outside)."""

        return page.evaluate(

            """

            ([regionSel, excludeSel]) => {

                const norm = a => ({href: a.href, text: (a.innerText || '').trim()});

                const all = Array.from(document.body.querySelectorAll('a'));

                const region = document.querySelector(regionSel);

                const contentEls = region ? Array.from(region.querySelectorAll('a')) : [];

                const contentSet = new Set(contentEls);

                const keep = [], filtered = [], outside = [];

                for (const a of all) {

                    const item = norm(a);

                    if (!item.href) continue;

                    if (!contentSet.has(a)) { outside.push(item); continue; }

                    if (a.closest(excludeSel)) { filtered.push(item); continue; }

                    keep.push(item);

                }

                return [keep, filtered, outside];

            }

            """,

            [LISTING_REGION_SELECTOR, LISTING_EXCLUDE_SELECTOR],

        )

    def _passes_link_rules(url: str, text: str) -> bool:

        """True when a normalized URL + link text look like a scholarship card."""

        parts = urlsplit(url)

        if parts.netloc != SITE_HOST:

            return False

        segments = [s for s in parts.path.split("/") if s]

        if len(segments) != 1:

            return False

        if segments[0] in EXCLUDED_SLUGS:

            return False

        return bool(YEAR_IN_TEXT.search(text))

    def collect_scholarship_links(page: Page) -> tuple[list[dict], list[dict], dict]:

        """Collect scholarship card links from the listing page."""

        content, widget_filtered, outside = _split_anchor_regions(page)

        seen: set[str] = set()

        collected: list[dict] = []

        for anchor in content:

            url = normalize_url(anchor.get("href", ""))

            if url is None or url in seen:

                continue

            text = anchor.get("text") or ""

            if not _passes_link_rules(url, text):

                continue

            seen.add(url)

            collected.append({"url": url, "name": text.strip()})

        missed: list[dict] = []

        missed_seen: set[str] = set()

        for anchor in content + widget_filtered + outside:

            url = normalize_url(anchor.get("href", ""))

            if url is None or url in seen or url in missed_seen:

                continue

            text = anchor.get("text") or ""

            if not _passes_link_rules(url, text):

                continue

            missed_seen.add(url)

            missed.append({"url": url, "name": text.strip()})

        diagnostics = {

            "raw": len(content) + len(widget_filtered) + len(outside),

            "in_content": len(content) + len(widget_filtered),

            "widget_filtered": len(widget_filtered),

            "outside_content": len(outside),

        }

        return collected, missed, diagnostics

    # ----------------------------------------------------------------------

    # Extraction: eligibility (5-step, first match wins)

    # ----------------------------------------------------------------------

    _MONTH_ALT = "|".join(TURKISH_MONTHS)

    def _snippet_around(norm: str, start: int, end: int, limit: int = 100) -> str:

        """Sentence around [start:end) cut from the case-preserved text."""

        ls = max(

            norm.rfind(".", 0, start),

            norm.rfind("!", 0, start),

            norm.rfind("?", 0, start),

        ) + 1

        le = len(norm)

        for ch in (".", "!", "?"):

            idx = norm.find(ch, end)

            if idx != -1:

                le = min(le, idx + 1)

        snippet = re.sub(r"\s+", " ", norm[ls:le]).strip()

        return snippet[:limit] + ("..." if len(snippet) > limit else "")

    def classify_eligibility(raw_text: str) -> tuple[str, str, str]:

        """5-step first-match-wins. Returns (category, matched, evidence)."""

        norm = normalize_flattened(raw_text)

        low = turkish_lower(norm)

        groups = (

            (FOREIGNERS_ONLY_PATTERNS, CATEGORY_FOREIGNERS),

            (TURKISH_ONLY_EXCLUSIVE_PATTERNS, CATEGORY_TURKISH_ONLY),

            (BOTH_PATTERNS, CATEGORY_BOTH),

            (TURKISH_GENERIC_PATTERNS, CATEGORY_TURKISH_ONLY),

        )

        for patterns, category in groups:

            for pattern in patterns:

                m = pattern.search(low)

                if m:

                    return category, m.group(0), _snippet_around(norm, m.start(), m.end())

        return CATEGORY_UNKNOWN, MISSING_KEYWORD, ""

    # ----------------------------------------------------------------------

    # Extraction: deadline

    # ----------------------------------------------------------------------

    DATE_RE = re.compile(

        r"\b\d{1,2}[./-]\d{1,2}[./-]\d{4}\b"

        r"|\b\d{1,2} (?:" + _MONTH_ALT + r") \d{4}\b"

    )

    RANGE_GAP_RE = re.compile(r"(?:^|\s)(?:-|–|—|ile|arasında|arasinda)(?:\s|$)")

    def _line_span(text: str, pos: int) -> tuple[int, int]:

        """Start/end indices of the line containing pos."""

        start = text.rfind("\n", 0, pos) + 1

        end = text.find("\n", pos)

        return start, (end if end != -1 else len(text))

    def _find_date_spans(low: str, start: int, end: int) -> list[tuple[int, int]]:

        """All date spans inside low[start:end]."""

        return [(m.start(), m.end()) for m in DATE_RE.finditer(low, start, end)]

    def _choose_date(low: str, spans: list[tuple[int, int]]) -> tuple[int, int] | None:

        """FIRST date - unless consecutive dates are joined by a range gap

        (dash / ile / arasında), then the LAST of the chain (closing date)."""

        if not spans:

            return None

        if len(spans) >= 2 and RANGE_GAP_RE.search(low[spans[0][1]:spans[1][0]]):

            last = spans[0]

            for span in spans[1:]:

                if RANGE_GAP_RE.search(low[last[1]:span[0]]):

                    last = span

                else:

                    break

            return last

        return spans[0]

    def _evidence_line(norm: str, start: int, end: int, limit: int = 100) -> str:

        """The line(s) containing [start:end), case-preserved, truncated."""

        ls, le = _line_span(norm, start)

        if end > le:

            nxt = norm.find("\n", le + 1)

            le = nxt if nxt != -1 else len(norm)

        snippet = re.sub(r"\s+", " ", norm[ls:le]).strip()

        return snippet[:limit] + ("..." if len(snippet) > limit else "")

    def extract_deadline(raw_text: str) -> tuple[str, str]:

        """Deadline: every label occurrence is a candidate; segment = end of

        label to the line break (capped), else the next non-empty line; a bare

        month-day without a year never matches. Value is cut case-preserved."""

        norm = normalize_keep_lines(raw_text)

        low = turkish_lower(norm)

        for label in DEADLINE_LABELS:

            for m in re.finditer(re.escape(label), low):

                nl = low.find("\n", m.end())

                line_end = nl if nl != -1 else len(low)

                seg_end = min(line_end, m.end() + SEGMENT_MAX_CHARS)

                spans = _find_date_spans(low, m.end(), seg_end)

                ev_start, ev_end = m.start(), seg_end

                if not spans and nl != -1:

                    probe = nl + 1

                    for _ in range(3):

                        if probe >= len(low):

                            break

                        p_end = low.find("\n", probe)

                        p_end = p_end if p_end != -1 else len(low)

                        if low[probe:p_end].strip():

                            cap = min(p_end, probe + SEGMENT_MAX_CHARS)

                            spans = _find_date_spans(low, probe, cap)

                            ev_start, ev_end = probe, cap

                            break

                        probe = p_end + 1

                chosen = _choose_date(low, spans)

                if chosen:

                    value = norm[chosen[0]:chosen[1]].strip()

                    return value, _evidence_line(norm, ev_start, ev_end)

        return MISSING_DEADLINE, ""

    # ----------------------------------------------------------------------

    # Detail page text + status

    # ----------------------------------------------------------------------

    def extract_visible_text(page: Page) -> tuple[str, str]:
        """Extract readable page text using Playwright's Locator API.

        Prefer article, then main, then body. Locator.inner_text accepts a
        timeout; ElementHandle.inner_text does not, which caused the reported crash.
        The body text is returned even when short so the record is still useful.
        """
        for selector in ("article", "main", "body"):
            locator = page.locator(selector).first
            try:
                if locator.count() == 0:
                    continue
                text = locator.inner_text(timeout=5_000)
            except PlaywrightError as exc:
                logger.debug("Could not read <%s>: %s", selector, exc)
                continue

            text = text.strip()
            if len(text) >= MIN_TEXT_CHARS:
                logger.info("Text source: <%s> (%d chars).", selector, len(text))
                return text, selector
            logger.info(
                "Source <%s> too short (%d chars) - falling through.",
                selector,
                len(text),
            )

        logger.warning("No page section reached %d characters; using available body text.", MIN_TEXT_CHARS)
        try:
            text = page.locator("body").first.inner_text(timeout=5_000).strip()
            return text, "body(short)"
        except PlaywrightError as exc:
            logger.error("Could not read any text from the page: %s", exc)
            return "", "none"

    def compute_status(deadline: str, eligibility: str) -> str:

        """OK = both found; Incomplete = loaded but one/both missing."""

        if deadline != MISSING_DEADLINE and eligibility != CATEGORY_UNKNOWN:

            return STATUS_OK

        return STATUS_INCOMPLETE

    # ----------------------------------------------------------------------

    # Output: CSV + terminal report

    # ----------------------------------------------------------------------

    def build_csv_path(page_number: int, run_start: datetime) -> Path:

        """output/scholarships_page_{N}_{DD}_{MM}_{YYYY}.csv (auto-create folder)."""

        folder = Path(OUTPUT_FOLDER)

        folder.mkdir(parents=True, exist_ok=True)

        date_str = run_start.strftime(CSV_DATE_FORMAT)

        return folder / f"scholarships_page_{page_number}_{date_str}.csv"

    def _write_rows(path: Path, records: list[dict]) -> None:

        with open(path, "w", newline="", encoding="utf-8-sig") as fh:

            writer = csv.writer(fh)

            writer.writerow(CSV_COLUMNS)

            for record in records:

                writer.writerow([record.get(col, "") for col in CSV_COLUMNS])

    def write_csv(path: Path, records: list[dict]) -> Path:

        """Write the CSV; on PermissionError (file open in Excel) retry with

        a _HHMMSS suffix. Returns the path actually written."""

        try:

            _write_rows(path, records)

            return path

        except PermissionError:

            suffix = datetime.now().strftime(CSV_TIME_SUFFIX_FORMAT)

            alt = path.with_name(f"{path.stem}_{suffix}{path.suffix}")

            logger.warning("PermissionError - writing to %s instead.", alt.name)

            _write_rows(alt, records)

            return alt

    def render_table(records: list[dict], page_number: int) -> None:

        """Terminal results table (rich)."""

        table = Table(title=f"Scholarships - page {page_number}")

        table.add_column("#", justify="right", style="cyan")

        table.add_column("Scholarship Name", overflow="fold")

        table.add_column("Deadline")

        table.add_column("Eligibility")

        table.add_column("Matched Keyword")

        table.add_column("Status")

        for idx, record in enumerate(records, 1):

            table.add_row(

                str(idx),

                record["Scholarship Name"],

                record["Deadline"],

                record["Eligibility"],

                record["Matched Keyword"],

                record["Status"],

            )

        console.print(table)

    def print_verification(verification: dict[int, tuple[str, list[str]]]) -> None:

        """One line per decision - no-match stated explicitly, never skipped."""

        console.print("\n[bold]Verification:[/bold]")

        for idx in sorted(verification):

            name, lines = verification[idx]

            for line in lines:

                console.print(f"  [{idx}] {name}: {line}")

    def print_summary(records: list[dict], page_number: int, saved_path: Path) -> None:

        """Summary with names/links of incomplete & failed records."""

        ok_n = sum(1 for r in records if r["Status"] == STATUS_OK)

        incomplete = [(i, r) for i, r in enumerate(records, 1) if r["Status"] == STATUS_INCOMPLETE]

        failed = [(i, r) for i, r in enumerate(records, 1) if r["Status"] == STATUS_FAILED]

        console.print(f"\n[bold]Summary - page {page_number}[/bold]")

        console.print(f"  Total scholarships: {len(records)}")

        console.print(f"  OK: {ok_n}")

        console.print(f"  Incomplete records: {len(incomplete)}")

        console.print(f"  Failed to load: {len(failed)}")

        if incomplete:

            console.print("\n  [yellow]Incomplete Records:[/yellow]")

            for idx, r in incomplete:

                missing = []

                if r["Deadline"] == MISSING_DEADLINE:

                    missing.append("Deadline not specified")

                if r["Eligibility"] == CATEGORY_UNKNOWN:

                    missing.append("Eligibility unknown")

                console.print(f"    [{idx}] {r['Scholarship Name']} - {', '.join(missing)}")

                console.print(f"        Link: {r['Link']}")

        if failed:

            console.print("\n  [red]Failed Records:[/red]")

            for idx, r in failed:

                console.print(f"    [{idx}] {r['Scholarship Name']}")

                console.print(f"        Link: {r['Link']}")

        console.print(f"\n  Saved to: {saved_path}")

    # ----------------------------------------------------------------------

    # Entry point

    # ----------------------------------------------------------------------

    def main() -> int:

        """Full run: one listing page -> all detail pages -> CSV + report."""

        run_start = datetime.now()

        operation_date = run_start.strftime(DATETIME_FORMAT)

        page_number = prompt_page_number()

        url = build_url(page_number)

        logger.info("Target listing URL: %s", url)

        records: list[dict] = []

        verification: dict[int, tuple[str, list[str]]] = {}

        collected: list[dict] = []

        interrupted = False

        with sync_playwright() as playwright:

            browser, page = launch_browser(playwright)

            try:

                outcome, detail = open_with_retry(page, url)

                if outcome == LOAD_NOT_FOUND:

                    logger.error("Page %d not found (Sayfa Bulunamadı).", page_number)

                    return 1

                if outcome != LOAD_OK:

                    logger.error("Listing page failed: %s", detail)

                    return 1

                collected, _missed, _diag = collect_scholarship_links(page)

                if not collected:

                    logger.warning("Page %d has no scholarships.", page_number)

                    return 0

                logger.info("Found %d scholarship links on page %d.", len(collected), page_number)

                try:

                    for idx, item in enumerate(collected, 1):

                        _politeness_delay()

                        d_outcome, d_detail = open_with_retry(page, item["url"])

                        record = {

                            "Operation Date": operation_date,

                            "Page": page_number,

                            "Scholarship Name": item["name"],

                            "Link": item["url"],

                            "Deadline": FAILED_LOAD,

                            "Eligibility": FAILED_LOAD,

                            "Matched Keyword": MISSING_KEYWORD,

                            "Status": STATUS_FAILED,

                            "_len": 0,

                        }

                        if d_outcome == LOAD_OK:

                            text, _source = extract_visible_text(page)

                            category, matched, ev_elig = classify_eligibility(text)

                            deadline, ev_dead = extract_deadline(text)

                            record.update({

                                "Deadline": deadline,

                                "Eligibility": category,

                                "Matched Keyword": matched,

                                "Status": compute_status(deadline, category),

                                "_len": len(text),

                            })

                            lines = [

                                f"deadline: {deadline}"

                                + (f' | "{ev_dead}"' if ev_dead else " (no label found)"),

                                f"eligibility: {category} (matched: {matched})"

                                + (f' | "{ev_elig}"' if ev_elig else " (no keyword matched)"),

                            ]

                        else:

                            lines = [f"page failed to load ({d_detail})"]

                        records.append(record)

                        verification[idx] = (item["name"], lines)

                        logger.info("[%d/%d] %s -> %s", idx, len(collected), item["name"], record["Status"])

                except KeyboardInterrupt:

                    interrupted = True

                    logger.warning("Interrupted by user (Ctrl+C) - saving partial results.")

                # Targeted retry: only Incomplete records whose text was short.

                if not interrupted:

                    try:
                        for idx, record in enumerate(records, 1):

                            if record["Status"] != STATUS_INCOMPLETE or record["_len"] >= MIN_TEXT_CHARS:

                                continue

                            logger.info("Targeted retry for incomplete [%d] (short text).", idx)

                            _politeness_delay()

                            d_outcome, _ = open_with_retry(page, record["Link"])

                            if d_outcome != LOAD_OK:

                                continue  # a failed retry keeps the first-pass data

                            text, _source = extract_visible_text(page)

                            category, matched, _ev = classify_eligibility(text)

                            deadline, ev_dead = extract_deadline(text)

                            record.update({

                                "Deadline": deadline,

                                "Eligibility": category,

                                "Matched Keyword": matched,

                                "Status": compute_status(deadline, category),

                                "_len": len(text),

                            })

                            name = verification[idx][0]

                            verification[idx] = (name, [

                                f"RETRY OK - deadline: {deadline}"

                                + (f' | "{ev_dead}"' if ev_dead else " (no label found)"),

                                f"RETRY OK - eligibility: {category} (matched: {matched})",

                            ])
                    except KeyboardInterrupt:
                        interrupted = True
                        logger.warning("Interrupted during verification retry; saving partial results.")
            finally:
                try:
                    browser.close()
                except PlaywrightError as exc:
                    logger.warning("Browser shutdown reported an error: %s", exc)
                else:
                    logger.info("Browser closed.")

        # Final in-memory verification pass - zero network requests.

        if not interrupted:

            if len(records) != len(collected):

                logger.error("Row-count mismatch: %d rows vs %d links.", len(records), len(collected))

            for record in records:

                if not record["Scholarship Name"]:

                    logger.error("Empty name for %s", record["Link"])

                if record["Status"] not in (STATUS_OK, STATUS_INCOMPLETE, STATUS_FAILED):

                    logger.error("Invalid status %r for %s", record["Status"], record["Link"])

        if not records:

            logger.warning("No rows collected - no CSV written.")

            return 0

        csv_path = build_csv_path(page_number, run_start)

        if interrupted:

            csv_path = csv_path.with_name(csv_path.stem + PARTIAL_MARKER + csv_path.suffix)

        if csv_path.exists():  # never silently overwrite (#48)

            suffix = datetime.now().strftime(CSV_TIME_SUFFIX_FORMAT)

            csv_path = csv_path.with_name(f"{csv_path.stem}_{suffix}{csv_path.suffix}")

        saved_path = write_csv(csv_path, records)

        # Fixed print order: table -> verification lines -> summary.

        render_table(records, page_number)

        print_verification(verification)

        print_summary(records, page_number, saved_path)

        if interrupted:

            console.print(

                f"[yellow]Interrupted: {len(records)} of {len(collected)} processed. "

                f"Partial CSV saved.[/yellow]"

            )

        return 0

    if __name__ == "__main__":
        try:
            raise SystemExit(main())
        except KeyboardInterrupt:
            console.print("\n[yellow]Cancelled by user (Ctrl+C).[/yellow]")
            raise SystemExit(130)
        except PlaywrightError as exc:
            logger.error("Playwright failed: %s", exc)
            logger.error(
                "Check that Microsoft Edge is installed and that Playwright is installed in the project's venv."
            )
            raise SystemExit(1) 