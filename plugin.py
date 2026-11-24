"""明日方舟wiki插件：轻量级《明日方舟》PRTS Wiki 知识、战术卡片与立绘插件。

核心特性：
1. 微秒级双档置信度语境感知（Hook 注入 Planner 尾部，不破坏前缀缓存、不污染记忆库）；
2. 仅注册 2 个显式可见 Tool：ak_wiki_query（总览/技能/模组/材料/档案/Wiki）与 ak_send_illustration（立绘/皮肤直发）；
3. 严守 PRTS MediaWiki API 礼仪与限流护栏，纯代码 MIT，按需本地 SQLite + LRU 缓存。
"""

from __future__ import annotations

import base64
import re
import time
from pathlib import Path
from typing import Any, Optional

from maibot_sdk import (
    Command,
    Field,
    HookHandler,
    MaiBotPlugin,
    PluginConfigBase,
    Tool,
)
from maibot_sdk.types import HookMode, HookOrder, ToolParameterInfo, ToolParamType
from pydantic import model_validator

from .cards import (
    build_enemy_card_html,
    build_events_card_html,
    build_help_card_html,
    build_lore_card_html,
    build_material_summary_card_html,
    build_operator_card_html,
    build_recruit_card_html,
    build_voice_card_html,
)
from .matcher import (
    ArknightsContextMatcher,
    load_runtime_aliases,
    parse_custom_alias_line,
    save_runtime_aliases,
)
from .prts import PRTSClient, extract_recruit_tags_from_text

PLUGIN_ID = "feiyu.arknights-wiki"
_HINT_MARKER = "[方舟语境参考"


# ============================================================
# 配置 Schema（MaiBot PluginConfigBase 自动生成/校验 config.toml）
# ============================================================


class PluginSection(PluginConfigBase):
    """插件总开关与配置版本。"""

    __ui_label__ = "插件"
    __ui_icon__ = "book-open"
    __ui_order__ = 0

    enabled: bool = Field(
        default=True,
        description="是否启用明日方舟wiki插件",
        json_schema_extra={"label": "启用插件"},
    )
    config_version: str = Field(default="0.3.1", description="配置版本")


_LIST_MODE_MAP = {
    "off": "不限制",
    "none": "不限制",
    "all": "不限制",
    "不限制": "不限制",
    "whitelist": "仅白名单",
    "white": "仅白名单",
    "仅白名单": "仅白名单",
    "blacklist": "排除黑名单",
    "black": "排除黑名单",
    "排除黑名单": "排除黑名单",
}


class ScopeSection(PluginConfigBase):
    """按群聊 / 私聊开关与黑白名单控制生效范围。"""

    __ui_label__ = "生效范围与黑白名单"
    __ui_icon__ = "shield"
    __ui_order__ = 1

    allow_group: bool = Field(
        default=True,
        description="是否允许在群聊中使用本插件（含黑话感知、工具与指令）",
        json_schema_extra={"label": "允许群聊使用"},
    )
    allow_private: bool = Field(
        default=True,
        description="是否允许在私聊中使用本插件",
        json_schema_extra={"label": "允许私聊使用"},
    )
    group_list_mode: str = Field(
        default="不限制",
        description="群聊名单过滤模式：不限制 / 仅白名单 / 排除黑名单",
        json_schema_extra={"label": "群聊名单模式", "choices": ["不限制", "仅白名单", "排除黑名单"]},
    )
    group_list: list[str] = Field(
        default_factory=list,
        description="群聊名单（支持纯群号、platform:群号 如 qq:123456、Telegram 父群号或群名称）",
        json_schema_extra={"label": "群聊名单列表"},
    )
    private_list_mode: str = Field(
        default="不限制",
        description="私聊名单过滤模式：不限制 / 仅白名单 / 排除黑名单",
        json_schema_extra={"label": "私聊名单模式", "choices": ["不限制", "仅白名单", "排除黑名单"]},
    )
    private_list: list[str] = Field(
        default_factory=list,
        description="私聊名单（支持纯用户 ID、platform:用户ID 或 OpenID）",
        json_schema_extra={"label": "私聊名单列表"},
    )

    @model_validator(mode="before")
    @classmethod
    def _migrate_legacy_scope(cls, data: Any) -> Any:
        """无缝兼容 v0.1/v0.2 的 group_whitelist/blacklist 与英文模式值。"""
        if not isinstance(data, dict):
            return data
        d = dict(data)
        for k in ("group_list_mode", "private_list_mode"):
            if k in d and isinstance(d[k], str):
                d[k] = _LIST_MODE_MAP.get(d[k].strip().lower(), d[k].strip())
        if not d.get("group_list"):
            if d.get("group_whitelist"):
                d["group_list_mode"] = "仅白名单"
                d["group_list"] = list(d["group_whitelist"])
            elif d.get("group_blacklist"):
                d["group_list_mode"] = "排除黑名单"
                d["group_list"] = list(d["group_blacklist"])
        if not d.get("private_list"):
            if d.get("private_whitelist"):
                d["private_list_mode"] = "仅白名单"
                d["private_list"] = list(d["private_whitelist"])
            elif d.get("private_blacklist"):
                d["private_list_mode"] = "排除黑名单"
                d["private_list"] = list(d["private_blacklist"])
        for old_k in ("group_whitelist", "group_blacklist", "private_whitelist", "private_blacklist"):
            d.pop(old_k, None)
        return d


class ContextSection(PluginConfigBase):
    """控制聊天语境中的方舟黑话感知与注入策略。"""

    __ui_label__ = "语境感知"
    __ui_icon__ = "sparkles"
    __ui_order__ = 2

    enable_planner_hint: bool = Field(
        default=True,
        description="检测到方舟黑话/干员名时，向 Planner 注入极简语境小抄（零网络请求）",
        json_schema_extra={"label": "Planner 黑话小抄注入"},
    )
    enable_replyer_style_hint: bool = Field(
        default=True,
        description="命中方舟语境时，提醒 Replyer 用自然玩家口吻聊天而非机械背百科",
        json_schema_extra={"label": "自然口吻提示"},
    )
    hot_session_ttl_seconds: int = Field(
        default=300,
        ge=30,
        le=1800,
        description="命中高置信方舟词后，保持该会话方舟热态的秒数",
        json_schema_extra={"label": "方舟热态时长（秒）"},
    )
    max_hint_items: int = Field(
        default=6,
        ge=1,
        le=8,
        description="单轮最多注入的方舟词条参考数（默认 6 条，兼顾准确度与省 Token）",
        json_schema_extra={"label": "单轮最大注入词条数"},
    )


class CommandSection(PluginConfigBase):
    """快捷斜杠指令开关。"""

    __ui_label__ = "快捷指令"
    __ui_icon__ = "terminal"
    __ui_order__ = 3

    enable_commands: bool = Field(
        default=True,
        description="是否启用 /方舟 与 /立绘 快捷指令",
        json_schema_extra={"label": "启用快捷指令"},
    )
    admin_users: list[str] = Field(
        default_factory=list,
        description="清理缓存和增删别名的管理员 ID（支持 platform:用户ID；留空时读取宿主 plugin.permission，均未配置则仅限已识别的私聊）",
        json_schema_extra={"label": "缓存管理管理员 ID"},
    )


class PRTSSection(PluginConfigBase):
    """PRTS MediaWiki 接口与礼貌限流参数。"""

    __ui_label__ = "PRTS 接口"
    __ui_icon__ = "globe"
    __ui_order__ = 4

    api_url: str = Field(
        default="https://prts.wiki/api.php",
        description="PRTS MediaWiki API 地址",
        json_schema_extra={"label": "API 地址"},
    )
    user_agent: str = Field(
        default="MaiBot-ArknightsWiki/0.3.1 (+https://github.com/Allure-Gui/maibot-arknights-wiki)",
        description="礼貌 User-Agent（遵循 MediaWiki API 规范）",
        json_schema_extra={"label": "User-Agent"},
    )
    timeout_seconds: float = Field(
        default=12.0,
        ge=3.0,
        le=30.0,
        description="单次请求超时（秒）",
        json_schema_extra={"label": "请求超时（秒）"},
    )
    min_request_interval_ms: int = Field(
        default=500,
        ge=300,
        le=5000,
        description="请求 PRTS 的最小间隔（毫秒，保护社区服务器）",
        json_schema_extra={"label": "最小请求间隔（毫秒）"},
    )
    illustration_thumb_width: int = Field(
        default=1600,
        ge=0,
        le=2560,
        description="立绘清晰度上限（像素；1600 高清锐利且原图≤3.5MB直出无损，0 表示始终原图）",
        json_schema_extra={"label": "立绘清晰度宽度（px）"},
    )


class RenderSection(PluginConfigBase):
    """罗德岛战术卡片图（材料 / 模组 / 干员面板）实时渲染设置。"""

    __ui_label__ = "卡片图渲染"
    __ui_icon__ = "image"
    __ui_order__ = 5

    enable_cards: bool = Field(
        default=True,
        description="开启后支持将干员材料、模组与面板渲染为 2x 高清黑曜石金箔风格卡片图",
        json_schema_extra={"label": "启用高清卡片图"},
    )
    auto_card_in_chat: bool = Field(
        default=True,
        description="群友聊天询问干员材料/模组时，自动附发一张高清材料/模组卡片图",
        json_schema_extra={"label": "聊天自动发材料/模组卡"},
    )
    card_scale: float = Field(
        default=2.0,
        ge=1.0,
        le=3.0,
        description="卡片图清晰度倍率（2.0 为 2x Retina 高清）",
        json_schema_extra={"label": "卡片清晰度倍率"},
    )
    stop_planner_after_image: bool = Field(
        default=True,
        description="发送完卡片图或立绘后立即结束本轮思考，不再让 Planner/Replyer 追加多余文字（极速且干脆）",
        json_schema_extra={"label": "发图后阻止多余文字回复"},
    )


class ReminderSection(PluginConfigBase):
    """活动与卡池即将结束时定时推送到允许的群聊（默认关闭，按需开启）。"""

    __ui_label__ = "活动到期提醒"
    __ui_icon__ = "bell"
    __ui_order__ = 6

    enable_event_reminder: bool = Field(
        default=False,
        description="开启后，在限时活动即将关闭时按设定时间（默认早上 9 点）自动推送到符合群聊黑/白名单的群聊",
        json_schema_extra={"label": "开启活动到期提醒"},
    )
    reminder_hour: int = Field(
        default=9,
        ge=0,
        le=23,
        description="每日检查并推送到期提醒的小时（北京时间 0~23，默认早上 9 点）",
        json_schema_extra={"label": "每日提醒时间（点）"},
    )
    remind_before_hours: int = Field(
        default=24,
        ge=6,
        le=72,
        description="距离活动结束不足多少小时时触发提醒（默认 24 小时内，每期活动每群仅提醒一次）",
        json_schema_extra={"label": "提前提醒时长（小时）"},
    )
    include_gacha: bool = Field(
        default=True,
        description="开启后，即将结束的限时寻访卡池也会一并提醒",
        json_schema_extra={"label": "同时提醒即将结束的卡池"},
    )


class CacheSection(PluginConfigBase):
    """本地 SQLite 文本与立绘 LRU 缓存配置。"""

    __ui_label__ = "本地缓存"
    __ui_icon__ = "database"
    __ui_order__ = 7

    page_ttl_hours: int = Field(
        default=168,
        ge=1,
        le=720,
        description="PRTS 页面文本本地缓存有效期（小时）",
        json_schema_extra={"label": "文本缓存有效期（小时）"},
    )
    image_cache_max_mb: int = Field(
        default=200,
        ge=20,
        le=2048,
        description="立绘本地 LRU 缓存上限（MB）",
        json_schema_extra={"label": "立绘缓存上限（MB）"},
    )


