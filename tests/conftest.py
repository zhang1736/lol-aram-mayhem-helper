"""共享 fixtures 与「需要真实客户端」的跳过标记。"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.data.database import Database  # noqa: E402
from src.lcu.credential import LcuCredentialProvider, LcuCredentials  # noqa: E402
from src.utils.logger import force_utf8_stdio  # noqa: E402

force_utf8_stdio()


@pytest.fixture()
def temp_database(tmp_path: Path) -> Database:
    database = Database(tmp_path / "test_aram_helper.db")
    database.initialize()
    yield database
    database.close()


def _live_credentials() -> LcuCredentials | None:
    try:
        return LcuCredentialProvider().find_credentials()
    except Exception:  # noqa: BLE001
        return None


@pytest.fixture(scope="session")
def lcu_credentials() -> LcuCredentials:
    credentials = _live_credentials()
    if credentials is None:
        pytest.skip("本机未发现运行中的 LeagueClientUx.exe，跳过在线测试")
    return credentials


requires_lcu = pytest.mark.skipif(
    _live_credentials() is None,
    reason="本机未发现运行中的 LeagueClientUx.exe，跳过在线测试",
)
