"""Scholarship scraper.

Reads scholarship announcements from guncel-egitim.org through the WordPress
API and uses a local LLM (Ollama) to extract the application deadline and
whether foreign students may apply. Results are kept in one master CSV keyed
by the WordPress post ID. See README.md for usage.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
import time
import unicodedata
from dataclasses import dataclass
from datetime import date, datetime
from functools import lru_cache
from pathlib import Path

import requests
from bs4 import BeautifulSoup

__version__ = "1.0"

SITE = "https://www.guncel-egitim.org"
POSTS_URL = SITE + "/wp-json/wp/v2/posts"
CATEGORIES_URL = SITE + "/wp-json/wp/v2/categories"
CATEGORY_SLUG = "burs-basvurusu"
PER_PAGE = 12
USER_AGENT = f"ScholarshipScraper/{__version__} (educational project)"
SITE_TIMEOUT = 30
LLM_TIMEOUT = 180
PAGE_DELAY = 1.0
MAX_PAGES = 1000

OUTPUT_FOLDER = Path("output")
CACHE_PATH = OUTPUT_FOLDER / "llm_cache.json"
MASTER_PATH = OUTPUT_FOLDER / "scholarships_master.csv"
STATE_PATH = OUTPUT_FOLDER / "state.json"
DATETIME_FORMAT = "%Y-%m-%d %H:%M:%S"

SCRIPT_COLUMNS = [
    "Post ID", "Scholarship Name", "Link", "Deadline", "Deadline Date", "Days Left",
    "Expired", "Foreigners Allowed", "Evidence", "Deadline Evidence", "Status", "Notes",
    "Model", "Modified", "First Seen", "Last Updated",
]
MANUAL_COLUMNS = ["Applied?", "My Notes"]
ALL_COLUMNS = SCRIPT_COLUMNS + MANUAL_COLUMNS

STATUS_OK = "OK"
STATUS_INCOMPLETE = "Incomplete"
STATUS_CHECK = "Check"
STATUS_FAILED = "Failed"

DEFAULT_PROVIDERS = "ollama:qwen2.5:7b,ollama:olmo-3:7b-instruct"

PRESETS = {
    "ollama": dict(
        base_url="http://localhost:11434/v1", model="qwen2.5:7b",
        key_env=None, max_chars=3500, min_interval=0.0,
    ),
    "gemini": dict(
        base_url="https://generativelanguage.googleapis.com/v1beta/openai",
        model="gemini-3-flash-preview",
        key_env="GEMINI_API_KEY", max_chars=12000, min_interval=6.5,
    ),
    "groq": dict(
        base_url="https://api.groq.com/openai/v1", model="llama-3.3-70b-versatile",
        key_env="GROQ_API_KEY", max_chars=12000, min_interval=3.0,
    ),
}

# Used only to shrink long pages: nationality lines first, then deadline lines,
# then conditions.
PRIORITY_WORDS = (
    ("vatandaş", "t.c.", "yabancı", "uyruk", "uluslararası öğrenci", "foreign"),
    ("son başvuru", "başvuru tarih", "son tarih", "bitiş", "başvuru süre", "tarihleri"),
    ("başvuru", "tarih", "şart", "koşul", "kimler"),
)
NATIONALITY_WORDS = ("vatandaş", "t.c.", "yabancı", "uyruk", "uluslararası",
                     "foreign", "international")
TURKISH_MONTHS = {
    "ocak": 1, "şubat": 2, "mart": 3, "nisan": 4, "mayıs": 5, "haziran": 6,
    "temmuz": 7, "ağustos": 8, "eylül": 9, "ekim": 10, "kasım": 11, "aralık": 12,
}

PROMPT = """You read a Turkish scholarship announcement and extract two facts.

TEXT:
\"\"\"
{text}
\"\"\"

