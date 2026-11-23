"""明日方舟wiki插件：罗德岛战术卡片 HTML 渲染器。

所有卡片共用 assets/card.css（模块导入时只读一次）；相同图标在 CSS 中只内嵌一次，
通过 class 复用，避免 Base64 在 HTML 中重复出现，减小体积、加快截图。
"""

from __future__ import annotations

import html
from pathlib import Path
from typing import Any

from .prts import PRTSClient

try:
    _CSS = (Path(__file__).resolve().parent / "assets" / "card.css").read_text(encoding="utf-8")
except (OSError, UnicodeError):
    _CSS = ""

_PALETTE: dict[int, tuple[str, str]] = {
    6: ("#F2A24E", "rgba(242,162,78,.13)"),
    5: ("#E4C469", "rgba(228,196,105,.13)"),
    4: ("#B9A0F2", "rgba(185,160,242,.13)"),
    3: ("#74BCEA", "rgba(116,188,234,.13)"),
    2: ("#9CCB72", "rgba(156,203,114,.13)"),
    1: ("#A0AABA", "rgba(160,170,186,.13)"),
}


def _esc(val: Any) -> str:
    return html.escape(str(val or ""), quote=True)


def _stars(op: dict[str, Any]) -> int:
    return max(1, min(6, int(op.get('stars') or 6)))


def _doc(
    title: str, body: str, accent: str, soft: str, width: int,
    extra_css: str = "",
) -> str:
    if not _CSS.strip():
        raise RuntimeError("卡片样式表未加载")
    return (
        "<!DOCTYPE html><html lang='zh-CN'><head><meta charset='utf-8'/>"
        f"<title>{_esc(title)}</title><style>{_CSS}{extra_css}</style></head><body>"
        f"<div class='card' style='--ac:{accent};--as:{soft};width:{width}px'>{body}"
        "<div class='ft'><span>DATA: PRTS WIKI (CC BY-NC-SA 4.0)</span>"
        "<span>ASSETS © HYPERGRYPH · MAIPRTS</span></div></div></body></html>"
    )


class _Icons:
    """把图标 Data URI 收敛为 CSS class：同一图标无论出现多少次，只内嵌一份。"""

    def __init__(self, uris: dict[str, str]) -> None:
        self.uris = uris
        self._cls: dict[str, str] = {}
        self._rules: list[str] = []

    def cls(self, name: str) -> str:
        uri = self.uris.get(f"道具_带框_{name}.png") or self.uris.get(f"道具_{name}.png") or ""
        if not uri:
            return ""
        c = self._cls.get(uri)
        if c is None:
            c = f"m{len(self._cls)}"
            self._cls[uri] = c
            self._rules.append(f".{c}{{background-image:url({uri})}}")
        return c

    @property
    def css(self) -> str:
        return "".join(self._rules)


def _mats(items: list[tuple[str, str]], icons: _Icons) -> str:
    cells = []
    for name, cnt in items:
        c = icons.cls(name)
        face = f"<i class='{c}'></i>" if c else f"<b>{_esc(name[:4])}</b>"
        cells.append(
            f"<div class='mi'><div class='ib'>{face}<span class='mc'>×{_esc(cnt)}</span></div>"
            f"<div class='mn'>{_esc(name)}</div></div>"
        )
    return f"<div class='mg'>{''.join(cells)}</div>"


def _rows(stages: list[dict[str, Any]], icons: _Icons) -> str:
    return "".join(
        f"<div class='sr'><div class='sl'>{_esc(s['stage'])}</div>{_mats(s['items'], icons)}</div>"
        for s in stages
    )


def _sec(idx: str, title: str, hint: str, inner: str) -> str:
    return (
        f"<div class='sc'><div class='st'><i>{_esc(idx)}</i><b>{_esc(title)}</b>"
        f"<s>{_esc(hint)}</s></div>{inner}</div>"
    )


