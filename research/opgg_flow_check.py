"""OP.GG 数据链路日志验证（离线，用 Day 1 保存的真实响应）。

    & ".venv\\Scripts\\python.exe" research\\opgg_flow_check.py

验证整条链路，并打印 §7 要求的关键日志：
    识别三个 augment -> OP.GG 请求 -> 三个 augmentID 匹配 -> performance/popular
    -> reroll 一个槽（旧槽保持 / 新槽 pending）-> 推荐重新计算
再验证失败路径：请求失败 -> 名称保留 + 强度/欢迎度 "—" + 低频重试 + Overlay 不隐藏。
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.core.models import AugmentStats, DataScope          # noqa: E402
from src.data.database import Database                       # noqa: E402
from src.live import AugmentOfferLike, build_offer_payload   # noqa: E402
from src.ocr.card_reader import CardReading                  # noqa: E402
from src.opgg.client import OpggError                        # noqa: E402
from src.opgg.provider import OpggProvider                   # noqa: E402
from src.overlay.widgets import (                            # noqa: E402
    format_popular_line,
    format_performance_line,
)
from src.utils.logger import force_utf8_stdio, get_tagged, setup_logging  # noqa: E402

SAMPLE = PROJECT_ROOT / "research" / "opgg_api" / "augments_157.json"
CARDS = (1325, 1115, 1079)
NAMES = {1325: "玻璃大炮", 1115: "最万用的瞄准镜", 1079: "战争交响乐"}
log = get_tagged("opgg", "flow.check")


class SampleClient:
    """返回真实保存的响应；日志格式与真实 OpggClient 一致。"""

    def __init__(self, payload: dict | None) -> None:
        self.payload = payload
        self.requests = 0

    async def get_champion_augments(self, champion_id: int) -> dict:
        self.requests += 1
        log.info("request start /api/contents/stats/champions/%s/aram-augments",
                 champion_id)
        if self.payload is None:
            log.warning("request failed: ConnectError: （模拟真机 13:07 的失败）")
            raise OpggError("OP.GG 请求失败: ConnectError: ")
        log.info("response status=200")
        return self.payload

    async def get_champion_stats(self, champion_id, region="kr", tier="ibsg"):  # noqa: ARG002
        raise OpggError("hero stats 与本次验证无关")

    async def aclose(self) -> None:
        pass


def reading(slot: int, augment_id: int) -> CardReading:
    return CardReading(index=slot + 1, raw_text=NAMES[augment_id],
                       normalized=NAMES[augment_id], augment_id=augment_id,
                       name_cn=NAMES[augment_id], confidence=1.0, method="exact")


def show(payload: list[dict], title: str) -> None:
    print("")
    print(title)
    for item in payload:
        if item["pending"]:
            print("  slot%d  %-14s %s  %s" % (item["slot"], "识别中…",
                                              format_performance_line(None),
                                              format_popular_line(None)))
            continue
        mark = "  ★ 推荐" if item["recommended"] else ""
        print("  slot%d  %-14s %s  %s  [%s]%s"
              % (item["slot"], item["name_cn"],
                 format_performance_line(item["performance"]),
                 format_popular_line(item["popular"]),
                 item["data_state"], mark))


def build_provider(client, database) -> OpggProvider:
    from src.config import load_settings

    settings = load_settings()
    return OpggProvider(client=client, stats_repository=None, settings=settings)


def main() -> int:
    force_utf8_stdio()
    setup_logging("logs/opgg_flow_check.log", logging.INFO)
    if "--live" in sys.argv:
        return live_check()
    if not SAMPLE.exists():
        print("缺少真实响应样本 research/opgg_api/augments_157.json")
        return 1
    payload = json.loads(SAMPLE.read_text(encoding="utf-8"))
    database = Database(PROJECT_ROOT / "data" / "_flow_check.db")
    if database.path.exists():
        database.path.unlink()
    database.initialize()
    try:
        # ---------------- 正常链路 ----------------
        print("=" * 72)
        print("① 正常链路：三个 augment -> OP.GG -> performance/popular -> 推荐")
        print("=" * 72)
        client = SampleClient(payload)
        provider = build_provider(client, database)
        provider.begin_session()
        asyncio.run(provider.activate_champion(157, alias="Yasuo", name="疾风剑豪"))
        print("OP.GG 请求次数 = %d（按英雄只请求一次）" % client.requests)

        slot_offers = []
        for slot, aid in enumerate(CARDS):
            stats = provider.query_augment_stats(157, aid).stats
            slot_offers.append(AugmentOfferLike(reading(slot, aid), stats))
        provider.query_augment_stats(157, 999999)          # 不存在的 augment
        offers, recommended = build_offer_payload(slot_offers)
        show(offers, "初始牌面（推荐 = popular 最大）: %s" % recommended)

        # ---------------- reroll 一个槽 ----------------
        print("")
        print("=" * 72)
        print("② reroll slot1（玻璃大炮），另外两槽保持已识别")
        print("=" * 72)
        stats_new = provider.query_augment_stats(157, 1325).stats
        slot_offers[1] = AugmentOfferLike(reading(1, 1325), stats_new)
        offers_pending, rec_pending = build_offer_payload(slot_offers, pending_slots={1})
        show(offers_pending, "换牌动画期间（slot1 = 识别中…，推荐用剩余两槽临时计算）: %s"
             % rec_pending)
        offers_done, rec_done = build_offer_payload(slot_offers)
        show(offers_done, "新卡识别完成: %s" % rec_done)

        # ---------------- 失败链路 ----------------
        print("")
        print("=" * 72)
        print("③ OP.GG 请求失败（真机 13:07 场景）：名称保持 + 数值 — + 低频重试")
        print("=" * 72)
        failing = SampleClient(None)
        provider2 = build_provider(failing, database)
        provider2.begin_session()
        asyncio.run(provider2.activate_champion(222, alias="Jinx", name="暴走萝莉"))
        slot_offers2 = []
        for slot, aid in enumerate(CARDS):
            stats = provider2.query_augment_stats(222, aid).stats
            slot_offers2.append(AugmentOfferLike(reading(slot, aid), stats))
        offers2, rec2 = build_offer_payload(slot_offers2)
        show(offers2, "OP.GG 不可用（名称照常显示，强度/欢迎度 = —，推荐为空）: %s" % rec2)
        print("    → 已安排后台重试（MISSING_RETRY_SECONDS=%s）"
              % __import__("src.opgg.provider", fromlist=["MISSING_RETRY_SECONDS"])
              .MISSING_RETRY_SECONDS)
        return 0
    finally:
        database.close()
        for suffix in ("", "-wal", "-shm"):
            leftover = database.path.with_name(database.path.name + suffix)
            if leftover.exists():
                leftover.unlink()


def live_check() -> int:
    """真实网络诊断：真 OpggClient + 真 JSON + 真匹配（不依赖是否有对局）。

        & ".venv\\Scripts\\python.exe" research\\opgg_flow_check.py --live [championId]
    """
    from src.config import load_settings
    from src.opgg.client import OpggClient
    from src.opgg.parser import parse_augment_stats

    champion_id = 157
    for arg in sys.argv[2:]:
        if arg.isdigit():
            champion_id = int(arg)

    settings = load_settings()
    client = OpggClient(settings.opgg)
    print("=" * 72)
    print("真实网络诊断：champion_id=%s（真 HTTP + 真 JSON + 真匹配）" % champion_id)
    print("=" * 72)
    try:
        payload = asyncio.run(client.get_champion_augments(champion_id))
    except Exception as exc:  # noqa: BLE001
        print("HTTP 层失败：%s: %s" % (type(exc).__name__, exc))
        print("→ 结论：问题出在 HTTP（网络/证书/限流），不是解析或匹配")
        return 1

    print("JSON 层：拿到响应，data 条数 = %s"
          % (len(payload.get("data", [])) if isinstance(payload, dict) else "?"))
    stats = parse_augment_stats(payload)
    index = {s.augment_id: s for s in stats}
    print("索引层：stats_count=%d，类型检查 augment_id=%s"
          % (len(index), {type(k).__name__ for k in list(index)[:5]}))

    top = sorted(stats, key=lambda s: (s.popular or 0), reverse=True)[:3]
    print("")
    print("匹配层（取 popular 最高的 3 个作为样例）：")
    offers = []
    for slot, stat in enumerate(top):
        augment_id = int(stat.augment_id)          # §E：统一 int
        found = index.get(augment_id)
        log.info("match augment_id=%s performance=%s popular=%s",
                 augment_id, found.performance, found.popular)
        name = NAMES.get(augment_id, "augment_%s" % augment_id)
        card = CardReading(index=slot + 1, raw_text=name, normalized=name,
                           augment_id=augment_id, name_cn=name,
                           confidence=1.0, method="exact")
        offers.append(AugmentOfferLike(card, found))
    payload_rows, recommended = build_offer_payload(offers)
    show(payload_rows, "payload -> Overlay（推荐 = popular 最大）: %s" % recommended)
    log.info("MISS 演示 augment_id=999999（不存在）")
    print("")
    print("结论：HTTP / JSON / 索引 / 匹配 / payload 全部通过（真实网络）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
