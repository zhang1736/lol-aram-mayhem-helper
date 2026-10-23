"""LCU HTTP 客户端测试（在线部分在客户端未运行时自动跳过）。"""

from __future__ import annotations

import asyncio
import base64

import pytest

from src.lcu.client import LcuClient, basic_auth_header
from src.lcu.endpoints import GAMEFLOW_PHASE

from tests.conftest import requires_lcu


def test_basic_auth_header_encodes_riot_user() -> None:
    header = basic_auth_header("secret")
    assert header.startswith("Basic ")
    decoded = base64.b64decode(header.split(" ", 1)[1]).decode()
    assert decoded == "riot:secret"


def test_connect_without_credentials_fails_cleanly() -> None:
    class _EmptyProvider:
        @staticmethod
        def find_credentials():
            return None

    client = LcuClient(credentials=None, provider=_EmptyProvider())
    assert client.connect() is False
    assert client.last_error


@requires_lcu
def test_connect_and_read_gameflow_phase(lcu_credentials) -> None:
    client = LcuClient(lcu_credentials)
    assert client.connect() is True

    async def _get() -> None:
        try:
            response = await client.get(GAMEFLOW_PHASE)
            assert response.status_code == 200
            assert isinstance(response.json(), str)
            assert await client.is_connected() is True
        finally:
            await client.aclose()

    asyncio.run(_get())


@requires_lcu
def test_missing_token_returns_401(lcu_credentials) -> None:
    """去掉 token 必须得到 401（Day 1 已验证的行为）。"""
    import httpx

    async def _probe() -> int:
        async with httpx.AsyncClient(
            base_url=lcu_credentials.base_url, verify=False, timeout=8.0
        ) as raw:
            response = await raw.get(GAMEFLOW_PHASE)
            return response.status_code

    assert asyncio.run(_probe()) == 401
