"""LCU 凭据发现（国服 Tencent 客户端）。

实测结论（Day 1）：
  * 国服 lockfile 是 0 字节空文件 -> 不能作为主要发现方式
  * WMI / Get-CimInstance / Get-WmiObject / wmic 在受限环境下被拒绝
  * 正确方式：Toolhelp32Snapshot + NtQueryInformationProcess(ProcessCommandLineInformation)
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from src.utils import process as proc_utils
from src.utils.logger import get_logger

logger = get_logger(__name__)

DEFAULT_PROCESS_NAME = "LeagueClientUx.exe"

RE_APP_PORT = re.compile(r"--app-port=(\d+)")
RE_TOKEN = re.compile(r"--remoting-auth-token=([^\s\"]+)")
RE_REGION = re.compile(r"--region=([\w\-]+)")
RE_LOCALE = re.compile(r"--locale=([\w\-]+)")
RE_APP_PID = re.compile(r"--app-pid=(\d+)")
RE_RSO = re.compile(r"--rso[_-]platform[_-]id=([\w\-]+)", re.IGNORECASE)


@dataclass(frozen=True)
class LcuCredentials:
    """LCU 连接凭据。

    port/token/protocol 是连接所必需的；region/locale/rso_platform_id 只是
    客户端环境信息（不参与认证），保留用于诊断与日志。
    """

    port: int
    token: str
    protocol: str = "https"
    region: str = ""
    locale: str = ""
    rso_platform_id: str = ""
    app_pid: int | None = None
    pid: int | None = None
    process_name: str = ""
    source: str = "process-commandline"

    @property
    def base_url(self) -> str:
        return f"{self.protocol}://127.0.0.1:{self.port}"

    @property
    def token_length(self) -> int:
        return len(self.token)

    def safe_repr(self) -> str:
        """可安全写日志的表示（不含 token）。"""
        return (f"LcuCredentials(port={self.port}, protocol={self.protocol}, "
                f"region={self.region or '-'}, locale={self.locale or '-'}, "
                f"rso_platform_id={self.rso_platform_id or '-'}, "
                f"token_length={self.token_length}, source={self.source})")


def parse_command_line(command_line: str) -> LcuCredentials | None:
    """从 LeagueClientUx 命令行解析凭据；缺少 port/token 时返回 None。"""
    if not command_line:
        return None
    port_match = RE_APP_PORT.search(command_line)
    token_match = RE_TOKEN.search(command_line)
    if not port_match or not token_match:
        return None

    def first(regex: re.Pattern[str]) -> str:
        match = regex.search(command_line)
        return match.group(1) if match else ""

    pid_text = first(RE_APP_PID)
    return LcuCredentials(
        port=int(port_match.group(1)),
        token=token_match.group(1),
        protocol="https",
        region=first(RE_REGION),
        locale=first(RE_LOCALE),
        rso_platform_id=first(RE_RSO),
        app_pid=int(pid_text) if pid_text.isdigit() else None,
    )


class LcuCredentialProvider:
    """发现本机运行中的 League Client 凭据。"""

    def __init__(self, process_name: str = DEFAULT_PROCESS_NAME,
                 allow_lockfile_fallback: bool = True) -> None:
        self.process_name = process_name
        self.allow_lockfile_fallback = allow_lockfile_fallback

    # -- 主路径 -------------------------------------------------------------
    def _from_process(self) -> LcuCredentials | None:
        found = proc_utils.find_process_command_line(self.process_name)
        if not found:
            return None
        info, command_line = found
        credentials = parse_command_line(command_line)
        if not credentials:
            logger.warning("找到 %s(pid=%s) 但命令行中缺少 app-port/token",
                           info.name, info.pid)
            return None
        credentials = LcuCredentials(
            **{**credentials.__dict__, "pid": info.pid, "process_name": info.name}
        )
        logger.debug("LeagueClientUx 命令行: %s", proc_utils.mask_token(command_line))
        return credentials

    # -- 兜底（实测国服多为 0 字节，通常不会命中） --------------------------
    def _from_lockfile(self) -> LcuCredentials | None:
        for candidate in proc_utils.lockfile_paths():
            path = Path(candidate)
            try:
                if not path.exists() or path.stat().st_size == 0:
                    continue
                parts = path.read_text(encoding="utf-8", errors="replace").strip().split(":")
                if len(parts) < 5 or not parts[2].isdigit():
                    continue
            except OSError:
                continue
            logger.info("通过 lockfile 发现 LCU 凭据: %s", path)
            return LcuCredentials(
                port=int(parts[2]),
                token=parts[3],
                protocol=parts[4] or "https",
                app_pid=int(parts[1]) if parts[1].isdigit() else None,
                process_name=parts[0],
                source="lockfile",
            )
        return None

    def find_credentials(self) -> LcuCredentials | None:
        credentials = self._from_process()
        if credentials:
            return credentials
        if self.allow_lockfile_fallback:
            return self._from_lockfile()
        return None


def find_credentials(process_name: str = DEFAULT_PROCESS_NAME) -> LcuCredentials | None:
    """便捷函数。"""
    return LcuCredentialProvider(process_name).find_credentials()


__all__ = [
    "LcuCredentials",
    "LcuCredentialProvider",
    "find_credentials",
    "parse_command_line",
    "DEFAULT_PROCESS_NAME",
]
