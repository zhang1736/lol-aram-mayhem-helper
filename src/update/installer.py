"""文件级增量更新安装器（安全路径 + 备份 + 回滚 + 用户数据保护）。

更新包格式（zip）：
    patch.json
    payload/<相对路径>...

patch.json:
    {
      "version": "1.0.2",
      "update_type": "incremental" | "full",
      "from_versions": ["1.0.1"],
      "operations": [
        {"op": "add"|"replace", "path": "_internal/xx.pyd", "size": 123, "sha256": "..."},
        {"op": "delete", "path": "_internal/old.dll"}
      ]
    }

安全约束：
    * 只允许安装目录内的相对路径；拒绝绝对路径、盘符、`..`、ADS(`:`)
    * 绝不允许覆盖用户数据（数据库/用户配置/日志/备份目录）
    * 先校验全部文件（大小+SHA-256）→ 备份 → 应用；任何一步失败都回滚
"""

from __future__ import annotations

import json
import os
import shutil
import time
import zipfile
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Iterable, Optional

from src.update.checksum import sha256_file
from src.utils.logger import get_tagged

logger = get_tagged("update", __name__)

PATCH_NAME = "patch.json"
PAYLOAD_PREFIX = "payload/"
BACKUP_DIR_NAME = "_update_backup"

#: 绝不能由更新包写入/删除的路径（相对安装目录）
PROTECTED_PATHS = (
    "data/aram_helper.db",
    "data/aram_helper.db-wal",
    "data/aram_helper.db-shm",
    "config/user_config.yaml",
    "logs",
    BACKUP_DIR_NAME,
)
PROTECTED_PREFIXES = ("logs/", BACKUP_DIR_NAME + "/", "data/aram_helper.db")


class UpdateApplyError(RuntimeError):
    pass


@dataclass
class Operation:
    op: str                     # add / replace / delete
    path: str                   # 相对安装目录（POSIX 形式）
    size: Optional[int] = None
    sha256: Optional[str] = None


@dataclass
class UpdatePlan:
    version: str
    update_type: str
    from_versions: list[str] = field(default_factory=list)
    operations: list[Operation] = field(default_factory=list)

    @property
    def adds(self) -> list[Operation]:
        return [o for o in self.operations if o.op == "add"]

    @property
    def replaces(self) -> list[Operation]:
        return [o for o in self.operations if o.op == "replace"]

    @property
    def deletes(self) -> list[Operation]:
        return [o for o in self.operations if o.op == "delete"]


def safe_relative_path(raw: str) -> PurePosixPath:
    """校验并规范化更新包内的相对路径。"""
    if not isinstance(raw, str) or not raw.strip():
        raise UpdateApplyError("更新包包含空路径")
    text = raw.replace("\\", "/").strip()
    if text.startswith("/") or text.startswith("//"):
        raise UpdateApplyError("更新包包含绝对路径: %r" % raw)
    if len(text) >= 2 and text[1] == ":":
        raise UpdateApplyError("更新包包含盘符路径: %r" % raw)
    path = PurePosixPath(text)
    parts = [p for p in path.parts if p not in ("", ".")]
    if not parts:
        raise UpdateApplyError("更新包包含空路径")
    for part in parts:
        if part == "..":
            raise UpdateApplyError("更新包包含路径穿越: %r" % raw)
        if ":" in part:
            raise UpdateApplyError("更新包包含非法路径字符: %r" % raw)
    normalized = "/".join(parts)
    if _is_protected(normalized):
        raise UpdateApplyError("更新包试图修改受保护的用户数据: %r" % raw)
    return PurePosixPath(normalized)


def _is_protected(relative: str) -> bool:
    lowered = relative.lower()
    for item in PROTECTED_PATHS:
        lowered_path = item.lower()
        if lowered == lowered_path or lowered.startswith(lowered_path + "/"):
            return True
    return any(lowered.startswith(prefix) for prefix in PROTECTED_PREFIXES)