def _hdr(op: dict[str, Any], label: str, av_uri: str) -> str:
    av = f"<img src='{av_uri}'/>" if av_uri else f"<i>{_esc(op['name'][:1])}</i>"
    tags = []
    if op.get('profession'):
        tags.append(f"<span class='t a'>{_esc(op['profession'])} · {_esc(op.get('sub_profession'))}</span>")
    if op.get('position'):
        tags.append(f"<span class='t'>{_esc(op['position'])}</span>")
    if op.get('org'):
        tags.append(f"<span class='t b'>{_esc(op['org'])}</span>")
    wm = _esc(str(op.get('en_name') or 'PRTS').upper())
    return (
        f"<div class='hd' data-w='{wm}'><div class='av'>{av}</div>"
        f"<div class='hm'><div class='nm'><span class='on'>{_esc(op['name'])}</span>"
        f"<span class='en'>{_esc(op.get('en_name'))}</span></div>"
        f"<div class='rs'>{'★' * _stars(op)}</div><div class='tg'>{''.join(tags)}</div></div>"
        f"<div class='hr'><b>PRTS // TERMINAL</b><span>{_esc(label)}</span></div></div><div class='hz'></div>"
    )


def _files(stage_lists: list[list[dict[str, Any]]]) -> list[str]:
    names = [f"道具_带框_{m}.png" for stages in stage_lists for st in stages for m, _ in st.get('items', [])]
    return list(dict.fromkeys(names))


