"""下载并合并腾讯 KIWI 海克斯字典。

合并策略：
    * 腾讯最新条目覆盖同 ID 的旧字段；
    * 保留本地已有但腾讯已下架的旧 ID，避免历史模式下名称突然消失；
    * 按 augmentID 排序并原子替换 data/kiwi_augments.json。

若游戏新版本加入了海克斯而字典仍是旧的，OCR 得到正确中文名称也无法映射
到 augmentId。因此每次游戏版本更新后都应运行本工具并重新打包。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
REMOTE_URL = "https://game.gtimg.cn/images/lol/act/img/js/kiwi/kiwi_augments.json"
CERT_PATH = ROOT / "certs" / "windows_ca_bundle.pem"
DEFAULT_TARGET = ROOT / "data" / "kiwi_augments.json"


def _index(payload: list[dict]) -> dict[int, dict]:
    indexed: dict[int, dict] = {}
    for row in payload:
        if not isinstance(row, dict):
            continue
        try:
            augment_id = int(row["augmentID"])
        except (KeyError, TypeError, ValueError):
            continue
        indexed[augment_id] = row
    return indexed


def merge_payloads(local: list[dict], remote: list[dict]) -> list[dict]:
    merged = _index(local)
    remote_index = _index(remote)
    added = sorted(set(remote_index) - set(merged))
    updated = sorted(augment_id for augment_id in set(merged) & set(remote_index)
                     if merged[augment_id] != remote_index[augment_id])
    merged.update(remote_index)
    print("腾讯条目: %d，本地条目: %d，新增: %d，更新: %d，合并后: %d"
          % (len(remote_index), len(_index(local)), len(added), len(updated),
             len(merged)))
    if added:
        print("新增 augmentId: %s" % ", ".join(map(str, added)))
    return [merged[augment_id] for augment_id in sorted(merged)]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--target", type=Path, default=DEFAULT_TARGET)
    parser.add_argument("--write", action="store_true",
                        help="写入合并后的字典；默认只检查")
    args = parser.parse_args()

    local = json.loads(args.target.read_text(encoding="utf-8"))
    if not isinstance(local, list):
        raise SystemExit("本地字典根节点必须是数组")
    verify = str(CERT_PATH) if CERT_PATH.exists() else True
    response = httpx.get(REMOTE_URL, timeout=30.0, verify=verify,
                         follow_redirects=True)
    response.raise_for_status()
    remote = response.json()
    if not isinstance(remote, list):
        raise SystemExit("腾讯字典根节点不是数组")

    merged = merge_payloads(local, remote)
    if not args.write:
        print("未写入；加 --write 更新正式字典。")
        return 0

    temp = args.target.with_suffix(".json.tmp")
    temp.write_text(json.dumps(merged, ensure_ascii=False, indent="\t") + "\n",
                    encoding="utf-8")
    os.replace(temp, args.target)
    print("已更新: %s" % args.target)
    return 0


if __name__ == "__main__":
    sys.exit(main())
