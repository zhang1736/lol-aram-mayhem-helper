"""四个实战 Bug 的回归测试：换英雄 / 对局结束清 UI / 多局数据隔离 / Overlay 下移。

真机日志证据（2026-09-21 正式版，LOCALAPPDATA/LOLHexHelper/logs/app.log）：

    22:55:00 ~ 22:55:10  选人阶段只读了一次 champ-select，拿到 championId=4 就永久写入
    23:13:52 ~ 23:14:01  InProgress -> Reconnect -> Unknown -> Lobby（从未出现 None）
    23:14:01 ~ 23:15:52  Lobby -> ChampSelect -> InProgress（下一局，但循环没退出）
    23:16:01 ~ 23:18:33  stage 回退被忽略: 4 -> 1（上一局的 Session 被下一局继续使用）

也就是说：游戏循环只在 phase == "None" 时才收尾，而真实客户端根本不给 "None"，
导致 Session / 英雄 / OP.GG 数据全部跨局泄漏。
"""

from __future__ import annotations

import asyncio
import logging

import pytest

from src.core.state_hub import HUB, StateHub
from src.lcu.gameflow import GAME_END_PHASES, IN_GAME_PHASES, GamePhase
from tests.session_harness import (
    DEFAULT_SEED,
    VARUS_OFFERS,
    FakeCardReader,
    FakeLiveClient,
    ScriptedLcuClient,
    install_fakes,
    make_args,
    seed_provider,
)

#: 戴安娜（上一局英雄）在 SQLite 里的"串数据标记"
DIANA_MARKERS = (9.99,)


@pytest.fixture()
def isolated_home(monkeypatch, tmp_path):
    monkeypatch.setenv("LOL_HEX_HELPER_HOME", str(tmp_path))
    return tmp_path


class PhaseDriver:
    """真机相位序列：ChampSelect -> InProgress -> (结算) Lobby。"""

    def __init__(self, champ_select_limit: int = 4) -> None:
        self.champ_select_limit = champ_select_limit
        self.override: str | None = None
        self.hook = None          # Callable[[int], None]，按读取次数做副作用

    def phase_for(self, n: int) -> str:
        if self.hook is not None:
            self.hook(n)
        if self.override is not None:
            return self.override
        return "ChampSelect" if n <= self.champ_select_limit else "InProgress"


def _read_all(caplog, level=logging.INFO) -> str:
    return "\n".join(record.getMessage() for record in caplog.records)


# --------------------------------------------------------------- Bug 1
def test_champion_changes_before_game_start(monkeypatch, caplog, temp_database,
                                            isolated_home) -> None:
    """ChampSelect 换英雄：必须持续轮询候选英雄，并切到新英雄的 OP.GG 数据。"""
    from src import live

    caplog.set_level(logging.INFO)
    provider = seed_provider(temp_database)
    driver = PhaseDriver(champ_select_limit=4)
    client = ScriptedLcuClient(phase_for=driver.phase_for, champion_id=131)
    live_client = FakeLiveClient()

    # 玩家在选人期间把英雄从戴安娜(131) 换成 韦鲁斯(110)
    def hook(n: int) -> None:
        if n == 3:
            client.champion_id = 110
        if n == 5:
            live_client.available = True
            live_client.champion_alias = "Varus"
            live_client.champion_name = "惩戒之箭"
            live_client.level = 3

    driver.hook = hook
    snapshots: list = []

    def on_read(call_index: int) -> None:
        snapshots.append(HUB.snapshot())
        if call_index >= 2:
            driver.override = "Lobby"      # 一局结束

    installed = install_fakes(monkeypatch, client=client, live=live_client,
                              reader=FakeCardReader(on_read=on_read))
    outcome = asyncio.run(live.play_one_game(
        live.Settings(), make_args(), client, provider, live.HubOverlay()))

    assert outcome == "game_end"
    # 1) 选人会话必须被持续读取（旧代码只读一次）
    assert client.champ_select_reads >= 3
    # 2) 主界面最终显示换过之后的英雄
    assert snapshots, "对局中应当有界面快照"
    assert snapshots[-1].champion_id == 110
    assert snapshots[-1].champion_name == "惩戒之箭"
    # 3) 海克斯统计必须来自新英雄（9.99 = 戴安娜的串数据标记）
    populars = [offer.get("popular") for snap in snapshots for offer in snap.offers]
    assert 1.93 in populars
    assert not any(mark in populars for mark in DIANA_MARKERS)
    log = _read_all(caplog)
    assert "候选英雄变化" in log
    assert "QueryStats champion=Varus(110)" in log
    assert "QueryStats champion=Diana(131) augment=1325" not in log


