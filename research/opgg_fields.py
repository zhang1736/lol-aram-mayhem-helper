# Characterise the numeric fields OP.GG exposes per augment (popular / performance / tier).

import io
import json
import os
import re

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "opgg")


def flight(text):
    parts = []
    for m in re.finditer(r'self\.__next_f\.push\(\[1,\s*(".*?")\s*\]\)', text, re.S):
        try:
            parts.append(json.loads(m.group(1)))
        except Exception:
            pass
    return "".join(parts)


def rows(payload):
    out = []
    for m in re.finditer(r'"data":\[', payload):
        s = payload.index("[", m.start())
        try:
            arr, _ = json.JSONDecoder().raw_decode(payload[s:])
        except Exception:
            continue
        for o in arr:
            if isinstance(o, dict) and "name" in o and "rarity" in o:
                out.append(o)
    return out


lines = []
for slug in ["yasuo", "ahri", "garen"]:
    path = os.path.join(OUT, "augments_%s.html" % slug)
    if not os.path.exists(path):
        continue
    with io.open(path, encoding="utf-8") as fh:
        data = rows(flight(fh.read()))

    lines.append("=== %s : %d augments ===" % (slug, len(data)))
    by_rarity = {}
    for o in data:
        by_rarity.setdefault(o.get("rarity"), []).append(o)
    for r in sorted(by_rarity, key=lambda x: (x is None, x)):
        group = by_rarity[r]
        pops = [o.get("popular") or 0 for o in group]
        perfs = [o.get("performance") or 0 for o in group]
        lines.append("  rarity %-4s n=%-4d avg popular=%.2f  avg performance=%.2f"
                     % (r, len(group), sum(pops) / len(pops), sum(perfs) / len(perfs)))

    lines.append("  tier buckets:")
    for t in sorted(set(o.get("tier") for o in data), key=lambda x: (x is None, x)):
        group = [o for o in data if o.get("tier") == t]
        pops = [o.get("popular") or 0 for o in group]
        perfs = [o.get("performance") or 0 for o in group]
        zero = sum(1 for p in pops if p == 0)
        lines.append("    tier %-3s n=%-4d avg_pop=%.2f avg_perf=%.2f  zero_pop=%d"
                     % (t, len(group), sum(pops) / len(pops), sum(perfs) / len(perfs), zero))

    # is `tier` simply a bucket of popularity? show max/min popular per tier
    lines.append("  popular range per tier: %s" % [
        (t, round(min(o.get("popular") or 0 for o in data if o.get("tier") == t), 2),
            round(max(o.get("popular") or 0 for o in data if o.get("tier") == t), 2))
        for t in sorted(set(o.get("tier") for o in data), key=lambda x: (x is None, x))
    ])

    # the sentinel-looking rows
    lines.append("  rows with popular == 0: %d / %d ; their performance values: %s"
                 % (sum(1 for o in data if (o.get("popular") or 0) == 0), len(data),
                    sorted(set(o.get("performance") for o in data if (o.get("popular") or 0) == 0))[:10]))
    lines.append("")

with io.open(os.path.join(OUT, "_fields.txt"), "w", encoding="utf-8") as fh:
    fh.write("\n".join(lines))
print("written: opgg/_fields.txt")
