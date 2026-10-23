"""多局 Session 隔离用的假件：LCU / Live Client / 截图 / OCR / OP.GG。

用途：
  * tools/repro_session_bugs.py 复现四个实战 Bug
  * tests/test_session_isolation.py 回归测试

原则：不依赖真实 LCU、mss、RapidOCR 与网络。
只需要按真机日志的**相位序列**驱动即可：
    ChampSelect -> InProgress -> Reconnect/Unknown -> Lobby -> ChampSelect -> ...
"""

from __future__ import annotations

import argparse
import json
from typing import Callable, Optional, Sequence

from src.core.models import AugmentStats, DataScope
from src.data.repository import AugmentStatsRepository
from src.lcu.endpoints import (
    CHAMPION_SUMMARY,
    CHAMP_SELECT_SESSION,
    GAMEFLOW_PHASE,
)
from src.lcu.live_client import LiveGameSnapshot
from src.ocr.card_reader import CardReading, OfferSet
from src.ocr.gate import BandFeature
from src.opgg.client import OpggError
from src.opgg.provider import OpggProvider

#: LCU champion-summary.json 的极小子集（真实英雄 id / alias / 中文名）
CHAMPION_SUMMARY_PAYLOAD = [
    {"id": 4, "alias": "TwistedFate", "name": "卡牌大师"},
    {"id": 110, "alias": "Varus", "name": "惩戒之箭"},
    {"id": 131, "alias": "Diana", "name": "皎月女神"},
    {"id": 157, "alias": "Yasuo", "name": "疾风剑豪"},
    {"id": 64, "alias": "LeeSin", "name": "盲僧"},
    {"id": 222, "alias": "Jinx", "name": "暴走萝莉"},
]

#: Day 3 实机样本
VARUS_OFFERS = (1325, 1115, 1079)
#: 另一局（戴安娜）样本
DIANA_OFFERS = (2064, 1116, 1180)

AUGMENT_NAMES = {1325: "玻璃大炮", 1115: "最万用的瞄准镜", 1079: "战争交响乐"}


class FakeResponse:
    def __init__(self, status_code: int, text: str = "") -> None:
        self.status_code = status_code
        self.text = text

    def json(self):
        return json.loads(self.text)


class ScriptedLcuClient:
    """可控 LCU 假客户端。

    * ``phase_for(n)`` ：第 n 次读取 gameflow-phase 时返回的阶段
                        （用来模拟真机的相位序列）
    * ``champion_id`` ：选人会话里 localPlayer 当前的 championId
                        （随时改 = 玩家换英雄）
    """

    def __init__(self, phase_for: Optional[Callable[[int], str]] = None,
                 champion_id: int = 0,
                 summary: Sequence[dict] | None = None) -> None:
        self._phase_for = phase_for or (lambda n: "None")
        self.champion_id = champion_id
        self.summary = list(summary or CHAMPION_SUMMARY_PAYLOAD)
        self.phase_reads = 0
        self.champ_select_reads = 0

    async def get(self, path: str) -> FakeResponse:
        if path == GAMEFLOW_PHASE:
            self.phase_reads += 1
            return FakeResponse(200, json.dumps(self._phase_for(self.phase_reads)))
        return FakeResponse(404)

    async def get_json(self, path: str):
        if path == CHAMP_SELECT_SESSION:
            self.champ_select_reads += 1
            return 200, {
                "localPlayerCellId": 0,
                "timer": {"phase": "BAN_PICK"},
                "myTeam": [{
                    "cellId": 0,
                    "championId": self.champion_id,
                    "championPickIntent": self.champion_id,
                    "assignedPosition": "",
                    "summonerId": 1,
                }],
            }
        if path == CHAMPION_SUMMARY:
            return 200, list(self.summary)
        return 404, None


class FakeLiveClient:
    """Live Client Data API 假件：可用性 / 英雄 / 等级可随时改。"""

    def __init__(self, available: bool = False, champion_alias: str | None = None,
                 champion_name: str | None = None, level: int = 1) -> None:
        self.available = available
        self.champion_alias = champion_alias
        self.champion_name = champion_name
        self.level = level
        self.calls = 0

    @property
    def snapshot(self) -> LiveGameSnapshot:
        return LiveGameSnapshot(champion_name=self.champion_name,
                                champion_alias=self.champion_alias,
                                level=self.level, game_time=120.0, player_count=10)

    async def get_snapshot(self) -> Optional[LiveGameSnapshot]:
        self.calls += 1
        return self.snapshot if self.available else None

    async def get_all_game_data(self):
        return {} if self.available else None

    async def get_active_player(self):
        return {}


class FakeScreenCapture:
    def __init__(self, *args, **kwargs) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True

    def grab_region(self, box):  # pragma: no cover - 假件不应真截图
        raise AssertionError("测试中不应真的截图")


