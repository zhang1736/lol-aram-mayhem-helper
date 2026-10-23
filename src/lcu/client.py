"""LCU HTTP 客户端（httpx，自签名证书，Basic Auth riot:{token}）。"""

from __future__ import annotations

import base64
import logging
from typing import Any

import httpx

from src.lcu.credential import LcuCredentialProvider, LcuCredentials
from src.utils.logger import get_logger

logger = get_logger(__name__)

DEFAULT_TIMEOUT = 8.0


class LcuClient:
    """League Client 本地 API 客户端。

    - 同步 connect() 用于握手探测（一次性）
    - 异步 get()/is_connected() 用于日常轮询
    """

    def __init__(
        self,
        credentials: LcuCredentials | None = None,
        timeout: float = DEFAULT_TIMEOUT,
        provider: LcuCredentialProvider | None = None,
    ) -> None:
        self.credentials = credentials
        self.timeout = timeout
        self._provider = provider or LcuCredentialProvider()
        self._async_client: httpx.AsyncClient | None = None
        self._last_error: str | None = None

    # -- 基础信息 -----------------------------------------------------------
    @property
    def base_url(self) -> str | None:
        return self.credentials.base_url if self.credentials else None

    @property
    def last_error(self) -> str | None:
        return self._last_error

    def _auth(self, credentials: LcuCredentials) -> tuple[str, str]:
        return ("riot", credentials.token)

    # -- 同步握手 -----------------------------------------------------------
    def connect(self) -> bool:
        """发现凭据并验证 LCU 可达。返回是否连接成功。"""
        if self.credentials is None:
            self.credentials = self._provider.find_credentials()
        if self.credentials is None:
            self._last_error = "LeagueClientUx.exe 未运行或无法读取命令行"
            logger.info("LCU 凭据发现失败: %s", self._last_error)
            return False

        logger.info("LCU credentials discovered: %s", self.credentials.safe_repr())
        try:
            with httpx.Client(
                base_url=self.credentials.base_url,
                auth=self._auth(self.credentials),
                verify=False,
                timeout=self.timeout,
            ) as client:
                response = client.get("/lol-gameflow/v1/gameflow-phase")
            if response.status_code == 200:
                logger.info("LCU connected (port=%s)", self.credentials.port)
                return True
            self._last_error = f"HTTP {response.status_code}"
            logger.warning("LCU 握手失败: %s", self._last_error)
            return False
        except httpx.HTTPError as exc:
            self._last_error = f"{type(exc).__name__}: {exc}"
            logger.warning("LCU 握手异常: %s", self._last_error)
            return False

    # -- 异步访问 -----------------------------------------------------------
    def _client(self) -> httpx.AsyncClient:
        if self.credentials is None:
            raise RuntimeError("尚未连接：请先调用 connect() 或提供 credentials")
        if self._async_client is None or self._async_client.is_closed:
            self._async_client = httpx.AsyncClient(
                base_url=self.credentials.base_url,
                auth=self._auth(self.credentials),
                verify=False,
                timeout=self.timeout,
                headers={"Accept": "application/json"},
            )
        return self._async_client

    async def get(self, path: str) -> httpx.Response:
        """GET 任意 LCU 端点。返回原始响应（404 等状态不抛异常）。"""
        response = await self._client().get(path)
        logger.debug("GET %s -> %s", path, response.status_code)
        return response

    async def get_json(self, path: str) -> tuple[int, Any | None]:
        """GET 并解析 JSON；非 2xx 或解析失败时 data 为 None。"""
        response = await self.get(path)
        if response.status_code != 200:
            return response.status_code, None
        try:
            return response.status_code, response.json()
        except ValueError:
            return response.status_code, None

    async def is_connected(self) -> bool:
        """异步探活。"""
        if self.credentials is None:
            return False
        try:
            response = await self.get("/lol-gameflow/v1/gameflow-phase")
        except (httpx.HTTPError, RuntimeError) as exc:
            self._last_error = f"{type(exc).__name__}: {exc}"
            return False
        return response.status_code == 200

    async def aclose(self) -> None:
        if self._async_client is not None and not self._async_client.is_closed:
            await self._async_client.aclose()
        self._async_client = None

    # -- 上下文管理 ---------------------------------------------------------
    async def __aenter__(self) -> "LcuClient":
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()


def basic_auth_header(token: str, user: str = "riot") -> str:
    """便于测试/调试：生成 Basic Auth 头。"""
    raw = f"{user}:{token}".encode("utf-8")
    return "Basic " + base64.b64encode(raw).decode("ascii")


__all__ = ["LcuClient", "basic_auth_header"]
