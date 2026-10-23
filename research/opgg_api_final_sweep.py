# Final sweep for "is there an ARAM Mayhem augment win-rate API?"
#
# 1. Does any ARAM Mayhem payload contain numeric win_rate/pick_rate/games?
# 2. Remaining plausible API paths.
# 3. Which ids does each dataset use (Arena id space vs ARAM Mayhem id space)?
# 4. Does /api/contents/tiers?type=... have other useful types?

import io
import json
import os
import re
import ssl
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "opgg_api")
BASE = "https://lol-api-champion.op.gg"

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
HEADERS = {"User-Agent": UA, "Accept": "application/json, text/plain, */*",
           "Origin": "https://op.gg", "Referer": "https://op.gg/"}


def get(path, timeout=25):
    req = urllib.request.Request(BASE + path, headers=HEADERS)
    ctx = ssl.create_default_context()
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")
    except Exception as exc:
        return type(exc).__name__, str(exc)


print("=" * 84)
print("1) numeric stat fields per dataset (champion 157 Yasuo)")
print("=" * 84)


def fields_of(obj, label):
    def walk(o, path=""):
        if isinstance(o, dict):
            for k, v in o.items():
                yield path + "." + k, v
                for x in walk(v, path + "." + k):
                    yield x
        elif isinstance(o, list) and o:
            for x in walk(o[0], path + "[]"):
                yield x
    keys = {}
    for p, v in walk(obj):
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            keys.setdefault(p, v)
    print("  --- %s ---" % label)
    for k in sorted(keys):
        print("      %-52s = %s" % (k, keys[k]))
    return keys


st, body = get("/api/contents/stats/champions/157/aram-augments")
if st == 200:
    fields_of(json.loads(body), "ARAM Mayhem aram-augments (contents endpoint)")

st, body = get("/api/global/champions/aram_mayhem/157/augments")
mayhem_aug = json.loads(body)
fields_of(mayhem_aug, "champion aram_mayhem/{id}/augments")

print("")
print("=" * 84)
print("2) remaining candidate API paths")
print("=" * 84)
PATHS = [
    "/api/contents/stats/champions/157/aram-augments/1077",
    "/api/contents/stats/champions/157/aram-augments/stats/1077",
    "/api/contents/stats/augment/1077",
    "/api/contents/stats/augments/1077/stats",
    "/api/contents/aram-augments",
    "/api/contents/aram-augments/1077",
    "/api/contents/stats/champions/157/augment-stats",
    "/api/global/champions/aram_mayhem/157/augments/1077",
    "/api/global/champions/aram_mayhem/157/augments?rarity=4",
    "/api/global/champions/aram_mayhem/157/augments?tier=all",
    "/api/global/champions/aram_mayhem/157/augments?version=16.18",
    "/api/contents/stats/champions/157/aram-augments/performance",
    "/api/contents/stats/champions/157/aram-augments/win-rate",
    "/api/contents/augments/aram_mayhem",
    "/api/contents/stats/augments?type=aram_mayhem",
    "/api/contents/stats/augments?champion_id=157&type=aram_mayhem",
    "/api/contents/tiers?type=aram_mayhem_classic",
    "/api/contents/tiers?type=arena",
    "/api/contents/tiers?type=aram",
    "/api/contents/aram-mayhem-tiers",
]
for p in PATHS:
    st, body = get(p)
    note = ""
    if st == 200:
        try:
            o = json.loads(body)
            if isinstance(o, dict) and isinstance(o.get("data"), list):
                first = o["data"][0] if o["data"] else {}
                note = "list[%d] fields=%s" % (len(o["data"]), sorted(first.keys()) if isinstance(first, dict) else "")
            else:
                note = json.dumps(o, ensure_ascii=False)[:120]
        except Exception:
            note = body[:120]
    else:
        note = body[:90]
    print("  %-70s %-4s %s" % (p, st, note))
    time.sleep(0.12)

print("")
print("=" * 84)
print("3) id spaces")
print("=" * 84)
st, body = get("/api/contents/stats/champions/157/aram-augments")
aram_ids = sorted(r["id"] for r in json.loads(body)["data"])
mayhem_ids = sorted(a["id"] for g in mayhem_aug["data"] for a in g["augments"])
print("  ARAM Mayhem augment ids (contents endpoint): n=%d min=%d max=%d"
      % (len(aram_ids), aram_ids[0], aram_ids[-1]))
print("     sample: %s" % aram_ids[:14])
print("  champion /augments ids                     : n=%d min=%d max=%d"
      % (len(mayhem_ids), mayhem_ids[0], mayhem_ids[-1]))
print("     sample: %s" % mayhem_ids[:14])
print("  overlap: %s" % sorted(set(aram_ids) & set(mayhem_ids)))
