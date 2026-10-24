"""增量安装器：新增/替换/删除、路径安全、用户数据保护、回滚。"""

from __future__ import annotations

import json
import os
import zipfile
from pathlib import Path

import pytest

from src.update.installer import (
    UpdateApplyError,
    apply_package,
    cleanup_old_backups,
    read_plan,
    rollback,
    safe_relative_path,
    verify_payload,
)
from tests.update_helpers import make_package, mark_existing


def _install_dir(tmp_path: Path) -> Path:
    root = tmp_path / "app"
    (root / "_internal").mkdir(parents=True)
    (root / "LOL海克斯助手.exe").write_bytes(b"old-exe")
    (root / "_internal" / "old.dll").write_bytes(b"old-dll")
    (root / "_internal" / "keep.dll").write_bytes(b"keep")
    (root / "data").mkdir()
    (root / "data" / "aram_helper.db").write_bytes(b"user-db")
    (root / "config").mkdir()
    (root / "config" / "user_config.yaml").write_text("x: 1\n", encoding="utf-8")
    return root


def test_apply_add_replace_delete(tmp_path: Path) -> None:
    root = _install_dir(tmp_path)
    mark_existing("LOL海克斯助手.exe")
    package = make_package(tmp_path / "upd.zip", version="1.0.1",
                           files={"LOL海克斯助手.exe": b"new-exe",
                                  "_internal/new.dll": b"new-dll"},
                           deletes=["_internal/old.dll"])
    backup = apply_package(package, root, "1.0.0")

    assert (root / "LOL海克斯助手.exe").read_bytes() == b"new-exe"
    assert (root / "_internal" / "new.dll").read_bytes() == b"new-dll"
    assert not (root / "_internal" / "old.dll").exists()
    assert (root / "_internal" / "keep.dll").read_bytes() == b"keep"
    # 用户数据完全不动
    assert (root / "data" / "aram_helper.db").read_bytes() == b"user-db"
    assert (root / "config" / "user_config.yaml").read_text(encoding="utf-8") == "x: 1\n"
    assert backup.exists() and (backup / "backup.json").exists()


def test_rollback_restores_everything(tmp_path: Path) -> None:
    root = _install_dir(tmp_path)
    mark_existing("LOL海克斯助手.exe")
    package = make_package(tmp_path / "upd.zip", version="1.0.1",
                           files={"LOL海克斯助手.exe": b"new-exe",
                                  "_internal/new.dll": b"new-dll"},
                           deletes=["_internal/old.dll"])
    backup = apply_package(package, root, "1.0.0")
    rollback(root, backup)

    assert (root / "LOL海克斯助手.exe").read_bytes() == b"old-exe"
    assert (root / "_internal" / "old.dll").read_bytes() == b"old-dll"
    assert not (root / "_internal" / "new.dll").exists()


@pytest.mark.parametrize("bad", [
    "../evil.dll", "a/../../evil.dll", "/abs.dll", "C:/abs.dll",
    "data/aram_helper.db", "config/user_config.yaml", "logs/app.log",
    "_update_backup/x.bin", "file.dll:stream",
])
def test_safe_path_rejects_dangerous(bad: str) -> None:
    with pytest.raises(UpdateApplyError):
        safe_relative_path(bad)


@pytest.mark.parametrize("bad", [
    "dist/LOL海克斯助手/LOL海克斯助手.exe",
    "LOL海克斯助手/_internal/base_library.zip",
])
def test_safe_path_rejects_release_root_prefix(bad: str) -> None:
    with pytest.raises(UpdateApplyError, match="发布目录前缀"):
        safe_relative_path(bad)


@pytest.mark.parametrize("bad", ["../evil.dll", "C:/abs.dll", "data/aram_helper.db"])
def test_package_with_unsafe_path_refused(tmp_path: Path, bad: str) -> None:
    root = _install_dir(tmp_path)
    extra = [{"op": "add", "path": bad, "size": 1, "sha256": "a" * 64}]
    package = make_package(tmp_path / "evil.zip", version="1.0.1",
                           files={}, extra_ops=extra)
    with pytest.raises(UpdateApplyError):
        apply_package(package, root, "1.0.0")
    assert (root / "LOL海克斯助手.exe").read_bytes() == b"old-exe"


