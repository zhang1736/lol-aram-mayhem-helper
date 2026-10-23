"""OP.GG 数据提供者：Memory -> SQLite -> Network，带 TTL 与 stale-while-revalidate。

读取顺序：Memory -> SQLite -> Network
写入顺序：Network -> Memory -> SQLite

缓存状态（Day 5 §3）：
    FRESH   直接用
    STALE   先用旧值显示，后台刷新（**不阻塞海克斯界面**）
    MISSING 等网络

任何请求失败（timeout / 422 / 429 / 5xx / 断网 / JSON 结构变化）
都只记日志并降级，绝不抛出到上层。
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Optional

from src.config.settings import Settings
from src.core.models import DataScope, OPGGAugmentStats
from src.data.cache import CacheStatus, evaluate
from src.opgg.client import OpggClient, OpggError
from src.opgg.parser import SchemaMismatchError, parse_augment_stats, parse_meta_patch
from src.utils.logger import get_tagged

logger = get_tagged("opgg", __name__)

DEFAULT_TTL_SECONDS = 6 * 3600.0

#: §C 要求的退避重试节奏：第一次失败 1s、第二次 2s、之后 5s
RETRY_BACKOFF = (1.0, 2.0, 5.0)


class StatsState(str, Enum):
    """当前英雄 OP.GG 数据的加载状态（与 OCR 识别状态完全无关）。"""

    NOT_REQUESTED = "NOT_REQUESTED"
    LOADING = "LOADING"
    READY = "READY"
    ERROR = "ERROR"


@dataclass
class StatsResult:
    """一次查询的结果。"""

    stats: Optional[OPGGAugmentStats]
    status: CacheStatus
    source: str = "memory"     # memory / sqlite / network / none

    @property
    def available(self) -> bool:
        return self.stats is not None


@dataclass
class PreloadResult:
    champion_id: int
    augment_count: int
    data_patch: Optional[str]
    status: CacheStatus
    hero_stats_ok: bool = False
    error: Optional[str] = None


def _parse_dt(value) -> datetime:
    from src.data.cache import parse_timestamp

    return parse_timestamp(value) or datetime.now(timezone.utc)


class OpggProvider:
    """对外：preload_champion / get_augment_stats / get_cached。"""

    def __init__(self, client: OpggClient | None = None,
                 stats_repository=None,
                 settings: Settings | None = None,
                 ttl_seconds: float | None = None) -> None:
        self.settings = settings
        self.client = client or OpggClient(settings.opgg if settings else None)
        self.stats_repository = stats_repository
        self.ttl_seconds = (ttl_seconds
                            if ttl_seconds is not None
                            else (settings.cache.ttl_seconds if settings
                                  else DEFAULT_TTL_SECONDS))

        self._memory: dict[int, dict[int, OPGGAugmentStats]] = {}
        self._memory_fetched_at: dict[int, datetime] = {}
        self._hero_stats: dict[int, dict] = {}
        self.data_patch: Optional[str] = None
        self.last_error: Optional[str] = None
        self._refresh_tasks: dict[int, asyncio.Task] = {}

        #: 本**进程**里成功加载过数据的英雄（诊断用；禁止启动时批量拉取所有英雄）。
        #: 注意语义：它是"整个进程运行期间"，**不是**"当前 Session"——
        #: Session 隔离请看 active_champion_id / active_opgg_stats。
        self._loaded_champions: set[int] = set()

        # -- Session 作用域（多局隔离，四个实战 Bug 修复） -------------------
        #: 当前 Session 的英雄（None = 新一局还没确定英雄 / 对局已结束）
        self.active_champion_id: Optional[int] = None
        #: 当前 Session 该英雄的海克斯统计（换英雄 / 结束对局立刻清空引用）
        self.active_opgg_stats: dict[int, OPGGAugmentStats] = {}
        self._session_active: bool = False
        self._champion_labels: dict[int, str] = {}
        #: 每个英雄的数据加载状态 + 版本号（READY 时 +1，供主循环检测"数值到位"）
        self._states: dict[int, StatsState] = {}
        self._stats_version: dict[int, int] = {}
        #: 会话代际：每局 +1；后台任务完成时用它判断"结果是否还属于当前局"
        self.session_id: int = 0
        #: 后台预取任务（同一英雄只跑一个）
        self._preload_tasks: dict[int, asyncio.Task] = {}
        #: 重试退避（1s -> 2s -> 5s）
        self._attempts: dict[int, int] = {}
        self._next_retry_at: dict[int, float] = {}
        #: 退避节奏（可注入，测试用）：默认 1s -> 2s -> 5s
        self.retry_backoff: tuple[float, ...] = RETRY_BACKOFF

    # -- 元信息 -------------------------------------------------------------
    @property
    def augment_scope_label(self) -> str:
        return "OP.GG Aggregate"

    @property
    def hero_scope_label(self) -> str:
        if self.settings:
            return self.settings.hero_scope_label
        return "Korea / Iron–Gold"

    @property
    def loaded_champions(self) -> set[int]:
        return set(self._loaded_champions)

    @property
    def active_champion_label(self) -> str:
        """形如 ``Diana(131)``；用于日志与排查串数据。"""
        if self.active_champion_id is None:
            return ""
        return self._champion_labels.get(self.active_champion_id,
                                        str(self.active_champion_id))

    def hero_stats(self, champion_id: int) -> dict:
        return self._hero_stats.get(champion_id, {})

    # -- Session 生命周期 ---------------------------------------------------
    def begin_session(self, session_id: Optional[int] = None) -> None:
        """新一局开始：Session 作用域清零（SQLite / 全局缓存一律保留）。"""
        self._session_active = True
        self.session_id = (int(session_id) if session_id is not None
                           else self.session_id + 1)
        self.active_champion_id = None
        self.active_opgg_stats = {}
        logger.info("Session begin（session_id=%s，active champion 清空，历史缓存保留）",
                    self.session_id)

    def end_session(self) -> None:
        """对局结束：清 Session 数据引用，**不删**任何缓存。"""
        self._session_active = False
        previous = self.active_champion_id
        if previous is not None:
            self._cancel_refresh(previous)
        self.active_champion_id = None
        self.active_opgg_stats = {}
        logger.info("Session end（active champion 清空，SQLite 缓存保留 %d 个英雄）",
                    len(self._loaded_champions))

    # -- 数据加载状态 -------------------------------------------------------
    def stats_state(self, champion_id: Optional[int]) -> StatsState:
        if champion_id is None:
            return StatsState.NOT_REQUESTED
        return self._states.get(champion_id, StatsState.NOT_REQUESTED)

    def stats_version(self, champion_id: Optional[int]) -> int:
        """READY 一次就 +1；主循环靠它发现"数值刚到，需要重画 Overlay"。"""
        if champion_id is None:
            return 0
        return self._stats_version.get(champion_id, 0)

    def switch_active_champion(self, champion_id: int, alias: str | None = None,
                               name: str | None = None) -> bool:
        """同步切换当前 Session 的英雄（**不发网络请求**）。

        返回是否真的发生了切换。切换时立即丢弃上一英雄的数据引用。
        """
        if not champion_id:
            return False
        self._champion_labels[champion_id] = "%s(%s)" % (alias or name or champion_id,
                                                         champion_id)
        if champion_id == self.active_champion_id:
            return False
        previous = self.active_champion_id
        if previous is not None:
            self._cancel_refresh(previous)
            logger.info("active_champion_id=%s（由 %s 切换而来，旧 snapshot 已废弃）",
                        champion_id, self._champion_labels.get(previous, previous))
        else:
            logger.info("active_champion_id=%s", champion_id)
        self.active_champion_id = champion_id
        self.active_opgg_stats = {}
        self._states.setdefault(champion_id, StatsState.NOT_REQUESTED)
        return True

    def start_preload(self, champion_id: int, *,
                      force: bool = False) -> Optional[asyncio.Task]:
        """后台预取当前英雄的 OP.GG 数据 —— **绝不阻塞调用方**。

        同一英雄同一时间只跑一个任务；成功后 state=READY 并让 version+1。
        """
        if not champion_id:
            return None
        task = self._preload_tasks.get(champion_id)
        if task is not None and not task.done():
            return task
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:            # 没有事件循环：调用方自行 await preload
            return None
        self._states[champion_id] = StatsState.LOADING
        task = loop.create_task(self._safe_preload(champion_id, force=force))
        self._preload_tasks[champion_id] = task
        return task

    async def _safe_preload(self, champion_id: int,
                            force: bool = False) -> Optional[PreloadResult]:
        """后台预取实现：任何异常都只记录，绝不冒泡。"""
        session_id = self.session_id
        try:
            result = await self.preload_champion(champion_id, force=force)
        except Exception as exc:  # noqa: BLE001
            logger.warning("后台预取异常 (champion_id=%s): %s", champion_id, exc)
            self._states[champion_id] = StatsState.ERROR
            self._schedule_retry(champion_id)
            return None

        if result.augment_count > 0 and result.status.value in ("FRESH", "STALE"):
            self._states[champion_id] = StatsState.READY
            self._attempts[champion_id] = 0
            self._next_retry_at.pop(champion_id, None)
            self._stats_version[champion_id] = self._stats_version.get(champion_id, 0) + 1
            logger.info("stats_count=%s（champion_id=%s, state=READY, version=%s）",
                        result.augment_count, champion_id,
                        self._stats_version[champion_id])
            # 只有"还是同一局 + 仍是当前英雄"时才写入 Session 数据引用；
            # 否则只保留全局 cache，绝不污染新一局。
            if self.session_id == session_id and self.active_champion_id == champion_id:
                self.active_opgg_stats = dict(self._memory.get(champion_id, {}))
            elif self.session_id != session_id:
                logger.info("丢弃上一局晚到的 OP.GG 结果（champion_id=%s, session %s -> %s）",
                            champion_id, session_id, self.session_id)
        else:
            self._states[champion_id] = StatsState.ERROR
            logger.warning("champion_id=%s 预取未拿到数据（state=ERROR）：%s",
                           champion_id, self.last_error or "无错误详情")
            self._schedule_retry(champion_id)
        return result

    def _schedule_retry(self, champion_id: int) -> float:
        """§C 退避：第 1 次失败 1s、第 2 次 2s、之后 5s。"""
        attempt = self._attempts.get(champion_id, 0) + 1
        self._attempts[champion_id] = attempt
        backoff = self.retry_backoff or RETRY_BACKOFF
        delay = backoff[min(attempt, len(backoff)) - 1]
        self._next_retry_at[champion_id] = time.monotonic() + delay
        logger.info("champion_id=%s 将在 %.0fs 后重试（第 %s 次）",
                    champion_id, delay, attempt)
        return delay

    def maybe_retry(self, champion_id: Optional[int],
                    now: Optional[float] = None) -> bool:
        """由主循环驱动的重试（到点才发请求）；READY 之后不再重复请求。"""
        if champion_id is None:
            return False
        if self._session_active and self.active_champion_id != champion_id:
            return False
        if self.stats_state(champion_id) is StatsState.READY:
            return False
        due = self._next_retry_at.get(champion_id)
        moment = time.monotonic() if now is None else now
        if due is None or moment < due:
            return False
        task = self.start_preload(champion_id, force=True)
        if task is None:
            return False
        logger.info("champion_id=%s 重试预取 OP.GG 数据", champion_id)
        return True

    async def activate_champion_background(self, champion_id: int,
                                           alias: str | None = None,
                                           name: str | None = None) -> Optional[asyncio.Task]:
        """同步切换英雄 + 后台预取（引擎侧用这个，绝不等网络）。"""
        changed = self.switch_active_champion(champion_id, alias=alias, name=name)
        task = self.start_preload(champion_id)
        if changed and task is None:
            logger.debug("champion_id=%s 已在预取中或没有事件循环", champion_id)
        return task

    async def activate_champion(self, champion_id: int, alias: str | None = None,
                                name: str | None = None, *,
                                force: bool = False) -> Optional[PreloadResult]:
        """把当前 Session 的英雄切到 champion_id，并重新预取。

        换英雄时必须调用本方法（而不是直接 preload_champion）：
            * 忽略上一英雄的 pending refresh
            * 立刻丢弃上一英雄的数据引用（active_opgg_stats）
            * **不删** SQLite 里上一英雄的历史缓存
        """
        if not champion_id:
            return None
        self._champion_labels[champion_id] = "%s(%s)" % (alias or name or champion_id,
                                                         champion_id)
        if champion_id == self.active_champion_id and not force:
            return None
        previous = self.active_champion_id
        if previous is not None and previous != champion_id:
            self._cancel_refresh(previous)
            logger.info("Session champion 切换: %s -> %s（换英雄，重新预取）",
                        self._champion_labels.get(previous, str(previous)),
                        self._champion_labels[champion_id])
        self.active_champion_id = champion_id
        self.active_opgg_stats = {}
        self._states[champion_id] = StatsState.LOADING
        result = await self.preload_champion(champion_id, force=force)
        self.active_opgg_stats = dict(self._memory.get(champion_id, {}))
        if result.augment_count > 0 and result.status.value in ("FRESH", "STALE"):
            self._states[champion_id] = StatsState.READY
            self._attempts[champion_id] = 0
            self._stats_version[champion_id] = self._stats_version.get(champion_id, 0) + 1
        else:
            self._states[champion_id] = StatsState.ERROR
        return result

    def _cancel_refresh(self, champion_id: int) -> None:
        task = self._refresh_tasks.pop(champion_id, None)
        if task is not None and not task.done():
            task.cancel()
            logger.info("已忽略 championId=%s 的后台刷新（换英雄/换局）",
                        champion_id)

    # -- 缓存判定 -----------------------------------------------------------
    def cache_status(self, champion_id: int) -> CacheStatus:
        fetched_at = self._memory_fetched_at.get(champion_id)
        if fetched_at is None or champion_id not in self._memory:
            return CacheStatus.MISSING
        return evaluate(fetched_at, self.ttl_seconds).status

    # -- 预取 ---------------------------------------------------------------
    async def preload_champion(self, champion_id: int,
                               force: bool = False) -> PreloadResult:
        """预取当前英雄的 patch、英雄统计与海克斯统计。"""
        status = self.cache_status(champion_id)
        if status is CacheStatus.FRESH and not force:
            logger.info("championId=%s 缓存 FRESH（%.1fh TTL），跳过网络",
                        champion_id, self.ttl_seconds / 3600.0)
            return PreloadResult(champion_id, len(self._memory.get(champion_id, {})),
                                 self.data_patch, CacheStatus.FRESH)

        # 先尽量拿到 SQLite 里的旧数据（失败时兜底）
        cached_rows = self._load_from_sqlite(champion_id)
        if cached_rows:
            self._memory[champion_id] = cached_rows
            logger.info("SQLite 命中 %d 条（championId=%s）", len(cached_rows), champion_id)

        # 载入 SQLite 后重新判定新鲜度：如果 SQLite 里的数据还是 FRESH，
        # 就没必要再打网络（省一次请求，也避免无谓的限流风险）。
        status = self.cache_status(champion_id)
        if status is CacheStatus.FRESH and not force:
            logger.info("SQLite 缓存 FRESH，跳过网络 (championId=%s)", champion_id)
            return PreloadResult(champion_id, len(cached_rows),
                                 self.data_patch, CacheStatus.FRESH)

        result = PreloadResult(champion_id, len(cached_rows), self.data_patch,
                               CacheStatus.STALE if cached_rows else CacheStatus.MISSING)

        # --- 1) 英雄统计（提供 data_patch，同时校验 KR/IBSG 口径） ---
        result.hero_stats_ok = await self._fetch_hero_stats(champion_id)

        # --- 2) 海克斯统计 ---
        try:
            logger.info("active_champion_id=%s request_start", champion_id)
            payload = await self.client.get_champion_augments(champion_id)
            stats = parse_augment_stats(payload, patch=self.data_patch)
        except (OpggError, SchemaMismatchError) as exc:
            self.last_error = str(exc)
            logger.warning("OP.GG request failed (championId=%s): %s", champion_id, exc)
            if cached_rows:
                logger.info("Falling back to stale cache (%d 条)", len(cached_rows))
                result.status = CacheStatus.STALE
                result.augment_count = len(cached_rows)
            else:
                logger.warning("无可用缓存，海克斯数据暂缺")
                result.status = CacheStatus.MISSING
            return result

        logger.info("stats_count=%s（champion_id=%s）", len(stats), champion_id)
        self._memory[champion_id] = {s.augment_id: s for s in stats}
        self._memory_fetched_at[champion_id] = datetime.now(timezone.utc)
        self._loaded_champions.add(champion_id)
        self.last_error = None
        result.augment_count = len(stats)
        result.status = CacheStatus.FRESH
        logger.info("prefetch %d augment stats (championId=%s, scope=%s, data_patch=%s)",
                    len(stats), champion_id, self.augment_scope_label, self.data_patch)

        if self.stats_repository is not None:
            try:
                self.stats_repository.upsert_many(champion_id, stats,
                                                  patch=self.data_patch)
                logger.info("已写入 SQLite 缓存: %d 条", len(stats))
            except Exception as exc:  # noqa: BLE001 - 缓存写失败不能影响主流程
                logger.warning("写入 SQLite 失败: %s", exc)
        return result

    async def _fetch_hero_stats(self, champion_id: int) -> bool:
        """英雄统计：提供 data_patch，并验证 KR / IBSG 口径真实生效。"""
        region = self.settings.data.region if self.settings else "kr"
        tier = self.settings.data.tier if self.settings else "ibsg"
        try:
            payload = await self.client.get_champion_stats(champion_id,
                                                           region=region, tier=tier)
        except (OpggError, SchemaMismatchError) as exc:
            logger.warning("英雄统计获取失败: %s", exc)
            return False
        self._hero_stats[champion_id] = payload
        patch = parse_meta_patch(payload)
        if patch:
            self.data_patch = patch
        logger.info("hero stats ok (region=%s, tier=%s, data_patch=%s)",
                    region, tier, patch)
        return True

    # -- 查询 ---------------------------------------------------------------
    def _champion_mismatch(self, champion_id: int) -> bool:
        """Session 已激活英雄时，禁止用别的英雄查询（防止串数据）。"""
        return self._session_active and self.active_champion_id != champion_id

    def get_cached(self, champion_id: int,
                   augment_id: int) -> Optional[OPGGAugmentStats]:
        """只查内存（不访问网络），供海克斯界面零等待读取。

        查询必须同时带 champion_id：当前 Session 只允许访问
        ``champion == active_champion`` 的数据，否则直接返回 None 并告警。
        """
        if self._champion_mismatch(champion_id):
            logger.warning("拒绝跨英雄读取: active=%s 请求=%s"
                           "（防止上一局数据串到本局）",
                           self.active_champion_id, champion_id)
            return None
        return self._memory.get(champion_id, {}).get(augment_id)

    def query_augment_stats(self, champion_id: int, augment_id: int) -> StatsResult:
        """海克斯界面用的单次查询（内存直读，绝不访问网络）+ 完整日志。

        日志格式（便于排查串数据）：
            [OPGG] QueryStats champion=Diana(131) augment=2064
            [OPGG] CacheStatus=FRESH
            [OPGG] popular=1.54
        """
        logger.info("QueryStats champion=%s augment=%s",
                    self._champion_labels.get(champion_id, champion_id), augment_id)
        if self._champion_mismatch(champion_id):
            logger.warning("拒绝跨英雄查询: active=%s 请求=%s",
                           self.active_champion_id, champion_id)
            return StatsResult(None, CacheStatus.MISSING, "none")
        stats = self.get_cached(champion_id, augment_id)
        status = self.cache_status(champion_id)
        logger.info("CacheStatus=%s", status.value)
        if stats is None:
            logger.warning("MISS augment_id=%s（champion=%s, state=%s, 强度/欢迎度显示 —）",
                           augment_id, self._champion_labels.get(champion_id, champion_id),
                           self.stats_state(champion_id).value)
            # 缺数据时低频重试一次（网络恢复后数值会自己出现）
            self.maybe_retry(champion_id)
        else:
            logger.info("match augment_id=%s performance=%s popular=%s",
                        augment_id, stats.performance, stats.popular)
        return StatsResult(stats, status, "memory")

    async def get_augment_stats(self, champion_id: int,
                                augment_id: int) -> StatsResult:
        """Memory -> SQLite -> Network。"""
        if self._champion_mismatch(champion_id):
            logger.warning("拒绝跨英雄查询(Memory->SQLite->Network): "
                           "active=%s 请求=%s", self.active_champion_id, champion_id)
            return StatsResult(None, CacheStatus.MISSING, "none")
        stats = self.get_cached(champion_id, augment_id)
        if stats is not None:
            return StatsResult(stats, self.cache_status(champion_id), "memory")

        rows = self._load_from_sqlite(champion_id)
        if rows:
            self._memory.setdefault(champion_id, {}).update(rows)
        stats = self.get_cached(champion_id, augment_id)
        if stats is not None:
            status = self.cache_status(champion_id)
            if status is CacheStatus.STALE:
                # 先用旧值，同时后台刷新（不阻塞调用方）
                self.schedule_background_refresh(champion_id)
            return StatsResult(stats, status, "sqlite")

        result = await self.preload_champion(champion_id)
        stats = self.get_cached(champion_id, augment_id)
        if stats is not None:
            return StatsResult(stats, result.status, "network")
        return StatsResult(None, CacheStatus.MISSING, "none")

    def offers_with_stats(self, champion_id: int, augment_ids) -> list[dict]:
        """组装 Overlay 数据；缺失字段保持 None（UI 显示"暂无"）。"""
        out = []
        for augment_id in augment_ids:
            stats = self.get_cached(champion_id, augment_id)
            out.append({
                "augment_id": augment_id,
                "performance": stats.performance if stats else None,
                "popular": stats.popular if stats else None,
                "opgg_augment_tier": stats.opgg_augment_tier if stats else None,
                "scope": self.augment_scope_label,
                "cache_status": self.cache_status(champion_id).value,
            })
        return out

    # -- 后台刷新（stale-while-revalidate） --------------------------------
    def schedule_background_refresh(self, champion_id: int) -> bool:
        """STALE 时先用旧值，同时后台刷新；不阻塞调用方。"""
        if self.cache_status(champion_id) is CacheStatus.FRESH:
            return False
        task = self._refresh_tasks.get(champion_id)
        if task is not None and not task.done():
            return False
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return False
        task = loop.create_task(self._safe_refresh(champion_id))
        self._refresh_tasks[champion_id] = task
        logger.info("缓存 STALE，已安排后台刷新 (championId=%s)", champion_id)
        return True

    async def _safe_refresh(self, champion_id: int) -> None:
        try:
            await self.preload_champion(champion_id, force=True)
        except Exception as exc:  # noqa: BLE001 - 后台任务绝不能冒泡
            logger.warning("后台刷新失败 (championId=%s): %s", champion_id, exc)

    async def aclose(self) -> None:
        for task in list(self._preload_tasks.values()):
            if not task.done():
                task.cancel()
        self._preload_tasks.clear()
        for task in list(self._refresh_tasks.values()):
            if not task.done():
                task.cancel()
        self._refresh_tasks.clear()

    # -- SQLite -------------------------------------------------------------
    def _load_from_sqlite(self, champion_id: int) -> dict[int, OPGGAugmentStats]:
        if self.stats_repository is None:
            return {}
        try:
            rows = self.stats_repository.list_for_champion(champion_id,
                                                           patch=self.data_patch)
        except Exception as exc:  # noqa: BLE001
            logger.warning("读取 SQLite 失败: %s", exc)
            return {}
        out: dict[int, OPGGAugmentStats] = {}
        newest: Optional[datetime] = None
        for row in rows:
            scope_label = row.get("scope") or self.augment_scope_label
            if scope_label != self.augment_scope_label:
                continue      # 绝不把 KR/IBSG 的英雄口径当成海克斯口径
            out[row["augment_id"]] = OPGGAugmentStats(
                augment_id=row["augment_id"],
                performance=row["performance"],
                popular=row["popular"],
                opgg_augment_tier=row["opgg_augment_tier"],
                source=row["source"],
                scope=DataScope.aggregate_scope(),
                patch=row["patch"],
                fetched_at=_parse_dt(row["fetched_at"]),
            )
            stamp = _parse_dt(row["fetched_at"])
            if newest is None or stamp > newest:
                newest = stamp
        if newest is not None:
            self._memory_fetched_at.setdefault(champion_id, newest)
        return out


__all__ = ["OpggProvider", "StatsResult", "PreloadResult", "CacheStatus",
           "StatsState", "RETRY_BACKOFF"]
