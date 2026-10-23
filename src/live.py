"""实机闭环与常驻模式。

Day 5 目标：多局连续运行、LCU 掉线自动重连、任何后台任务异常都不退出主程序。
"""

from __future__ import annotations

import asyncio
import itertools
import sys
import threading
import time
from typing import Optional

from src.config import Settings
from src.core.state_hub import HUB
from src.core.augment_flow import (
    AugmentFlow,
    AugmentFlowState,
    recommendation_reason,
    recommended_augment_ids,
)
from src.core.game_session import GameSession
from src.data.cache import CacheStatus
from src.lcu.champion import ChampionDetector
from src.lcu.client import LcuClient
from src.lcu.credential import LcuCredentialProvider
from src.lcu.gameflow import (
    GAME_END_PHASES,
    IN_GAME_PHASES,
    GamePhase,
    GameflowMonitor,
)
from src.lcu.live_client import LiveClientDataSource
from src.overlay.manager import OverlayManager, OverlayUnavailableError, launch_overlay
from src.overlay.window import MISSING_PYSIDE_MESSAGE
from src.ocr.gate import WATCHDOG_INTERVAL, AugmentScanGate, ScanMode
from src.opgg.provider import OpggProvider
from src.utils.logger import get_logger, get_tagged

logger = get_logger(__name__)
client_log = get_tagged("client")
lcu_log = get_tagged("lcu")
gameflow_log = get_tagged("gameflow")
champion_log = get_tagged("champion")
ocr_log = get_tagged("ocr")
augment_log = get_tagged("augment")
flow_log = get_tagged("flow")
overlay_log = get_tagged("overlay")
perf_log = get_tagged("flow", "perf")

MISS_LIMIT = 3

#: 每局一个单调递增的 session id（防止上一局的异步结果污染新局）
_SESSION_SEQ = itertools.count(1)

#: 选人阶段轮询候选英雄的间隔（秒）；换英雄是玩家操作，秒级足够
CHAMP_SELECT_POLL_SECONDS = 1.0

#: Live Client 等级轮询间隔（秒）。0.5s 只发本机 HTTP 请求，开销很小；
#: 同时把"第一次选取后进入下一 stage"的最坏发现等待从 1s 降到 0.5s。
LEVEL_POLL_INTERVAL = 0.5

#: 某张卡长时间确认不出来时的兜底秒数（只在"本 stage 还没有可用快照"时生效）
SNAPSHOT_GIVEUP_SECONDS = 6.0

#: LCU 连续读取失败多少次判定为"连接已断"（需要重新发现端口/token）
CLIENT_MAX_FAILURES = 5

#: 判定"海克斯界面消失"还需要持续这么久（秒），避免单帧 OCR 失败就清空
MISS_SECONDS = 1.2

#: 已显示牌面后三槽同时消失多久，就先记为"轮次边界"。
#: 它早于 1.2 秒隐藏判定，用于极快的同等级下一轮不被误当成槽位刷新。
ROUND_GAP_CONFIRM_SECONDS = 0.25

#: 常驻主循环的停止信号（由 resident() 注入），供各层循环检查
_STOP: list = [None]


def _stopping() -> bool:
    event = _STOP[0]
    return bool(event is not None and event.is_set())


async def _sleep(seconds: float, slice_seconds: float = 0.2) -> None:
    """可中断睡眠：收到停止信号立即返回，避免 shutdown 等满整个轮询间隔。"""
    remaining = seconds
    while remaining > 0:
        if _stopping():
            return
        step = min(slice_seconds, remaining)
        await asyncio.sleep(step)
        remaining -= step


class Control:
    """GUI -> 引擎 的控制信号（线程安全）。"""

    def __init__(self) -> None:
        self.stop = threading.Event()
        self.reconnect = threading.Event()
        self.overlay_visible = True


def request_reconnect(control: "Control", hub=None) -> None:
    """GUI -> 引擎：请求重新发现 LeagueClientUx 并重建 LCU 连接。"""
    control.reconnect.set()
    (hub or HUB).update(status_message="正在重新连接 LOL...")
    client_log.info("用户请求重新连接 LOL")


class AugmentOfferLike:
    """识别结果 + OP.GG 统计，供推荐算法使用。"""

    __slots__ = ("augment_id", "name_cn", "performance", "popular")

    def __init__(self, reading, stats) -> None:
        self.augment_id = reading.augment_id
        self.name_cn = stats.name_cn if stats and stats.name_cn else reading.name_cn
        self.performance = stats.performance if stats else None
        self.popular = stats.popular if stats else None


def print_offers(payload: list[dict], update) -> None:
    """日志格式：Stage + OfferVersion + SlotRerolled + Offers。"""
    print("")
    print(update.describe())
    for index, item in enumerate(payload, 1):
        mark = "  ★ 推荐" if item["recommended"] else ""
        pop = "—" if item["popular"] is None else "%.2f%%" % item["popular"]
        perf = "—" if item["performance"] is None else "%.2f" % item["performance"]
        flag = " [后台待确认]" if item.get("pending") else ""
        print("  %d. %-14s 强度：%-8s 欢迎度：%-9s%s  [%s]%s"
              % (index, item["name_cn"] or "?", perf, pop, mark,
                 item.get("data_state", "READY"), flag))


