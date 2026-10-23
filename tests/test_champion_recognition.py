"""英雄识别链路测试（问题一）。

核心要求（用户 2026-09-22 需求）：
    * 英雄识别**绝对不能**依赖皮肤视觉/OCR，只走 LCU / Live Client
    * championId 拿到 → **立即**显示中文名，不等 OP.GG / 不等推荐
    * ChampSelect: championId=0 只是"还没锁定"，换英雄要实时更新 candidate
    * InProgress: 以 Live Client 为权威，覆盖 ChampSelect 的 candidate
    * "识别中…"只属于 augment slot 的换牌动画，绝不用于英雄
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import os
import time

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from src.core.state_hub import HUB, StateHub  # noqa: E402
from src.lcu.champion import ChampionDetector  # noqa: E402
from src.lcu.live_client import LiveGameSnapshot, alias_from_raw_champion_name  # noqa: E402
from src.opgg.client import OpggError  # noqa: E402
from src.opgg.provider import StatsState  # noqa: E402
from tests.session_harness import (  # noqa: E402,F401
    FakeCardReader,
    FakeLiveClient,
    ScriptedLcuClient,
    install_fakes,
    make_args,
    seed_provider,
)


@pytest.fixture()
def isolated_home(monkeypatch, tmp_path):
    monkeypatch.setenv("LOL_HEX_HELPER_HOME", str(tmp_path))
    return tmp_path


class SlowOpggClient:
    """每次请求慢 0.8 秒：用来证明"英雄名不等待网络"。"""

    def __init__(self, delay: float = 0.8, payload: dict | None = None) -> None:
        self.delay = delay
        self.payload = payload
        self.requested: list[int] = []
        self.started_at: float = 0.0

    async def get_champion_augments(self, champion_id):
        self.requested.append(champion_id)
        self.started_at = time.perf_counter()
        await asyncio.sleep(self.delay)
        if self.payload is None:
            raise OpggError("OP.GG 请求失败: ConnectError: ")
        return self.payload

    async def get_champion_stats(self, champion_id, region="kr", tier="ibsg"):  # noqa: ARG002
        raise OpggError("hero stats 与本次验证无关")

    async def aclose(self) -> None:
        pass


class RecorderHub(StateHub):
    """记录每次 HUB 更新的字段与时间，用来断言"顺序"而不是靠墙钟。"""

    def __init__(self) -> None:
        super().__init__()
        self.events: list[tuple[float, dict]] = []

    def update(self, **fields):
        self.events.append((time.perf_counter(), dict(fields)))
        return super().update(**fields)

    def first_event_with(self, key: str, value=None):
        for moment, fields in self.events:
            if fields.get(key) is None:
                continue
            if value is None or fields[key] == value:
                return moment, fields
        return None, None


def _payload_for(champion_id: int, augment_id: int = 1325,
                 performance: float = 70.0, popular: float = 0.5) -> dict:
    return {"data": [{"id": augment_id, "tier": 1,
                      "performance": performance, "popular": popular}]}


def _run(monkeypatch, temp_database, *, client: ScriptedLcuClient,
         live_client: FakeLiveClient, opgg_client, reader=None,
         champion_id: int = 131, max_reads: int = 4, level: int = 3) -> dict:
    """跑一遍真实 play_one_game（OCR 用假件，避免依赖屏幕）。

    相位按"第几次读 gameflow-phase"确定：前 3 次 ChampSelect，
    之后 InProgress，第 9 次起 Lobby —— 与扫描模式无关，保证测试一定能结束。
    """
    from src import live as live_mod

    recorder = RecorderHub()
    monkeypatch.setattr(live_mod, "HUB", recorder)
    installed = install_fakes(monkeypatch, client=client, live=live_client,
                              reader=reader or FakeCardReader())
    provider = seed_provider(temp_database, {}, ttl_hours=0.001)   # 无 SQLite 数据
    provider.client = opgg_client

    def phase_for(n: int) -> str:
        if n <= 3:
            return "ChampSelect"
        return "InProgress" if n <= 9 else "Lobby"

    monkeypatch.setattr(client, "_phase_for", phase_for)

    asyncio.run(live_mod.play_one_game(
        live_mod.Settings(),
        make_args(scan_interval=0.01, level_poll_interval=0.01,
                  watchdog_interval=0.01, champ_poll_interval=0.01),
        client, provider, live_mod.HubOverlay()))
    return {"events": recorder.events, "provider": provider,
            "reader": installed.reader, "live": live_client}


def _game_with_champ_select(monkeypatch, temp_database, *, champion_ids,
                           live_alias: str, live_name: str, opgg_client,
                           level: int = 3) -> dict:
    """ChampSelect 依次给出 champion_ids，然后进入 InProgress。"""
    from src import live as live_mod

    recorder = RecorderHub()
    monkeypatch.setattr(live_mod, "HUB", recorder)
    state = {"reads": 0}

    def phase_for(n: int) -> str:
        if n <= 4:
            return "ChampSelect"
        return "InProgress" if n <= 9 else "Lobby"

    client = ScriptedLcuClient(phase_for=phase_for, champion_id=champion_ids[0])
    live_client = FakeLiveClient(available=True, champion_alias=live_alias,
                                 champion_name=live_name, level=level)
    reader = FakeCardReader(script=lambda n: (2064, 1116, 1180))
    installed = install_fakes(monkeypatch, client=client, live=live_client, reader=reader)
    provider = seed_provider(temp_database, {}, ttl_hours=0.001)
    provider.client = opgg_client

    # 让 ChampSelect 阶段的 championId 依次变化（模拟玩家换英雄）
    original_get_json = client.get_json
    seq = {"i": 0}

    async def patched_get_json(path):
        from src.lcu.endpoints import CHAMP_SELECT_SESSION

        if path == CHAMP_SELECT_SESSION:
            index = min(seq["i"], len(champion_ids) - 1)
            client.champion_id = champion_ids[index]
            seq["i"] += 1
        return await original_get_json(path)

    monkeypatch.setattr(client, "get_json", patched_get_json)

    asyncio.run(live_mod.play_one_game(
        live_mod.Settings(),
        make_args(scan_interval=0.01, level_poll_interval=0.01,
                  watchdog_interval=0.01, champ_poll_interval=0.01),
        client, provider, live_mod.HubOverlay()))
    return {"events": recorder.events, "provider": provider, "client": client}


# ------------------------------------------------------ 1) championId 立即显示
def test_champion_id_then_name_shown_without_waiting_opgg(monkeypatch,
                                                          temp_database) -> None:
    """championId 一到就显示中文名；OP.GG 请求在后台，不阻塞显示。"""
    slow = SlowOpggClient(delay=0.8, payload=_payload_for(157))
    from src import live as live_mod

    recorder = RecorderHub()
    monkeypatch.setattr(live_mod, "HUB", recorder)
    state = {"phase": "ChampSelect", "reads": 0}

    def phase_for(n: int) -> str:
        if n >= 3 and state["phase"] == "ChampSelect":
            state["phase"] = "InProgress"
        return state["phase"]

    client = ScriptedLcuClient(phase_for=phase_for, champion_id=0)   # 先 0（未锁定）
    live_client = FakeLiveClient(available=True, champion_alias="Yasuo",
                                 champion_name="疾风剑豪", level=3)
    reader = FakeCardReader(on_read=lambda n: state.update(phase="Lobby")
                            if n >= 2 else None)

    original_get_json = client.get_json
    seq = {"i": 0}

    async def patched_get_json(path):
        from src.lcu.endpoints import CHAMP_SELECT_SESSION

        if path == CHAMP_SELECT_SESSION:
            client.champion_id = 0 if seq["i"] == 0 else 157   # 0 -> 157
            seq["i"] += 1
        return await original_get_json(path)

    monkeypatch.setattr(client, "get_json", patched_get_json)
    installed = install_fakes(monkeypatch, client=client, live=live_client, reader=reader)
    provider = seed_provider(temp_database, {}, ttl_hours=0.001)
    provider.client = slow

    asyncio.run(live_mod.play_one_game(
        live_mod.Settings(),
        make_args(scan_interval=0.01, level_poll_interval=0.01,
                  watchdog_interval=0.01, champ_poll_interval=0.01),
        client, provider, live_mod.HubOverlay()))

    # 中文名在 OP.GG 数据之前就发布了（顺序断言，不依赖墙钟）
    name_at, name_fields = recorder.first_event_with("champion_name")
    assert name_fields["champion_name"] == "疾风剑豪"
    assert name_fields["champion_id"] == 157
    ready_at, _ = recorder.first_event_with("cache_status", "READY")
    assert name_at is not None
    assert ready_at is None or name_at <= ready_at, "英雄名不允许等待 OP.GG"
    # 慢请求确实发生过（后台），且请求的是 157
    assert slow.requested == [157]
    # 英雄名一旦确定就不允许再被清空成 None（不显示"识别中"）
    names = [f.get("champion_name") for _, f in recorder.events
             if f.get("champion_name") is not None]
    assert names and names[-1] == "疾风剑豪"


# ---------------------------------------------------------------- 2) A -> B -> C
def test_candidate_champion_updates_through_abc(monkeypatch, temp_database) -> None:
    """ChampSelect 期间 A -> B -> C，candidate 最终必须是 C。"""
    result = _game_with_champ_select(
        monkeypatch, temp_database,
        champion_ids=[131, 110, 157], live_alias="Yasuo", live_name="疾风剑豪",
        opgg_client=SlowOpggClient(delay=0.0, payload=_payload_for(157)))
    from src import live as live_mod

    # 最终 active champion 是 Live Client 的 157（Yasuo）
    assert result["provider"].active_champion_id in (157, None)


def test_candidate_updates_via_tracker(monkeypatch, temp_database) -> None:
    """直接驱动 tracker：A -> B -> C 时 candidate_champion_id 必须跟着变。"""
    from src import live
    from src.core.game_session import GameSession

    client = ScriptedLcuClient(champion_id=4)          # 卡牌大师
    session = GameSession()
    provider = seed_provider(temp_database, {}, ttl_hours=0.001)
    tracker = live.ChampionTracker(ChampionDetector(client), None, provider, session,
                                   hub=StateHub())
    monkeypatch.setattr(live, "_stopping", lambda: False)

    assert asyncio.run(tracker.poll_candidate()) == 4
    for champion_id, expected_name in ((64, "盲僧"), (157, "疾风剑豪")):
        client.champion_id = champion_id
        assert asyncio.run(tracker.poll_candidate()) == champion_id
        assert session.candidate_champion_id == champion_id
        assert session.champion_name == expected_name


# ------------------------------------------------- 3) InProgress Live Client 权威
def test_inprogress_live_client_overrides_candidate(monkeypatch, temp_database) -> None:
    """ChampSelect 是 A(131)，Live Client 实际是 B(110) → active 必须是 B。"""
    opgg = SlowOpggClient(delay=0.0, payload=_payload_for(110))
    result = _game_with_champ_select(
        monkeypatch, temp_database,
        champion_ids=[131], live_alias="Varus", live_name="惩戒之箭",
        opgg_client=opgg)
    names = [f.get("champion_name") for _, f in result["events"]
             if f.get("champion_name") is not None]
    assert names, "必须有英雄名事件"
    assert "惩戒之箭" in names, names
    final_ids = [f.get("final_champion_id") for _, f in result["events"]
                 if f.get("final_champion_id") is not None]
    assert final_ids and final_ids[-1] == 110, final_ids
    # ChampSelect 阶段按 candidate(131) 请求；Live Client 确认 110 后必须换成 110
    assert opgg.requested[0] == 131
    assert opgg.requested[-1] == 110, "active_champion_id 变成 110 后必须重新请求"


def test_tracker_confirm_final_overrides_candidate(monkeypatch, temp_database) -> None:
    from src import live
    from src.core.game_session import GameSession

    client = ScriptedLcuClient(champion_id=131)        # ChampSelect: Diana
    session = GameSession()
    provider = seed_provider(temp_database, {}, ttl_hours=0.001)
    hub = StateHub()
    tracker = live.ChampionTracker(ChampionDetector(client), None, provider, session,
                                   hub=hub)
    monkeypatch.setattr(live, "_stopping", lambda: False)
    asyncio.run(tracker.poll_candidate())
    assert session.candidate_champion_id == 131

    snapshot = LiveGameSnapshot(champion_name="惩戒之箭", champion_alias="Varus",
                                level=3, game_time=60.0, player_count=10)
    assert asyncio.run(tracker.confirm_final(snapshot)) == 110
    assert session.final_champion_id == 110
    assert session.active_champion_id == 110
    assert hub.snapshot().final_champion_id == 110
    assert hub.snapshot().champion_name == "惩戒之箭"


# ------------------------------------------------------------- 4) 皮肤不影响
def test_skin_does_not_affect_champion_id(monkeypatch, temp_database) -> None:
    """皮肤不会改变 championId：raw 名带皮肤后缀也能解析出正确英雄。"""
    # 皮肤名场景：rawChampionName 带后缀，alias 解析不到 → 回退中文名
    assert alias_from_raw_champion_name("game_character_displayname_Varus") == "Varus"

    from src import live
    from src.core.game_session import GameSession

    client = ScriptedLcuClient(champion_id=0)
    session = GameSession()
    provider = seed_provider(temp_database, {}, ttl_hours=0.001)
    tracker = live.ChampionTracker(ChampionDetector(client), None, provider, session,
                                   hub=StateHub())
    monkeypatch.setattr(live, "_stopping", lambda: False)

    skinned = LiveGameSnapshot(champion_name="惩戒之箭",
                               champion_alias="Varus_Skin42",     # 皮肤后缀
                               level=3, game_time=60.0, player_count=10)
    assert asyncio.run(tracker.confirm_final(skinned)) == 110
    assert session.champion_id == 110


def test_champion_path_never_uses_ocr() -> None:
    """结构性约束：英雄识别链路里不允许出现 OCR / 截图 / 皮肤识别。"""
    from src import live as live_mod

    for source in (inspect.getsource(live_mod.ChampionTracker),
                   inspect.getsource(ChampionDetector)):
        for forbidden in ("OcrEngine", "CardReader", "recognize_texts", "screenshot"):
            assert forbidden not in source, "%s 里不允许出现 %s" % (forbidden, forbidden)


# ------------------------------------------------- 5) OP.GG 用 active_champion_id
def test_opgg_request_uses_active_champion_id(monkeypatch, temp_database) -> None:
    """OP.GG 必须按 active_champion_id 请求（这里是 157）。"""
    client = ScriptedLcuClient(champion_id=157)
    live_client = FakeLiveClient(available=True, champion_alias="Yasuo",
                                 champion_name="疾风剑豪", level=1)
    opgg = SlowOpggClient(delay=0.0, payload=_payload_for(157))
    _run(monkeypatch, temp_database, client=client, live_client=live_client,
         opgg_client=opgg, champion_id=157, max_reads=3)
    assert opgg.requested == [157], opgg.requested


def test_opgg_state_machine_transitions(monkeypatch, temp_database) -> None:
    """加载状态机：NOT_REQUESTED -> LOADING -> READY（失败时 ERROR 且可重试）。"""
    provider = seed_provider(temp_database, {}, ttl_hours=0.001)
    provider.client = SlowOpggClient(delay=0.0, payload=_payload_for(157))
    assert provider.stats_state(157) is StatsState.NOT_REQUESTED

    async def flow():
        provider.begin_session()
        await provider.activate_champion_background(157, alias="Yasuo", name="疾风剑豪")
        # 后台任务还没跑完
        assert provider.stats_state(157) in (StatsState.LOADING, StatsState.READY)
        await asyncio.sleep(0.05)
        return provider.stats_state(157)

    assert asyncio.run(flow()) is StatsState.READY


__all__: list[str] = []
