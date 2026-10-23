# Does any OTHER ARAM Mayhem page payload carry numeric augment win rates?
# (the /build tab is rendered by the same table component that has pick_rate/win_rate
#  columns, so it is the last plausible place for augment win rate to surface)

import io
import json
import os
import re
import ssl
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "opgg_api")

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")


def fetch(url, timeout=40):
    req = urllib.request.Request(url, headers={"User-Agent": UA,
                                               "Accept": "text/html,*/*;q=0.8"})
    ctx = ssl.create_default_context()
    with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
        return resp.status, resp.read().decode("utf-8", "replace")


def flight(html_text):
    parts = []
    for m in re.finditer(r'self\.__next_f\.push\(\[1,\s*(".*?")\s*\]\)', html_text, re.S):
        try:
            parts.append(json.loads(m.group(1)))
        except Exception:
            pass
    return "".join(parts)


PAGES = [
    ("augments", "https://op.gg/lol/modes/aram-mayhem/yasuo/augments"),
    ("build", "https://op.gg/lol/modes/aram-mayhem/yasuo/build"),
    ("items", "https://op.gg/lol/modes/aram-mayhem/yasuo/items"),
]

for tag, url in PAGES:
    print("=" * 84)
    print("%s  %s" % (tag, url))
    print("=" * 84)
    try:
        status, html = fetch(url)
    except Exception as exc:
        print("  FAILED: %s" % exc)
        continue
    with io.open(os.path.join(OUT, "page_%s.html" % tag), "w", encoding="utf-8") as fh:
        fh.write(html)
    p = flight(html)
    print("  status=%s html=%d flight=%d" % (status, len(html), len(p)))

    for kw in ["augment_group", "augmentStats", "aram-augments", "win_rate", "pick_rate"]:
        print("  %-16s occurrences in flight = %d" % (kw, p.count(kw)))

    # numeric win_rate values (<1 fractions) or percentages
    nums = re.findall(r'"win_rate":\s*([0-9.]+)', p)
    print("  numeric \"win_rate\" values in flight: %d  sample=%s" % (len(nums), nums[:8]))
    nums2 = re.findall(r'"pick_rate":\s*([0-9.]+)', p)
    print("  numeric \"pick_rate\" values in flight: %d  sample=%s" % (len(nums2), nums2[:8]))

    idx = p.find("augment_group")
    if idx >= 0:
        print("  augment_group context: ...%s..." % p[max(0, idx - 200):idx + 500])

    # what augment-ish arrays exist?
    for m in re.finditer(r'"data":\[|"augments":\[|"augment_group":\[', p):
        print("     array marker at %d: %s" % (m.start(), p[m.start():m.start() + 90]))
    print("")