class GlossarySection(PluginConfigBase):
    """自定义追加群内方舟黑话与干员昵称（优先覆盖内置词典）。"""

    __ui_label__ = "自定义黑话与昵称"
    __ui_icon__ = "tag"
    __ui_order__ = 8

    custom_aliases: list[str] = Field(
        default_factory=list,
        description="点击「+ 添加项目」逐条填写，示例：牢猫=凯尔希 或 牢猫=凯尔希|6★医疗（也支持冒号或空格分隔，如 大肥鱼 斯卡蒂）",
        json_schema_extra={
            "label": "自定义黑话列表（示例：牢猫=凯尔希）",
            "placeholder": "例：牢猫=凯尔希 或 大肥鱼=斯卡蒂|6★近卫",
            "hint": "格式：外号=干员名（可选 |简短定位），例如：牢猫=凯尔希",
        },
    )
    custom_alias_force_tier1: bool = Field(
        default=True,
        description="开启后，你自定义的昵称/黑话直接作为高置信词命中（无需等待上下文热态）；关闭则对短词启用防误触门控",
        json_schema_extra={"label": "自定义黑话直接高置信命中"},
    )


class ArknightsWikiConfig(PluginConfigBase):
    plugin: PluginSection = Field(default_factory=PluginSection)
    scope: ScopeSection = Field(default_factory=ScopeSection)
    context: ContextSection = Field(default_factory=ContextSection)
    commands: CommandSection = Field(default_factory=CommandSection)
    prts: PRTSSection = Field(default_factory=PRTSSection)
    render: RenderSection = Field(default_factory=RenderSection)
    reminder: ReminderSection = Field(default_factory=ReminderSection)
    cache: CacheSection = Field(default_factory=CacheSection)
    glossary: GlossarySection = Field(default_factory=GlossarySection)


# ============================================================
# 工具辅助函数
# ============================================================


def _resolve_data_dir() -> Path:
    """推导插件专属数据目录，避免写入 MaiBot 核心目录或源码目录。"""
    plugin_dir = Path(__file__).resolve().parent
    if plugin_dir.parent.name.lower() == "plugins":
        return plugin_dir.parent.parent / "data" / "plugins" / PLUGIN_ID
    return plugin_dir / ".runtime_data"


def _send_ok(res: Any) -> bool:
    """仅把 SDK 的布尔回执或明确标记成功的详情视为送达。"""
    if isinstance(res, bool):
        return res
    if isinstance(res, dict):
        return res.get("sent", res.get("success")) is True
    return False


_LEAD_PH = r"(?:\s*\[(?:图片|图|表情|回复|引用|CQ:[^\]]+)(?:[^\[\]]|\[[^\[\]]*\])*\])*\s*"
_RE_RECRUIT_COMMAND = re.compile(rf"^{_LEAD_PH}/(?:公招|公开招募|recruit)(?:\s+(?P<args>.*))?$", re.I | re.S)
_RE_WIKI_COMMAND = re.compile(rf"^{_LEAD_PH}/(?:方舟|prts|ark)(?:\s+(?P<args>.*))?$", re.I | re.S)
_RE_ILLUSTRATION_COMMAND = re.compile(rf"^{_LEAD_PH}/(?:立绘|方舟立绘|ak图)(?:\s+(?P<args>.*))?$", re.I | re.S)
_RE_MSG_PLACEHOLDER = re.compile(r"\[(?:图片|图|表情|回复|引用|CQ:[^\]]+)(?:[^\[\]]|\[[^\[\]]*\])*\]", re.I | re.S)


def _command_args(kwargs: dict[str, Any], pattern: re.Pattern[str]) -> str:
    groups = kwargs.get("matched_groups")
    if isinstance(groups, dict) and "args" in groups:
        return str(groups["args"] or "").strip()
    # 兼容未传匹配组的旧宿主与直接调用。
    text = kwargs.get("matched_text") or kwargs.get("text")
    message = kwargs.get("message")
    if not text and isinstance(message, dict):
        text = message.get("processed_plain_text") or message.get("raw_message")
    match = pattern.fullmatch(str(text or "").strip())
    return (match.group("args") or "").strip() if match else ""


def _exc_text(exc: BaseException) -> str:
    """把异常转成能给人看的文本。

    底层超时经常是空消息（httpcore 抛 `ReadTimeout()`，httpx 原样保留空串），
    直接 f"{exc}" 会发出「获取立绘失败：」这种半截提示。这里补上可读说明。
    """
    msg = str(exc).strip()
    if msg:
        return msg
    if isinstance(exc, TimeoutError) or "Timeout" in type(exc).__name__:
        return "请求 PRTS 超时（网络较慢或图片较大），隔一会儿重试即可"
    name = type(exc).__name__
    return f"{name}（无错误详情）"


_PLATFORM_NAMES = {
    "qq": "qq", "qq_official": "qq", "qqbot": "qq", "onebot": "qq",
    "napcat": "qq", "snowluma": "qq", "tg": "telegram", "telegram": "telegram",
    "weixin_clawbot": "weixin_clawbot", "weixin": "weixin_clawbot", "wechat": "weixin_clawbot",
    "internal": "internal", "webui": "internal", "bot_console": "bot_console", "discord": "discord",
}
_TELEGRAM_TOPIC_ID = re.compile(r"(-?\d+):\d+")


def _split_scope_id(raw: str) -> tuple[str, str]:
    value = str(raw or "").strip()
    prefix, sep, identifier = value.partition(":")
    platform = _PLATFORM_NAMES.get(prefix.lower(), "") if sep else ""
    return (platform, identifier.strip()) if platform else ("", value)


def _scope_list_hit(rules: list[str], identifier: str, platform: str, *, group_name: str = "", group: bool = False) -> bool:
    supplied_platform, identifier = _split_scope_id(identifier)
    actual_platform = _PLATFORM_NAMES.get(platform, platform) or supplied_platform
    identifiers = {identifier}
    topic = _TELEGRAM_TOPIC_ID.fullmatch(identifier) if group and actual_platform == "telegram" else None
    if topic:
        identifiers.add(topic.group(1))
    name = group_name.strip().casefold()
    for rule in rules:
        rule_platform, value = _split_scope_id(rule)
        if not value or (rule_platform and rule_platform != actual_platform):
            continue
        if value in identifiers or (name and name == value.casefold()):
            return True
    return False


def _message_info(kwargs: dict[str, Any]) -> dict[str, Any]:
    message = kwargs.get("message")
    info = message.get("message_info") if isinstance(message, dict) else None
    return info if isinstance(info, dict) else {}


def _message_group_name(kwargs: dict[str, Any]) -> str:
    group = _message_info(kwargs).get("group_info")
    return str(group.get("group_name") or "").strip() if isinstance(group, dict) else ""


def _is_private_command(kwargs: dict[str, Any]) -> bool:
    info = _message_info(kwargs)
    return "group_info" in info and info["group_info"] is None


def _output_platform(kwargs: dict[str, Any], platform: str) -> str:
    meta = _message_info(kwargs).get("additional_config")
    if platform == "qq" and isinstance(meta, dict) and (meta.get("qq_official_user_openid") or meta.get("qq_official_group_openid")):
        return "qq_official"
    return platform


def _extract_user_texts_from_items(items: list[Any], max_items: int = 6) -> list[str]:
    """从 Planner Context Items 尾部提取最近的用户消息纯文本（排除自身注入的小抄）。"""
    texts: list[str] = []
    for item in reversed(items):
        if not isinstance(item, dict) or item.get("item_type") != "UserMessageItem":
            continue
        parts = item.get("parts")
        if not isinstance(parts, list):
            continue
        buf: list[str] = []
        for part in parts:
            if isinstance(part, dict) and part.get("type") == "text":
                t = str(part.get("text") or "")
                # 剔除本插件历史注入过的小抄段落，防止自我触发
                if _HINT_MARKER in t:
                    t = t.split(_HINT_MARKER, 1)[0]
                if t.strip():
                    buf.append(t.strip())
        if buf:
            texts.append("\n".join(buf))
            if len(texts) >= max_items:
                break
    texts.reverse()
    return texts


def _append_note_to_last_user_item(items: list[Any], note: str) -> bool:
    """将一次性参考提示追加到最后一条 UserMessageItem（缓存友好，不触碰 System Prompt）。"""
    for item in reversed(items):
        if not isinstance(item, dict) or item.get("item_type") != "UserMessageItem":
            continue
        parts = item.get("parts")
        if not isinstance(parts, list) or not parts:
            continue
        last = parts[-1]
        if isinstance(last, dict) and last.get("type") == "text":
            base = str(last.get("text") or "")
            if _HINT_MARKER in base:
                return False
            last["text"] = f"{base}\n{note}" if base else note
            return True
        parts.append({"type": "text", "text": note})
        return True
    return False


# ============================================================
# 插件主体
# ============================================================


