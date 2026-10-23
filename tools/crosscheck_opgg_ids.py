"""Day 3 §11：验证腾讯 augmentID 与 OP.GG 的 id 同源。

随机抽 20 个 Mayhem 海克斯（覆盖 Silver / Gold / Prismatic），
检查它们是否都出现在 OP.GG 的 aram-augments 响应里。

结果写入 data/_opgg_id_crosscheck.txt（UTF-8）。
"""

from __future__ import annotations

import io
import json
import os
import random
import ssl
import sys
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.data.augment_repository import AugmentRepository  # noqa: E402

#: 覆盖射手/法师/坦克/刺客/辅助/战士等不同定位，避免"按英雄收录"造成的假阴性
BASE = "https://lol-api-champion.op.gg/api/contents/stats/champions/%d/aram-augments"
CHAMPIONS = [157, 103, 32, 99, 86, 115, 22, 64, 89, 1, 4, 7, 11, 14, 16,
             21, 24, 26, 29, 31, 34, 35, 36, 37, 39]
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")


def fetch_opgg_ids() -> dict[int, set[int]]:
    out: dict[int, set[int]] = {}
    ctx = ssl.create_default_context()
    for champion_id in CHAMPIONS:
        request = urllib.request.Request(
            BASE % champion_id,
            headers={"User-Agent": UA, "Accept": "application/json",
                     "Origin": "https://op.gg", "Referer": "https://op.gg/"},
        )
        with urllib.request.urlopen(request, timeout=45, context=ctx) as response:
            payload = json.loads(response.read().decode("utf-8", "replace"))
        out[champion_id] = {row["id"] for row in payload.get("data", [])}
    return out


def main() -> int:
    repository = AugmentRepository().load()
    lines: list[str] = []

    lines.append("=== 腾讯 KIWI 字典 ===")
    lines.append("原始条目: %d" % len(repository.all_items))
    lines.append("Mayhem(KIWI) 有效: %d" % len(repository.items))

    by_level: dict[str, int] = {}
    for item in repository.items:
        by_level[item.level or "unknown"] = by_level.get(item.level or "unknown", 0) + 1
    lines.append("等级分布: %s" % json.dumps(by_level, ensure_ascii=False))

    excluded = [i for i in repository.all_items if not i.in_mode("KIWI")]
    lines.append("被排除(KIWI_JADE-only / 空 mode): %d" % len(excluded))
    lines.append("被排除示例: %s"
                 % [(i.augment_id, i.mode, i.name_cn) for i in excluded[:5]])
    lines.append("")

    opgg = fetch_opgg_ids()
    union: set[int] = set()
    for champion_id, ids in opgg.items():
        lines.append("OP.GG champion %d -> %d 条 id" % (champion_id, len(ids)))
        union |= ids
    lines.append("OP.GG 并集 id 数: %d" % len(union))
    lines.append("")

    tencent_ids = repository.ids()
    overlap = tencent_ids & union
    lines.append("腾讯 Mayhem id ∩ OP.GG 并集 = %d / %d (%.1f%%)"
                 % (len(overlap), len(tencent_ids),
                    100.0 * len(overlap) / max(1, len(tencent_ids))))
    lines.append("")

    # 随机抽 20 个，覆盖三个等级
    random.seed(20260918)
    sample: list = []
    for level in ("kSilver", "kGold", "kPrismatic"):
        pool = [i for i in repository.items if i.level == level]
        sample.extend(random.sample(pool, min(7, len(pool))))
    sample = sample[:20]

    lines.append("=== 随机抽样 20 个（跨 Silver/Gold/Prismatic）===")
    lines.append("%-8s %-12s %-22s %-12s %s"
                 % ("id", "level", "name_cn", "in_opgg", "opgg 出现的英雄"))
    hit = 0
    for item in sample:
        champions = [cid for cid, ids in opgg.items() if item.augment_id in ids]
        ok = bool(champions)
        hit += 1 if ok else 0
        lines.append("%-8s %-12s %-22s %-12s %s"
                     % (item.augment_id, (item.level or "-")[1:],
                        item.name_cn or "-", "YES" if ok else "NO",
                        champions if champions else "-"))

    lines.append("")
    lines.append("对照结果: %d/%d" % (hit, len(sample)))

    text = "\n".join(lines)
    with io.open(os.path.join("data", "_opgg_id_crosscheck.txt"), "w",
                 encoding="utf-8") as fh:
        fh.write(text + "\n")
    print(text)
    return 0 if hit == len(sample) else 1


if __name__ == "__main__":
    sys.exit(main())