Return ONLY a JSON object with exactly these keys:
- "deadline": the FINAL application deadline exactly as written in the text (for example "21 Ekim 2026"). If the text gives a start date and an end date, return the END date. Use null if there is no deadline.
- "deadline_evidence": the exact line or sentence from the text that contains the deadline, or "".
- "foreigners_allowed": "yes" if foreign / international students may apply, "no" if only Turkish citizens (T.C. vatandaşı) may apply, "unknown" if the text does not say.
- "evidence": the exact sentence copied from the text that supports "foreigners_allowed", or "" if unknown.

Rules: copy evidence word for word from the text, never invent text, never guess. JSON only."""


# --- text helpers -------------------------------------------------------

def turkish_lower(text: str) -> str:
    return text.replace("İ", "i").replace("I", "ı").lower()


def squash(text: str) -> str:
    """Letters and digits only, lowercase: makes 'is A inside B' robust."""
    text = turkish_lower(unicodedata.normalize("NFC", str(text)))
    return re.sub(r"[\W_]+", "", text)


def html_to_text(html: str) -> str:
    soup = BeautifulSoup(html or "", "html.parser")
    for tag in soup(["script", "style", "noscript", "iframe"]):
        tag.decompose()
    text = soup.get_text("\n", strip=True)
    return unicodedata.normalize("NFC", text).replace("\xa0", " ")


def select_relevant(text: str, limit: int) -> str:
    """Shrink a long page to `limit` characters, keeping the most useful lines."""
    if len(text) <= limit:
        return text
    lines = [line[:800] for line in text.split("\n")]
    first_seen: dict[str, int] = {}
    for i, line in enumerate(lines):
        first_seen.setdefault(line.strip(), i)
    unique = {i for key, i in first_seen.items() if key}
    chosen = {i for i in range(min(5, len(lines))) if i in unique}
    size = sum(len(lines[i]) + 1 for i in chosen)
    for words in PRIORITY_WORDS:
        for i, line in enumerate(lines):
            if i not in unique or not any(w in turkish_lower(line) for w in words):
                continue
            for j in (i, i - 1, i + 1):
                if 0 <= j < len(lines) and j in unique and j not in chosen:
                    if size + len(lines[j]) + 1 <= limit:
                        chosen.add(j)
                        size += len(lines[j]) + 1
    return "\n".join(lines[i] for i in sorted(chosen))


def parse_deadline(text: str) -> date | None:
    """First full date found, using the last one when a range is given."""
    t = turkish_lower(text or "")
    found: list[tuple[int, date]] = []

    def add(pos: int, year: int, month: int, day: int) -> None:
        try:
            found.append((pos, date(year, month, day)))
        except ValueError:
            pass

    for m in re.finditer(r"(\d{1,2})\s+([a-zçğıöşü]+)\s+(\d{4})", t):
        month = TURKISH_MONTHS.get(m.group(2))
        if month:
            add(m.start(), int(m.group(3)), month, int(m.group(1)))
    for m in re.finditer(r"(\d{1,2})[./](\d{1,2})[./](\d{4})", t):
        add(m.start(), int(m.group(3)), int(m.group(2)), int(m.group(1)))
    return max(found)[1] if found else None


# --- site access --------------------------------------------------------

def http_get(url: str, params: dict | None = None, attempts: int = 3) -> requests.Response:
    last = "no attempts"
    for attempt in range(1, attempts + 1):
        try:
            r = requests.get(url, params=params, timeout=SITE_TIMEOUT,
                             headers={"User-Agent": USER_AGENT})
            if r.status_code not in (429, 500, 502, 503, 504):
                return r
            last = f"HTTP {r.status_code}"
        except requests.RequestException as exc:
            last = f"{type(exc).__name__}: {exc}"
        if attempt < attempts:
            time.sleep(2 * attempt)
    raise RuntimeError(f"GET {url} failed after {attempts} attempts - {last}")


@lru_cache(maxsize=1)
def resolve_category() -> int | None:
    try:
        r = http_get(CATEGORIES_URL, {"slug": CATEGORY_SLUG, "_fields": "id,slug"})
        data = r.json() if r.ok else []
    except (RuntimeError, ValueError):
        data = []
    if data:
        return data[0]["id"]
    print(f"[!] Category '{CATEGORY_SLUG}' not found - using ALL posts instead.")
    return None


def header_int(response: requests.Response, name: str) -> int | None:
    value = response.headers.get(name, "")
    return int(value) if value.isdigit() else None


def fetch_page(page: int, by_modified: bool = False) -> tuple[list[dict], int | None, int | None]:
    """One page of posts plus the site's total page count and post count."""
    params = {
        "per_page": PER_PAGE,
        "page": page,
        "_fields": "id,modified,link,title,content",
    }
    if by_modified:
        params.update(orderby="modified", order="desc")
    category = resolve_category()
    if category:
        params["categories"] = category
    r = http_get(POSTS_URL, params)
    if r.status_code == 400 and page > 1:     # WordPress answers 400 past the last page
        return [], None, None
    r.raise_for_status()
    return r.json(), header_int(r, "X-WP-TotalPages"), header_int(r, "X-WP-Total")