class ArknightsWikiPlugin(MaiBotPlugin):
    """明日方舟wiki插件主类。"""

    config_model = ArknightsWikiConfig

    def __init__(self) -> None:
        super().__init__()
        self._store = None
        self._matcher: Optional[ArknightsContextMatcher] = None
        self._prts: Optional[PRTSClient] = None
        self._data_dir: Path = _resolve_data_dir()
        self._recent_hint_sessions: dict[str, float] = {}
        self._recent_cards: dict[tuple[str, str, str], float] = {}
        # 已渲染卡片 PNG(base64) 缓存：(干员, 板块, 细分, 倍率) -> (时间戳, base64)，1 小时有效、最多 24 张
        self._card_png: dict[tuple[str, str, str, float], tuple[float, str]] = {}
        # 帮助图内容固定：倍率 -> base64，进程内只截一次
        self._help_png: dict[float, str] = {}
        self._stream_meta_cache: dict[str, tuple[str, str, str, str]] = {}
        self._stream_meta_ts: float = 0.0
        self._no_image_platforms: set[str] = {"bot_console", "weixin", "wechat"}
        self._reminder_task: Any = None

    @property
    def cfg(self) -> ArknightsWikiConfig:
        try:
            c = self.config
            if isinstance(c, ArknightsWikiConfig):
                return c
        except RuntimeError:
            pass
        return ArknightsWikiConfig()

    def _runtime_alias_path(self) -> Path:
        return self._data_dir / "custom_aliases.json"

    def _merged_custom_aliases(self) -> list[str]:
        cfg_aliases = list(self.cfg.glossary.custom_aliases or [])
        rt_aliases = load_runtime_aliases(self._runtime_alias_path())
        # 运行时动态添加的放在后面以覆盖同名旧项
        return cfg_aliases + rt_aliases

    def _rebuild_matcher(self) -> None:
        glossary_path = Path(__file__).resolve().parent / "assets" / "glossary.json"
        cfg = self.cfg
        self._matcher = ArknightsContextMatcher(
            glossary_path=glossary_path,
            custom_aliases=self._merged_custom_aliases(),
            hot_ttl_seconds=float(cfg.context.hot_session_ttl_seconds),
            custom_alias_force_tier1=bool(cfg.glossary.custom_alias_force_tier1),
        )
        if self._prts is not None:
            self._prts.matcher = self._matcher

    def _build_services(self) -> None:
        from .store import WikiCacheStore

        try:
            ctx = self.ctx
        except RuntimeError:
            ctx = None
        ctx_paths = getattr(ctx, "paths", None) if ctx else None
        data_dir = Path(ctx_paths.data_dir) if ctx_paths and getattr(ctx_paths, "data_dir", None) else _resolve_data_dir()
        self._data_dir = data_dir
        cfg = self.cfg
        self._store = WikiCacheStore(data_dir=data_dir, image_max_mb=cfg.cache.image_cache_max_mb)
        self._rebuild_matcher()
        assert self._matcher is not None
        self._prts = PRTSClient(
            store=self._store,
            matcher=self._matcher,
            api_url=cfg.prts.api_url,
            user_agent=cfg.prts.user_agent,
            timeout_seconds=cfg.prts.timeout_seconds,
            min_interval_ms=cfg.prts.min_request_interval_ms,
            page_ttl_hours=cfg.cache.page_ttl_hours,
            thumb_width=cfg.prts.illustration_thumb_width,
        )

    def _stop_reminder_task(self) -> None:
        task = self._reminder_task
        self._reminder_task = None
        if task is not None and not task.done():
            task.cancel()

    def _start_reminder_task_if_needed(self) -> None:
        import asyncio

        self._stop_reminder_task()
        if not (self.cfg.plugin.enabled and self.cfg.reminder.enable_event_reminder):
            return
        try:
            loop = asyncio.get_running_loop()
            self._reminder_task = loop.create_task(self._reminder_loop(), name="ak_event_reminder")
        except RuntimeError:
            pass

    def _reminder_sent_path(self) -> Path:
        return self._data_dir / "reminder_sent.json"

    def _load_reminder_sent(self) -> dict[str, int]:
        import json

        p = self._reminder_sent_path()
        if not p.exists():
            return {}
        try:
            raw = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                return {str(k): int(v) for k, v in raw.items() if isinstance(v, (int, float))}
        except Exception:
            pass
        return {}

    def _save_reminder_sent(self, data: dict[str, int]) -> None:
        import json

        p = self._reminder_sent_path()
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            now_ts = int(time.time())
            # 自动清理已过期超过 7 天的推送记录，避免文件膨胀
            cleaned = {k: v for k, v in data.items() if (now_ts - v) < 7 * 86400}
            p.write_text(json.dumps(cleaned, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:
            pass

    async def _reminder_loop(self) -> None:
        import asyncio

        # 启动后稍作延迟，待宿主会话流初始化完成再进入巡检
        await asyncio.sleep(20)
        while True:
            try:
                await self._check_and_push_event_reminders()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if self.ctx:
                    self.ctx.logger.warning(f"[明日方舟wiki插件] 活动到期提醒巡检异常: {_exc_text(exc)}")
            await asyncio.sleep(600)

    async def _check_and_push_event_reminders(self) -> None:
        import asyncio
        import datetime

        if not self._is_enabled() or not self.cfg.reminder.enable_event_reminder or not self._prts or not self.ctx:
            return
        tz_cn = datetime.timezone(datetime.timedelta(hours=8))
        now_dt = datetime.datetime.now(tz_cn)
        target_hour = int(self.cfg.reminder.reminder_hour)
        if now_dt.hour != target_hour:
            return

        chat_cap = getattr(self.ctx, "chat", None)
        if not chat_cap or not hasattr(chat_cap, "get_all_streams"):
            return

        data = await self._prts.get_current_events_struct()
        now_ts = int(now_dt.timestamp())
        horizon = int(self.cfg.reminder.remind_before_hours) * 3600

        closing_events = [
            e for e in (data.get("events") or [])
            if not e.get("is_soon") and not e.get("is_long") and 0 < (int(e.get("end_ts") or 0) - now_ts) <= horizon
        ]
        closing_gachas = (
            [
                g for g in (data.get("gachas") or [])
                if not g.get("is_soon") and not g.get("is_long") and 0 < (int(g.get("end_ts") or 0) - now_ts) <= horizon
            ]
            if self.cfg.reminder.include_gacha
            else []
        )
        if not closing_events and not closing_gachas:
            return

        streams = await chat_cap.get_all_streams(all_platforms=True)
        if not isinstance(streams, list) or not streams:
            return

        sent_map = await asyncio.to_thread(self._load_reminder_sent)
        changed = False

        for s in streams:
            if not isinstance(s, dict):
                continue
            sid = str(s.get("stream_id") or "").strip()
            gid = str(s.get("group_id") or "").strip()
            gname = str(s.get("group_name") or "").strip()
            plat = str(s.get("platform") or "").strip().lower()
            # 仅推送到群聊，且严格遵守群聊开关与黑/白名单
            if not sid or not gid:
                continue
            if not self._scope_allowed(group_id=gid, group_name=gname, platform=plat):
                continue

            todo_ev = [
                e for e in closing_events
                if f"{sid}|ev|{e['name']}|{e['end_ts']}" not in sent_map
            ]
            todo_ga = [
                g for g in closing_gachas
                if f"{sid}|ga|{g['name']}|{g['end_ts']}" not in sent_map
            ]
            if not todo_ev and not todo_ga:
                continue

            lines = ["【明日方舟 · 活动即将关闭提醒】"]
            for e in todo_ev:
                lines.append(f"• 活动「{e['name']}」{e['remain']}（{e['period']}）")
            for g in todo_ga:
                lines.append(f"• 卡池「{g['name']}」{g['remain']}（{g['period']}）")
            lines.append("记得及时清理理智、搬空商店与领取奖励～")
            msg = "\n".join(lines)

            try:
                ok_txt = _send_ok(await self.ctx.send.text(msg, stream_id=sid))
                if ok_txt:
                    for e in todo_ev:
                        sent_map[f"{sid}|ev|{e['name']}|{e['end_ts']}"] = now_ts
                    for g in todo_ga:
                        sent_map[f"{sid}|ga|{g['name']}|{g['end_ts']}"] = now_ts
                    changed = True
                    # 顺便附发一张对应的海报卡片图（若有）
                    if self.cfg.render.enable_cards and not self._platform_no_image(plat):
                        focus = "events" if todo_ev and not todo_ga else ("gacha" if todo_ga and not todo_ev else "all")
                        await self._send_events_card_or_text(sid, focus=focus, platform=plat)
            except Exception as exc:
                self.ctx.logger.warning(f"[明日方舟wiki插件] 向群 {gid} 发送活动到期提醒失败: {_exc_text(exc)}")

        if changed:
            await asyncio.to_thread(self._save_reminder_sent, sent_map)

    async def on_load(self) -> bool:
        self._build_services()
        if self._store and self._prts:
            try:
                import asyncio
                await asyncio.to_thread(self._store.cleanup_expired, self._prts.page_ttl_seconds)
            except Exception:
                pass
        self._start_reminder_task_if_needed()
        if self.ctx:
            cache_path = self._store.data_dir if self._store else _resolve_data_dir()
            self.ctx.logger.info(
                f"[明日方舟wiki插件] 已就绪（PRTS={self.cfg.prts.api_url}，缓存目录={cache_path}）"
            )
        return True

    async def on_config_update(self, scope: str, config_data: dict[str, Any], version: str) -> None:
        if scope != "self":
            return
        self._stop_reminder_task()
        if self._prts:
            await self._prts.close()
        self._build_services()
        self._start_reminder_task_if_needed()
        if self.ctx:
            self.ctx.logger.info("[明日方舟wiki插件] 配置已热更新并重建客户端")

    async def on_unload(self) -> None:
        self._stop_reminder_task()
        if self._prts:
            await self._prts.close()

    def _is_enabled(self) -> bool:
        return bool(self.cfg.plugin.enabled and self._matcher and self._prts)

    def _scope_allowed(self, *, group_id: str = "", user_id: str = "", group_name: str = "", platform: str = "") -> bool:
        sc = self.cfg.scope
        if group_id:
            if not sc.allow_group:
                return False
            mode = _LIST_MODE_MAP.get(str(sc.group_list_mode or "").strip().lower(), "不限制")
            if mode == "不限制":
                return True
            hit = _scope_list_hit(sc.group_list, group_id, platform, group_name=group_name, group=True)
            return hit if mode == "仅白名单" else (not hit)

        if user_id:
            if not sc.allow_private:
                return False
            mode = _LIST_MODE_MAP.get(str(sc.private_list_mode or "").strip().lower(), "不限制")
            if mode == "不限制":
                return True
            hit = _scope_list_hit(sc.private_list, user_id, platform)
            return hit if mode == "仅白名单" else (not hit)
        return True

    async def _resolve_stream_meta(self, stream_id: str) -> tuple[str, str, str, str]:
        """按 stream_id 轻量反查 (platform, group_id, user_id, group_name)，缓存 60 秒。"""
        sid = str(stream_id or "").strip()
        if not sid:
            return "", "", "", ""
        now = time.time()
        # 先看整表新鲜度：60 秒内直接按缓存回答（含未命中）。
        # 旧逻辑把新鲜度判断挂在「命中」分支上，导致适配器没上报的 stream 每条消息都要全量拉一次流列表。
        if (now - self._stream_meta_ts) < 60.0:
            return self._stream_meta_cache.get(sid, ("", "", "", ""))
        try:
            ctx = self.ctx
        except RuntimeError:
            ctx = None
        chat = getattr(ctx, "chat", None) if ctx else None
        if chat and hasattr(chat, "get_all_streams"):
            try:
                streams = await chat.get_all_streams(platform="all_platforms")
                if isinstance(streams, list):
                    fresh: dict[str, tuple[str, str, str, str]] = {}
                    for st in streams:
                        if not isinstance(st, dict):
                            continue
                        k = str(st.get("stream_id") or st.get("session_id") or "").strip()
                        if not k:
                            continue
                        plat = str(st.get("platform") or "").strip().lower()
                        gid = str(st.get("group_id") or "").strip()
                        uid = str(st.get("user_id") or "").strip()
                        gname = str(st.get("group_name") or st.get("name") or "").strip()
                        fresh[k] = (plat, gid, uid, gname)
                    if fresh:
                        self._stream_meta_cache = fresh
            except Exception:
                pass
        # 无论成功、空结果还是异常都记时间戳：否则宿主返回空列表/一直报错时，每条消息都会重试一次全量拉取
        self._stream_meta_ts = now
        return self._stream_meta_cache.get(sid, ("", "", "", ""))

    async def _scope_allowed_async(
        self,
        *,
        stream_id: str = "",
        group_id: str = "",
        user_id: str = "",
        platform: str = "",
        group_name: str = "",
    ) -> tuple[bool, str]:
        """全平台异步生效范围校验，返回 (是否放行, 识别到的平台名)。"""
        sc = self.cfg.scope
        group_mode = _LIST_MODE_MAP.get(str(sc.group_list_mode or "").strip().lower(), "不限制")
        gname = group_name
        plat = str(platform or "").strip().lower()
        needs_group_name = group_id and not gname and group_mode != "不限制" and sc.group_list
        if stream_id and (not group_id or not plat or needs_group_name):
            m_plat, m_gid, m_uid, m_gname = await self._resolve_stream_meta(stream_id)
            plat = plat or m_plat
            group_id = group_id or m_gid
            if not group_id:
                user_id = user_id or m_uid
            gname = gname or m_gname
        if not group_id and not user_id:
            private_mode = _LIST_MODE_MAP.get(str(sc.private_list_mode or "").strip().lower(), "不限制")
            if not sc.allow_group or not sc.allow_private or group_mode != "不限制" or private_mode != "不限制":
                return False, plat
        allowed = self._scope_allowed(group_id=group_id, user_id=user_id, group_name=gname, platform=plat)
        return allowed, plat

    def _platform_no_image(self, platform: str) -> bool:
        p = str(platform or "").strip().lower()
        return bool(p and p in self._no_image_platforms)

    @staticmethod
    def _sanitize_for_platform(text: str, platform: str) -> str:
        """针对 QQ 官方机器人等禁止外链 URL 的平台自动剥离裸链接，避免消息被平台拦截。"""
        p = str(platform or "").strip().lower()
        if p in ("qq_official", "qqbot", "weixin_clawbot"):
            text = re.sub(r"\n?PRTS条目[：:]\s*https?://\S+", "", text)
            text = re.sub(r"https?://\S+", "（已略去链接，可在 PRTS Wiki 搜索查看）", text)
        return text.strip()

    async def _can_clear_cache(
        self, *, group_id: str = "", user_id: str = "", platform: str = "", private: bool = False,
    ) -> bool:
        """校验缓存清理与别名增删权限。"""
        if not user_id:
            return False
        admins = [item for item in self.cfg.commands.admin_users if item.strip()]
        if admins:
            return _scope_list_hit(admins, user_id, platform)

        try:
            config = getattr(self.ctx, "config", None)
        except RuntimeError:
            config = None
        if config is not None:
            for key in ("plugin.permission", "permission.master_users", "bot.master_users"):
                try:
                    value = await config.get(key)
                except Exception:
                    return False
                if isinstance(value, str):
                    value = [value]
                if isinstance(value, (list, tuple)):
                    admins = [
                        f"{item[0]}:{item[1]}" if isinstance(item, (list, tuple)) and len(item) >= 2 else str(item)
                        for item in value if item
                    ]
                    if admins:
                        return _scope_list_hit(admins, user_id, platform)
        return private and not group_id

    @staticmethod
    def _extract_stream_and_ids(kwargs: dict[str, Any]) -> tuple[str, str, str, str]:
        stream_id = str(kwargs.get("stream_id") or kwargs.get("session_id") or "").strip()
        group_id = str(kwargs.get("group_id") or "").strip()
        user_id = str(kwargs.get("user_id") or kwargs.get("sender_id") or "").strip()
        platform = str(kwargs.get("platform") or "").strip().lower()
        msg = kwargs.get("message")
        if isinstance(msg, dict):
            minfo = msg.get("message_info") if isinstance(msg.get("message_info"), dict) else {}
            ginfo = minfo.get("group_info") if isinstance(minfo.get("group_info"), dict) else {}
            uinfo = minfo.get("user_info") if isinstance(minfo.get("user_info"), dict) else {}
            stream_id = stream_id or str(msg.get("stream_id") or msg.get("session_id") or minfo.get("session_id") or "")
            group_id = group_id or str(msg.get("group_id") or ginfo.get("group_id") or "")
            user_id = user_id or str(msg.get("user_id") or msg.get("sender_id") or uinfo.get("user_id") or "")
            platform = platform or str(msg.get("platform") or minfo.get("platform") or "").strip().lower()
        return stream_id, group_id, user_id, _output_platform(kwargs, platform)

    # ============================================================
    # Hook 1 & 2：微秒级方舟黑话语境感知（零网络、零破坏前缀缓存、受黑白名单约束）
    # ============================================================

    @HookHandler(
        "maisaka.planner.before_request",
        name="arknights_planner_hint",
        description="检测聊天上下文中的明日方舟干员名与黑话，按双档门控注入极简参考小抄",
        mode=HookMode.BLOCKING,
        order=HookOrder.NORMAL,
        timeout_ms=1500,
    )
    async def on_planner_before_request(self, **kwargs: Any) -> dict[str, Any]:
        if not self._is_enabled() or not self.cfg.context.enable_planner_hint:
            return {"action": "continue"}
        items = kwargs.get("items")
        if not isinstance(items, list) or not items:
            return {"action": "continue"}
        session_id, gid, uid, plat = self._extract_stream_and_ids(kwargs)
        allowed, _ = await self._scope_allowed_async(stream_id=session_id, group_id=gid, user_id=uid, platform=plat, group_name=_message_group_name(kwargs))
        if not allowed:
            return {"action": "continue"}

        recent_texts = _extract_user_texts_from_items(items, max_items=6)
        if not recent_texts or not self._matcher:
            return {"action": "continue"}

        matches = self._matcher.scan(
            session_id,
            recent_texts,
            max_items=self.cfg.context.max_hint_items,
        )
        if not matches:
            return {"action": "continue"}

        hint = self._matcher.build_planner_hint(matches)
        if hint and _append_note_to_last_user_item(items, hint):
            if session_id:
                self._recent_hint_sessions[session_id] = time.time()
                # 只用于 30 秒内的 replyer 联动，防止长期运行后字典无限增长
                if len(self._recent_hint_sessions) > 512:
                    now_ts = time.time()
                    self._recent_hint_sessions = {
                        k: t for k, t in self._recent_hint_sessions.items() if now_ts - t <= 30.0
                    }
            return {"action": "continue", "modified_kwargs": kwargs}
        return {"action": "continue"}

    @HookHandler(
        "maisaka.replyer.before_request",
        name="arknights_replyer_style",
        description="命中方舟语境时向 Replyer 注入轻量口吻提示，防止机械背百科",
        mode=HookMode.BLOCKING,
        order=HookOrder.NORMAL,
        timeout_ms=1000,
    )
    async def on_replyer_before_request(self, **kwargs: Any) -> dict[str, Any]:
        if not self._is_enabled() or not self.cfg.context.enable_replyer_style_hint:
            return {"action": "continue"}
        session_id, gid, uid, plat = self._extract_stream_and_ids(kwargs)
        ts = self._recent_hint_sessions.get(session_id)
        if not ts or (time.time() - ts) > 30.0:
            return {"action": "continue"}
        allowed, _ = await self._scope_allowed_async(stream_id=session_id, group_id=gid, user_id=uid, platform=plat, group_name=_message_group_name(kwargs))
        if not allowed:
            return {"action": "continue"}
        extra = str(kwargs.get("extra_prompt") or "")
        style_note = "聊方舟时最多说1～2句自然短口语（35字内），不背百科不刷屏。"
        if style_note not in extra:
            kwargs["extra_prompt"] = f"{extra}\n{style_note}".strip()
            return {"action": "continue", "modified_kwargs": kwargs}
        return {"action": "continue"}

    # ============================================================
    # 高清罗德岛战术卡片渲染与发送（2x Retina，15 秒防重防刷屏）
    # ============================================================

    async def _render_and_send_card(
        self,
        stream_id: str,
        target: str,
        section: str,
        detail: str = "",
        *,
        dedup_seconds: float = 15.0,
        platform: str = "",
    ) -> tuple[bool, str]:
        """将干员材料/模组/总览/技能渲染为 2x 高清黑曜石金箔风格卡片并直发，返回 (是否发图成功, 干员标准名)。"""
        if not self.cfg.render.enable_cards or not self._prts or not self.ctx or not stream_id:
            return False, ""
        if self._platform_no_image(platform):
            return False, ""
        render = getattr(self.ctx, "render", None)
        if render is None:
            return False, ""

        sec = (section or "overview").strip().lower()
        if sec not in ("overview", "skills", "modules", "materials"):
            return False, ""

        op_name = ""
        try:
            op = await self._prts.get_operator_struct(target)
            if not op:
                return False, ""
            op_name = op["name"]

            # 同一会话短时间内避免重复发同类卡片。
            card_group = "materials_or_modules" if sec in ("modules", "materials") else "overview_or_skills"
            dedup_key = (stream_id, op_name, card_group)
            now = time.time()
            if dedup_seconds > 0 and (now - self._recent_cards.get(dedup_key, 0.0)) < dedup_seconds:
                return False, op_name

            scale = float(self.cfg.render.card_scale or 2.0)
            ckey = (op_name, sec, (detail or "").strip().lower(), scale)
            hit = self._card_png.get(ckey)
            if hit and now - hit[0] < 3600.0:
                b64 = hit[1]
            else:
                html_str, vp_w, vp_h, complete = await build_operator_card_html(
                    self._prts,
                    op,
                    section=sec,
                    detail=detail,
                )
                res = await render.html2png(
                    html_str,
                    selector=".card",
                    viewport={"width": vp_w + 40, "height": max(320, vp_h)},
                    device_scale_factor=scale,
                    omit_background=True,
                    wait_until="domcontentloaded",
                    allow_network=False,
                    render_timeout_ms=15000,
                )
                b64 = res.get("image_base64") if isinstance(res, dict) else None
                if not b64:
                    return False, op_name
                if complete:
                    self._card_png[ckey] = (now, str(b64))
                    while len(self._card_png) > 24:
                        self._card_png.pop(next(iter(self._card_png)))
            sent = await self.ctx.send.image(str(b64), stream_id=stream_id)
            if _send_ok(sent):
                self._recent_cards[dedup_key] = now
                if len(self._recent_cards) > 256:
                    self._recent_cards = {k: t for k, t in self._recent_cards.items() if now - t < 60.0}
                return True, op_name
        except Exception as exc:
            if self.ctx:
                self.ctx.logger.warning(f"[明日方舟wiki插件] 卡片渲染或发送失败（已降级纯文本）: {_exc_text(exc)}")
        return False, op_name

    async def _command_reply(
        self, stream_id: str, text: str, response: str, *, platform: str = "",
    ) -> tuple[bool, str, int]:
        """报告发送结果；已处理的指令即使发送失败也不进入普通聊天。"""
        text = self._sanitize_for_platform(text, platform)
        try:
            if text and _send_ok(await self.ctx.send.text(text, stream_id=stream_id)):
                return True, response, 2
        except Exception as exc:
            self.ctx.logger.warning(f"[明日方舟wiki插件] 指令回复发送异常（{type(exc).__name__}）: {exc!r}")
        else:
            self.ctx.logger.warning("[明日方舟wiki插件] 指令回复未获得成功发送回执")
        return False, "指令回复发送失败", 2

    async def _send_help_card_or_text(self, stream_id: str, platform: str = "") -> tuple[bool, str, int]:
        """优先发送轻量化总帮助 2x 渲染图，平台不支持或渲染不可用时自动降级纯文本。"""
        if self.ctx and self.cfg.render.enable_cards and not self._platform_no_image(platform):
            render = getattr(self.ctx, "render", None)
            if render is not None:
                try:
                    scale = float(self.cfg.render.card_scale or 2.0)
                    b64 = self._help_png.get(scale)
                    if not b64:
                        html_str, vp_w, vp_h = build_help_card_html()
                        res = await render.html2png(
                            html_str,
                            selector=".card",
                            viewport={"width": vp_w + 40, "height": vp_h + 40},
                            device_scale_factor=scale,
                            omit_background=True,
                            wait_until="domcontentloaded",
                            allow_network=False,
                            render_timeout_ms=10000,
                        )
                        b64 = res.get("image_base64") if isinstance(res, dict) else None
                        if b64:
                            self._help_png[scale] = str(b64)
                    if b64 and _send_ok(await self.ctx.send.image(str(b64), stream_id=stream_id)):
                        return True, "已发送帮助卡片", 2
                except Exception as exc:
                    self.ctx.logger.warning(f"[明日方舟wiki插件] 帮助图渲染失败（已降级纯文本）: {_exc_text(exc)}")

        help_msg = (
            "【明日方舟wiki · 指令手册】\n"
            "• /方舟 干员 [板块]  — 总览 · 技能 · 模组 · 材料 · 档案\n"
            "• /公招 词条 或 附图  — 算公招保底（支持带图或回复截图识图）\n"
            "• /方舟 敌人 名称 [级别]  — 查 BOSS 与敌人数值、免疫与机制\n"
            "• /立绘 干员 [版本]  — 精二 · 初始 · 皮肤 · 列表\n"
            "• /方舟 总计 干员 [范围]  — 精二专三模组材料总账\n"
            "• /方舟 语音 干员 [关键词]  — 干员台词彩蛋（仅发文字，不含语音）\n"
            "• /方舟 卡池 · /方舟 活动  — 当期寻访与活动官方海报图\n"
            "• /方舟 别名 列表·添加·删除  — 自定义黑话（增删限管理）\n"
            "• /方舟 缓存 · /方舟 清理缓存  — 查看或清空本地缓存（清理限管理）"
        )
        return await self._command_reply(stream_id, help_msg, "已发送帮助文本", platform=platform)

    async def _send_recruit_card_or_text(
        self,
        stream_id: str,
        tags: list[str],
        *,
        platform: str = "",
    ) -> tuple[bool, str, int]:
        """计算公开招募保底组合；优先发送黑曜石金箔半身像卡片图，不可用时自动降级纯文本。"""
        if not self._prts or not self.ctx:
            return False, "", 0
        try:
            data = await self._prts.calculate_recruit_struct(tags)
        except Exception as exc:
            return await self._command_reply(stream_id, f"公招计算失败：{_exc_text(exc)}", "公招计算失败", platform=platform)

        if not data.get("ok"):
            msg = str(data.get("error") or "未找到高星保底组合。")
            return await self._command_reply(stream_id, msg, "已发送公招提示", platform=platform)

        if self.cfg.render.enable_cards and not self._platform_no_image(platform):
            render = getattr(self.ctx, "render", None)
            if render is not None:
                try:
                    now = time.time()
                    scale = float(self.cfg.render.card_scale or 2.0)
                    ckey = ("__recruit__", "|".join(sorted(data["tags"])), "", scale)
                    hit = self._card_png.get(ckey)
                    if hit and now - hit[0] < 3600.0:
                        b64 = hit[1]
                    else:
                        html_str, vp_w, vp_h, complete = await build_recruit_card_html(self._prts, data)
                        res = await render.html2png(
                            html_str,
                            selector=".card",
                            viewport={"width": vp_w + 40, "height": max(360, vp_h)},
                            device_scale_factor=scale,
                            omit_background=True,
                            wait_until="domcontentloaded",
                            allow_network=False,
                            render_timeout_ms=15000,
                        )
                        b64 = res.get("image_base64") if isinstance(res, dict) else None
                        if b64 and complete:
                            self._card_png[ckey] = (now, str(b64))
                            while len(self._card_png) > 24:
                                self._card_png.pop(next(iter(self._card_png)))
                    if b64 and _send_ok(await self.ctx.send.image(str(b64), stream_id=stream_id)):
                        return True, "已发送公招卡片图", 2
                except Exception as exc:
                    self.ctx.logger.warning(f"[明日方舟wiki插件] 公招卡片渲染或发送失败: {_exc_text(exc)}")
                return await self._command_reply(stream_id, "公招卡片图渲染或发送失败，请稍后重试。", "公招出图失败", platform=platform)

        reply = self._prts.format_recruit_text(data)
        return await self._command_reply(stream_id, reply, "已发送公招计算结果", platform=platform)

    async def _send_enemy_card_or_text(
        self,
        stream_id: str,
        target: str,
        detail: str = "",
        *,
        platform: str = "",
    ) -> tuple[bool, str, int]:
        """查询 BOSS/敌人数值与机制；优先发黑曜石档案卡片图，不可用时自动降级纯文本。"""
        if not self._prts or not self.ctx:
            return False, "", 0
        try:
            en = await self._prts.get_enemy_struct(target, detail=detail)
        except Exception as exc:
            return await self._command_reply(stream_id, f"查询敌人信息失败：{_exc_text(exc)}", "查询敌人失败", platform=platform)

        if not en:
            return await self._command_reply(stream_id, f"未在 PRTS Wiki 找到敌人或 BOSS「{target}」。", "未找到敌人", platform=platform)

        if self.cfg.render.enable_cards and not self._platform_no_image(platform):
            render = getattr(self.ctx, "render", None)
            if render is not None:
                try:
                    now = time.time()
                    scale = float(self.cfg.render.card_scale or 2.0)
                    ckey = ("__enemy__", en["name"], str(en.get("level_index", 0)), scale)
                    hit = self._card_png.get(ckey)
                    if hit and now - hit[0] < 3600.0:
                        b64 = hit[1]
                    else:
                        html_str, vp_w, vp_h, complete = await build_enemy_card_html(self._prts, en)
                        res = await render.html2png(
                            html_str,
                            selector=".card",
                            viewport={"width": vp_w + 40, "height": max(360, vp_h)},
                            device_scale_factor=scale,
                            omit_background=True,
                            wait_until="domcontentloaded",
                            allow_network=False,
                            render_timeout_ms=15000,
                        )
                        b64 = res.get("image_base64") if isinstance(res, dict) else None
                        if b64 and complete:
                            self._card_png[ckey] = (now, str(b64))
                            while len(self._card_png) > 24:
                                self._card_png.pop(next(iter(self._card_png)))
                    if b64 and _send_ok(await self.ctx.send.image(str(b64), stream_id=stream_id)):
                        return True, f"已发送 {en['name']} 敌方档案卡", 2
                except Exception as exc:
                    self.ctx.logger.warning(f"[明日方舟wiki插件] 敌人卡片渲染失败（已降级纯文本）: {_exc_text(exc)}")

        return await self._command_reply(stream_id, self._prts.format_enemy_text(en), f"已发送 {en['name']} 敌人资料", platform=platform)

    async def _send_events_card_or_text(
        self,
        stream_id: str,
        *,
        focus: str = "events",
        platform: str = "",
    ) -> tuple[bool, str, int]:
        """查询国服当期寻访卡池或当期活动；优先发黑曜石官方横幅海报卡，不可用时自动降级纯文本。"""
        if not self._prts or not self.ctx:
            return False, "", 0
        try:
            data = await self._prts.get_current_events_struct()
        except Exception as exc:
            return await self._command_reply(stream_id, f"查询当期活动与卡池失败：{_exc_text(exc)}", "查询活动失败", platform=platform)

        if focus == "gacha":
            items = data.get("gachas") or []
        elif focus == "events":
            items = data.get("events") or []
        else:
            items = (data.get("gachas") or []) + (data.get("events") or [])
        if not items:
            tip = "暂未从 PRTS 获取到当期寻访卡池信息，请稍后再试。" if focus == "gacha" else "暂未从 PRTS 获取到当期活动信息，请稍后再试。"
            return await self._command_reply(stream_id, tip, "暂无当期数据", platform=platform)

        if self.cfg.render.enable_cards and not self._platform_no_image(platform):
            render = getattr(self.ctx, "render", None)
            if render is not None:
                try:
                    now = time.time()
                    scale = float(self.cfg.render.card_scale or 2.0)
                    ckey = ("__events__", focus, "", scale)
                    hit = self._card_png.get(ckey)
                    if hit and now - hit[0] < 900.0:
                        b64 = hit[1]
                    else:
                        html_str, vp_w, vp_h, complete = await build_events_card_html(self._prts, data, focus=focus)
                        res = await render.html2png(
                            html_str,
                            selector=".card",
                            viewport={"width": vp_w + 40, "height": max(380, vp_h)},
                            device_scale_factor=scale,
                            omit_background=True,
                            wait_until="domcontentloaded",
                            allow_network=False,
                            render_timeout_ms=15000,
                        )
                        b64 = res.get("image_base64") if isinstance(res, dict) else None
                        if b64 and complete:
                            self._card_png[ckey] = (now, str(b64))
                            while len(self._card_png) > 24:
                                self._card_png.pop(next(iter(self._card_png)))
                    if b64 and _send_ok(await self.ctx.send.image(str(b64), stream_id=stream_id)):
                        label = "已发送当期寻访卡池图" if focus == "gacha" else "已发送当期活动海报图"
                        return True, label, 2
                except Exception as exc:
                    self.ctx.logger.warning(f"[明日方舟wiki插件] 活动/卡池卡片渲染失败（已降级纯文本）: {_exc_text(exc)}")

        reply = self._prts.format_current_events_text(data, focus=focus)
        label = "已发送当期寻访卡池" if focus == "gacha" else "已发送当期活动"
        return await self._command_reply(stream_id, reply, label, platform=platform)

    async def _send_summary_card_or_text(
        self,
        stream_id: str,
        target: str,
        detail: str = "",
        *,
        platform: str = "",
    ) -> tuple[bool, str, int]:
        """汇总干员养成材料总账；优先发黑曜石金箔总账卡片图，不可用时自动降级纯文本。"""
        if not self._prts or not self.ctx:
            return False, "", 0
        try:
            data = await self._prts.get_material_summary_struct(target, detail=detail)
        except Exception as exc:
            return await self._command_reply(stream_id, f"计算材料总账失败：{_exc_text(exc)}", "计算材料总账失败", platform=platform)

        if not data.get("ok"):
            return await self._command_reply(stream_id, str(data.get("error") or "未找到养成材料总账数据。"), "未找到材料总账", platform=platform)

        op_name = data["op"]["name"]
        if self.cfg.render.enable_cards and not self._platform_no_image(platform):
            render = getattr(self.ctx, "render", None)
            if render is not None:
                try:
                    now = time.time()
                    scale = float(self.cfg.render.card_scale or 2.0)
                    ckey = ("__summary__", op_name, "|".join(data.get("included") or []), scale)
                    hit = self._card_png.get(ckey)
                    if hit and now - hit[0] < 3600.0:
                        b64 = hit[1]
                    else:
                        html_str, vp_w, vp_h, complete = await build_material_summary_card_html(self._prts, data)
                        res = await render.html2png(
                            html_str,
                            selector=".card",
                            viewport={"width": vp_w + 40, "height": max(340, vp_h)},
                            device_scale_factor=scale,
                            omit_background=True,
                            wait_until="domcontentloaded",
                            allow_network=False,
                            render_timeout_ms=15000,
                        )
                        b64 = res.get("image_base64") if isinstance(res, dict) else None
                        if b64 and complete:
                            self._card_png[ckey] = (now, str(b64))
                            while len(self._card_png) > 24:
                                self._card_png.pop(next(iter(self._card_png)))
                    if b64 and _send_ok(await self.ctx.send.image(str(b64), stream_id=stream_id)):
                        return True, f"已发送 {op_name} 材料总账卡", 2
                except Exception as exc:
                    self.ctx.logger.warning(f"[明日方舟wiki插件] 材料总账卡片渲染失败（已降级纯文本）: {_exc_text(exc)}")

        return await self._command_reply(
            stream_id, self._prts.format_material_summary_text(data), f"已发送 {op_name} 材料总账", platform=platform,
        )

    async def _send_voice_card_or_text(
        self,
        stream_id: str,
        target: str,
        keyword: str = "",
        *,
        platform: str = "",
    ) -> tuple[bool, str, int]:
        """查询干员中文语音台词；优先发黑曜石金箔语音记录卡（纯文字台词），不可用时自动降级纯文本。"""
        if not self._prts or not self.ctx:
            return False, "", 0
        try:
            data = await self._prts.get_voice_lines_struct(target, keyword=keyword)
        except Exception as exc:
            return await self._command_reply(stream_id, f"查询干员语音失败：{_exc_text(exc)}", "查询语音失败", platform=platform)

        if not data.get("ok"):
            return await self._command_reply(stream_id, str(data.get("error") or "未找到干员语音记录。"), "未找到语音", platform=platform)

        op_name = data["op"]["name"]
        is_random = bool(data.get("is_random"))
        if self.cfg.render.enable_cards and not self._platform_no_image(platform):
            render = getattr(self.ctx, "render", None)
            if render is not None:
                try:
                    now = time.time()
                    scale = float(self.cfg.render.card_scale or 2.0)
                    ckey = ("__voice__", op_name, str(keyword or "").strip().lower(), scale)
                    hit = None if is_random else self._card_png.get(ckey)
                    if hit and now - hit[0] < 3600.0:
                        b64 = hit[1]
                    else:
                        html_str, vp_w, vp_h, complete = await build_voice_card_html(self._prts, data)
                        res = await render.html2png(
                            html_str,
                            selector=".card",
                            viewport={"width": vp_w + 40, "height": max(340, vp_h)},
                            device_scale_factor=scale,
                            omit_background=True,
                            wait_until="domcontentloaded",
                            allow_network=False,
                            render_timeout_ms=15000,
                        )
                        b64 = res.get("image_base64") if isinstance(res, dict) else None
                        if b64 and complete and not is_random:
                            self._card_png[ckey] = (now, str(b64))
                            while len(self._card_png) > 24:
                                self._card_png.pop(next(iter(self._card_png)))
                    if b64 and _send_ok(await self.ctx.send.image(str(b64), stream_id=stream_id)):
                        return True, f"已发送 {op_name} 语音记录卡", 2
                except Exception as exc:
                    self.ctx.logger.warning(f"[明日方舟wiki插件] 语音卡片渲染失败（已降级纯文本）: {_exc_text(exc)}")

        return await self._command_reply(
            stream_id, self._prts.format_voice_lines_text(data), f"已发送 {op_name} 语音台词", platform=platform,
        )

    async def _send_lore_card_or_text(
        self,
        stream_id: str,
        target: str,
        detail: str = "",
        *,
        platform: str = "",
    ) -> tuple[bool, str, int]:
        """查询干员机密档案；优先发黑曜石金箔档案卡，不可用或非干员时自动降级纯文本。"""
        if not self._prts or not self.ctx:
            return False, "", 0
        try:
            data = await self._prts.get_operator_lore_struct(target, detail=detail)
        except Exception:
            data = {"ok": False}

        if data.get("ok") and self.cfg.render.enable_cards and not self._platform_no_image(platform):
            render = getattr(self.ctx, "render", None)
            if render is not None:
                op_name = data["op"]["name"]
                try:
                    now = time.time()
                    scale = float(self.cfg.render.card_scale or 2.0)
                    ckey = ("__lore__", op_name, str(detail or "").strip().lower(), scale)
                    hit = self._card_png.get(ckey)
                    if hit and now - hit[0] < 3600.0:
                        b64 = hit[1]
                    else:
                        html_str, vp_w, vp_h, complete = await build_lore_card_html(self._prts, data)
                        res = await render.html2png(
                            html_str,
                            selector=".card",
                            viewport={"width": vp_w + 40, "height": max(360, vp_h)},
                            device_scale_factor=scale,
                            omit_background=True,
                            wait_until="domcontentloaded",
                            allow_network=False,
                            render_timeout_ms=15000,
                        )
                        b64 = res.get("image_base64") if isinstance(res, dict) else None
                        if b64 and complete:
                            self._card_png[ckey] = (now, str(b64))
                            while len(self._card_png) > 24:
                                self._card_png.pop(next(iter(self._card_png)))
                    if b64 and _send_ok(await self.ctx.send.image(str(b64), stream_id=stream_id)):
                        return True, f"已发送 {op_name} 机密档案卡", 2
                except Exception as exc:
                    self.ctx.logger.warning(f"[明日方舟wiki插件] 档案卡片渲染失败（已降级纯文本）: {_exc_text(exc)}")

        try:
            reply = await self._prts.query_wiki(target=target, section="lore", detail=detail)
        except Exception as exc:
            reply = f"查询干员档案失败：{_exc_text(exc)}"
        return await self._command_reply(stream_id, reply, "已发送干员档案", platform=platform)

    @staticmethod
    def _extract_image_and_reply_from_msg(msg: Any) -> tuple[str, str]:
        """从消息字典/对象中递归提取第一张图片（data URI / base64 / URL）以及引用消息 ID。"""
        image_ref = ""
        reply_id = ""

        def _norm_img(val: Any) -> str:
            if isinstance(val, dict):
                for k in ("binary_data_base64", "base64", "image_base64", "b64", "url", "file", "data"):
                    v = val.get(k)
                    if isinstance(v, str) and v.strip():
                        res = _norm_img(v)
                        if res:
                            return res
                return ""
            if not isinstance(val, str):
                return ""
            s = val.strip()
            if not s:
                return ""
            if s.startswith(("data:image/", "http://", "https://")):
                return s
            # 裸 base64 图片（常见 PNG/JPEG/WEBP 头或长度 > 128 且不含空白）
            if len(s) > 128 and not any(ch.isspace() for ch in s[:64]):
                mime = "image/jpeg" if s.startswith("/9j/") else ("image/webp" if s.startswith("UklGR") else "image/png")
                return f"data:{mime};base64,{s}"
            return ""

        def _walk_segs(segs: Any) -> None:
            nonlocal image_ref, reply_id
            if isinstance(segs, dict):
                st = str(segs.get("type") or "").lower()
                data = segs.get("data")
                if st in ("image", "img", "photo") and not image_ref:
                    image_ref = _norm_img(segs.get("binary_data_base64")) or _norm_img(data) or _norm_img(segs)
                elif st == "reply" and not reply_id:
                    if isinstance(data, dict):
                        reply_id = str(
                            data.get("target_message_id")
                            or data.get("id")
                            or data.get("message_id")
                            or ""
                        ).strip()
                    elif data is not None:
                        reply_id = str(data).strip()
                elif st == "seglist" and isinstance(data, list):
                    _walk_segs(data)
            elif isinstance(segs, list):
                for item in segs:
                    _walk_segs(item)
            elif hasattr(segs, "type") and hasattr(segs, "data"):
                _walk_segs({"type": getattr(segs, "type", ""), "data": getattr(segs, "data", None)})

        # 兼容 capability.message.get_by_id 返回的 {"success": True, "message": {...}} 包装层
        if isinstance(msg, dict) and isinstance(msg.get("message"), dict):
            msg = msg["message"]

        if isinstance(msg, dict):
            for key in ("message_segments", "raw_message", "segments", "seg_list"):
                if msg.get(key):
                    _walk_segs(msg.get(key))
            if not reply_id:
                reply_id = str(
                    msg.get("reply_to")
                    or msg.get("reply_id")
                    or msg.get("reply_message_id")
                    or msg.get("target_message_id")
                    or ""
                ).strip()
            if not image_ref:
                for key in ("image_base64", "image_url", "image"):
                    if msg.get(key):
                        image_ref = _norm_img(msg.get(key))
                        if image_ref:
                            break
        return image_ref, reply_id

    async def _find_recruit_image(self, kwargs: dict[str, Any], stream_id: str, user_id: str) -> str:
        """按顺序查找：1) 当前消息附带图；2) 引用/回复的消息图；3) 同用户 60 秒内刚发的上一张图。"""
        msg = kwargs.get("message")
        img_ref, reply_id = self._extract_image_and_reply_from_msg(msg)
        if img_ref:
            return img_ref

        msg_cap = getattr(self.ctx, "message", None) if self.ctx else None
        if not msg_cap:
            return ""

        # 1b) 当前消息有 message_id 但未附带二进制数据时，通过 get_by_id(include_binary_data=True) 拉取
        cur_mid = ""
        if isinstance(msg, dict):
            minfo = msg.get("message_info") if isinstance(msg.get("message_info"), dict) else {}
            cur_mid = str(msg.get("message_id") or minfo.get("message_id") or kwargs.get("message_id") or "").strip()
        if cur_mid and hasattr(msg_cap, "get_by_id"):
            try:
                full_msg = await msg_cap.get_by_id(cur_mid, stream_id=stream_id, include_binary_data=True)
                img_ref, rid2 = self._extract_image_and_reply_from_msg(full_msg)
                if img_ref:
                    return img_ref
                reply_id = reply_id or rid2
            except Exception:
                pass

        # 2) 引用/回复的消息里找图
        if reply_id and hasattr(msg_cap, "get_by_id"):
            try:
                rep_msg = await msg_cap.get_by_id(reply_id, stream_id=stream_id, include_binary_data=True)
                img_ref, _ = self._extract_image_and_reply_from_msg(rep_msg)
                if img_ref:
                    return img_ref
            except Exception:
                pass

        # 3) 同用户 60 秒内刚发的图片（兼容「先发图、紧接着发 /公招」的顺手操作）
        if hasattr(msg_cap, "get_recent") and hasattr(msg_cap, "get_by_id"):
            try:
                recent_raw = await msg_cap.get_recent(stream_id, limit=5)
                recent = recent_raw.get("messages") if isinstance(recent_raw, dict) else recent_raw
                now = time.time()
                if isinstance(recent, list):
                    for item in reversed(recent):
                        if not isinstance(item, dict):
                            continue
                        imid = str(item.get("message_id") or "").strip()
                        if not imid or imid == cur_mid:
                            continue
                        ts = float(item.get("time") or item.get("timestamp") or 0.0)
                        if ts and (now - ts) > 60.0:
                            continue
                        item_uid = str(item.get("user_id") or (item.get("user_info") or {}).get("user_id") or "").strip()
                        if user_id and item_uid and _norm_token(item_uid) != _norm_token(user_id):
                            continue
                        full_item = await msg_cap.get_by_id(imid, stream_id=stream_id, include_binary_data=True)
                        img_ref, _ = self._extract_image_and_reply_from_msg(full_item)
                        if img_ref:
                            return img_ref
            except Exception:
                pass
        return ""

    async def _recognize_recruit_tags_from_image(self, image_ref: str) -> tuple[list[str], str]:
        """定向调用宿主 vlm 模型极简识别公招截图中的 5 个词条（仅在用户显式发 /公招 时触发）。"""
        llm = getattr(self.ctx, "llm", None) if self.ctx else None
        if not llm or not hasattr(llm, "generate"):
            return [], "当前宿主未启用 VLM 识图能力，请直接输入词条，例如：/公招 资深 控场 减速"

        # 若为远程 HTTP 图片 URL，先拉成 data URI 避免 VLM 接口无法访问内网/临时图链
        img_url = image_ref
        if img_url.startswith(("http://", "https://")) and self._prts:
            try:
                resp = await self._prts._client().get(img_url)
                resp.raise_for_status()
                ct = (resp.headers.get("content-type") or "image/png").split(";", 1)[0].strip()
                if not ct.startswith("image/"):
                    ct = "image/png"
                img_url = f"data:{ct};base64," + base64.b64encode(resp.content).decode("ascii")
            except Exception:
                pass

        prompt = [
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": (
                            "这是一张《明日方舟》公开招募界面截图。"
                            "请只输出图中「职业需求」里的全部招募词条（通常为5个，如：控场 资深干员 减速 削弱 位移），"
                            "用空格隔开，不要输出任何其他解释或标点。"
                        ),
                    },
                    {"type": "image_url", "image_url": {"url": img_url}},
                ],
            }
        ]
        try:
            res = await llm.generate(prompt, task_name="vlm", temperature=0.0, max_tokens=64)
        except Exception as exc:
            return [], f"识别公招截图失败：{_exc_text(exc)}"

        if not isinstance(res, dict) or not res.get("success"):
            return [], "未能通过 VLM 识别该图片，请确认已配置视觉模型，或直接输入词条：/公招 资深 控场 减速"

        raw_out = str(res.get("response") or "").strip()
        tags = extract_recruit_tags_from_text(raw_out)
        if not tags:
            return [], "未在图片中识别到有效的明日方舟公招词条，请确认截图清晰包含「职业需求」词条。"
        return tags, ""

    # ============================================================
    # Tool 1 & 2：显式可见工具（visibility="visible"，首轮即可调）
    # ============================================================

    @Tool(
        name="ak_wiki_query",
        description="查《明日方舟》PRTS Wiki（支持外号）。查材料/模组时自动发卡片图。",
        parameters=[
            ToolParameterInfo(
                name="target",
                param_type=ToolParamType.STRING,
                description="干员名、外号或词条名（查活动可留空）",
                required=False,
            ),
            ToolParameterInfo(
                name="section",
                param_type=ToolParamType.STRING,
                description="板块：overview总览/skills技能/modules模组/materials材料/summary材料总账/enemy敌人BOSS/voice语音/events当期活动/lore档案/wiki词条",
                required=False,
                enum_values=["overview", "skills", "modules", "materials", "summary", "enemy", "voice", "events", "lore", "wiki"],
            ),
            ToolParameterInfo(
                name="detail",
                param_type=ToolParamType.STRING,
                description="可选聚焦词（如 精二/三技能/模组/戳一戳/随机）",
                required=False,
            ),
            ToolParameterInfo(
                name="send_card",
                param_type=ToolParamType.BOOLEAN,
                description="是否发高清卡片图（materials/modules 默认开启）",
                required=False,
            ),
        ],
        visibility="visible",
    )
    async def tool_wiki_query(
        self,
        target: str = "",
        section: str = "overview",
        detail: str = "",
        send_card: Optional[bool] = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        if not self._is_enabled() or not self._prts:
            return {"success": False, "content": "明日方舟wiki插件未启用。", "error": "明日方舟wiki插件未启用。"}
        stream_id, gid, uid, plat = self._extract_stream_and_ids(kwargs)
        allowed, plat = await self._scope_allowed_async(stream_id=stream_id, group_id=gid, user_id=uid, platform=plat, group_name=_message_group_name(kwargs))
        if not allowed:
            return {"success": False, "content": "当前会话未开启明日方舟wiki插件。", "error": "当前会话未开启明日方舟wiki插件。"}
        sec = str(section or "overview").strip().lower()
        if sec not in ("events", "event", "活动", "卡池", "寻访") and not str(target or "").strip():
            return {"success": False, "content": "请提供要查询的干员名或词条名。", "error": "请提供要查询的干员名或词条名。"}
        try:
            text = await self._prts.query_wiki(target=str(target).strip(), section=sec, detail=str(detail or ""))
        except Exception as exc:
            return {"success": False, "content": f"查询 PRTS Wiki 失败：{_exc_text(exc)}", "error": f"查询 PRTS Wiki 失败：{_exc_text(exc)}"}
        text = self._sanitize_for_platform(text, plat)
        # 控制回传给 LLM 的单次文本上限，避免超长词条消耗过多输入 Token
        if len(text) > 1200:
            text = text[:1185].rstrip() + "\n…（内容过长已精简）"

        # 决定是否附发 2x 高清战术卡片图（聊天查材料/模组/总账/敌人/活动卡池默认开启，不支持发图平台自动跳过）
        is_enemy_sec = sec in ("enemy", "boss", "敌人", "领袖")
        is_events_sec = sec in ("events", "event", "活动", "卡池", "寻访", "gacha", "pool")
        is_summary_sec = sec in ("summary", "总计", "汇总", "总账")
        want_card = (
            bool(send_card)
            if send_card is not None
            else (self.cfg.render.auto_card_in_chat and (sec in ("materials", "modules") or is_summary_sec or is_enemy_sec or is_events_sec))
        )
        card_sent = False
        if want_card and stream_id and not self._platform_no_image(plat):
            if is_events_sec:
                ev_focus = "gacha" if sec in ("卡池", "寻访", "gacha", "pool") else "events"
                dedup_key = (stream_id, "__events__", ev_focus)
                now = time.time()
                if (now - self._recent_cards.get(dedup_key, 0.0)) >= 15.0:
                    ok_ev, _, _ = await self._send_events_card_or_text(stream_id, focus=ev_focus, platform=plat)
                    if ok_ev:
                        self._recent_cards[dedup_key] = now
                        card_sent = True
            elif is_summary_sec:
                dedup_key = (stream_id, "__summary__", f"{str(target).strip()}|{str(detail or '').strip()}")
                now = time.time()
                if (now - self._recent_cards.get(dedup_key, 0.0)) >= 15.0:
                    ok_s, _, _ = await self._send_summary_card_or_text(
                        stream_id=stream_id,
                        target=str(target).strip(),
                        detail=str(detail or ""),
                        platform=plat,
                    )
                    if ok_s:
                        self._recent_cards[dedup_key] = now
                        card_sent = True
            elif is_enemy_sec:
                dedup_key = (stream_id, "__enemy__", f"{str(target).strip()}|{str(detail or '').strip()}")
                now = time.time()
                if (now - self._recent_cards.get(dedup_key, 0.0)) >= 15.0:
                    ok_e, _, _ = await self._send_enemy_card_or_text(
                        stream_id=stream_id,
                        target=str(target).strip(),
                        detail=str(detail or ""),
                        platform=plat,
                    )
                    if ok_e:
                        self._recent_cards[dedup_key] = now
                        card_sent = True
            else:
                card_sent, _ = await self._render_and_send_card(
                    stream_id=stream_id,
                    target=str(target).strip(),
                    section=sec,
                    detail=str(detail or ""),
                    platform=plat,
                )
        if card_sent:
            stop_now = bool(self.cfg.render.stop_planner_after_image)
            return {"success": True, "content": "已发送高清卡片图。", "stop_after_execution": stop_now}
        return {"success": True, "content": text}

    @Tool(
        name="ak_send_illustration",
        description="发送《明日方舟》干员精二/初始/皮肤高清立绘（支持外号）。",
        parameters=[
            ToolParameterInfo(
                name="operator",
                param_type=ToolParamType.STRING,
                description="干员名或外号",
                required=True,
            ),
            ToolParameterInfo(
                name="variant",
                param_type=ToolParamType.STRING,
                description="版本：精二(默认)/初始/皮肤1/皮肤名/列表",
                required=False,
            ),
        ],
        visibility="visible",
    )
    async def tool_send_illustration(
        self,
        operator: str = "",
        variant: str = "e2",
        **kwargs: Any,
    ) -> dict[str, Any]:
        if not self._is_enabled() or not self._prts or not self.ctx:
            return {"success": False, "content": "明日方舟wiki插件未启用。", "error": "明日方舟wiki插件未启用。"}
        stream_id, gid, uid, plat = self._extract_stream_and_ids(kwargs)
        allowed, plat = await self._scope_allowed_async(stream_id=stream_id, group_id=gid, user_id=uid, platform=plat, group_name=_message_group_name(kwargs))
        if not allowed:
            return {"success": False, "content": "当前会话未开启明日方舟wiki插件。", "error": "当前会话未开启明日方舟wiki插件。"}
        if not str(operator or "").strip():
            return {"success": False, "content": "请提供干员名称或外号。", "error": "请提供干员名称或外号。"}

        try:
            res = await self._prts.fetch_illustration(str(operator).strip(), str(variant or "e2"))
        except Exception as exc:
            return {"success": False, "content": f"获取干员立绘失败：{_exc_text(exc)}", "error": f"获取干员立绘失败：{_exc_text(exc)}"}

        if not res.ok:
            msg = res.error or "获取立绘失败。"
            return {"success": False, "content": msg, "error": msg}

        if res.label == "列表" or not res.image_bytes:
            avail = "、".join(res.available_variants or [])
            return {"success": True, "content": f"干员「{res.operator}」可选立绘：{avail}"}

        if not stream_id:
            msg = f"已获取「{res.operator}」{res.label}，但当前上下文缺少 stream_id 无法发图。"
            return {"success": False, "content": msg, "error": msg}

        if not self._platform_no_image(plat):
            img_b64 = base64.b64encode(res.image_bytes).decode("ascii")
            try:
                send_res = await self.ctx.send.image(img_b64, stream_id=stream_id)
                if _send_ok(send_res):
                    avail = "、".join(res.available_variants or [])
                    stop_now = bool(self.cfg.render.stop_planner_after_image)
                    return {
                        "success": True,
                        "content": (
                            f"已成功向聊天发送干员「{res.operator}」的{res.label}（图源：PRTS Wiki）。"
                            f"该干员全部可选立绘：{avail}。"
                        ),
                        "stop_after_execution": stop_now,
                    }
            except Exception as exc:
                if self.ctx:
                    self.ctx.logger.warning(f"[明日方舟wiki插件] 发送立绘异常（{type(exc).__name__}）: {exc!r}")

        # 平台不支持发图时的优雅降级（对禁止外链平台自动剥离裸 URL）
        fallback = f"当前平台未能直接发送「{res.operator}」{res.label}图片。"
        if res.source_url:
            fallback += f" 可查看 PRTS 原图：{res.source_url}"
        fallback = self._sanitize_for_platform(fallback, plat)
        return {"success": False, "content": fallback, "error": fallback}

    @Tool(
        name="ak_recruit_calc",
        description="计算《明日方舟》公开招募保底组合并发送半身像卡片图。看到公招截图或公招词条时调用。",
        parameters=[
            ToolParameterInfo(
                name="tags",
                param_type=ToolParamType.STRING,
                description="公招词条，逗号或空格分隔（如：控场,资深干员,减速,削弱,位移）",
                required=True,
            ),
        ],
        visibility="visible",
    )
    async def tool_recruit_calc(
        self,
        tags: str = "",
        **kwargs: Any,
    ) -> dict[str, Any]:
        if not self._is_enabled() or not self._prts or not self.ctx:
            return {"success": False, "content": "明日方舟wiki插件未启用。"}
        stream_id, gid, uid, plat = self._extract_stream_and_ids(kwargs)
        allowed, plat = await self._scope_allowed_async(
            stream_id=stream_id, group_id=gid, user_id=uid, platform=plat, group_name=_message_group_name(kwargs),
        )
        if not allowed:
            return {"success": False, "content": "当前会话未开启明日方舟wiki插件。"}

        raw_parts = [t for t in re.split(r"[\s,，/、+]+", str(tags or "").strip()) if t]
        if len(raw_parts) <= 1 and tags:
            extracted = extract_recruit_tags_from_text(str(tags))
            if extracted:
                raw_parts = extracted
        if not raw_parts:
            return {"success": False, "content": "请提供至少 1 个公招词条。"}

        if not stream_id:
            txt = await self._prts.calculate_recruit(raw_parts)
            return {"success": True, "content": self._sanitize_for_platform(txt, plat)}

        # 10 秒防重：同一会话同一组词条不连发两张图
        dedup_key = (stream_id, "__recruit__", "|".join(sorted(raw_parts)))
        now = time.time()
        if (now - self._recent_cards.get(dedup_key, 0.0)) < 10.0:
            return {"success": True, "content": "刚刚已发送过该公招组合图。", "stop_after_execution": True}

        ok, summary, _ = await self._send_recruit_card_or_text(stream_id, raw_parts, platform=plat)
        if ok:
            self._recent_cards[dedup_key] = now
            stop_now = bool(self.cfg.render.stop_planner_after_image)
            return {"success": True, "content": summary or "已发送公招卡片图。", "stop_after_execution": stop_now}
        return {"success": False, "content": summary or "公招组合发送失败。", "error": summary or "公招组合发送失败。"}

    # ============================================================
    # Commands：快捷直出指令（返回三元组 intercept=2，100% 阻止进 Planner）
    # ============================================================

    @Command(
        name="ak_help_cmd",
        description="发送明日方舟wiki插件总帮助卡片图：/方舟帮助",
        pattern=r"^/(?:方舟帮助|方舟指令|prts帮助|ark帮助)\s*$",
    )
    async def cmd_help(self, **kwargs: Any) -> tuple[bool, str, int]:
        if not self._is_enabled() or not self.cfg.commands.enable_commands or not self.ctx:
            return False, "", 0
        stream_id, gid, uid, plat = self._extract_stream_and_ids(kwargs)
        if not stream_id:
            return False, "", 0
        allowed, plat = await self._scope_allowed_async(stream_id=stream_id, group_id=gid, user_id=uid, platform=plat, group_name=_message_group_name(kwargs))
        if not allowed:
            return False, "", 0
        return await self._send_help_card_or_text(stream_id, platform=plat)

    @Command(
        name="ak_recruit_cmd",
        description="公开招募保底组合计算器：/公招 <词条1> <词条2> ...",
        pattern=_RE_RECRUIT_COMMAND.pattern,
    )
    async def cmd_recruit(self, **kwargs: Any) -> tuple[bool, str, int]:
        if not self._is_enabled() or not self.cfg.commands.enable_commands or not self._prts or not self.ctx:
            return False, "", 0
        stream_id, gid, uid, plat = self._extract_stream_and_ids(kwargs)
        if not stream_id:
            return False, "", 0
        allowed, plat = await self._scope_allowed_async(stream_id=stream_id, group_id=gid, user_id=uid, platform=plat, group_name=_message_group_name(kwargs))
        if not allowed:
            return False, "", 0

        raw_arg_str = _command_args(kwargs, _RE_RECRUIT_COMMAND)
        # 剥离宿主自动拼进文本里的 [图片：...] / [回复：...] 等占位符，避免把图片描述误当成手打词条
        clean_arg_str = _RE_MSG_PLACEHOLDER.sub(" ", raw_arg_str).strip()
        tags = [t for t in re.split(r"[\s,，/、+]+", clean_arg_str) if t]
        if not tags:
            img_ref = await self._find_recruit_image(kwargs, stream_id, uid)
            if img_ref:
                tags, err = await self._recognize_recruit_tags_from_image(img_ref)
                if err:
                    return await self._command_reply(stream_id, err, "公招截图识别失败", platform=plat)
            elif raw_arg_str:
                # 少数适配器未回传图片二进制时，兜底从占位描述中按官方词条白名单精准抽取
                tags = extract_recruit_tags_from_text(raw_arg_str)
            if not tags:
                tip = "用法：/公招 <词条1> <词条2> ...，或带图 / 回复公招截图发 /公招（例：/公招 资深 控场 减速）"
                return await self._command_reply(stream_id, tip, "已提示公招用法", platform=plat)
        return await self._send_recruit_card_or_text(stream_id, tags, platform=plat)

    async def _handle_alias_subcmd(self, args: list[str], *, gid: str, uid: str, platform: str, private: bool) -> str:
        """处理 /方舟 别名 [列表|添加|删除] 子命令。"""
        sub = args[0].lower() if args else "列表"
        rt_path = self._runtime_alias_path()
        rt_list = load_runtime_aliases(rt_path)

        if sub in ("列表", "list", "查看", "ls"):
            cfg_list = list(self.cfg.glossary.custom_aliases or [])
            lines = ["【自定义黑话/昵称列表】"]
            if cfg_list:
                lines.append("▸ 配置页：" + "；".join(cfg_list[:20]))
            if rt_list:
                lines.append("▸ 群内热添加：" + "；".join(rt_list[:30]))
            if not cfg_list and not rt_list:
                lines.append("暂无自定义黑话。")
            lines.append("用法：/方舟 别名 添加 <外号> <干员名> [定位]　或　/方舟 别名 删除 <外号>")
            return "\n".join(lines)

        if not await self._can_clear_cache(group_id=gid, user_id=uid, platform=platform, private=private):
            return "仅插件管理员可在群聊中增删自定义黑话（可在设置 commands.admin_users 中添加管理员 ID）。"

        if sub in ("添加", "add", "新增", "设置") and len(args) >= 3:
            alias = args[1].strip()
            target = args[2].strip()
            brief = " ".join(args[3:]).strip() if len(args) > 3 else ""
            entry = f"{alias}={target}|{brief}" if brief else f"{alias}={target}"
            if not parse_custom_alias_line(entry):
                return "格式无效，请使用：/方舟 别名 添加 <外号> <干员名> [简短定位]"
            # 去重同名外号
            new_list = [x for x in rt_list if (parse_custom_alias_line(x) or ("", "", ""))[0].lower() != alias.lower()]
            new_list.append(entry)
            save_runtime_aliases(rt_path, new_list)
            self._rebuild_matcher()
            return f"已添加自定义黑话：「{alias}」→「{target}」，已立即生效。"

        if sub in ("删除", "del", "rm", "remove") and len(args) >= 2:
            alias = args[1].strip().lower()
            new_list = [x for x in rt_list if (parse_custom_alias_line(x) or ("", "", ""))[0].lower() != alias]
            if len(new_list) == len(rt_list):
                return f"未在热添加列表中找到外号「{args[1]}」（若在设置页配置请前往插件设置删除）。"
            save_runtime_aliases(rt_path, new_list)
            self._rebuild_matcher()
            return f"已删除自定义黑话「{args[1]}」，已立即生效。"

        return "用法：/方舟 别名 列表 | /方舟 别名 添加 <外号> <干员名> [定位] | /方舟 别名 删除 <外号>"

    @Command(
        name="ak_wiki_cmd",
        description="快捷查询明日方舟 PRTS Wiki：/方舟 <干员/词条> [技能|模组|材料|总计|语音|档案] 或 /方舟 活动/公招/别名/帮助/缓存",
        pattern=_RE_WIKI_COMMAND.pattern,
    )
    async def cmd_wiki(self, **kwargs: Any) -> tuple[bool, str, int]:
        if not self._is_enabled() or not self.cfg.commands.enable_commands or not self._prts or not self.ctx:
            return False, "", 0
        stream_id, gid, uid, plat = self._extract_stream_and_ids(kwargs)
        if not stream_id:
            return False, "", 0
        allowed, plat = await self._scope_allowed_async(stream_id=stream_id, group_id=gid, user_id=uid, platform=plat, group_name=_message_group_name(kwargs))
        if not allowed:
            return False, "", 0

        arg_str = _command_args(kwargs, _RE_WIKI_COMMAND)
        low_arg = arg_str.lower()

        if not arg_str or low_arg in ("帮助", "help", "指令", "菜单"):
            return await self._send_help_card_or_text(stream_id, platform=plat)

        # 缓存查看与一键清理（清理缓存需管理员校验）
        if low_arg in ("清理缓存", "清缓存", "清除缓存", "clear", "clean", "clearcache"):
            if not await self._can_clear_cache(group_id=gid, user_id=uid, platform=plat, private=_is_private_command(kwargs)):
                msg = "仅插件管理员可在群聊中执行清理缓存（可在插件配置 commands.admin_users 中添加管理员 ID）。"
                return await self._command_reply(stream_id, msg, msg, platform=plat)
            import asyncio
            self._prts.clear_memory_cache()
            self._recent_cards.clear()
            self._card_png.clear()
            self._help_png.clear()
            if self._store:
                p_cnt, i_cnt, freed_mb = await asyncio.to_thread(self._store.clear_all)
                msg = f"缓存已清空：移除 {p_cnt} 条词条缓存、{i_cnt} 张图片缓存（释放 {freed_mb} MB）。"
            else:
                msg = "内存缓存已清空。"
            return await self._command_reply(stream_id, msg, msg, platform=plat)

        if low_arg in ("缓存", "缓存状态", "cache", "stats"):
            import asyncio
            if self._store:
                st = await asyncio.to_thread(self._store.get_stats)
                msg = (
                    f"明日方舟wiki缓存状态：词条 {st['pages']} 条 | 别名重定向 {st['redirects']} 条 | "
                    f"立绘与图标 {st['images']} 张（{st['image_mb']} MB / 上限 {self.cfg.cache.image_cache_max_mb} MB）| "
                    f"索引库 {st['db_kb']} KB\n（管理员发「/方舟 清理缓存」可清空）"
                )
            else:
                msg = "缓存仓库未初始化。"
            return await self._command_reply(stream_id, msg, msg, platform=plat)

        if low_arg in ("卡池", "寻访", "当期卡池", "gacha", "pool"):
            return await self._send_events_card_or_text(stream_id, focus="gacha", platform=plat)

        if low_arg in ("活动", "当期活动", "events", "event"):
            return await self._send_events_card_or_text(stream_id, focus="events", platform=plat)

        parts = arg_str.split()
        p0 = parts[0].lower()

        if p0 in ("别名", "黑话", "昵称", "alias"):
            msg = await self._handle_alias_subcmd(
                parts[1:], gid=gid, uid=uid, platform=plat, private=_is_private_command(kwargs),
            )
            return await self._command_reply(stream_id, msg, "已处理别名管理", platform=plat)

        if p0 in ("公招", "公开招募", "recruit"):
            raw_rest = " ".join(parts[1:])
            clean_rest = _RE_MSG_PLACEHOLDER.sub(" ", raw_rest).strip()
            tags = [t for t in re.split(r"[\s,，/、+]+", clean_rest) if t]
            if not tags:
                img_ref = await self._find_recruit_image(kwargs, stream_id, uid)
                if img_ref:
                    tags, err = await self._recognize_recruit_tags_from_image(img_ref)
                    if err:
                        return await self._command_reply(stream_id, err, "公招截图识别失败", platform=plat)
                elif raw_rest:
                    tags = extract_recruit_tags_from_text(raw_rest)
            return await self._send_recruit_card_or_text(stream_id, tags, platform=plat)

        if p0 in ("敌人", "boss", "领袖", "enemy") and len(parts) >= 2:
            return await self._send_enemy_card_or_text(stream_id, parts[1], detail=" ".join(parts[2:]), platform=plat)

        if p0 in ("总计", "汇总", "总账", "缺口", "summary") and len(parts) >= 2:
            return await self._send_summary_card_or_text(stream_id, parts[1], detail=" ".join(parts[2:]), platform=plat)

        if p0 in ("语音", "台词", "voice") and len(parts) >= 2:
            return await self._send_voice_card_or_text(stream_id, parts[1], keyword=" ".join(parts[2:]), platform=plat)

        # 只打了子命令、没带干员：直接给用法，别掉到词条检索里回一句「没找到『总计』」
        if p0 in ("总计", "汇总", "总账", "缺口", "summary"):
            msg = "用法：/方舟 总计 <干员> [精二|一二三技能|模组]，例：/方舟 总计 维什戴尔 精二 三技能"
            return await self._command_reply(stream_id, msg, "已提示材料总账用法", platform=plat)
        if p0 in ("语音", "台词", "voice"):
            msg = "用法：/方舟 语音 <干员> [戳一戳|信赖|交谈|随机]，例：/方舟 语音 叔叔 戳一戳"
            return await self._command_reply(stream_id, msg, "已提示语音用法", platform=plat)

        target = parts[0]
        section_map = {
            "总览": "overview", "基础": "overview", "面板": "overview",
            "技能": "skills", "专精": "skills",
            "模组": "modules",
            "材料": "materials", "升级": "materials", "精二": "materials",
            "总计": "summary", "汇总": "summary", "总账": "summary",
            "语音": "voice", "台词": "voice",
            "档案": "lore", "故事": "lore", "剧情": "lore",
            "wiki": "wiki", "词条": "wiki",
        }
        section = "overview"
        detail = ""
        if len(parts) >= 2:
            p2 = parts[1].lower()
            if p2 in section_map:
                section = section_map[p2]
                # 「精二」同时指定材料板块和筛选阶段。
                detail = " ".join(parts[1:] if p2 == "精二" else parts[2:])
            else:
                detail = " ".join(parts[1:])

        if section == "summary":
            return await self._send_summary_card_or_text(stream_id, target, detail=detail, platform=plat)
        if section == "voice":
            return await self._send_voice_card_or_text(stream_id, target, keyword=detail, platform=plat)
        if section == "lore":
            return await self._send_lore_card_or_text(stream_id, target, detail=detail, platform=plat)

        # 对干员总览/技能/模组/材料优先发 2x 高清黑曜石金箔风格卡片图；不可用或非干员自动无缝降级纯文本
        if section in ("overview", "skills", "modules", "materials") and not self._platform_no_image(plat):
            sent_card, op_name = await self._render_and_send_card(
                stream_id=stream_id,
                target=target,
                section=section,
                detail=detail,
                dedup_seconds=0.0,
                platform=plat,
            )
            if sent_card:
                return True, f"已发送 {op_name or target} 战术卡片图", 2

        try:
            reply = await self._prts.query_wiki(target=target, section=section, detail=detail)
        except Exception as exc:
            reply = f"查询 PRTS Wiki 失败：{_exc_text(exc)}"
        return await self._command_reply(stream_id, reply, "已发送 Wiki 查询结果", platform=plat)

    @Command(
        name="ak_illust_cmd",
        description="快捷发送明日方舟干员立绘：/立绘 <干员/外号> [精二|初始|皮肤1|皮肤名|列表]",
        pattern=_RE_ILLUSTRATION_COMMAND.pattern,
    )
    async def cmd_illustration(self, **kwargs: Any) -> tuple[bool, str, int]:
        if not self._is_enabled() or not self.cfg.commands.enable_commands or not self._prts or not self.ctx:
            return False, "", 0
        stream_id, gid, uid, plat = self._extract_stream_and_ids(kwargs)
        if not stream_id:
            return False, "", 0
        allowed, plat = await self._scope_allowed_async(stream_id=stream_id, group_id=gid, user_id=uid, platform=plat, group_name=_message_group_name(kwargs))
        if not allowed:
            return False, "", 0

        arg_str = _command_args(kwargs, _RE_ILLUSTRATION_COMMAND)
        if not arg_str:
            usage = "用法：/立绘 <干员名或外号> [精二|初始|皮肤1|皮肤名|列表]"
            return await self._command_reply(stream_id, usage, "已提示立绘指令用法", platform=plat)

        parts = arg_str.split(maxsplit=1)
        operator = parts[0]
        variant = parts[1].strip() if len(parts) > 1 else "e2"

        try:
            res = await self._prts.fetch_illustration(operator, variant)
        except Exception as exc:
            # 超时类异常常常没有消息文本，日志里带上类型与 repr 便于事后定位
            if self.ctx:
                self.ctx.logger.warning(
                    f"[明日方舟wiki插件] 立绘获取异常（干员={operator} 版本={variant}）: "
                    f"{type(exc).__name__} {exc!r}"
                )
            return await self._command_reply(stream_id, f"获取立绘失败：{_exc_text(exc)}", "获取立绘失败", platform=plat)

        if not res.ok:
            return await self._command_reply(stream_id, res.error or "获取立绘失败。", "获取立绘失败", platform=plat)

        if res.label == "列表" or not res.image_bytes:
            avail = "\n".join(f"• {x}" for x in (res.available_variants or []))
            tip = f"\n发「/立绘 {res.operator} 皮肤1」或「/立绘 {res.operator} 初始」即可查看"
            return await self._command_reply(
                stream_id, f"【{res.operator} · 可选立绘】\n{avail}{tip}", "已列出可选立绘", platform=plat,
            )

        if not self._platform_no_image(plat):
            img_b64 = base64.b64encode(res.image_bytes).decode("ascii")
            try:
                if _send_ok(await self.ctx.send.image(img_b64, stream_id=stream_id)):
                    return True, f"已发送 {res.operator} 立绘", 2
            except Exception as exc:
                self.ctx.logger.warning(f"[明日方舟wiki插件] 发送立绘异常（{type(exc).__name__}）: {exc!r}")

        msg = f"当前会话未能直发「{res.operator}」{res.label}图片。"
        if res.source_url:
            msg += f" 可查看 PRTS 图源：{res.source_url}"
        return await self._command_reply(stream_id, msg, f"已降级提示 {res.operator} 立绘", platform=plat)


def create_plugin() -> ArknightsWikiPlugin:
    return ArknightsWikiPlugin()
