"""Scholarship scraper - Part 1 (browser core + listing page).

Opens a listing page of https://www.guncel-egitim.org/burslar/burs-basvurusu/
in headless Microsoft Edge (Playwright, sync API), collects the scholarship
card links of the chosen page and prints them for verification.

Run with: python main.py
"""

import logging
import random
import re
import time
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
# Entries are plain strings (escaped, substring match) OR compiled
# re.Pattern objects written in already-lowercase form (used as-is).
# ----------------------------------------------------------------------
KeywordItem = str | re.Pattern

FOREIGNERS_ONLY_KEYWORDS: list[KeywordItem] = [
    # "sadece yabancı dil" (= "foreign language only") must NOT match, so the
    # phrase is restricted to nationality/student wording.
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
    # Foreigners have a separate quota (the bare phrase "ayrı kotalarda" is too
    # generic and was removed).
    re.compile(r"yabancı\w*( uyruklu\w*)?( öğrenci\w*)? için ayrı( bir)? kota\w*"),
    "foreign students",
]

TURKISH_ONLY_EXCLUSIVE_KEYWORDS: list[KeywordItem] = [
    # Explicit exclusions - strong "Turkish only" signals.
    # The regex allows ONE extra word between the phrases, e.g.
    # "yabancı uyruklu öğrenciler başvuramaz" - a plain substring would miss it
    # AND the page would wrongly match "yabancı uyruklu öğrenci" in BOTH_KEYWORDS.
    re.compile(
        r"yabancı\w*( uyruklu\w*)?( \w+)? "
        r"(başvuramaz|başvuru yapamaz|kabul edilmez)"
    ),
    "sadece türkiye cumhuriyeti vatandaşları",
    "sadece t.c. vatandaşları",
    "sadece türk vatandaşları",
]

TURKISH_GENERIC_KEYWORDS: list[KeywordItem] = [
    # Generic mentions - appear on both-accepted pages too.
    "türkiye cumhuriyeti vatandaşı olmak",
    "t.c. vatandaşı olmak",
    "türk vatandaşı olmak",
]

