"""OP.GG HTTP 客户端（Day 4 正式接入）。

Day 1 实测确认：
  海克斯    GET /api/contents/stats/champions/{championId}/aram-augments
            -> data[].{id,tier,performance,popular}
            该接口**无法**按 region/tier 过滤，scope 只能是 "OP.GG Aggregate"
  英雄统计  GET /api/{region}/champions/aram/{championId}/none?tier={tier}
            mode 必须用 "aram"（"aram_mayhem" 会 422）

证书：本机 httpx 自带的 certifi 不被信任，优先使用导出的
      certs/windows_ca_bundle.pem，缺失时回退默认行为。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

import httpx

from src.config.settings import OpggSettings
from src.utils import paths
from src.utils.logger import get_tagged

logger = get_tagged("opgg", __name__)

#: 证书路径必须用 resource_dir()：开发态=项目根，打包态=_MEIPASS
#: （用 __file__ 上溯在打包后会指到 _internal，容易漏带 certs）
CA_BUNDLE = paths.resource_dir() / "certs" / "windows_ca_bundle.pem"

AUGMENTS_PATH = "/api/contents/stats/champions/%d/aram-augments"
CHAMPION_STATS_PATH = "/api/{region}/champions/aram/{champion_id}/none"


def verify_option():
    """httpx 的 verify 参数。

    优先级（2026-09-22 实测三种方式都能连上 OP.GG）：
        1. 导出的 Windows CA bundle（certs/windows_ca_bundle.pem）
        2. 系统证书库（ssl.create_default_context()）—— 打包版如果没带上 certs，
           也**不要**退回 httpx 自带的 certifi（本项目实测本机不被信任）
        3. True（最后兜底）
    """
    if CA_BUNDLE.exists():
        return str(CA_BUNDLE)
    try:
        import ssl

        context = ssl.create_default_context()
        logger.info("CA bundle 缺失，改用系统证书库校验 TLS")
        return context
    except Exception as exc:  # noqa: BLE001
        logger.warning("无法创建系统证书上下文，回退默认校验: %s", exc)
        return True


class OpggError(RuntimeError):
    """OP.GG 请求失败。"""


class OpggClient:
    """OP.GG 数据接口客户端。"""

    def __init__(self, settings: OpggSettings | None = None,
                 timeout: float = 20.0) -> None:
        self.settings = settings or OpggSettings()
        self.timeout = timeout

    def _headers(self) -> dict[str, str]:
        return {
            "Accept": "application/json, text/plain, */*",
            "Origin": "https://op.gg",
            "Referer": "https://op.gg/",
        }

    async def _get_json(self, path: str) -> dict[str, Any]:
        url = self.settings.base_url.rstrip("/") + path
        logger.info("request_start")
        logger.info("url=%s", path)
        try:
            async with httpx.AsyncClient(verify=verify_option(),
                                         timeout=self.timeout,
                                         headers=self._headers()) as client:
                response = await client.get(url)
        except httpx.HTTPError as exc:
            logger.warning("request_failed url=%s: %s: %s", path, type(exc).__name__, exc)
            raise OpggError(f"OP.GG 请求失败: {type(exc).__name__}: {exc}") from exc
        logger.info("status=%s", response.status_code)
        if response.status_code != 200:
            raise OpggError(f"OP.GG 返回 HTTP {response.status_code}: {url}")
        try:
            return response.json()
        except ValueError as exc:
            logger.warning("response 不是 JSON (%s)", path)
            raise OpggError(f"OP.GG 响应不是 JSON: {url}") from exc

    async def get_champion_augments(self, champion_id: int) -> dict[str, Any]:
        """当前英雄的海克斯统计（原始 JSON）。"""
        return await self._get_json(AUGMENTS_PATH % champion_id)

    async def get_champion_stats(self, champion_id: int, region: str = "kr",
                                 tier: str = "ibsg") -> dict[str, Any]:
        """英雄统计（主界面显示口径用；MVP 不参与推荐）。"""
        path = CHAMPION_STATS_PATH.format(region=region, champion_id=champion_id)
        return await self._get_json(f"{path}?tier={tier}")


__all__ = ["OpggClient", "OpggError", "verify_option", "CA_BUNDLE"]