async def build_operator_card_html(
    prts: PRTSClient,
    op: dict[str, Any],
    *,
    section: str = "materials",
    detail: str = "",
) -> tuple[str, int, int, bool]:
    """返回 HTML、宽度、预估高度，以及本次渲染是否可缓存。"""
    ac, soft = _PALETTE.get(_stars(op), _PALETTE[6])
    name = op['name']
    sec = (section or 'materials').strip().lower()
    focus = (detail or '').strip().lower()
    av = [f'头像_{name}_2.png', f'头像_{name}.png']
    need = list(av)
    width = 660
    is_mod = sec in ('modules', 'module', '模组')
    is_mat = sec in ('materials', 'material', '材料', '升级材料', '专精材料')

    if is_mod or is_mat:
        elite, groups, mods = [], [], []
        label = '专属模组档案'
        if is_mod:
            mods = op.get('modules') or []
        else:
            fc = focus.replace('精一', '').replace('精二', '').replace('精1', '').replace('精2', '')
            e_kw = any(k in focus for k in ('精', 'elite'))
            s_kw = any(k in fc for k in ('技', '专', 'skill', '1', '2', '3', '一', '二', '三'))
            m_kw = any(k in focus for k in ('模组', 'mod'))
            every = not (e_kw or s_kw or m_kw) or '全' in focus
            if every or e_kw:
                elite = op.get('elite_mats') or []
                # detail 明确指定精一/精二时只保留对应阶段
                only_e1 = ('精一' in focus or '精1' in focus) and not ('精二' in focus or '精2' in focus)
                only_e2 = ('精二' in focus or '精2' in focus) and not ('精一' in focus or '精1' in focus)
                if only_e1 and not only_e2:
                    elite = [s for s in elite if s.get('stage') == '精1']
                elif only_e2 and not only_e1:
                    elite = [s for s in elite if s.get('stage') == '精2']
            if every or s_kw:
                pick = [n for cn, n in (('一', 1), ('二', 2), ('三', 3)) if cn in fc or str(n) in fc]
                groups = [g for g in (op.get('skill_mastery_mats') or []) if not pick or g['index'] in pick]
            if every or m_kw:
                mods = op.get('modules') or []
            label = f'材料清单 · {detail}' if detail.strip() else '养成升级材料'
        need += _files([elite, *[g.get('stages', []) for g in groups], *[m.get('stages', []) for m in mods]])
        uris, complete = await prts.fetch_file_data_uris(need, thumb_width=160)
        icons = _Icons(uris)
        av_uri = uris.get(av[0]) or uris.get(av[1]) or ''
        parts: list[str] = []
        if elite:
            parts.append(_sec(f'{len(parts) + 1:02d}', '精英化消耗', 'ELITE PROMOTION', _rows(elite, icons)))
        for g in groups:
            gi, gn = g['index'], g['name']
            parts.append(_sec(f'{len(parts) + 1:02d}', f'{gi}技能 · {gn}', 'MASTERY I ~ III', _rows(g.get('stages', []), icons)))
        for i, m in enumerate(mods, 1):
            rows = _rows(m.get('stages', []), icons)
            if is_mod:
                kv = []
                if m.get('stats'):
                    kv.append("<div class='tg' style='margin:0 0 9px'>" + ''.join(f"<span class='t a'>{_esc(s)}</span>" for s in m['stats']) + '</div>')
                for key, lbl in (('trait', 'Lv.1 特性'), ('talent2', 'Lv.2 天赋'), ('talent3', 'Lv.3 天赋')):
                    if m.get(key):
                        kv.append(f"<div class='kv'><span class='kt'>{lbl}</span>{_esc(m[key])}</div>")
                if m.get('tasks'):
                    kv.append(f"<div class='kv'><span class='kt b'>解锁任务</span>{_esc('；'.join(m['tasks']))}</div>")
                rows = ''.join(kv) + rows
            elif not m.get('stages'):
                continue
            mt = ' [' + m['type'] + ']' if m.get('type') else ''
            head = f'模组 {i} · ' if is_mod else '专属模组 · '
            parts.append(_sec(f'{len(parts) + 1:02d}', head + m['name'] + mt, 'MODULE Lv.1 ~ Lv.3', rows))
        if not parts:
            parts.append(_sec('--', '养成材料', 'NO DATA', "<div class='em'>" + ('该干员暂未实装专属模组。' if is_mod else '未匹配到对应阶段的养成材料数据。') + '</div>'))
        rc = len(elite) + sum(len(g.get('stages', [])) for g in groups) + sum(len(m.get('stages', [])) for m in mods)
        est = 280 + max(1, rc) * 112 + (len(mods) * 170 if is_mod else 0)
        body = _hdr(op, label, av_uri) + "<div class='bd'>" + ''.join(parts) + '</div>'
        return _doc(
            f'{name} 模组' if is_mod else f'{name} 材料', body, ac, soft, width, icons.css,
        ), width, est, complete

    # 干员总览 / 专三技能：半身像 + 白值 + 特性天赋 + 技能
    width = 680
    pt = [f'半身像_{name}_2.png', f'半身像_{name}_1.png']
    need += pt
    uris, complete = await prts.fetch_file_data_uris(need, thumb_width=320)
    av_uri = uris.get(av[0]) or uris.get(av[1]) or ''
    pt_uri = uris.get(pt[0]) or uris.get(pt[1])
    st = op.get('stats') or {}
    label = '专三技能档案' if sec in ('skills', 'skill', '技能') else '干员战术档案'
    boxes = ''.join(
        f"<div class='sb'><i>{k}</i><b>{_esc(st.get(f, '-'))}</b></div>"
        for k, f in (('生命上限', 'hp'), ('攻击', 'atk'), ('防御', 'def'), ('法术抗性', 'res'))
    )
    pills = ''.join(
        f"<span class='t'>{k} {_esc(st.get(f))}</span>"
        for k, f in (('费用', 'cost'), ('阻挡', 'block'), ('攻速', 'speed'), ('再部署', 'redeploy'))
        if st.get(f)
    )
    talents = ''.join(
        f"<div class='kv'><span class='kt'>{_esc(t['name'])}</span>{_esc(t['effect'])}</div>"
        for t in (op.get('talents') or [])
    )
    traits = _sec(
        '01', '特性与满级天赋', 'TRAIT & TALENTS',
        f"<div class='kv'><span class='kt b'>特性</span>{_esc(op.get('trait'))}</div>{talents}",
    )
    portrait = f"<div class='pt'><img src='{pt_uri}'/></div>" if pt_uri else ''
    top = (
        f"<div class='ov'>{portrait}"
        f"<div class='or'><div class='sg'>{boxes}</div><div class='tg' style='margin:0'>{pills}</div>{traits}</div></div>"
    )
    cards: list[str] = []
    for sk in op.get('skills') or []:
        sp = f"初{sk.get('init_sp', '-')} / 消{sk['cost_sp']}" if sk.get('cost_sp') else ''
        du = f"持{sk['duration']}" if sk.get('duration') else ''
        pl = ''.join(
            f"<span class='p {c}'>{_esc(v)}</span>"
            for c, v in (('g', sk.get('level')), ('', sk.get('recovery')), ('', sk.get('trigger')), ('s', sp), ('s', du))
            if v
        )
        cards.append(
            f"<div class='sk'><div class='sh'><span class='sn'>{_esc(sk.get('index'))}. {_esc(sk.get('name'))}</span>"
            f"<div class='sp'>{pl}</div></div><div class='sd'>{_esc(sk.get('desc'))}</div></div>"
        )
    skills = _sec('02', '核心技能（满级/专三数据）', 'SKILLS', ''.join(cards)) if cards else ''
    body = _hdr(op, label, av_uri) + "<div class='bd'>" + top + skills + '</div>'
    return _doc(f'{name} 档案', body, ac, soft, width), width, 480 + len(cards) * 110, complete