def test_candidate_champion_updates(monkeypatch, temp_database, isolated_home) -> None:
    """候选英雄字段必须随选人变化，并向 HUB / Provider 同步。"""
    from src import live
    from src.core.game_session import GameSession
    from src.lcu.champion import ChampionDetector

    provider = seed_provider(temp_database)
    client = ScriptedLcuClient(champion_id=131)
    session = GameSession()
    hub = StateHub()
    tracker = live.ChampionTracker(ChampionDetector(client), None, provider,
                                  session, hub=hub)
    monkeypatch.setattr(live, "_stopping", lambda: False)

    assert asyncio.run(tracker.poll_candidate()) == 131
    assert session.candidate_champion_id == 131
    assert session.champion_name == "皎月女神"
    assert hub.snapshot().candidate_champion_id == 131
    assert hub.snapshot().final_champion_id is None
    assert provider.active_champion_id == 131

    client.champion_id = 110                     # 换英雄
    assert asyncio.run(tracker.poll_candidate()) == 110
    assert session.candidate_champion_id == 110
    assert session.champion_id == 110
    assert session.active_champion_id == 110
    assert hub.snapshot().champion_name == "惩戒之箭"
    assert provider.active_champion_id == 110
    assert 2064 not in provider.active_opgg_stats      # 旧英雄数据引用已丢弃
    assert 1115 in provider.active_opgg_stats          # 新英雄已重新预取

    # 再次读取同一个英雄：不重复预取（幂等）
    assert asyncio.run(tracker.poll_candidate()) == 110
    assert session.candidate_champion_id == 110


# --------------------------------------------------------------- Bug 2
def test_game_end_clears_ui_state(monkeypatch, caplog, temp_database,
                                  isolated_home) -> None:
    """对局结束后主界面必须回到「等待游戏 / - / - / -」。"""
    from src import live
    from src.data.repository import AugmentStatsRepository
    from src.ui import texts

    caplog.set_level(logging.INFO)
    HUB.update(client_connected=True)
    provider = seed_provider(temp_database)
    driver = PhaseDriver(champ_select_limit=2)
    client = ScriptedLcuClient(phase_for=driver.phase_for, champion_id=131)
    live_client = FakeLiveClient(available=True, champion_alias="Diana",
                                 champion_name="皎月女神", level=15)
    driver.hook = None
    installed = install_fakes(monkeypatch, client=client, live=live_client,
                              reader=FakeCardReader(script=lambda n: (2064, 1116, 1180),
                                                    on_read=lambda i: setattr(
                                                        driver, "override",
                                                        "Unknown" if i < 2 else "Lobby")))

    outcome = asyncio.run(live.play_one_game(
        live.Settings(), make_args(), client, provider, live.HubOverlay()))
    assert outcome == "game_end"

    snap = HUB.snapshot()
    assert snap.gameflow == "None"
    assert texts.gameflow_text(snap.gameflow) == "等待游戏"
    assert snap.champion_id is None
    assert snap.champion_name is None
    assert snap.candidate_champion_id is None
    assert snap.final_champion_id is None
    assert snap.stage is None and snap.level is None
    assert snap.offer_version == 0
    assert snap.offers == []
    assert snap.changed_slots == [] and snap.rerolled_slots == []
    assert snap.recommended == []
    assert snap.data_patch is None
    assert snap.overlay_visible is False
    assert texts.hero_text(snap.champion_name, snap.champion_alias) == "-"
    assert texts.stage_text(snap.stage, snap.level) == "-"
    assert texts.patch_text(snap.data_patch) == "-"
    assert texts.overlay_status_text(snap.client_connected, bool(snap.offers),
                                     snap.overlay_visible) == "○ 等待海克斯"
    # 清 Session ≠ 清 Cache
    repository = AugmentStatsRepository(temp_database)
    assert repository.count() > 0
    assert provider.get_cached(131, 2064) is not None
    assert provider.active_opgg_stats == {}
    assert provider.active_champion_id is None


