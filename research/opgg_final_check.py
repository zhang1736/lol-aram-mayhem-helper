# Final confirmations:
#   * why the contents endpoint has more rows than the rendered page payload
#   * whether the `aram` / `arena` champion endpoints carry augment_group

import collections
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


def get(path, timeout=45, tries=3):
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
            time.sleep(1.5)
    return type(last).__name__, str(last)


lines = []

st, body = get("/api/contents/stats/champions/157/aram-augments")
rows = json.loads(body)["data"]
lines.append("contents endpoint: status=%s rows=%d" % (st, len(rows)))
lines.append("  row keys (union): %s" % sorted(set(k for r in rows for k in r.keys())))
lines.append("  rows with a 'rarity' key: %d  <- the API does NOT supply rarity"
             % sum(1 for r in rows if "rarity" in r))
lines.append("  tier distribution: %s" % dict(collections.Counter(r.get("tier") for r in rows)))
lines.append("  rows with tier is None: %d" % sum(1 for r in rows if r.get("tier") is None))
lines.append("  -> the rendered page payload had 180 rows WITH rarity/name/desc, i.e. the")
lines.append("     web server joins these 202 stat rows against a game-data dictionary.")
lines.append("  rows with tier is None     : %d" % sum(1 for r in rows if r.get("tier") is None))
lines.append("")

for path in ["/api/global/champions/aram/157/none",
             "/api/global/champions/arena/157",
             "/api/global/champions/aram_mayhem/157/augments"]:
    st, body = get(path)
    if st != 200:
        lines.append("%s -> status=%s %s" % (path, st, body[:100]))
        continue
    o = json.loads(body)
    d = o.get("data")
    if isinstance(d, dict):
        lines.append("%s -> 200  data keys = %s" % (path, sorted(d.keys())))
        ag = d.get("augment_group")
        lines.append("    augment_group present=%s len=%s"
                     % ("augment_group" in d, len(ag) if ag else 0))
        lines.append("    average_stats = %s"
                     % json.dumps((d.get("summary") or {}).get("average_stats"), ensure_ascii=False))
    else:
        lines.append("%s -> 200  data is %s len=%s" % (path, type(d).__name__, len(d or [])))
    lines.append("    meta = %s" % json.dumps(o.get("meta"), ensure_ascii=False))
    lines.append("")

with io.open(os.path.join(OUT, "_final_check.txt"), "w", encoding="utf-8") as fh:
    fh.write("\n".join(lines))
print("written")