def build_offer_payload(slot_offers, pending_slots=(),
                        stats_state=None) -> tuple[list[dict], list[int]]:
    """把三槽数据组装成 Overlay payload。

    正式 UI 只显示"已确认快照"：
        * pending 槽**沿用上一份快照的内容**（名称/强度/欢迎度/推荐都保持旧值），
          这样刷新期间界面不会闪、也不会出现"识别中"；
        * pending 只是内部标记（Debug 用），不改变任何 UI 文案；
        * popular/performance 为 None → 强度/欢迎度显示 "—"（OP.GG 正在请求/失败）。

    推荐始终基于**三张卡的完整数值**计算（= 上一份已确认快照），
    等新卡确认后由调用方整体替换，再重新计算（原子更新）。

    data_state（只用于 Debug/日志，UI 缺数据统一显示 "—"）：
        READY      有 performance/popular
        NO_STATS   请求成功，但该 augment 没有统计记录
        LOADING    OP.GG 正在请求
        ERROR      请求失败（无可用缓存）
        NOT_REQUESTED 还没有发起请求
    """
    pending = set(pending_slots)
    state = getattr(stats_state, "value", stats_state) or "MISSING"
    offers = [item for item in slot_offers if item is not None]
    recommended = recommended_augment_ids(offers)
    is_tie = len(recommended) > 1
    payload = [{
        "slot": index,
        "augment_id": item.augment_id,
        "name_cn": item.name_cn,
        "performance": item.performance,
        "popular": item.popular,
        "recommended": item.augment_id in recommended,
        "tie": is_tie,
        #: 内部调试标记：该槽后台仍在确认（不影响 UI 文案）
        "pending": index in pending,
        #: 数据状态：READY 有数值 / MISSING 正在请求或请求失败
        "data_state": ("READY" if item.popular is not None
                       else ("NO_STATS" if state == "READY" else state)),
    } for index, item in enumerate(slot_offers) if item is not None]
    return payload, recommended


def refresh_slot_stats(session, provider: OpggProvider, slot_offers) -> int:
    """OP.GG 数据到位后，用当前牌面的 augment_id 重新查一次本地表并写回数值。

    只动 performance/popular，**不动** augment_id、名称与识别状态（§H）。
    返回本次成功填上数值的槽数。
    """
    filled = 0
    for item in slot_offers:
        if item is None or item.augment_id is None:
            continue
        stats = provider.query_augment_stats(
            session.active_champion_id, int(item.augment_id)).stats
        item.performance = stats.performance if stats else None
        item.popular = stats.popular if stats else None
        if stats is not None and getattr(stats, "name_cn", None):
            item.name_cn = stats.name_cn
        if stats is not None and stats.popular is not None:
            filled += 1
    return filled


async def pump_qt() -> None:
    """让出事件循环。

    **不要在这里调用 QApplication.processEvents()**：引擎跑在后台线程，
    而 Qt 的 GUI 操作必须在主线程。实测在工作线程调用 Qt（processEvents /
    创建 QWidget）会让「托盘退出」时主线程卡在 app.quit() 里死锁。
    """
    await asyncio.sleep(0)


class HubOverlay:
    """引擎侧的"悬浮窗"出口：只写状态中枢，绝不碰 Qt。

    真正的窗口由 GUI 主线程创建并定时从 HUB 同步内容。
    """

    available = False

    def set_offers(self, offers) -> None:
        offers = list(offers or [])
        HUB.update(offers=offers, overlay_visible=bool(offers))

    def update_offers(self, offers, pending_slots=()) -> None:
        """单槽刷新 / 待更新时的统一入口（§11）：pending 槽标记后仍然显示。"""
        items = [dict(o) for o in (offers or [])]
        for slot in pending_slots or ():
            try:
                index = int(slot)
            except (TypeError, ValueError):
                continue
            if 0 <= index < len(items):
                items[index]["pending"] = True
        self.set_offers(items)

    def update(self, lcu_connected: bool = False, phase=None) -> None:  # noqa: ARG002
        pass

    def close(self) -> None:
        HUB.update(offers=[], overlay_visible=False)


def make_overlay(settings: Settings, enabled: bool = True) -> HubOverlay:
    return HubOverlay()


def publish_offers(overlay, payload: list[dict], pending_slots=(),
                   recommended=(), **extra) -> None:
    """把"已确认快照"一次性发布到 Overlay 与 HUB。

    GUI 形态下 overlay 就是 HubOverlay（只写 HUB），所以这里必须保证
    offers 与 recommended 在**同一条** HUB 更新里，避免出现
    "卡片已更新但推荐还是旧的"这种中间态。
    """
    if not isinstance(overlay, HubOverlay):
        overlay.update_offers(payload, pending_slots)
    extra.setdefault("offers_updated_at", time.perf_counter())
    HUB.update(offers=payload, overlay_visible=True,
               recommended=list(recommended), **extra)


# ---------------------------------------------------------------------------
# 连接
# ---------------------------------------------------------------------------

def discover_and_connect(settings: Settings):
    provider = LcuCredentialProvider(settings.lcu.process_name)
    credentials = provider.find_credentials()
    if credentials is None:
        return None, False
    client = LcuClient(credentials, timeout=settings.lcu.request_timeout)
    return client, client.connect()


async def wait_for_client(settings: Settings, timeout_minutes: float,
                          overlay: OverlayManager | None = None):
    """等待 LeagueClientUx（含掉线重连）。返回 LcuClient 或 None。"""
    deadline = time.monotonic() + timeout_minutes * 60
    announced = False
    while time.monotonic() < deadline:
        if _stopping():
            client_log.info("停止信号已设置，放弃等待客户端")
            return None
        client, connected = discover_and_connect(settings)
        if connected:
            client_log.info("LCU connected (port=%s)", client.credentials.port)
            credentials = client.credentials
            HUB.update(client_connected=True, lcu_port=credentials.port,
                       lcu_pid=getattr(credentials, "pid", None),
                       region=getattr(credentials, "region", ""),
                       locale=getattr(credentials, "locale", ""),
                       hero_scope=settings.hero_scope_label,
                       augment_scope=settings.augment_scope_label)
            return client
        if not announced:
            client_log.info("等待 LeagueClientUx 启动（最多 %.0f 分钟）...", timeout_minutes)
            announced = True
        if overlay is not None and overlay.available:
            overlay.update(False, "Waiting for client")
        HUB.update(client_connected=False)
        await _sleep(settings.lcu.reconnect_interval)
    client_log.warning("等待客户端超时")
    return None