def fetch_posts(page: int, by_modified: bool = False) -> list[dict]:
    return fetch_page(page, by_modified)[0]


# --- LLM access (OpenAI-compatible API) ---------------------------------

class LLMError(Exception):
    def __init__(self, message: str, permanent: bool = False):
        super().__init__(message)
        self.permanent = permanent


@dataclass
class Provider:
    name: str
    base_url: str
    model: str
    key: str | None
    max_chars: int
    min_interval: float
    timeout: int = LLM_TIMEOUT
    last_call: float = 0.0
    json_mode: bool = True

    @property
    def label(self) -> str:
        return f"{self.name}:{self.model}"

    def headers(self) -> dict:
        h = {"Content-Type": "application/json"}
        if self.key:
            h["Authorization"] = f"Bearer {self.key}"
        return h


def build_providers(spec: str, max_chars: int | None = None,
                    timeout: int = LLM_TIMEOUT) -> list[Provider]:
    providers = []
    for item in spec.split(","):
        item = item.strip()
        if not item:
            continue
        name, _, model = item.partition(":")      # "ollama:qwen2.5:7b" -> qwen2.5:7b
        if name not in PRESETS:
            raise SystemExit(f"Unknown provider '{name}'. Choose from: {', '.join(PRESETS)}")
        preset = PRESETS[name]
        key = None
        if preset["key_env"]:
            key = os.environ.get(preset["key_env"])
            if not key:
                raise SystemExit(
                    f"Missing API key for '{name}'. In PowerShell run:\n"
                    f'  $env:{preset["key_env"]} = "your-key"'
                )
        providers.append(Provider(
            name=name, base_url=preset["base_url"], model=model or preset["model"],
            key=key, max_chars=max_chars or preset["max_chars"],
            min_interval=preset["min_interval"], timeout=timeout,
        ))
    if not providers:
        raise SystemExit("No provider given.")
    return providers


def chat(p: Provider, prompt: str) -> str:
    url = p.base_url + "/chat/completions"
    rate_hits = 0
    for attempt in range(1, 5):
        body = {
            "model": p.model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0,
        }
        if p.json_mode:
            body["response_format"] = {"type": "json_object"}
        wait = p.min_interval - (time.time() - p.last_call)
        if wait > 0:
            time.sleep(wait)
        p.last_call = time.time()
        try:
            r = requests.post(url, headers=p.headers(), json=body, timeout=p.timeout)
        except requests.ConnectionError as exc:
            hint = "is Ollama running?" if p.name == "ollama" else str(exc)[:120]
            raise LLMError(f"cannot connect ({hint})", permanent=True)
        except requests.Timeout:
            raise LLMError(f"no answer within {p.timeout} s")
        except requests.RequestException as exc:
            if attempt == 4:
                raise LLMError(type(exc).__name__)
            time.sleep(2 * attempt)
            continue

        if r.status_code == 429:
            rate_hits += 1
            if rate_hits >= 2:
                raise LLMError("rate/quota limit reached", permanent=True)
            print("[!] 429 rate limit - waiting 20 s, then retrying once ...")
            time.sleep(20)
            continue
        if r.status_code == 402:
            raise LLMError("credits used up / payment required", permanent=True)
        if r.status_code in (401, 403, 404):
            hint = f" (see: python main.py --list-models {p.name})" if r.status_code == 404 else ""
            raise LLMError(f"HTTP {r.status_code}: {r.text[:200]}{hint}", permanent=True)
        if r.status_code == 400 and p.json_mode:
            p.json_mode = False
            continue
        if r.status_code >= 500 or r.status_code == 408:
            time.sleep(2 * attempt)
            continue
        if r.status_code >= 400:
            raise LLMError(f"HTTP {r.status_code}: {r.text[:300]}")
        try:
            return r.json()["choices"][0]["message"]["content"] or ""
        except (ValueError, KeyError, IndexError, TypeError):
            raise LLMError("unexpected response format")
    raise LLMError("no valid response after retries")


