"""main2.py - Scholarship analyzer: WordPress JSON API + an LLM.

Pipeline
  1. Fetch one page of posts from the site's WordPress REST API (no browser).
  2. Clean the HTML into plain text and keep the most relevant lines.
  3. Ask an LLM (local Ollama or a free cloud API) for the deadline and
     whether foreign students may apply.
  4. Verify that the evidence the model quotes really exists in the page.
  5. Save a CSV and print a summary.

Install:
    pip install requests beautifulsoup4

Examples (PowerShell):
    python main2.py --page 1 --limit 3
    python main2.py --page 1 --providers ollama:qwen2.5:3b
    $env:GEMINI_API_KEY = "your-key"
    python main2.py --page 1 --providers gemini,ollama      # cloud first, local backup
    python main2.py --list-models gemini                    # see valid model names
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
from datetime import datetime
from pathlib import Path

import requests
from bs4 import BeautifulSoup

# ----------------------------------------------------------------------
# Settings
# ----------------------------------------------------------------------
SITE = "https://www.guncel-egitim.org"
POSTS_URL = SITE + "/wp-json/wp/v2/posts"
CATEGORIES_URL = SITE + "/wp-json/wp/v2/categories"
CATEGORY_SLUG = "burs-basvurusu"  # same category as /burslar/burs-basvurusu/
PER_PAGE = 12                     # the old listing page had 12 cards
USER_AGENT = "ScholarshipScraper/1.0 (educational project)"
SITE_TIMEOUT = 30
LLM_TIMEOUT = 300                 # the first local call loads the model - be patient

OUTPUT_FOLDER = Path("output")
CACHE_PATH = OUTPUT_FOLDER / "llm_cache.json"
DATETIME_FORMAT = "%Y-%m-%d %H:%M:%S"

CSV_COLUMNS = [
    "Operation Date", "Page", "Scholarship Name", "Link", "Deadline",
    "Foreigners Allowed", "Evidence", "Deadline Evidence", "Status",
    "Notes", "Model",
]

STATUS_OK = "OK"
STATUS_INCOMPLETE = "Incomplete"   # model found nothing for one field
STATUS_CHECK = "Check"             # model's answer failed verification
STATUS_FAILED = "Failed"           # page or LLM call failed

# Provider presets (all speak the OpenAI-compatible /chat/completions API).
# Cloud model names change often - use --list-models or --providers name:model.
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

# Used ONLY to shrink long pages (the LLM still makes the decisions).
# Lines are kept by priority: 1) nationality words, 2) deadline words, 3) conditions.
PRIORITY_WORDS = (
    ("vatandaş", "t.c.", "yabancı", "uyruk", "uluslararası öğrenci", "foreign"),
    ("son başvuru", "başvuru tarih", "son tarih", "bitiş", "başvuru süre", "tarihleri"),
    ("başvuru", "tarih", "şart", "koşul", "kimler"),
)

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


# ----------------------------------------------------------------------
# Text helpers
# ----------------------------------------------------------------------
def turkish_lower(text: str) -> str:
    return text.replace("İ", "i").replace("I", "ı").lower()


def squash(text: str) -> str:
    """Lowercase letters/digits only - used to check 'is A inside B' robustly."""
    text = turkish_lower(unicodedata.normalize("NFC", str(text)))
    return re.sub(r"[\W_]+", "", text)


def html_to_text(html: str) -> str:
    soup = BeautifulSoup(html or "", "html.parser")
    for tag in soup(["script", "style", "noscript", "iframe"]):
        tag.decompose()
    text = soup.get_text("\n", strip=True)
    return unicodedata.normalize("NFC", text).replace("\xa0", " ")


def select_relevant(text: str, limit: int) -> str:
    """Shrink long pages to `limit` chars: first lines + priority lines.

    Duplicate lines are dropped, nationality lines are kept first, then
    deadline lines, then conditions. Output keeps the original order."""
    if len(text) <= limit:
        return text
    lines = [line[:500] for line in text.split("\n")]
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


# ----------------------------------------------------------------------
# Site access (WordPress REST API)
# ----------------------------------------------------------------------
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


def fetch_posts(page_number: int) -> list[dict]:
    params = {
        "per_page": PER_PAGE,
        "page": page_number,
        "_fields": "id,modified,link,title,content",
    }
    category = resolve_category()
    if category:
        params["categories"] = category
    r = http_get(POSTS_URL, params)
    if r.status_code == 400:  # WordPress answers 400 for a page past the end
        return []
    r.raise_for_status()
    return r.json()


# ----------------------------------------------------------------------
# LLM access (OpenAI-compatible: Ollama, Gemini, Groq, ...)
# ----------------------------------------------------------------------
class LLMError(Exception):
    def __init__(self, message: str, permanent: bool = False):
        super().__init__(message)
        self.permanent = permanent  # True -> this provider is unusable now


@dataclass
class Provider:
    name: str
    base_url: str
    model: str
    key: str | None
    max_chars: int
    min_interval: float
    last_call: float = 0.0

    def headers(self) -> dict:
        h = {"Content-Type": "application/json"}
        if self.key:
            h["Authorization"] = f"Bearer {self.key}"
        return h


def build_providers(spec: str) -> list[Provider]:
    providers = []
    for item in spec.split(","):
        item = item.strip()
        if not item:
            continue
        name, _, model = item.partition(":")  # "ollama:qwen2.5:3b" -> qwen2.5:3b
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
            key=key, max_chars=preset["max_chars"], min_interval=preset["min_interval"],
        ))
    if not providers:
        raise SystemExit("No provider given.")
    return providers


def chat(p: Provider, prompt: str) -> str:
    body = {
        "model": p.model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0,
        "response_format": {"type": "json_object"},
    }
    url = p.base_url + "/chat/completions"
    rate_hits = 0
    for attempt in range(1, 5):
        wait = p.min_interval - (time.time() - p.last_call)
        if wait > 0:
            time.sleep(wait)
        p.last_call = time.time()
        try:
            r = requests.post(url, headers=p.headers(), json=body, timeout=LLM_TIMEOUT)
        except requests.ConnectionError as exc:
            hint = "is Ollama running?" if p.name == "ollama" else str(exc)[:120]
            raise LLMError(f"cannot connect to {p.name} ({hint})", permanent=True)
        except requests.RequestException as exc:
            if attempt == 4:
                raise LLMError(f"{type(exc).__name__} while calling {p.name}")
            time.sleep(2 * attempt)
            continue

        if r.status_code == 429:
            rate_hits += 1
            if rate_hits >= 2:
                raise LLMError(f"rate/quota limit reached on {p.name}", permanent=True)
            print("[!] 429 rate limit - waiting 20 s, then retrying once ...")
            time.sleep(20)
            continue
        if r.status_code in (401, 403, 404):
            raise LLMError(
                f"HTTP {r.status_code} from {p.name}/{p.model}: {r.text[:200]}", permanent=True
            )
        if r.status_code == 400 and "response_format" in body:
            body.pop("response_format")  # this provider has no JSON mode - retry without
            continue
        if r.status_code >= 500 or r.status_code == 408:
            time.sleep(2 * attempt)
            continue
        if r.status_code >= 400:
            raise LLMError(f"HTTP {r.status_code}: {r.text[:300]}")
        try:
            return r.json()["choices"][0]["message"]["content"] or ""
        except (ValueError, KeyError, IndexError):
            raise LLMError("unexpected response format from the model server")
    raise LLMError(f"no valid response from {p.name} after retries")


class LLM:
    """Holds the provider list; on a permanent failure moves to the next one."""

    def __init__(self, providers: list[Provider]):
        self.providers = providers
        self.index = 0

    @property
    def first_model(self) -> str:
        return self.providers[0].model

    def ask(self, text: str) -> tuple[str, Provider]:
        while True:
            p = self.providers[self.index]
            prompt = PROMPT.format(text=select_relevant(text, p.max_chars))
            try:
                return chat(p, prompt), p
            except LLMError as exc:
                if exc.permanent and self.index + 1 < len(self.providers):
                    nxt = self.providers[self.index + 1]
                    print(f"[!] {p.name}/{p.model} unavailable: {exc}")
                    print(f"[!] Switching to {nxt.name}/{nxt.model} for the rest of this run.")
                    self.index += 1
                    continue
                raise


def list_models(p: Provider) -> None:
    r = requests.get(p.base_url + "/models", headers=p.headers(), timeout=30)
    r.raise_for_status()
    for m in r.json().get("data", []):
        print(m.get("id"))


def parse_json(raw: str) -> dict:
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


# ----------------------------------------------------------------------
# Verification: never trust the model blindly
# ----------------------------------------------------------------------
def evaluate(result: dict, full_text: str) -> tuple[str, str]:
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

    if notes:
        return STATUS_CHECK, "; ".join(notes)
    if not result["deadline"] or result["foreigners_allowed"] == "unknown":
        return STATUS_INCOMPLETE, ""
    return STATUS_OK, ""


# ----------------------------------------------------------------------
# Cache (saves free-tier quota: a re-run never asks twice)
# ----------------------------------------------------------------------
def load_cache() -> dict:
    try:
        return json.loads(CACHE_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_cache(cache: dict) -> None:
    OUTPUT_FOLDER.mkdir(parents=True, exist_ok=True)
    CACHE_PATH.write_text(json.dumps(cache, ensure_ascii=False, indent=1), encoding="utf-8")


# ----------------------------------------------------------------------
# Per-post analysis
# ----------------------------------------------------------------------
def analyze_post(post: dict, llm: LLM, cache: dict, use_cache: bool) -> dict:
    name = html_to_text(post["title"]["rendered"]).replace("\n", " ").strip()
    text = html_to_text(post["content"]["rendered"])
    record = {
        "Scholarship Name": name,
        "Link": post["link"],
        "Deadline": "",
        "Foreigners Allowed": "",
        "Evidence": "",
        "Deadline Evidence": "",
        "Status": STATUS_FAILED,
        "Notes": "",
        "Model": "",
    }
    if len(text) < 100:
        record["Notes"] = "page text is empty or too short"
        return record

    key = f"{post['id']}|{post.get('modified', '')}|{llm.first_model}"
    cached = cache.get(key) if use_cache else None
    if cached:
        result, model_used = cached["result"], cached["model"] + " (cached)"
    else:
        try:
            raw, provider = llm.ask(text)
            result = clean_result(parse_json(raw))
        except (LLMError, ValueError) as exc:
            record["Notes"] = f"LLM failed: {exc}"
            return record
        model_used = f"{provider.name}:{provider.model}"
        cache[key] = {"result": result, "model": model_used}
        save_cache(cache)

    status, notes = evaluate(result, text)
    record.update({
        "Deadline": result["deadline"] or "Not specified",
        "Foreigners Allowed": result["foreigners_allowed"],
        "Evidence": result["evidence"],
        "Deadline Evidence": result["deadline_evidence"],
        "Status": status,
        "Notes": notes,
        "Model": model_used,
    })
    return record


# ----------------------------------------------------------------------
# Output
# ----------------------------------------------------------------------
def write_csv(path: Path, rows: list[dict]) -> Path:
    def _write(target: Path) -> None:
        with open(target, "w", newline="", encoding="utf-8-sig") as fh:
            writer = csv.writer(fh)
            writer.writerow(CSV_COLUMNS)
            for row in rows:
                writer.writerow([row.get(col, "") for col in CSV_COLUMNS])

    OUTPUT_FOLDER.mkdir(parents=True, exist_ok=True)
    if path.exists():  # never overwrite an earlier run
        path = path.with_name(f"{path.stem}_{datetime.now():%H%M%S}{path.suffix}")
    try:
        _write(path)
        return path
    except PermissionError:  # file is open in Excel
        alt = path.with_name(f"{path.stem}_{datetime.now():%H%M%S}{path.suffix}")
        print(f"[!] PermissionError - writing to {alt.name} instead.")
        _write(alt)
        return alt


def print_summary(rows: list[dict], page: int, saved: Path) -> None:
    counts = {s: sum(1 for r in rows if r["Status"] == s)
              for s in (STATUS_OK, STATUS_INCOMPLETE, STATUS_CHECK, STATUS_FAILED)}
    print(f"\nSummary - page {page}: {len(rows)} scholarships")
    for status, n in counts.items():
        print(f"  {status}: {n}")
    problems = [(i, r) for i, r in enumerate(rows, 1) if r["Status"] != STATUS_OK]
    if problems:
        print("\nNeeds a human look:")
        for i, r in problems:
            print(f"  [{i}] {r['Scholarship Name']} -> {r['Status']} {r['Notes']}")
            print(f"       {r['Link']}")
    print(f"\nSaved to: {saved}")


# ----------------------------------------------------------------------
# Entry point
# ----------------------------------------------------------------------
def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="Scholarship analyzer (API + LLM)")
    ap.add_argument("--page", type=int, help="listing page number (asked if omitted)")
    ap.add_argument("--limit", type=int, help="only process the first N posts (for testing)")
    ap.add_argument("--providers", default="ollama",
                    help="comma list, first is tried first. e.g. gemini,ollama:qwen2.5:3b")
    ap.add_argument("--no-cache", action="store_true", help="ignore saved model answers")
    ap.add_argument("--list-models", metavar="PROVIDER", help="print model names and exit")
    return ap.parse_args()


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):  # Turkish letters on Windows consoles
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    args = parse_args()

    if args.list_models:
        list_models(build_providers(args.list_models)[0])
        return 0

    llm = LLM(build_providers(args.providers))
    page = args.page
    while not page or page < 1:
        raw = input("Enter page number: ").strip()
        page = int(raw) if raw.isdigit() else 0

    run_start = datetime.now()
    print(f"Fetching page {page} from the WordPress API ...")
    posts = fetch_posts(page)
    if not posts:
        print(f"Page {page} has no posts.")
        return 0
    if args.limit:
        posts = posts[: args.limit]
    print(f"Found {len(posts)} posts. Model: {llm.providers[0].name}:{llm.first_model}\n")

    cache = load_cache()
    rows: list[dict] = []
    interrupted = False
    try:
        for idx, post in enumerate(posts, 1):
            record = analyze_post(post, llm, cache, not args.no_cache)
            record.update({"Operation Date": run_start.strftime(DATETIME_FORMAT), "Page": page})
            rows.append(record)
            print(f"[{idx}/{len(posts)}] {record['Scholarship Name']}\n"
                  f"      -> {record['Status']} | deadline: {record['Deadline'] or '-'}"
                  f" | foreigners: {record['Foreigners Allowed'] or '-'}"
                  + (f" | {record['Notes']}" if record["Notes"] else ""))
    except KeyboardInterrupt:
        interrupted = True
        print("\n[!] Interrupted - saving partial results.")

    if not rows:
        return 0
    stem = f"scholarships_llm_page_{page}_{run_start:%d_%m_%Y}" + ("_partial" if interrupted else "")
    saved = write_csv(OUTPUT_FOLDER / f"{stem}.csv", rows)
    print_summary(rows, page, saved)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())