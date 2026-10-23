# Enumerates OP.GG API surface around ARAM Mayhem augments:
#   A) which `mode` tokens the champion endpoint accepts
#   B) candidate endpoints that might carry per-augment win rate
#   C) query params that might add fields to aram-augments

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

if not os.path.isdir(OUT):
    os.makedirs(OUT)


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


def shape(body, limit=180):
    try:
        obj = json.loads(body)
    except Exception:
        return body[:limit]
    if isinstance(obj, dict):
        out = {}
        for k, v in list(obj.items())[:6]:
            if isinstance(v, list):
                out[k] = "list[%d] %s" % (len(v), (list(v[0].keys()) if v and isinstance(v[0], dict) else ""))
            elif isinstance(v, dict):
                out[k] = "dict %s" % list(v.keys())[:8]
            else:
                out[k] = v
        return json.dumps(out, ensure_ascii=False)[:limit]
    return json.dumps(obj, ensure_ascii=False)[:limit]


print("=" * 80)
print("A) champion endpoint: which mode tokens are valid?  (champion 157)")
print("=" * 80)
MODES = ["aram", "aram_mayhem", "mayhem", "kiwi", "arena", "ranked", "ranked_solo",
         "urf", "nexus_blitz", "one_for_all", "ultimate_spellbook", "classic",
         "normal", "aram_mayhem_ranked", "all"]
valid_modes = []
for mode in MODES:
    status, body = get("/api/global/champions/%s/157/none" % mode)
    note = ""
    if status == 200:
        try:
            obj = json.loads(body)
            avg = (obj.get("data", {}).get("summary", {}) or {}).get("average_stats") or {}
            note = "win_rate=%s pick_rate=%s play=%s version=%s" % (
                avg.get("win_rate"), avg.get("pick_rate"), avg.get("play"),
                (obj.get("meta") or {}).get("version"))
            valid_modes.append(mode)
        except Exception as exc:
            note = "parse error %s" % exc
    else:
        note = body[:80]
    print("  %-22s -> %-4s %s" % (mode, status, note))
    time.sleep(0.15)

print("")
print("=" * 80)
print("B) candidate endpoints for per-augment win rate")
print("=" * 80)
CANDIDATES = [
    "/api/contents/stats/champions/157/aram-augments",
    "/api/contents/stats/champions/157/aram-augments/stats",
    "/api/contents/stats/champions/157/aram-augments/win",
    "/api/contents/stats/champions/157/augments",
    "/api/contents/stats/champions/157/mayhem-augments",
    "/api/contents/stats/champions/aram_mayhem/157/augments",
    "/api/contents/stats/champions/157/aram-mayhem",
    "/api/contents/stats/augments/157",
    "/api/contents/stats/augments/1077",
    "/api/contents/stats/champions/157/augments/1077",
    "/api/contents/stats/champions/157/aram-augments/1077",
    "/api/contents/augments",
    "/api/contents/augments/157",
    "/api/contents/stats",
    "/api/contents/tiers?type=aram_mayhem",
    "/api/contents/stats/champions/157/aram-augments?tier=all",
    "/api/contents/stats/champions/157/aram-augments?version=16.18",
    "/api/global/champions/aram_mayhem/157",
    "/api/global/champions/aram_mayhem/157/augments",
    "/api/global/champions/aram_mayhem",
    "/api/global/champions/aram_mayhem/versions",
]
for path in CANDIDATES:
    status, body = get(path)
    print("  %-62s -> %-4s %s" % (path, status, shape(body)))
    time.sleep(0.15)

print("")
print("=" * 80)
print("C) does aram-augments accept extra query params / do fields change?")
print("=" * 80)
for q in ["", "?tier=all", "?version=16.18", "?tier=gold_plus",
          "?region=global", "?type=aram_mayhem", "?with_stats=true"]:
    path = "/api/contents/stats/champions/157/aram-augments" + q
    status, body = get(path)
    fields = ""
    n = 0
    if status == 200:
        try:
            arr = json.loads(body).get("data", [])
            n = len(arr)
            fields = ",".join(sorted(arr[0].keys())) if arr else "(empty)"
        except Exception:
            fields = "parse error"
    print("  %-62s -> %-4s n=%-4s fields=%s" % (path, status, n, fields))
    time.sleep(0.15)
