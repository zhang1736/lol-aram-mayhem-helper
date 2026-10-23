"""Updater 辅助器：等待退出 → 应用 → 重启；启动失败自动回滚。"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.update.job import UpdateJob, read_job, write_job
from src.update.updater import run_job, wait_for_exit
from tests.update_helpers import make_package


class FakeProcess:
    def __init__(self, pid: int = 999, exit_code=None) -> None:
        self.pid = pid
        self._exit = exit_code

    def poll(self):
        return self._exit


def _install(tmp_path: Path) -> Path:
    root = tmp_path / "app"
    root.mkdir()
    (root / "LOL海克斯助手.exe").write_bytes(b"old")
    (root / "_internal").mkdir()
    return root


def _job(tmp_path: Path, root: Path, package: Path, sha: str) -> Path:
    job = UpdateJob(version="1.0.1", from_version="1.0.0",
                    package_path=str(package), sha256=sha,
                    install_dir=str(root), exe_name="LOL海克斯助手.exe",
                    pid=0, package_size=package.stat().st_size)
    path = tmp_path / "job.json"
    write_job(path, job)
    return path


def test_job_roundtrip(tmp_path: Path) -> None:
    root = _install(tmp_path)
    package = make_package(tmp_path / "p.zip", version="1.0.1", files={})
    job_path = _job(tmp_path, root, package, "a" * 64)
    job = read_job(job_path)
    assert job.version == "1.0.1"
    assert job.install_dir == str(root)


def test_run_job_applies_and_relaunches(tmp_path: Path) -> None:
    root = _install(tmp_path)
    package = make_package(tmp_path / "p.zip", version="1.0.1",
                           files={"LOL海克斯助手.exe": b"new"})
    from src.update.checksum import sha256_file

    job_path = _job(tmp_path, root, package, sha256_file(package))
    launched = {}

    def spawn(path: Path):
        launched["path"] = path
        return FakeProcess(exit_code=None)

    assert run_job(job_path, wait=False, post_launch=0.1, spawn=spawn) == 0
    assert (root / "LOL海克斯助手.exe").read_bytes() == b"new"
    assert launched["path"].name == "LOL海克斯助手.exe"


def test_run_job_rolls_back_when_new_version_crashes(tmp_path: Path) -> None:
    root = _install(tmp_path)
    package = make_package(tmp_path / "p.zip", version="1.0.1",
                           files={"LOL海克斯助手.exe": b"broken"})
    from src.update.checksum import sha256_file

    job_path = _job(tmp_path, root, package, sha256_file(package))

    def spawn(path: Path):
        return FakeProcess(exit_code=1)

    assert run_job(job_path, wait=False, post_launch=0.1, spawn=spawn) == 1
    assert (root / "LOL海克斯助手.exe").read_bytes() == b"old"


def test_run_job_refuses_bad_sha(tmp_path: Path) -> None:
    root = _install(tmp_path)
    package = make_package(tmp_path / "p.zip", version="1.0.1",
                           files={"LOL海克斯助手.exe": b"new"})
    job_path = _job(tmp_path, root, package, "0" * 64)
    assert run_job(job_path, wait=False, post_launch=0.0) == 1
    assert (root / "LOL海克斯助手.exe").read_bytes() == b"old"


def test_wait_for_exit_returns_true_for_dead_pid() -> None:
    assert wait_for_exit(0, timeout=0.1) is True
