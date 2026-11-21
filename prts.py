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
_RE_WIKILINK = re.compile(r"\[\[(?:[^|\]]+\|)?([^\]]+)\]\]")
_RE_EXTLINK = re.compile(r"\[https?://\S+\s+([^\]]+)\]")
_RE_MATERIAL = re.compile(r"\{\{材料消耗\|([^|{}]+)\|([^|{}]+)(?:\|[^{}]*)?\}\}")
_RE_INLINE_TPL = re.compile(
    r"\{\{(?:color|colour|变动数值lite|变动数值|术语|黑幕|ruby|lj|注释|提示|b|u)\|(?:[^{}]*\|)?([^{}|]+)\}\}",
    re.I,
)
_RE_OTHER_TPL = re.compile(r"\{\{[^{}]*\}\}")
_RE_SECTION_HDR = re.compile(r"^==\s*([^=\n]+?)\s*==\s*$", re.M)
_RE_SKIN_NAME = re.compile(r"^时装(\d+)名称$")


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


def clean_wiki_text(text: str) -> str:
    """将常见 PRTS Wikitext 标记清洗为紧凑可读纯文本。"""
    if not text:
        return ""
    s = _RE_HTML_COMMENT.sub("", text)
    s = _RE_REF_TAG.sub("", s)
    s = _RE_BR_TAG.sub("\n", s)
    s = _RE_MATERIAL.sub(lambda m: f"{m.group(1).strip()}×{m.group(2).strip()}", s)
    # 多轮展开常见内联格式模板
    for _ in range(3):
        ns = _RE_INLINE_TPL.sub(r"\1", s)
        if ns == s:
            break
        s = ns
    s = _RE_WIKILINK.sub(r"\1", s)
    s = _RE_EXTLINK.sub(r"\1", s)
    s = _RE_HTML_TAG.sub("", s)
    s = _RE_MAGIC_WORD.sub("", s)
    # 清理残余简单模板
    for _ in range(2):
        ns = _RE_OTHER_TPL.sub("", s)
        if ns == s:
            break
        s = ns
    s = s.replace("'''", "").replace("''", "")
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
        user_agent: str = "MaiBot-ArknightsWiki/0.2.0 (+https://github.com/Allure-Gui/maibot-arknights-wiki)",
        timeout_seconds: float = 12.0,
        min_interval_ms: int = 500,
        page_ttl_hours: int = 168,
        thumb_width: int = 1600,
    ) -> None:
        self.store = store
        self.matcher = matcher
        self.api_url = api_url.strip() or "https://prts.wiki/api.php"
        self.user_agent = user_agent.strip() or "MaiBot-ArknightsWiki/0.1.0"
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

    def clear_memory_cache(self) -> None:
        """清空 L1 内存热缓存。"""
        self._mem_titles.clear()
        self._mem_structs.clear()
        self._mem_uris.clear()

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
                elapsed = time.monotonic() - self._last_req_ts
                if elapsed < self.min_interval:
                    await asyncio.sleep(self.min_interval - elapsed)
                self._last_req_ts = time.monotonic()

            resp = await self._client().get(url, params=params)
            if resp.status_code in (429, 503):
                retry_after = 60.0
                raw_ra = resp.headers.get("Retry-After", "").strip()
                if raw_ra.isdigit():
                    retry_after = min(300.0, max(15.0, float(raw_ra)))
                self._cooldown_until = time.monotonic() + retry_after
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

        # 4. 模糊搜索兜底
        sr = await self._api({"action": "query", "list": "search", "srsearch": q, "srlimit": 5})
        hits = sr.get("query", {}).get("search", [])
        if not hits:
            return None
        # 优先排除“/语音记录”“/干员密录”等子页
        best = next((h["title"] for h in hits if "/" not in str(h.get("title", ""))), hits[0]["title"])
        best = str(best).strip()
        self._mem_titles[q_key] = best
        await asyncio.to_thread(self.store.put_redirect, raw_query, best)
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
        title = await self.resolve_title(target)
        if not title:
            return f"未在 PRTS Wiki 找到与「{target}」匹配的词条，请检查名称或换个关键词。"

        fetched = await self.fetch_wikitext(title)
        if not fetched:
            return f"找到了词条「{title}」，但暂未获取到正文内容。"

        wikitext, is_operator = fetched
        sec = (section or "overview").strip().lower()
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
        skill_tpls = extract_templates(wikitext, {"技能"})
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
        skill_tpls = extract_templates(wikitext, {"技能"})
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
            lines.append("▶ 精英化：")
            for k in ("精1", "精2"):
                if ep.get(k):
                    lines.append(f"  {k}：{format_materials_inline(ep[k])}")

        # 2. 技能升级与专精材料
        skill_mat_tpls = extract_templates(wikitext, {"技能升级材料"})
        if skill_mat_tpls and (show_all or has_skill_kw):
            smp = parse_template_params(skill_mat_tpls[0][1])
            skill_names = [parse_template_params(inn).get("技能名", "") for _, inn in extract_templates(wikitext, {"技能"})]

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
                sname = skin_map.get(stem, "")
                label = f"皮肤「{sname}」({stem})" if sname else f"皮肤({stem})"
            else:
                tag, label = stem, f"立绘({stem})"
            items.append({"file_name": fname, "tag": tag, "label": label, "skin_name": skin_map.get(stem, "")})

        if items:
            await asyncio.to_thread(self.store.put_image_list, operator_name, json.dumps(items, ensure_ascii=False))
        return items

    async def fetch_illustration(self, operator_query: str, variant: str = "e2") -> IllustrationResult:
        """按干员名/别称与变体（e2/e0/skin1/皮肤名/list）获取立绘缩图字节。"""
        op_name = await self.resolve_title(operator_query)
        if not op_name:
            return IllustrationResult(ok=False, operator=operator_query, error=f"未找到干员「{operator_query}」。")

        items = await self.list_illustrations(op_name)
        if not items:
            return IllustrationResult(ok=False, operator=op_name, error=f"未在 PRTS 找到「{op_name}」的立绘文件。")

        avail = [f"{it['tag']}: {it['label']}" for it in items]
        v = (variant or "e2").strip().lower()
        if v in ("list", "列表", "全部", "皮肤列表"):
            return IllustrationResult(ok=True, operator=op_name, available_variants=avail, label="列表")

        chosen: Optional[dict[str, str]] = None
        if v in ("e2", "精二", "精2", "2", "默认", ""):
            chosen = next((it for it in items if it["tag"] == "e2"), None) or next(
                (it for it in items if it["tag"] == "e0"), items[0]
            )
        elif v in ("e0", "e1", "初始", "原皮", "精0", "精1", "1"):
            chosen = next((it for it in items if it["tag"] == "e0"), items[0])
        elif v in ("skin", "皮肤", "时装", "最新皮肤"):
            skins = [it for it in items if it["tag"].startswith("skin")]
            chosen = skins[-1] if skins else items[-1]
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
        cached_bytes = await asyncio.to_thread(self.store.get_image_bytes, cache_key)
        if cached_bytes:
            return IllustrationResult(
                ok=True,
                operator=op_name,
                label=chosen["label"],
                file_name=fname,
                image_bytes=cached_bytes,
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
        for idx, (_, inner) in enumerate(extract_templates(wikitext, {"技能"}), 1):
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

    async def fetch_file_data_uris(self, file_names: list[str], *, thumb_width: int = 160) -> dict[str, str]:
        """批量极速获取 PRTS 小图标/头像/半身像并转为离线 data:image/png;base64 URI（L1内存 + L2磁盘双级缓存）。"""
        unique_names = list(dict.fromkeys(f.strip() for f in file_names if f and f.strip()))
        if not unique_names:
            return {}

        result: dict[str, str] = {}
        missing: list[str] = []
        for fn in unique_names:
            cache_key = f"icon:{fn}@{thumb_width}"
            mem_uri = self._mem_uris.get(cache_key)
            if mem_uri:
                self._mem_uris.move_to_end(cache_key)
                result[fn] = mem_uri
                continue
            cached = await asyncio.to_thread(self.store.get_image_bytes, cache_key)
            if cached:
                uri = "data:image/png;base64," + base64.b64encode(cached).decode("ascii")
                self._mem_uris[cache_key] = uri
                if len(self._mem_uris) > 256:
                    self._mem_uris.popitem(last=False)
                result[fn] = uri
            else:
                missing.append(fn)

        if not missing:
            return result

        # 每批最多 30 个文件，1 次 MediaWiki API 批量查 URL（极大降低对 PRTS 服务器压力）
        url_map: dict[str, str] = {}
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

            # 建立规范化标题回查表（MediaWiki 会把下划线转为空格、去掉 File:/文件: 前缀）
            norm_lookup = {fn.replace("_", " ").lower(): fn for fn in chunk}
            for pid, pinfo in info.get("query", {}).get("pages", {}).items():
                if int(pid) <= 0:
                    continue
                ptitle = str(pinfo.get("title") or "")
                bare = ptitle.split(":", 1)[1] if ":" in ptitle else ptitle
                orig_fn = norm_lookup.get(bare.replace("_", " ").lower())
                if not orig_fn:
                    continue
                ii = (pinfo.get("imageinfo") or [{}])[0]
                img_url = str(ii.get("thumburl") or ii.get("url") or "")
                if img_url:
                    url_map[orig_fn] = img_url

        # 从 media.prts.wiki 静态 CDN 受控并发拉取小图标（6 并发，约 200ms 完成）
        async def _download_one(fn: str, url: str) -> None:
            if time.monotonic() < self._cooldown_until:
                return
            try:
                async with self._cdn_sem:
                    resp = await self._client().get(url)
                    if resp.status_code in (429, 503):
                        self._cooldown_until = time.monotonic() + 30.0
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
        return result
