"""微秒级《明日方舟》黑话与干员语境识别器（含双档置信度防误触门控）。

设计原则：
- Tier 1 强专属词（如 维什戴尔/史尔特尔/水陈/罗德岛/精二/模组）：单次出现立即激活并点亮会话热态；
- Tier 2 日常多义/短词（如 叔叔/小羊/山/年/令/夕/42/ew）：仅在同窗多词共现、会话热态期内、
  或伴随方舟游戏动作词时才放行，将日常闲聊误触率压到接近 0%。
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence

_ASCII_WORD_RE = re.compile(r"^[a-z0-9_]+$")
_CUSTOM_ALIAS_SPLIT_RE = re.compile(r"\s*[=＝:：]\s*|\s+")


def parse_custom_alias_line(line: str) -> tuple[str, str, str] | None:
    """宽容解析自定义黑话行，支持 `外号=干员名|定位`、`外号:干员名`、`外号 干员名` 等常见写法。"""
    s = str(line or "").strip()
    if not s or s.startswith("#"):
        return None
    if any(sep in s for sep in ("=", "＝", ":", "：")):
        parts = re.split(r"[=＝:：]", s, maxsplit=1)
        alias = parts[0].strip()
        rhs = parts[1].strip() if len(parts) > 1 else ""
    else:
        parts = s.split(None, 1)
        if len(parts) < 2:
            return None
        alias, rhs = parts[0].strip(), parts[1].strip()
    if not alias or not rhs:
        return None
    if "|" in rhs or "｜" in rhs:
        target, brief = [p.strip() for p in re.split(r"[|｜]", rhs, maxsplit=1)]
    else:
        target, brief = rhs.strip(), "自定义别称"
    if not target:
        return None
    return alias, target, brief or "自定义别称"


def load_runtime_aliases(path: Path) -> list[str]:
    """从插件数据目录读取运行时热维护的自定义别称列表。"""
    if not path.is_file():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, list):
            return [str(x).strip() for x in data if str(x).strip()]
    except Exception:
        pass
    return []


def save_runtime_aliases(path: Path, aliases: Sequence[str]) -> None:
    """将运行时自定义别称列表持久化到插件数据目录。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    clean = [str(x).strip() for x in aliases if str(x).strip()]
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(clean, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


@dataclass(frozen=True, slots=True)
class GlossaryMatch:
    """命中的单条方舟词条。"""

    term: str
    target: str
    brief: str
    tier: int


class ArknightsContextMatcher:
    """双档置信度方舟语境匹配器。"""

    def __init__(
        self,
        glossary_path: Path,
        custom_aliases: Sequence[str] | None = None,
        hot_ttl_seconds: float = 300.0,
        custom_alias_force_tier1: bool = True,
    ) -> None:
        self.hot_ttl_seconds = max(30.0, float(hot_ttl_seconds))
        self.custom_alias_force_tier1 = bool(custom_alias_force_tier1)
        self._hot_sessions: dict[str, float] = {}
        self._context_words: list[str] = []
        self._tier1: list[tuple[str, str, str, bool]] = []
        self._tier2: list[tuple[str, str, str, bool]] = []
        self._alias_lookup: dict[str, tuple[str, str]] = {}
        self._load(glossary_path, custom_aliases or ())

    def _load(self, glossary_path: Path, custom_aliases: Sequence[str]) -> None:
        raw = json.loads(glossary_path.read_text(encoding="utf-8"))
        self._context_words = [str(w).strip().lower() for w in raw.get("context_words", []) if str(w).strip()]

        # 先解析用户自定义别称，允许覆盖内置词典同名外号
        custom_parsed: list[tuple[str, str, str, bool]] = []
        custom_keys: set[str] = set()
        for line in custom_aliases:
            parsed = parse_custom_alias_line(line)
            if not parsed:
                continue
            alias, target, brief = parsed
            key_lower = alias.lower()
            is_ascii = bool(_ASCII_WORD_RE.fullmatch(key_lower))
            custom_parsed.append((alias, target, brief, is_ascii))
            custom_keys.add(key_lower)

        def add_entries(mapping: dict[str, list[str]], bucket: list[tuple[str, str, str, bool]]) -> None:
            for k, v in mapping.items():
                term = str(k).strip()
                if not term or not isinstance(v, list) or not v:
                    continue
                key_lower = term.lower()
                if key_lower in custom_keys:
                    continue
                target = str(v[0]).strip()
                brief = str(v[1]).strip() if len(v) > 1 else ""
                is_ascii = bool(_ASCII_WORD_RE.fullmatch(key_lower))
                bucket.append((term, target, brief, is_ascii))
                self._alias_lookup[key_lower] = (target, brief)

        add_entries(raw.get("tier1", {}), self._tier1)
        add_entries(raw.get("tier2", {}), self._tier2)

        for alias, target, brief, is_ascii in custom_parsed:
            key_lower = alias.lower()
            if self.custom_alias_force_tier1:
                bucket = self._tier1
            else:
                bucket = self._tier1 if (not is_ascii and len(alias) >= 2) or (is_ascii and len(alias) >= 3) else self._tier2
            bucket.append((alias, target, brief, is_ascii))
            self._alias_lookup[key_lower] = (target, brief)

        # 按词长降序，优先匹配长词避免短词遮蔽（如 "42姐" 优先于 "42"）
        self._tier1.sort(key=lambda x: len(x[0]), reverse=True)
        self._tier2.sort(key=lambda x: len(x[0]), reverse=True)

    def resolve_alias_local(self, query: str) -> tuple[str, str] | None:
        """从本地词典快速解析别称，返回 (标准PRTS词条名, 简短定位)。"""
        key = str(query or "").strip().lower()
        if not key:
            return None
        return self._alias_lookup.get(key)

    def is_session_hot(self, session_id: str) -> bool:
        """判断会话是否处于方舟讨论热态窗口内。"""
        ts = self._hot_sessions.get(session_id)
        if ts is None:
            return False
        if time.time() - ts <= self.hot_ttl_seconds:
            return True
        self._hot_sessions.pop(session_id, None)
        return False

    def mark_session_hot(self, session_id: str) -> None:
        """标记会话进入方舟讨论热态。"""
        if session_id:
            self._hot_sessions[session_id] = time.time()
            # 防止长期运行字典无限增长
            if len(self._hot_sessions) > 512:
                now = time.time()
                expired = [sid for sid, t in self._hot_sessions.items() if now - t > self.hot_ttl_seconds]
                for sid in expired:
                    self._hot_sessions.pop(sid, None)

    @staticmethod
    def _contains(text_lower: str, term_lower: str, is_ascii: bool) -> bool:
        if term_lower not in text_lower:
            return False
        if not is_ascii:
            return True
        # ASCII/数字短词（如 ew、42、w）强制做英文数字边界隔离，避免误命中 news / 1420 / www
        return bool(re.search(rf"(?<![a-z0-9_]){re.escape(term_lower)}(?![a-z0-9_])", text_lower))

    def scan(
        self,
        session_id: str,
        recent_texts: Iterable[str],
        *,
        max_items: int = 6,
    ) -> list[GlossaryMatch]:
        """扫描最近聊天消息，通过双档置信度门控返回命中的方舟词条。"""
        joined = "\n".join(str(t or "") for t in recent_texts if str(t or "").strip())
        if not joined:
            return []
        text_lower = joined.lower()

        tier1_hits: list[GlossaryMatch] = []
        tier2_hits: list[GlossaryMatch] = []
        seen_targets: set[str] = set()

        for term, target, brief, is_ascii in self._tier1:
            if target in seen_targets:
                continue
            if self._contains(text_lower, term.lower(), is_ascii):
                seen_targets.add(target)
                tier1_hits.append(GlossaryMatch(term=term, target=target, brief=brief, tier=1))
                if len(tier1_hits) >= max_items:
                    break

        has_context = any(cw in text_lower for cw in self._context_words)
        is_hot = self.is_session_hot(session_id)

        # Tier 2 收集（仅在需要时扫描）
        if len(tier1_hits) < max_items:
            for term, target, brief, is_ascii in self._tier2:
                if target in seen_targets:
                    continue
                if self._contains(text_lower, term.lower(), is_ascii):
                    seen_targets.add(target)
                    tier2_hits.append(GlossaryMatch(term=term, target=target, brief=brief, tier=2))
                    if len(tier1_hits) + len(tier2_hits) >= max_items:
                        break

        # 双档门控决策
        if tier1_hits:
            self.mark_session_hot(session_id)
            return (tier1_hits + tier2_hits)[:max_items]

        # 只有长度 >= 2 的 Tier 2 词（如 叔叔/小羊/ew/42）才允许通过多词共现自举激活；
        # 单字中文干员名（如 年/山/红/黑/陈/余）严禁自举，必须依赖热态或明确方舟上下文词。
        multi_char_t2 = [h for h in tier2_hits if len(h.term) >= 2]
        if tier2_hits and (is_hot or has_context or len(multi_char_t2) >= 2):
            self.mark_session_hot(session_id)
            return tier2_hits[:max_items]

        if has_context:
            # 没具体干员名但明确在聊方舟（如"精二材料不够"），不强行注词，只续热态
            self.mark_session_hot(session_id)
        return []

    @staticmethod
    def build_planner_hint(matches: Sequence[GlossaryMatch]) -> str:
        """构造紧凑、缓存友好的 Planner 一次性语境参考（严格控制在 ~180 字内）。"""
        if not matches:
            return ""
        parts: list[str] = []
        for m in matches:
            label = f"{m.term}={m.target}" if m.term.lower() != m.target.lower() else m.target
            if m.brief:
                parts.append(f"{label}({m.brief})")
            else:
                parts.append(label)
        body = "；".join(parts)
        return (
            f"[方舟语境参考·仅助理解切忌生硬科普] {body}。"
            "若群友询问具体技能/模组/材料/档案或剧情，可调用 ak_wiki_query；"
            "若想看干员立绘/皮肤，可调用 ak_send_illustration 直接发图。"
        )