# ---------------------------------------------------------------------------
# 英雄
# ---------------------------------------------------------------------------

class ChampionTracker:
    """Session 内的英雄跟踪：候选英雄可变化，最终英雄由 Live Client 确定。

    真机日志证据（2026-09-21 正式版 22:55:00 ~ 22:55:10）：
        选人阶段只读了 **一次** ``/lol-champ-select/v1/session``，
        拿到第一个非 0 championId（4 / 卡牌大师）后就永久写入。
        玩家随后换英雄完全被忽略——之后的每一次海克斯查询都用了旧英雄。

    因此现在的规则：
        ChampSelect          -> candidate_champion（持续读取，允许变化）
        进入 InProgress 之后  -> final_champion（Live Client Data API 权威）
        换英雄               -> 切换 Session 的英雄并重新预取（不删 SQLite 缓存）
    """

    def __init__(self, detector: ChampionDetector, live: LiveClientDataSource,
                 provider: OpggProvider, session: GameSession,
                 hub=None, session_id: Optional[int] = None) -> None:
        self.detector = detector
        self.live = live
        self.provider = provider
        self.session = session
        self._hub = hub
        #: 本局会话代际（用于丢弃上一局晚到的后台结果）
        self.session_id = session_id if session_id is not None else provider.session_id
        self.candidate_id: Optional[int] = None
        self.final_id: Optional[int] = None

    @property
    def hub(self):
        return self._hub if self._hub is not None else HUB

    # -- 内部 ---------------------------------------------------------------
    async def _resolve_info(self, champion_id: int):
        try:
            return await self.detector.resolve_champion(champion_id)
        except Exception as exc:  # noqa: BLE001
            champion_log.debug("解析英雄 %s 失败: %s", champion_id, exc)
            return None

    async def _live_snapshot(self):
        try:
            return await self.live.get_snapshot()
        except Exception as exc:  # noqa: BLE001
            logger.debug("Live Client 读取失败: %s", exc)
            return None

    def _publish(self, kind: str, champion_id: int, alias, name) -> None:
        """同时更新 Session 的生效英雄与对应角色（candidate / final）。"""
        self.hub.update(champion_id=champion_id, champion_alias=alias, champion_name=name,
                        **{"%s_champion_id" % kind: champion_id,
                           "%s_champion_alias" % kind: alias,
                           "%s_champion_name" % kind: name})

    async def _activate(self, champion_id: int, alias, name, source: str) -> None:
        """切换 Session 的英雄；OP.GG 预取**放后台**，绝不让网络阻塞扫描。

        §C：拿到 championId 立刻显示中文名（_publish 在调用本方法之前就完成），
        网络请求慢 / 失败都不影响英雄名与海克斯名称的显示。
        """
        task = await self.provider.activate_champion_background(
            champion_id, alias=alias, name=name)
        if task is not None:
            task.add_done_callback(
                lambda finished, cid=champion_id: self._on_preload_done(cid, finished))
        champion_log.info("%s -> active champion = %s / %s（OP.GG 后台预取）",
                          source, name or alias, champion_id)

    def _on_preload_done(self, champion_id: int, task) -> None:
        """后台预取完成：只更新缓存状态/口径，不碰识别状态。"""
        if self.session_id != self.provider.session_id:
            champion_log.info("丢弃上一局（session=%s）晚到的预取完成回调",
                              self.session_id)
            return
        if self.provider.active_champion_id != champion_id:
            champion_log.info("丢弃非当前英雄（champion_id=%s）的预取回调", champion_id)
            return
        result = None
        if not task.cancelled():
            try:
                result = task.result()
            except Exception as exc:  # noqa: BLE001 - 后台任务异常只记录
                champion_log.debug("后台预取失败 champion_id=%s: %s", champion_id, exc)
        state = self.provider.stats_state(champion_id)
        self.hub.update(cache_status=state.value,
                        data_patch=self.provider.data_patch)
        if result is not None:
            print("OP.GG 预取: %s 条 (champion=%s, scope = %s, data_patch=%s, state=%s)"
                  % (result.augment_count, self.provider.active_champion_label,
                     self.provider.augment_scope_label, result.data_patch, state.value))

    # -- ChampSelect：候选英雄 -------------------------------------------------
    async def poll_candidate(self) -> Optional[int]:
        """读一次选人会话；英雄变化时更新 Session + 重新预取。返回当前候选 id。"""
        try:
            info = await self.detector.get_champ_select_info()
        except Exception as exc:  # noqa: BLE001 - 选人接口异常不能退出
            champion_log.debug("读取选人会话失败: %s", exc)
            return None
        if info is None:
            return None
        champion_id = info.resolved_champion_id
        if not champion_id:
            champion_log.debug("championId 仍为 0（cellId=%s, timer=%s），继续等待",
                               info.local_player_cell_id, info.timer_phase)
            return None
        if champion_id == self.candidate_id:
            return champion_id
        champion = await self._resolve_info(champion_id)
        alias = getattr(champion, "alias", None)
        name = getattr(champion, "name", None)
        previous = self.candidate_id
        self.candidate_id = champion_id
        self.session.set_candidate_champion(champion_id, alias=alias, name=name)
        self._publish("candidate", champion_id, alias, name)
        champion_log.info("候选英雄%s: %s / %s",
                          "变化" if previous else "确定", name or alias, champion_id)
        await self._activate(champion_id, alias, name, "ChampSelect 候选英雄")
        return champion_id

    # -- 对局内：Live Client 是权威来源 ----------------------------------------
    async def _champion_id_from_snapshot(self, snapshot) -> Optional[int]:
        try:
            summary = await self.detector.get_champion_summary()
        except Exception as exc:  # noqa: BLE001
            champion_log.debug("champion-summary 读取失败: %s", exc)
            return None
        if snapshot.champion_alias:
            info = next((c for c in summary
                         if (c.alias or "").lower() == snapshot.champion_alias.lower()), None)
            if info is not None:
                return info.champion_id
        if snapshot.champion_name:
            info = next((c for c in summary if c.name == snapshot.champion_name), None)
            if info is not None:
                return info.champion_id
        return None

    async def confirm_final(self, snapshot) -> Optional[int]:
        """进入对局后确定最终英雄（Live Client Data API 权威）。"""
        if snapshot is None:
            return None
        champion_id = await self._champion_id_from_snapshot(snapshot)
        if not champion_id or champion_id == self.final_id:
            return self.final_id
        alias, name = snapshot.champion_alias, snapshot.champion_name
        self.final_id = champion_id
        self.session.set_final_champion(champion_id, alias=alias, name=name)
        self._publish("final", champion_id, alias, name)
        champion_log.info("最终英雄(Live Client): %s / %s level=%s",
                          name or alias, champion_id, snapshot.level)
        if champion_id != self.provider.active_champion_id:
            await self._activate(champion_id, alias, name, "Live Client 最终英雄")
        return champion_id

    # -- 等待英雄 -------------------------------------------------------------
    async def wait_for_champion(self, timeout: float,
                                monitor: GameflowMonitor) -> Optional[int]:
        """等到确定英雄：ChampSelect 候选，或对局中的 Live Client 权威值。"""
        deadline = time.monotonic() + max(1.0, timeout)
        while time.monotonic() < deadline:
            if _stopping():
                champion_log.info("停止信号已设置，放弃等待英雄")
                return None
            phase = await monitor.get_phase()
            if monitor.consecutive_failures >= CLIENT_MAX_FAILURES:
                champion_log.warning("等待英雄期间 LCU 连续 %d 次无响应，放弃本局",
                                     monitor.consecutive_failures)
                return None
            if phase is GamePhase.CHAMP_SELECT:
                if await self.poll_candidate():
                    return self.candidate_id
            elif phase in IN_GAME_PHASES:
                # 程序可能在对局中途才启动：Live Client 直接给答案
                if await self.confirm_final(await self._live_snapshot()):
                    return self.final_id
            await _sleep(2.0, 0.5)

        # 超时兜底：再试一次 Live Client
        if await self.confirm_final(await self._live_snapshot()):
            return self.final_id
        champion_log.warning("等待英雄超时（%.0fs）", timeout)
        return None