def parse_json(raw: str) -> dict:
    raw = re.sub(r"<think>.*?</think>", "", raw, flags=re.S)
    raw = re.sub(r"^```(?:json)?|```$", "", raw.strip(), flags=re.M).strip()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", raw, re.S)
        if not m:
            raise ValueError("model did not return JSON")
        data = json.loads(m.group(0))
    if not isinstance(data, dict):
        raise ValueError("model JSON is not an object")
    return data


def clean_result(data: dict) -> dict:
    def text(key: str) -> str:
        value = data.get(key)
        if value is None or str(value).strip().lower() in ("", "null", "none"):
            return ""
        return re.sub(r"\s+", " ", str(value)).strip()

    allowed = text("foreigners_allowed").lower()
    if allowed not in ("yes", "no", "unknown"):
        allowed = "unknown"
    return {
        "deadline": text("deadline"),
        "deadline_evidence": text("deadline_evidence"),
        "foreigners_allowed": allowed,
        "evidence": text("evidence"),
    }


class LLM:
    """Asks the first working model; falls back to the next ones when it fails.

    A failure (timeout, server error, unreadable answer) sends only the current
    scholarship to the next model. A model that is unavailable (not running, not
    installed, quota reached) or fails MAX_FAILURES times in a row is skipped for
    the rest of the run.
    """

    MAX_FAILURES = 3

    def __init__(self, providers: list[Provider]):
        self.providers = providers
        self.failures = [0] * len(providers)
        self.disabled: set[int] = set()

    @property
    def first_model(self) -> str:
        return self.providers[0].model

    def ask(self, text: str) -> tuple[dict, Provider]:
        errors = []
        for i, p in enumerate(self.providers):
            if i in self.disabled:
                continue
            prompt = PROMPT.format(text=select_relevant(text, p.max_chars))
            try:
                try:
                    result = clean_result(parse_json(chat(p, prompt)))
                except ValueError as exc:
                    raise LLMError(f"unreadable answer ({exc})")
            except LLMError as exc:
                self.failures[i] += 1
                errors.append(f"{p.label}: {exc}")
                if exc.permanent or self.failures[i] >= self.MAX_FAILURES:
                    self.disabled.add(i)
                    print(f"[!] {p.label} disabled for this run: {exc}")
                else:
                    print(f"[!] {p.label} failed ({exc}) - trying the next model")
                continue
            self.failures[i] = 0
            return result, p
        everything_down = len(self.disabled) == len(self.providers)
        raise LLMError("; ".join(errors) or "no model available", permanent=everything_down)


def list_models(p: Provider) -> None:
    r = requests.get(p.base_url + "/models", headers=p.headers(), timeout=30)
    r.raise_for_status()
    for model_id in sorted(m.get("id", "") for m in r.json().get("data", [])):
        print(model_id)


# --- verification -------------------------------------------------------

