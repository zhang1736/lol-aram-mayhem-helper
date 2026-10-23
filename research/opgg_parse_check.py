# Compare two extraction strategies for the OP.GG flight payload:
#   A) regex over augment objects
#   B) locate the JSON "data":[...] array and raw_decode it
# Strategy B is the robust one; this script proves it finds >= as many rows.

import html as html_mod
import io
import json
import os
import re

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "opgg")


def flight(html_text):
    parts = []
    for m in re.finditer(r'self\.__next_f\.push\(\[1,\s*(".*?")\s*\]\)', html_text, re.S):
        try:
            parts.append(json.loads(m.group(1)))
        except Exception:
            pass
    return "".join(parts)


def by_regex(payload):
    found = {}
    for m in re.finditer(r'\{"id":\d+,"tier":\d+,.*?"rarity":\d+.*?\}', payload):
        try:
            o = json.loads(m.group(0))
        except Exception:
            continue
        found[o["id"]] = o
    return found


def by_array(payload):
    found = {}
    for m in re.finditer(r'"data":\[', payload):
        start = payload.index("[", m.start())
        try:
            arr, _ = json.JSONDecoder().raw_decode(payload[start:])
        except Exception:
            continue
        if not isinstance(arr, list):
            continue
        for o in arr:
            if isinstance(o, dict) and "id" in o and "name" in o and "rarity" in o:
                found[o["id"]] = o
    return found


def main():
    for slug in ["yasuo", "garen", "ziggs"]:
        path = os.path.join(OUT, "augments_%s.html" % slug)
        with io.open(path, encoding="utf-8") as fh:
            raw = fh.read()
        payload = flight(raw)
        a = by_regex(payload)
        b = by_array(payload)
        ssr = [html_mod.unescape(x) for x in
               re.findall(r'<strong class="text-sm text-gray-900">(.*?)</strong>', raw)]
        print("%-7s regex=%-4d array=%-4d ssr_names=%-4d unique_ssr=%-4d"
              % (slug, len(a), len(b), len(ssr), len(set(ssr))))
        missing_in_array = sorted(set(a) - set(b))
        if missing_in_array:
            print("   ids only found by regex: %s" % missing_in_array[:10])
        only_ssr = sorted(set(ssr) - set(o["name"] for o in b.values()))
        print("   SSR names not present in array: %s" % only_ssr[:8])


if __name__ == "__main__":
    main()
