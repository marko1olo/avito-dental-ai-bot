# -*- coding: utf-8 -*-
"""Coordinate-based extraction of the ДентаКлиника price list.

pdftotext -layout reflows multi-line service names and drags the price column out
of alignment, so prices end up attributed to the wrong service. This binds each
price to the row whose vertical band actually contains it.
"""
import re
import sys

import pdfplumber

PDF = r"C:\temp\dentalia2-price-2026.1.pdf"

# Row label in the leftmost column: 1, 1.1, 2.1.3.1, 5.2. etc.
ROW_NO = re.compile(r"^\d+(?:\.\d+)*\.?$")
# A price cell: 800, 500-1000, от 6000, 25000-27000, 0т 50000 (typo in source).
PRICE = re.compile(r"^(?:от|0т|От)?\s*\d[\d\s]*(?:-\d[\d\s]*)?$")

Y_TOL = 3.0  # points; words within this vertical distance are one visual line


def lines_of(page):
    """Group words into visual lines keyed by rounded vertical position."""
    words = page.extract_words(use_text_flow=False, keep_blank_chars=False)
    buckets = {}
    for w in words:
        key = round(w["top"] / Y_TOL)
        buckets.setdefault(key, []).append(w)
    out = []
    for key in sorted(buckets):
        row = sorted(buckets[key], key=lambda w: w["x0"])
        out.append({"top": min(w["top"] for w in row),
                    "bottom": max(w["bottom"] for w in row),
                    "words": row})
    return out


def column_split(all_words):
    """Find the x where the price column starts: the rightmost dense numeric band."""
    xs = [w["x0"] for w in all_words if PRICE.match(w["text"].strip())]
    if not xs:
        return None
    xs.sort()
    # The price column is the tight cluster of the largest x values.
    tail = xs[int(len(xs) * 0.5):]
    return min(tail) - 5


def main():
    with pdfplumber.open(PDF) as pdf:
        for pno, page in enumerate(pdf.pages, 1):
            rows = lines_of(page)
            allw = [w for r in rows for w in r["words"]]
            xsplit = column_split(allw)
            print(f"\n{'='*100}\nPAGE {pno}   price column starts at x >= {xsplit:.1f}"
                  f"   page width {page.width:.0f}\n{'='*100}")

            # Pass 1: identify service rows (they open with a № label on the left).
            entries = []
            for r in rows:
                left = r["words"][0]
                label = left["text"].strip()
                if left["x0"] < xsplit * 0.25 and ROW_NO.match(label):
                    name = " ".join(w["text"] for w in r["words"][1:]
                                    if w["x0"] < xsplit)
                    entries.append({"no": label, "top": r["top"], "bottom": r["bottom"],
                                    "name": name, "prices": []})
                elif entries:
                    # continuation line of the previous service name
                    cont = " ".join(w["text"] for w in r["words"] if w["x0"] < xsplit)
                    if cont:
                        entries[-1]["name"] += " " + cont
                    entries[-1]["bottom"] = max(entries[-1]["bottom"], r["bottom"])

            # Pass 2: bind every price token to the entry whose band contains it.
            orphans = []
            for r in rows:
                for w in r["words"]:
                    if w["x0"] < xsplit:
                        continue
                    txt = w["text"].strip()
                    if not PRICE.match(txt):
                        continue
                    ymid = (w["top"] + w["bottom"]) / 2
                    hit = next((e for e in entries
                                if e["top"] - Y_TOL <= ymid <= e["bottom"] + Y_TOL), None)
                    if hit:
                        hit["prices"].append(txt)
                    else:
                        orphans.append((round(ymid, 1), txt))

            for e in entries:
                name = re.sub(r"\s+", " ", e["name"]).strip()
                price = " / ".join(e["prices"]) if e["prices"] else "—"
                print(f"{e['no']:<9} {name[:78]:<78} {price}")
            if orphans:
                print(f"\n  UNBOUND PRICES (no row owns them): {orphans}")


if __name__ == "__main__":
    sys.exit(main())
