"""主程序 → Updater.exe 的握手文件（JSON）。"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Optional


@dataclass
class UpdateJob:
    version: str
    from_version: str
    package_path: str
    sha256: str
    install_dir: str
    exe_name: str
    pid: int
    package_size: Optional[int] = None

    @property
    def install_dir_path(self) -> Path:
        return Path(self.install_dir)

    @property
    def package(self) -> Path:
        return Path(self.package_path)


def write_job(path: Path, job: UpdateJob) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(asdict(job), ensure_ascii=False, indent=2),
                   encoding="utf-8")
    os.replace(tmp, path)
    return path


def read_job(path: Path) -> UpdateJob:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    return UpdateJob(
        version=str(payload["version"]),
        from_version=str(payload.get("from_version") or ""),
        package_path=str(payload["package_path"]),
        sha256=str(payload["sha256"]),
        install_dir=str(payload["install_dir"]),
        exe_name=str(payload.get("exe_name") or "LOL海克斯助手.exe"),
        pid=int(payload.get("pid") or 0),
        package_size=(int(payload["package_size"])
                      if payload.get("package_size") is not None else None),
    )


__all__ = ["UpdateJob", "write_job", "read_job"]