def test_real_client_phase_sequence_is_recognized_as_game_end() -> None:
    """实测序列 InProgress -> Reconnect -> Unknown -> Lobby 必须收尾。"""
    for raw in ("None", "Lobby", "Matchmaking", "ReadyCheck",
                "PreEndOfGame", "EndOfGame", "WaitingForStats"):
        assert GamePhase.from_raw(raw).is_game_end is True, raw
    for raw in ("GameStart", "InProgress", "Reconnect"):
        assert GamePhase.from_raw(raw).in_game is True, raw
    assert GamePhase.from_raw("Unknown").is_game_end is False
    assert GamePhase.IN_PROGRESS in IN_GAME_PHASES
    assert GamePhase.LOBBY in GAME_END_PHASES


# --------------------------------------------------------------- Bug 3
def test_new_game_does_not_reuse_previous_champion_snapshot(monkeypatch, caplog,
                                                            temp_database,
                                                            isolated_home) -> None:
    """Game1 戴安娜 -> Game2 韦鲁斯：第二局所有查询都必须用 110。"""
    from src import live

    caplog.set_level(logging.INFO)
    provider = seed_provider(temp_database)
    settings = live.Settings()

    # ---- Game 1: 戴安娜(131) ----
    game1_driver = PhaseDriver(champ_select_limit=2)
    game1_client = ScriptedLcuClient(phase_for=game1_driver.phase_for, champion_id=131)
    game1_live = FakeLiveClient(available=True, champion_alias="Diana",
                                champion_name="皎月女神", level=3)
    install_fakes(monkeypatch, client=game1_client, live=game1_live,
                  reader=FakeCardReader(script=lambda n: (2064, 1116, 1180),
                                        on_read=lambda i: setattr(
                                            game1_driver, "override",
                                            "Unknown" if i < 2 else "Lobby")))
    asyncio.run(live.play_one_game(settings, make_args(), game1_client, provider,
                                   live.HubOverlay()))
    assert "QueryStats champion=TwistedFate" not in _read_all(caplog)
    assert "QueryStats champion=Diana(131)" in _read_all(caplog)

    # ---- Game 2: 韦鲁斯(110) ----
    caplog.clear()
    game2_driver = PhaseDriver(champ_select_limit=2)
    game2_client = ScriptedLcuClient(phase_for=game2_driver.phase_for, champion_id=110)
    game2_live = FakeLiveClient(available=True, champion_alias="Varus",
                                champion_name="惩戒之箭", level=3)
    snapshots: list = []

    def on_read(call_index: int) -> None:
        snapshots.append(HUB.snapshot())
        if call_index >= 2:
            game2_driver.override = "Lobby"

    install_fakes(monkeypatch, client=game2_client, live=game2_live,
                  reader=FakeCardReader(script=lambda n: VARUS_OFFERS,
                                        on_read=on_read))
    asyncio.run(live.play_one_game(settings, make_args(), game2_client, provider,
                                   live.HubOverlay()))

    log = _read_all(caplog)
    assert "QueryStats champion=Varus(110)" in log
    assert "champion=Diana(131)" not in log, "第二局不得再查上一局英雄"
    populars = [offer.get("popular") for snap in snapshots for offer in snap.offers]
    assert 1.93 in populars                       # 韦鲁斯的数值
    assert not any(mark in populars for mark in DIANA_MARKERS)
    assert provider.active_champion_id is None    # 对局结束后 Session 已清空
    # SQLite 里上一局英雄的历史缓存必须保留
    assert provider.get_cached(131, 2064) is not None