def build_help_card_html() -> tuple[str, int, int]:
    """构建帮助卡片 HTML，返回 (html, width, est_height)。"""
    ac, soft = '#F5A623', 'rgba(245,166,35,.14)'
    width = 660

    def box(title: str, tag: str, items: list[tuple[str, str, str]]) -> str:
        rows = ''.join(
            "<div class='hi'><span class='hc'>%s</span><div class='hy'>%s</div>%s</div>"
            % (_esc(c), _esc(d), f"<div class='he'>{_esc(e)}</div>" if e else '')
            for c, d, e in items
        )
        return f"<div class='hb'><div class='hh'><span>{_esc(title)}</span><s>{tag}</s></div>{rows}</div>"

    boxes = ''.join((
        box('干员资料 & 卡片图', 'WIKI', [
            ('/方舟 <干员/外号> [板块]', '板块可选：总览 / 技能 / 模组 / 材料 / 档案（别名 /prts /ark）', '例：/方舟 ew 模组　/方舟 叔叔 材料'),
            ('/方舟 总计 <干员> [范围]', '一键汇总精二、专三、模组养成材料总账', '例：/方舟 总计 维什戴尔 精二 三技能 模组'),
        ]),
        box('高清立绘 & 皮肤', 'ILLUST', [
            ('/立绘 <干员/外号> [版本]', '支持 精二 / 初始 / 皮肤1 / 皮肤中文名 / 列表（别名 /ak图）', '例：/立绘 水陈 列表　/立绘 42 皮肤1'),
            ('/方舟 语音 <干员> [关键词]', '查看干员中文台词彩蛋（戳一戳/信赖/交谈/问候/随机）', '例：/方舟 语音 叔叔 戳一戳'),
        ]),
        box('实用工具', 'TOOLS', [
            ('/公招 <词条1> <词条2> ...', '毫秒级算公招保底组合（支持高资/资深/机械/高台/地面等简写）', '例：/公招 资深 狙击 削弱 爆发 生存'),
            ('/方舟 活动', '速查国服当前进行中的活动、卡池与剩余时间', ''),
        ]),
        box('黑话与缓存管理', 'ADMIN', [
            ('/方舟 别名 [列表|添加|删除]', '查看或热增删群内自定义黑话外号（增删限管理员）', '例：/方舟 别名 添加 牢猫 凯尔希'),
            ('/方舟 缓存　/方舟 清理缓存', '查看本地缓存占用，或由管理员一键清空缓存', ''),
        ]),
    ))
    hdr = (
        "<div class='hd' data-w='PRTS'><div class='hm'><div class='nm'>"
        "<span class='on'>明日方舟wiki插件 · 指令速查</span></div>"
        "<div class='en' style='margin-top:4px'>Rhodes Island Terminal // PRTS Wiki &amp; Recruitment</div></div>"
        "<div class='hr'><b>PRTS // TERMINAL</b><span>COMMAND GUIDE</span></div></div><div class='hz'></div>"
    )
    tip = (
        "<div class='tp'>"
        + _esc('日常聊天也无需打指令：直接问「ew 模组开哪个」「玛恩纳精二要什么材料」「看看水陈皮肤」即可自动响应；'
               '发 /方舟帮助 可随时呼出本速查图。数据来源 PRTS Wiki (CC BY-NC-SA 4.0)。')
        + '</div>'
    )
    body = hdr + "<div class='bd'><div class='hg'>" + boxes + '</div>' + tip + '</div>'
    return _doc('明日方舟wiki插件 帮助', body, ac, soft, width), width, 700
