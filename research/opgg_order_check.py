"""Compare the server-rendered augment ORDER with the numeric fields in the payload."""
from __future__ import print_function

import io
import json
import os
import re

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


def main(champ="yasuo"):
    with io.open(os.path.join(OUT, "augments_%s.html" % champ), "r", encoding="utf-8") as fh:
        html = fh.read()
    flight = load_flight(html)

    # 1) server-rendered names, in document order
    ssr = re.findall(r'<strong class="text-sm text-gray-900">(.*?)</strong>', html)
    print("SSR-rendered augment names: %d" % len(ssr))
    print("first 12: %s" % ssr[:12])

    # 2) payload objects
    objs = {}
    for m in re.finditer(r'\{"id":\d+,"tier":\d+,.*?"rarity":\d+.*?\}', flight):
        try:
            o = json.loads(m.group(0))
        except Exception:
            continue
        objs.setdefault(o["name"], o)
    print("payload augment objects: %d" % len(objs))

    by_pop = [o["name"] for o in sorted(objs.values(), key=lambda x: -x.get("popular", 0))]
    by_perf = [o["name"] for o in sorted(objs.values(), key=lambda x: -x.get("performance", 0))]

    print("\nSSR order == payload order sorted by 'popular'? %s"
          % (ssr[:len(by_pop)] == by_pop[:len(ssr)]))
    print("SSR order == payload order sorted by 'performance'? %s"
          % (ssr[:len(by_perf)] == by_perf[:len(ssr)]))

    print("\nidx  SSR-name                      popular  performance  rarity  tier")
    for i, n in enumerate(ssr[:15]):
        o = objs.get(n)
        if o:
            print("%3d  %-30s %7.2f  %10.2f  %6s  %4s"
                  % (i + 1, n, o["popular"], o["performance"], o["rarity"], o["tier"]))
        else:
            print("%3d  %-30s (not in payload)" % (i + 1, n))

    # duplicates in the SSR list?
    print("\nSSR list unique names: %d / %d" % (len(set(ssr)), len(ssr)))

    # tab buttons present in the page
    tabs = re.findall(r'>(\s*(?:All|Silver|Gold|Prismatic)\s*)<', html)
    print("tab-like labels found: %s" % sorted(set(t.strip() for t in tabs if t.strip())))


if __name__ == "__main__":
    main()
