# Probes the real OP.GG internal API used by LeagueAkari:
#   https://lol-api-champion.op.gg
# Dumps raw JSON to research/opgg_api/ for evidence and prints a structural summary.

import io
import json
import os
import ssl
import sys
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "opgg_api")
BASE = "https://lol-api-champion.op.gg"

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

if not os.path.isdir(OUT):
    os.makedirs(OUT)


def fetch(path, extra_headers=None, timeout=30):
    url = BASE + path
    headers = {
        "User-Agent": UA,
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "Origin": "https://op.gg",
        "Referer": "https://op.gg/",
    }
    if extra_headers:
        headers.update(extra_headers)
    req = urllib.request.Request(url, headers=headers)
    ctx = ssl.create_default_context()
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
            body = resp.read().decode("utf-8", "replace")
            return resp.status, body, dict(resp.headers)
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace"), dict(exc.headers)
    except Exception as exc:
        return type(exc).__name__, str(exc), {}


def save(name, body):
    path = os.path.join(OUT, name)
    with io.open(path, "w", encoding="utf-8") as fh:
        fh.write(body)
    return path


def key_paths(obj, prefix="", out=None, depth=0):
    if out is None:
        out = []
    if depth > 6:
        return out
    if isinstance(obj, dict):
        for k, v in obj.items():
            p = "%s.%s" % (prefix, k) if prefix else k
            out.append((p, type(v).__name__))
            key_paths(v, p, out, depth + 1)
    elif isinstance(obj, list) and obj:
        key_paths(obj[0], prefix + "[]", out, depth + 1)
    return out


def grep_keys(obj, needles, prefix=""):
    """Return all key paths whose name contains any needle (case-insensitive)."""
    hits = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            p = "%s.%s" % (prefix, k) if prefix else k
            if any(n.lower() in k.lower() for n in needles):
                hits.append((p, v if not isinstance(v, (dict, list)) else type(v).__name__))
            hits.extend(grep_keys(v, needles, p))
    elif isinstance(obj, list) and obj:
        hits.extend(grep_keys(obj[0], needles, prefix + "[]"))
    return hits


def probe(label, path, filename):
    print("")
    print("=" * 78)
    print("%s" % label)
    print("GET %s" % (BASE + path))
    print("=" * 78)
    t0 = time.time()
    status, body, headers = fetch(path)
    dt = time.time() - t0
    print("status=%s  bytes=%d  %.2fs  content-type=%s"
          % (status, len(body), dt, headers.get("Content-Type")))
    if status != 200:
        print("body head: %s" % body[:400])
        return None
    save(filename, body)
    try:
        obj = json.loads(body)
    except Exception as exc:
        print("json parse failed: %s" % exc)
        print("body head: %s" % body[:400])
        return None

    if isinstance(obj, dict):
        print("top-level keys: %s" % list(obj.keys()))
        if "meta" in obj:
            print("meta: %s" % json.dumps(obj["meta"], ensure_ascii=False)[:300])
        if "data" in obj:
            d = obj["data"]
            print("data type: %s%s" % (type(d).__name__,
                                       " len=%d" % len(d) if isinstance(d, (list, dict)) else ""))
    hits = grep_keys(obj, ["win", "pick", "augment", "performance", "popular",
                           "games", "play", "rate", "stat"])
    seen = set()
    print("key paths matching win/pick/augment/... :")
    for p, v in hits:
        if p in seen:
            continue
        seen.add(p)
        print("   %-58s = %s" % (p, v))
    return obj


def main():
    # Task 1: champion average stats for ARAM Mayhem (Yasuo = 157)
    probe("TASK 1  champion average stats (aram_mayhem, Yasuo=157)",
          "/api/global/champions/aram_mayhem/157/none",
          "champion_aram_mayhem_157.json")

    # Task 2: augment list endpoint
    probe("TASK 2  aram-augments for champion 157",
          "/api/contents/stats/champions/157/aram-augments",
          "augments_157.json")

    # Comparison: classic ARAM (sanity check that the champion endpoint works generally)
    probe("SANITY  champion average stats (aram, Yasuo=157)",
          "/api/global/champions/aram/157/none",
          "champion_aram_157.json")

    # Task 3 lead: Arena champion build carries an augment_group with win/play/pick_rate
    probe("LEAD    champion build (arena, Yasuo=157) - augment_group?",
          "/api/global/champions/arena/157",
          "champion_arena_157.json")

    # versions
    probe("INFO    versions (aram_mayhem)",
          "/api/global/champions/aram_mayhem/versions",
          "versions_aram_mayhem.json")

    return 0


if __name__ == "__main__":
    sys.exit(main())
