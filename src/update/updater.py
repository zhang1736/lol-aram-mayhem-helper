"""更新辅助器（Updater.exe）：等待主程序退出 → 校验 → 应用 → 重启 → 失败回滚。

该 EXE 由软件自行启动，用户无需手动运行。
"""

from __future__ import annotations

import argparse
import os
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
DEFAULT_POST_LAUNCH = 10.0


def _log_path(job: UpdateJob) -> Path:
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

        backup = apply_package(job.package, job.install_dir_path, job.from_version)
        log.append("applied; backup=%s" % backup)
        cleanup_old_backups(job.install_dir_path, keep=2)

        exe = job.install_dir_path / job.exe_name
        if not exe.exists():
            raise UpdateApplyError("更新后找不到主程序: %s" % exe)

        launcher = spawn or (lambda path: subprocess.Popen([str(path)],
                                                          cwd=str(path.parent)))
        process = launcher(exe)
        log.append("relaunched %s (pid=%s)" % (exe, getattr(process, "pid", "?")))

        # 启动后观察一小段时间：立即非 0 退出 → 认为新版本不可用 → 回滚
        deadline = time.monotonic() + max(0.0, post_launch)
        while time.monotonic() < deadline:
            code = getattr(process, "poll", lambda: None)()
            if code is not None:
                if int(code) != 0:
                    raise UpdateApplyError("新版本启动后立即退出（code=%s）" % code)
                break
            time.sleep(0.5)
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
        # 尽力回滚（apply_package 内部失败时已自行回滚，这里处理后续步骤失败）
        try:
            backups = sorted((job.install_dir_path / "_update_backup").glob("*"),
                             key=lambda p: p.stat().st_mtime, reverse=True)
            if backups:
                rollback(job.install_dir_path, backups[0])
                log.append("rolled back from %s" % backups[0])
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
        return run_job(Path(args.job), wait=args.wait)
    except Exception:  # noqa: BLE001
        return 1


if __name__ == "__main__":
    sys.exit(main())
