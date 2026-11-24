"""PRTS MediaWiki API 合规客户端与 Wikitext 精准解析引擎。

合规护栏：
1. 规范 User-Agent（遵循 MediaWiki API 礼仪）；
2. 全局并发锁 (Semaphore=2) + 请求最小间隔节流；
3. 严格遵守 HTTP 429 / 503 与 Retry-After，触发熔断冷却绝不暴力重试；
4. 仅按需单条查询并配合 SQLite 文本缓存与立绘 LRU 缩图缓存，严禁整站抓取；
5. 返回内容附带 PRTS Wiki (CC BY-NC-SA 4.0) 来源标注。
"""

from __future__ import annotations

import asyncio
import base64
import json
import re
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, Optional
from urllib.parse import quote

import httpx

from .matcher import ArknightsContextMatcher
from .store import WikiCacheStore

_RE_HTML_COMMENT = re.compile(r"<!--.*?-->", re.S)
_RE_REF_TAG = re.compile(r"<ref\b[^>]*>.*?</ref>|<ref\b[^>]*/\s*>", re.S | re.I)
_RE_BR_TAG = re.compile(r"<br\s*/?>", re.I)
_RE_HTML_TAG = re.compile(
    r"</?(?:span|div|font|small|big|b|i|u|sup|sub|section|nowiki|includeonly|noinclude|center|p|hr)\b[^>]*>",
    re.I,
)
_RE_MAGIC_WORD = re.compile(r"__(?:TOC|NOTOC|FORCETOC|NOEDITSECTION|INDEX|NOINDEX)__", re.I)
_RE_FILE_LINK = re.compile(r"\[\[\s*(?:文件|File|Image|图像|Media)\s*:[^\]]*\]\]", re.I)
_RE_WIKILINK = re.compile(r"\[\[(?:[^|\]]*\|)*([^|\]]+)\]\]")
_RE_EXTLINK = re.compile(r"\[https?://\S+\s+([^\]]+)\]")
_RE_MATERIAL = re.compile(r"\{\{材料消耗\|([^|{}]+)\|([^|{}]+)(?:\|[^{}]*)?\}\}")
_RE_INLINE_TPL = re.compile(
    r"\{\{(?:color|colour|变动数值lite|变动数值|术语|黑幕|ruby|lj|注释|提示|b|u)\|(?:[^{}]*\|)?([^{}|]+)\}\}",
    re.I,
)
_RE_OTHER_TPL = re.compile(r"\{\{[^{}]*\}\}")
_RE_SECTION_HDR = re.compile(r"^==\s*([^=\n]+?)\s*==\s*$", re.M)
_RE_SKIN_NAME = re.compile(r"^时装(\d+)名称$")
# <references group=注/> 这类自闭合引用容器不在通用标签白名单里，需单独剥离
_RE_REFERENCES = re.compile(r"<references\b[^>]*?/\s*>|<references\b[^>]*>.*?</references>", re.S | re.I)
_RE_TABLE_CELL_SPLIT = re.compile(r"\|\||!!|\||!")
# 分类标签属于元数据，正文里不该出现「分类:集成战略关卡」
_RE_CATEGORY_LINE = re.compile(r"^[ \t]*分类[:：].*$", re.M)


RECRUIT_TAG_ALIAS: dict[str, str] = {
    "高资": "高级资深干员", "高级资深": "高级资深干员",
    "资深": "资深干员",
    "机械": "支援机械", "小车": "支援机械",
    "高台": "远程位", "远程": "远程位",
    "地面": "近战位", "近战": "近战位",
    "狙击": "狙击干员", "术师": "术师干员", "近卫": "近卫干员",
    "先锋": "先锋干员", "重装": "重装干员", "医疗": "医疗干员",
    "辅助": "辅助干员", "特种": "特种干员",
    "回费": "费用回复", "快活": "快速复活",
}

RECRUIT_CANONICAL_TAGS: tuple[str, ...] = (
    "高级资深干员", "资深干员", "支援机械", "费用回复", "快速复活",
    "先锋干员", "狙击干员", "医疗干员", "术师干员", "近卫干员", "重装干员", "辅助干员", "特种干员",
    "近战位", "远程位",
    "新手", "治疗", "支援", "输出", "群攻", "减速", "生存", "防护", "削弱", "位移", "控场", "爆发", "召唤", "元素",
)

_RECRUIT_SCAN_WORDS: tuple[tuple[str, str], ...] = tuple(
    sorted(
        [(t, t) for t in RECRUIT_CANONICAL_TAGS]
        + [(k, v) for k, v in RECRUIT_TAG_ALIAS.items() if len(k) >= 2],
        key=lambda kv: len(kv[0]),
        reverse=True,
    )
)

_RECRUIT_SCENE_HINTS: tuple[str, ...] = (
    "公开招募", "招募时限", "职业需求", "招募预算", "人脉资源", "公招", "高资", "高级资深干员", "资深干员",
)


def extract_recruit_tags_from_text(text: str) -> list[str]:
    """从自然语言或 MaiBot 图片识别描述中精确提取公招官方词条（按词长降序，防「高级资深干员」误拆出「资深干员」）。"""
    s = str(text or "")
    if not s:
        return []
    found: list[str] = []
    masked = s
    for raw_w, canon in _RECRUIT_SCAN_WORDS:
        if raw_w in masked:
            if canon not in found:
                found.append(canon)
            # 用占位符抹掉已匹配片段，避免子串重复命中（如 高级资深干员 -> 资深干员）
            masked = masked.replace(raw_w, " ")
            if len(found) >= 6:
                break
    return found


def detect_recruit_screenshot_tags(recent_texts: list[str]) -> list[str]:
    """检测最新用户消息/识图描述是否为公招截图或公招咨询，满足条件时返回提取到的词条列表。"""
    if not recent_texts:
        return []
    latest = str(recent_texts[-1] or "")
    if not any(h in latest for h in _RECRUIT_SCENE_HINTS):
        return []
    tags = extract_recruit_tags_from_text(latest)
    return tags if len(tags) >= 2 else []


@dataclass(slots=True)
class IllustrationResult:
    """立绘获取结果。"""

    ok: bool
    operator: str
    label: str = ""
    file_name: str = ""
    image_bytes: bytes = b""
    source_url: str = ""
    available_variants: list[str] | None = None
    error: str = ""


# PRTS 干员页的技能模板名不统一：1/2 技能常为 {{技能2，3 技能为 {{技能，需一并匹配（按页面顺序即技能序号）
SKILL_TPL_NAMES = {"技能", "技能2", "技能3"}


def extract_templates(wikitext: str, template_names: set[str]) -> list[tuple[str, str]]:
    """括号配平安全地提取指定名称的顶层模板，返回 [(模板名, 内部参数串)]。"""
    results: list[tuple[str, str]] = []
    n = len(wikitext)
    i = 0
    lower_targets = {name.lower() for name in template_names}

    while i < n - 1:
        pos = wikitext.find("{{", i)
        if pos == -1:
            break
        # 向前找模板名结束处（| 或 }} 或换行）
        j = pos + 2
        while j < n and wikitext[j] in " \t\r\n":
            j += 1
        name_start = j
        while j < n and wikitext[j] not in "|}\r\n{":
            j += 1
        tpl_name = wikitext[name_start:j].strip()
        if tpl_name.lower() not in lower_targets:
            i = pos + 2
            continue

        # 栈式配平找到匹配的结束 }}
        depth = 1
        k = j
        while k < n - 1 and depth > 0:
            two = wikitext[k : k + 2]
            if two == "{{":
                depth += 1
                k += 2
            elif two == "}}":
                depth -= 1
                k += 2
            else:
                k += 1
        if depth == 0:
            inner = wikitext[j : k - 2]
            results.append((tpl_name, inner))
            i = k
        else:
            i = pos + 2
    return results


def parse_template_params(inner: str) -> dict[str, str]:
    """在嵌套花括号与方括号安全边界下，解析模板的 |key=value 参数表。"""
    params: dict[str, str] = {}
    chunks: list[str] = []
    buf: list[str] = []
    brace_depth = 0
    bracket_depth = 0
    n = len(inner)
    i = 0

    while i < n:
        two = inner[i : i + 2]
        if two == "{{":
            brace_depth += 1
            buf.append(two)
            i += 2
            continue
        if two == "}}" and brace_depth > 0:
            brace_depth -= 1
            buf.append(two)
            i += 2
            continue
        if two == "[[":
            bracket_depth += 1
            buf.append(two)
            i += 2
            continue
        if two == "]]" and bracket_depth > 0:
            bracket_depth -= 1
            buf.append(two)
            i += 2
            continue
        ch = inner[i]
        if ch == "|" and brace_depth == 0 and bracket_depth == 0:
            chunks.append("".join(buf))
            buf.clear()
        else:
            buf.append(ch)
        i += 1
    if buf:
        chunks.append("".join(buf))

    pos_idx = 1
    for raw in chunks:
        s = _RE_HTML_COMMENT.sub("", raw).strip()
        if not s:
            continue
        if "=" in s:
            key, val = s.split("=", 1)
            k = key.strip()
            if k:
                params[k] = val.strip()
                continue
        params[str(pos_idx)] = s
        pos_idx += 1
    return params


_RE_TITLE_NOISE = re.compile(r"[\s()（）·・\-—–_/]")


def _norm_title(text: str) -> str:
    """归一化标题：去掉空格、括号、连接符等排版噪音，用于判断两个标题是否同形。"""
    return _RE_TITLE_NOISE.sub("", str(text or "").lower())


def _strip_tables(text: str) -> str:
    """把 Wikitext 表格语法压成单行可读文本，避免 `{|class="wikitable"` 之类残留成乱码。"""
    if "{|" not in text and not text.lstrip().startswith(("|", "!")):
        return text
    out: list[str] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            out.append(raw_line)
            continue
        if line.startswith("{|") or line.startswith("|}") or line.startswith("|-") or line.startswith("|+"):
            continue
        if line.startswith("分类:") or line.startswith("分类："):
            continue
        if line.startswith("|") or line.startswith("!"):
            cells = []
            for cell in _RE_TABLE_CELL_SPLIT.split(line[1:]):
                c = cell.strip()
                # 单元格可能带 "style=xxx|内容" 属性，取最后一段
                if "|" in c:
                    c = c.split("|")[-1].strip()
                if c:
                    cells.append(c)
            if cells:
                out.append(" ｜ ".join(cells))
            continue
        out.append(raw_line)
    return "\n".join(out)


def clean_wiki_text(text: str) -> str:
    """将常见 PRTS Wikitext 标记清洗为紧凑可读纯文本。"""
    if not text:
        return ""
    s = _RE_HTML_COMMENT.sub("", text)
    s = _RE_REF_TAG.sub("", s)
    s = _RE_REFERENCES.sub("", s)
    s = _RE_BR_TAG.sub("\n", s)
    s = _RE_MATERIAL.sub(lambda m: f"{m.group(1).strip()}×{m.group(2).strip()}", s)
    # 多轮展开常见内联格式模板
    for _ in range(3):
        ns = _RE_INLINE_TPL.sub(r"\1", s)
        if ns == s:
            break
        s = ns
    s = _RE_FILE_LINK.sub("", s)
    s = _RE_WIKILINK.sub(r"\1", s)
    s = _RE_EXTLINK.sub(r"\1", s)
    s = _RE_CATEGORY_LINE.sub("", s)
    s = _RE_HTML_TAG.sub("", s)
    s = _RE_MAGIC_WORD.sub("", s)
    # 清理残余简单模板
    for _ in range(2):
        ns = _RE_OTHER_TPL.sub("", s)
        if ns == s:
            break
        s = ns
    s = s.replace("'''", "").replace("''", "")
    # 各级标题压成「· 标题」，避免 ==xxx== / ===xxx=== 残留
    s = re.sub(r"^={2,}\s*(.+?)\s*={2,}\s*$", r"· \1", s, flags=re.M)
    s = _strip_tables(s)
    lines = [re.sub(r"[ \t]+", " ", ln).strip() for ln in s.splitlines()]
    out = "\n".join(ln for ln in lines if ln)
    return re.sub(r"\n{3,}", "\n\n", out).strip()


def parse_materials_list(raw: str) -> list[tuple[str, str]]:
    """从 {{材料消耗|名称|数量}} 序列中提取 [(材料名, 数量), ...]。"""
    return [(n.strip(), c.strip()) for n, c in _RE_MATERIAL.findall(raw or "") if n.strip()]


