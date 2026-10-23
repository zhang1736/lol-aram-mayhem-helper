"""多局常驻生命周期回归测试（本次任务的核心）。

真机历史失效模式：
    * `GameflowMonitor.get_phase()` 把连接错误伪装成 UNKNOWN
    * `wait_for_game()` 只在超时后返回 False（默认 180 分钟）
    * `resident()` 内层循环复用同一个 LcuClient，且从不重新发现凭据
  => 客户端重启（端口/token 变化）后，助手静默空等，表现成"某局之后突然失效"。
"""

from __future__ import annotations

import asyncio
import os
import time

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from src.config import load_settings  # noqa: E402
from src.core.state_hub import HUB  # noqa: E402,F401
from src.lcu.gameflow import GamePhase  # noqa: E402
from src.opgg.client import OpggError  # noqa: E402
from src.opgg.provider import OpggProvider, StatsState  # noqa: E402
from tests.replay_harness import (  # noqa: E402
    FakeClock,
    ReplayScreen,
    real_ocr_engine,  # noqa: F401 - pytest fixture
    run_replay,
)
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


class FakeClient:
    """轻量 LCU 客户端（resident 只用到 credentials/aclose）。"""

    def __init__(self, port: int) -> None:
        self.credentials = type("C", (), {"port": port})()
        self.closed = False

    async def aclose(self) -> None:
        self.closed = True


class BrokenLcuClient:
    """所有请求都抛 ConnectError（模拟客户端退出/端口失效）。"""

    def __init__(self) -> None:
        self.calls = 0

    async def get(self, path):  # noqa: ARG002
        self.calls += 1
        raise ConnectionResetError(10054, "WinError 10054")

    async def get_json(self, path):  # noqa: ARG002
        raise ConnectionResetError(10054, "WinError 10054")

    async def aclose(self) -> None:
        pass


def _settings():
    settings = load_settings()
    settings.lcu.reconnect_interval = 0.01
    return settings


# ---------------------------------------------------------------- 1) 等待链路
def test_wait_for_game_none_then_champselect() -> None:
    """启动时 None → 等待 → ChampSelect：必须返回 ready。"""
    from src import live

    state = {"phase": "None"}

    def phase_for(n: int) -> str:
        if n >= 2:
            state["phase"] = "ChampSelect"
        return state["phase"]

    client = ScriptedLcuClient(phase_for=phase_for)
    outcome = asyncio.run(live.wait_for_game(client, timeout_minutes=0.05))
    assert outcome == "ready"


def test_wait_for_game_disconnects_after_repeated_failures() -> None:
    """LCU 连续失败 → 必须返回 disconnected（不能静默等 180 分钟）。"""
    from src import live

    client = BrokenLcuClient()
    started = time.perf_counter()
    outcome = asyncio.run(live.wait_for_game(client, timeout_minutes=5,
                                             max_failures=3))
    assert outcome == "disconnected"
    assert time.perf_counter() - started < 5, "必须尽快判定断线"


def test_game_end_phase_classification() -> None:
    """InProgress/Unknown 不是结束；None/EndOfGame 才是结束。"""
    assert GamePhase.from_raw("InProgress").is_game_end is False
    assert GamePhase.from_raw("Unknown").is_game_end is False
    assert GamePhase.from_raw("None").is_game_end is True
    assert GamePhase.from_raw("EndOfGame").is_game_end is True


# --------------------------------------------------- 2) 常驻连打多局（脚本化）
def _run_resident(monkeypatch, outcomes, *, stop_after=3, reconnect_plan=()):
    """驱动 resident()：用脚本化 play_one_game 观察生命周期。"""
    from src import live

    settings = _settings()
    args = make_args(resident=True)
    control = live.Control()
    calls: list = []
    active = {"max": 0, "now": 0}
    clients: list = []

    def fake_discover(_settings):
        client = FakeClient(4695 + len(clients))
        clients.append(client)
        return client, True

    async def fake_play(settings, args, client, provider, overlay):  # noqa: ARG001
        active["now"] += 1
        active["max"] = max(active["max"], active["now"])
        calls.append(client.credentials.port)
        try:
            await asyncio.sleep(0)
            if len(calls) <= len(reconnect_plan) and reconnect_plan[len(calls) - 1]:
                control.reconnect.set()
            if len(calls) >= stop_after:
                control.stop.set()
            return outcomes[min(len(calls) - 1, len(outcomes) - 1)]
        finally:
            active["now"] -= 1

    monkeypatch.setattr(live, "discover_and_connect", fake_discover)
    monkeypatch.setattr(live, "play_one_game", fake_play)
    asyncio.run(live.resident(settings, args, control=control))
    return {"calls": calls, "clients": clients, "active": active,
            "control": control}


def test_resident_handles_three_games(monkeypatch) -> None:
    """同一进程连打三局：play_one_game 调用 3 次，常驻不退出。"""
    result = _run_resident(monkeypatch, ["game_end"] * 3, stop_after=3)
    assert len(result["calls"]) == 3
    assert result["active"]["max"] == 1, "对局循环不允许并行/重复"


def test_resident_survives_unknown_and_none(monkeypatch) -> None:
    """某局返回 game_end/no_game 后必须继续下一局。"""
    result = _run_resident(monkeypatch, ["no_game", "game_end", "game_end"],
                           stop_after=3)
    assert len(result["calls"]) == 3


