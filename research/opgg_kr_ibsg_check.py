# Verifies the "KR + IBSG (Iron/Bronze/Silver/Gold)" data-scope claim in the new rules doc.
#
#   * does the champion endpoint accept region=kr in the path and tier=ibsg in the query?
#   * do those parameters actually change the numbers?
#   * does the ARAM-Mayhem augment endpoint accept region/tier at all?
#
# ASCII-only source; results are written to a UTF-8 file.

import io
import json
import os
import ssl
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "opgg_api")
BASE = "https://lol-api-champion.op.gg"

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
H = {"User-Agent": UA, "Accept": "application/json, text/plain, */*",
     "Origin": "https://op.gg", "Referer": "https://op.gg/"}


def get(path, timeout=40, tries=2):
    last = None
    for _ in range(tries):
        try:
            req = urllib.request.Request(BASE + path, headers=H)
            ctx = ssl.create_default_context()
            with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
                return resp.status, resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read().decode("utf-8", "replace")
        except Exception as exc:
            last = exc
            time.sleep(1.0)
    return type(last).__name__, str(last)


L = []


def p(s=""):
    L.append(s)


def avg(body):
    try:
        return json.loads(body)["data"]["summary"]["average_stats"]
    except Exception:
        return None


def meta(body):
    try:
        return json.loads(body).get("meta")
    except Exception:
        return None


# ---------------------------------------------------------------- 1
p("=" * 86)
p("1) champion endpoint: region in PATH, tier in QUERY  (champion 157 Yasuo, mode aram)")
p("=" * 86)
p("%-58s %-5s %-9s %-9s %-8s %s" % ("request", "code", "win_rate", "pick_rate", "play", "version"))
combos = [
    "/api/global/champions/aram/157/none",
    "/api/global/champions/aram/157/none?tier=all",
    "/api/global/champions/aram/157/none?tier=ibsg",
    "/api/kr/champions/aram/157/none",
    "/api/kr/champions/aram/157/none?tier=all",
    "/api/kr/champions/aram/157/none?tier=ibsg",
    "/api/kr/champions/aram/157/none?tier=gold_plus",
    "/api/kr/champions/aram/157/none?tier=emerald_plus",
    "/api/kr/champions/aram/157/none?tier=master_plus",
    "/api/na/champions/aram/157/none?tier=ibsg",
    "/api/kr/champions/aram/157/none?tier=bogus_tier",
]
for path in combos:
    st, body = get(path)
    a = avg(body) or {}
    m = meta(body) or {}
    p("%-58s %-5s %-9s %-9s %-8s %s"
      % (path.replace("/api/", ""), st, a.get("win_rate"), a.get("pick_rate"),
         a.get("play"), m.get("version")))
    time.sleep(0.15)

# ---------------------------------------------------------------- 2
p("")
p("=" * 86)
p("2) does TIER change the ARAM-Mayhem augment endpoint? (contents/stats/.../aram-augments)")
p("=" * 86)
p("%-64s %-5s %-7s %-38s %s" % ("request", "code", "rows", "first row", "meta"))
aug_paths = [
    "/api/contents/stats/champions/157/aram-augments",
    "/api/contents/stats/champions/157/aram-augments?tier=ibsg",
    "/api/contents/stats/champions/157/aram-augments?region=kr",
    "/api/contents/stats/champions/157/aram-augments?region=kr&tier=ibsg",
    "/api/kr/contents/stats/champions/157/aram-augments",
    "/api/kr/contents/stats/champions/157/aram-augments?tier=ibsg",
]
snapshots = {}
for path in aug_paths:
    st, body = get(path)
    rows = None
    first = ""
    m = ""
    if st == 200:
        try:
            o = json.loads(body)
            rows = o.get("data")
            first = json.dumps(rows[0], ensure_ascii=False) if rows else ""
            m = json.dumps(o.get("meta"), ensure_ascii=False)
            snapshots[path] = json.dumps(rows, sort_keys=True)
        except Exception as exc:
            first = "parse error %s" % exc
    else:
        first = body[:60]
    p("%-64s %-5s %-7s %-38s %s" % (path.replace("/api/", ""), st, rows and len(rows), first, m))
    time.sleep(0.15)

# ---------------------------------------------------------------- 3
p("")
p("=" * 86)
p("3) identity check: are all aram-augments responses byte-identical?")
p("=" * 86)
uniq = {}
for path, blob in snapshots.items():
    uniq.setdefault(blob, []).append(path)
p("  distinct payloads: %d" % len(uniq))
for blob, paths in uniq.items():
    p("    same payload for: %s" % [x.replace("/api/", "") for x in paths])
if len(uniq) == 1:
    p("  => region/tier are IGNORED by the ARAM-Mayhem augment endpoint (no scope filter).")

# ---------------------------------------------------------------- 4
p("")
p("=" * 86)
p("4) ARAM-Mayhem champion tiers endpoint - does it take region/tier?")
p("=" * 86)
for path in ["/api/contents/tiers?type=aram_mayhem",
             "/api/contents/tiers?type=aram_mayhem&region=kr",
             "/api/contents/tiers?type=aram_mayhem&tier=ibsg",
             "/api/contents/tiers?type=aram_mayhem&region=kr&tier=ibsg"]:
    st, body = get(path)
    n = ""
    m = ""
    if st == 200:
        try:
            o = json.loads(body)
            n = len(o.get("data") or [])
            m = json.dumps(o.get("meta"), ensure_ascii=False)
        except Exception:
            pass
    p("  %-62s %-5s rows=%-5s %s" % (path.replace("/api/", ""), st, n, m))
    time.sleep(0.15)

# ---------------------------------------------------------------- 5
p("")
p("=" * 86)
p("5) how large are the tier buckets? (sample counts decide if ibsg is usable)")
p("=" * 86)
for tier in ["all", "ibsg", "gold_plus", "platinum_plus", "emerald_plus", "diamond_plus"]:
    path = "/api/kr/champions/aram/157/none?tier=%s" % tier
    st, body = get(path)
    a = avg(body) or {}
    p("  kr tier=%-14s play=%-8s win_rate=%-10s pick_rate=%-10s kda=%s"
      % (tier, a.get("play"), a.get("win_rate"), a.get("pick_rate"), a.get("kda")))
    time.sleep(0.15)

with io.open(os.path.join(OUT, "_kr_ibsg_check.txt"), "w", encoding="utf-8") as fh:
    fh.write("\n".join(L))
print("written: opgg_api/_kr_ibsg_check.txt")