# ---------------------------------------------------------------------------
# Session 清理
# ---------------------------------------------------------------------------

def reset_hub_session_state(hub=None):
    """把「当前对局」相关的界面状态统一清零（Game End / 新一局）。

    §6 / §7：对局结束后主界面必须是
        LOL Client   ● 已连接
        游戏状态      等待游戏
        当前英雄      -
        海克斯阶段    -
        Patch        -
        悬浮窗        ○ 等待海克斯

    只清 Session —— SQLite 缓存、用户设置、OP.GG 历史缓存、字典一律保留。
    """
    target = hub if hub is not None else HUB
    return target.update(
        champion_id=None, champion_alias=None, champion_name=None,
        candidate_champion_id=None, candidate_champion_alias=None,
        candidate_champion_name=None,
        final_champion_id=None, final_champion_alias=None, final_champion_name=None,
        level=None, stage=None, augment_round=0,
        offers=[], overlay_visible=False, offer_version=0,
        changed_slots=[], rerolled_slots=[], recommended=[],
        ocr_texts=[], cache_status=CacheStatus.MISSING.value, data_patch=None,
        scan_mode="-", ocr_calls=0,
        gameflow=GamePhase.NONE.value,
        perf_trace=None, offers_updated_at=0.0,
    )


# ---------------------------------------------------------------------------
# 一局
# ---------------------------------------------------------------------------

async def wait_for_game(client: LcuClient, timeout_minutes: float,
                        max_failures: int = CLIENT_MAX_FAILURES) -> str:
    """等待进入对局（含选人）。

    返回：
        "ready"        已进入 ChampSelect / GameStart / InProgress
        "timeout"      等到超时
        "disconnected" LCU 连续读取失败（端口/token 可能已变）→ 必须重新发现客户端

    旧实现只在超时后返回 False，而 `get_phase()` 把连接错误伪装成 UNKNOWN，
    客户端重启后会静默空等 `wait_game`（默认 180 分钟）——这是"多局后突然失效"的主因之一。
    """
    monitor = GameflowMonitor(client)
    deadline = time.monotonic() + timeout_minutes * 60
    last: Optional[GamePhase] = None
    while time.monotonic() < deadline:
        if _stopping():
            gameflow_log.info("停止信号已设置，放弃等待对局")
            return "timeout"
        phase = await monitor.get_phase()
        if monitor.consecutive_failures >= max_failures:
            client_log.warning("LCU 连续 %d 次读取失败，判定连接断开（%s）",
                               monitor.consecutive_failures, monitor.last_error)
            return "disconnected"
        if phase is not last:
            gameflow_log.info("phase=%s", phase.value)
            last = phase
            HUB.update(gameflow=phase.value)
        if phase in (GamePhase.CHAMP_SELECT, GamePhase.GAME_START, GamePhase.IN_PROGRESS):
            return "ready"
        await _sleep(2)
    gameflow_log.warning("等待对局超时（%.0f 分钟），继续常驻", timeout_minutes)
    return "timeout"


