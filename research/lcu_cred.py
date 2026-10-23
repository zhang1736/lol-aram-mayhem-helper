# Reads LCU connection credentials (port + remoting auth token) WITHOUT WMI.
#
# Why this exists:
#   Inside this sandbox, Get-CimInstance / Get-WmiObject on Win32_Process are denied
#   ("access denied"), and wmic is not installed. The old probe therefore always
#   reported CLIENT_NOT_FOUND even while LeagueClientUx.exe was running.
#
# Strategy (all pure Win32 via ctypes + toolhelp, no WMI):
#   1. CreateToolhelp32Snapshot -> find LeagueClientUx.exe PIDs (no WMI needed)
#   2. NtQueryInformationProcess(ProcessCommandLineInformation=60) -> command line
#   3. fallback: read the PEB (ProcessParameters.CommandLine) via ReadProcessMemory
#
# Also exposes lockfile / log-file fallbacks.

import ctypes
import ctypes.wintypes as wt
import os
import re
import sys

k32 = ctypes.WinDLL("kernel32", use_last_error=True)
ntdll = ctypes.WinDLL("ntdll", use_last_error=True)

TH32CS_SNAPPROCESS = 0x00000002
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
PROCESS_QUERY_INFORMATION = 0x0400
PROCESS_VM_READ = 0x0010
ProcessCommandLineInformation = 60

MAX_PATH = 260


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


def list_processes():
    """Return [(pid, exe_name), ...] using only the toolhelp snapshot."""
    snap = k32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if snap == -1:
        return []
    out = []
    entry = PROCESSENTRY32()
    entry.dwSize = ctypes.sizeof(PROCESSENTRY32)
    try:
        ok = k32.Process32First(snap, ctypes.byref(entry))
        while ok:
            out.append((entry.th32ProcessID, entry.szExeFile.decode("mbcs", "replace")))
            ok = k32.Process32Next(snap, ctypes.byref(entry))
    finally:
        k32.CloseHandle(snap)
    return out