def test_resident_reconnects_with_new_port(monkeypatch) -> None:
    """第一局判定 disconnected → 重新发现客户端（新端口）→ 第二局继续。"""
    result = _run_resident(monkeypatch, ["disconnected", "game_end"],
                           stop_after=2)
    assert result["calls"] == [4695, 4696], result["calls"]
    assert result["clients"][0].closed is True, "旧连接必须关闭"
    assert result["clients"][1].closed is False


def test_resident_manual_reconnect_closes_old_client(monkeypatch) -> None:
    """手动重连：关闭旧 client、重新发现；不会并行跑两个对局循环。"""
    result = _run_resident(monkeypatch, ["no_game", "no_game", "game_end"],
                           stop_after=3, reconnect_plan=(False, True, False))
    assert len(result["calls"]) == 3
    assert result["active"]["max"] == 1
    assert result["clients"][0].closed is True


def test_overlay_hide_show_does_not_stop_monitoring(monkeypatch) -> None:
    """用户隐藏/显示浮层不能影响常驻监控（仍要继续跑下一局）。"""
    from src import live

    settings = _settings()
    args = make_args(resident=True)
    control = live.Control()
    calls = {"n": 0}

    def fake_discover(_settings):
        return FakeClient(4700), True

    async def fake_play(settings, args, client, provider, overlay):  # noqa: ARG001
        calls["n"] += 1
        HUB.update(overlay_visible=False, offers=[])
        HUB.update(overlay_visible=True)
        if calls["n"] >= 3:
            control.stop.set()
        return "game_end"

    monkeypatch.setattr(live, "discover_and_connect", fake_discover)
    monkeypatch.setattr(live, "play_one_game", fake_play)
    asyncio.run(live.resident(settings, args, control=control))
    assert calls["n"] == 3


# --------------------------------------------- 3) 旧会话结果不能污染新会话
class SlowOpggClient:
    def __init__(self, delay: float, payloads: dict) -> None:
        self.delay = delay
        self.payloads = payloads

    async def get_champion_augments(self, champion_id):
        await asyncio.sleep(self.delay)
        return self.payloads[champion_id]

    async def get_champion_stats(self, *args, **kwargs):  # noqa: ARG002
        raise OpggError("hero stats 不需要")

    async def aclose(self) -> None:
        pass


def _payload(augment_id: int, performance: float, popular: float) -> dict:
    return {"data": [{"id": augment_id, "tier": 1,
                      "performance": performance, "popular": popular}]}


def test_late_previous_session_result_is_discarded() -> None:
    """第 1 局（131）的慢查询在第 2 局（110）开始后才返回 → 必须丢弃。"""
    #: 两个英雄的数值故意不同，便于验证"旧局结果没有覆盖新局"
    slow = SlowOpggClient(0.2, {
        131: _payload(1325, 11.11, 9.99),
        110: _payload(1325, 70.88, 0.49),
    })
    provider = OpggProvider(client=slow)

    async def flow():
        provider.begin_session(1)
        await provider.activate_champion_background(131, alias="Diana")
        await asyncio.sleep(0)                 # 让慢任务启动
        provider.begin_session(2)              # 第 2 局开始
        await provider.activate_champion_background(110, alias="Varus")
        await asyncio.sleep(0.35)              # 等第 1 局的结果回来
        return provider

    asyncio.run(flow())
    assert provider.session_id == 2
    assert provider.active_champion_id == 110
    # 第 1 局的 131 数据不能变成"当前英雄"的数据
    assert provider.query_augment_stats(131, 1325).stats is None
    #: 当前 Session 的数据必须是 110 的（70.88/0.49），不能是 131 晚到的 11.11/9.99
    stats = provider.active_opgg_stats.get(1325)
    assert stats is not None
    assert (stats.performance, stats.popular) == (70.88, 0.49)


def test_session_state_machine_states() -> None:
    """数据状态机：NOT_REQUESTED → READY；内部状态只用于 Debug/日志。"""
    provider = OpggProvider(client=SlowOpggClient(0.0, {
        110: _payload(1325, 70.0, 1.0), 131: _payload(1325, 1.0, 1.0)}))
    assert provider.stats_state(110) is StatsState.NOT_REQUESTED

    async def flow():
        provider.begin_session(1)
        await provider.activate_champion_background(110, alias="Varus")
        await asyncio.sleep(0.05)
        return provider.stats_state(110)

    assert asyncio.run(flow()) is StatsState.READY


# ------------------------------------------- 4) 新 stage 强制全量识别
def test_reset_slots_forces_full_ocr(monkeypatch, temp_database,
                                     real_ocr_engine) -> None:
    """reset_slots() 后必须对三个槽重新 OCR（新局/新 stage 用）。"""
    screen = ReplayScreen()
    clock = FakeClock()
    result = run_replay(monkeypatch, temp_database, real_ocr_engine, screen,
                        on_call=lambda i, s, c: c.advance(0.30),
                        clock=clock, max_reads=4)
    reader = result["reader"]
    engine = result["engine"]
    before = engine.per_slot()
    reader.reset_slots("test")
    reader.read_once()
    after = engine.per_slot()
    assert after[0] > before[0] and after[1] > before[1] and after[2] > before[2]
