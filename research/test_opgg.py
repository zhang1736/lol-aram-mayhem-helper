# OP.GG ARAM: Mayhem augment provider + Day 1 acceptance test.
#
# Verified facts baked in (see research/opgg/*.html for the raw evidence):
#   * https://op.gg/lol/modes/aram-mayhem/{champion}/augments is server-side rendered
#     (Next.js App Router). The full augment list for the champion is embedded in the
#     RSC flight payload inside <script>self.__next_f.push([1,"..."])</script>.
#   * No browser, cookie, JS execution or private API is required.
#   * Each augment object exposes:
#       id, key, name, rarity (1=Silver 4=Gold 8=Prismatic), tier, desc, tooltip,
#       popular   -> OP.GG pick rate, in percent
#       performance -> OP.GG internal performance score (NOT a win rate)

import argparse
import json
import re
import ssl
import sys
import time
import urllib.error
import urllib.request

BASE = "https://op.gg/lol/modes/aram-mayhem/%s/augments"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

RARITY_NAMES = {1: "Silver", 4: "Gold", 8: "Prismatic"}


class OpggAugmentProvider:
    """Fetches and parses OP.GG ARAM: Mayhem augment statistics."""

    def __init__(self):
        self._cache = {}

    def _fetch(self, champion_slug, timeout=30):
        url = BASE % champion_slug
        req = urllib.request.Request(url, headers={
            "User-Agent": UA,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        })
        ctx = ssl.create_default_context()
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")

    @staticmethod
    def _flight(html):
        parts = []
        for m in re.finditer(r'self\.__next_f\.push\(\[1,\s*(".*?")\s*\]\)', html, re.S):
            try:
                parts.append(json.loads(m.group(1)))
            except Exception:
                continue
        return "".join(parts)

    @staticmethod
    def _patch(html):
        m = re.search(r"Patch (\d+\.\d+)", html)
        if m:
            return m.group(1)
        m = re.search(r"aram-augment/[^\"]*v=(\d+)", html)
        return m.group(1) if m else None

    def get_champion_augments(self, champion_slug, use_cache=True):
        key = champion_slug.lower()
        if use_cache and key in self._cache:
            return self._cache[key]

        status, html = self._fetch(champion_slug)
        patch = self._patch(html)
        payload = self._flight(html)

        # The augment table lives in the flight payload as "data":[{...},...].
        # Decoding that array JSON-wise is far more reliable than regex-matching
        # individual objects (regex silently skipped ~11 rows with nested braces).
        rows = {}
        for m in re.finditer(r'"data":\[', payload):
            start = payload.index("[", m.start())
            try:
                arr, _ = json.JSONDecoder().raw_decode(payload[start:])
            except Exception:
                continue
            if not isinstance(arr, list):
                continue
            for obj in arr:
                if not (isinstance(obj, dict) and "id" in obj and "name" in obj
                        and "rarity" in obj):
                    continue
                rows[obj["name"]] = {
                    "augment_id": str(obj["id"]),
                    "key": obj.get("key"),
                    "name": obj["name"],
                    "rarity": RARITY_NAMES.get(obj.get("rarity"), str(obj.get("rarity"))),
                    "rarity_id": obj.get("rarity"),
                    "tier": obj.get("tier"),
                    "pick_rate": obj.get("popular"),
                    "performance": obj.get("performance"),
                    "patch": patch,
                    "source": "opgg",
                    "fetched_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                }

        result = {"champion": champion_slug, "patch": patch, "status": status,
                  "count": len(rows), "augments": rows}
        self._cache[key] = result
        return result

    def get_augment_stats(self, champion_slug, augment_name):
        data = self.get_champion_augments(champion_slug)
        for name, row in data["augments"].items():
            if name.lower() == augment_name.lower():
                return row
        return None


TEST_MATRIX = [
    ("yasuo", ["Soul Siphon", "Upgrade Infinity Edge", "Mystic Punch"]),
    ("sett", ["Soul Siphon", "Goliath"]),
    ("lux", ["Echo Cast", "Archmage"]),
    ("garen", ["Adamant", "Courage of the Colossus"]),
    ("ziggs", ["Magic Missile", "Poro Blaster"]),
]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true", help="dump the parsed table as JSON")
    args = ap.parse_args()

    print("=" * 78)
    print("OP.GG ARAM: Mayhem augment provider - acceptance test  (%s)"
          % time.strftime("%Y-%m-%d %H:%M:%S"))
    print("=" * 78)

    provider = OpggAugmentProvider()
    total = 0
    ok = 0
    dump = {}

    for slug, augments in TEST_MATRIX:
        t0 = time.time()
        try:
            data = provider.get_champion_augments(slug)
        except urllib.error.HTTPError as exc:
            print("[%s] HTTP %s" % (slug, exc.code))
            continue
        except Exception as exc:
            print("[%s] FAILED: %s: %s" % (slug, type(exc).__name__, exc))
            continue
        dt = time.time() - t0

        print("")
        print("[%s]  http=%s  patch=%s  augments=%d  (%.2fs)"
              % (slug, data["status"], data["patch"], data["count"], dt))
        print("  %-30s %-10s %10s %13s %s" % ("augment", "rarity", "pick_rate", "performance", "matched"))
        for name in augments:
            total += 1
            row = data["augments"].get(name)
            if row:
                ok += 1
                print("  %-30s %-10s %9.2f%% %13.2f  YES"
                      % (name, row["rarity"], row["pick_rate"], row["performance"]))
            else:
                near = [n for n in data["augments"] if name.split()[0].lower() in n.lower()]
                print("  %-30s %-10s %10s %13s  no (candidates: %s)"
                      % (name, "-", "-", "-", near[:3]))
        dump[slug] = data

    print("")
    print("=" * 78)
    print("matched %d/%d requested augments across %d champions" % (ok, total, len(TEST_MATRIX)))

    # cache behaviour: second call must not hit the network
    t0 = time.time()
    provider.get_champion_augments("yasuo")
    print("cache hit for yasuo took %.4fs" % (time.time() - t0))

    if args.json:
        with open("opgg/provider_dump.json", "w", encoding="utf-8") as fh:
            json.dump(dump, fh, ensure_ascii=False, indent=1)
        print("wrote opgg/provider_dump.json")

    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
