"""Decode the Next.js App Router (RSC flight) payload of an OP.GG page.

ASCII-only source. Writes UTF-8 output files next to the HTML.
"""
from __future__ import print_function

import io
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "opgg")


def load_flight(html):
    """Concatenate and unescape every self.__next_f.push([1,"..."]) chunk."""
    parts = []
    for m in re.finditer(r'self\.__next_f\.push\(\[1,\s*(".*?")\s*\]\)', html, re.S):
        raw = m.group(1)
        try:
            parts.append(json.loads(raw))
        except Exception:
            continue
    return "".join(parts)


def walk(obj, path=""):
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield path + "/" + str(k), v
            for item in walk(v, path + "/" + str(k)):
                yield item
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            for item in walk(v, path + "[%d]" % i):
                yield item


def main(champ="yasuo"):
    html_path = os.path.join(OUT, "augments_%s.html" % champ)
    with io.open(html_path, "r", encoding="utf-8") as fh:
        html = fh.read()

    flight = load_flight(html)
    out_path = os.path.join(OUT, "flight_%s.txt" % champ)
    with io.open(out_path, "w", encoding="utf-8") as fh:
        fh.write(flight)

    print("[flight] decoded %d chars from %s" % (len(flight), os.path.basename(html_path)))
    print("[flight] saved -> %s" % os.path.basename(out_path))

    # candidate keys that would carry statistics
    keys = {}
    for k, v in walk(None):
        pass  # placeholder, replaced below

    # find JSON objects that look like augment entries
    aug_objs = []
    for m in re.finditer(r'\{"id":\d+,"tier":\d+.*?"rarity":\d+.*?\}', flight):
        try:
            aug_objs.append(json.loads(m.group(0)))
        except Exception:
            pass
    print("[augment objects] count=%d" % len(aug_objs))
    if aug_objs:
        print("[augment keys] %s" % sorted(aug_objs[0].keys()))
        print("[augment sample] %s" % json.dumps(aug_objs[0], ensure_ascii=False)[:600])

    # search for stat-ish tokens anywhere in the flight payload
    for token in ["winRate", "pickRate", "win_rate", "pick_rate", "\\\"win\\\"",
                  "\\\"pick\\\"", "performance", "popular", "tier", "rarity",
                  "augment_stats", "augmentStats", "AUGMENT"]:
        print("[token] %-16s occurrences=%d" % (token, flight.count(token)))

    # dump a window around the first augment-stat-ish structure
    idx = flight.find("performance")
    if idx >= 0:
        print("\n[context around first 'performance']")
        print(flight[max(0, idx - 700):idx + 900])


if __name__ == "__main__":
    sys.exit(main(*(sys.argv[1:2] or ["yasuo"])))
