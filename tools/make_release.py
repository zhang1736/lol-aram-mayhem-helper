"""发布工具：生成文件清单、完整更新包、文件级增量更新包与远程 manifest。

用法（示例）：

    & ".venv\\Scripts\\python.exe" tools\\make_release.py `
        --app-dir "dist\\LOL海克斯助手" `
        --version 1.0.2 `
        --notes "release\\notes-v1.0.2.md" `
        --out "release\\1.0.2" `
        --previous "build\\old-v1.0.1" --from-version 1.0.1 `
        --base-url "https://github.com/<OWNER>/<REPO>/releases/download/v1.0.2"

说明：
  * 完整包 = 全部文件 + patch.json
  * 增量包 = 与所有 --previous 版本做文件级 diff（新增/修改/删除），
    因此可以支持"跳版本升级"：只要用户版本在 from_versions 里即可安全应用
  * 不给 --base-url 时只生成 manifest.template.json（不会伪造可用下载地址）
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
import zipfile
from pathlib import Path, PurePosixPath

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.update.version import parse_version  # noqa: E402

EXCLUDED_DIRS = {"_update_backup"}
EXCLUDED_SUFFIXES = {".update-new", ".part", ".tmp"}


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def scan_app(app_dir: Path) -> dict[str, dict]:
    """扫描安装目录：相对路径 -> {size, sha256}（排除更新备份/临时文件）。"""
    files: dict[str, dict] = {}
    app_dir = Path(app_dir).resolve()
    for root, dirs, names in os.walk(app_dir):
        rel_root = Path(root).relative_to(app_dir)
        dirs[:] = [d for d in dirs if d.lower() not in EXCLUDED_DIRS]
        for name in names:
            if Path(name).suffix.lower() in EXCLUDED_SUFFIXES:
                continue
            path = Path(root) / name
            relative = (rel_root / name).as_posix()
            files[relative] = {"size": path.stat().st_size,
                               "sha256": sha256_file(path)}
    return files


def read_notes(path: Path | None) -> list[str]:
    if path is None:
        return []
    text = Path(path).read_text(encoding="utf-8")
    return [line.strip(" -•\t") for line in text.splitlines()
            if line.strip() and not line.lstrip().startswith("#")]


def build_package(app_dir: Path, target: Path, version: str, *,
                  update_type: str, operations: list[dict],
                  from_versions: list[str]) -> Path:
    """按 installer 的格式打包：patch.json + payload/..."""
    app_dir = Path(app_dir)
    patch = {"version": version, "update_type": update_type,
             "from_versions": list(from_versions), "operations": operations}
    target.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("patch.json", json.dumps(patch, ensure_ascii=False, indent=2))
        for op in operations:
            if op["op"] == "delete":
                continue
            path = app_dir / op["path"]
            zf.write(path, "payload/" + op["path"])
    return target


def full_operations(files: dict[str, dict], old_files: dict[str, dict]) -> list[dict]:
    ops = []
    for relative, meta in sorted(files.items()):
        op = "replace" if relative in old_files else "add"
        ops.append({"op": op, "path": relative, "size": meta["size"],
                    "sha256": meta["sha256"]})
    return ops


def incremental_operations(files: dict[str, dict],
                           old_sets: list[dict[str, dict]],
                           excluded: set[str] | None = None) -> list[dict]:
    """对多个旧版本做"并集 diff"，保证从任一旧版本升级都能得到新文件集。"""
    excluded = {Path(p).as_posix().lower() for p in (excluded or set())}
    ops: list[dict] = []
    old_keys: set[str] = set()
    for old in old_sets:
        old_keys.update(old)
    for relative, meta in sorted(files.items()):
        if relative.lower() in excluded:
            continue
        changed = any((relative not in old) or (old[relative]["sha256"] != meta["sha256"])
                      for old in old_sets)
        if changed:
            op = "replace" if any(relative in old for old in old_sets) else "add"
            ops.append({"op": op, "path": relative, "size": meta["size"],
                        "sha256": meta["sha256"]})
    for relative in sorted(old_keys - set(files)):
        if relative.lower() in excluded:
            continue
        ops.append({"op": "delete", "path": relative})
    return ops


def validate_app_layout(app_dir: Path) -> None:
    """确认 app-dir 本身就是应用根目录，不能再带 dist 前缀。"""
    app_dir = Path(app_dir)
    for required in ("LOL海克斯助手.exe", "Updater.exe", "_internal"):
        if not (app_dir / required).exists():
            raise ValueError("应用目录缺少必要文件: %s" % required)
    if app_dir.name.lower() == "dist":
        raise ValueError("app-dir 不能直接指向 dist，应指向 dist/LOL海克斯助手")


def validate_operation_paths(operations: list[dict]) -> None:
    """拒绝把公共发布目录前缀写进增量目标路径。"""
    bad_prefixes = ("dist/", "lol海克斯助手/", "dist/lol海克斯助手/")
    for op in operations:
        raw = str(op.get("path") or "").replace("\\", "/")
        lowered = raw.lower()
        if any(lowered.startswith(prefix) for prefix in bad_prefixes):
            raise ValueError(
                "更新包路径包含发布目录前缀，拒绝生成: %s" % raw)
        if raw.startswith("/") or ".." in PurePosixPath(raw).parts:
            raise ValueError("更新包路径不安全: %s" % raw)


def build_installer_zip(app_dir: Path, target: Path) -> Path:
    """生成首次安装 ZIP；只保留一个应用根目录。"""
    app_dir = Path(app_dir)
    validate_app_layout(app_dir)
    target.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as zf:
        for root, dirs, names in os.walk(app_dir):
            dirs.sort()
            for name in sorted(names):
                path = Path(root) / name
                relative = path.relative_to(app_dir)
                zf.write(path, (Path(app_dir.name) / relative).as_posix())
    return target


def write_manifest(out: Path, *, version: str, notes: list[str],
                   package: Path | None, update_type: str,
                   from_versions: list[str], min_version: str,
                   base_url: str | None, full_package: Path | None) -> Path:
    published = time.strftime("%Y-%m-%d")
    payload: dict = {
        "version": version,
        "minimum_supported_version": min_version or version,
        "release_notes": notes,
        "published_at": published,
        "update_type": update_type,
        "from_versions": list(from_versions),
    }
    if base_url and package is not None:
        payload["package_url"] = base_url.rstrip("/") + "/" + package.name
        payload["sha256"] = sha256_file(package)
        payload["package_size"] = package.stat().st_size
    else:
        payload["package_url"] = "<SET_PACKAGE_URL>"
        payload["sha256"] = "<SET_SHA256>"
    if base_url and full_package is not None:
        payload["full_package_url"] = base_url.rstrip("/") + "/" + full_package.name
        payload["full_sha256"] = sha256_file(full_package)
        payload["full_package_size"] = full_package.stat().st_size
    name = "manifest.json" if base_url else "manifest.template.json"
    target = out / name
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                      encoding="utf-8")
    return target


def write_release_doc(out: Path, *, version: str, notes: list[str],
                      files: dict, full_package: Path | None,
                      incremental: Path | None, from_versions: list[str],
                      manifest: Path, repository: str | None) -> Path:
    repo = repository or "<OWNER>/<REPO>"
    lines = [
        "# LOL 海克斯助手 v%s 发布说明" % version, "",
        "## 更新内容", "",
        *["- " + line for line in notes], "",
        "## 产物", "",
        "| 文件 | 大小 |", "| --- | --- |",
    ]
    for package in (full_package, incremental):
        if package is not None:
            lines.append("| %s | %.1f MB |" % (package.name,
                                               package.stat().st_size / 1024 / 1024))
    lines += ["| %s | %.1f KB |" % (manifest.name, manifest.stat().st_size / 1024),
              "", "文件总数：%d" % len(files), "",
              "## 发布到 GitHub Releases", "",
              "```powershell",
              "gh release create v%s --title \"v%s\" --notes-file \"%s\"" %
              (version, version, out / "RELEASE.md"),
              "gh release upload v%s \"%s\" \"%s\" \"%s\" --clobber" %
              (version, full_package or "", incremental or "", manifest),
              "```", "",
              "然后让 `config/config.yaml` 的 `update.manifest_url` 指向稳定地址（推荐）：",
              "",
              "```text",
              "https://github.com/%s/releases/latest/download/manifest.json" % repo,
              "```", "",
              "> `releases/latest/download/<asset>` 是 GitHub 的稳定地址：",
              "> 每次发布新 tag 后，旧链接会自动指向最新 release 的清单。",
              "",
              "## 增量包 from_versions",
              "",
              "```text", ", ".join(from_versions) or "（无：仅完整包）", "```", "",
              "不在该列表中的版本会自动回退到清单里的 `full_package_url`。",
            ]
    target = out / "RELEASE.md"
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return target


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="生成 LOL 海克斯助手更新包")
    parser.add_argument("--app-dir", required=True, type=Path)
    parser.add_argument("--version", required=True)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--notes", type=Path)
    parser.add_argument("--previous", action="append", default=[], type=Path,
                        help="旧版本安装目录（可重复，用于增量）")
    parser.add_argument("--from-version", action="append", default=[],
                        help="与 --previous 一一对应的旧版本号")
    parser.add_argument("--base-url", default=None,
                        help="GitHub Release 下载基地址（不给则只生成模板）")
    parser.add_argument("--min-version", default="")
    parser.add_argument("--repository", default=None, help="owner/repo")
    parser.add_argument(
        "--exclude-from-incremental", action="append", default=[],
        help="不写入增量包的文件（相对 app-dir，可重复）；用于兼容旧 Updater")
    args = parser.parse_args(argv)

    version = str(parse_version(args.version))
    app_dir = args.app_dir.resolve()
    if not app_dir.is_dir():
        print("app-dir 不存在: %s" % app_dir)
        return 2
    try:
        validate_app_layout(app_dir)
    except ValueError as exc:
        print(str(exc))
        return 2
    if len(args.previous) != len(args.from_version):
        print("--previous 与 --from-version 必须一一对应")
        return 2

    out: Path = args.out
    out.mkdir(parents=True, exist_ok=True)
    files = scan_app(app_dir)
    if "LOL海克斯助手.exe" not in files:
        print("app-dir 里没有主程序 LOL海克斯助手.exe")
        return 2
    if "Updater.exe" not in files:
        print("警告：app-dir 里没有 Updater.exe（自动更新将无法安装）")

    notes = read_notes(args.notes)
    (out / ("files-v%s.json" % version)).write_text(
        json.dumps({"version": version, "files": files}, ensure_ascii=False, indent=2),
        encoding="utf-8")

    old_sets = [scan_app(p.resolve()) for p in args.previous]
    full_ops = full_operations(files, old_sets[0] if old_sets else {})
    validate_operation_paths(full_ops)
    full_package = build_package(app_dir, out / ("update-full-v%s.zip" % version),
                                 version, update_type="full", operations=full_ops,
                                 from_versions=[])
    installer_package = build_installer_zip(
        app_dir, out / ("LOLHexHelper-v%s.zip" % version))

    incremental = None
    inc_ops: list[dict] = []
    if old_sets:
        inc_ops = incremental_operations(
            files, old_sets, excluded=set(args.exclude_from_incremental))
        validate_operation_paths(inc_ops)
        if inc_ops:
            incremental = build_package(
                app_dir, out / ("update-incremental-to-v%s.zip" % version),
                version, update_type="incremental", operations=inc_ops,
                from_versions=list(args.from_version))
        else:
            print("旧版本与新版本文件完全相同，跳过增量包")

    package = incremental or full_package
    update_type = "incremental" if incremental else "full"
    manifest = write_manifest(out, version=version, notes=notes, package=package,
                              update_type=update_type,
                              from_versions=list(args.from_version) if incremental else [],
                              min_version=args.min_version,
                              base_url=args.base_url, full_package=full_package)
    doc = write_release_doc(out, version=version, notes=notes, files=files,
                            full_package=full_package, incremental=incremental,
                            from_versions=list(args.from_version) if incremental else [],
                            manifest=manifest, repository=args.repository)
    print("=" * 68)
    print("版本: v%s  文件数: %d" % (version, len(files)))
    print("完整包: %s (%.1f MB)" % (full_package.name,
                                    full_package.stat().st_size / 1024 / 1024))
    print("安装包: %s (%.1f MB)" % (installer_package.name,
                                    installer_package.stat().st_size / 1024 / 1024))
    if incremental is not None:
        print("增量包: %s (%.1f MB, %d 个操作)" %
              (incremental.name, incremental.stat().st_size / 1024 / 1024, len(inc_ops)))
        added = sum(1 for o in inc_ops if o["op"] == "add")
        replaced = sum(1 for o in inc_ops if o["op"] == "replace")
        deleted = sum(1 for o in inc_ops if o["op"] == "delete")
        print("  新增 %d / 替换 %d / 删除 %d" % (added, replaced, deleted))
    print("清单: %s（%s）" % (manifest.name,
                              "可直接发布" if args.base_url else "模板，需填 package_url"))
    print("说明: %s" % doc.name)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