DEADLINE_LABELS: list[str] = [
    # Lowered form; compared after turkish_lower. Longer labels first.
    # "başvuru tarihleri" (plural) is NOT a substring of "başvuru tarihi".
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
SITE_HOST = "guncel-egitim.org"  # normalized form: lowercase, no "www."

TIMEOUT_SECONDS = 120  # seconds - converted to ms for Playwright
MAX_ATTEMPTS = 3
RETRYABLE_STATUS_CODES = {408, 429}  # plus every 5xx
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

# Display values - each defined ONCE; the other names are aliases.
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

PAGE_NOT_FOUND_TEXT = "sayfa bulunamadı"  # compared after turkish_lower

# Outcomes of a single page-load attempt.
LOAD_OK = "ok"
LOAD_RETRYABLE = "retryable"
LOAD_NOT_FOUND = "not_found"
LOAD_FAILED = "failed"

# ----------------------------------------------------------------------
# Link-discovery constants
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
# Helpers
# ----------------------------------------------------------------------


def turkish_lower(text: str) -> str:
    """Lowercase with Turkish rules while preserving string length.

    'İ'.lower() returns TWO chars in Python (i + combining dot), which would
    shift indices. Replacing first keeps len(text) unchanged.
    """
    return text.replace("İ", "i").replace("I", "ı").lower()


def compile_keywords(items: list[KeywordItem]) -> list[re.Pattern]:
    """Compile a keyword list once at startup.

    Compiled patterns are used as-is (they are written lowercase); plain
    strings are lowercased and escaped, so '.' in 't.c.' stays a literal dot.
    """
    return [
        item if isinstance(item, re.Pattern)
        else re.compile(re.escape(turkish_lower(item)))
        for item in items
    ]


FOREIGNERS_ONLY_PATTERNS = compile_keywords(FOREIGNERS_ONLY_KEYWORDS)
BOTH_PATTERNS = compile_keywords(BOTH_KEYWORDS)
TURKISH_ONLY_EXCLUSIVE_PATTERNS = compile_keywords(TURKISH_ONLY_EXCLUSIVE_KEYWORDS)
TURKISH_GENERIC_PATTERNS = compile_keywords(TURKISH_GENERIC_KEYWORDS)

# ----------------------------------------------------------------------
# Browser core
# ----------------------------------------------------------------------


def launch_browser(playwright: Playwright) -> tuple[Browser, Page]:
    """Launch Microsoft Edge headless and return (browser, page).

    The context carries the explicit User-Agent (politeness).
    """
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


def open_page(page: Page, url: str) -> tuple[str, str]:
    """Open a URL once - no retry. Returns (outcome, detail).

    Outcomes: LOAD_OK / LOAD_RETRYABLE (timeout, network, 5xx, 408, 429)
    / LOAD_NOT_FOUND (404 or marker text) / LOAD_FAILED (other 4xx).
    """
    timeout_ms = TIMEOUT_SECONDS * 1000  # Playwright expects milliseconds!
    try:
        response = page.goto(url, timeout=timeout_ms, wait_until="domcontentloaded")
    except (PlaywrightTimeoutError, PlaywrightError) as exc:
        return LOAD_RETRYABLE, f"navigation error - {type(exc).__name__}: {exc}"

    # Guard: response may be None (no HTTP status available).
    if response is not None:
        status = response.status
        if status == 404:
            return LOAD_NOT_FOUND, "HTTP 404"
        if status >= 500 or status in RETRYABLE_STATUS_CODES:
            return LOAD_RETRYABLE, f"HTTP {status}"
        if 400 <= status < 500:
            return LOAD_FAILED, f"HTTP {status} (non-retryable)"

    # Status is fine (or unavailable) - soft not-found check: the site
    # may serve the not-found page with status 200.
    try:
        body_text = page.inner_text("body")
    except PlaywrightError:
        return LOAD_RETRYABLE, "could not read page text"
    if PAGE_NOT_FOUND_TEXT in turkish_lower(body_text):
        return LOAD_NOT_FOUND, "not-found marker in page text"
    return LOAD_OK, "ok"


def open_with_retry(page: Page, url: str, attempts: int = MAX_ATTEMPTS) -> tuple[str, str]:
    """Open a URL with up to `attempts` tries and 2-5 s backoff.

    Retries only LOAD_RETRYABLE outcomes; LOAD_NOT_FOUND and LOAD_FAILED
    are deterministic and return immediately. Returns the final
    (outcome, detail).
    """
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
    """Ask for a listing page number; re-ask until a valid positive integer."""
    while True:
        raw = console.input("Enter page number: ").strip()
        if raw.isdigit() and int(raw) >= 1:
            return int(raw)
        logger.warning("Invalid input %r - enter a positive integer (1, 2, 3, ...).", raw)


def build_url(page_number: int) -> str:
    """Build the listing-page URL for the given page number.

    Note: /page/1/ 301-redirects to the category base URL on this site;
    the content is identical and Playwright follows redirects silently.
    """
    return LIST_URL_TEMPLATE.format(n=page_number)


def normalize_url(href: str) -> str | None:
    """Normalize a URL for dedupe: https, lowercase host, no www.,
    no fragment/query, trailing slash on directory-style paths."""
    if not href:
        return None
    parts = urlsplit(href)
    if parts.scheme not in ("http", "https"):
        return None  # mailto:, javascript:, tel:, data: ...
    host = parts.netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    path = parts.path or "/"
    last_segment = path.rstrip("/").rsplit("/", 1)[-1]
    if "." not in last_segment and not path.endswith("/"):
        path += "/"
    return urlunsplit(("https", host, path, "", ""))


def _split_anchor_regions(page: Page) -> tuple[list[dict], list[dict], list[dict]]:
    """Split body anchors into (content, widget_filtered, outside).

    content         - anchors inside the listing region (#content)
    widget_filtered - content anchors removed by the sidebar/widget rule
    outside         - anchors elsewhere (header, nav, footer, top bar)
    """
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
        return False  # grouped sections (/burslar/...), pagination, homepage
    if segments[0] in EXCLUDED_SLUGS:
        return False
    return bool(YEAR_IN_TEXT.search(text))  # cards always carry a year


def collect_scholarship_links(page: Page) -> tuple[list[dict], list[dict], dict]:
    """Collect scholarship card links from the listing page.

    Returns (collected, missed, diagnostics):
      collected  - card links passing all rules (deduped, first non-empty
                   name kept per URL)
      missed     - year-bearing card-like links that did NOT pass because
                   they were outside the region or inside the sidebar -
                   exactly the pollution we must inspect
      diagnostics- anchor counts per stage
    """
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
# Entry point
# ----------------------------------------------------------------------


def main() -> int:
    """Phase 3: open the listing page and collect scholarship card links
    with the confirmed pattern, then print them for verification."""
    page_number = prompt_page_number()
    url = build_url(page_number)
    logger.info("Target listing URL: %s", url)

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

            collected, missed, diag = collect_scholarship_links(page)
            logger.info(
                "Anchors: raw=%d, in_content=%d, widget_filtered=%d, outside=%d",
                diag["raw"], diag["in_content"], diag["widget_filtered"],
                diag["outside_content"],
            )

            if not collected:
                logger.warning("Page %d has no scholarships.", page_number)
                return 0

            console.print(
                f"\n[bold]COLLECTED - page {page_number} ({len(collected)} cards):[/bold]"
            )
            for i, item in enumerate(collected, 1):
                console.print(f"  {i:2d}. {item['name']}")
                console.print(f"      {item['url']}")

            if missed:
                console.print(
                    f"\n[bold yellow]MISSED (card-like links rejected by the "
                    f"region/sidebar rules - verify these are NOT cards of "
                    f"THIS page) ({len(missed)}):[/bold yellow]"
                )
                for i, item in enumerate(missed, 1):
                    console.print(f"  {i:2d}. {item['name']}")
                    console.print(f"      {item['url']}")
            console.print(
                "\n[bold]--> Compare COLLECTED with the visible card grid, "
                "then paste both runs back.[/bold]"
            )
        finally:
            browser.close()
            logger.info("Browser closed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())