def test_deleted_even_if_missing(tmp_path: Path) -> None:
    root = _install_dir(tmp_path)
    package = make_package(tmp_path / "u.zip", version="1.0.1",
                           files={}, deletes=["_internal/missing.dll"])
    apply_package(package, root, "1.0.0")          # 不抛异常


def test_incremental_requires_supported_from_version(tmp_path: Path) -> None:
    root = _install_dir(tmp_path)
    package = make_package(tmp_path / "u.zip", version="1.0.1",
                           files={"_internal/new.dll": b"x"},
                           from_versions=["1.0.0"])
    with pytest.raises(UpdateApplyError):
        apply_package(package, root, "0.9.0")


def test_corrupted_payload_size_or_hash_refused(tmp_path: Path) -> None:
    root = _install_dir(tmp_path)
    good = make_package(tmp_path / "good.zip", version="1.0.1",
                        files={"_internal/new.dll": b"payload"})
    plan = read_plan(good)
    # 篡改 payload（保持 patch.json 不变）
    bad = tmp_path / "bad.zip"
    with zipfile.ZipFile(good) as src, zipfile.ZipFile(bad, "w") as dst:
        for item in src.infolist():
            data = src.read(item.filename)
            if item.filename.startswith("payload/"):
                data = b"tampered"
            dst.writestr(item, data)
    with pytest.raises(UpdateApplyError):
        verify_payload(bad, plan)
    with pytest.raises(UpdateApplyError):
        apply_package(bad, root, "1.0.0")
    assert (root / "LOL海克斯助手.exe").read_bytes() == b"old-exe"


def test_missing_patch_json_refused(tmp_path: Path) -> None:
    root = _install_dir(tmp_path)
    package = tmp_path / "empty.zip"
    with zipfile.ZipFile(package, "w") as zf:
        zf.writestr("payload/x", b"x")
    with pytest.raises(UpdateApplyError):
        apply_package(package, root, "1.0.0")


def test_apply_failure_rolls_back(tmp_path: Path, monkeypatch) -> None:
    root = _install_dir(tmp_path)
    package = make_package(tmp_path / "u.zip", version="1.0.1",
                           files={"LOL海克斯助手.exe": b"new-exe",
                                  "_internal/new.dll": b"new-dll"})
    real_replace = os.replace

    def flaky_replace(src, dst):
        if str(dst).endswith("new.dll"):
            raise PermissionError("file locked")
        return real_replace(src, dst)

    monkeypatch.setattr(os, "replace", flaky_replace)
    with pytest.raises(UpdateApplyError):
        apply_package(package, root, "1.0.0")
    monkeypatch.setattr(os, "replace", real_replace)
    assert (root / "LOL海克斯助手.exe").read_bytes() == b"old-exe"


def test_cleanup_old_backups(tmp_path: Path) -> None:
    root = _install_dir(tmp_path)
    backups = []
    for index in range(4):
        path = root / "_update_backup" / ("b%d" % index)
        path.mkdir(parents=True)
        os.utime(path, (1000 + index, 1000 + index))
        backups.append(path)
    cleanup_old_backups(root, keep=2)
    remaining = sorted(p.name for p in (root / "_update_backup").iterdir())
    assert remaining == ["b2", "b3"]


def test_full_package_has_no_from_version_requirement(tmp_path: Path) -> None:
    root = _install_dir(tmp_path)
    package = make_package(tmp_path / "full.zip", version="2.0.0",
                           files={"_internal/new.dll": b"x"},
                           update_type="full", from_versions=[])
    apply_package(package, root, "1.0.0")
    assert (root / "_internal" / "new.dll").exists()