def format_materials_inline(raw: str) -> str:
    """把 {{材料消耗|名称|数量}} 序列格式化为“名称×数量、...”"""
    items = parse_materials_list(raw)
    if items:
        return "、".join(f"{name}×{cnt}" for name, cnt in items)
    return clean_wiki_text(raw)


def split_sections(wikitext: str) -> dict[str, str]:
    """按二级标题 ==标题== 切分 Wikitext 章节。"""
    matches = list(_RE_SECTION_HDR.finditer(wikitext))
    sections: dict[str, str] = {}
    if not matches:
        sections[""] = wikitext
        return sections
    sections[""] = wikitext[: matches[0].start()]
    for idx, m in enumerate(matches):
        name = m.group(1).strip()
        start = m.end()
        end = matches[idx + 1].start() if idx + 1 < len(matches) else len(wikitext)
        sections[name] = wikitext[start:end].strip()
    return sections


def extract_skin_map(wikitext: str) -> dict[str, str]:
    """从 CharinfoV2 模板提取皮肤编号到皮肤名称的映射，例如 {'skin1': '超新星'}。"""
    tpls = extract_templates(wikitext, {"CharinfoV2", "干员信息"})
    if not tpls:
        return {}
    params = parse_template_params(tpls[0][1])
    skins: dict[str, str] = {}
    for k, v in params.items():
        m = _RE_SKIN_NAME.match(k)
        if m and v.strip():
            skins[f"skin{m.group(1)}"] = clean_wiki_text(v)
    return skins


