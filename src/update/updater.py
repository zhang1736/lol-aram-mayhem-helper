"""更新辅助器（Updater.exe）：等待主程序退出 → 校验 → 应用 → 重启 → 失败回滚。

该 EXE 由软件自行启动，用户无需手动运行。
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import traceback
from pathlib import Path
from typing import Callable, Optional

from src.update.checksum import sha256_file
from src.update.installer import (
    UpdateApplyError,
    apply_package,
    cleanup_old_backups,
    rollback,
)
from src.update.job import UpdateJob, read_job
from src.utils import paths
from src.utils.logger import get_tagged

logger = get_tagged("update", __name__)

DEFAULT_PID_TIMEOUT = 90.0
DEFAULT_POST_LAUNCH = 20.0


def _log_path(job: UpdateJob) -> Path:
    if job.log_path:
        return Path(job.log_path)
    try:
        return paths.user_data_dir() / "logs" / "update.log"
    except Exception:  # noqa: BLE001
        return job.install_dir_path / "update.log"


def _write_log(job: UpdateJob, lines: list[str]) -> None:
    try:
        target = _log_path(job)
        target.parent.mkdir(parents=True, exist_ok=True)
        with open(target, "a", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")
    except Exception:  # noqa: BLE001
        pass


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        import psutil
    except Exception:  # noqa: BLE001
        return False
    try:
        return psutil.pid_exists(pid) and not psutil.Process(pid).status() == \
            psutil.STATUS_ZOMBIE
    except Exception:  # noqa: BLE001
        return False


def wait_for_exit(pid: int, timeout: float = DEFAULT_PID_TIMEOUT) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _pid_alive(pid):
            return True
        time.sleep(0.5)
    return not _pid_alive(pid)


def _startup_marker(job: UpdateJob) -> Optional[Path]:
    if not job.startup_token:
        return None
    marker_dir = job.work_dir or str(job.package.parent / "startup")
    return Path(marker_dir) / ("startup-%s.json" % job.startup_token)


def _read_startup_marker(job: UpdateJob,
                         process_pid: Optional[int] = None) -> Optional[dict]:
    marker = _startup_marker(job)
    if marker is None or not marker.exists():
        return None
    try:
        payload = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if str(payload.get("version")) != job.version:
        return None
    expected_exe = (job.install_dir_path / job.exe_name).resolve()
    try:
        actual_exe = Path(str(payload.get("exe"))).resolve()
    except Exception:  # noqa: BLE001
        return None
    if actual_exe != expected_exe:
        return None
    try:
        marker_pid = int(payload.get("pid"))
    except (TypeError, ValueError):
        return None
    if process_pid is not None and marker_pid != int(process_pid):
        return None
    return payload


def _spawn_new_version(exe: Path, job: UpdateJob) -> object:
    env = os.environ.copy()
    if job.startup_token:
        from src.update.startup import MARKER_DIR_ENV, TOKEN_ENV

        env[TOKEN_ENV] = job.startup_token
        env[MARKER_DIR_ENV] = str(_startup_marker(job).parent)  # type: ignore[union-attr]
    flags = 0
    if os.name == "nt":
        flags = (getattr(subprocess, "DETACHED_PROCESS", 0)
                 | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
    return subprocess.Popen([str(exe)], cwd=str(exe.parent), env=env,
                            close_fds=True, creationflags=flags)


def bootstrap_from_temp(job_path: Path, job: UpdateJob) -> bool:
    """若当前可执行文件就是安装目录中的 Updater，则复制到外部再运行。"""
    if not getattr(sys, "frozen", False):
        return False
    current = Path(sys.executable).resolve()
    installed = (job.install_dir_path / "Updater.exe").resolve()
    if current != installed:
        return False
    work_dir = Path(job.work_dir or job.package.parent / "updater-runtime")
    work_dir.mkdir(parents=True, exist_ok=True)
    copied = work_dir / ("Updater-%s-%s.exe" % (os.getpid(), int(time.time() * 1000)))
    shutil.copy2(current, copied)
    env = os.environ.copy()
    env["LOL_HEX_HELPER_UPDATER_BOOTSTRAPPED"] = "1"
    flags = 0
    if os.name == "nt":
        flags = (getattr(subprocess, "DETACHED_PROCESS", 0)
                 | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
    subprocess.Popen([str(copied), "--job", str(job_path)],
                     cwd=str(work_dir), env=env, close_fds=True,
                     creationflags=flags)
    return True


def run_job(job_path: Path, *, wait: bool = True,
            pid_timeout: float = DEFAULT_PID_TIMEOUT,
            post_launch: float = DEFAULT_POST_LAUNCH,
            spawn: Optional[Callable[[Path], object]] = None) -> int:
    """执行更新任务。返回 0=成功，1=失败（已回滚）。"""
    started = time.strftime("%Y-%m-%d %H:%M:%S")
    job = read_job(job_path)
    log: list[str] = ["", "=== update %s ===" % started,
                      "from=%s to=%s install=%s" % (job.from_version, job.version,
                                                    job.install_dir)]
    applied_backup: Optional[Path] = None
    process = None
    try:
        if not job.package.exists():
            raise UpdateApplyError("更新包不存在: %s" % job.package)
        digest = sha256_file(job.package)
        if digest.lower() != job.sha256.lower():
            raise UpdateApplyError("更新包 SHA-256 校验失败，拒绝安装")
        log.append("package sha256 ok (%s)" % digest)

        if wait and not wait_for_exit(job.pid, pid_timeout):
            raise UpdateApplyError("主程序在 %.0fs 内未退出（文件可能被占用）" % pid_timeout)
        log.append("main process exited")

        applied_backup = apply_package(
            job.package, job.install_dir_path, job.from_version)
        log.append("applied; backup=%s" % applied_backup)
        cleanup_old_backups(job.install_dir_path, keep=2)

        exe = job.install_dir_path / job.exe_name
        if not exe.exists():
            raise UpdateApplyError("更新后找不到主程序: %s" % exe)

        if spawn is not None:
            process = spawn(exe)
        else:
            process = _spawn_new_version(exe, job)
        log.append("relaunched %s (pid=%s)" % (exe, getattr(process, "pid", "?")))

        # 启动健康和版本握手：不能只把 Popen 返回当作成功。
        deadline = time.monotonic() + max(0.0, post_launch)
        marker_seen = False
        while time.monotonic() < deadline:
            code = getattr(process, "poll", lambda: None)()
            if code is not None:
                if int(code) != 0:
                    raise UpdateApplyError("新版本启动后立即退出（code=%s）" % code)
                if job.startup_token:
                    raise UpdateApplyError(
                        "新版本在完成版本握手前退出（code=0）")
                break
            marker = _read_startup_marker(
                job, getattr(process, "pid", None))
            if marker is not None:
                marker_seen = True
                log.append("startup handshake ok (version=%s pid=%s)" %
                           (marker.get("version"), marker.get("pid")))
                break
            time.sleep(0.5)
        if job.startup_token and not marker_seen:
            raise UpdateApplyError(
                "新版本在 %.0fs 内未完成启动握手" % post_launch)
        log.append("update successful")
        _write_log(job, log)
        try:
            job.package.unlink(missing_ok=True)
        except OSError:
            pass
        return 0
    except Exception as exc:  # noqa: BLE001
        log.append("FAILED: %s" % exc)
        log.append(traceback.format_exc())
        logger.error("更新失败: %s", exc)
        # 失败后先终止未通过健康检查的新进程，再恢复旧版本并重新启动。
        if process is not None:
            try:
                if getattr(process, "poll", lambda: 0)() is None:
                    process.terminate()  # type: ignore[attr-defined]
                    process.wait(timeout=5)  # type: ignore[attr-defined]
                    log.append("terminated failed new process")
            except Exception as terminate_exc:  # noqa: BLE001
                log.append("terminate failed: %s" % terminate_exc)
        try:
            backups = sorted((job.install_dir_path / "_update_backup").glob("*"),
                             key=lambda p: p.stat().st_mtime, reverse=True)
            if backups:
                rollback(job.install_dir_path, backups[0])
                log.append("rolled back from %s" % backups[0])
                restored = job.install_dir_path / job.exe_name
                if restored.exists():
                    try:
                        restored_process = (spawn(restored) if spawn is not None
                                            else _spawn_new_version(restored, job))
                        log.append("restored old version pid=%s" %
                                   getattr(restored_process, "pid", "?"))
                    except Exception as start_exc:  # noqa: BLE001
                        log.append("restore launch failed: %s" % start_exc)
        except Exception as rollback_exc:  # noqa: BLE001
            log.append("ROLLBACK FAILED: %s" % rollback_exc)
        _write_log(job, log)
        return 1


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="Updater")
    parser.add_argument("--job", required=True, help="更新任务 JSON 路径")
    parser.add_argument("--wait", action="store_true", default=True)
    parser.add_argument("--no-wait", dest="wait", action="store_false")
    args = parser.parse_args(argv)
    try:
        job_path = Path(args.job)
        job = read_job(job_path)
        if (os.environ.get("LOL_HEX_HELPER_UPDATER_BOOTSTRAPPED") != "1"
                and bootstrap_from_temp(job_path, job)):
            return 0
        return run_job(job_path, wait=args.wait)
    except Exception as exc:  # noqa: BLE001
        logger.error("Updater 启动失败: %s", exc, exc_info=True)
        return 1


if __name__ == "__main__":
    sys.exit(main())