def read_plan(package: Path) -> UpdatePlan:
    package = Path(package)
    if not package.exists():
        raise UpdateApplyError("更新包不存在: %s" % package)
    try:
        with zipfile.ZipFile(package) as zf:
            names = zf.namelist()
            if PATCH_NAME not in names:
                raise UpdateApplyError("更新包缺少 %s" % PATCH_NAME)
            info = zf.getinfo(PATCH_NAME)
            if info.file_size > 1024 * 1024:
                raise UpdateApplyError("patch.json 过大")
            payload = json.loads(zf.read(PATCH_NAME).decode("utf-8"))
    except zipfile.BadZipFile as exc:
        raise UpdateApplyError("更新包不是有效 ZIP（可能损坏）") from exc
    except (ValueError, UnicodeDecodeError) as exc:
        raise UpdateApplyError("patch.json 不是合法 JSON") from exc

    if not isinstance(payload, dict):
        raise UpdateApplyError("patch.json 根节点必须是对象")
    version = str(payload.get("version") or "").strip()
    if not version:
        raise UpdateApplyError("patch.json 缺少 version")
    update_type = str(payload.get("update_type") or "incremental").lower()
    if update_type not in ("full", "incremental"):
        raise UpdateApplyError("不支持的 update_type: %r" % update_type)
    from_versions = payload.get("from_versions") or []
    if not isinstance(from_versions, list):
        raise UpdateApplyError("from_versions 必须是数组")
    raw_ops = payload.get("operations")
    if not isinstance(raw_ops, list) or not raw_ops:
        raise UpdateApplyError("更新包没有任何文件操作")

    operations: list[Operation] = []
    seen: set[str] = set()
    for index, row in enumerate(raw_ops):
        if not isinstance(row, dict):
            raise UpdateApplyError("operations[%d] 必须是对象" % index)
        op = str(row.get("op") or "").lower()
        if op not in ("add", "replace", "delete"):
            raise UpdateApplyError("operations[%d] 非法操作: %r" % (index, op))
        path = str(safe_relative_path(row.get("path")))
        if path in seen:
            raise UpdateApplyError("operations 重复路径: %s" % path)
        seen.add(path)
        size = row.get("size")
        if size is not None:
            try:
                size = int(size)
            except (TypeError, ValueError) as exc:
                raise UpdateApplyError("operations[%d].size 非法" % index) from exc
        sha = row.get("sha256")
        if op in ("add", "replace"):
            if not isinstance(sha, str) or len(sha) != 64:
                raise UpdateApplyError("operations[%d] 缺少合法 sha256" % index)
        operations.append(Operation(op, path, size, (sha or "").lower() or None))
    return UpdatePlan(version=version, update_type=update_type,
                      from_versions=[str(v) for v in from_versions],
                      operations=operations)


def verify_payload(package: Path, plan: UpdatePlan) -> None:
    """校验包内每个 add/replace 文件的大小与 SHA-256。"""
    with zipfile.ZipFile(package) as zf:
        names = set(zf.namelist())
        for op in plan.operations:
            if op.op == "delete":
                continue
            member = PAYLOAD_PREFIX + op.path
            if member not in names:
                raise UpdateApplyError("更新包缺少文件: %s" % op.path)
            info = zf.getinfo(member)
            if op.size is not None and info.file_size != op.size:
                raise UpdateApplyError("文件大小不符: %s" % op.path)
            digest = _zip_sha256(zf, member)
            if digest != (op.sha256 or ""):
                raise UpdateApplyError("文件 SHA-256 不符: %s" % op.path)


def _zip_sha256(zf: zipfile.ZipFile, member: str) -> str:
    import hashlib

    digest = hashlib.sha256()
    with zf.open(member) as fh:
        while True:
            block = fh.read(1024 * 1024)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def ensure_compatible(plan: UpdatePlan, current_version: str) -> None:
    if plan.update_type == "incremental" and plan.from_versions and \
            current_version not in plan.from_versions:
        raise UpdateApplyError(
            "增量包不支持从当前版本 %s 升级（支持：%s），请改用完整包"
            % (current_version, ", ".join(plan.from_versions)))


