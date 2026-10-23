# Round 2: the first pass missed the route-specific chunk because its URL is written
# as a relative "static/chunks/app/.../page-*.js" inside the RSC flight payload.
# This pass extracts every .js path (absolute + relative) and greps for the API host,
# the augment endpoints and the field labels.

import io
import os
import re
import ssl
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "opgg_js")
if not os.path.isdir(OUT):
    os.makedirs(OUT)

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")


def fetch(url, timeout=40):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
    ctx = ssl.create_default_context()
    with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
        return resp.read().decode("utf-8", "replace")


page = io.open(os.path.join(OUT, "page.html"), encoding="utf-8").read()

# release base, e.g. https://c-lol-web.op.gg/app-router/releases/production-267-1-27f8c8dc
m = re.search(r"https://c-lol-web\.op\.gg/app-router/releases/[A-Za-z0-9.\-]+", page)
release = m.group(0) if m else None
print("release base: %s" % release)

paths = set(re.findall(r"([A-Za-z0-9._\-/]*static/chunks/[A-Za-z0-9._\-/%]+\.js)", page))
print("relative js paths found: %d" % len(paths))

urls = set()
for p in paths:
    p = p.lstrip("/")
    if p.startswith("_next/"):
        urls.add(release + "/" + p)
    else:
        urls.add(release + "/_next/" + p)
urls |= set(re.findall(r"https://c-lol-web\.op\.gg/[A-Za-z0-9._\-/]*_next/static/[A-Za-z0-9._\-/]+\.js", page))
urls = sorted(urls)
print("total js urls: %d" % len(urls))

PATTERNS = ["lol-api-champion", "aram-augments", "lol-api", "champions/aram", "/api/"]
found = {}

for i, u in enumerate(urls, 1):
    name = u.split("/")[-1]
    local = os.path.join(OUT, name)
    if os.path.exists(local):
        js = io.open(local, encoding="utf-8").read()
    else:
        try:
            js = fetch(u)
        except Exception as exc:
            print("  [%2d] FAIL %s (%s)" % (i, name, type(exc).__name__))
            continue
        with io.open(local, "w", encoding="utf-8") as fh:
            fh.write(js)
    hits = [p for p in PATTERNS if p in js]
    if hits:
        found[name] = (hits, len(js))
        print("  [%2d] %-46s %8d bytes  hits=%s" % (i, name, len(js), hits))
    time.sleep(0.03)

print("")
print("=" * 84)
print("context around API references")
print("=" * 84)
for name, (hits, size) in found.items():
    js = io.open(os.path.join(OUT, name), encoding="utf-8").read()
    print("### %s (%d bytes)" % (name, size))
    for kw in ["lol-api-champion", "aram-augments", "/api/"]:
        if kw not in js:
            continue
        seen = 0
        for mm in re.finditer(re.escape(kw), js):
            s = max(0, mm.start() - 220)
            e = min(len(js), mm.end() + 220)
            print("   [%s] ...%s..." % (kw, js[s:e].replace("\n", " ")))
            seen += 1
            if seen >= 4:
                break
    print("")

print("=" * 84)
print("field-name / label search across ALL downloaded chunks")
print("=" * 84)
NEEDLES = ["performance", "popular", "pickRate", "pick_rate", "winRate", "win_rate",
           "Pick Rate", "Win Rate", "augment"]
hits_by_needle = {}
for fn in sorted(os.listdir(OUT)):
    if not fn.endswith(".js"):
        continue
    js = io.open(os.path.join(OUT, fn), encoding="utf-8", errors="replace").read()
    for n in NEEDLES:
        if n in js:
            hits_by_needle.setdefault(n, []).append(fn)
for n in NEEDLES:
    print("  %-12s in %s" % (n, hits_by_needle.get(n, [])))
