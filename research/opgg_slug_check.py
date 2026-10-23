# Which URL slug does OP.GG use for champions whose LCU alias is not a clean lowercase word?
# Candidates below are the LCU "alias" values (lowercased) plus likely overrides.

import re
import ssl
import time
import urllib.error
import urllib.request

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

CANDIDATES = {
    "AurelionSol": ["aurelionsol", "aurelion-sol", "aurelionsol", "asol"],
    "MonkeyKing": ["monkeyking", "wukong", "monkey-king"],
    "RekSai": ["reksai", "rek-sai"],
    "KSante": ["ksante", "k-sante"],
    "Belveth": ["belveth", "bel-veth"],
    "Chogath": ["chogath", "cho-gath"],
    "Leblanc": ["leblanc", "le-blanc"],
    "Velkoz": ["velkoz", "vel-koz"],
    "DrMundo": ["drmundo", "dr-mundo"],
    "JarvanIV": ["jarvaniv", "jarvan-iv"],
    "MissFortune": ["missfortune", "miss-fortune"],
    "TwistedFate": ["twistedfate", "twisted-fate"],
    "XinZhao": ["xinzhao", "xin-zhao"],
    "KogMaw": ["kogmaw", "kog-maw"],
    "Nunu": ["nunu", "nunu-willump"],
    "Renata": ["renata", "renataglasc"],
    "Wukong2": ["wukong"],
    "Hwei": ["hwei"],
    "Yunara": ["yunara"],
    "Ambessa": ["ambessa"],
    "Mel": ["mel"],
}


def probe(slug):
    url = "https://op.gg/lol/modes/aram-mayhem/%s/augments" % slug
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    ctx = ssl.create_default_context()
    try:
        with urllib.request.urlopen(req, timeout=25, context=ctx) as resp:
            body = resp.read().decode("utf-8", "replace")
        n = body.count('"rarity":')
        return resp.status, n, len(body)
    except urllib.error.HTTPError as exc:
        return exc.code, 0, 0
    except Exception as exc:
        return type(exc).__name__, 0, 0


for champion, slugs in CANDIDATES.items():
    results = []
    for slug in slugs:
        status, n, size = probe(slug)
        results.append("%s=%s(aug=%d)" % (slug, status, n))
        time.sleep(0.2)
    print("%-14s %s" % (champion, "  ".join(results)))