def evaluate(result: dict, full_text: str) -> tuple[str, str]:
    """Check that what the model quoted really exists in the page."""
    source = squash(full_text)
    notes = []
    if result["deadline"] and squash(result["deadline"]) not in source:
        notes.append("deadline text not found in page")
    if result["deadline_evidence"] and squash(result["deadline_evidence"]) not in source:
        notes.append("deadline evidence not found in page")
    if result["foreigners_allowed"] != "unknown":
        if not result["evidence"]:
            notes.append("no evidence for eligibility")
        elif squash(result["evidence"]) not in source:
            notes.append("eligibility evidence not found in page")
        elif not any(w in turkish_lower(result["evidence"]) for w in NATIONALITY_WORDS):
            notes.append("eligibility evidence does not mention nationality")
    if notes:
        return STATUS_CHECK, "; ".join(notes)
    if not result["deadline"] or result["foreigners_allowed"] == "unknown":
        return STATUS_INCOMPLETE, ""
    return STATUS_OK, ""


# --- cache, state and master table --------------------------------------

def load_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_json(path: Path, data: dict) -> None:
    OUTPUT_FOLDER.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")


def columns_for(master: dict[str, dict]) -> list[str]:
    """Script columns, the manual ones, then any extra column you added."""
    extra: list[str] = []
    for row in master.values():
        for key in row:
            if key and key not in ALL_COLUMNS and key not in extra:
                extra.append(key)
    return ALL_COLUMNS + extra


def needs_analysis(master: dict, post: dict, force: bool) -> bool:
    row = master.get(str(post["id"]))
    return (force or row is None
            or row.get("Modified") != post.get("modified", "")
            or row.get("Status") == STATUS_FAILED)


def upsert(master: dict, post: dict, record: dict, now: str) -> None:
    """Insert or update one post; manual columns are left untouched."""
    pid = str(post["id"])
    row = master.setdefault(pid, {"First Seen": now})
    row.update(record)
    row.update({"Post ID": pid, "Modified": post.get("modified", ""), "Last Updated": now})


def refresh_dates(master: dict[str, dict]) -> None:
    """Days Left / Expired depend on today's date, so recompute them for every row."""
    today = date.today()
    for row in master.values():
        try:
            left = (date.fromisoformat(str(row.get("Deadline Date") or "")) - today).days
        except ValueError:
            row["Days Left"], row["Expired"] = "", ""
            continue
        row["Days Left"] = left
        row["Expired"] = "yes" if left < 0 else ""


def load_master_csv() -> dict[str, dict]:
    if not MASTER_PATH.exists():
        return {}
    with open(MASTER_PATH, newline="", encoding="utf-8-sig") as fh:
        return {row["Post ID"]: row for row in csv.DictReader(fh) if row.get("Post ID")}


