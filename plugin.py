"""泰拉终端（MaiPRTS）：轻量级《明日方舟》PRTS Wiki 知识与立绘插件。

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

from .matcher import ArknightsContextMatcher
from .prts import PRTSClient

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
        description="是否启用泰拉终端（明日方舟百科）插件",
        json_schema_extra={"label": "启用插件"},
    )
    config_version: str = Field(default="0.1.0", description="配置版本")


class ScopeSection(PluginConfigBase):
    """按群聊 / 私聊黑白名单控制生效范围。"""

    __ui_label__ = "生效范围"
    __ui_icon__ = "shield"
    __ui_order__ = 1

    group_whitelist: list[str] = Field(
        default_factory=list,
        description="群聊白名单（留空为全群生效，支持纯群号或 platform:id 如 qq:123456）",
        json_schema_extra={"label": "群聊白名单"},
    )
    group_blacklist: list[str] = Field(
        default_factory=list,
        description="群聊黑名单",
        json_schema_extra={"label": "群聊黑名单"},
    )
    private_whitelist: list[str] = Field(
        default_factory=list,
        description="私聊白名单（留空为全私聊生效）",
        json_schema_extra={"label": "私聊白名单"},
    )
    private_blacklist: list[str] = Field(
        default_factory=list,
        description="私聊黑名单",
        json_schema_extra={"label": "私聊黑名单"},
    )


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
        le=12,
        description="单轮最多注入的方舟词条参考数",
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
        default="MaiBot-ArknightsWiki/0.1.0 (+https://github.com/Allure-Gui/maibot-arknights-wiki)",
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
        default=1280,
        ge=0,
        le=2560,
        description="立绘缩略图宽度（像素；1280 兼顾清晰度与发送速度，0 表示原图）",
        json_schema_extra={"label": "立绘缩图宽度（px）"},
    )


class CacheSection(PluginConfigBase):
    """本地 SQLite 文本与立绘 LRU 缓存配置。"""

    __ui_label__ = "本地缓存"
    __ui_icon__ = "database"
    __ui_order__ = 5

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
    """自定义追加群内方舟别称与外号。"""

    __ui_label__ = "自定义别称"
    __ui_icon__ = "tag"
    __ui_order__ = 6

    custom_aliases: list[str] = Field(
        default_factory=list,
        description="自定义追加别称，格式：外号=标准PRTS词条名|简短定位(可选)",
        json_schema_extra={"label": "自定义别称列表"},
    )


class ArknightsWikiConfig(PluginConfigBase):
    plugin: PluginSection = Field(default_factory=PluginSection)
    scope: ScopeSection = Field(default_factory=ScopeSection)
    context: ContextSection = Field(default_factory=ContextSection)
    commands: CommandSection = Field(default_factory=CommandSection)
    prts: PRTSSection = Field(default_factory=PRTSSection)
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
    """兼容不同宿主版本 / 适配器的发送返回值判定。"""
    if isinstance(res, bool):
        return res
    if isinstance(res, dict):
        if "success" in res:
            return bool(res["success"])
        if "sent" in res:
            return bool(res["sent"])
    return bool(res)


def _norm_token(raw: str) -> str:
    s = str(raw or "").strip().lower()
    for pfx in ("qq:", "qq_official:", "onebot:", "napcat:", "snowluma:", "telegram:", "tg:", "weixin:", "wechat:", "internal:"):
        if s.startswith(pfx):
            return s[len(pfx) :]
    return s


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
    """泰拉终端（MaiPRTS）主插件类。"""

    config_model = ArknightsWikiConfig

    def __init__(self) -> None:
        super().__init__()
        self._store = None
        self._matcher: Optional[ArknightsContextMatcher] = None
        self._prts: Optional[PRTSClient] = None
        self._recent_hint_sessions: dict[str, float] = {}

    @property
    def cfg(self) -> ArknightsWikiConfig:
        try:
            c = self.config
            if isinstance(c, ArknightsWikiConfig):
                return c
        except RuntimeError:
            pass
        return ArknightsWikiConfig()

    def _build_services(self) -> None:
        from .store import WikiCacheStore

        ctx_paths = getattr(self.ctx, "paths", None) if self.ctx else None
        data_dir = Path(ctx_paths.data_dir) if ctx_paths and getattr(ctx_paths, "data_dir", None) else _resolve_data_dir()
        glossary_path = Path(__file__).resolve().parent / "assets" / "glossary.json"
        cfg = self.cfg
        self._store = WikiCacheStore(data_dir=data_dir, image_max_mb=cfg.cache.image_cache_max_mb)
        self._matcher = ArknightsContextMatcher(
            glossary_path=glossary_path,
            custom_aliases=cfg.glossary.custom_aliases,
            hot_ttl_seconds=float(cfg.context.hot_session_ttl_seconds),
        )
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

    async def on_load(self) -> bool:
        self._build_services()
        if self.ctx:
            self.ctx.logger.info(
                f"[泰拉终端] 已就绪（PRTS={self.cfg.prts.api_url}，缓存目录={_resolve_data_dir()}）"
            )
        return True

    async def on_config_update(self, scope: str, config_data: dict[str, Any], version: str) -> None:
        if scope != "self":
            return
        if self._prts:
            await self._prts.close()
        self._build_services()
        if self.ctx:
            self.ctx.logger.info("[泰拉终端] 配置已热更新并重建客户端")

    async def on_unload(self) -> None:
        if self._prts:
            await self._prts.close()

    def _is_enabled(self) -> bool:
        return bool(self.cfg.plugin.enabled and self._matcher and self._prts)

    def _scope_allowed(self, *, group_id: str = "", user_id: str = "") -> bool:
        sc = self.cfg.scope
        if group_id:
            candidates = {group_id.lower(), _norm_token(group_id)}
            if ":" in group_id:
                candidates.add(group_id.split(":", 1)[0].lower())
            bl = {_norm_token(x) for x in sc.group_blacklist} | {x.strip().lower() for x in sc.group_blacklist}
            if candidates & bl:
                return False
            if sc.group_whitelist:
                wl = {_norm_token(x) for x in sc.group_whitelist} | {x.strip().lower() for x in sc.group_whitelist}
                return bool(candidates & wl)
            return True

        if user_id:
            candidates = {user_id.lower(), _norm_token(user_id)}
            bl = {_norm_token(x) for x in sc.private_blacklist} | {x.strip().lower() for x in sc.private_blacklist}
            if candidates & bl:
                return False
            if sc.private_whitelist:
                wl = {_norm_token(x) for x in sc.private_whitelist} | {x.strip().lower() for x in sc.private_whitelist}
                return bool(candidates & wl)
        return True

    @staticmethod
    def _extract_stream_and_ids(kwargs: dict[str, Any]) -> tuple[str, str, str]:
        stream_id = str(kwargs.get("stream_id") or kwargs.get("session_id") or "").strip()
        group_id = str(kwargs.get("group_id") or "").strip()
        user_id = str(kwargs.get("user_id") or "").strip()
        msg = kwargs.get("message")
        if isinstance(msg, dict):
            minfo = msg.get("message_info") if isinstance(msg.get("message_info"), dict) else {}
            ginfo = minfo.get("group_info") if isinstance(minfo.get("group_info"), dict) else {}
            uinfo = minfo.get("user_info") if isinstance(minfo.get("user_info"), dict) else {}
            stream_id = stream_id or str(msg.get("stream_id") or msg.get("session_id") or minfo.get("session_id") or "")
            group_id = group_id or str(msg.get("group_id") or ginfo.get("group_id") or "")
            user_id = user_id or str(msg.get("user_id") or uinfo.get("user_id") or "")
        return stream_id, group_id, user_id

    # ============================================================
    # Hook 1 & 2：微秒级方舟黑话语境感知（零网络、零破坏前缀缓存）
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
        session_id = str(kwargs.get("session_id") or "").strip()

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
        session_id = str(kwargs.get("session_id") or "").strip()
        ts = self._recent_hint_sessions.get(session_id)
        if not ts or (time.time() - ts) > 30.0:
            return {"action": "continue"}
        extra = str(kwargs.get("extra_prompt") or "")
        style_note = "聊到《明日方舟》内容时，把参考信息当作自然游戏常识聊出来，贴合平时说话语气，不要机械复读整段百科模板。"
        if style_note not in extra:
            kwargs["extra_prompt"] = f"{extra}\n{style_note}".strip()
            return {"action": "continue", "modified_kwargs": kwargs}
        return {"action": "continue"}

    # ============================================================
    # Tool 1 & 2：显式可见工具（visibility="visible"，首轮即可调）
    # ============================================================

    @Tool(
        name="ak_wiki_query",
        description=(
            "查询《明日方舟》PRTS Wiki 权威资料（支持玩家外号如 42/ew/水陈/叔叔/小羊/异德/奶羊 等）。"
            "可按 section 精准获取干员总览(overview)、技能专三数值(skills)、专属模组与效果(modules)、"
            "精二/专精/模组升级材料(materials)、干员档案故事(lore)或任意活动/剧情/敌人/术语词条(wiki)。"
        ),
        parameters=[
            ToolParameterInfo(
                name="target",
                param_type=ToolParamType.STRING,
                description="要查询的干员名、玩家外号或 PRTS 词条名（如：维什戴尔、ew、42、玛恩纳、集成战略）",
                required=True,
            ),
            ToolParameterInfo(
                name="section",
                param_type=ToolParamType.STRING,
                description="查询板块：overview(总览) / skills(技能) / modules(模组) / materials(升级材料) / lore(干员档案故事) / wiki(通用词条)",
                required=False,
                enum_values=["overview", "skills", "modules", "materials", "lore", "wiki"],
            ),
            ToolParameterInfo(
                name="detail",
                param_type=ToolParamType.STRING,
                description="可选聚焦关键词（例如查材料时填「精二」「三技能」「模组」；查档案时填「晋升记录」「模组故事」）",
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
        **kwargs: Any,
    ) -> dict[str, Any]:
        if not self._is_enabled() or not self._prts:
            return {"success": False, "content": "泰拉终端插件未启用。"}
        _, gid, uid = self._extract_stream_and_ids(kwargs)
        if not self._scope_allowed(group_id=gid, user_id=uid):
            return {"success": False, "content": "当前会话未开启泰拉终端功能。"}
        if not str(target or "").strip():
            return {"success": False, "content": "请提供要查询的干员名或词条名。"}
        try:
            text = await self._prts.query_wiki(target=str(target).strip(), section=str(section or "overview"), detail=str(detail or ""))
            return {"success": True, "content": text}
        except Exception as exc:
            return {"success": False, "content": f"查询 PRTS Wiki 失败：{exc}"}

    @Tool(
        name="ak_send_illustration",
        description=(
            "获取并直接向当前聊天发送《明日方舟》干员高清立绘或皮肤立绘（自动支持玩家外号如 ew/水陈/42/叔叔/小羊）。"
            "支持 variant=e2(精二默认)、e0(初始)、skin(最新皮肤)、skin1/skin2、皮肤中文名，或 variant=list 查看该干员全部可用立绘与皮肤名称。"
        ),
        parameters=[
            ToolParameterInfo(
                name="operator",
                param_type=ToolParamType.STRING,
                description="干员名称或玩家外号（如：维什戴尔、ew、42、玛恩纳、艾雅法拉）",
                required=True,
            ),
            ToolParameterInfo(
                name="variant",
                param_type=ToolParamType.STRING,
                description="立绘版本：e2(精二,默认) / e0(初始) / skin(最新皮肤) / skin1 / 具体皮肤名 / list(仅列出可选立绘不发图)",
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
            return {"success": False, "content": "泰拉终端插件未启用。"}
        stream_id, gid, uid = self._extract_stream_and_ids(kwargs)
        if not self._scope_allowed(group_id=gid, user_id=uid):
            return {"success": False, "content": "当前会话未开启泰拉终端功能。"}
        if not str(operator or "").strip():
            return {"success": False, "content": "请提供干员名称或外号。"}

        try:
            res = await self._prts.fetch_illustration(str(operator).strip(), str(variant or "e2"))
        except Exception as exc:
            return {"success": False, "content": f"获取干员立绘失败：{exc}"}

        if not res.ok:
            return {"success": False, "content": res.error or "获取立绘失败。"}

        if res.label == "列表" or not res.image_bytes:
            avail = "、".join(res.available_variants or [])
            return {"success": True, "content": f"干员「{res.operator}」可选立绘：{avail}"}

        if not stream_id:
            return {
                "success": False,
                "content": f"已获取「{res.operator}」{res.label}，但当前上下文缺少 stream_id 无法发图。",
            }

        img_b64 = base64.b64encode(res.image_bytes).decode("ascii")
        try:
            send_res = await self.ctx.send.image(img_b64, stream_id=stream_id)
            if _send_ok(send_res):
                avail = "、".join(res.available_variants or [])
                return {
                    "success": True,
                    "content": (
                        f"已成功向聊天发送干员「{res.operator}」的{res.label}（图源：PRTS Wiki）。"
                        f"该干员全部可选立绘：{avail}。请用自然语气简单回应群友即可，无需再发图片链接。"
                    ),
                }
        except Exception as exc:
            if self.ctx:
                self.ctx.logger.warning(f"[泰拉终端] 发送立绘异常: {exc}")

        # 平台不支持发图时的优雅降级
        fallback = f"当前平台未能直接发送「{res.operator}」{res.label}图片。"
        if res.source_url:
            fallback += f" 可查看 PRTS 原图：{res.source_url}"
        return {"success": False, "content": fallback}

    # ============================================================
    # Command 1 & 2：快捷直出指令（不经过 LLM，秒查秒发图）
    # ============================================================

    @Command(
        name="ak_wiki_cmd",
        description="快捷查询明日方舟 PRTS Wiki：/方舟 <干员/词条> [技能|模组|材料|档案]",
        pattern=r"^/(?:方舟|prts|ark)(?:\s+.*)?$",
    )
    async def cmd_wiki(self, **kwargs: Any) -> Any:
        if not self._is_enabled() or not self.cfg.commands.enable_commands or not self._prts or not self.ctx:
            return None
        stream_id, gid, uid = self._extract_stream_and_ids(kwargs)
        if not stream_id or not self._scope_allowed(group_id=gid, user_id=uid):
            return None

        raw_text = str(kwargs.get("matched_text") or kwargs.get("text") or "").strip()
        if not raw_text and isinstance(kwargs.get("message"), dict):
            raw_text = str(kwargs["message"].get("processed_plain_text") or kwargs["message"].get("raw_message") or "").strip()

        arg_str = re.sub(r"^/(?:方舟|prts|ark)\s*", "", raw_text, flags=re.I).strip()
        if not arg_str:
            help_msg = (
                "【泰拉终端 · 快捷指令】\n"
                "• /方舟 <干员/外号/词条> [总览|技能|模组|材料|档案]\n"
                "• /立绘 <干员/外号> [精二|初始|皮肤1|皮肤名|列表]\n"
                "（日常聊天中直接聊方舟黑话，麦麦也会自动看懂并按需查资料/发立绘）"
            )
            await self.ctx.send.text(help_msg, stream_id=stream_id)
            return True

        parts = arg_str.split()
        target = parts[0]
        section_map = {
            "总览": "overview", "基础": "overview", "面板": "overview",
            "技能": "skills", "专精": "skills",
            "模组": "modules",
            "材料": "materials", "升级": "materials", "精二": "materials",
            "档案": "lore", "故事": "lore", "剧情": "lore",
            "wiki": "wiki", "词条": "wiki",
        }
        section = "overview"
        detail = ""
        if len(parts) >= 2:
            p2 = parts[1].lower()
            if p2 in section_map:
                section = section_map[p2]
                detail = " ".join(parts[2:])
            else:
                detail = " ".join(parts[1:])

        try:
            reply = await self._prts.query_wiki(target=target, section=section, detail=detail)
        except Exception as exc:
            reply = f"查询 PRTS Wiki 失败：{exc}"
        await self.ctx.send.text(reply, stream_id=stream_id)
        return True

    @Command(
        name="ak_illust_cmd",
        description="快捷发送明日方舟干员立绘：/立绘 <干员/外号> [精二|初始|皮肤1|皮肤名|列表]",
        pattern=r"^/(?:立绘|方舟立绘|ak图)(?:\s+.*)?$",
    )
    async def cmd_illustration(self, **kwargs: Any) -> Any:
        if not self._is_enabled() or not self.cfg.commands.enable_commands or not self._prts or not self.ctx:
            return None
        stream_id, gid, uid = self._extract_stream_and_ids(kwargs)
        if not stream_id or not self._scope_allowed(group_id=gid, user_id=uid):
            return None

        raw_text = str(kwargs.get("matched_text") or kwargs.get("text") or "").strip()
        if not raw_text and isinstance(kwargs.get("message"), dict):
            raw_text = str(kwargs["message"].get("processed_plain_text") or kwargs["message"].get("raw_message") or "").strip()

        arg_str = re.sub(r"^/(?:立绘|方舟立绘|ak图)\s*", "", raw_text, flags=re.I).strip()
        if not arg_str:
            await self.ctx.send.text("用法：/立绘 <干员名或外号> [精二|初始|皮肤1|皮肤名|列表]", stream_id=stream_id)
            return True

        parts = arg_str.split(maxsplit=1)
        operator = parts[0]
        variant = parts[1].strip() if len(parts) > 1 else "e2"

        try:
            res = await self._prts.fetch_illustration(operator, variant)
        except Exception as exc:
            await self.ctx.send.text(f"获取立绘失败：{exc}", stream_id=stream_id)
            return True

        if not res.ok:
            await self.ctx.send.text(res.error or "获取立绘失败。", stream_id=stream_id)
            return True

        if res.label == "列表" or not res.image_bytes:
            avail = "\n".join(f"• {x}" for x in (res.available_variants or []))
            await self.ctx.send.text(f"【{res.operator} · 可选立绘】\n{avail}", stream_id=stream_id)
            return True

        img_b64 = base64.b64encode(res.image_bytes).decode("ascii")
        try:
            ok = _send_ok(await self.ctx.send.image(img_b64, stream_id=stream_id))
            if not ok:
                msg = f"当前平台暂不支持直发图片，可查看：{res.source_url}" if res.source_url else "图片发送失败。"
                await self.ctx.send.text(msg, stream_id=stream_id)
        except Exception as exc:
            await self.ctx.send.text(f"图片发送异常：{exc}", stream_id=stream_id)
        return True


def create_plugin() -> ArknightsWikiPlugin:
    return ArknightsWikiPlugin()
