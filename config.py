"""礼貌性沉默（Polite Silence）插件的 WebUI 配置模型。"""

from __future__ import annotations

from typing import Any, ClassVar

from maibot_sdk import Field, PluginConfigBase
from pydantic import field_validator

CONFIG_SCHEMA_VERSION = "1.1.0"


class PluginSectionConfig(PluginConfigBase):
    """插件总开关与日志开关。"""

    __ui_label__: ClassVar[str] = "插件设置"
    __ui_icon__: ClassVar[str] = "power"
    __ui_order__: ClassVar[int] = 0

    config_version: str = Field(
        default=CONFIG_SCHEMA_VERSION,
        description="配置 schema 版本，请勿手动修改。",
        json_schema_extra={
            "disabled": True,
            "hidden": True,
            "label": "配置版本",
            "order": 99,
        },
    )
    enabled: bool = Field(
        default=True,
        description="是否启用礼貌性沉默插件。关闭后不拦截消息，也不向模型提供忽略工具。",
        json_schema_extra={
            "label": "启用插件",
            "hint": "总开关，关闭后拒答拦截、工具与管理指令全部停用。",
        },
    )
    enable_log: bool = Field(
        default=True,
        description="是否输出插件的运行日志。",
        json_schema_extra={
            "label": "启用运行日志",
            "hint": "关闭后不再输出加载摘要、拦截记录与入站诊断日志；警告和错误仍会保留。",
        },
    )


class SilenceConfig(PluginConfigBase):
    """礼貌性沉默的行为配置。"""

    __ui_label__: ClassVar[str] = "礼貌性沉默"
    __ui_icon__: ClassVar[str] = "volume-x"
    __ui_order__: ClassVar[int] = 1

    min_ignore_minutes: int = Field(
        default=10,
        ge=0,
        description="单次拒答的最短时长（分钟）。",
        json_schema_extra={
            "label": "最短拒答时长 (分钟)",
            "hint": "模型给出的时长低于该值时会被抬高到该值；工具未指定时长时也使用该值。",
        },
    )
    max_ignore_minutes: int = Field(
        default=1440,
        ge=0,
        description="单次拒答的最长时长（分钟）。",
        json_schema_extra={
            "label": "最长拒答时长 (分钟)",
            "hint": "模型给出的时长高于该值时会被压到该值，默认 1 天。",
        },
    )
    whitelist_users: list[str] = Field(
        default_factory=list,
        description="不会被拒答的 QQ 号列表。",
        json_schema_extra={
            "label": "白名单 QQ 号",
            "hint": "白名单用户的消息始终放行，也不会被写入拒答名单。兼容逗号分隔字符串写法。",
        },
    )
    is_global_ignore: bool = Field(
        default=False,
        description="是否让拒答在所有聊天中生效。",
        json_schema_extra={
            "label": "全局拒答",
            "hint": "开启后，用户在一个会话中被拒答，会在所有群聊和私聊中被忽略；关闭时仅限当前聊天流。",
        },
    )

    @field_validator("whitelist_users", mode="before")
    @classmethod
    def _normalize_whitelist_users(cls, value: Any) -> list[str]:
        """兼容逗号分隔字符串、列表与单个 ID。"""
        if value is None:
            return []
        if isinstance(value, str):
            items: list[Any] = value.split(",")
        elif isinstance(value, (list, tuple, set)):
            items = list(value)
        else:
            items = [value]
        return [str(item).strip() for item in items if str(item).strip()]


class PoliteSilenceSettings(PluginConfigBase):
    """礼貌性沉默插件完整配置。"""

    plugin: PluginSectionConfig = Field(default_factory=PluginSectionConfig)
    silence: SilenceConfig = Field(default_factory=SilenceConfig)
