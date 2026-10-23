# Verification of the newly found endpoint:
#   GET https://lol-api-champion.op.gg/api/global/champions/aram_mayhem/{championId}/augments
#
# Checks:
#   1. response shape / fields
#   2. is win_rate == win / play ?
#   3. is pick_rate the same number as `popular` from the aram-augments contents endpoint?
#   4. what are total_place / first_place in this mode?
#   5. coverage vs the contents endpoint
#   6. does it work for several champions?

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

HEADERS = {
    "User-Agent": UA,
    "Accept": "application/json, text/plain, */*",
    "Origin": "https://op.gg",
    "Referer": "https://op.gg/",
}


def get(path, timeout=30):
    req = urllib.request.Request(BASE + path, headers=HEADERS)
    ctx = ssl.create_default_context()
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")
    except Exception as exc:
        return type(exc).__name__, str(exc)


def save(name, body):
    with io.open(os.path.join(OUT, name), "w", encoding="utf-8") as fh:
        fh.write(body)


CHAMPIONS = [
    (157, "Yasuo"), (875, "Sett"), (99, "Lux"), (86, "Garen"),
    (115, "Ziggs"), (103, "Ahri"), (32, "Amumu"),
]

print("=" * 82)
print("1) shape of /api/global/champions/aram_mayhem/{id}/augments")
print("=" * 82)
status, body = get("/api/global/champions/aram_mayhem/157/augments")
save("mayhem_augments_157.json", body)
obj = json.loads(body)
print("status=%s meta=%s" % (status, json.dumps(obj.get("meta"), ensure_ascii=False)))
print("groups=%d" % len(obj["data"]))
total_augments = 0
for g in obj["data"]:
    augs = g.get("augments", [])
    total_augments += len(augs)
    print("  rarity=%-2s augments=%-3d fields=%s"
          % (g.get("rarity"), len(augs), sorted(augs[0].keys()) if augs else []))
print("total augments in response = %d" % total_augments)

print("")
print("first 3 rows of rarity=1 (raw JSON):")
print(json.dumps(obj["data"][0]["augments"][:3], ensure_ascii=False, indent=1))

print("")
print("=" * 82)
print("2) is win_rate == win / play ?")
print("=" * 82)
bad = 0
checked = 0
for g in obj["data"]:
    for a in g["augments"]:
        if a.get("play"):
            checked += 1
            expect = a["win"] / a["play"]
            if abs(expect - a["win_rate"]) > 1e-9:
                bad += 1
                if bad <= 3:
                    print("  mismatch id=%s win=%s play=%s win_rate=%s expected=%.10f"
                          % (a["id"], a["win"], a["play"], a["win_rate"], expect))
print("  checked=%d  mismatches=%d  -> %s"
      % (checked, bad, "win_rate IS win/play" if bad == 0 else "NOT simply win/play"))

print("")
print("=" * 82)
print("3) pick_rate here vs `popular` in /api/contents/stats/champions/157/aram-augments")
print("=" * 82)
st2, body2 = get("/api/contents/stats/champions/157/aram-augments")
contents = {row["id"]: row for row in json.loads(body2)["data"]}
print("contents endpoint rows = %d" % len(contents))
rows = []
for g in obj["data"]:
    for a in g["augments"]:
        rows.append(a)
print("mayhem/augments rows = %d" % len(rows))
same = 0
diff = []
missing = []
for a in rows:
    c = contents.get(a["id"])
    if not c:
        missing.append(a["id"])
        continue
    if abs((c.get("popular") or 0) - (a.get("pick_rate") or 0) * 100) < 0.02:
        same += 1
    else:
        diff.append((a["id"], a.get("pick_rate"), c.get("popular")))
print("  pick_rate*100 == popular : %d/%d" % (same, len(rows)))
print("  differences (id, pick_rate, popular): %s" % diff[:5])
print("  ids not present in contents endpoint: %s" % missing[:10])

print("")
print("=" * 82)
print("4) total_place / first_place in this mode")
print("=" * 82)
tp = sorted(set(a.get("total_place") for a in rows))
fp = sorted(set(a.get("first_place") for a in rows))
print("  distinct total_place: %s (first 10)" % tp[:10])
print("  distinct first_place: %s (first 10)" % fp[:10])
print("  sample: %s" % json.dumps(rows[0], ensure_ascii=False))

print("")
print("=" * 82)
print("5) coverage: how many of the 202 contents augments appear here?")
print("=" * 82)
print("  mayhem/augments covers %d / %d contents ids (%.1f%%)"
      % (len(set(a["id"] for a in rows)), len(contents),
         100.0 * len(set(a["id"] for a in rows)) / len(contents)))

print("")
print("=" * 82)
print("6) other champions")
print("=" * 82)
print("  %-10s %-6s %-6s %-7s %-8s %s" % ("champion", "status", "groups", "augs", "sample_wr", "meta.version"))
for cid, name in CHAMPIONS:
    st, bd = get("/api/global/champions/aram_mayhem/%d/augments" % cid)
    if st != 200:
        print("  %-10s %-6s %s" % (name, st, bd[:60]))
    else:
        o = json.loads(bd)
        n = sum(len(g.get("augments", [])) for g in o["data"])
        first = o["data"][0]["augments"][0] if o["data"] and o["data"][0]["augments"] else {}
        print("  %-10s %-6s %-6d %-7d %-8s %s"
              % (name, st, len(o["data"]), n, first.get("win_rate"),
                 (o.get("meta") or {}).get("version")))
        save("mayhem_augments_%d.json" % cid, bd)
    time.sleep(0.25)