def apply_package(package: Path, install_dir: Path, current_version: str,
                  backup_root: Optional[Path] = None) -> Path:
    """校验并应用更新包；返回备份目录（可用于回滚）。"""
    package = Path(package)
    install_dir = Path(install_dir).resolve()
    if not install_dir.is_dir():
        raise UpdateApplyError("安装目录不存在: %s" % install_dir)

    plan = read_plan(package)
    ensure_compatible(plan, current_version)
    verify_payload(package, plan)

    total_new = sum((op.size or 0) for op in plan.operations if op.op != "delete")
    free = shutil.disk_usage(install_dir).free
    if free < total_new * 2 + 20 * 1024 * 1024:
        raise UpdateApplyError("磁盘空间不足（需要约 %.0f MB）"
                               % (total_new * 2 / 1024 / 1024))

    stamp = time.strftime("%Y%m%d-%H%M%S")
    backup_dir = Path(backup_root) if backup_root else (
        install_dir / BACKUP_DIR_NAME / ("%s-%s" % (plan.version, stamp)))
    backup_dir.mkdir(parents=True, exist_ok=True)
    manifest = {"version": plan.version, "install_dir": str(install_dir),
                "created_at": stamp, "replaced": [], "deleted": [], "added": []}

    # ---- 备份（任何将被覆盖/删除的现有文件都要备份，包括被误标成 add 的路径）----
    for op in plan.operations:
        target = install_dir / op.path
        if op.op == "delete" and not target.exists():
            continue
        if target.exists():
            if target.is_dir():
                raise UpdateApplyError("更新目标不是文件: %s" % op.path)
            dest = backup_dir / op.path
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(target, dest)
            manifest["deleted" if op.op == "delete" else "replaced"].append(op.path)

    (backup_dir / "backup.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    applied: list[Operation] = []
    try:
        with zipfile.ZipFile(package) as zf:
            for op in plan.operations:
                target = install_dir / op.path
                target.parent.mkdir(parents=True, exist_ok=True)
                if op.op == "delete":
                    if target.exists():
                        target.unlink()
                    continue
                existed = target.exists()
                temp = target.with_name(target.name + ".update-new")
                with zf.open(PAYLOAD_PREFIX + op.path) as src, open(temp, "wb") as dst:
                    shutil.copyfileobj(src, dst, 1024 * 1024)
                os.replace(temp, target)
                applied.append(op)
                if op.op == "add" and not existed:
                    manifest["added"].append(op.path)
        (backup_dir / "backup.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as exc:  # noqa: BLE001
        logger.error("应用更新失败，开始回滚: %s", exc)
        rollback(install_dir, backup_dir)
        raise UpdateApplyError("应用更新失败（已回滚）：%s" % exc) from exc

    logger.info("更新已应用: %s -> %s（新增 %d / 替换 %d / 删除 %d）",
                current_version, plan.version, len(plan.adds), len(plan.replaces),
                len(plan.deletes))
    return backup_dir


def rollback(install_dir: Path, backup_dir: Path) -> None:
    """按 backup.json 恢复被替换/删除的文件，并移除新增的文件。"""
    install_dir = Path(install_dir)
    backup_dir = Path(backup_dir)
    state_path = backup_dir / "backup.json"
    if not state_path.exists():
        raise UpdateApplyError("找不到备份清单，无法回滚: %s" % state_path)
    state = json.loads(state_path.read_text(encoding="utf-8"))

    for relative in state.get("added", []):
        try:
            (install_dir / safe_relative_path(relative)).unlink(missing_ok=True)
        except Exception as exc:  # noqa: BLE001
            logger.warning("回滚删除新增文件失败 %s: %s", relative, exc)
    for key in ("replaced", "deleted"):
        for relative in state.get(key, []):
            source = backup_dir / safe_relative_path(relative)
            target = install_dir / safe_relative_path(relative)
            if not source.exists():
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
    logger.warning("已回滚到更新前状态（备份: %s）", backup_dir)


def cleanup_old_backups(install_dir: Path, keep: int = 2) -> None:
    """保留最近 N 份备份，删除更老的（失败不影响主流程）。"""
    root = Path(install_dir) / BACKUP_DIR_NAME
    if not root.is_dir():
        return
    dirs = sorted((p for p in root.iterdir() if p.is_dir()),
                  key=lambda p: p.stat().st_mtime, reverse=True)
    for old in dirs[keep:]:
        shutil.rmtree(old, ignore_errors=True)


__all__ = ["Operation", "UpdatePlan", "UpdateApplyError", "read_plan",
           "verify_payload", "ensure_compatible", "apply_package", "rollback",
           "safe_relative_path", "cleanup_old_backups", "PATCH_NAME",
           "PAYLOAD_PREFIX", "BACKUP_DIR_NAME"]
