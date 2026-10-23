"""更新功能测试用：构造真实格式的更新包（zip + patch.json + payload）。"""

from __future__ import annotations

import hashlib
import json
import zipfile
from pathlib import Path
from typing import Iterable, Optional, Sequence


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def make_package(target: Path, *, version: str, files: dict[str, bytes] | None = None,
                 deletes: Sequence[str] = (), update_type: str = "incremental",
                 from_versions: Sequence[str] = ("1.0.0",),
                 extra_ops: Optional[Iterable[dict]] = None) -> Path:
    """生成一个符合 installer 格式的更新包。"""
    files = files or {}
    operations = []
    for relative, data in files.items():
        op = "add" if relative not in _existing_hint else "replace"
        operations.append({"op": op, "path": relative, "size": len(data),
                           "sha256": _sha(data)})
    for relative in deletes:
        operations.append({"op": "delete", "path": relative})
    if extra_ops:
        operations.extend(extra_ops)
    patch = {"version": version, "update_type": update_type,
             "from_versions": list(from_versions), "operations": operations}
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("patch.json", json.dumps(patch, ensure_ascii=False, indent=2))
        for relative, data in files.items():
            zf.writestr("payload/" + relative.replace("\\", "/"), data)
    return target


#: 让 make_package 知道哪些路径是"替换"（测试里通常直接指定）
_existing_hint: set[str] = set()


def mark_existing(*paths: str) -> None:
    _existing_hint.clear()
    _existing_hint.update(p.replace("\\", "/") for p in paths)


def sha256_bytes(data: bytes) -> str:
    return _sha(data)


__all__ = ["make_package", "mark_existing", "sha256_bytes"]
