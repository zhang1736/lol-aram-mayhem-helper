"""腾讯 KIWI 海克斯字典测试。"""

from __future__ import annotations

import json

import pytest

from src.data.augment_repository import (
    MAYHEM_MODE,
    AugmentRepository,
    default_raw_path,
    mode_distribution,
    parse_raw_dict,
)
from src.utils import paths


@pytest.fixture(scope="module")
def repository() -> AugmentRepository:
    repo = AugmentRepository()
    try:
        repo.load()
    except FileNotFoundError:
        pytest.skip("data/kiwi_augments.json 不存在，跳过字典测试")
    return repo


def test_dictionary_contains_latest_tencent_entries() -> None:
    path = paths.bundled_augment_dict_path()
    assert path.exists(), "data/kiwi_augments.json 必须存在"
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(payload, list)
    assert len(payload) >= 200
    by_id = {row["augmentID"]: row for row in payload}
    # 2026-10-24 腾讯最新字典新增项；旧版 248 条字典不含这些 ID。
    assert by_id[3004]["name_cn"] == "罪恶快感！"
    assert by_id[3159]["name_cn"] == "登峰造极境"


def test_raw_fields_match_tencent_export() -> None:
    payload = json.loads(default_raw_path().read_text(encoding="utf-8"))
    keys = set(payload[0].keys())
    for field in ("augmentID", "name_cn", "name_en", "level", "mode",
                  "desc", "tooltip", "large_Icon", "small_Icon"):
        assert field in keys, f"原始字典缺少字段 {field}"


def test_parse_raw_dict_maps_fields() -> None:
    payload = json.loads(default_raw_path().read_text(encoding="utf-8"))
    items = parse_raw_dict(payload)
    assert len(items) == len(payload)
    sample = next(i for i in items if i.augment_id == 1077)
    assert sample.name_en == "ARAM_SoulSiphon"
    assert sample.level == "kGold"
    assert sample.rarity == "Gold"
    assert sample.icon


def test_mayhem_filter_excludes_other_modes(repository: AugmentRepository) -> None:
    """只保留 mode 含 KIWI 的条目；KIWI_JADE-only 必须被排除。"""
    assert len(repository.all_items) > len(repository.items)
    for item in repository.items:
        assert item.in_mode(MAYHEM_MODE)
    excluded = [i for i in repository.all_items if not i.in_mode(MAYHEM_MODE)]
    assert excluded, "应当存在被排除的条目（KIWI_JADE-only / 空 mode）"
    for item in excluded:
        modes = item.modes()
        assert modes == [] or modes == ["KIWI_JADE"]


def test_level_values_are_rarity_keys(repository: AugmentRepository) -> None:
    levels = {item.level for item in repository.items}
    assert levels <= {"kSilver", "kGold", "kPrismatic"}


def test_by_level_groups(repository: AugmentRepository) -> None:
    grouped = repository.by_level()
    assert set(grouped) <= {"kSilver", "kGold", "kPrismatic"}
    assert sum(len(v) for v in grouped.values()) == len(repository.items)


def test_id_lookup_and_name_lookup(repository: AugmentRepository) -> None:
    item = repository.get(1077)
    assert item is not None
    assert repository.get(999999) is None
    assert repository.get_by_name(item.name_cn or "").augment_id == 1077
    assert repository.get_by_name(item.name_en or "").augment_id == 1077


def test_mode_distribution_counts(repository: AugmentRepository) -> None:
    distribution = mode_distribution(repository.all_items)
    assert distribution
    assert sum(distribution.values()) == len(repository.all_items)


def test_old_user_dictionary_does_not_shadow_bundled_updates(monkeypatch,
                                                             tmp_path) -> None:
    """已迁移到用户目录的旧字典必须与新版内置字典合并。"""
    monkeypatch.setenv("LOL_HEX_HELPER_HOME", str(tmp_path))
    override = tmp_path / "data" / "kiwi_augments.json"
    override.parent.mkdir(parents=True, exist_ok=True)
    override.write_text(json.dumps([{
        "augmentID": 1077,
        "name_cn": "灵魂虹吸",
        "name_en": "ARAM_SoulSiphon",
        "mode": "KIWI",
        "level": "kGold",
    }], ensure_ascii=False), encoding="utf-8")

    repo = AugmentRepository().load()
    assert repo.get(1077).name_cn == "灵魂虹吸"
    assert repo.get(3004).name_cn == "罪恶快感！"
