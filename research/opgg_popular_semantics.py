# Evidence for the meaning of `popular` (is it a pick rate?) and a look at the
# Tencent CDN augment dictionary that LeagueAkari uses for ARAM Mayhem.

import io
import json
import os
import ssl
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "opgg_api")

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")


def get(url, headers=None, timeout=30):
    h = {"User-Agent": UA, "Accept": "application/json, */*",
         "Origin": "https://op.gg", "Referer": "https://op.gg/"}
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, headers=h)
    ctx = ssl.create_default_context()
    with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
        return resp.status, resp.read().decode("utf-8", "replace")


print("=" * 84)
print("1) `popular` arithmetic - can it be a pick rate?")
print("=" * 84)
for cid, name in [(157, "Yasuo"), (103, "Ahri"), (32, "Amumu")]:
    url = "https://lol-api-champion.op.gg/api/contents/stats/champions/%d/aram-augments" % cid
    st, body = get(url)
    rows = json.loads(body)["data"]
    pops = [r.get("popular") or 0 for r in rows]
    nonzero = [p for p in pops if p > 0]
    print("  %-8s rows=%-4d sum(popular)=%.1f  max=%.2f  mean(nonzero)=%.2f  zeros=%d"
          % (name, len(rows), sum(pops), max(pops),
             sum(nonzero) / len(nonzero) if nonzero else 0,
             sum(1 for p in pops if p == 0)))

print("")
print("  interpretation test: if `popular` were '% of games this augment was CHOSEN',")
print("  then across 4 augment stages the sum should land near 4 * 100 = 400.")
print("  if it were '% of offer slots', the sum would be near 100.")

print("")
print("=" * 84)
print("2) champion average stats endpoint - the real win_rate source")
print("=" * 84)
st, body = get("https://lol-api-champion.op.gg/api/global/champions/aram/157/none")
o = json.loads(body)
print("  GET /api/global/champions/aram/157/none  -> %s" % st)
print("  meta: %s" % json.dumps(o["meta"], ensure_ascii=False))
print("  data.summary.average_stats: %s"
      % json.dumps(o["data"]["summary"]["average_stats"], ensure_ascii=False))

print("")
print("=" * 84)
print("3) Tencent CDN augment dictionary used by LeagueAkari (KIWI = ARAM Mayhem)")
print("=" * 84)
for url in [
    "https://game.gtimg.cn/images/lol/act/img/js/kiwi/kiwi_augments.json",
]:
    try:
        st, body = get(url, headers={"Origin": None} if False else None)
        print("  %s -> %s  bytes=%d" % (url, st, len(body)))
        with io.open(os.path.join(OUT, "kiwi_augments.json"), "w", encoding="utf-8") as fh:
            fh.write(body)
        try:
            o = json.loads(body)
            if isinstance(o, dict):
                print("     top-level keys: %s" % list(o.keys())[:10])
                for k in list(o.keys())[:4]:
                    v = o[k]
                    if isinstance(v, list) and v:
                        print("     %s: list[%d] first item keys=%s"
                              % (k, len(v), sorted(v[0].keys()) if isinstance(v[0], dict) else type(v[0]).__name__))
                        print("       first item: %s"
                              % json.dumps(v[0], ensure_ascii=False)[:400])
            elif isinstance(o, list):
                print("     list[%d] first item keys=%s"
                      % (len(o), sorted(o[0].keys()) if o and isinstance(o[0], dict) else ""))
                print("       first item: %s" % json.dumps(o[0], ensure_ascii=False)[:400])
        except Exception as exc:
            print("     json parse failed: %s ; head=%s" % (exc, body[:200]))
    except Exception as exc:
        print("  %s -> FAILED %s" % (url, exc))
