# Checks:
#  1. does the zh-CN locale page also embed the augment data (Chinese names)?
#  2. what do the HTTP response headers look like (CDN / anti-bot posture)?
#  3. how does OP.GG behave under a short burst of requests (rate limiting)?

import io
import json
import os
import re
import ssl
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "opgg")

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")


def get(url, headers=None, timeout=30):
    h = {"Accept": "text/html,*/*;q=0.8", "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8"}
    if headers != "no-ua":
        h["User-Agent"] = UA
    if headers and headers != "no-ua":
        h.update(headers)
    req = urllib.request.Request(url, headers=h)
    ctx = ssl.create_default_context()
    with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
        return resp.status, dict(resp.headers), resp.read().decode("utf-8", "replace")


def flight(html_text):
    parts = []
    for m in re.finditer(r'self\.__next_f\.push\(\[1,\s*(".*?")\s*\]\)', html_text, re.S):
        try:
            parts.append(json.loads(m.group(1)))
        except Exception:
            pass
    return "".join(parts)


def extract(payload):
    rows = []
    for m in re.finditer(r'"data":\[', payload):
        start = payload.index("[", m.start())
        try:
            arr, _ = json.JSONDecoder().raw_decode(payload[start:])
        except Exception:
            continue
        for o in (arr if isinstance(arr, list) else []):
            if isinstance(o, dict) and "name" in o and "rarity" in o:
                rows.append(o)
    return rows


print("=" * 78)
print("1) locale check: zh-CN page")
print("=" * 78)
for locale, url in [
    ("en", "https://op.gg/lol/modes/aram-mayhem/yasuo/augments"),
    ("zh-CN", "https://op.gg/zh-cn/lol/modes/aram-mayhem/yasuo/augments"),
]:
    t0 = time.time()
    try:
        status, headers, body = get(url)
        rows = extract(flight(body))
        names = [r.get("name") for r in rows][:8]
        print("[%s] status=%s bytes=%d augments=%d (%.2fs)"
              % (locale, status, len(body), len(rows), time.time() - t0))
        print("     first names: %s" % names)
        out = os.path.join(OUT, "augments_yasuo_%s.html" % locale.replace("-", "_"))
        with io.open(out, "w", encoding="utf-8") as fh:
            fh.write(body)
    except Exception as exc:
        print("[%s] FAILED: %s: %s" % (locale, type(exc).__name__, exc))

print("")
print("=" * 78)
print("2) response headers (anti-bot / CDN posture)")
print("=" * 78)
try:
    status, headers, body = get("https://op.gg/lol/modes/aram-mayhem/yasuo/augments")
    for k in ["Server", "Content-Type", "Cache-Control", "CF-RAY", "cf-cache-status",
              "Set-Cookie", "X-Powered-By", "Vary", "Age", "Date",
              "X-RateLimit-Limit", "X-RateLimit-Remaining"]:
        if k in headers:
            print("  %-22s %s" % (k, headers[k]))
    print("  (all header names: %s)" % sorted(headers.keys()))
except Exception as exc:
    print("  failed: %s" % exc)

print("")
print("=" * 78)
print("3) no-User-Agent request")
print("=" * 78)
try:
    status, headers, body = get("https://op.gg/lol/modes/aram-mayhem/yasuo/augments",
                                headers="no-ua")
    rows = extract(flight(body))
    print("  status=%s bytes=%d augments=%d" % (status, len(body), len(rows)))
except urllib.error.HTTPError as exc:
    print("  HTTP %s" % exc.code)
except Exception as exc:
    print("  FAILED: %s: %s" % (type(exc).__name__, exc))

print("")
print("=" * 78)
print("4) burst: 12 sequential requests, no delay")
print("=" * 78)
slugs = ["ahri", "akali", "alistar", "amumu", "annie", "ashe",
         "bard", "blitzcrank", "brand", "caitlyn", "cassiopeia", "darius"]
ok = 0
for i, slug in enumerate(slugs, 1):
    t0 = time.time()
    try:
        status, headers, body = get("https://op.gg/lol/modes/aram-mayhem/%s/augments" % slug)
        rows = extract(flight(body))
        ok += 1
        print("  %2d %-12s status=%s augments=%-4d %.2fs"
              % (i, slug, status, len(rows), time.time() - t0))
    except urllib.error.HTTPError as exc:
        print("  %2d %-12s HTTP %s" % (i, slug, exc.code))
    except Exception as exc:
        print("  %2d %-12s FAILED %s" % (i, slug, type(exc).__name__))
print("  success %d/%d" % (ok, len(slugs)))