async def play_one_game(settings: Settings, args, client: LcuClient,
                        provider: OpggProvider, overlay: OverlayManager) -> str:
    """跑完一局。返回 'game_end' / 'disconnected' / 'no_game'。"""
    from src.ocr.capture import ScreenCapture
    from src.ocr.card_reader import CardReader
    from src.ocr.engine import shared_ocr_engine

    session_id = next(_SESSION_SEQ)
    # 新一局：Session 作用域清零（SQLite / 用户设置 / 字典一律保留）
    provider.begin_session(session_id=session_id)
    reset_hub_session_state()

    wait_outcome = await wait_for_game(client, args.wait_game)
    if wait_outcome == "disconnected":
        provider.end_session()
        reset_hub_session_state()
        return "disconnected"
    if wait_outcome != "ready":
        provider.end_session()
        return "no_game"

    capture = ScreenCapture()
    monitor = GameflowMonitor(client)
    detector = ChampionDetector(client, monitor)
    live = LiveClientDataSource()
    flow = AugmentFlow()
    session = GameSession()
    tracker = ChampionTracker(detector, live, provider, session,
                              session_id=session_id)
    disconnected = False

    try:
        champion_id = await tracker.wait_for_champion(args.champ_timeout, monitor)
        if not champion_id:
            if monitor.consecutive_failures >= CLIENT_MAX_FAILURES:
                client_log.warning("等待英雄期间 LCU 已断开，交由常驻循环重新连接")
                return "disconnected"
            champion_log.warning("无法确定英雄（不在选人也未在对局中）")
            return "no_game"
        print("Champion: %s" % (session.champion_name or session.champion_alias))
        print("ChampionId: %s" % champion_id)
        print("Alias: %s" % session.champion_alias)

        reader = CardReader(capture, ocr_engine=shared_ocr_engine(),
                            interval=args.scan_interval)
        #: 海克斯扫描门控（P0-2）：只在"该看牌面"时才抓屏 / OCR
        gate = AugmentScanGate(
            scan_interval=args.scan_interval,
            watchdog_interval=float(getattr(args, "watchdog_interval",
                                            WATCHDOG_INTERVAL)))
        gate.reset()
        last_signature = None
        miss_count = 0
        slot_offers: list = [None, None, None]
        active_at_scan = session.active_champion_id
        #: Live Client 只在需要时轮询（§15：等级 1 秒一次足够）
        level_poll_interval = float(getattr(args, "level_poll_interval",
                                            LEVEL_POLL_INTERVAL))
        next_level_poll = 0.0
        #: 已经发布出去的 OP.GG 数据版本（用于"数值晚到后自动补上"）
        published_stats_version = -1
        #: 已经发布过完整快照的 stage（用于"长时间确认不出来"的兜底判断）
        published_stage = None
        #: 本局最近一次已知 stage（stage 变化时要强制全量识别）
        last_stage = session.stage
        #: 连续"三槽都确认不了"的起始时间（配合 MISS_SECONDS 做时间判定）
        miss_since = None
        #: 本局已经出现过的 stage，用于区分首次和"第一次选取后的下一轮"
        seen_stages: set[int] = set()
        #: 本局实际海克斯轮次，不受等级上限限制；KDA 额外轮次可为 5。
        augment_round = 0
        round_boundary_pending = False
        round_boundary_since = None
        last_pending_notice = 0.0

        while True:
            if _stopping():
                augment_log.info("停止信号已设置，退出本局循环")
                break
            now = time.monotonic()

            phase = await monitor.get_phase()
            if monitor.consecutive_failures >= CLIENT_MAX_FAILURES:
                gameflow_log.warning(
                    "LCU 连续 %d 次无响应（%s），结束本局并重新发现客户端",
                    monitor.consecutive_failures, monitor.last_error)
                disconnected = True
                break

            # --- 1) 选人阶段：持续跟踪候选英雄（换英雄 -> 立刻切预取） ------
            if phase is GamePhase.CHAMP_SELECT:
                if tracker.final_id is None:
                    # 本局还没开始：候选英雄允许变化
                    await tracker.poll_candidate()
                    HUB.update(gameflow=phase.value)
                    await pump_qt()
                    await _sleep(float(getattr(args, "champ_poll_interval",
                                               CHAMP_SELECT_POLL_SECONDS)))
                    continue
                # 已经打完一局（final 已确定）后又进入选人 = 新一局
                gameflow_log.info("重新进入选人阶段，本局结束")
                gate.note_game_end()
                break

            # --- 2) 对局结束：任何"离开对局"的阶段都必须收尾 ---------------
            if phase.is_game_end:
                gameflow_log.info("对局结束（phase=%s）", phase.value)
                gate.note_game_end()
                break
            HUB.update(gameflow=phase.value)

            # --- 3) 对局中：Live Client 是英雄与等级的权威来源 --------------
            #         等级不需要 0.25s 刷新，1 秒一次即可（§15）
            if now >= next_level_poll:
                next_level_poll = now + level_poll_interval
                snapshot = None
                try:
                    snapshot = await live.get_snapshot()
                except Exception as exc:  # noqa: BLE001
                    logger.debug("Live Client 读取失败: %s", exc)
                if snapshot is not None:
                    await tracker.confirm_final(snapshot)
                    if snapshot.level:
                        session.set_level(snapshot.level)
                        HUB.update(level=snapshot.level, stage=session.stage,
                                   augment_round=augment_round)

            # 英雄在本次识别前发生变化 -> 上一套牌面与统计全部作废
            if session.active_champion_id != active_at_scan:
                active_at_scan = session.active_champion_id
                slot_offers = [None, None, None]
                last_signature = None
                miss_count = 0
                miss_since = None
                published_stats_version = -1
                reader.reset_slots("英雄变化")
                last_stage = session.stage
                augment_log.info("英雄已切换为 %s，重新识别并重新查询",
                                 provider.active_champion_label)

            # 新的海克斯阶段：清空逐槽基线，强制全量识别（要求 C）
            if session.stage != last_stage:
                last_stage = session.stage
                reader.reset_slots("stage 变为 %s" % session.stage)
                slot_offers = [None, None, None]
                last_signature = None
                miss_count = 0
                miss_since = None
                published_stage = None
                augment_log.info("进入新 stage=%s，执行一次全量识别", session.stage)

            # --- 3.5) OP.GG：失败重试 + 数据晚到时补上数值（不影响识别状态） --
            provider.maybe_retry(session.active_champion_id, now)
            stats_version = provider.stats_version(session.active_champion_id)
            if stats_version != published_stats_version:
                published_stats_version = stats_version
                if slot_offers and any(o is not None for o in slot_offers):
                    filled = refresh_slot_stats(session, provider, slot_offers)
                    pending_now = set(reader.pending_slots())
                    payload, recommended = build_offer_payload(
                        slot_offers, pending_now,
                        stats_state=provider.stats_state(session.active_champion_id))
                    publish_offers(
                        overlay, payload, pending_now, recommended,
                        cache_status=provider.stats_state(
                            session.active_champion_id).value,
                        ocr_calls=reader.ocr_calls,
                        offers_updated_at=time.perf_counter())
                    augment_log.info(
                        "OP.GG 数据到位（version=%s, state=%s），已刷新强度/欢迎度：%d/3 槽",
                        stats_version,
                        provider.stats_state(session.active_champion_id).value, filled)

            # --- 4) 扫描门控（P0-2）：不该看牌面时绝不 OCR -----------------
            gate.update_stage(session.stage, now)
            mode = gate.mode(now)
            if mode is ScanMode.WATCHDOG:
                # 低频（默认 0.35s）只看"画面是不是静止 / 亮度是不是跳变"，不做 OCR
                feature = reader.probe_feature()
                if gate.observe(feature, now):
                    augment_log.info("[GATE] 低频检测命中（stable=%s bright=%.0f），"
                                     "重新开启高频扫描 stage=%s",
                                     feature.stable, feature.mean_brightness, gate.stage)
                HUB.update(scan_mode=mode.value, ocr_calls=reader.ocr_calls)
                await pump_qt()
                await _sleep(gate.interval(mode))
                continue
            if mode is ScanMode.STOPPED:
                gameflow_log.info("扫描已停止（对局结束）")
                break

            HUB.update(scan_mode=mode.value)
            scan_started = time.perf_counter()
            try:
                offer = reader.read_once()
            except Exception as exc:  # noqa: BLE001 - OCR 异常不能退出
                ocr_log.warning("扫描失败: %s", exc)
                await pump_qt()
                await _sleep(args.scan_interval)
                continue
            metrics = getattr(offer, "metrics", None)
            if metrics is None:
                # 兼容测试注入的 FakeCardReader。
                from src.ocr.card_reader import ReadMetrics

                elapsed = (time.perf_counter() - scan_started) * 1000.0
                metrics = ReadMetrics(
                    detect_ms=elapsed, total_ms=elapsed,
                    ocr_calls=max(0, len(offer.readings)),
                    slot_ocr_calls=[
                        1 if reading.matched else 0
                        for reading in offer.readings
                    ][:3],
                )
            HUB.perf.record("ocr", metrics.ocr_ms / 1000.0)
            if metrics.ocr_calls:
                flow.set_state(AugmentFlowState.SCANNING)

            pending_slots = set(offer.pending_slots)
            # 已经显示过浮层时，三槽都无法匹配到牌面应进入消失判定；
            # 只有首次识别（last_signature 为空）的失败才无条件重试。
            if pending_slots and last_signature is None:
                gate.note_offers(True, now)
            stuck = [slot for slot in pending_slots
                     if reader.pending_age(slot) >= SNAPSHOT_GIVEUP_SECONDS]
            if stuck:
                reader.retry_unresolved(stuck)

            # §6/§10/§13：只有"三个槽一个都确认不了"才算界面消失。
            # 单槽刷新动画 / 单槽 OCR 失败时，另外两槽仍处于 STABLE →
            # 绝不允许把整个 Overlay 清掉。
            if not offer.confirmed_slots:
                if last_signature is not None:
                    if round_boundary_since is None:
                        round_boundary_since = now
                    elif now - round_boundary_since >= ROUND_GAP_CONFIRM_SECONDS:
                        round_boundary_pending = True
                else:
                    round_boundary_since = None
                miss_count += 1
                if miss_since is None:
                    miss_since = now
                # 连续次数 + 持续时间都满足才判定界面消失
                # （避免单帧 OCR 失败/动画瞬间把整个 Overlay 清掉）
                if (last_signature is not None and miss_count >= MISS_LIMIT
                        and now - miss_since >= MISS_SECONDS):
                    augment_log.info(
                        "海克斯界面消失（连续 %d 次 / %.1fs 三个槽都无法确认）",
                        miss_count, now - miss_since)
                    overlay.set_offers([])
                    overlay_log.info("hide")
                    HUB.update(overlay_visible=False, offers=[])
                    last_signature = None
                    miss_since = None
                    published_stage = None
                    gate.note_offers(False, now)
                    flow.set_state(AugmentFlowState.WAIT_AUGMENT)
                    reader.reset_slots("海克斯界面消失")
                    round_boundary_pending = True
                    round_boundary_since = None
                    last_pending_notice = 0.0
                elif pending_slots and now - last_pending_notice >= 2.0:
                    last_pending_notice = now
                    augment_log.info(
                        "三槽暂未确认 slots=%s，等待界面消失判定或重新识别",
                        sorted(pending_slots))
                await pump_qt()
                await _sleep(args.scan_interval)
                continue

            miss_count = 0
            miss_since = None
            round_boundary_since = None
            if pending_slots:
                # 后台还在确认（刷新/动画/重试）→ **界面完全不动**，
                # 继续显示上一份已确认快照（绝不出现"识别中"）。
                if now - last_pending_notice >= 2.0:
                    last_pending_notice = now
                    augment_log.info(
                        "后台确认中 slots=%s（界面保持上一份已确认快照）",
                        sorted(pending_slots))
                await pump_qt()
                await _sleep(args.scan_interval)
                continue

            if offer.signature == last_signature:
                round_boundary_pending = False
                await pump_qt()
                await _sleep(args.scan_interval)
                continue

            last_signature = offer.signature
            gate.note_offers(True, now)      # 牌面已在屏幕上 → 继续跟踪刷新
            published_stage = session.stage
            flow.set_state(AugmentFlowState.STABLE)
            if (augment_round == 0 or round_boundary_pending
                    or flow.stage != session.stage):
                augment_round += 1
                round_boundary_pending = False
            update = flow.register_offers(
                session.stage, offer.signature, augment_round=augment_round)
            if update.is_new_round:
                session.begin_round(update.stage, update.offers,
                                    augment_round=augment_round)
            else:
                session.apply_slot_update(update.offer_version, update.changed_slots,
                                          update.offers)
            flow.set_state(AugmentFlowState.QUERYING)

            for index, reading in enumerate(offer.readings):
                if reading.augment_id:
                    ocr_log.info("slot%d=%s%s", index, reading.augment_id,
                                 " (reused)" if reading.reused else "")
            HUB.update(ocr_texts=[r.raw_text for r in offer.readings],
                       ocr_latency_ms=HUB.perf.stats.ocr_ms)

            if update.is_new_stage:
                slot_offers = [None] * len(offer.readings)
                slots = range(len(offer.readings))
            else:
                slots = update.changed_slots or range(len(offer.readings))

            stats_started = time.perf_counter()
            stats_source = "none"
            for slot in slots:
                if slot >= len(offer.readings):
                    continue
                reading = offer.readings[slot]
                # 必须带当前 Session 的英雄（final 优先），绝不按"只查海克斯"取数
                result = provider.query_augment_stats(
                    session.active_champion_id, reading.augment_id)
                stats = result.stats
                if stats_source == "none":
                    stats_source = result.source
                slot_offers[slot] = AugmentOfferLike(reading, stats)
            stats_lookup_ms = (time.perf_counter() - stats_started) * 1000.0

            recommend_started = time.perf_counter()
            payload, recommended = build_offer_payload(
                slot_offers, pending_slots,
                stats_state=provider.stats_state(session.active_champion_id))
            recommend_ms = (time.perf_counter() - recommend_started) * 1000.0

            if update.is_new_round:
                trigger = "INITIAL" if not seen_stages else "NEXT_STAGE"
                if session.stage is not None:
                    seen_stages.add(session.stage)
            elif update.changed_slots:
                trigger = "SLOT_CHANGE"
            else:
                trigger = "SIGNATURE_UPDATE"

            augment_log.info("Stage=%s OfferVersion=%s SlotRerolled=%s",
                             update.stage, update.offer_version, update.changed_slots)
            flow_log.info("Recommended=%s (%s) 重查槽位=%s",
                          recommended, recommendation_reason(), sorted(slots))
            print_offers(payload, update)
            overlay_started = time.perf_counter()
            trace = HUB.perf.new_trace(
                session_id=session_id, stage=session.stage,
                augment_round=augment_round,
                offer_version=update.offer_version, trigger=trigger,
                started_at_perf=scan_started)
            trace.screenshot_ms = metrics.screenshot_ms
            trace.offer_detect_ms = metrics.detect_ms
            trace.ocr_ms = metrics.ocr_ms
            trace.stabilize_ms = metrics.stabilize_ms
            trace.stats_lookup_ms = stats_lookup_ms
            trace.stats_source = stats_source
            trace.recommend_ms = recommend_ms
            trace.ocr_calls = metrics.ocr_calls
            trace.slot_ocr_calls = list(metrics.slot_ocr_calls)
            trace.total_ms = (metrics.total_ms + metrics.stabilize_ms
                              + stats_lookup_ms + recommend_ms)
            # 先进入历史记录，Qt 主线程收到 offers 信号时即可补写真实可见延迟。
            HUB.perf.record_trace(trace)
            try:
                publish_offers(
                    overlay, payload, pending_slots, recommended,
                    offer_version=update.offer_version,
                    changed_slots=list(update.changed_slots),
                    rerolled_slots=sorted(flow.rerolled_slots),
                    level=session.level, stage=session.stage,
                    augment_round=augment_round,
                    ocr_calls=reader.ocr_calls,
                    cache_status=provider.cache_status(
                        session.active_champion_id).value,
                    perf_trace=trace,
                    offers_updated_at=time.perf_counter())
                overlay_log.info("show %d cards (cache=%s)",
                                 len(payload),
                                 provider.cache_status(session.active_champion_id).value)
            except Exception as exc:  # noqa: BLE001
                overlay_log.warning("更新失败: %s", exc)
            trace.overlay_update_ms = (time.perf_counter() - overlay_started) * 1000.0
            trace.total_ms = (metrics.total_ms + metrics.stabilize_ms
                              + stats_lookup_ms + recommend_ms
                              + trace.overlay_update_ms)
            HUB.perf.record_trace(trace)
            HUB.update(perf_trace=trace)
            HUB.perf.record("overlay", trace.overlay_update_ms / 1000.0)
            perf_log.info("%s", trace.describe())
            flow.set_state(AugmentFlowState.SHOWING)
            await pump_qt()
            await _sleep(args.scan_interval)
    finally:
        capture.close()
        overlay.set_offers([])
        try:
            await live.aclose()        # 释放复用的 Live Client 连接（§14）
        except Exception:  # noqa: BLE001
            logger.debug("关闭 Live Client 失败", exc_info=True)
        session.reset()
        flow.reset()
        provider.end_session()
        reset_hub_session_state()
        augment_log.info("对局清理完成（Session 清空；SQLite 缓存 / 用户设置 / 字典保留）")
    return "disconnected" if disconnected else "game_end"


