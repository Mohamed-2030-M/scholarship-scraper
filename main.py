"""Scholarship Scraper - Part 1.

Scrapes one listing page of Turkish scholarships from guncel-egitim.org,
visits each scholarship's detail page, extracts the application deadline
and the eligibility category (with the exact matched keyword), then
prints a terminal table and saves everything to a CSV file.

Keyword lists and settings live at the top of this file - adding a
keyword later is a one-line edit.
"""

import logging
import random
import re
import time

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

# ======================================================================
# Console & logging
# ======================================================================

console = Console()

logging.basicConfig(
    level=logging.INFO,
    format="%(message)s",  # RichHandler already renders time and level
    datefmt="[%X]",
    handlers=[RichHandler(console=console, rich_tracebacks=True)],
)
logger = logging.getLogger("scholarship-scraper")

# ======================================================================
# Keyword configuration
# ======================================================================

KeywordItem = str | re.Pattern

FOREIGNERS_ONLY_KEYWORDS: list[KeywordItem] = [
    # "sadece yabancı dil" (= "foreign language only") must NOT match,
    # so the phrase is restricted to nationality/student wording.
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
    # Foreigners have a separate quota. The bare phrase "ayrı kotalarda"
    # is too generic, so the pattern ties it to the foreign-student context.
    re.compile(r"yabancı\w*( uyruklu\w*)?( öğrenci\w*)? için ayrı( bir)? kota\w*"),
    "foreign students",
]

TURKISH_ONLY_EXCLUSIVE_KEYWORDS: list[KeywordItem] = [
    # Explicit exclusions - strong "Turkish only" signals.
    # The regex allows ONE extra word between the phrases, e.g.
    # "yabancı uyruklu öğrenciler başvuramaz" - a plain substring would
    # miss it AND the page would wrongly match "yabancı uyruklu öğrenci".
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
    # Written in lowered form; compared after turkish_lower.
    # Longer/more specific labels first.
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

# ======================================================================
# Settings
# ======================================================================

BASE_URL = "https://www.guncel-egitim.org"
LIST_URL_TEMPLATE = BASE_URL + "/burslar/burs-basvurusu/page/{n}/"
USER_AGENT = "ScholarshipScraper/1.0 (educational project)"

TIMEOUT_SECONDS = 120                 # seconds - passed to Playwright as TIMEOUT_SECONDS * 1000 (ms!)
MAX_ATTEMPTS = 3
RETRYABLE_STATUS_CODES = {408, 429}   # plus every 5xx
BACKOFF_MIN_SECONDS = 2
BACKOFF_MAX_SECONDS = 5
DETAIL_DELAY_MIN_SECONDS = 0.5
DETAIL_DELAY_MAX_SECONDS = 1.0

MIN_TEXT_CHARS = 300                  # below this a text source is rejected / page counts as partially loaded
SEGMENT_MAX_CHARS = 120               # deadline segment cap

OUTPUT_FOLDER = "output"
CSV_DATE_FORMAT = "%d_%m_%Y"          # leading zeros: 09_10_2026
CSV_TIME_SUFFIX_FORMAT = "%H%M%S"     # collision / PermissionError suffix
PARTIAL_MARKER = "_partial"           # interrupted runs
DATETIME_FORMAT = "%Y-%m-%d %H:%M:%S"

# Display values - no magic strings. Each value is defined ONCE;
# the other names are aliases.
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

# Internal load outcomes (control flow - not display values).
LOAD_OK = "load_ok"
LOAD_RETRYABLE = "load_retryable"
LOAD_NOT_FOUND = "load_not_found"
LOAD_FAILED = "load_failed"

# ======================================================================
# Text helpers
# ======================================================================


def turkish_lower(text: str) -> str:
    """Lowercase with Turkish rules while preserving string length.

    'İ'.lower() returns TWO chars in Python (i + combining dot), which
    would shift indices. Replacing first keeps len(text) unchanged.
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


# Compiled once at startup.
FOREIGNERS_ONLY_PATTERNS: list[re.Pattern] = compile_keywords(FOREIGNERS_ONLY_KEYWORDS)
BOTH_PATTERNS: list[re.Pattern] = compile_keywords(BOTH_KEYWORDS)
TURKISH_ONLY_EXCLUSIVE_PATTERNS: list[re.Pattern] = compile_keywords(TURKISH_ONLY_EXCLUSIVE_KEYWORDS)
TURKISH_GENERIC_PATTERNS: list[re.Pattern] = compile_keywords(TURKISH_GENERIC_KEYWORDS)


# ======================================================================
# Browser core
# ======================================================================


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

    Outcomes:
      LOAD_OK         - loaded; passed the not-found text check
      LOAD_RETRYABLE  - transient failure (timeout / network / 5xx / 408 / 429)
      LOAD_NOT_FOUND  - deterministic not-found (HTTP 404 or marker text)
      LOAD_FAILED     - non-retryable error (e.g. 403)
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
    body_text = page.inner_text("body")
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


# ======================================================================
# Entry point
# ======================================================================


def main() -> int:
    """Phase 2 smoke test: launch Edge headless and load a page through
    the retry core (example.com - the target site arrives in Phase 3).
    """
    with sync_playwright() as playwright:
        browser, page = launch_browser(playwright)
        try:
            outcome, detail = open_with_retry(page, "https://example.com")
            if outcome != LOAD_OK:
                logger.error("Unexpected outcome for example.com: %s (%s)", outcome, detail)
                return 1
            logger.info("Loaded example.com (%s) - title: %s", detail, page.title())
        finally:
            browser.close()
            logger.info("Browser closed.")
    logger.info("Phase 2 browser core OK - listing page arrives in Phase 3.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())