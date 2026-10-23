"""Deeper schema extraction for the OP.GG ARAM: Mayhem augment payload."""
from __future__ import print_function

import io
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "opgg")


def load_flight(html):
    parts = []
    for m in re.finditer(r'self\.__next_f\.push\(\[1,\s*(".*?")\s*\]\)', html, re.S):
        try:
            parts.append(json.loads(m.group(1)))
        except Exception:
            continue
    return "".join(parts)


def find_objects(text, needle):
    """Return every top-level {...} JSON object whose body contains `needle`."""
    objs = []
    for m in re.finditer(r'\{"id":\d+[^{}]*?%s[^{}]*?\}' % re.escape(needle), text):
        try:
            objs.append(json.loads(m.group(0)))
        except Exception:
            pass
    return objs


def main(champ="yasuo"):
    with io.open(os.path.join(OUT, "augments_%s.html" % champ), "r", encoding="utf-8") as fh:
        flight = load_flight(fh.read())

    objs = []
    seen = set()
    for m in re.finditer(r'\{"id":\d+,"tier":\d+,.*?"rarity":\d+.*?\}', flight):
        try:
            o = json.loads(m.group(0))
        except Exception:
            continue
        if o["id"] in seen:
            continue
        seen.add(o["id"])
        objs.append(o)

    print("unique augment objects: %d" % len(objs))
    keysets = {}
    for o in objs:
        keysets.setdefault(tuple(sorted(o.keys())), []).append(o)
    for ks, items in keysets.items():
        print("\nkeyset (%d objects): %s" % (len(items), list(ks)))

    print("\nrarity distribution: %s"
          % sorted((r, sum(1 for o in objs if o.get("rarity") == r))
                   for r in set(o.get("rarity") for o in objs)))
    print("tier distribution:   %s"
          % sorted((t, sum(1 for o in objs if o.get("tier") == t))
                   for t in set(o.get("tier") for o in objs)))

    perfs = [o["performance"] for o in objs if isinstance(o.get("performance"), (int, float))]
    pops = [o["popular"] for o in objs if isinstance(o.get("popular"), (int, float))]
    print("performance range: %.2f .. %.2f" % (min(perfs), max(perfs)))
    print("popular     range: %.2f .. %.2f" % (min(pops), max(pops)))

    print("\ntop 5 by performance:")
    for o in sorted(objs, key=lambda x: -x.get("performance", 0))[:5]:
        print("  %-32s perf=%-6s pop=%-6s rarity=%s tier=%s key=%s"
              % (o["name"], o["performance"], o["popular"], o["rarity"], o["tier"], o["key"]))

    print("\ntop 5 by popular:")
    for o in sorted(objs, key=lambda x: -x.get("popular", 0))[:5]:
        print("  %-32s perf=%-6s pop=%-6s rarity=%s tier=%s key=%s"
              % (o["name"], o["performance"], o["popular"], o["rarity"], o["tier"], o["key"]))

    # names we expect from the requirement doc (Soul Siphon / Draw Your Sword)
    print("\nkeyword lookups:")
    for kw in ["Soul Siphon", "Draw", "Infinity", "Vulnerability"]:
        hits = [o["name"] for o in objs if kw.lower() in o["name"].lower()]
        print("  %-14s -> %s" % (kw, hits))

    with io.open(os.path.join(OUT, "augments_%s.json" % champ), "w", encoding="utf-8") as fh:
        json.dump(objs, fh, ensure_ascii=False, indent=1)
    print("\nsaved: opgg/augments_%s.json" % champ)

    # any other structure holding stats?
    print("\nother stat-bearing key names in payload:")
    for k in sorted(set(re.findall(r'"([a-zA-Z_]{3,24})":(?:-?\d+\.?\d*)(?=[,}])', flight))):
        print("  %s" % k)


if __name__ == "__main__":
    sys.exit(main(*(sys.argv[1:2] or ["yasuo"])))
