# Cross-check: is /api/global/champions/aram_mayhem/{id}/augments really ARAM Mayhem data,
# or is it the same payload as the Arena augment_group?

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
HEADERS = {"User-Agent": UA, "Accept": "application/json, text/plain, */*",
           "Origin": "https://op.gg", "Referer": "https://op.gg/"}


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


def flatten_groups(groups):
    out = {}
    for g in groups or []:
        for a in g.get("augments", []) or []:
            out[a["id"]] = a
    return out


CID = 157

print("=" * 82)
print("A) /api/global/champions/{mode}/{id}/augments   for several modes")
print("=" * 82)
mode_payloads = {}
for mode in ["aram_mayhem", "arena", "aram", "urf", "nexus_blitz"]:
    path = "/api/global/champions/%s/%d/augments" % (mode, CID)
    st, body = get(path)
    if st == 200:
        o = json.loads(body)
        flat = flatten_groups(o.get("data"))
        mode_payloads[mode] = flat
        print("  %-14s -> 200  groups=%-3s augments=%-4d meta=%s"
              % (mode, len(o.get("data") or []), len(flat),
                 json.dumps(o.get("meta"), ensure_ascii=False)))
        with io.open(os.path.join(OUT, "mode_%s_%d_augments.json" % (mode, CID)),
                     "w", encoding="utf-8") as fh:
            fh.write(body)
    else:
        print("  %-14s -> %s %s" % (mode, st, body[:70]))
    time.sleep(0.2)

print("")
print("=" * 82)
print("B) arena champion build augment_group (the reference Arena payload)")
print("=" * 82)
st, body = get("/api/global/champions/arena/%d" % CID)
arena_flat = {}
if st == 200:
    o = json.loads(body)
    arena_flat = flatten_groups(o["data"].get("augment_group"))
    print("  status=200  augment_group augments=%d  meta=%s"
          % (len(arena_flat), json.dumps(o.get("meta"), ensure_ascii=False)))
else:
    print("  failed %s" % st)

print("")
print("=" * 82)
print("C) identity comparison")
print("=" * 82)
mayhem = mode_payloads.get("aram_mayhem", {})


def compare(name_a, a, name_b, b):
    same_ids = set(a) == set(b)
    if not same_ids:
        print("  %s vs %s: DIFFERENT id sets (onlyA=%d onlyB=%d)"
              % (name_a, name_b, len(set(a) - set(b)), len(set(b) - set(a))))
        return
    diff = [i for i in a if json.dumps(a[i], sort_keys=True) != json.dumps(b[i], sort_keys=True)]
    print("  %s vs %s: same %d ids, %d rows differ byte-wise"
          % (name_a, name_b, len(a), len(diff)))
    for i in diff[:3]:
        print("     id=%s\n       A=%s\n       B=%s"
              % (i, json.dumps(a[i], ensure_ascii=False), json.dumps(b[i], ensure_ascii=False)))


compare("aram_mayhem/augments", mayhem, "arena augment_group", arena_flat)
for mode in ["arena", "aram", "urf", "nexus_blitz"]:
    if mode in mode_payloads:
        compare("aram_mayhem/augments", mayhem, "%s/augments" % mode, mode_payloads[mode])

print("")
print("=" * 82)
print("D) are the augment ids from mayhem/augments present in the aram-augments contents list?")
print("=" * 82)
st, body = get("/api/contents/stats/champions/%d/aram-augments" % CID)
contents_ids = set(r["id"] for r in json.loads(body)["data"])
mayhem_ids = set(mayhem)
arena_ids = set(arena_flat)
print("  contents ids      = %d" % len(contents_ids))
print("  mayhem/augments   = %d ; overlap with contents = %d"
      % (len(mayhem_ids), len(mayhem_ids & contents_ids)))
print("  arena augment ids = %d ; overlap with contents = %d"
      % (len(arena_ids), len(arena_ids & contents_ids)))

print("")
print("=" * 82)
print("E) how do augment NAME lookups behave? (id 22 should be an Arena augment)")
print("=" * 82)
for cid_aug in [22, 138, 206, 1077, 1336]:
    for fam in ["aram_mayhem"]:
        pass
print("  ids in mayhem/augments: %s" % sorted(mayhem_ids)[:12])
print("  ids in contents list  : %s" % sorted(contents_ids)[:12])
print("  mayhem/augments rarity groups sample ids: %s"
      % json.dumps([[a["id"] for a in g["augments"][:4]]
                    for g in json.loads(open(os.path.join(OUT, "mayhem_augments_157.json"),
                                             encoding="utf-8").read())["data"]]))