class FakeCardReader:
    """按脚本返回牌面；每次读取后触发 on_read(call_index)。

    同时实现 P0-2 门控需要的 ``probe_feature()``（窗口外的低成本检测），
    这样 live.py 的 WATCHDOG 分支也能在没有真实屏幕时被测试。
    """

    def __init__(self, script: Optional[Callable[[int], Sequence[int] | None]] = None,
                 on_read: Optional[Callable[[int], None]] = None,
                 feature_for: Optional[Callable[[int], BandFeature]] = None,
                 **kwargs) -> None:
        self.script = script or (lambda n: VARUS_OFFERS)
        self.on_read = on_read
        self._feature_for = feature_for or (
            lambda n: BandFeature(band_hash="probe-%d" % n, mean_brightness=100.0,
                                  changed=True, stable=False))
        self.calls = 0          # 兼容旧测试：等于 read_once 次数
        self.read_calls = 0
        self.probe_calls = 0
        self.ocr_calls = 0

    def read_once(self) -> OfferSet:
        self.calls += 1
        self.read_calls += 1
        if self.on_read is not None:
            self.on_read(self.calls)
        augment_ids = self.script(self.calls)
        if not augment_ids:
            return OfferSet(readings=[])
        readings = []
        for index, augment_id in enumerate(augment_ids):
            name = AUGMENT_NAMES.get(augment_id, "海克斯%d" % augment_id)
            readings.append(CardReading(index=index + 1, raw_text=name,
                                        normalized=name, augment_id=augment_id,
                                        name_cn=name, confidence=1.0,
                                        method="exact"))
        self.ocr_calls += len(readings)
        #: 与真实 CardReader 一致：全部匹配 = 全部已确认；没有 pending
        return OfferSet(readings=readings, pending_slots=[],
                        confirmed_slots=[i for i, r in enumerate(readings) if r.matched])

    def probe_feature(self) -> BandFeature:
        self.probe_calls += 1
        return self._feature_for(self.probe_calls)

    def pending_slots(self) -> list[int]:
        """与真实 CardReader 一致：假件没有 pending 概念。"""
        return []

    def reset_slots(self, reason: str = "") -> None:
        """与真实 CardReader 一致：新局/新 stage 会清空逐槽基线（假件无状态）。"""
        self.reset_count = getattr(self, "reset_count", 0) + 1
        self.reset_reason = reason


class OfflineOpggClient:
    """任何网络请求都直接失败：测试只允许走 SQLite / Memory。"""

    async def get_champion_augments(self, champion_id):  # noqa: ARG002
        raise OpggError("offline test")

    async def get_champion_stats(self, champion_id, region="kr", tier="ibsg"):  # noqa: ARG002
        raise OpggError("offline test")

    async def aclose(self) -> None:
        pass


def make_args(**overrides) -> argparse.Namespace:
    """快节奏参数（测试不需要真的等 1 秒 / 0.25 秒）。"""
    base = dict(scan_interval=0.01, champ_timeout=2.0, champ_poll_interval=0.01,
                level_poll_interval=0.01, watchdog_interval=0.01,
                wait_client=0.01, resident_pause=0.01,
                wait_game=1.0, resident=False, once=False, no_overlay=True,
                stats_repository=None)
    base.update(overrides)
    return argparse.Namespace(**base)


#: 两个英雄各自的 (augmentId, performance, popular)
DEFAULT_SEED = {
    110: [(1325, 70.88, 0.49), (1115, 81.77, 1.93), (1079, 80.07, 0.84)],
    131: [(2064, 74.63, 1.54), (1116, 66.10, 0.31), (1180, 90.02, 2.75)],
}


def seed_provider(database, offers_by_champion=None, ttl_hours: float = 6.0,
                  patch: str = "16.18") -> OpggProvider:
    """把英雄的海克斯统计写进 SQLite，测试全程离线可用。"""
    from src.config import load_settings

    #: None = 用默认样本；{} = 明确"不预置任何缓存"（测试网络/重试路径时要用）
    rows_by_champion = DEFAULT_SEED if offers_by_champion is None else offers_by_champion
    repository = AugmentStatsRepository(database)
    for champion_id, rows in rows_by_champion.items():
        repository.upsert_many(champion_id, [
            AugmentStats(augment_id=augment_id, performance=performance,
                         popular=popular, scope=DataScope.aggregate_scope())
            for augment_id, performance, popular in rows
        ], patch=patch)
    settings = load_settings()
    settings.cache.opgg_ttl_hours = ttl_hours
    return OpggProvider(client=OfflineOpggClient(), stats_repository=repository,
                        settings=settings, ttl_seconds=ttl_hours * 3600.0)


class Installed:
    """install_fakes() 的返回值：持有所有假件，便于断言与驱动。"""

    def __init__(self, client: ScriptedLcuClient, live: FakeLiveClient,
                 capture: FakeScreenCapture, reader: FakeCardReader) -> None:
        self.client = client
        self.live = live
        self.capture = capture
        self.reader = reader


def install_fakes(monkeypatch, *, client: ScriptedLcuClient,
                  live: FakeLiveClient | None = None,
                  reader: FakeCardReader | None = None) -> Installed:
    """把 live.py 里的真实数据源替换成假件。"""
    from src import live as live_mod
    from src.ocr import capture as capture_mod
    from src.ocr import card_reader as card_reader_mod

    live_client = live or FakeLiveClient()
    capture = FakeScreenCapture()
    card_reader = reader or FakeCardReader()

    monkeypatch.setattr(live_mod, "LiveClientDataSource",
                        lambda *a, **kw: live_client)
    monkeypatch.setattr(capture_mod, "ScreenCapture", lambda *a, **kw: capture)
    monkeypatch.setattr(card_reader_mod, "CardReader",
                        lambda *a, **kw: card_reader)
    return Installed(client, live_client, capture, card_reader)


__all__ = [
    "CHAMPION_SUMMARY_PAYLOAD", "VARUS_OFFERS", "DIANA_OFFERS", "AUGMENT_NAMES",
    "DEFAULT_SEED", "ScriptedLcuClient", "FakeLiveClient", "FakeScreenCapture",
    "FakeCardReader", "OfflineOpggClient", "make_args", "seed_provider",
    "install_fakes", "Installed",
]
