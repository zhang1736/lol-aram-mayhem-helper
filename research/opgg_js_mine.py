# Mines the OP.GG web app JS bundles for:
#   * the API hosts/paths it calls (to see exactly which endpoints the page uses)
#   * how `performance` / `popular` are consumed (labels, units)
#
# Downloads the page, extracts every _next/static chunk URL, fetches them, and greps.

import io
import json
import os
import re
import ssl
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "opgg_js")
BASE_PAGE = "https://op.gg/lol/modes/aram-mayhem/yasuo/augments"

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

if not os.path.isdir(OUT):
    os.makedirs(OUT)


def fetch(url, timeout=40):
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.9",
    })
    ctx = ssl.create_default_context()
    with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
        return resp.read().decode("utf-8", "replace")


print("fetching page ...")
page = fetch(BASE_PAGE)
with io.open(os.path.join(OUT, "page.html"), "w", encoding="utf-8") as fh:
    fh.write(page)

# collect chunk urls
urls = set(re.findall(r"https://c-lol-web\.op\.gg/[A-Za-z0-9._\-/]*_next/static/[A-Za-z0-9._\-/]+\.js", page))
urls |= set("https://c-lol-web.op.gg" + u for u in
            re.findall(r'"(/_next/static/[A-Za-z0-9._\-/]+\.js)"', page))
urls = sorted(urls)
print("chunk urls found: %d" % len(urls))

PATTERNS = [
    "lol-api-champion",
    "aram-augments",
    "aram_mayhem",
    "augment",
    "/api/",
]

hosts = set()
paths = set()
interesting = []
total_bytes = 0

for i, u in enumerate(urls, 1):
    try:
        js = fetch(u)
    except Exception as exc:
        print("  [%2d] FAILED %s (%s)" % (i, os.path.basename(u), type(exc).__name__))
        continue
    total_bytes += len(js)
    name = os.path.basename(u)
    with io.open(os.path.join(OUT, name), "w", encoding="utf-8") as fh:
        fh.write(js)

    for h in re.findall(r"https?://[a-z0-9.\-]*op\.gg[A-Za-z0-9._\-/]*", js):
        hosts.add(h)
    for p in re.findall(r"[\"'`](/api/[A-Za-z0-9._\-/{}$:]{2,90})[\"'`]", js):
        paths.add(p)

    hits = [p for p in PATTERNS if p in js]
    if hits:
        interesting.append((name, hits, len(js)))
    print("  [%2d/%2d] %-42s %6d bytes  hits=%s" % (i, len(urls), name, len(js), hits))
    time.sleep(0.05)

print("")
print("total js bytes: %d" % total_bytes)
print("")
print("=" * 82)
print("op.gg URLs referenced in JS")
print("=" * 82)
for h in sorted(hosts):
    print("  %s" % h)

print("")
print("=" * 82)
print("/api/ paths referenced in JS")
print("=" * 82)
for p in sorted(paths):
    print("  %s" % p)

print("")
print("=" * 82)
print("chunks containing 'aram-augments' or 'lol-api-champion'")
print("=" * 82)
for name, hits, size in interesting:
    if "aram-augments" in hits or "lol-api-champion" in hits:
        js = io.open(os.path.join(OUT, name), encoding="utf-8").read()
        print("--- %s (%d bytes) ---" % (name, size))
        for kw in ["lol-api-champion", "aram-augments", "aram_mayhem"]:
            for m in re.finditer(re.escape(kw), js):
                s = max(0, m.start() - 260)
                e = min(len(js), m.end() + 260)
                print("  [%s] ...%s..." % (kw, js[s:e].replace("\n", " ")))
                break
        print("")
