"""Day 3 真实样本抓取：截图 -> OCR -> matcher -> 腾讯字典 -> OP.GG。

仅用于研究留档，不属于正式项目代码。

    python research/capture_augment_sample.py <截图路径> <stamp>
"""

from __future__ import annotations

import io
import json
import os
import ssl
import sys
import urllib.request

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from src.data.augment_repository import AugmentRepository  # noqa: E402
from src.ocr.engine import OcrEngine  # noqa: E402
from src.ocr.matcher import AugmentMatcher, normalize_text  # noqa: E402

CAPTURE_DIR = os.path.join(PROJECT_ROOT, "research", "captures")

#: 三个卡片名称所在的横向区间（2560x1440 基准）
CARD_X_RANGES = [(560, 1010), (1010, 1520), (1550, 2050)]
#: 名称所在纵向带；下方的「综合 / 伤害 / 伤害速度」标签必须排除
NAME_BAND_Y = (565, 614)
MIN_TEXT_HEIGHT = 20


def _center(box):
    return ((box[0] + box[2]) / 2, (box[1] + box[3]) / 2)


def collect_card_blocks(results):
    """按卡片横向区间 + 名称纵向带，收集每张卡的候选文本块。"""
    raw_hits = [[] for _ in CARD_X_RANGES]
    for r in results:
        cx, cy = _center(r.bbox)
        if not (NAME_BAND_Y[0] <= cy <= NAME_BAND_Y[1]):
            continue
        if (r.bbox[3] - r.bbox[1]) < MIN_TEXT_HEIGHT:
            continue
        for index, (x0, x1) in enumerate(CARD_X_RANGES):
            if x0 <= cx <= x1:
                raw_hits[index].append(r)
                break
    for index, hits in enumerate(raw_hits):
        hits.sort(key=lambda r: r.bbox[0])
    return raw_hits


def resolve_card(hits, matcher):
    """逐块候选匹配：优先 exact/normalized，其次多块拼接，最后 fuzzy。

    返回 (match, 使用的原文)。
    """
    if not hits:
        return None, ""

    # 1) 逐个文本块单独匹配，优先精确
    best = None
    for hit in hits:
        match = matcher.match(hit.text)
        if not match.matched:
            continue
        if match.method in ("exact", "normalized"):
            return match, hit.text
        if best is None or match.confidence > best[0].confidence:
            best = (match, hit.text)
    if best is not None:
        return best

    # 2) 多块拼接后再匹配（名称被 OCR 拆成两块的情况）
    for hit in hits:
        match = matcher.match(hit.text)
        if match.matched:
            return match, hit.text
    joined = "".join(h.text for h in hits)
    return matcher.match(joined), joined


def resolve_champion_id(alias: str, client) -> int | None:
    import asyncio

    async def _run():
        try:
            status, data = await client.get_json(
                "/lol-game-data/assets/v1/champion-summary.json")
            if status != 200:
                return None
            for entry in data or []:
                if isinstance(entry, dict) and (entry.get("alias") or "").lower() == alias.lower():
                    return entry.get("id")
        finally:
            await client.aclose()
        return None

    return asyncio.run(_run())


def fetch_opgg(champion_id: int) -> dict:
    url = ("https://lol-api-champion.op.gg/api/contents/stats/champions/%d/aram-augments"
           % champion_id)
    request = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        "Accept": "application/json, text/plain, */*",
        "Origin": "https://op.gg",
        "Referer": "https://op.gg/",
    })
    context = ssl.create_default_context()
    with urllib.request.urlopen(request, timeout=45, context=context) as response:
        payload = json.loads(response.read().decode("utf-8", "replace"))
    return {row["id"]: row for row in payload.get("data", [])}


