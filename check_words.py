"""check_words.py - show the lines that mention nationality in the FULL page text.

Run from the same folder as main.py:
    python check_words.py
"""
import re
import sys

import main

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

PATTERN = re.compile(r"vatandaş|yabancı|uyruk|uluslararası")

for post in main.fetch_posts(1)[:3]:
    text = main.html_to_text(post["content"]["rendered"])
    print("\n==", post["link"], "|", len(text), "chars")
    found = False
    for line in text.split("\n"):
        if PATTERN.search(main.turkish_lower(line)):
            print("  >", line[:200])
            found = True
    if not found:
        print("  (no nationality words found in this page)")