def save_master_csv(master: dict[str, dict]) -> Path:
    OUTPUT_FOLDER.mkdir(parents=True, exist_ok=True)
    columns = columns_for(master)

    def write(target: Path) -> None:
        with open(target, "w", newline="", encoding="utf-8-sig") as fh:
            writer = csv.DictWriter(fh, fieldnames=columns, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(master.values())

    tmp = MASTER_PATH.with_suffix(".tmp")
    try:
        write(tmp)
        tmp.replace(MASTER_PATH)
        return MASTER_PATH
    except PermissionError:           # the file is open in Excel
        alt = MASTER_PATH.with_name(f"{MASTER_PATH.stem}_{datetime.now():%H%M%S}.csv")
        print(f"[!] {MASTER_PATH.name} is locked (open in Excel?) - writing {alt.name} instead.")
        write(alt)
        return alt


# --- per-post analysis --------------------------------------------------

def analyze_post(post: dict, llm: LLM, cache: dict, use_cache: bool) -> dict:
    """Script-owned columns for one post. Raises LLMError only when every model
    is down, so the post is simply retried on the next run."""
    text = html_to_text(post["content"]["rendered"])
    record = {
        "Scholarship Name": html_to_text(post["title"]["rendered"]).replace("\n", " ").strip(),
        "Link": post["link"],
        "Deadline": "",
        "Deadline Date": "",
        "Foreigners Allowed": "",
        "Evidence": "",
        "Deadline Evidence": "",
        "Status": STATUS_FAILED,
        "Notes": "",
        "Model": "",
    }
    if len(text) < 100:
        record.update(Status=STATUS_INCOMPLETE, Notes="page text is empty or too short")
        return record

    # After editing PROMPT, run with --no-cache so old answers are not reused.
    key = f"{post['id']}|{post.get('modified', '')}|{llm.first_model}"
    cached = cache.get(key) if use_cache else None
    if cached:
        result, model_used = cached["result"], cached["model"] + " (cached)"
    else:
        try:
            result, provider = llm.ask(text)
        except LLMError as exc:
            if exc.permanent:
                raise
            record["Notes"] = f"LLM failed: {exc}"
            return record
        model_used = provider.label
        cache[key] = {"result": result, "model": model_used}
        save_json(CACHE_PATH, cache)

    status, notes = evaluate(result, text)
    deadline_date = parse_deadline(result["deadline"])
    record.update({
        "Deadline": result["deadline"] or "Not specified",
        "Deadline Date": deadline_date.isoformat() if deadline_date else "",
        "Foreigners Allowed": result["foreigners_allowed"],
        "Evidence": result["evidence"],
        "Deadline Evidence": result["deadline_evidence"],
        "Status": status,
        "Notes": notes,
        "Model": model_used,
    })
    return record


# --- command line -------------------------------------------------------

def parse_pages(spec: str) -> tuple[int, int | None]:
    """'all' -> (1, None); '3' -> (3, 3); '2-5' -> (2, 5)."""
    spec = spec.strip().lower()
    if spec == "all":
        return 1, None
    m = re.fullmatch(r"(\d+)(?:-(\d+))?", spec)
    if not m:
        raise SystemExit(f'Invalid --pages value "{spec}". Use e.g. 3, 1-5 or all.')
    first, last = int(m.group(1)), int(m.group(2) or m.group(1))
    if first < 1 or last < first:
        raise SystemExit(f'Invalid page range "{spec}".')
    return first, last


def iter_pages(first: int, last: int | None):
    page = first
    while page <= (last if last is not None else MAX_PAGES):
        yield page
        page += 1


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="Scholarship scraper (WordPress API + LLM)")
    ap.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    ap.add_argument("--pages", metavar="SPEC",
                    help='pages to scan: "3", "1-5" or "all". Default: every page on the '
                         "first run, afterwards only new or changed scholarships")
    ap.add_argument("--page", type=int, help=argparse.SUPPRESS)
    ap.add_argument("--limit", type=int, help="only process the first N posts of each page")
    ap.add_argument("--full", action="store_true", help="scan every page again")
    ap.add_argument("--reanalyze", action="store_true",
                    help="ask the model again even for unchanged posts "
                         "(add --no-cache after editing the prompt)")
    ap.add_argument("--providers", default=DEFAULT_PROVIDERS,
                    help="comma list of models, tried in order (default: %(default)s)")
    ap.add_argument("--max-chars", type=int, metavar="N",
                    help="characters of a page sent to the model")
    ap.add_argument("--timeout", type=int, default=LLM_TIMEOUT, metavar="SEC",
                    help=f"seconds to wait for one model answer (default {LLM_TIMEOUT})")
    ap.add_argument("--no-cache", action="store_true", help="ignore saved model answers")
    ap.add_argument("--list-models", metavar="PROVIDER", help="print model names and exit")
    return ap.parse_args()