def test_opgg_cache_isolated_by_champion(temp_database, isolated_home,
                                        caplog) -> None:
    """同一个 Provider 内，跨英雄查询必须被拒绝（不串数据）。"""
    from src.opgg.provider import OpggProvider

    caplog.set_level(logging.WARNING)
    provider = seed_provider(temp_database)
    assert isinstance(provider, OpggProvider)

    provider.begin_session()
    asyncio.run(provider.activate_champion(131, alias="Diana", name="皎月女神"))
    assert provider.query_augment_stats(131, 2064).stats.popular == 1.54
    assert provider.query_augment_stats(110, 1115).stats is None      # 跨英雄 -> 拒绝
    assert "拒绝跨英雄查询" in _read_all(caplog)

    # 换英雄：旧英雄的数据引用立刻丢弃，但 SQLite 历史缓存保留
    asyncio.run(provider.activate_champion(110, alias="Varus", name="惩戒之箭"))
    assert provider.active_champion_id == 110
    assert provider.query_augment_stats(131, 2064).stats is None      # 旧英雄不可再读
    assert provider.query_augment_stats(110, 1115).stats.popular == 1.93

    # 结束对局：Session 清空，但 SQLite / 内存历史缓存必须保留
    provider.end_session()
    assert provider.get_cached(131, 2064) is not None                 # 缓存没被删

    # 新一局：还没激活英雄时，任何"只按海克斯查"的读取都必须被拦住
    provider.begin_session()
    assert provider.active_champion_id is None
    assert provider.active_opgg_stats == {}
    assert provider.query_augment_stats(110, 1115).stats is None


def test_opgg_query_uses_active_champion(temp_database, isolated_home,
                                        caplog) -> None:
    """查询日志必须写清 champion=名字(Id) / CacheStatus / popular。"""
    caplog.set_level(logging.INFO)
    provider = seed_provider(temp_database)
    provider.begin_session()
    asyncio.run(provider.activate_champion(131, alias="Diana", name="皎月女神"))
    caplog.clear()

    result = provider.query_augment_stats(131, 2064)
    log = _read_all(caplog)
    assert result.stats is not None
    assert "QueryStats champion=Diana(131) augment=2064" in log
    assert "CacheStatus=FRESH" in log
    assert "popular=1.54" in log


# --------------------------------------------------------------- Bug 4
def test_overlay_default_position_moved_down() -> None:
    """Overlay 默认整体下移 60px（2560x1440: 710 -> 770），横向不动。"""
    from src.overlay.geometry import CARD_GAP_Y, overlay_card_positions
    from src.ocr.roi import build_roi_set

    assert CARD_GAP_Y == 70
    base = overlay_card_positions(2560, 1440)
    roi = build_roi_set(2560, 1440)
    assert [p[1] for p in base] == [770, 770, 770]
    assert [p[0] for p in base] == [roi.card_box(i).left for i in range(3)]
    assert base[0][1] - roi.card_box(0).bottom == 70

    # 1080p / 4K 用 scale_y 适配（不是硬编码 770）
    for width, height in ((1920, 1080), (3840, 2160)):
        positions = overlay_card_positions(width, height)
        current = build_roi_set(width, height)
        gap = positions[0][1] - current.card_box(0).bottom
        assert gap == round(CARD_GAP_Y * current.scale_y)
        assert positions[0][1] > current.card_box(0).bottom

    # 用户手动微调仍然生效（叠加在默认位置之上）
    shifted = overlay_card_positions(2560, 1440, offset_y=-30, offset_x=12)
    assert shifted[0][1] == 740
    assert shifted[0][0] == base[0][0] + 12


__all__: list[str] = []
