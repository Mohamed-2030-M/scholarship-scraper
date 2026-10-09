"""Scholarship Scraper - Part 1.

Scrapes one listing page of Turkish scholarships from guncel-egitim.org,
visits each scholarship's detail page, extracts the application deadline
and the eligibility category (with the exact matched keyword), then
prints a terminal table and saves everything to a CSV file.

Keyword lists and settings live at the top of this file - adding a
keyword later is a one-line edit.
"""

import logging
import re

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
# Entry point
# ======================================================================


def main() -> int:
    """Phase 1 scaffold: validate configuration, compilation and logging.

    Browser core, page navigation, extraction and CSV output arrive in
    the upcoming phases - this run only proves the scaffold works.
    """
    compiled_groups = {
        "FOREIGNERS_ONLY": FOREIGNERS_ONLY_PATTERNS,
        "BOTH": BOTH_PATTERNS,
        "TURKISH_ONLY_EXCLUSIVE": TURKISH_ONLY_EXCLUSIVE_PATTERNS,
        "TURKISH_GENERIC": TURKISH_GENERIC_PATTERNS,
    }
    for name, patterns in compiled_groups.items():
        logger.info("%s: %d pattern(s) compiled", name, len(patterns))

    logger.info(
        "Deadline labels: %d | Turkish months: %d | Timeout: %ds x %d attempts",
        len(DEADLINE_LABELS), len(TURKISH_MONTHS), TIMEOUT_SECONDS, MAX_ATTEMPTS,
    )
    logger.info("Phase 1 scaffold OK - browser core arrives in Phase 2.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())