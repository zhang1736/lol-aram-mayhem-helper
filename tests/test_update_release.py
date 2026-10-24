"""发布包结构与增量排除规则回归。"""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from tools.make_release import (
    build_installer_zip,
    incremental_operations,
    scan_app,
    validate_app_layout,
    validate_operation_paths,
)


def _app(root: Path, version: bytes = b"1.0.0") -> Path:
    root.mkdir(parents=True)
    (root / "LOL海克斯助手.exe").write_bytes(version)
    (root / "Updater.exe").write_bytes(b"updater")
    internal = root / "_internal"
    internal.mkdir()
    (internal / "base_library.zip").write_bytes(b"base")
    return root


def test_installer_zip_has_exactly_one_app_root(tmp_path: Path) -> None:
    app = _app(tmp_path / "LOL海克斯助手")
    target = build_installer_zip(app, tmp_path / "install.zip")
    with zipfile.ZipFile(target) as zf:
        names = zf.namelist()
    assert "LOL海克斯助手/LOL海克斯助手.exe" in names
    assert "LOL海克斯助手/Updater.exe" in names
    assert "LOL海克斯助手/_internal/base_library.zip" in names
    assert not any(name.startswith("dist/") for name in names)
    assert not any(name.startswith("LOL海克斯助手/LOL海克斯助手/") for name in names)


def test_incremental_can_exclude_busy_old_updater(tmp_path: Path) -> None:
    old = _app(tmp_path / "old", b"old")
    new = _app(tmp_path / "new", b"new")
    (old / "Updater.exe").write_bytes(b"old-updater")
    (new / "Updater.exe").write_bytes(b"new-updater")
    files = scan_app(new)
    old_files = [scan_app(old)]
    operations = incremental_operations(
        files, old_files, excluded={"Updater.exe"})
    paths = {op["path"] for op in operations}
    assert "Updater.exe" not in paths
    assert "LOL海克斯助手.exe" in paths
    assert "_internal/base_library.zip" not in paths


def test_release_layout_rejects_common_dist_prefix(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="发布目录前缀"):
        validate_operation_paths([
            {"path": "dist/LOL海克斯助手/LOL海克斯助手.exe"},
        ])


def test_app_layout_requires_runtime_files(tmp_path: Path) -> None:
    app = _app(tmp_path / "LOL海克斯助手")
    validate_app_layout(app)
    (app / "Updater.exe").unlink()
    with pytest.raises(ValueError, match="Updater.exe"):
        validate_app_layout(app)


__all__: list[str] = []