# ---------------------------------------------------------------------------
# 常驻
# ---------------------------------------------------------------------------

async def resident(settings: Settings, args, control: "Control | None" = None) -> int:
    """常驻：等待客户端 -> 一局接一局，绝不因为一局结束而退出。"""
    control = control or Control()
    from src.ocr.engine import start_ocr_prewarm

    start_ocr_prewarm()
    _STOP[0] = control.stop
    overlay = make_overlay(settings, enabled=not args.no_overlay)
    provider = OpggProvider(stats_repository=args.stats_repository, settings=settings)
    client_log.info("常驻模式启动")
    HUB.perf.start()
    try:
        while True:
            if control.stop.is_set():
                client_log.info("收到停止信号，常驻退出")
                return 0
            if control.reconnect.is_set():
                control.reconnect.clear()
                client_log.info("收到重连请求，重新发现 LeagueClientUx")
            try:
                client = await wait_for_client(settings, args.wait_client, overlay)
                if client is None:
                    continue
                while True:
                    if control.stop.is_set():
                        return 0
                    if control.reconnect.is_set():
                        control.reconnect.clear()
                        client_log.info("收到重连请求，断开后重新连接")
                        try:
                            await client.aclose()
                        except Exception:  # noqa: BLE001
                            logger.debug("关闭旧 LCU 连接失败", exc_info=True)
                        break
                    outcome = await play_one_game(settings, args, client, provider, overlay)
                    if outcome == "disconnected":
                        client_log.warning("本局因 LCU 断开而结束，重新发现客户端（端口/token 可能已变）")
                        try:
                            await client.aclose()
                        except Exception:  # noqa: BLE001
                            logger.debug("关闭旧 LCU 连接失败", exc_info=True)
                        HUB.update(client_connected=False)
                        break
                    if outcome == "no_game" and args.once:
                        return 0
                    if args.once:
                        return 0
                    # 回到等待状态，准备下一局
                    if overlay.available:
                        overlay.update(True, "Waiting")
                    HUB.perf.log()
                    await _sleep(float(getattr(args, "resident_pause", 3.0)))
            except Exception as exc:  # noqa: BLE001 - 常驻循环绝不能被单点异常打断
                logger.error("常驻循环异常（继续运行）: %s", exc, exc_info=True)
                await _sleep(5)
    finally:
        HUB.perf.stop()
        await provider.aclose()
        HUB.update(client_connected=False, overlay_visible=False)
        overlay.close()
        # 清理全局停止信号，避免污染同一进程内的下一次 resident()/测试
        if _STOP[0] is control.stop:
            _STOP[0] = None


def run(settings: Settings, args) -> int:
    """入口：--live 跑一局；--resident 常驻。"""
    import asyncio as _asyncio

    if args.resident:
        try:
            return _asyncio.run(resident(settings, args))
        except KeyboardInterrupt:
            client_log.info("收到 Ctrl+C，常驻退出")
            return 0

    async def _single() -> int:
        from src.ocr.engine import start_ocr_prewarm

        start_ocr_prewarm()
        overlay = make_overlay(settings, enabled=not args.no_overlay)
        provider = OpggProvider(stats_repository=args.stats_repository, settings=settings)
        try:
            client = await wait_for_client(settings, args.wait_client, overlay)
            if client is None:
                print("LCU: Disconnected")
                return 1
            await play_one_game(settings, args, client, provider, overlay)
            return 0
        finally:
            await provider.aclose()
            overlay.close()

    return _asyncio.run(_single())


__all__ = ["run", "resident", "play_one_game", "make_overlay", "print_offers",
           "Control", "request_reconnect", "ChampionTracker",
           "reset_hub_session_state", "AugmentOfferLike",
           "AugmentScanGate", "ScanMode", "LEVEL_POLL_INTERVAL"]
