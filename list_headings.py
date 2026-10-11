"""list_headings.py - which section headings appear across the posts?

Run from the same folder as main.py:
    python list_headings.py          # page 1
    python list_headings.py 1 2 3    # several pages

Prints the most common headings and saves everything to output/headings.csv
(open it in Excel). Use it to decide which headings to SKIP and which to FOCUS on.
"""
import csv
import sys
from collections import Counter

from bs4 import BeautifulSoup

import main

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def norm(text: str) -> str:
    text = " ".join(text.split())
    return main.turkish_lower(text).strip(" :.-–—")


def headings_of(html: str) -> list[tuple[str, str]]:
    """(kind, heading) for real <h1-h4> tags and for bold-only short paragraphs."""
    soup = BeautifulSoup(html or "", "html.parser")
    found = []
    for tag in soup.find_all(["h1", "h2", "h3", "h4"]):
        text = norm(tag.get_text(" ", strip=True))
        if text:
            found.append((tag.name, text))
    for p in soup.find_all("p"):
        bold = p.find(["strong", "b"])
        text = p.get_text(" ", strip=True)
        if bold and bold.get_text(" ", strip=True) == text and 3 <= len(text) <= 80:
            found.append(("bold", norm(text)))
    return found


pages = [int(a) for a in sys.argv[1:] if a.isdigit()] or [1]
full, tail2, links = Counter(), Counter(), {}
total = 0
for page in pages:
    for post in main.fetch_posts(page):
        total += 1
        seen = set(headings_of(post["content"]["rendered"]))  # count once per post
        for kind, text in seen:
            full[(kind, text)] += 1
            links.setdefault((kind, text), post["link"])
            tail2[" ".join(text.split()[-2:])] += 1

print(f"Scanned {total} posts from pages {pages}\n")
print("== Most common ENDINGS (last two words) - names of scholarships are ignored ==")
for tail, n in tail2.most_common(25):
    print(f"  {n:3d}  {tail}")

print("\n== Most common full headings ==")
for (kind, text), n in full.most_common(25):
    print(f"  {n:3d}  [{kind}] {text[:90]}")

main.OUTPUT_FOLDER.mkdir(parents=True, exist_ok=True)
out = main.OUTPUT_FOLDER / "headings.csv"
try:
    with open(out, "w", newline="", encoding="utf-8-sig") as fh:
        writer = csv.writer(fh)
        writer.writerow(["Kind", "Heading", "Posts", "Example link"])
        for (kind, text), n in full.most_common():
            writer.writerow([kind, text, n, links[(kind, text)]])
    print(f"\nAll headings saved to: {out}")
except PermissionError:
    print(f"\n[!] Could not write {out} (open in Excel?). Close it and run again.")