def main(argv):
    png = argv[0] if argv else os.path.join(
        CAPTURE_DIR, "augment_select_20260918_213517.png")
    stamp = argv[1] if len(argv) > 1 else "20260918_213517"

    repository = AugmentRepository().load()
    matcher = AugmentMatcher(repository)
    engine = OcrEngine()
    results = engine.recognize(png)

    raw_hits = collect_card_blocks(results)
    resolved = [resolve_card(hits, matcher) for hits in raw_hits]

    # 英雄：Live Client API 给的是中文名，用 LCU 反查 championId
    champion_id = None
    try:
        from src.lcu.client import LcuClient
        from src.lcu.credential import LcuCredentialProvider

        credentials = LcuCredentialProvider().find_credentials()
        if credentials:
            client = LcuClient(credentials)
            if client.connect():
                champion_id = resolve_champion_id("Varus", client)
    except Exception as exc:  # noqa: BLE001
        print("champion resolve failed: %s" % exc)

    opgg = {}
    if champion_id:
        try:
            opgg = fetch_opgg(champion_id)
        except Exception as exc:  # noqa: BLE001
            print("opgg fetch failed: %s" % exc)

    report = {
        "stamp": stamp,
        "screenshot": os.path.abspath(png),
        "resolution": list(engine.recognize(png)[0].bbox) if False else None,
        "champion": {"championId": champion_id, "alias": "Varus", "name": "惩戒之箭"},
        "opgg_scope": "OP.GG Aggregate",
        "cards": [],
    }

    lines = []
    lines.append("Champion: 惩戒之箭")
    lines.append("ChampionId: %s" % champion_id)
    lines.append("Alias: Varus")
    lines.append("Name: 惩戒之箭")
    lines.append("")
    lines.append("%-5s %-22s %-16s %-8s %-10s %-6s %s"
                 % ("卡片", "OCR原文", "标准名称", "augmentId", "method", "conf", "腾讯 level"))
    for index, (match, text) in enumerate(resolved, 1):
        if match is None:
            match = matcher.match("")
        item = repository.get(match.augment_id) if match.augment_id else None
        stats = opgg.get(match.augment_id) if match.augment_id else None
        report["cards"].append({
            "index": index,
            "ocr_raw": text,
            "ocr_blocks": [{"text": r.text, "confidence": r.confidence,
                            "bbox": list(r.bbox)} for r in raw_hits[index - 1]],
            "normalized": normalize_text(text),
            "augment_id": match.augment_id,
            "name_cn": match.name_cn,
            "confidence": match.confidence,
            "method": match.method,
            "tencent": ({"name_cn": item.name_cn, "name_en": item.name_en,
                         "level": item.level, "mode": item.mode} if item else None),
            "opgg": ({"performance": stats.get("performance"),
                      "popular": stats.get("popular"),
                      "opgg_augment_tier": stats.get("tier")} if stats else None),
        })
        lines.append("%-5d %-22s %-16s %-8s %-10s %-6.3f %s"
                     % (index, text, match.name_cn or "-", match.augment_id or "-",
                        match.method, match.confidence,
                        item.level if item else "-"))

    lines.append("")
    for entry in report["cards"]:
        lines.append("Augment %d:" % entry["index"])
        lines.append("  OCR: %s" % entry["ocr_raw"])
        lines.append("  augmentId: %s" % entry["augment_id"])
        lines.append("  name: %s (%s)" % (entry["name_cn"],
                                          (entry["tencent"] or {}).get("name_en")))
        opgg_row = entry["opgg"] or {}
        lines.append("  performance: %s" % opgg_row.get("performance"))
        lines.append("  popular: %s" % opgg_row.get("popular"))
        lines.append("  opgg_augment_tier: %s" % opgg_row.get("opgg_augment_tier"))
        lines.append("  scope: OP.GG Aggregate")
        lines.append("")

    with io.open(os.path.join(CAPTURE_DIR, "sample_%s.json" % stamp), "w",
                 encoding="utf-8") as fh:
        json.dump(report, fh, ensure_ascii=False, indent=1)
    with io.open(os.path.join(CAPTURE_DIR, "sample_%s.txt" % stamp), "w",
                 encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")

    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
