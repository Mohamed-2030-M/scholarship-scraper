"""
test_heading_filter.py

Experiment: heading-based filtering BEFORE sending text to the LLM.
Fetches page 1, picks the post whose title contains "turk loydu",
splits its HTML at headings, drops SKIP sections, keeps FOCUS sections,
and always keeps any section containing a calendar date (deadline
insurance). Falls back to "intro + all non-skipped sections" when no
FOCUS section exists.

Run:  python test_heading_filter.py
"""
import re
import requests
from bs4 import BeautifulSoup, Tag

BASE = "https://www.guncel-egitim.org/wp-json/wp/v2"

SKIP_HEADING_KEYWORDS = [
    "tıklayın",             # "apply here" link buttons (covers tıklayınız too)
    "kaynak link",          # source link section
    "gerekli belgeler",     # required documents
    "devam koşulları",      # conditions to KEEP the scholarship, not to apply
    "nereden ve ne zaman",  # where/when to apply
    "detaylar için",        # "details" link
]

FOCUS_HEADING_KEYWORDS = [
    "başvuru şartları",     # application requirements
    "başvuru koşulları",    # application conditions
    "koşulları",            # any "...koşulları" section
    "kimler başvurabilir",  # who can apply
    "yapamayacak",          # those who CANNOT apply  <- key for foreign students
    "başvuramayacak",       # same meaning
    "öncelikli",            # priority criteria
]

EVIDENCE_WORDS = ["vatandaş", "yabancı", "uyruk", "t.c."]  # sanity check

# any section whose body contains a real calendar date is always kept
# (deadlines often live in "announcement" sections, not condition sections)
DATE_PATTERN = re.compile(
    r"\b\d{1,2}\s+(?:Ocak|Şubat|Mart|Nisan|Mayıs|Haziran|Temmuz|Ağustos|Eylül|Ekim|Kasım|Aralık)\s+\d{4}"
    r"|\b\d{1,2}[./]\d{1,2}[./]\d{4}\b"
    r"|\b\d{4}-\d{2}-\d{2}\b"
)


def fetch_category_id(slug):
    r = requests.get(f"{BASE}/categories", params={"slug": slug}, timeout=30)
    r.raise_for_status()
    cats = r.json()
    if not cats:
        raise SystemExit(f"No category found with slug '{slug}'")
    return cats[0]["id"]


def fetch_posts(page=1, per_page=12):
    cat_id = fetch_category_id("burs-basvurusu")
    r = requests.get(
        f"{BASE}/posts",
        params={"categories": cat_id, "per_page": per_page, "page": page},
        timeout=30,
    )
    r.raise_for_status()
    return r.json()


def is_heading(tag: Tag) -> bool:
    """h1-h4, or a paragraph that is entirely bold (same idea as list_headings.py)."""
    if tag.name in ("h1", "h2", "h3", "h4"):
        return True
    if tag.name != "p":
        return False
    text = tag.get_text(" ", strip=True)
    if not text or len(text) > 120:
        return False
    bolds = tag.find_all(["strong", "b"])
    if not bolds:
        return False
    bold_text = " ".join(b.get_text(" ", strip=True) for b in bolds)
    return bold_text == text  # whole paragraph is bold -> heading-like


def split_sections(html: str):
    """Return (intro_text, sections) where sections = list of (heading, body_text)."""
    soup = BeautifulSoup(html, "html.parser")
    intro_parts, sections = [], []
    current_heading, current_parts = None, []

    blocks = ["h1", "h2", "h3", "h4", "p", "ul", "ol", "blockquote", "table"]
    for el in soup.find_all(blocks):
        if is_heading(el):
            if current_heading is not None:
                sections.append((current_heading, "\n".join(current_parts)))
            elif current_parts:
                intro_parts.extend(current_parts)
            current_heading = el.get_text(" ", strip=True)
            current_parts = []
        else:
            current_parts.append(el.get_text(" ", strip=True))

    if current_heading is not None:
        sections.append((current_heading, "\n".join(current_parts)))
    elif current_parts:
        intro_parts.extend(current_parts)

    return "\n".join(intro_parts), sections


def classify(heading: str) -> str:
    h = heading.casefold()
    if any(k in h for k in SKIP_HEADING_KEYWORDS):
        return "skip"
    if any(k in h for k in FOCUS_HEADING_KEYWORDS):
        return "focus"
    return "normal"


def select_text(html: str):
    """Heading-based selection. Always keeps: intro + focus sections + any
    section containing a calendar date (deadline insurance)."""
    intro, sections = split_sections(html)
    if not sections:  # no headings found -> keep everything (safe fallback)
        return intro, [], "no headings found - kept everything"

    focus = [(h, b) for h, b in sections if classify(h) == "focus"]
    picked, skipped = [], []
    for h, b in sections:
        c = classify(h)
        if c == "focus" or DATE_PATTERN.search(b):
            picked.append((h, b))        # focus OR contains a date -> keep
        elif c == "skip":
            skipped.append(h)            # link sections etc.
        elif focus:
            pass                         # normal section dropped (focus exists)
        else:
            picked.append((h, b))        # no focus at all -> keep all non-skipped

    parts = [f"[intro]\n{intro}"]
    for h, b in picked:
        parts.append(f"[{h}]\n{b}")
    return "\n\n".join(parts), skipped, f"{len(focus)} focus section(s)"


def main():
    posts = fetch_posts(page=1)
    post = next(
        (p for p in posts if "turk loydu" in p["title"]["rendered"].casefold().replace("ü", "u")),
        None,
    )
    if post is None:
        raise SystemExit("Turk Loydu post not found on page 1 - check the title search word.")

    html = post["content"]["rendered"]
    full_text = BeautifulSoup(html, "html.parser").get_text("\n", strip=True)
    selected, skipped, info = select_text(html)

    pct = 100 * len(selected) // max(len(full_text), 1)
    print(f"Post: {post['title']['rendered']}")
    print(f"Full text: {len(full_text)} chars | Selected: {len(selected)} chars ({pct}%)")
    print(f"Filter info: {info}")
    print(f"Skipped headings: {skipped}")
    for w in EVIDENCE_WORDS:
        in_full = "YES" if w in full_text.casefold() else "no"
        in_sel = "YES" if w in selected.casefold() else "no"
        print(f"evidence '{w}': full_text={in_full}  selected={in_sel}")

    out = "output/heading_filter_test.txt"
    with open(out, "w", encoding="utf-8") as f:
        f.write(selected)
    print(f"\nSelected text saved to: {out}")
    print("--- first 40 lines of selected text ---")
    print("\n".join(selected.splitlines()[:40]))


if __name__ == "__main__":
    main()