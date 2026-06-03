"""今日老婆插件配置模型。"""
from __future__ import annotations
from typing import ClassVar, List, Dict

from pydantic import field_validator
from maibot_sdk import Field, PluginConfigBase

class PluginSection(PluginConfigBase):
    __ui_label__: ClassVar[str] = "插件开关"
    __ui_order__: ClassVar[int] = 0
    enabled: bool = Field(
        default=True,
        description="是否启用本插件。",
        json_schema_extra={"label": "启用插件", "order": 0},
    )
    config_version: str = Field(
        default="2.0.0",
        json_schema_extra={"disabled": True, "hidden": True, "label": "配置版本", "order": 99},
    )

class PartnerGameSection(PluginConfigBase):
    __ui_label__: ClassVar[str] = "今日老婆 Pro 游戏设置"
    __ui_order__: ClassVar[int] = 1
    exclude_self: bool = Field(
        default=True,
        description="是否将机器人自身排除出候选池。",
        json_schema_extra={"label": "排除机器人自身", "order": 0},
    )
    exclude_sender: bool = Field(
        default=True,
        description="是否将指令发起者排除出候选池。",
        json_schema_extra={"label": "排除指令发起者", "order": 1},
    )
    bot_qq: str = Field(
        default="",
        description="（可选）机器人 QQ。留空时自动通过 napcat 获取。",
        json_schema_extra={"label": "机器人 QQ（可选）", "placeholder": "留空自动获取", "order": 2},
    )
    no_limit_users: List[str] = Field(
        default_factory=list,
        description="不受每日一次限制的白名单 QQ 列表。",
        json_schema_extra={"label": "白名单 QQ", "order": 3},
    )
    admin_users: List[str] = Field(
        default_factory=list,
        description="拥有最高权限的管理员 QQ 列表（可使用 /重置、/全服补偿 等高危指令）。",
        json_schema_extra={"label": "管理员 QQ", "order": 4},
    )
    tz_offset_hours: int = Field(
        default=8,
        description="时区偏移（小时），用于确定 “今日” 的边界。",
        json_schema_extra={"label": "时区偏移", "order": 4, "step": 1},
    )
    send_avatar: bool = Field(
        default=True,
        description="是否额外发送对方的 QQ 头像。",
        json_schema_extra={"label": "发送头像", "order": 5},
    )
    avatar_size: int = Field(
        default=640,
        description="头像尺寸（像素）。",
        json_schema_extra={"label": "头像尺寸", "order": 6, "step": 1},
    )
    cooldown_seconds: int = Field(
        default=60,
        description="同一指令连续触发的冷却秒数。",
        json_schema_extra={"label": "冷却秒数", "order": 7, "step": 1},
    )
    persist_enabled: bool = Field(
        default=True,
        description="是否启用本地持久化（保存今日抽取与离婚记录）。",
        json_schema_extra={"label": "启用持久化", "order": 8},
    )
    force_marry_probability: float = Field(
        default=0.4,
        description="强娶成功的概率 (0.0 ~ 1.0)。",
        json_schema_extra={"label": "强娶成功概率", "order": 9},
    )
    rob_wife_probability: float = Field(
        default=0.3,
        description="抢老婆成功的基础概率 (0.0 ~ 1.0)。",
        json_schema_extra={"label": "抢老婆基础成功率", "order": 10},
    )
    rob_money_probability: float = Field(
        default=0.35,
        description="打劫金币的基础成功率 (0.0 ~ 1.0)。",
        json_schema_extra={"label": "打劫金币基础成功率", "order": 11},
    )
    proposal_timeout_seconds: int = Field(
        default=60,
        description="娶老婆等待同意的超时秒数。",
        json_schema_extra={"label": "求婚超时秒数", "order": 10, "step": 1},
    )
    forced_wives: List[str] = Field(
        default_factory=list,
        description="强制指定的黑幕列表，每条规则格式为 发起者QQ:强制老婆QQ",
        json_schema_extra={
            "label": "强制黑幕名单", 
            "hint": "神秘开关：指定某个QQ永远抽到另外一个特定的QQ（例如指定抽到机器人自己）。\n格式为：【发起人QQ号:老婆QQ号】。例如 12345678:87654321。\n如果有多个黑幕，添加多条即可。",
            "order": 9
        },
    )

    @field_validator("forced_wives", mode="before")
    def _parse_forced_wives(cls, v):
        if isinstance(v, dict):
            # 兼容旧版本保存的字典配置
            return [f"{k}:{val}" for k, val in v.items()]
        return v

class PartnerGameConfig(PluginConfigBase):
    plugin: PluginSection = Field(default_factory=PluginSection)
    partner_game: PartnerGameSection = Field(default_factory=PartnerGameSection)