class PRTSClient:
    """合规限流、带本地 SQLite/LRU 缓存的 PRTS Wiki 异步客户端。"""

    def __init__(
        self,
        store: WikiCacheStore,
        matcher: ArknightsContextMatcher,
        *,
        api_url: str = "https://prts.wiki/api.php",
        user_agent: str = "MaiBot-ArknightsWiki/0.3.0 (+https://github.com/Allure-Gui/maibot-arknights-wiki)",
        timeout_seconds: float = 12.0,
        min_interval_ms: int = 500,
        page_ttl_hours: int = 168,
        thumb_width: int = 1600,
    ) -> None:
        self.store = store
        self.matcher = matcher
        self.api_url = api_url.strip() or "https://prts.wiki/api.php"
        self.user_agent = user_agent.strip() or "MaiBot-ArknightsWiki/0.3.0"
        self.timeout_seconds = max(3.0, float(timeout_seconds))
        self.min_interval = max(0.3, float(min_interval_ms) / 1000.0)
        self.page_ttl_seconds = max(3600.0, float(page_ttl_hours) * 3600.0)
        self.thumb_width = max(0, int(thumb_width))

        self._sem = asyncio.Semaphore(2)
        self._cdn_sem = asyncio.Semaphore(6)
        self._rate_lock = asyncio.Lock()
        self._last_req_ts = 0.0
        self._cooldown_until = 0.0
        self._http: Optional[httpx.AsyncClient] = None

        # L1 内存极速热缓存（零磁盘 IO、零重复解析）
        self._mem_titles: dict[str, str] = {}
        self._mem_structs: OrderedDict[str, tuple[float, dict[str, Any]]] = OrderedDict()
        self._mem_uris: OrderedDict[str, str] = OrderedDict()
        # 图标负缓存：PRTS 上不存在的文件名 -> 记录时间，避免每次出卡都重复查询
        self._mem_miss: dict[str, float] = {}

    def clear_memory_cache(self) -> None:
        """清空 L1 内存热缓存。"""
        self._mem_titles.clear()
        self._mem_structs.clear()
        self._mem_uris.clear()
        self._mem_miss.clear()

    def _client(self) -> httpx.AsyncClient:
        if self._http is None or self._http.is_closed:
            self._http = httpx.AsyncClient(
                timeout=self.timeout_seconds,
                follow_redirects=True,
                headers={"User-Agent": self.user_agent, "Accept-Encoding": "gzip, deflate"},
            )
        return self._http

    async def close(self) -> None:
        if self._http is not None and not self._http.is_closed:
            await self._http.aclose()
            self._http = None

    async def _throttled_get(self, url: str, params: dict[str, Any] | None = None) -> httpx.Response:
        """带全局限流与 429/503 熔断保护的礼貌 GET 请求。"""
        now = time.monotonic()
        if now < self._cooldown_until:
            wait_sec = int(self._cooldown_until - now) + 1
            raise RuntimeError(f"PRTS 接口正在限流冷却中（剩余约 {wait_sec} 秒），已跳过请求以保护社区服务器。")

        async with self._sem:
            async with self._rate_lock:
                # Recheck after waiting for a slot: another in-flight request may have opened cooldown.
                now = time.monotonic()
                if now < self._cooldown_until:
                    wait_sec = int(self._cooldown_until - now) + 1
                    raise RuntimeError(
                        f"PRTS 接口正在限流冷却中（剩余约 {wait_sec} 秒），已跳过请求以保护社区服务器。"
                    )
                elapsed = now - self._last_req_ts
                if elapsed < self.min_interval:
                    await asyncio.sleep(self.min_interval - elapsed)
                now = time.monotonic()
                if now < self._cooldown_until:
                    wait_sec = int(self._cooldown_until - now) + 1
                    raise RuntimeError(
                        f"PRTS 接口正在限流冷却中（剩余约 {wait_sec} 秒），已跳过请求以保护社区服务器。"
                    )
                self._last_req_ts = now

            resp = await self._client().get(url, params=params)
            if resp.status_code in (429, 503):
                retry_after = 60.0
                raw_ra = resp.headers.get("Retry-After", "").strip()
                if raw_ra.isdigit():
                    retry_after = min(300.0, max(15.0, float(raw_ra)))
                self._cooldown_until = max(self._cooldown_until, time.monotonic() + retry_after)
                raise RuntimeError(f"PRTS 返回 HTTP {resp.status_code}，已自动退避冷却 {int(retry_after)} 秒。")
            resp.raise_for_status()
            return resp

    async def _api(self, params: dict[str, Any]) -> dict[str, Any]:
        resp = await self._throttled_get(self.api_url, params={**params, "format": "json"})
        return resp.json()

    async def resolve_title(self, raw_query: str) -> Optional[str]:
        """将玩家外号/模糊词解析为规范 PRTS 页面标题。"""
        q = str(raw_query or "").strip()
        if not q:
            return None

        q_key = q.lower()
        if q_key in self._mem_titles:
            return self._mem_titles[q_key]

        # 1. 本地别称词典
        local = self.matcher.resolve_alias_local(q)
        if local:
            q = local[0]

        # 2. 本地 SQLite 重定向缓存
        cached_target = await asyncio.to_thread(self.store.get_redirect, q)
        if cached_target:
            self._mem_titles[q_key] = cached_target
            return cached_target

        # 3. PRTS 官方 Redirect 精确查询（支持水陈/小羊/42等全社区外号）
        data = await self._api({"action": "query", "titles": q, "redirects": 1})
        pages = data.get("query", {}).get("pages", {})
        for pid, pinfo in pages.items():
            if int(pid) > 0 and pinfo.get("title"):
                canonical = str(pinfo["title"]).strip()
                self._mem_titles[q_key] = canonical
                await asyncio.to_thread(self.store.put_redirect, raw_query, canonical)
                if q != raw_query:
                    await asyncio.to_thread(self.store.put_redirect, q, canonical)
                return canonical

        # 4. 模糊搜索兜底：只接受与查询词高度相关的标题。
        #    旧逻辑直接取搜索第一名，会把「龙门」解析成关卡页「UO-2 初涉龙门」、
        #    「乌萨斯」落到「乌萨斯社团壁纸」，而且还会写进 14 天的 redirect 缓存长期粘住。
        sr = await self._api({"action": "query", "list": "search", "srsearch": q, "srlimit": 8})
        q_norm = q.strip().lower()
        cands: list[tuple[int, int, str]] = []
        for h in sr.get("query", {}).get("search", []):
            t = str(h.get("title") or "").strip()
            if not t or "/" in t:  # 排除「/语音记录」「/干员密录」等子页
                continue
            tl = t.lower()
            if tl == q_norm:
                rank = 0
            elif len(tl) >= 2 and q_norm.startswith(tl):
                # 查询词里包含完整标题，如「陈sir」→「陈」
                rank = 1
            elif tl.startswith(q_norm) and len(tl) - len(q_norm) <= 2:
                # 标题仅在查询词尾部多一两个字，且去掉括号空格后同形（如「阿米娅近卫」→「阿米娅(近卫)」）
                if _norm_title(tl) == _norm_title(q_norm):
                    rank = 2
                else:
                    continue
            else:
                continue
            cands.append((rank, len(t), t))
        if not cands:
            return None
        cands.sort()
        best = cands[0][2]
        self._mem_titles[q_key] = best
        if cands[0][0] == 0:
            # 只有精确命中才落 14 天 redirect 缓存；模糊命中留给下次重新判定，避免脏映射长期粘住
            await asyncio.to_thread(self.store.put_redirect, raw_query, best)
            if q != raw_query:
                await asyncio.to_thread(self.store.put_redirect, q, best)
        return best

    async def fetch_wikitext(self, title: str, *, force_refresh: bool = False) -> Optional[tuple[str, bool]]:
        """获取并缓存 PRTS 页面的 Wikitext，返回 (wikitext, is_operator)。"""
        if not force_refresh:
            cached = await asyncio.to_thread(self.store.get_page, title, self.page_ttl_seconds)
            if cached is not None:
                return cached

        data = await self._api(
            {
                "action": "query",
                "titles": title,
                "redirects": 1,
                "prop": "revisions",
                "rvprop": "content",
                "rvslots": "main",
            }
        )
        pages = data.get("query", {}).get("pages", {})
        for pid, pinfo in pages.items():
            if int(pid) <= 0:
                continue
            revs = pinfo.get("revisions") or []
            if not revs:
                continue
            content = str(revs[0].get("slots", {}).get("main", {}).get("*") or "")
            if not content:
                continue
            canonical = str(pinfo.get("title") or title).strip()
            is_op = "{{CharinfoV2" in content or "{{干员信息" in content
            await asyncio.to_thread(self.store.put_page, canonical, content, is_op)
            if canonical != title:
                await asyncio.to_thread(self.store.put_redirect, title, canonical)
            return content, is_op
        return None

    async def query_wiki(self, target: str, section: str = "overview", detail: str = "") -> str:
        """统一对外查询入口：返回紧凑、LLM 友好的结构化摘要。"""
        sec = (section or "overview").strip().lower()
        if sec in ("events", "event", "活动", "卡池", "寻访"):
            return await self.query_current_events()
        if sec in ("enemy", "boss", "敌人", "领袖"):
            return await self.query_enemy(target, detail=detail)
        if sec in ("voice", "voices", "语音", "台词"):
            return await self.query_voice_lines(target, keyword=detail)
        if sec in ("summary", "total", "总计", "汇总", "总账"):
            return await self.query_material_summary(target, detail=detail)

        title = await self.resolve_title(target)
        if not title:
            # 名字本身是词典里的方舟阵营/概念词（罗德岛、卡兹戴尔等），只是 PRTS 没有独立页面，
            # 此时提示「请检查名称」会误导人，改为说明原因并给出替代查法。
            local = self.matcher.resolve_alias_local(target)
            if str(target).strip() in ("明日方舟", "方舟", "舟游", "arknights"):
                return (
                    "「明日方舟」是游戏本体，PRTS Wiki 按干员/关卡/道具组织内容，没有「游戏本体」这一词条。"
                    "想查什么直接给干员名就好，例如 /方舟 阿米娅。"
                )
            if local:
                return (
                    f"「{target}」是《明日方舟》的阵营/概念类词条（{local[1]}），"
                    "PRTS Wiki 上没有独立页面，查不到正文。可以改查具体干员，例如 /方舟 阿米娅。"
                )
            return f"未在 PRTS Wiki 找到与「{target}」匹配的词条，请检查名称或换个关键词。"

        fetched = await self.fetch_wikitext(title)
        if not fetched:
            return f"找到了词条「{title}」，但暂未获取到正文内容。"

        wikitext, is_operator = fetched
        page_url = f"https://prts.wiki/w/{quote(title)}"

        if not is_operator or sec == "wiki":
            body = self._format_general_wiki(title, wikitext, detail)
        elif sec in ("overview", "info", "总览", "简介", "基础"):
            body = self._format_operator_overview(title, wikitext)
        elif sec in ("skills", "skill", "技能"):
            body = self._format_operator_skills(title, wikitext)
        elif sec in ("modules", "module", "模组"):
            body = self._format_operator_modules(title, wikitext)
        elif sec in ("materials", "material", "材料", "升级材料", "专精材料"):
            body = self._format_operator_materials(title, wikitext, detail)
        elif sec in ("lore", "story", "档案", "故事", "履历"):
            body = self._format_operator_lore(title, wikitext, detail)
        else:
            body = self._format_operator_overview(title, wikitext)

        if not str(body or "").strip():
            body = (
                f"【PRTS 词条：{title}】该页面只有数据模板/表格，暂无可读正文。"
                "可以改用 /方舟 <干员名> 查干员资料，或直接打开 PRTS 原页查看。"
            )
        return f"{body}\n\n来源：PRTS Wiki ({page_url}) [CC BY-NC-SA 4.0]"

    # ---- 以下为各视图精准排版器（严格控制输出长度防撑爆上下文） ----

    def _format_operator_overview(self, title: str, wikitext: str) -> str:
        tpls = extract_templates(wikitext, {"CharinfoV2", "干员信息"})
        info = parse_template_params(tpls[0][1]) if tpls else {}

        rarity_raw = info.get("稀有度", "").strip()
        rarity = f"{int(rarity_raw) + 1}★" if rarity_raw.isdigit() else rarity_raw
        prof = info.get("职业", "")
        sub = info.get("分支", "")
        pos = info.get("位置", "")
        org = " / ".join(x for x in (info.get("所属国家", ""), info.get("所属组织", ""), info.get("所属团队", "")) if x)
        trait = clean_wiki_text(info.get("特性", ""))
        en_name = info.get("干员外文名", "")

        lines = [f"【{title}{f' ({en_name})' if en_name else ''}】{rarity} {prof}·{sub} | {pos}"]
        if org:
            lines.append(f"所属：{org}")
        if trait:
            lines.append(f"特性：{trait}")

        # 精二满级白值
        attr_tpls = extract_templates(wikitext, {"属性"})
        if attr_tpls:
            ap = parse_template_params(attr_tpls[0][1])
            hp = ap.get("精英2_满级_生命上限") or ap.get("精英1_满级_生命上限") or ap.get("精英0_满级_生命上限", "")
            atk = ap.get("精英2_满级_攻击") or ap.get("精英1_满级_攻击") or ap.get("精英0_满级_攻击", "")
            dfn = ap.get("精英2_满级_防御") or ap.get("精英1_满级_防御") or ap.get("精英0_满级_防御", "")
            res = ap.get("精英2_满级_法术抗性") or ap.get("精英1_满级_法术抗性") or ap.get("精英0_满级_法术抗性", "")
            cost = ap.get("部署费用", "")
            blk = ap.get("阻挡数", "")
            spd = ap.get("攻击速度", "")
            redep = ap.get("再部署", "")
            stat_parts = [f"血{hp}" if hp else "", f"攻{atk}" if atk else "", f"防{dfn}" if dfn else "", f"法抗{res}" if res else ""]
            stat_line = " / ".join(p for p in stat_parts if p)
            extra_line = " | ".join(p for p in [f"费用{cost}" if cost else "", f"阻挡{blk}" if blk else "", f"攻速{spd}" if spd else "", f"再部署{redep}" if redep else ""] if p)
            if stat_line:
                lines.append(f"满级白值：{stat_line}（{extra_line}）")

        # 天赋（取每个天赋的最高精英阶段版本，省 token 且准确）
        talent_tpls = extract_templates(wikitext, {"天赋列表3", "天赋列表2", "天赋列表"})
        if talent_tpls:
            lines.append("天赋：")
            for _, inner in talent_tpls:
                tp = parse_template_params(inner)
                best_name = ""
                best_eff = ""
                best_cond = ""
                for idx in range(1, 8):
                    eff = tp.get(f"天赋{idx}效果", "").strip()
                    cond = tp.get(f"天赋{idx}条件", "").strip()
                    name = tp.get(f"天赋{idx}", "").strip()
                    if eff and "潜能" not in cond:
                        best_name = name or best_name
                        best_eff = eff
                        best_cond = cond
                if best_eff:
                    lines.append(f"- {best_name}({best_cond})：{clean_wiki_text(best_eff)}")

        # 技能名速览
        skill_tpls = extract_templates(wikitext, SKILL_TPL_NAMES)
        if skill_tpls:
            s_names = [parse_template_params(inn).get("技能名", "").strip() for _, inn in skill_tpls]
            s_names = [f"{i + 1}.{n}" for i, n in enumerate(s_names) if n]
            if s_names:
                lines.append("技能：" + " / ".join(s_names) + "（查详细数值可用 section=skills）")

        # 皮肤清单
        skins = extract_skin_map(wikitext)
        if skins:
            lines.append("已有皮肤：" + "、".join(f"{k}:{v}" for k, v in skins.items()))

        return "\n".join(lines)

    def _format_operator_skills(self, title: str, wikitext: str) -> str:
        skill_tpls = extract_templates(wikitext, SKILL_TPL_NAMES)
        if not skill_tpls:
            return f"未在「{title}」页面解析到技能数据。"

        lines = [f"【{title} · 技能一览（展示满级/专三数据）】"]
        for idx, (_, inner) in enumerate(skill_tpls, 1):
            sp = parse_template_params(inner)
            sname = sp.get("技能名", f"技能{idx}")
            stype = " / ".join(x for x in (sp.get("技能类型1", ""), sp.get("技能类型2", "")) if x)
            # 优先取专三，依次降级到专二/专一/Lv.7/Lv.1
            lv_candidates = [
                ("专精3", "专三"),
                ("专精2", "专二"),
                ("专精1", "专一"),
                ("10", "专三"),
                ("9", "专二"),
                ("8", "专一"),
                ("7", "Lv.7"),
                ("1", "Lv.1"),
            ]
            top_key = ""
            lv_label = ""
            for key_pfx, lbl in lv_candidates:
                if sp.get(f"技能{key_pfx}描述"):
                    top_key = key_pfx
                    lv_label = lbl
                    break
            desc = clean_wiki_text(sp.get(f"技能{top_key}描述", "")) if top_key else ""
            init_sp = sp.get(f"技能{top_key}初始", "") if top_key else ""
            cost_sp = sp.get(f"技能{top_key}消耗", "") if top_key else ""
            dur = sp.get(f"技能{top_key}持续", "") if top_key else ""
            sp_meta = " | ".join(
                p for p in [
                    stype,
                    f"初始{init_sp}" if init_sp != "" else "",
                    f"消耗{cost_sp}" if cost_sp != "" else "",
                    f"持续{dur}" if dur else "",
                ]
                if p
            )
            lines.append(f"{idx}. {sname} [{lv_label}]（{sp_meta}）")
            if desc:
                lines.append(f"   {desc}")
        return "\n".join(lines)

    def _format_operator_modules(self, title: str, wikitext: str) -> str:
        mod_tpls = extract_templates(wikitext, {"模组"})
        exclusive_mods: list[dict[str, str]] = []
        for _, inner in mod_tpls:
            mp = parse_template_params(inner)
            if mp.get("基础证章", "").lower() in ("yes", "1", "true") or mp.get("类型图标") == "original":
                continue
            exclusive_mods.append(mp)

        if not exclusive_mods:
            return f"干员「{title}」目前暂未实装专属模组（或仅含基础证章）。"

        lines = [f"【{title} · 专属模组一览】"]
        stat_keys = ("生命", "攻击", "防御", "法术抗性", "攻击速度", "部署费用", "再部署", "阻挡数")
        for idx, mp in enumerate(exclusive_mods, 1):
            mname = clean_wiki_text(mp.get("名称", f"模组{idx}"))
            mtype = mp.get("类型", mp.get("类型图标", ""))
            lines.append(f"\n◆ 模组{idx}：{mname}{f' [{mtype}]' if mtype else ''}")

            # 3 级属性加成（优先取 3 级）
            lv3_stats = []
            for sk in stat_keys:
                val = mp.get(f"{sk}3") or mp.get(f"{sk}2") or mp.get(sk)
                if val:
                    lv3_stats.append(f"{sk}+{val}")
            if lv3_stats:
                lines.append("  满级属性：" + "、".join(lv3_stats))

            if mp.get("特性"):
                lines.append("  Lv1 特性：" + clean_wiki_text(mp["特性"]))
            if mp.get("天赋2"):
                lines.append("  Lv2 天赋：" + clean_wiki_text(mp["天赋2"]).replace("\n", " "))
            if mp.get("天赋3"):
                lines.append("  Lv3 天赋：" + clean_wiki_text(mp["天赋3"]).replace("\n", " "))

            # 解锁任务
            tasks = [clean_wiki_text(mp.get(f"任务{i}", "")) for i in (1, 2) if mp.get(f"任务{i}")]
            if tasks:
                lines.append("  解锁任务：" + "；".join(tasks))

            # 升级材料
            mat_lines = []
            for lv, key in ((1, "材料消耗"), (2, "材料消耗2"), (3, "材料消耗3")):
                if mp.get(key):
                    mat_lines.append(f"Lv{lv}({format_materials_inline(mp[key])})")
            if mat_lines:
                lines.append("  升级材料：" + " / ".join(mat_lines))
        return "\n".join(lines)

    def _format_operator_materials(self, title: str, wikitext: str, detail: str = "") -> str:
        lines = [f"【{title} · 养成升级材料清单】"]
        focus = (detail or "").strip().lower()
        focus_clean = focus.replace("精一", "").replace("精二", "").replace("精1", "").replace("精2", "")
        has_elite_kw = any(k in focus for k in ("精", "elite"))
        has_skill_kw = any(k in focus_clean for k in ("技", "专", "skill", "1", "2", "3", "一", "二", "三", "7", "七"))
        has_mod_kw = any(k in focus for k in ("模组", "mod"))
        show_all = not (has_elite_kw or has_skill_kw or has_mod_kw) or "全" in focus

        # 1. 精英化材料
        elite_tpls = extract_templates(wikitext, {"精英化材料"})
        if elite_tpls and (show_all or has_elite_kw):
            ep = parse_template_params(elite_tpls[0][1])
            # 明确说了「精一」或「精二」就只给对应阶段，别把两段都糊上去
            elite_stages = ("精1", "精2")
            only_e1 = ("精一" in focus or "精1" in focus) and not ("精二" in focus or "精2" in focus)
            only_e2 = ("精二" in focus or "精2" in focus) and not ("精一" in focus or "精1" in focus)
            if only_e1 and not only_e2:
                elite_stages = ("精1",)
            elif only_e2 and not only_e1:
                elite_stages = ("精2",)
            lines.append("▶ 精英化：")
            for k in elite_stages:
                if ep.get(k):
                    lines.append(f"  {k}：{format_materials_inline(ep[k])}")

        # 2. 技能升级与专精材料
        skill_mat_tpls = extract_templates(wikitext, {"技能升级材料"})
        if skill_mat_tpls and (show_all or has_skill_kw):
            smp = parse_template_params(skill_mat_tpls[0][1])
            skill_names = [parse_template_params(inn).get("技能名", "") for _, inn in extract_templates(wikitext, SKILL_TPL_NAMES)]

            if show_all or any(k in focus_clean for k in ("7", "七", "通用")):
                base_parts = [f"Lv{lv}:{format_materials_inline(smp[str(lv)])}" for lv in range(2, 8) if smp.get(str(lv))]
                if base_parts:
                    lines.append("▶ 技能通用 2→7 级：")
                    for bp in base_parts:
                        lines.append(f"  {bp}")

            cn_nums = (("一", 1), ("二", 2), ("三", 3))
            specific_idx = [num for cn, num in cn_nums if cn in focus_clean or str(num) in focus_clean]
            for cn, num in cn_nums:
                if specific_idx and num not in specific_idx:
                    continue
                sname = f"({skill_names[num - 1]})" if num - 1 < len(skill_names) and skill_names[num - 1] else ""
                m_parts = []
                for lv, lbl in ((8, "专一"), (9, "专二"), (10, "专三")):
                    raw = smp.get(f"{cn}{lv}")
                    if raw:
                        m_parts.append(f"  {lbl}：{format_materials_inline(raw)}")
                if m_parts:
                    lines.append(f"▶ {num}技能{sname} 专精：")
                    lines.extend(m_parts)

        # 3. 模组升级材料
        mod_tpls = extract_templates(wikitext, {"模组"})
        mod_lines = []
        for _, inn in mod_tpls:
            mp = parse_template_params(inn)
            if mp.get("基础证章", "").lower() in ("yes", "1", "true") or mp.get("类型图标") == "original":
                continue
            mname = clean_wiki_text(mp.get("名称", "专属模组"))
            mtype = mp.get("类型", "")
            parts = [f"Lv{lv}({format_materials_inline(mp[k])})" for lv, k in ((1, "材料消耗"), (2, "材料消耗2"), (3, "材料消耗3")) if mp.get(k)]
            if parts:
                mod_lines.append(f"  {mname}{f'[{mtype}]' if mtype else ''}：" + " | ".join(parts))
        if mod_lines and (show_all or has_mod_kw):
            lines.append("▶ 专属模组：")
            lines.extend(mod_lines)

        return "\n".join(lines)

    def _format_operator_lore(self, title: str, wikitext: str, detail: str = "") -> str:
        sections = split_sections(wikitext)
        lore_sec = sections.get("干员档案", "")
        if not lore_sec:
            return self._format_general_wiki(title, wikitext, detail)

        lines = [f"【{title} · 干员档案与背景】"]
        profile_tpls = extract_templates(lore_sec, {"人员档案set"})
        if profile_tpls:
            pp = parse_template_params(profile_tpls[0][1])
            brief_fields = [
                f"性别:{pp['性别']}" if pp.get("性别") else "",
                f"种族:{pp['种族']}" if pp.get("种族") else "",
                f"出身:{pp['出身地']}" if pp.get("出身地") else "",
                f"身高:{pp['身高']}" if pp.get("身高") else "",
                f"生日:{pp['生日']}" if pp.get("生日") else "",
                f"战斗经验:{pp['战斗经验']}" if pp.get("战斗经验") else "",
                f"感染:{pp['矿石病感染情况']}" if pp.get("矿石病感染情况") else "",
            ]
            lines.append("基本资料：" + " | ".join(x for x in brief_fields if x))

        # 解析 {{人员档案}} 模板中的各段履历/档案资料/晋升记录
        archive_entries: list[tuple[str, str]] = []
        for _, inn in extract_templates(lore_sec, {"人员档案", "干员档案"}):
            ap = parse_template_params(inn)
            for idx in range(1, 15):
                heading = clean_wiki_text(ap.get(f"档案{idx}", ""))
                body = clean_wiki_text(ap.get(f"档案{idx}文本", ""))
                if not body:
                    continue
                # 跳过与上方基本资料重复的基础档案/体检表（除非用户显式要求）
                if not detail.strip() and heading in ("基础档案", "综合体检测试"):
                    continue
                archive_entries.append((heading or f"档案{idx}", body))

        # 同时提取专属模组故事作为补充
        for _, inn in extract_templates(wikitext, {"模组"}):
            mp = parse_template_params(inn)
            if mp.get("基础证章", "").lower() in ("yes", "1", "true") or mp.get("类型图标") == "original":
                continue
            story = clean_wiki_text(mp.get("基础信息", ""))
            if story:
                mname = clean_wiki_text(mp.get("名称", ""))
                archive_entries.append((f"模组故事·{mname}", story))

        if detail.strip():
            kw = detail.strip().lower()
            matched = [(h, b) for h, b in archive_entries if kw in h.lower() or kw in b.lower()]
            chosen_entries = matched or archive_entries
        else:
            chosen_entries = archive_entries

        if chosen_entries:
            blocks = [f"◆ {h}：\n{b}" for h, b in chosen_entries]
            full_text = "\n\n".join(blocks)
        else:
            full_text = clean_wiki_text(re.sub(r"\{\{人员档案set.*?\}\}", "", lore_sec, count=1, flags=re.S))

        if len(full_text) > 1000:
            full_text = full_text[:1000].rstrip() + "……（档案较长，可通过 detail 参数指定「客观履历」「档案资料一」「晋升记录」「模组故事」查看对应全文）"
        if full_text:
            lines.append(full_text)
        return "\n".join(lines)

    def _format_general_wiki(self, title: str, wikitext: str, detail: str = "") -> str:
        """通用非干员词条（活动/剧情/敌人/术语/阵营/材料）精简摘要。"""
        sections = split_sections(wikitext)
        chosen = ""
        if detail.strip():
            for h, body in sections.items():
                if detail.strip().lower() in h.lower() or detail.strip().lower() in body.lower():
                    chosen = f"{f'[{h}] ' if h else ''}{clean_wiki_text(body)}"
                    break
        if not chosen:
            lead = clean_wiki_text(sections.get("", ""))
            rest_parts = []
            for h, body in sections.items():
                if not h or h in ("注释与链接", "参考资料", "外部链接"):
                    continue
                c = clean_wiki_text(body)
                if c:
                    rest_parts.append(f"◆ {h}：{c[:260]}")
                if len(rest_parts) >= 3:
                    break
            chosen = "\n".join(x for x in [lead[:400], *rest_parts] if x)
        if not chosen:
            chosen = clean_wiki_text(wikitext)
        if not str(chosen).strip():
            # 正文全是模板/表格，已被清洗掉：交给上层给出可操作的提示，而不是甩一个空标题
            return ""
        # 只剩小标题、没有任何正文（典型是纯数据表的关卡页），同样视为无内容
        body_lines = [ln.strip() for ln in str(chosen).splitlines() if ln.strip()]
        if body_lines and all(ln.startswith("· ") for ln in body_lines):
            return ""
        if len(chosen) > 900:
            chosen = chosen[:900].rstrip() + "……"
        return f"【PRTS 词条：{title}】\n{chosen}"

    # ---- 立绘与皮肤获取（1280px 缩图 + 本地 LRU 缓存） ----

    async def list_illustrations(self, operator_name: str) -> list[dict[str, str]]:
        """获取指定干员在 PRTS 上的全部立绘/皮肤列表（带 7 天缓存）。"""
        cached_json = await asyncio.to_thread(self.store.get_image_list, operator_name, self.page_ttl_seconds)
        if cached_json:
            try:
                return json.loads(cached_json)
            except json.JSONDecodeError:
                pass

        # 查皮肤名映射（顺便复用页面缓存）
        skin_map: dict[str, str] = {}
        page = await self.fetch_wikitext(operator_name)
        if page:
            skin_map = extract_skin_map(page[0])

        res = await self._api(
            {
                "action": "query",
                "list": "allimages",
                "aiprefix": f"立绘_{operator_name}_",
                "ailimit": 30,
            }
        )
        items: list[dict[str, str]] = []
        prefix = f"立绘_{operator_name}_"
        for im in res.get("query", {}).get("allimages", []):
            fname = str(im.get("name") or "")
            if not fname.startswith(prefix) or not fname.lower().endswith((".png", ".jpg", ".jpeg", ".webp")):
                continue
            stem = fname[len(prefix) :].rsplit(".", 1)[0]
            if stem == "1":
                tag, label = "e0", "初始立绘"
            elif stem == "2":
                tag, label = "e2", "精二立绘"
            elif stem.startswith("skin"):
                tag = stem
                num = stem[4:]
                sname = skin_map.get(stem, "")
                label = f"皮肤{num}「{sname}」" if sname else f"皮肤{num}"
            else:
                # 「立绘_X_1+.png」「立绘_X_2b.png」这类是剧情/差分立绘，别混进基础立绘列表
                tag, label = f"alt{stem}", f"差分立绘（{stem}）"
            items.append({"file_name": fname, "tag": tag, "label": label, "skin_name": skin_map.get(stem, "")})

        if items:
            await asyncio.to_thread(self.store.put_image_list, operator_name, json.dumps(items, ensure_ascii=False))
        return items

    @staticmethod
    def _format_variant_cn(it: dict[str, str]) -> str:
        """将立绘条目格式化为纯中文展示文本（兼容旧缓存数据）。"""
        tag = str(it.get("tag") or "").strip().lower()
        sname = str(it.get("skin_name") or "").strip()
        if tag == "e0":
            return "初始（初始立绘）"
        if tag == "e2":
            return "精二（精二立绘）"
        if tag.startswith("skin"):
            num = tag[4:]
            return f"皮肤{num}：「{sname}」" if sname else f"皮肤{num}"
        if tag.startswith("alt"):
            return str(it.get("label") or f"差分立绘（{tag[3:]}）")
        return str(it.get("label") or tag)

    async def fetch_illustration(self, operator_query: str, variant: str = "e2") -> IllustrationResult:
        """按干员名/别称与变体（精二/初始/皮肤1/皮肤名/列表）获取立绘缩图字节。"""
        op_name = await self.resolve_title(operator_query)
        if not op_name:
            return IllustrationResult(ok=False, operator=operator_query, error=f"未找到干员「{operator_query}」。")

        items = await self.list_illustrations(op_name)
        if not items:
            return IllustrationResult(ok=False, operator=op_name, error=f"未在 PRTS 找到「{op_name}」的立绘文件。")

        avail = [self._format_variant_cn(it) for it in items]
        v = (variant or "e2").strip().lower()
        if v in ("list", "列表", "全部", "皮肤列表"):
            return IllustrationResult(ok=True, operator=op_name, available_variants=avail, label="列表")

        chosen: Optional[dict[str, str]] = None
        # 兜底时只从「初始/精二」里挑，避免把剧情差分立绘当成主立绘发出去
        base = [it for it in items if it["tag"] in ("e0", "e2")] or items
        if v in ("e2", "精二", "精2", "2", "默认", ""):
            chosen = next((it for it in items if it["tag"] == "e2"), None) or next(
                (it for it in items if it["tag"] == "e0"), base[0]
            )
        elif v in ("e0", "e1", "初始", "原皮", "精0", "精1", "1"):
            chosen = next((it for it in items if it["tag"] == "e0"), base[0])
        elif v in ("skin", "皮肤", "时装", "最新皮肤"):
            skins = [it for it in items if it["tag"].startswith("skin")]
            chosen = skins[-1] if skins else base[-1]
        else:
            # 精确或模糊匹配 tag（skin1/皮肤1）或皮肤中文名（如“超新星”）
            norm_v = v.replace("皮肤", "skin").replace("时装", "skin")
            for it in items:
                if it["tag"].lower() == norm_v or (it["skin_name"] and v in it["skin_name"].lower()):
                    chosen = it
                    break
            if chosen is None:
                return IllustrationResult(
                    ok=False,
                    operator=op_name,
                    available_variants=avail,
                    error=f"未找到「{op_name}」对应的立绘变体「{variant}」。可选：{'、'.join(avail)}",
                )

        fname = chosen["file_name"]
        cache_key = f"{fname}@{self.thumb_width}"
        cached = await asyncio.to_thread(self.store.get_image, cache_key)
        if cached and cached[0]:
            cached_bytes, source_url = cached
            return IllustrationResult(
                ok=True,
                operator=op_name,
                label=chosen["label"],
                file_name=fname,
                image_bytes=cached_bytes,
                source_url=source_url,
                available_variants=avail,
            )

        # 请求 MediaWiki imageinfo 获取高质量缩略图或原图 URL
        q_params: dict[str, Any] = {
            "action": "query",
            "titles": f"File:{fname}",
            "prop": "imageinfo",
            "iiprop": "url|size|mime",
        }
        if self.thumb_width > 0:
            q_params["iiurlwidth"] = self.thumb_width
        info_res = await self._api(q_params)
        img_url = ""
        for pinfo in info_res.get("query", {}).get("pages", {}).values():
            ii = (pinfo.get("imageinfo") or [{}])[0]
            orig_w = int(ii.get("width") or 0)
            orig_size = int(ii.get("size") or 0)
            # 若原图尺寸本就不大（宽度 <= 目标宽，或体积 <= 3.5MB 且用户设高画质），优先取无损原图避免二次重采样发糊
            if self.thumb_width == 0 or (orig_w and orig_w <= self.thumb_width) or (self.thumb_width >= 1600 and 0 < orig_size <= 3_500_000):
                img_url = str(ii.get("url") or ii.get("thumburl") or "")
            else:
                img_url = str(ii.get("thumburl") or ii.get("url") or "")
            if img_url:
                break
        if not img_url:
            return IllustrationResult(ok=False, operator=op_name, error=f"未能从 PRTS 获取 {fname} 的图片链接。")

        resp = await self._throttled_get(img_url)
        img_bytes = resp.content
        if not img_bytes:
            return IllustrationResult(ok=False, operator=op_name, error="下载立绘为空。")

        ext = ".png"
        ct = resp.headers.get("content-type", "").lower()
        if "jpeg" in ct or "jpg" in ct:
            ext = ".jpg"
        elif "webp" in ct:
            ext = ".webp"
        await asyncio.to_thread(self.store.put_image_bytes, cache_key, img_bytes, img_url, ext)

        return IllustrationResult(
            ok=True,
            operator=op_name,
            label=chosen["label"],
            file_name=fname,
            image_bytes=img_bytes,
            source_url=img_url,
            available_variants=avail,
        )

    # ---- 结构化干员数据提取 & 批量图标 Data URI 获取（供高质量卡片渲染） ----

    async def get_operator_struct(self, target: str) -> Optional[dict[str, Any]]:
        """解析干员完整结构化数据（带 L1 内存热缓存），非干员或未找到时返回 None。"""
        title = await self.resolve_title(target)
        if not title:
            return None
        now = time.time()
        mem_hit = self._mem_structs.get(title)
        if mem_hit and (now - mem_hit[0]) < self.page_ttl_seconds:
            self._mem_structs.move_to_end(title)
            return mem_hit[1]
        fetched = await self.fetch_wikitext(title)
        if not fetched:
            return None
        wikitext, is_operator = fetched
        if not is_operator:
            return None
        parsed = self._parse_operator_struct(title, wikitext)
        self._mem_structs[title] = (now, parsed)
        if len(self._mem_structs) > 64:
            self._mem_structs.popitem(last=False)
        return parsed

    def _parse_operator_struct(self, title: str, wikitext: str) -> dict[str, Any]:
        tpls = extract_templates(wikitext, {"CharinfoV2", "干员信息"})
        info = parse_template_params(tpls[0][1]) if tpls else {}

        rarity_raw = info.get("稀有度", "").strip()
        stars = (int(rarity_raw) + 1) if rarity_raw.isdigit() else 6
        org = " / ".join(
            x for x in (info.get("所属国家", ""), info.get("所属组织", ""), info.get("所属团队", "")) if x
        )

        # 白值
        stats: dict[str, str] = {}
        attr_tpls = extract_templates(wikitext, {"属性"})
        if attr_tpls:
            ap = parse_template_params(attr_tpls[0][1])
            stats = {
                "hp": ap.get("精英2_满级_生命上限") or ap.get("精英1_满级_生命上限") or ap.get("精英0_满级_生命上限", "-"),
                "atk": ap.get("精英2_满级_攻击") or ap.get("精英1_满级_攻击") or ap.get("精英0_满级_攻击", "-"),
                "def": ap.get("精英2_满级_防御") or ap.get("精英1_满级_防御") or ap.get("精英0_满级_防御", "-"),
                "res": ap.get("精英2_满级_法术抗性") or ap.get("精英1_满级_法术抗性") or ap.get("精英0_满级_法术抗性", "0"),
                "cost": ap.get("部署费用", "-"),
                "block": ap.get("阻挡数", "-"),
                "speed": ap.get("攻击速度", "-"),
                "redeploy": ap.get("再部署", "-"),
            }

        # 天赋
        talents: list[dict[str, str]] = []
        for _, inner in extract_templates(wikitext, {"天赋列表3", "天赋列表2", "天赋列表"}):
            tp = parse_template_params(inner)
            best_name, best_eff, best_cond = "", "", ""
            for idx in range(1, 8):
                eff = tp.get(f"天赋{idx}效果", "").strip()
                cond = tp.get(f"天赋{idx}条件", "").strip()
                name = tp.get(f"天赋{idx}", "").strip()
                if eff and "潜能" not in cond:
                    best_name = name or best_name
                    best_eff = eff
                    best_cond = cond
            if best_eff:
                talents.append({"name": best_name, "cond": best_cond, "effect": clean_wiki_text(best_eff)})

        # 技能
        skills: list[dict[str, str]] = []
        lv_candidates = [
            ("专精3", "专三"), ("专精2", "专二"), ("专精1", "专一"),
            ("10", "专三"), ("9", "专二"), ("8", "专一"), ("7", "Lv.7"), ("1", "Lv.1"),
        ]
        for idx, (_, inner) in enumerate(extract_templates(wikitext, SKILL_TPL_NAMES), 1):
            sp = parse_template_params(inner)
            top_key, lv_label = "", ""
            for key_pfx, lbl in lv_candidates:
                if sp.get(f"技能{key_pfx}描述"):
                    top_key, lv_label = key_pfx, lbl
                    break
            skills.append(
                {
                    "index": str(idx),
                    "name": sp.get("技能名", f"技能{idx}"),
                    "recovery": sp.get("技能类型1", ""),
                    "trigger": sp.get("技能类型2", ""),
                    "level": lv_label,
                    "init_sp": sp.get(f"技能{top_key}初始", "") if top_key else "",
                    "cost_sp": sp.get(f"技能{top_key}消耗", "") if top_key else "",
                    "duration": sp.get(f"技能{top_key}持续", "") if top_key else "",
                    "desc": clean_wiki_text(sp.get(f"技能{top_key}描述", "")) if top_key else "",
                }
            )

        # 精英化材料
        elite_mats: list[dict[str, Any]] = []
        elite_tpls = extract_templates(wikitext, {"精英化材料"})
        if elite_tpls:
            ep = parse_template_params(elite_tpls[0][1])
            for k in ("精1", "精2"):
                items = parse_materials_list(ep.get(k, ""))
                if items:
                    elite_mats.append({"stage": k, "items": items})

        # 技能专精材料（以及 2-7 通用材料）
        skill_base_mats: list[dict[str, Any]] = []
        skill_mastery_mats: list[dict[str, Any]] = []
        skill_mat_tpls = extract_templates(wikitext, {"技能升级材料"})
        if skill_mat_tpls:
            smp = parse_template_params(skill_mat_tpls[0][1])
            for lv in range(2, 8):
                items = parse_materials_list(smp.get(str(lv), ""))
                if items:
                    skill_base_mats.append({"stage": f"Lv.{lv}", "items": items})
            for cn, num in (("一", 1), ("二", 2), ("三", 3)):
                sname = skills[num - 1]["name"] if num - 1 < len(skills) else f"技能{num}"
                stages = []
                for lv, lbl in ((8, "专一"), (9, "专二"), (10, "专三")):
                    items = parse_materials_list(smp.get(f"{cn}{lv}", ""))
                    if items:
                        stages.append({"stage": lbl, "items": items})
                if stages:
                    skill_mastery_mats.append({"index": num, "name": sname, "stages": stages})

        # 专属模组
        modules: list[dict[str, Any]] = []
        stat_keys = ("生命", "攻击", "防御", "法术抗性", "攻击速度", "部署费用", "再部署", "阻挡数")
        for _, inner in extract_templates(wikitext, {"模组"}):
            mp = parse_template_params(inner)
            if mp.get("基础证章", "").lower() in ("yes", "1", "true") or mp.get("类型图标") == "original":
                continue
            lv3_stats = []
            for sk in stat_keys:
                val = mp.get(f"{sk}3") or mp.get(f"{sk}2") or mp.get(sk)
                if val:
                    lv3_stats.append(f"{sk}+{val}")
            stages = []
            for lv, key in ((1, "材料消耗"), (2, "材料消耗2"), (3, "材料消耗3")):
                items = parse_materials_list(mp.get(key, ""))
                if items:
                    stages.append({"stage": f"Lv.{lv}", "items": items})
            tasks = [clean_wiki_text(mp.get(f"任务{i}", "")) for i in (1, 2) if mp.get(f"任务{i}")]
            modules.append(
                {
                    "name": clean_wiki_text(mp.get("名称", "专属模组")),
                    "type": mp.get("类型", mp.get("类型图标", "")),
                    "stats": lv3_stats,
                    "trait": clean_wiki_text(mp.get("特性", "")),
                    "talent2": clean_wiki_text(mp.get("天赋2", "")).replace("\n", " "),
                    "talent3": clean_wiki_text(mp.get("天赋3", "")).replace("\n", " "),
                    "tasks": tasks,
                    "stages": stages,
                }
            )

        return {
            "name": title,
            "en_name": info.get("干员外文名", ""),
            "stars": stars,
            "profession": info.get("职业", ""),
            "sub_profession": info.get("分支", ""),
            "position": info.get("位置", ""),
            "org": org,
            "trait": clean_wiki_text(info.get("特性", "")),
            "stats": stats,
            "talents": talents,
            "skills": skills,
            "elite_mats": elite_mats,
            "skill_base_mats": skill_base_mats,
            "skill_mastery_mats": skill_mastery_mats,
            "modules": modules,
            "skins": extract_skin_map(wikitext),
        }

    async def fetch_file_data_uris(self, file_names: list[str], *, thumb_width: int = 160) -> tuple[dict[str, str], bool]:
        """批量极速获取 PRTS 小图标/头像/半身像并转为离线 data:image/png;base64 URI（L1内存 + L2磁盘双级缓存）。"""
        unique_names = list(dict.fromkeys(f.strip() for f in file_names if f and f.strip()))
        if not unique_names:
            return {}, True

        result: dict[str, str] = {}
        missing: list[str] = []
        pending: dict[str, str] = {}
        now_m = time.monotonic()
        for fn in unique_names:
            cache_key = f"icon:{fn}@{thumb_width}"
            mem_uri = self._mem_uris.get(cache_key)
            if mem_uri:
                self._mem_uris.move_to_end(cache_key)
                result[fn] = mem_uri
            elif now_m - self._mem_miss.get(cache_key, -1e9) < 21600.0:
                continue  # 负缓存：6 小时内已确认 PRTS 无此文件，直接跳过
            else:
                self._mem_miss.pop(cache_key, None)
                pending[cache_key] = fn
        if pending:
            # 一次线程、一次 SQLite 连接读完全部磁盘缓存
            disk = await asyncio.to_thread(self.store.get_image_bytes_many, list(pending))
            for cache_key, fn in pending.items():
                raw = disk.get(cache_key)
                if raw:
                    result[fn] = self._mem_uris[cache_key] = "data:image/png;base64," + base64.b64encode(raw).decode("ascii")
                else:
                    missing.append(fn)
            while len(self._mem_uris) > 256:
                self._mem_uris.popitem(last=False)

        if not missing:
            return result, True

        url_map: dict[str, str] = {}
        confirmed_missing: set[str] = set()
        for start in range(0, len(missing), 30):
            chunk = missing[start : start + 30]
            titles_str = "|".join(f"File:{fn}" for fn in chunk)
            try:
                q_params: dict[str, Any] = {
                    "action": "query",
                    "titles": titles_str,
                    "prop": "imageinfo",
                    "iiprop": "url|size",
                }
                if thumb_width > 0:
                    q_params["iiurlwidth"] = thumb_width
                info = await self._api(q_params)
            except Exception:
                continue

            # API errors and malformed responses are transient/unknown; never negative-cache them.
            if not isinstance(info, dict) or "error" in info or not isinstance(info.get("query"), dict):
                continue

            norm_lookup = {fn.replace("_", " ").lower(): fn for fn in chunk}
            pages = info["query"].get("pages")
            if not isinstance(pages, dict):
                continue
            for pid, pinfo in pages.items():
                if not isinstance(pinfo, dict):
                    continue
                ptitle = str(pinfo.get("title") or "")
                bare = ptitle.split(":", 1)[1] if ":" in ptitle else ptitle
                orig_fn = norm_lookup.get(bare.replace("_", " ").lower())
                if not orig_fn:
                    continue
                # MediaWiki marks a definitively absent file with the `missing` property.
                if "missing" in pinfo:
                    confirmed_missing.add(orig_fn)
                    continue
                ii_list = pinfo.get("imageinfo")
                if not isinstance(ii_list, list) or not ii_list or not isinstance(ii_list[0], dict):
                    continue
                ii = ii_list[0]
                img_url = str(ii.get("thumburl") or ii.get("url") or "")
                if img_url:
                    url_map[orig_fn] = img_url

        # Negative-cache only explicit MediaWiki missing markers, never absent URLs or API errors.
        miss_now = time.monotonic()
        for fn in confirmed_missing:
            self._mem_miss[f"icon:{fn}@{thumb_width}"] = miss_now
        if len(self._mem_miss) > 512:
            self._mem_miss = {k: t for k, t in self._mem_miss.items() if miss_now - t < 21600.0}

        # 从 media.prts.wiki 静态 CDN 受控并发拉取小图标（6 并发，约 200ms 完成）
        async def _download_one(fn: str, url: str) -> None:
            try:
                async with self._cdn_sem:
                    if time.monotonic() < self._cooldown_until:
                        return
                    resp = await self._client().get(url)
                    if resp.status_code in (429, 503):
                        self._cooldown_until = max(self._cooldown_until, time.monotonic() + 30.0)
                        return
                    resp.raise_for_status()
                    raw = resp.content
                if not raw:
                    return
                cache_key = f"icon:{fn}@{thumb_width}"
                await asyncio.to_thread(self.store.put_image_bytes, cache_key, raw, url, ".png")
                ct = resp.headers.get("content-type", "").lower()
                mime = "image/webp" if "webp" in ct else ("image/jpeg" if "jpeg" in ct else "image/png")
                uri = f"data:{mime};base64," + base64.b64encode(raw).decode("ascii")
                self._mem_uris[cache_key] = uri
                if len(self._mem_uris) > 256:
                    self._mem_uris.popitem(last=False)
                result[fn] = uri
            except Exception:
                return

        if url_map:
            await asyncio.gather(*(_download_one(fn, u) for fn, u in url_map.items()))
        # 只按本次请求实际取得的图片或明确缺失的文件判断。
        return result, all(fn in result or fn in confirmed_missing for fn in missing)

    # --------------------------------------------------------
    # 轻量化扩展能力：材料总账汇总 / 语音台词 / 当期活动 / 公招计算
    # --------------------------------------------------------

    @staticmethod
    def _parse_mat_qty(raw: str) -> int:
        s = str(raw or "").strip().lower().replace(",", "").replace("，", "")
        if not s:
            return 0
        mult = 1
        if s.endswith("w") or s.endswith("万"):
            mult = 10000
            s = s[:-1]
        elif s.endswith("k") or s.endswith("千"):
            mult = 1000
            s = s[:-1]
        m = re.search(r"[\d.]+", s)
        if not m:
            return 0
        try:
            return int(round(float(m.group(0)) * mult))
        except ValueError:
            return 0

    async def get_material_summary_struct(self, target: str, detail: str = "") -> dict[str, Any]:
        """一键汇总干员养成材料总账结构化数据（支持按 精二 / 1~7级 / 一二三技能专三 / 模组 自由组合）。"""
        op = await self.get_operator_struct(target)
        if not op:
            return {"ok": False, "error": f"未找到干员「{target}」的养成材料数据。"}

        focus = (detail or "").strip().lower()
        has_elite = any(k in focus for k in ("精一", "精二", "精1", "精2", "精英"))
        has_base = any(k in focus for k in ("7级", "七级", "基础", "技能7"))
        has_skill_specific = any(k in focus for k in ("专精", "专三", "专一", "专二", "一技能", "二技能", "三技能", "1技能", "2技能", "3技能"))
        has_skill = has_skill_specific or ("技能" in focus and not has_base)
        has_mod = any(k in focus for k in ("模组", "mod"))
        default_all = not (has_elite or has_base or has_skill or has_mod) or "全" in focus

        totals: dict[str, int] = {}
        included: list[str] = []

        def _add_items(items: list[tuple[str, str]]) -> None:
            for name, qty_s in items:
                n = clean_wiki_text(name).strip()
                q = self._parse_mat_qty(qty_s)
                if n and q > 0:
                    totals[n] = totals.get(n, 0) + q

        if default_all or has_elite:
            only_e1 = ("精一" in focus or "精1" in focus) and not ("精二" in focus or "精2" in focus)
            only_e2 = ("精二" in focus or "精2" in focus) and not ("精一" in focus or "精1" in focus)
            for st in op.get("elite_mats") or []:
                if only_e1 and st["stage"] != "精1":
                    continue
                if only_e2 and st["stage"] != "精2":
                    continue
                _add_items(st["items"])
                included.append(st["stage"])

        if has_base:
            for st in op.get("skill_base_mats") or []:
                _add_items(st["items"])
            if op.get("skill_base_mats"):
                included.append("技能Lv.2~7")

        if default_all or has_skill:
            focus_clean = re.sub(r"精[一二12]|模组[123一二三]?|专[一二三123]", "", focus)
            spec_idx = [num for cn, num in (("一", 1), ("二", 2), ("三", 3)) if cn in focus_clean or str(num) in focus_clean]
            for g in op.get("skill_mastery_mats") or []:
                if spec_idx and g["index"] not in spec_idx:
                    continue
                for st in g.get("stages") or []:
                    _add_items(st["items"])
                included.append(f"{g['index']}技能专三")

        if default_all or has_mod:
            mods = op.get("modules") or []
            target_mods = mods[:1] if (default_all and len(mods) > 1 and "全模组" not in focus) else mods
            for m in target_mods:
                for st in m.get("stages") or []:
                    _add_items(st["items"])
                if m.get("stages"):
                    mtype = f"({m['type']})" if m.get("type") else ""
                    included.append(f"模组{mtype}Lv.1~3")

        if not totals:
            return {"ok": False, "error": f"未找到「{op['name']}」对应范围的材料数据。"}

        lmb = totals.pop("龙门币", 0)
        sorted_items = sorted(totals.items(), key=lambda kv: (-kv[1], kv[0]))
        return {
            "ok": True,
            "op": op,
            "included": included,
            "lmb": lmb,
            "items": sorted_items,
        }

    def format_material_summary_text(self, data: dict[str, Any]) -> str:
        """将养成材料总账结构化数据格式化为纯文本（降级兜底或无图平台使用）。"""
        if not data.get("ok"):
            return str(data.get("error") or "未找到养成材料总账数据。")
        op = data["op"]
        included = data.get("included") or []
        lmb = int(data.get("lmb") or 0)
        sorted_items = data.get("items") or []
        lines = [
            f"【{op['name']} · 养成材料总账】",
            f"统计范围：{' + '.join(included)}",
        ]
        if lmb:
            lmb_str = f"{lmb / 10000:.1f}万" if lmb >= 10000 else str(lmb)
            lines.append(f"• 龙门币：{lmb_str}（{lmb:,}）")
        chunks = [f"{name}×{cnt}" for name, cnt in sorted_items]
        lines.append("• 素材合计：" + "、".join(chunks))
        lines.append("（提示：可指定范围，如 /方舟 总计 维什戴尔 精二 三技能 模组）")
        return "\n".join(lines)

    async def query_material_summary(self, target: str, detail: str = "") -> str:
        """一键汇总干员养成材料总账（支持按 精二 / 1~7级 / 一二三技能专三 / 模组 自由组合）。"""
        data = await self.get_material_summary_struct(target, detail=detail)
        return self.format_material_summary_text(data)

    async def get_operator_lore_struct(self, target: str, detail: str = "") -> dict[str, Any]:
        """提取干员机密档案结构化数据（基本资料 + 履历/档案资料/模组故事）。"""
        op = await self.get_operator_struct(target)
        if not op:
            return {"ok": False, "error": f"未找到干员「{target}」的档案数据。"}
        fetched = await self.fetch_wikitext(op["name"])
        if not fetched:
            return {"ok": False, "error": f"未获取到「{op['name']}」的档案正文。"}
        wikitext, _ = fetched
        sections = split_sections(wikitext)
        lore_sec = sections.get("干员档案", "")
        if not lore_sec:
            return {"ok": False, "error": f"「{op['name']}」暂未收录干员档案段落。"}

        profile: list[tuple[str, str]] = []
        profile_tpls = extract_templates(lore_sec, {"人员档案set"})
        if profile_tpls:
            pp = parse_template_params(profile_tpls[0][1])
            for k, lbl in (
                ("性别", "性别"),
                ("种族", "种族"),
                ("出身地", "出身"),
                ("身高", "身高"),
                ("生日", "生日"),
                ("战斗经验", "战斗经验"),
                ("矿石病感染情况", "感染情况"),
            ):
                val = clean_wiki_text(pp.get(k, "")).strip()
                if val:
                    profile.append((lbl, val))

        archive_entries: list[tuple[str, str]] = []
        for _, inn in extract_templates(lore_sec, {"人员档案", "干员档案"}):
            ap = parse_template_params(inn)
            for idx in range(1, 15):
                heading = clean_wiki_text(ap.get(f"档案{idx}", "")).strip()
                body = clean_wiki_text(ap.get(f"档案{idx}文本", "")).strip()
                if not body:
                    continue
                if not detail.strip() and heading in ("基础档案", "综合体检测试"):
                    continue
                archive_entries.append((heading or f"档案{idx}", body))

        for _, inn in extract_templates(wikitext, {"模组"}):
            mp = parse_template_params(inn)
            if mp.get("基础证章", "").lower() in ("yes", "1", "true") or mp.get("类型图标") == "original":
                continue
            story = clean_wiki_text(mp.get("基础信息", "")).strip()
            if story:
                mname = clean_wiki_text(mp.get("名称", "")).strip()
                archive_entries.append((f"模组故事 · {mname}", story))

        if detail.strip():
            kw = detail.strip().lower()
            matched = [(h, b) for h, b in archive_entries if kw in h.lower() or kw in b.lower()]
            chosen = matched or archive_entries
        else:
            chosen = archive_entries

        if not profile and not chosen:
            return {"ok": False, "error": f"暂未解析到「{op['name']}」的详细档案。"}
        return {
            "ok": True,
            "op": op,
            "profile": profile,
            "entries": chosen[:4],
            "detail": detail.strip(),
        }

    async def get_voice_lines_struct(self, target: str, keyword: str = "") -> dict[str, Any]:
        """查询干员中文语音台词结构化数据（仅文本，不下载音频）。"""
        op = await self.get_operator_struct(target)
        title = op["name"] if op else (await self.resolve_title(target))
        if not title:
            return {"ok": False, "error": f"未找到干员「{target}」。"}
        voice_page = f"{title}/语音记录"
        fetched = await self.fetch_wikitext(voice_page)
        if not fetched:
            return {"ok": False, "error": f"未在 PRTS 找到「{title}」的语音记录。"}
        wikitext, _ = fetched

        titles_map: dict[str, str] = {}
        for m in re.finditer(r"\|\s*标题(\d+)\s*=\s*([^\n|]+)", wikitext):
            titles_map[m.group(1)] = clean_wiki_text(m.group(2)).strip()

        entries: list[tuple[str, str]] = []
        for m in re.finditer(r"\|\s*台词(\d+)\s*=([\s\S]*?)(?=\n\|\s*(?:标题|台词)\d+\s*=|\n\}\}|$)", wikitext):
            idx = m.group(1)
            block = m.group(2)
            cn_m = re.search(r"\{\{\s*VoiceData/word\s*\|\s*中文\s*\|\s*([\s\S]*?)\}\}", block)
            if not cn_m:
                continue
            line_txt = clean_wiki_text(cn_m.group(1)).replace("\n", " ").strip()
            if not line_txt:
                continue
            label = titles_map.get(idx, f"语音{idx}")
            entries.append((label, line_txt))

        if not entries:
            return {"ok": False, "error": f"暂未解析到「{title}」的中文台词。"}

        kw = (keyword or "").strip().lower()
        kw_alias = {
            "戳一戳": "戳一下",
            "摸头": "信赖触摸",
            "触摸": "信赖触摸",
            "助理": "任命助理",
            "上线": "问候",
            "抽卡": "干员报到",
            "报到": "干员报到",
        }
        kw_eff = kw_alias.get(kw, kw)
        is_random = kw in ("随机", "random", "来一句")
        if is_random:
            import random
            picked = [random.choice(entries)]
        elif kw_eff:
            matched = [(lbl, txt) for lbl, txt in entries if kw_eff in lbl.lower() or kw_eff in txt.lower()]
            if not matched:
                sample_labels = "、".join(lbl for lbl, _ in entries[:10])
                return {"ok": False, "error": f"未在「{title}」台词中找到「{keyword}」。常见分类：{sample_labels}"}
            picked = matched[:4]
        else:
            prefer = ("戳一下", "信赖触摸", "问候", "交谈1", "信赖提升后交谈1", "任命助理", "标题")
            picked = []
            for pf in prefer:
                for item in entries:
                    if pf in item[0] and item not in picked:
                        picked.append(item)
                        break
                if len(picked) >= 3:
                    break
            if not picked:
                picked = entries[:3]

        return {
            "ok": True,
            "title": title,
            "op": op or {"name": title, "stars": 6},
            "entries": picked,
            "keyword": keyword.strip(),
            "is_random": is_random,
        }

    def format_voice_lines_text(self, data: dict[str, Any]) -> str:
        """将干员语音台词结构化数据格式化为纯文本（降级兜底或无图平台使用）。"""
        if not data.get("ok"):
            return str(data.get("error") or "未找到语音台词。")
        title = data.get("title") or data["op"]["name"]
        picked = data.get("entries") or []
        if data.get("is_random") and len(picked) == 1:
            lbl, txt = picked[0]
            return f"【{title} · {lbl}】\n「{txt}」"
        out = [f"【{title} · 语音台词】"]
        for lbl, txt in picked:
            out.append(f"• {lbl}：「{txt}」")
        return "\n".join(out)

    async def query_voice_lines(self, target: str, keyword: str = "") -> str:
        """查询干员中文语音台词彩蛋（仅缓存纯文本，不下载音频）。"""
        data = await self.get_voice_lines_struct(target, keyword=keyword)
        return self.format_voice_lines_text(data)

    async def get_current_events_struct(self) -> dict[str, Any]:
        """通过 PRTS Semantic MediaWiki ask 接口获取国服当前活动与寻访卡池结构化数据（2 小时缓存，倒计时实时计算）。"""
        import datetime

        tz_cn = datetime.timezone(datetime.timedelta(hours=8))
        now_dt = datetime.datetime.now(tz_cn)
        now_ts = int(now_dt.timestamp())

        cache_title = "__prts_current_events_struct_v1__"
        raw_struct: Optional[dict[str, Any]] = None
        cached = await asyncio.to_thread(self.store.get_page, cache_title, 2 * 3600.0)
        if cached:
            try:
                parsed = json.loads(cached[0])
                if isinstance(parsed, dict) and ("gachas" in parsed or "events" in parsed):
                    raw_struct = parsed
            except Exception:
                raw_struct = None

        if raw_struct is None:
            now_iso = now_dt.strftime("%Y-%m-%dT%H:%M:%S+08:00")
            q_events = (
                f"[[分类:有活动信息的页面]][[活动结束时间::>{now_iso}]]"
                "|?名称缩短|?标题图文件名|?活动开始时间|?活动结束时间|sort=活动开始时间|order=desc|limit=12"
            )
            q_gacha = (
                f"[[分类:国服寻访]][[寻访关闭时间cn::>{now_iso}]]"
                "|?寻访名cn|?卡池名|?寻访开启时间cn|?寻访关闭时间cn|sort=寻访开启时间cn|order=desc|limit=10"
            )
            ev_res, ga_res = await asyncio.gather(
                self._api({"action": "ask", "query": q_events}),
                self._api({"action": "ask", "query": q_gacha}),
                return_exceptions=True,
            )

            def _extract_ts(arr: Any) -> int:
                if isinstance(arr, list) and arr and isinstance(arr[0], dict):
                    try:
                        return int(arr[0].get("timestamp") or 0)
                    except (TypeError, ValueError):
                        return 0
                return 0

            def _clean_file(raw_name: str) -> str:
                s = str(raw_name or "").strip()
                if s.startswith("文件:"):
                    s = s[3:].strip()
                elif s.lower().startswith("file:"):
                    s = s[5:].strip()
                return s

            raw_gachas: list[dict[str, Any]] = []
            if isinstance(ga_res, dict):
                results = ga_res.get("query", {}).get("results") or {}
                if isinstance(results, dict):
                    for title, obj in results.items():
                        po = obj.get("printouts") or {}
                        name = str((po.get("寻访名cn") or [title])[0]).strip()
                        if name.startswith("文件:"):
                            name = name[3:].rsplit(".", 1)[0]
                        st_ts = _extract_ts(po.get("寻访开启时间cn"))
                        ed_ts = _extract_ts(po.get("寻访关闭时间cn"))
                        if st_ts and st_ts - now_ts > 14 * 86400:
                            continue
                        raw_gachas.append({
                            "name": name,
                            "banner": _clean_file(title),
                            "start_ts": st_ts,
                            "end_ts": ed_ts,
                        })

            raw_events: list[dict[str, Any]] = []
            if isinstance(ev_res, dict):
                results = ev_res.get("query", {}).get("results") or {}
                if isinstance(results, dict):
                    for title, obj in results.items():
                        po = obj.get("printouts") or {}
                        name = str((po.get("名称缩短") or [title])[0]).strip()
                        banner = _clean_file(str((po.get("标题图文件名") or [""])[0]))
                        st_ts = _extract_ts(po.get("活动开始时间"))
                        ed_ts = _extract_ts(po.get("活动结束时间"))
                        if st_ts and st_ts - now_ts > 14 * 86400:
                            continue
                        raw_events.append({
                            "name": name,
                            "banner": banner,
                            "start_ts": st_ts,
                            "end_ts": ed_ts,
                        })

            raw_struct = {"gachas": raw_gachas, "events": raw_events}
            if raw_gachas or raw_events:
                await asyncio.to_thread(
                    self.store.put_page,
                    cache_title,
                    json.dumps(raw_struct, ensure_ascii=False),
                    False,
                )

        def _enrich(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
            out: list[dict[str, Any]] = []
            for it in items:
                st_ts = int(it.get("start_ts") or 0)
                ed_ts = int(it.get("end_ts") or 0)
                if ed_ts and ed_ts <= now_ts:
                    continue
                is_soon = bool(st_ts and st_ts > now_ts)
                diff = (st_ts - now_ts) if is_soon else max(0, ed_ts - now_ts)
                days, hours = divmod(diff // 3600, 24)
                is_long = (not is_soon) and (days > 180)
                if is_soon:
                    remain = f"{days}天{hours}小时后开启" if days else f"{max(1, hours)}小时后开启"
                    status = "soon"
                elif is_long:
                    remain = "长期常驻"
                    status = "long"
                else:
                    remain = f"剩余 {days}天 {hours}时" if days else f"剩余 {max(1, hours)}小时"
                    status = "urgent" if days <= 2 else "live"

                if st_ts and ed_ts and not is_long:
                    st_s = datetime.datetime.fromtimestamp(st_ts, tz_cn).strftime("%m.%d %H:%M")
                    ed_s = datetime.datetime.fromtimestamp(ed_ts, tz_cn).strftime("%m.%d %H:%M")
                    period = f"{st_s} — {ed_s}"
                elif is_long and st_ts:
                    st_s = datetime.datetime.fromtimestamp(st_ts, tz_cn).strftime("%Y.%m.%d")
                    period = f"{st_s} 起常驻开放"
                else:
                    period = ""

                # 进度条百分比（仅限时进行中有效）
                pct = 0
                if not is_soon and not is_long and st_ts and ed_ts and ed_ts > st_ts:
                    pct = max(4, min(98, int((now_ts - st_ts) * 100 / (ed_ts - st_ts))))

                out.append({
                    "name": str(it.get("name") or ""),
                    "banner": str(it.get("banner") or ""),
                    "start_ts": st_ts,
                    "end_ts": ed_ts,
                    "is_soon": is_soon,
                    "is_long": is_long,
                    "status": status,
                    "remain": remain,
                    "period": period,
                    "pct": pct,
                })
            # 排序：进行中限时（最新开启优先）> 即将开启 > 长期常驻
            out.sort(key=lambda x: (1 if x["is_soon"] else (2 if x["is_long"] else 0), -x["start_ts"]))
            return out

        return {
            "updated_at": now_dt.strftime("%Y-%m-%d %H:%M"),
            "gachas": _enrich(raw_struct.get("gachas") or []),
            "events": _enrich(raw_struct.get("events") or []),
        }

    def format_current_events_text(self, data: dict[str, Any], *, focus: str = "events") -> str:
        """将当期活动与卡池结构化数据格式化为纯文本（降级兜底或无图平台使用）。"""
        gachas = data.get("gachas") or []
        events = data.get("events") or []
        stamp = data.get("updated_at") or ""
        if focus == "gacha":
            if not gachas:
                return "暂未从 PRTS 获取到当期寻访卡池信息，请稍后再试。"
            lines = [f"【明日方舟国服 · 当期寻访卡池】（{stamp}）"]
            for g in gachas[:6]:
                p = f" | {g['period']}" if g.get("period") else ""
                lines.append(f"• {g['name']}（{g['remain']}{p}）")
            lines.append("来源：PRTS Wiki (CC BY-NC-SA 4.0)")
            return "\n".join(lines)

        if focus == "events":
            if not events:
                return "暂未从 PRTS 获取到当期活动信息，请稍后再试。"
            lines = [f"【明日方舟国服 · 当期活动】（{stamp}）"]
            for e in events[:6]:
                p = f" | {e['period']}" if e.get("period") else ""
                lines.append(f"• {e['name']}（{e['remain']}{p}）")
            lines.append("来源：PRTS Wiki (CC BY-NC-SA 4.0)")
            return "\n".join(lines)

        if not gachas and not events:
            return "暂未从 PRTS 获取到当期活动信息，请稍后再试。"
        lines = [f"【明日方舟国服 · 当期活动与卡池】（{stamp}）"]
        if gachas:
            lines.append("▸ 寻访卡池：")
            for g in gachas[:5]:
                lines.append(f"• {g['name']}（{g['remain']}）")
        if events:
            lines.append("▸ 当期活动：")
            for e in events[:6]:
                lines.append(f"• {e['name']}（{e['remain']}）")
        lines.append("来源：PRTS Wiki (CC BY-NC-SA 4.0)")
        return "\n".join(lines)

    async def query_current_events(self, *, focus: str = "events") -> str:
        """通过 PRTS Semantic MediaWiki ask 接口速查国服当前活动或寻访卡池。"""
        data = await self.get_current_events_struct()
        return self.format_current_events_text(data, focus=focus)

    async def _get_recruit_pool(self) -> list[dict[str, Any]]:
        """按需从 PRTS 干员一览拉取最新公开招募干员池并缓存 7 天（仅约 15 KB JSON）。"""
        cache_title = "__prts_recruit_pool_v1__"
        cached = await asyncio.to_thread(self.store.get_page, cache_title, self.page_ttl_seconds)
        if cached:
            try:
                data = json.loads(cached[0])
                if isinstance(data, list) and data:
                    return data
            except Exception:
                pass

        resp = await self._api({"action": "parse", "page": "干员一览", "prop": "text"})
        html_text = str(resp.get("parse", {}).get("text", {}).get("*") or resp.get("parse", {}).get("text") or "")
        if not html_text:
            return []

        pool: list[dict[str, Any]] = []
        # 每个干员是一个带 data-zh / data-profession / data-rarity / data-position / data-tag / data-obtain-method 的 div
        for m in re.finditer(r"<div\s+([^>]*data-zh=[^>]*)>", html_text):
            attrs = m.group(1)
            om = re.search(r'data-obtain-method="([^"]*)"', attrs)
            if not om or "公开招募" not in om.group(1):
                continue
            zh_m = re.search(r'data-zh="([^"]+)"', attrs)
            rar_m = re.search(r'data-rarity="(\d+)"', attrs)
            prof_m = re.search(r'data-profession="([^"]+)"', attrs)
            pos_m = re.search(r'data-position="([^"]+)"', attrs)
            tag_m = re.search(r'data-tag="([^"]*)"', attrs)
            if not (zh_m and rar_m):
                continue
            rarity = int(rar_m.group(1)) + 1
            tags: set[str] = set()
            if prof_m:
                tags.add(prof_m.group(1).strip() + "干员")
            if pos_m:
                tags.add(pos_m.group(1).strip())
            if tag_m:
                for t in tag_m.group(1).split():
                    t = t.strip()
                    if t:
                        tags.add(t)
            if rarity == 6:
                tags.add("高级资深干员")
            elif rarity == 5:
                tags.add("资深干员")
            elif rarity == 1:
                tags.add("支援机械")
            pool.append({"name": zh_m.group(1).strip(), "rarity": rarity, "tags": sorted(tags)})

        if pool:
            await asyncio.to_thread(self.store.put_page, cache_title, json.dumps(pool, ensure_ascii=False), False)
        return pool

    async def calculate_recruit_struct(self, raw_tags: list[str]) -> dict[str, Any]:
        """毫秒级公开招募保底组合计算器（结构化返回，供卡片图渲染与纯文本降级共用）。"""
        from itertools import combinations

        tag_alias = {
            "高资": "高级资深干员", "高级资深": "高级资深干员",
            "资深": "资深干员",
            "机械": "支援机械", "小车": "支援机械",
            "高台": "远程位", "远程": "远程位",
            "地面": "近战位", "近战": "近战位",
            "狙击": "狙击干员", "术师": "术师干员", "近卫": "近卫干员",
            "先锋": "先锋干员", "重装": "重装干员", "医疗": "医疗干员",
            "辅助": "辅助干员", "特种": "特种干员",
            "回费": "费用回复", "快活": "快速复活", "群攻": "群攻",
            "防护": "防护", "生存": "生存", "治疗": "治疗", "支援": "支援",
            "新手": "新手", "输出": "输出", "减速": "减速", "控场": "控场",
            "削弱": "削弱", "爆发": "爆发", "召唤": "召唤", "位移": "位移",
            "元素": "元素",
        }
        norm_tags: list[str] = []
        for t in raw_tags:
            s = str(t or "").strip()
            if not s:
                continue
            mapped = tag_alias.get(s, s)
            if mapped not in norm_tags:
                norm_tags.append(mapped)
        if not norm_tags:
            return {"ok": False, "tags": [], "combos": [], "error": "请提供公招词条，例：/公招 资深 狙击 削弱 爆发 生存"}
        norm_tags = norm_tags[:6]

        pool = await self._get_recruit_pool()
        if not pool:
            return {"ok": False, "tags": norm_tags, "combos": [], "error": "暂未能拉取 PRTS 公招池数据，请稍后再试。"}

        combos: list[dict[str, Any]] = []
        for r in range(1, min(4, len(norm_tags)) + 1):
            for comb in combinations(norm_tags, r):
                has_top = "高级资深干员" in comb
                matched = [
                    op for op in pool
                    if (op["rarity"] != 6 or has_top) and all(tg in op["tags"] for tg in comb)
                ]
                if not matched:
                    continue
                # 1★ 支援机械在 9 小时招募会被排除，除非带「支援机械」词条或全池仅 1★
                non_1 = [op["rarity"] for op in matched if op["rarity"] >= 2]
                is_robot = "支援机械" in comb or not non_1
                if is_robot:
                    min_r = 1
                    score = 45
                else:
                    min_r = min(non_1)
                    score = min_r * 10
                if min_r >= 4 or is_robot:
                    matched_sorted = sorted(matched, key=lambda x: (-x["rarity"], x["name"]))
                    combos.append({
                        "tags": list(comb),
                        "min_rarity": min_r,
                        "is_robot": is_robot,
                        "score": score,
                        "operators": [{"name": o["name"], "rarity": o["rarity"]} for o in matched_sorted],
                    })

        if not combos:
            return {
                "ok": False,
                "tags": norm_tags,
                "combos": [],
                "error": f"词条「{' / '.join(norm_tags)}」未找到必出 4★ 及以上或支援机械的保底组合（可按需刷新）。",
            }

        # 同保底星级下，优先把人数更少、更精准的狙击组合排在前面
        combos.sort(key=lambda x: (-x["score"], len(x["operators"]), len(x["tags"])))
        return {"ok": True, "tags": norm_tags, "combos": combos[:12], "error": ""}

    @staticmethod
    def format_recruit_text(data: dict[str, Any]) -> str:
        """将结构化公招计算结果格式化为紧凑纯文本（不支持发图平台或降级时使用）。"""
        if not data.get("ok"):
            return str(data.get("error") or "未找到高星保底组合。")
        norm_tags = data.get("tags") or []
        combos = data.get("combos") or []
        lines = [f"【公开招募保底组合】（{' / '.join(norm_tags)}）"]
        for c in combos[:8]:
            badge = "★1 支援机械" if c.get("is_robot") else f"保底{c['min_rarity']}★"
            ops = c.get("operators") or []
            op_names = "、".join(f"{o['name']}({o['rarity']}★)" for o in ops[:6])
            if len(ops) > 6:
                op_names += f" 等{len(ops)}人"
            lines.append(f"• [{badge}] {' + '.join(c['tags'])} → {op_names}")
        return "\n".join(lines)

    async def calculate_recruit(self, raw_tags: list[str]) -> str:
        """毫秒级公开招募 Tag 保底组合计算器（纯文本接口）。"""
        data = await self.calculate_recruit_struct(raw_tags)
        return self.format_recruit_text(data)

    # --------------------------------------------------------
    # BOSS / 敌人数值与机制解析（兼容干员同名自动跳转「xxx(敌方)」）
    # --------------------------------------------------------

    async def get_enemy_struct(self, target: str, detail: str = "") -> Optional[dict[str, Any]]:
        """解析 PRTS 敌人/BOSS 结构化数值、控制抗性与技能机制。"""
        q = str(target or "").strip()
        if not q:
            return None

        wikitext = ""
        title = ""

        # 优先尝试解析标题；若命中干员页则自动尝试「xxx(敌方)」或搜索敌方词条
        cand_titles: list[str] = []
        resolved = await self.resolve_title(q)
        if resolved:
            cand_titles.append(resolved)
        if not q.endswith("(敌方)"):
            cand_titles.append(f"{q}(敌方)")
        if resolved and not resolved.endswith("(敌方)") and f"{resolved}(敌方)" not in cand_titles:
            cand_titles.append(f"{resolved}(敌方)")

        for ct in cand_titles:
            fetched = await self.fetch_wikitext(ct)
            if not fetched:
                continue
            wt, _ = fetched
            if "敌人信息/" in wt:
                title, wikitext = ct, wt
                break
            # 页面里若有 {{参阅|xxx(敌方)|...}}，顺藤摸瓜跳到敌方页
            ref_m = re.search(r"\{\{\s*参阅\s*\|\s*([^|}\n]+敌方[^|}\n]*)", wt)
            if ref_m:
                ref_title = ref_m.group(1).strip()
                ref_fetched = await self.fetch_wikitext(ref_title)
                if ref_fetched and "敌人信息/" in ref_fetched[0]:
                    title, wikitext = ref_title, ref_fetched[0]
                    break

        if not wikitext:
            # 模糊搜索带“敌方/领袖”兜底
            sr = await self._api({"action": "query", "list": "search", "srsearch": f"{q} 敌人信息", "srlimit": 5})
            for hit in sr.get("query", {}).get("search", []):
                ht = str(hit.get("title") or "").strip()
                if not ht:
                    continue
                fetched = await self.fetch_wikitext(ht)
                if fetched and "敌人信息/" in fetched[0]:
                    title, wikitext = ht, fetched[0]
                    break

        if not wikitext:
            return None

        common_tpls = extract_templates(wikitext, {"敌人信息/common2", "敌人信息/common"})
        common = parse_template_params(common_tpls[0][1]) if common_tpls else {}
        level_tpls = extract_templates(wikitext, {"敌人信息/levelcontent"})
        levels = [parse_template_params(inner) for _, inner in level_tpls]

        # 按 detail 选择级别（PRTS 为 0 起：级别0、级别1...；若用户输入超出最高档则自动取最高档，避免静默回退到级别0）
        lv_idx = 0
        lv_m = re.search(r"(\d+)", str(detail or ""))
        if lv_m and levels:
            want_lv = int(lv_m.group(1))
            if want_lv >= len(levels):
                lv_idx = len(levels) - 1
            elif want_lv >= 0:
                lv_idx = want_lv
        lv = levels[lv_idx] if levels else {}

        name = clean_wiki_text(common.get("名称") or lv.get("名称") or title).strip()
        rank = clean_wiki_text(common.get("地位级别") or lv.get("地位") or "普通").strip()
        resist_map = (
            ("眩晕抗性", "眩晕"), ("沉默抗性", "沉默"), ("沉睡抗性", "沉睡"),
            ("冻结抗性", "冻结"), ("浮空抗性", "浮空"), ("战栗抗性", "战栗"),
            ("恐惧抗性", "恐惧"), ("麻痹抗性", "麻痹"), ("诱导抗性", "诱导"),
            ("传送抗性", "传送"), ("缚地抗性", "缚地"),
        )
        immunities = [label for key, label in resist_map if str(lv.get(key) or "").strip() in ("有", "1", "true", "免疫")]

        skills: list[dict[str, str]] = []
        for i in range(0, 10):
            sname = clean_wiki_text(lv.get(f"技能{i}", "")).replace("\n", " ").strip()
            seff = clean_wiki_text(lv.get(f"技能{i}效果", "")).strip()
            if sname or seff:
                skills.append({
                    "name": sname or f"技能 {i + 1}",
                    "init_sp": str(lv.get(f"技能{i}初始") or "").strip(),
                    "cost_sp": str(lv.get(f"技能{i}消耗") or "").strip(),
                    "effect": seff,
                })

        ability_raw = common.get("能力") or common.get("能力修正") or ""
        ability = clean_wiki_text(ability_raw).strip()
        talent = clean_wiki_text(lv.get("天赋", "")).strip()

        return {
            "title": title,
            "name": name,
            "index": str(common.get("index") or "").strip(),
            "rank": rank,
            "race": clean_wiki_text(common.get("种类") or lv.get("种类") or "").strip(),
            "damage_type": clean_wiki_text(common.get("伤害类型") or "").strip(),
            "attack_mode": clean_wiki_text(common.get("攻击方式") or lv.get("攻击方式") or "").strip(),
            "move_mode": clean_wiki_text(common.get("行动方式") or lv.get("行动方式") or "").strip(),
            "event": clean_wiki_text(common.get("登场活动") or "").strip(),
            "level_index": lv_idx,
            "level_count": len(levels),
            "stats": {
                "hp": str(lv.get("最大生命值") or "-").strip(),
                "atk": str(lv.get("攻击力") or "-").strip(),
                "def": str(lv.get("防御力") or "-").strip(),
                "res": str(lv.get("法术抗性") or "-").strip(),
                "speed": str(lv.get("移动速度") or "").strip(),
                "interval": str(lv.get("攻击间隔") or "").strip(),
                "weight": str(lv.get("重量等级") or "").strip(),
                "life_point": str(lv.get("数量") or "").strip(),
                "elem_res": str(lv.get("元素抗性") or "").strip(),
            },
            "immunities": immunities,
            "ability": ability,
            "talent": talent,
            "skills": skills,
        }

    @staticmethod
    def format_enemy_text(en: dict[str, Any]) -> str:
        """将敌人/BOSS 结构化数据格式化为紧凑纯文本。"""
        st = en.get("stats") or {}
        tags = [x for x in (en.get("rank"), en.get("race"), en.get("damage_type"), en.get("attack_mode"), en.get("move_mode")) if x]
        lv_tip = f"（级别{en.get('level_index', 0)}，共{en.get('level_count', 1)}档）" if (en.get("level_count") or 0) > 1 else ""
        lines = [
            f"【{en['name']} · 敌方档案】{lv_tip}",
            "定位：" + " / ".join(tags) if tags else "",
            f"属性：生命 {st.get('hp','-')} | 攻击 {st.get('atk','-')} | 防御 {st.get('def','-')} | 法抗 {st.get('res','-')} | 攻速间隔 {st.get('interval','-')}s | 移速 {st.get('speed','-')} | 重量 {st.get('weight','-')}",
            "控制免疫：" + ("、".join(en["immunities"]) if en.get("immunities") else "无常规异常免疫"),
        ]
        if en.get("ability"):
            ab = en["ability"]
            lines.append("核心机制：\n" + (ab[:420] + "…" if len(ab) > 420 else ab))
        if en.get("skills"):
            lines.append("主要技能：")
            for sk in en["skills"][:4]:
                eff = sk["effect"].replace("\n", " ")
                lines.append(f"• {sk['name']}：{eff[:160]}{'…' if len(eff) > 160 else ''}")
        lines.append("来源：PRTS Wiki (CC BY-NC-SA 4.0)")
        return "\n".join(x for x in lines if x)

    async def query_enemy(self, target: str, detail: str = "") -> str:
        """查询敌人/BOSS 数值与机制（纯文本接口）。"""
        en = await self.get_enemy_struct(target, detail=detail)
        if not en:
            return f"未在 PRTS Wiki 找到敌人或 BOSS「{target}」，请检查名称。"
        return self.format_enemy_text(en)
