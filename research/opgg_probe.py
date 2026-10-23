"""OP.GG ARAM: Mayhem augment data-source reverse engineering probe.

ASCII-only source on purpose (the container console mangles non-ASCII output).
Writes a UTF-8 report file and prints an ASCII summary.
"""
from __future__ import print_function

import io
import json
import os
import re
import sys

import requests

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "opgg")
REPORT = os.path.join(OUT, "_report.txt")

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

CHAMPS = ["yasuo", "sett", "lux", "garen", "ziggs"]

lines = []


def log(msg=""):
    lines.append(msg)
    try:
        print(msg)
    except Exception:
        print(msg.encode("ascii", "replace").decode("ascii"))


def fetch(session, url):
    return session.get(url, timeout=30, headers={
        "User-Agent": UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9,zh-CN;q=0.8",
    })


def main():
    if not os.path.isdir(OUT):
        os.makedirs(OUT)
    session = requests.Session()

    pages = {}
    for c in CHAMPS:
        url = "https://op.gg/lol/modes/aram-mayhem/%s/augments" % c
        try:
            r = fetch(session, url)
            pages[c] = r
            path = os.path.join(OUT, "augments_%s.html" % c)
            with io.open(path, "w", encoding="utf-8") as fh:
                fh.write(r.text)
            log("[page] %-28s status=%s bytes=%d -> %s"
                % (c, r.status_code, len(r.text), os.path.basename(path)))
        except Exception as exc:  # noqa: BLE001
            log("[page] %-28s FAILED: %s" % (c, exc))

    log("")
    log("=========== per-page signal detection ===========")
    for c, r in pages.items():
        html = r.text
        log("--- %s (status %s, %d bytes) ---" % (c, r.status_code, len(html)))
        for marker in ["__NEXT_DATA__", "self.__next_f", "buildId", "/_next/data",
                       "\"augment", "augmentName", "pickRate", "winRate", "GraphQL"]:
            log("  %-18s occurrences=%d" % (marker, html.count(marker)))
        hosts = sorted(set(re.findall(r"https?://[A-Za-z0-9._\-]*op\.gg[^\"'\\\s<>]*", html)))
        log("  op.gg urls (%d):" % len(hosts))
        for h in hosts[:20]:
            log("    %s" % h)
        apis = sorted(set(re.findall(r"[\"'](/api/[A-Za-z0-9._/\-]{2,80})[\"']", html)))
        log("  /api/ paths (%d): %s" % (len(apis), apis[:15]))
        nextdata = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.S)
        if nextdata:
            try:
                data = json.loads(nextdata.group(1))
                props = data.get("props", {})
                log("  __NEXT_DATA__ buildId=%s pageKeys=%s"
                    % (data.get("buildId"), list(props.get("pageProps", {}).keys())[:12]))
                blob = json.dumps(props, ensure_ascii=False)
                with io.open(os.path.join(OUT, "nextdata_%s.json" % c), "w",
                             encoding="utf-8") as fh:
                    fh.write(json.dumps(data, ensure_ascii=False, indent=1))
                log("  pageProps json bytes=%d, contains 'augment'=%s"
                    % (len(blob), "augment" in blob.lower()))
            except Exception as exc:  # noqa: BLE001
                log("  __NEXT_DATA__ parse failed: %s" % exc)
        log("")

    with io.open(REPORT, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines))
    print("\nreport written: %s" % REPORT)


if __name__ == "__main__":
    sys.exit(main())
