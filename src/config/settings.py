"""配置模型与 YAML 加载。

术语（重要，禁止混用）：
    data.tier        统计数据口径段位区间（ibsg = Iron/Bronze/Silver/Gold）
    opgg_augment_tier  OP.GG 海克斯响应里的 tier（0~5 欢迎度档位）
"""

from __future__ import annotations

import os
from pathlib import Path

import yaml
from pydantic import BaseModel, Field, field_validator

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config" / "config.yaml"

#: OP.GG 海克斯统计接口无法按地区/段位过滤，只能标为聚合口径
SCOPE_AGGREGATE = "aggregate"


class DataScopeSettings(BaseModel):
    """英雄统计数据的统计范围（会作为 region 路径段 + tier 查询参数发给 OP.GG）。"""

    region: str = "kr"
    tier: str = "ibsg"

    @field_validator("region", "tier")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        value = (value or "").strip()
        if not value:
            raise ValueError("region/tier 不能为空")
        return value


class OpggSettings(BaseModel):
    base_url: str = "https://lol-api-champion.op.gg"
    augment_scope: str = SCOPE_AGGREGATE


class UpdateSettings(BaseModel):
    """自动检查更新（发布者配置，写入 config.yaml，不进用户配置）。

    manifest_url 留空 = 未配置更新源：启动检查静默跳过，
    手动检查会提示"更新源未配置"，绝不伪造可用地址。
    """

    enabled: bool = True
    manifest_url: str = ""
    check_on_startup: bool = True
    timeout_seconds: float = 15.0
    #: 只允许本地模拟服务器/测试使用 http
    allow_http: bool = False

    @field_validator("manifest_url")
    @classmethod
    def _url(cls, value: str) -> str:
        value = (value or "").strip()
        if not value:
            return ""
        lowered = value.lower()
        if not (lowered.startswith("https://") or lowered.startswith("http://")):
            raise ValueError("update.manifest_url 必须以 https:// 开头")
        return value


class LcuSettings(BaseModel):
    process_name: str = "LeagueClientUx.exe"
    request_timeout: float = 8.0
    #: 常驻模式下 LCU 掉线后的重连间隔（秒）
    reconnect_interval: float = 5.0


class OverlaySettings(BaseModel):
    enabled: bool = True
    screen_index: int = 0
    #: 透明度 0~100（100 = 完全不透明）
    opacity: int = 100
    #: 整体位置偏移（像素）
    offset_x: int = 0
    offset_y: int = 0

    @field_validator("opacity")
    @classmethod
    def _clamp_opacity(cls, value: int) -> int:
        return max(0, min(100, int(value)))


class CacheSettings(BaseModel):
    """OP.GG 缓存策略。

    patch + champion + augment 只是缓存 Key，**不能**决定缓存永久有效
    —— 实测同一 patch 内 OP.GG 数据也会漂移，因此必须带 TTL。
    """

    opgg_ttl_hours: float = 6.0

    @field_validator("opgg_ttl_hours")
    @classmethod
    def _positive(cls, value: float) -> float:
        value = float(value)
        if value <= 0:
            raise ValueError("opgg_ttl_hours 必须为正数")
        return value

    @property
    def ttl_seconds(self) -> float:
        return self.opgg_ttl_hours * 3600.0


class DebugSettings(BaseModel):
    enabled: bool = False


class LoggingSettings(BaseModel):
    level: str = "INFO"
    file: str = "logs/app.log"


class Settings(BaseModel):
    data: DataScopeSettings = Field(default_factory=DataScopeSettings)
    opgg: OpggSettings = Field(default_factory=OpggSettings)
    update: UpdateSettings = Field(default_factory=UpdateSettings)
    lcu: LcuSettings = Field(default_factory=LcuSettings)
    overlay: OverlaySettings = Field(default_factory=OverlaySettings)
    cache: CacheSettings = Field(default_factory=CacheSettings)
    debug: DebugSettings = Field(default_factory=DebugSettings)
    logging: LoggingSettings = Field(default_factory=LoggingSettings)

    #: 配置文件所在目录，便于解析相对路径
    config_dir: Path = PROJECT_ROOT

    @property
    def log_file(self) -> Path:
        from src.utils import paths

        return paths.log_path()

    @property
    def database_file(self) -> Path:
        from src.utils import paths

        return paths.database_path()

    # -- 两个 scope 的展示文案（绝不混用） ---------------------------------
    @property
    def hero_scope_label(self) -> str:
        """英雄统计口径，例如 Korea / Iron–Gold。"""
        region = {"kr": "Korea"}.get(self.data.region.lower(), self.data.region)
        tier = {"ibsg": "Iron–Gold"}.get(self.data.tier.lower(), self.data.tier)
        return f"{region} / {tier}"

    @property
    def augment_scope_label(self) -> str:
        """海克斯统计口径：接口无法按地区/段位过滤，只能是聚合口径。"""
        return "OP.GG Aggregate"


def _read_yaml(path: Path) -> dict:
    if not path.exists():
        return {}
    with open(path, "r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    if not isinstance(data, dict):
        raise ValueError(f"配置文件根节点必须是映射: {path}")
    return data


def _deep_merge(base: dict, override: dict) -> dict:
    """用户配置覆盖默认配置（只覆盖显式写出的键）。"""
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def load_settings(path: str | os.PathLike[str] | None = None,
                  user_path: str | os.PathLike[str] | None = None) -> Settings:
    """加载配置：默认 config.yaml + 用户 config/user_config.yaml（后者优先）。

    正式版的数据目录（SQLite / logs / 用户配置）统一走用户可写目录，
    绝不写 Program Files。
    """
    from src.utils import paths

    defaults_path = Path(path) if path else paths.default_config_path()
    raw = _read_yaml(defaults_path)

    user_file = Path(user_path) if user_path else paths.user_config_path()
    user_raw = _read_yaml(user_file)
    if user_raw:
        raw = _deep_merge(raw, user_raw)

    settings = Settings(**raw)
    settings.config_dir = paths.user_data_dir()
    return settings


def save_user_settings(settings: Settings,
                       path: str | os.PathLike[str] | None = None) -> Path:
    """只保存用户可调项到 config/user_config.yaml（不碰默认配置）。"""
    from src.utils import paths

    target = Path(path) if path else paths.user_config_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "data": {"region": settings.data.region, "tier": settings.data.tier},
        "cache": {"opgg_ttl_hours": settings.cache.opgg_ttl_hours},
        "overlay": {
            "enabled": settings.overlay.enabled,
            "screen_index": settings.overlay.screen_index,
            "opacity": settings.overlay.opacity,
            "offset_x": settings.overlay.offset_x,
            "offset_y": settings.overlay.offset_y,
        },
        "debug": {"enabled": settings.debug.enabled},
        "logging": {"level": settings.logging.level},
    }
    tmp = target.with_suffix(".yaml.tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        yaml.safe_dump(payload, fh, allow_unicode=True, sort_keys=False)
    tmp.replace(target)
    return target