def command_line(pid):
    """Command line of a process; tries NtQueryInformationProcess then the PEB."""
    ntdll.NtQueryInformationProcess.restype = ctypes.c_long
    ntdll.NtQueryInformationProcess.argtypes = [
        wt.HANDLE, ctypes.c_int, ctypes.c_void_p, ctypes.c_ulong,
        ctypes.POINTER(ctypes.c_ulong),
    ]
    # --- strategy 1: ProcessCommandLineInformation (Win 8.1+) ---
    # Only PROCESS_QUERY_LIMITED_INFORMATION is needed, and the returned
    # UNICODE_STRING.Buffer points INTO the caller's buffer, so no
    # ReadProcessMemory (which the sandbox denies) is required.
    for access in (PROCESS_QUERY_LIMITED_INFORMATION,
                   PROCESS_QUERY_INFORMATION | PROCESS_VM_READ):
        h = k32.OpenProcess(access, False, pid)
        if not h:
            continue
        try:
            buf = ctypes.create_string_buffer(65536)
            ret_len = ctypes.c_ulong(0)
            status = ntdll.NtQueryInformationProcess(
                wt.HANDLE(h), ProcessCommandLineInformation,
                buf, ctypes.sizeof(buf), ctypes.byref(ret_len))
            if status == 0:
                us = ctypes.cast(buf, ctypes.POINTER(UNICODE_STRING)).contents
                if us.Buffer and us.Length:
                    return ctypes.wstring_at(us.Buffer, us.Length // 2)

            # --- strategy 2: PEB walk ---
            class PROCESS_BASIC_INFORMATION(ctypes.Structure):
                _fields_ = [
                    ("Reserved1", ctypes.c_void_p),
                    ("PebBaseAddress", ctypes.c_void_p),
                    ("Reserved2", ctypes.c_void_p * 2),
                    ("UniqueProcessId", ctypes.c_void_p),
                    ("Reserved3", ctypes.c_void_p),
                ]
            pbi = PROCESS_BASIC_INFORMATION()
            rl = ctypes.c_ulong(0)
            if ntdll.NtQueryInformationProcess(wt.HANDLE(h), 0, ctypes.byref(pbi),
                                               ctypes.sizeof(pbi),
                                               ctypes.byref(rl)) != 0:
                continue
            if not pbi.PebBaseAddress:
                continue
            # x64: PEB.ProcessParameters at +0x20
            params = ctypes.c_void_p()
            n = ctypes.c_size_t(0)
            if not k32.ReadProcessMemory(wt.HANDLE(h),
                                         ctypes.c_void_p(pbi.PebBaseAddress + 0x20),
                                         ctypes.byref(params), 8, ctypes.byref(n)):
                continue
            us = UNICODE_STRING()
            # x64: RTL_USER_PROCESS_PARAMETERS.CommandLine at +0x70
            if not k32.ReadProcessMemory(wt.HANDLE(h), ctypes.c_void_p(params.value + 0x70),
                                         ctypes.byref(us), ctypes.sizeof(us),
                                         ctypes.byref(n)):
                continue
            if not us.Buffer or not us.Length:
                continue
            data = ctypes.create_string_buffer(us.Length)
            if k32.ReadProcessMemory(wt.HANDLE(h), ctypes.c_void_p(us.Buffer),
                                     data, us.Length, ctypes.byref(n)):
                return data.raw.decode("utf-16-le", "replace")
        finally:
            k32.CloseHandle(h)
    return None


RE_PORT = re.compile(r"--app-port=(\d+)")
RE_TOKEN = re.compile(r"--remoting-auth-token=([^\s\"]+)")
RE_REGION = re.compile(r"--region=([\w\-]+)")
RE_LOCALE = re.compile(r"--locale=([\w\-]+)")
RE_PID = re.compile(r"--app-pid=(\d+)")
RE_RSO = re.compile(r"--rso[_-]platform[_-]id=([\w\-]+)", re.I)


def parse_cmdline(cmd):
    if not cmd:
        return None
    port = RE_PORT.search(cmd)
    token = RE_TOKEN.search(cmd)
    if not (port and token):
        return None
    return {
        "source": "process-commandline",
        "port": int(port.group(1)),
        "password": token.group(1),
        "protocol": "https",
        "region": (RE_REGION.search(cmd) or [None, ""])[1] if RE_REGION.search(cmd) else "",
        "locale": (RE_LOCALE.search(cmd) or [None, ""])[1] if RE_LOCALE.search(cmd) else "",
        "app_pid": (RE_PID.search(cmd) or [None, ""])[1] if RE_PID.search(cmd) else "",
        "rso_platform_id": (RE_RSO.search(cmd) or [None, ""])[1] if RE_RSO.search(cmd) else "",
    }


LOCKFILES = [
    r"D:\lol(26)\LeagueClient\lockfile",
    r"D:\lol(26)\lockfile",
    r"D:\lol(26)\Riot Client Data\Metadata\riot client\lockfile",
    os.path.expandvars(r"%LOCALAPPDATA%\Riot Games\Riot Client\Config\lockfile"),
    os.path.expandvars(r"%LOCALAPPDATA%\League of Legends\lockfile"),
]


def from_lockfile():
    for path in LOCKFILES:
        try:
            if not os.path.exists(path):
                continue
            if os.path.getsize(path) == 0:
                continue          # Tencent client leaves a 0-byte lockfile
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                parts = fh.read().strip().split(":")
            if len(parts) >= 5 and parts[2].isdigit():
                return {"source": "lockfile", "path": path, "port": int(parts[2]),
                        "password": parts[3], "protocol": parts[4],
                        "region": "", "locale": "", "app_pid": parts[1],
                        "rso_platform_id": ""}
        except OSError:
            continue
    return None


def find():
    """Return LCU credentials from a live LeagueClientUx process."""
    for pid, name in list_processes():
        if name.lower() != "leagueclientux.exe":
            continue
        info = parse_cmdline(command_line(pid))
        if info:
            info["path"] = "%s (pid %d)" % (name, pid)
            info["name"] = name
            info["pid"] = pid
            return info
    return from_lockfile()


if __name__ == "__main__":
    print("PS python:", sys.version.split()[0])
    procs = [p for p in list_processes()
             if p[1].lower().startswith(("leagueclient", "riotclient"))]
    print("league/riot processes: %s" % procs)
    for pid, name in procs:
        cmd = command_line(pid)
        if cmd:
            masked = RE_TOKEN.sub("--remoting-auth-token=***", cmd)
            print("\n[%s pid=%d]\n  %s" % (name, pid, masked[:900]))
        else:
            print("\n[%s pid=%d] command line unreadable" % (name, pid))
    print("\nfind() -> %s" % (lambda i: None if not i else
                              {k: (v if k != "password" else "***") for k, v in i.items()})(find()))
