"""复现 / 验证四个实战 Bug 的 Session 隔离（离线，不依赖 LCU 与屏幕）。

    & ".venv\\Scripts\\python.exe" tools\\repro_session_bugs.py

真机日志证据（正式版 %LOCALAPPDATA%/LOLHexHelper/logs/app.log，2026-09-21）：
    22:55:00 ~ 22:55:10  选人阶段只读了一次 champ-select，championId=4 之后永久写入
    23:13:52 ~ 23:14:01  InProgress -> Reconnect -> Unknown -> Lobby（从未出现 None）
    23:14:01 ~ 23:15:52  Lobby -> ChampSelect -> InProgress（循环没退出，Session 泄漏）
    23:16:01 ~ 23:18:33  stage 回退被忽略: 4 -> 1（下一局继续用上一局的 Session）

本脚本用假 LCU / 假 Live Client / 假 OCR 驱动同一套相位序列，打印：
    * 选人会话被读了几次（换英雄是否被跟踪）
    * 每个海克斯查询用的是哪个英雄（是否串到上一局）
    * 对局结束后主界面状态（英雄 / 阶段 / Patch / 悬浮窗）
    * Overlay 默认位置（2560x1440 是否 770）
"""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.core.state_hub import HUB                      # noqa: E402
from src.data.database import Database                  # noqa: E402
from src.live import (                                  # noqa: E402
    ChampionTracker,
    HubOverlay,
    play_one_game,
    reset_hub_session_state,
)
from src.lcu.champion import ChampionDetector           # noqa: E402
from src.overlay.geometry import overlay_card_positions  # noqa: E402
from src.overlay.widgets import format_popular_line      # noqa: E402
from src.ui import texts                                 # noqa: E402
from src.utils.logger import force_utf8_stdio, setup_logging  # noqa: E402
from tests.session_harness import (                      # noqa: E402
    FakeCardReader,
    FakeLiveClient,
    ScriptedLcuClient,
    install_fakes,
    make_args,
    seed_provider,
)

QUERIES: list[tuple[int, int]] = []


class _Spy:
    """记录每次海克斯查询用的 championId（用于检查是否串数据）。"""

    def __init__(self, monkeypatch, provider) -> None:
        self.provider = provider
        original = provider.query_augment_stats

        def spy(champion_id, augment_id):
            QUERIES.append((champion_id, augment_id))
            return original(champion_id, augment_id)

        monkeypatch.setattr(provider, "query_augment_stats", spy)


class PhaseDriver:
    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.override = None
        self.hook = None

    def phase_for(self, n: int) -> str:
        if self.hook:
            self.hook(n)
        if self.override:
            return self.override
        return "ChampSelect" if n <= self.limit else "InProgress"


class _MonkeyPatch:
    """极简 monkeypatch（脱离 pytest 也能跑）。"""

    def __init__(self) -> None:
        self._undo: list = []

    def setattr(self, target, name, value) -> None:
        self._undo.append((target, name, getattr(target, name)))
        setattr(target, name, value)

    def undo(self) -> None:
        for target, name, value in reversed(self._undo):
            setattr(target, name, value)


def show(title: str) -> None:
    print("")
    print("=" * 72)
    print(title)
    print("=" * 72)


def main() -> int:
    force_utf8_stdio()
    setup_logging("logs/repro.log", logging.INFO, console=False)
    patch = _MonkeyPatch()
    database = Database(PROJECT_ROOT / "data" / "_repro_session.db")
    if database.path.exists():
        database.path.unlink()
    database.initialize()
    settings = None
    try:
        provider = seed_provider(database)
        provider.begin_session()
        _Spy(patch, provider)
        driver = PhaseDriver(limit=4)
        client = ScriptedLcuClient(phase_for=driver.phase_for, champion_id=131)
        live_client = FakeLiveClient()

        # 玩家在选人期间把英雄从 戴安娜(131) 换成 韦鲁斯(110)
        def hook(n: int) -> None:
            if n == 3:
                client.champion_id = 110
            if n == 5:
                live_client.available = True
                live_client.champion_alias = "Varus"
                live_client.champion_name = "惩戒之箭"
                live_client.level = 3

        driver.hook = hook
        snaps: list = []
        reader = FakeCardReader(on_read=lambda n: (
            snaps.append(HUB.snapshot()),
            setattr(driver, "override", "Lobby") if n >= 2 else None))
        install_fakes(patch, client=client, live=live_client, reader=reader)

        from src.config import load_settings

        settings = load_settings()
        reset_hub_session_state()
        outcome = asyncio.run(play_one_game(settings, make_args(), client, provider,
                                            HubOverlay()))

        show("Bug 1 / Bug 3：ChampSelect 换英雄（131 -> 110）")
        print("选人会话读取次数        : %d（旧实现只有 1 次）" % client.champ_select_reads)
        print("对局中主界面英雄        : %s / %s"
              % (snaps[-1].champion_name, snaps[-1].champion_id) if snaps else "无快照")
        print("海克斯查询使用的英雄    : %s"
              % sorted({cid for cid, _ in QUERIES}))
        print("三个数值                : %s"
              % ", ".join(format_popular_line(o.get("popular"))
                          for o in (snaps[-1].offers if snaps else [])))
        print("是否出现上一局英雄数据  : %s"
              % ("是（BUG）" if any(cid == 131 for cid, _ in QUERIES) else "否"))

        show("Bug 2：对局结束后的主界面（Game End -> Lobby）")
        snap = HUB.snapshot()
        print("结果                    : %s" % outcome)
        print("LOL Client              : %s" % ("● 已连接" if snap.client_connected else "○ 未连接"))
        print("游戏状态                : %s" % texts.gameflow_text(snap.gameflow))
        print("当前英雄                : %s" % texts.hero_text(snap.champion_name,
                                                              snap.champion_alias))
        print("海克斯阶段              : %s" % texts.stage_text(snap.stage, snap.level))
        print("Patch                   : %s" % texts.patch_text(snap.data_patch))
        print("悬浮窗状态              : %s" % texts.overlay_status_text(
            True, bool(snap.offers), snap.overlay_visible))
        print("Session 已清 / 缓存保留 : %s / %s"
              % (snap.champion_id is None and snap.stage is None,
                 provider.get_cached(131, 2064) is not None))

        show("Bug 4：Overlay 默认位置")
        for width, height in ((1920, 1080), (2560, 1440), (3840, 2160)):
            positions = overlay_card_positions(width, height)
            print("  %4dx%-5d -> y = %s（横向 = %s）"
                  % (width, height, positions[0][1],
                     [p[0] for p in positions]))
        return 0
    finally:
        patch.undo()
        database.close()
        for suffix in ("", "-wal", "-shm"):
            leftover = database.path.with_name(database.path.name + suffix)
            if leftover.exists():
                leftover.unlink()


if __name__ == "__main__":
    raise SystemExit(main())
