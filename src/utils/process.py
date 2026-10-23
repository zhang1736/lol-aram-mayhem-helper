"""Windows 进程命令行读取（不依赖 WMI / lockfile / PROCESS_VM_READ）。

背景（Day 1 实测结论）：
  * 国服 Tencent 客户端把 lockfile 写成 0 字节空文件，不能作为主要发现方式。
  * Get-CimInstance / Get-WmiObject / wmic 在受限环境下会被拒绝。
  * PROCESS_QUERY_INFORMATION | PROCESS_VM_READ 会被拒绝（error 5），
    所以 PEB 读取只能作为兜底。

本模块使用两条路径：
  1. CreateToolhelp32Snapshot                -> 枚举进程（不需要 WMI）
  2. NtQueryInformationProcess(class 60)     -> 直接取命令行
     只需要 PROCESS_QUERY_LIMITED_INFORMATION。
     该 API 返回的 UNICODE_STRING.Buffer 指向调用方自己的缓冲区，
     因此不需要 ReadProcessMemory。
  3. （兜底）PEB 读取，需要 PROCESS_VM_READ，失败时静默跳过。

仅支持 Windows；在其它平台返回空结果。
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import os
import re
import sys
from dataclasses import dataclass
from typing import Iterator

IS_WINDOWS = sys.platform == "win32"

TH32CS_SNAPPROCESS = 0x00000002
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
PROCESS_QUERY_INFORMATION = 0x0400
PROCESS_VM_READ = 0x0010
PROCESS_COMMAND_LINE_INFORMATION = 60

MAX_PATH = 260

if IS_WINDOWS:
    _k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _ntdll = ctypes.WinDLL("ntdll", use_last_error=True)
else:  # pragma: no cover - 便于在非 Windows 上 import
    _k32 = None
    _ntdll = None


class PROCESSENTRY32(ctypes.Structure):
    _fields_ = [
        ("dwSize", wt.DWORD),
        ("cntUsage", wt.DWORD),
        ("th32ProcessID", wt.DWORD),
        ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
        ("th32ModuleID", wt.DWORD),
        ("cntThreads", wt.DWORD),
        ("th32ParentProcessID", wt.DWORD),
        ("pcPriClassBase", ctypes.c_long),
        ("dwFlags", wt.DWORD),
        ("szExeFile", ctypes.c_char * MAX_PATH),
    ]


class UNICODE_STRING(ctypes.Structure):
    _fields_ = [
        ("Length", ctypes.c_ushort),
        ("MaximumLength", ctypes.c_ushort),
        ("Buffer", ctypes.c_void_p),
    ]


class PROCESS_BASIC_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("Reserved1", ctypes.c_void_p),
        ("PebBaseAddress", ctypes.c_void_p),
        ("Reserved2", ctypes.c_void_p * 2),
        ("UniqueProcessId", ctypes.c_void_p),
        ("Reserved3", ctypes.c_void_p),
    ]


@dataclass(frozen=True)
class ProcessInfo:
    pid: int
    name: str


def iter_processes() -> Iterator[ProcessInfo]:
    """枚举所有进程（只依赖 toolhelp，不依赖 WMI）。"""
    if not IS_WINDOWS:
        return
    snapshot = _k32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if snapshot == -1:
        return
    entry = PROCESSENTRY32()
    entry.dwSize = ctypes.sizeof(PROCESSENTRY32)
    try:
        ok = _k32.Process32First(snapshot, ctypes.byref(entry))
        while ok:
            yield ProcessInfo(entry.th32ProcessID, entry.szExeFile.decode("mbcs", "replace"))
            ok = _k32.Process32Next(snapshot, ctypes.byref(entry))
    finally:
        _k32.CloseHandle(snapshot)


def _read_command_line_via_query(handle) -> str | None:
    """NtQueryInformationProcess(ProcessCommandLineInformation)。"""
    _ntdll.NtQueryInformationProcess.restype = ctypes.c_long
    _ntdll.NtQueryInformationProcess.argtypes = [
        wt.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        ctypes.c_ulong,
        ctypes.POINTER(ctypes.c_ulong),
    ]
    buf = ctypes.create_string_buffer(65536)
    ret_len = ctypes.c_ulong(0)
    status = _ntdll.NtQueryInformationProcess(
        wt.HANDLE(handle),
        PROCESS_COMMAND_LINE_INFORMATION,
        buf,
        ctypes.sizeof(buf),
        ctypes.byref(ret_len),
    )
    if status != 0:
        return None
    us = ctypes.cast(buf, ctypes.POINTER(UNICODE_STRING)).contents
    if not us.Buffer or not us.Length:
        return None
    return ctypes.wstring_at(us.Buffer, us.Length // 2)


def _read_command_line_via_peb(handle) -> str | None:
    """兜底：读 PEB -> ProcessParameters -> CommandLine（需要 PROCESS_VM_READ）。"""
    pbi = PROCESS_BASIC_INFORMATION()
    ret_len = ctypes.c_ulong(0)
    if _ntdll.NtQueryInformationProcess(wt.HANDLE(handle), 0, ctypes.byref(pbi),
                                        ctypes.sizeof(pbi), ctypes.byref(ret_len)) != 0:
        return None
    if not pbi.PebBaseAddress:
        return None

    def read(address, size):
        out = ctypes.create_string_buffer(size)
        got = ctypes.c_size_t(0)
        if not _k32.ReadProcessMemory(wt.HANDLE(handle), ctypes.c_void_p(address),
                                      out, size, ctypes.byref(got)):
            return None
        return out.raw

    # x64 偏移：PEB.ProcessParameters = 0x20；RTL_USER_PROCESS_PARAMETERS.CommandLine = 0x70
    raw = read(pbi.PebBaseAddress + 0x20, 8)
    if not raw:
        return None
    params = ctypes.c_void_p.from_buffer_copy(raw).value
    if not params:
        return None
    raw = read(params + 0x70, ctypes.sizeof(UNICODE_STRING))
    if not raw:
        return None
    us = UNICODE_STRING.from_buffer_copy(raw)
    if not us.Buffer or not us.Length:
        return None
    raw = read(us.Buffer, us.Length)
    if not raw:
        return None
    return raw.decode("utf-16-le", "replace")


def get_command_line(pid: int) -> str | None:
    """返回指定进程的完整命令行，失败返回 None。"""
    if not IS_WINDOWS:
        return None
    for access in (PROCESS_QUERY_LIMITED_INFORMATION,
                   PROCESS_QUERY_INFORMATION | PROCESS_VM_READ):
        handle = _k32.OpenProcess(access, False, pid)
        if not handle:
            continue
        try:
            cmd = None
            if access == PROCESS_QUERY_LIMITED_INFORMATION:
                cmd = _read_command_line_via_query(handle)
            if not cmd:
                cmd = _read_command_line_via_peb(handle)
            if cmd:
                return cmd
        finally:
            _k32.CloseHandle(handle)
    return None


def find_process_command_line(exe_name: str) -> tuple[ProcessInfo, str] | None:
    """按进程名查找，返回 (进程信息, 命令行)。"""
    target = exe_name.lower()
    for proc in iter_processes():
        if proc.name.lower() != target:
            continue
        cmd = get_command_line(proc.pid)
        if cmd:
            return proc, cmd
    return None


_RE_TOKEN = re.compile(r"--remoting-auth-token=([^\s\"]+)")


def mask_token(text: str | None) -> str | None:
    """把命令行里的 token 掩码，避免写入日志。"""
    if not text:
        return text
    return _RE_TOKEN.sub("--remoting-auth-token=***", text)


def lockfile_paths() -> list[str]:
    """国服可能的 lockfile 位置（仅作为兜底，实测多为 0 字节）。"""
    candidates = [
        r"D:\lol(26)\LeagueClient\lockfile",
        r"D:\lol(26)\lockfile",
        r"D:\lol(26)\Riot Client Data\Metadata\riot client\lockfile",
    ]
    local = os.environ.get("LOCALAPPDATA")
    if local:
        candidates += [
            os.path.join(local, "Riot Games", "Riot Client", "Config", "lockfile"),
            os.path.join(local, "League of Legends", "lockfile"),
        ]
    return candidates