def print_summary(master: dict[str, dict], touched: list[dict], where: str) -> None:
    counts = {s: sum(1 for r in master.values() if r.get("Status") == s)
              for s in (STATUS_OK, STATUS_INCOMPLETE, STATUS_CHECK, STATUS_FAILED)}
    open_now = sum(1 for r in master.values()
                   if str(r.get("Days Left", "")).lstrip("-").isdigit() and int(r["Days Left"]) >= 0)
    print(f"\nThis run: {len(touched)} scholarships analyzed")
    print(f"Master table: {len(master)} scholarships, {open_now} with an open deadline")
    for status, n in counts.items():
        print(f"  {status}: {n}")
    problems = [r for r in touched if r.get("Status") != STATUS_OK]
    if problems:
        print("\nNeeds a human look (from this run):")
        for r in problems:
            print(f"  {r['Scholarship Name']} -> {r['Status']} {r.get('Notes', '')}")
            print(f"    {r['Link']}")
    print(f"\nSaved to: {where}")


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):      # Turkish letters on Windows consoles
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    args = parse_args()

    if args.list_models:
        list_models(build_providers(args.list_models, timeout=args.timeout)[0])
        return 0

    llm = LLM(build_providers(args.providers, args.max_chars, args.timeout))
    master = load_master_csv()
    state = load_json(STATE_PATH)

    spec = args.pages or (str(args.page) if args.page else None)
    if spec:
        mode = "range"
    elif args.full or not state.get("last_complete_scan"):
        mode, spec = "full", "all"
    else:
        mode, spec = "incremental", "all"
    incremental = mode == "incremental"
    first, last = parse_pages(spec)

    now = datetime.now().strftime(DATETIME_FORMAT)
    cache = load_json(CACHE_PATH)
    seen: set[int] = set()
    touched: list[dict] = []
    finished = False        # reached the last page, or the incremental early stop
    stopped = False         # interrupted or stopped by an error

    models = " -> ".join(p.label for p in llm.providers)
    print(f"scholarship-scraper v{__version__} | mode: {mode} | pages: {spec} | models: {models}")
    print(f"Master table has {len(master)} scholarships ({MASTER_PATH}).\n")

    try:
        for page in iter_pages(first, last):
            posts, total_pages, total_posts = fetch_page(page, by_modified=incremental)
            if page == first and total_pages:
                print(f"The site reports {total_posts} scholarships on {total_pages} pages.\n")
            if not posts:
                finished = True
                print(f"Page {page} is empty - reached the end.")
                break
            if args.limit:
                posts = posts[: args.limit]

            fresh = [p for p in posts if p["id"] not in seen]
            seen.update(p["id"] for p in fresh)
            todo = [p for p in fresh if needs_analysis(master, p, args.reanalyze)]
            of_total = f"/{total_pages}" if total_pages else ""
            print(f"Page {page}{of_total}: {len(fresh)} posts, {len(todo)} to analyze, "
                  f"{len(fresh) - len(todo)} unchanged")

            for idx, post in enumerate(todo, 1):
                upsert(master, post, analyze_post(post, llm, cache, not args.no_cache), now)
                row = master[str(post["id"])]
                touched.append(row)
                print(f"  [p{page} {idx}/{len(todo)}] {row['Scholarship Name']}\n"
                      f"   -> {row['Status']} | deadline: {row['Deadline'] or '-'}"
                      f" | foreigners: {row['Foreigners Allowed'] or '-'}"
                      + (f" | {row['Notes']}" if row["Notes"] else ""))

            if todo:
                refresh_dates(master)
                save_master_csv(master)
            if incremental and not todo:
                finished = True
                print("Nothing new or changed on this page - stopping early.")
                break
            if total_pages and page >= total_pages:
                finished = True
                break
            time.sleep(PAGE_DELAY)
    except KeyboardInterrupt:
        stopped = True
        print("\n[!] Interrupted - saving what was done so far.")
    except LLMError as exc:
        stopped = True
        print(f"\n[!] No model available any more: {exc}")
        print("[!] Progress is saved. Run the same command later (cached answers are reused).")
    except (RuntimeError, requests.RequestException) as exc:
        stopped = True
        print(f"\n[!] Stopped by an error: {exc} - saving what was done so far.")

    where = "nothing to save"
    if master:
        refresh_dates(master)
        where = str(save_master_csv(master))

    if finished and not stopped and spec == "all" and not args.limit:
        state["last_complete_scan"] = now
        save_json(STATE_PATH, state)

    print_summary(master, touched, where)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())