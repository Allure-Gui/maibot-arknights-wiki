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
        sp_init = f"初 {sk['init_sp']}" if sk.get('init_sp') not in (None, '', '-') else ''
        sp_cost = f"消 {sk['cost_sp']}" if sk.get('cost_sp') else ''
        du = f"持 {sk['duration']}" if sk.get('duration') else ''
        pl = ''.join(
            f"<span class='p {c}'>{_esc(v)}</span>"
            for c, v in (
                ('g', sk.get('level')),
                ('', sk.get('recovery')),
                ('', sk.get('trigger')),
                ('s', sp_init),
                ('s', sp_cost),
                ('s', du),
            )
            if v
        )
        cards.append(
            f"<div class='sk'><div class='sh'><span class='sn'>{_esc(sk.get('index'))}. {_esc(sk.get('name'))}</span>"
            f"<div class='sp'>{pl}</div></div><div class='sd'>{_esc(sk.get('desc'))}</div></div>"
        )
    skills = _sec('02', '核心技能 · 满级专三', 'SKILLS', ''.join(cards)) if cards else ''
    body = _hdr(op, label, av_uri) + "<div class='bd'>" + top + skills + '</div>'
    return _doc(f'{name} 档案', body, ac, soft, width), width, 480 + len(cards) * 110, complete


def build_help_card_html() -> tuple[str, int, int]:
    """构建帮助卡片 HTML，返回 (html, width, est_height)。"""
    ac, soft = '#F5A623', 'rgba(245,166,35,.14)'
    width = 720

    def hero(num: str, title: str, color: str, cmd: str, arg: str, desc_html: str, ex: str) -> str:
        arg_html = f"<u>{_esc(arg)}</u>" if arg else ""
        return (
            f"<div class='hf' style='--fc:{color}'>"
            f"<div class='hf-t'><b>{_esc(title)}</b><i>{num}</i></div>"
            f"<div class='hc'>{_esc(cmd)}{arg_html}</div>"
            f"<div class='hf-d'>{desc_html}</div>"
            f"<div class='hf-e'>{_esc(ex)}</div>"
            f"</div>"
        )

    heroes = "<div class='h3'>" + "".join((
        hero(
            "01", "干员档案", "#F5A623",
            "/方舟", "干员名 [板块]",
            "支持 <b>总览 · 技能 · 模组 · 材料</b> 与外号，一键出卡片图。",
            "例：/方舟 ew 模组",
        ),
        hero(
            "02", "公开招募", "#7FB8D6",
            "/公招", "词条 或 附图",
            "算保底出半身像卡，支持手打词条、<b>带图</b>或<b>回复截图</b>识图。",
            "例：/公招 资深 狙击",
        ),
        hero(
            "03", "敌方档案", "#E05A5A",
            "/方舟 敌人", "名称 [级别]",
            "查 <b>BOSS 与敌人</b> 血攻防抗、控制免疫、多档级别与机制。",
            "例：/方舟 敌人 爱国者",
        ),
    )) + "</div>"

    def section_box(title: str, tag: str, rows_data: list[tuple[str, str, str]]) -> str:
        rows = "".join(
            f"<div class='hl'><span class='hl-c'>{_esc(cmd)}"
            f"{f'<u>{_esc(arg)}</u>' if arg else ''}</span>"
            f"<span class='hl-d'>{_esc(desc)}</span></div>"
            for cmd, arg, desc in rows_data
        )
        return f"<div class='hb'><div class='hh'><span>{_esc(title)}</span><s>{tag}</s></div>{rows}</div>"

    boxes = "<div class='hg'>" + "".join((
        section_box("情报与图鉴", "ARCHIVE", [
            ("/立绘", "干员 [版本]", "精二 · 初始 · 皮肤 · 列表"),
            ("/方舟 总计", "干员 [范围]", "精二专三模组材料一键总账"),
            ("/方舟 语音", "干员 [词]", "台词彩蛋（仅文字无语音）"),
            ("/方舟 卡池", "· 活动", "当期寻访与活动海报图"),
        ]),
        section_box("终端与管理", "SYSTEM", [
            ("/方舟 别名", "[增删]", "查看或热增删黑话（限管理）"),
            ("/方舟 缓存", "", "查看本地词条与图片缓存占用"),
            ("/方舟 清理缓存", "", "一键清空本地缓存（限管理）"),
            ("/方舟帮助", "", "随时呼出本终端指令手册"),
        ]),
    )) + "</div>"

    hdr = (
        "<div class='hd' data-w='PRTS'><div class='hm'><div class='nm'>"
        "<span class='on'>明日方舟wiki · 指令手册</span></div>"
        "<div class='en' style='margin-top:4px'>Rhodes Island Terminal // PRTS Wiki &amp; Tactical Index</div></div>"
        "<div class='hr'><b>PRTS // TERMINAL</b><span>COMMAND MANUAL</span></div></div><div class='hz'></div>"
    )
    tip = (
        "<div class='tp'>"
        "<span class='tp-b'>日常直聊免指令</span>"
        "<div class='tp-q'>"
        "<span>ew 模组开哪个</span>"
        "<span>玛恩纳精二材料</span>"
        "<span>看看水陈皮肤</span>"
        "</div>"
        "<div class='tp-s'>群聊直接提及即可自动响应 · 简写指令 /prts、/ark、/ak图 · 敌人别名 boss</div>"
        "</div>"
    )
    body = hdr + "<div class='bd'>" + heroes + boxes + tip + "</div>"
    return _doc('明日方舟wiki插件 帮助', body, ac, soft, width), width, 600


async def build_recruit_card_html(
    prts: PRTSClient,
    data: dict[str, Any],
) -> tuple[str, int, int, bool]:
    """构建黑曜石金箔风格公开招募卡片 HTML（120px 半身像 + CSS class 去重复用），返回 (html, width, est_height, complete)。"""
    norm_tags: list[str] = list(data.get("tags") or [])
    combos: list[dict[str, Any]] = list(data.get("combos") or [])

    # 收集去重干员半身像文件名，同一干员在多个组合里出现只拉一次、只嵌一份 Base64
    op_names = list(dict.fromkeys(
        op["name"] for c in combos for op in (c.get("operators") or []) if op.get("name")
    ))
    need_files = [f"半身像_{n}_1.png" for n in op_names]
    uris, complete = await prts.fetch_file_data_uris(need_files, thumb_width=120)

    op_cls_map: dict[str, str] = {}
    css_rules: list[str] = []
    for n in op_names:
        uri = uris.get(f"半身像_{n}_1.png") or ""
        if uri:
            cls = f"o{len(op_cls_map)}"
            op_cls_map[n] = cls
            css_rules.append(f".{cls}{{background-image:url({uri})}}")

    max_ops = max((len(c.get("operators") or []) for c in combos), default=1)
    width = 860 if max_ops > 10 else 720
    inner_w = width - 68
    per_row = 12 if width >= 860 else 10

    top_score = combos[0]["score"] if combos else 40
    top_rarity = 6 if top_score >= 60 else (5 if top_score >= 50 else (1 if top_score == 45 else 4))
    ac, soft = _PALETTE.get(top_rarity, _PALETTE[5])

    # 头部：输入词条胶囊 + 招募时长提醒
    tag_pills = "".join(f"<span class='t a'>{_esc(t)}</span>" for t in norm_tags)
    tips_badges: list[str] = []
    if any(t in norm_tags for t in ("高级资深干员", "资深干员")):
        tips_badges.append("<span class='t b'>建议拉满 9:00</span>")
    if "支援机械" in norm_tags or any(c.get("is_robot") for c in combos):
        tips_badges.append("<span class='t b'>小车请设 3:50</span>")

    hdr = (
        f"<div class='hd' data-w='RECRUIT'><div class='hm'>"
        f"<div class='nm'><span class='on'>公开招募 · 保底组合</span>"
        f"<span class='en'>Public Recruitment</span></div>"
        f"<div class='tg'>{tag_pills}{''.join(tips_badges)}</div></div>"
        f"<div class='hr'><b>PRTS // RECRUIT</b><span>共 {len(combos)} 组保底</span></div></div>"
        f"<div class='hz'></div>"
    )

    def _render_combo_box(c: dict[str, Any], is_wide: bool, grow: int = 1) -> str:
        ops = c.get("operators") or []
        is_robot = bool(c.get("is_robot"))
        min_r = int(c.get("min_rarity") or 4)
        max_r = max((int(o.get("rarity") or min_r) for o in ops), default=min_r)
        rc = _PALETTE.get(1 if is_robot else min_r, _PALETTE[4])[0]
        if is_robot:
            badge = "1★车"
        elif max_r > min_r:
            badge = f"{min_r}-{max_r}★"
        else:
            badge = f"{min_r}★"
        tag_html = "".join(f"<span class='rt'>{_esc(t)}</span>" for t in (c.get("tags") or []))
        def _render_op(o: dict[str, Any]) -> str:
            oname = o["name"]
            orarity = max(1, min(6, int(o.get("rarity") or 4)))
            oc = _PALETTE.get(orarity, _PALETTE[4])[0]
            cls = op_cls_map.get(oname)
            face = f"<i class='{cls}'></i>" if cls else f"<b>{_esc(oname[:3])}</b>"
            return (
                f"<div class='ro'><div class='rp' style='--oc:{oc}'>{face}<em>{orarity}★</em></div>"
                f"<div class='rn'>{_esc(oname)}</div></div>"
            )

        # 按星级降序分组；若包含多个星级，则左右分成独立子区（如左侧 5★、右侧 4★），各自带星级标题
        rarity_groups: dict[int, list[dict[str, Any]]] = {}
        for o in ops:
            r_val = max(1, min(6, int(o.get("rarity") or 4)))
            rarity_groups.setdefault(r_val, []).append(o)

        if len(rarity_groups) > 1:
            sub_htmls: list[str] = []
            for r_val in sorted(rarity_groups.keys(), reverse=True):
                g_ops = rarity_groups[r_val]
                sc_col = _PALETTE.get(r_val, _PALETTE[4])[0]
                sub_cards = "".join(_render_op(o) for o in g_ops)
                sub_htmls.append(
                    f"<div class='rsub' style='--sc:{sc_col}'>"
                    f"<div class='rl'>{sub_cards}</div></div>"
                )
            content_html = f"<div class='rsubs'>{''.join(sub_htmls)}</div>"
        else:
            content_html = f"<div class='rl'>{''.join(_render_op(o) for o in ops)}</div>"

        return (
            f"<div class='rb{' wide' if is_wide else ''}' style='--rc:{rc}'>"
            f"<div class='rh'>{tag_html}</div>"
            f"{content_html}</div>"
        )

    # 保持 6★ > 5★ > 4★ > 1★ 严格顺序：小组合紧贴内容自然宽度流式紧密排布（零框内黑洞），大组合（>8人）独占整行
    box_htmls: list[str] = []
    est_rows = 0
    for c in combos:
        ops_n = len(c.get("operators") or [])
        is_wide = ops_n > 8
        box_htmls.append(_render_combo_box(c, is_wide))
        if is_wide:
            est_rows += 1 + (ops_n + per_row - 1) // per_row
        else:
            est_rows += 1

    body = hdr + "<div class='bd'><div class='rg'>" + "".join(box_htmls) + "</div></div>"
    est_h = max(360, 190 + est_rows * 140)
    return _doc("公开招募组合", body, ac, soft, width, "".join(css_rules)), width, est_h, complete


async def build_enemy_card_html(
    prts: PRTSClient,
    en: dict[str, Any],
) -> tuple[str, int, int, bool]:
    """构建黑曜石风格 BOSS / 敌人数值与机制速查卡片（完全复用 assets/card.css）。"""
    name = en["name"]
    rank = str(en.get("rank") or "普通")
    if "领袖" in rank:
        ac, soft = "#E55353", "rgba(229,83,83,.14)"
    elif "精英" in rank:
        ac, soft = "#F2A24E", "rgba(242,162,78,.13)"
    else:
        ac, soft = "#74BCEA", "rgba(116,188,234,.13)"

    av_candidates = [f"头像_敌人_{name}.png", f"头像_敌人_{en.get('title','')}.png"]
    uris, complete = await prts.fetch_file_data_uris(av_candidates, thumb_width=160)
    av_uri = uris.get(av_candidates[0]) or uris.get(av_candidates[1]) or ""
    av = f"<img src='{av_uri}'/>" if av_uri else f"<i>{_esc(name[:1])}</i>"

    tags: list[str] = []
    if rank:
        tags.append(f"<span class='t a'>{_esc(rank)}</span>")
    for k in ("race", "damage_type", "attack_mode", "move_mode"):
        if en.get(k):
            tags.append(f"<span class='t'>{_esc(en[k])}</span>")
    if en.get("event"):
        tags.append(f"<span class='t b'>{_esc(en['event'])}</span>")

    lv_cnt = int(en.get("level_count") or 1)
    lv_label = f"级别 {en.get('level_index', 0)}" + (f" · 可选 0~{lv_cnt - 1}" if lv_cnt > 1 else "")
    wm = _esc(str(en.get("index") or "HOSTILE").upper())
    hdr = (
        f"<div class='hd' data-w='{wm}'><div class='av'>{av}</div>"
        f"<div class='hm'><div class='nm'><span class='on'>{_esc(name)}</span>"
        f"<span class='en'>{_esc(en.get('index') or 'HOSTILE UNIT')}</span></div>"
        f"<div class='tg'>{''.join(tags)}</div></div>"
        f"<div class='hr'><b>PRTS // HOSTILE</b><span>{_esc(lv_label)}</span></div></div><div class='hz'></div>"
    )

    st = en.get("stats") or {}
    boxes = "".join(
        f"<div class='sb'><i>{k}</i><b>{_esc(st.get(f, '-'))}</b></div>"
        for k, f in (("生命上限", "hp"), ("攻击力", "atk"), ("防御力", "def"), ("法术抗性", "res"))
    )
    sub_pills = "".join(
        f"<span class='t'>{k} {_esc(st.get(f))}{u}</span>"
        for k, f, u in (
            ("攻击间隔", "interval", "s"),
            ("移速", "speed", ""),
            ("重量", "weight", ""),
            ("目标生命消耗", "life_point", ""),
            ("元素抗性", "elem_res", ""),
        )
        if st.get(f) and str(st.get(f)) != "0"
    )
    imm_list = en.get("immunities") or []
    imm_html = (
        "".join(f"<span class='t a'>免疫 {_esc(x)}</span>" for x in imm_list)
        if imm_list
        else "<span class='t'>无常规控制免疫</span>"
    )

    parts: list[str] = [
        _sec(
            "01", "基础数值与抗性", "ATTRIBUTES & IMMUNITIES",
            f"<div class='sg'>{boxes}</div>"
            f"<div class='tg' style='margin-top:10px'>{sub_pills}</div>"
            f"<div class='tg' style='margin-top:8px'>{imm_html}</div>",
        )
    ]

    if en.get("ability"):
        ab_lines = [ln.strip().lstrip("·•- ").strip() for ln in en["ability"].splitlines() if ln.strip()]
        ab_html = "".join(f"<div class='kv'>· {_esc(ln)}</div>" for ln in ab_lines[:10])
        parts.append(_sec("02", "核心特性与姿态", "MECHANICS", ab_html))

    sk_cards: list[str] = []
    for sk in (en.get("skills") or [])[:5]:
        sp_pills = []
        if sk.get("init_sp") and sk["init_sp"] != "9999":
            sp_pills.append(f"<span class='p s'>初 {sk['init_sp']}</span>")
        if sk.get("cost_sp") and sk["cost_sp"] != "9999":
            sp_pills.append(f"<span class='p s'>消 {sk['cost_sp']}</span>")
        sk_cards.append(
            f"<div class='sk'><div class='sh'><span class='sn'>{_esc(sk['name'])}</span>"
            f"<div class='sp'>{''.join(sp_pills)}</div></div>"
            f"<div class='sd'>{_esc(sk['effect'])}</div></div>"
        )
    if sk_cards:
        parts.append(_sec(f"{len(parts) + 1:02d}", "技能机制详情", "SKILLS", "".join(sk_cards)))

    width = 680
    est_h = 340 + len(sk_cards) * 105 + (180 if en.get("ability") else 0)
    body = hdr + "<div class='bd'>" + "".join(parts) + "</div>"
    return _doc(f"{name} 敌方档案", body, ac, soft, width), width, est_h, complete


async def build_events_card_html(
    prts: PRTSClient,
    data: dict[str, Any],
    *,
    focus: str = "events",
) -> tuple[str, int, int, bool]:
    """构建黑曜石金箔风格「当期寻访卡池 / 当期活动」官方横幅海报卡片，返回 (html, width, est_height, complete)。"""
    gachas = [] if focus == "events" else list((data.get("gachas") or [])[:6])
    events = [] if focus == "gacha" else list((data.get("events") or [])[:6])
    stamp = str(data.get("updated_at") or "")

    timed_events = [e for e in events if not e.get("is_long")]
    long_events = [e for e in events if e.get("is_long")]

    need_files: list[str] = []
    for item in gachas + timed_events + long_events:
        b = str(item.get("banner") or "").strip()
        if b:
            need_files.append(b)
    need_files = list(dict.fromkeys(need_files))

    uris: dict[str, str] = {}
    complete = True
    if need_files:
        uris, complete = await prts.fetch_file_data_uris(need_files, thumb_width=640)

    ac, soft = "#F2A24E", "rgba(242,162,78,.15)"
    width = 780

    status_label = {
        "live": "进行中",
        "urgent": "即将结束",
        "soon": "即将开启",
        "long": "长期常驻",
    }

    def _poster(it: dict[str, Any], *, feat: bool = False) -> str:
        st = str(it.get("status") or "live")
        uri = uris.get(str(it.get("banner") or "")) or ""
        img = f"<img src='{uri}'/>" if uri else f"<div class='eb-f'>{_esc(it.get('name') or 'PRTS')}</div>"
        remain = str(it.get("remain") or "")
        period = str(it.get("period") or "")
        pct = int(it.get("pct") or 0)
        bar = (
            f"<div class='ep{' urgent' if st == 'urgent' else ''}'><i style='width:{pct}%'></i></div>"
            if pct > 0 else ""
        )
        return (
            f"<div class='ec{' feat' if feat else ''}'>"
            f"<div class='eb'>{img}"
            f"<span class='ef {st}'>{status_label.get(st, '进行中')}</span>"
            f"<span class='er{' urgent' if st == 'urgent' else ''}'>{_esc(remain)}</span>"
            f"</div>"
            f"<div class='ei'>"
            f"<div class='en-t'><span class='en-n'>{_esc(it.get('name') or '')}</span>"
            f"{f'<span class=\"en-p\">{_esc(period)}</span>' if period else ''}</div>"
            f"{bar}</div></div>"
        )

    sections: list[str] = []
    est_h = 180

    if gachas:
        one_cls = " one" if len(gachas) == 1 else ""
        # 奇数个且超过 1 个时，首张做通栏主视觉大海报，其余两列工整对齐
        cards_html = "".join(
            _poster(g, feat=(len(gachas) % 2 == 1 and idx == 0))
            for idx, g in enumerate(gachas)
        )
        sections.append(_sec("01", "当期寻访卡池", "HEADHUNTING", f"<div class='eg{one_cls}'>{cards_html}</div>"))
        rows_g = 1 if len(gachas) == 1 else (len(gachas) + 1) // 2
        est_h += 70 + rows_g * 225

    if timed_events or long_events:
        ev_parts: list[str] = []
        if timed_events:
            one_cls = " one" if len(timed_events) == 1 else ""
            ev_cards = "".join(
                _poster(e, feat=(len(timed_events) % 2 == 1 and idx == 0))
                for idx, e in enumerate(timed_events)
            )
            ev_parts.append(f"<div class='eg{one_cls}'>{ev_cards}</div>")
            rows_e = 1 if len(timed_events) == 1 else (len(timed_events) + 1) // 2
            est_h += rows_e * 225
        if long_events:
            long_cards: list[str] = []
            for e in long_events:
                uri = uris.get(str(e.get("banner") or "")) or ""
                th = f"<div class='em-th'><img src='{uri}'/></div>" if uri else ""
                sub = str(e.get("period") or e.get("remain") or "长期常驻")
                long_cards.append(
                    f"<div class='em-c'>{th}<div class='em-m'>"
                    f"<div class='em-n'>{_esc(e.get('name') or '')}</div>"
                    f"<div class='em-s'>{_esc(sub)}</div></div></div>"
                )
            el_cls = " one" if len(long_events) == 1 else ""
            ev_parts.append(f"<div class='el{el_cls}'>{''.join(long_cards)}</div>")
            est_h += ((len(long_events) + 1) // 2) * 76
        sec_idx = f"{len(sections) + 1:02d}"
        sections.append(_sec(sec_idx, "当期活动与常驻", "OPERATIONS", "".join(ev_parts)))
        est_h += 70

    if focus == "gacha":
        title_cn = "明日方舟国服 · 当期寻访"
        title_en = "Headhunting Schedule // CN Server"
        badge = f"共 {len(gachas)} 个卡池"
        watermark = "GACHA"
    elif focus == "events":
        title_cn = "明日方舟国服 · 当期活动"
        title_en = "Operations Schedule // CN Server"
        badge = f"共 {len(events)} 项活动"
        watermark = "EVENTS"
    else:
        title_cn = "明日方舟国服 · 活动与卡池"
        title_en = "Operations & Headhunting // CN Server"
        badge = f"{len(gachas)} 卡池 · {len(events)} 活动"
        watermark = "EVENTS"

    hdr = (
        f"<div class='hd' data-w='{watermark}'><div class='hm'>"
        f"<div class='nm'><span class='on'>{_esc(title_cn)}</span></div>"
        f"<div class='en' style='margin-top:4px'>{title_en}"
        f"{f' · {stamp}' if stamp else ''}</div></div>"
        f"<div class='hr'><b>PRTS // SCHEDULE</b><span>{_esc(badge)}</span></div></div>"
        f"<div class='hz'></div>"
    )
    body = hdr + "<div class='bd'>" + "".join(sections) + "</div>"
    return _doc(title_cn, body, ac, soft, width), width, max(380, est_h), complete


async def build_material_summary_card_html(
    prts: PRTSClient,
    data: dict[str, Any],
) -> tuple[str, int, int, bool]:
    """构建黑曜石金箔风格「养成材料总账」卡片（真实带框材料图标 + 范围胶囊 + 龙门币置顶），返回 (html, width, est_height, complete)。"""
    op: dict[str, Any] = data["op"]
    included: list[str] = list(data.get("included") or [])
    lmb: int = int(data.get("lmb") or 0)
    items: list[tuple[str, int]] = list(data.get("items") or [])

    ac, soft = _PALETTE.get(_stars(op), _PALETTE[6])
    name = op["name"]
    av = [f"头像_{name}_2.png", f"头像_{name}.png"]

    # 龙门币置顶进图标网格（格式化为 18w / 24.5w），其余按数量降序
    display_items: list[tuple[str, str]] = []
    if lmb > 0:
        if lmb >= 10000:
            w_val = lmb / 10000
            lmb_lbl = f"{int(w_val)}w" if lmb % 10000 == 0 else f"{w_val:.1f}w"
        else:
            lmb_lbl = str(lmb)
        display_items.append(("龙门币", lmb_lbl))
    for mat_name, cnt in items:
        display_items.append((mat_name, str(cnt)))

    need = list(av) + list(dict.fromkeys(f"道具_带框_{m}.png" for m, _ in display_items))
    uris, complete = await prts.fetch_file_data_uris(need, thumb_width=160)
    icons = _Icons(uris)
    av_uri = uris.get(av[0]) or uris.get(av[1]) or ""

    scope_pills = "".join(f"<span class='t b'>{_esc(x)}</span>" for x in included)
    if lmb > 0:
        scope_pills += f"<span class='t a'>龙门币 {lmb:,}</span>"
    scope_bar = f"<div class='tg' style='margin:0 0 12px'>{scope_pills}</div>" if scope_pills else ""

    mat_grid = _mats(display_items, icons)
    sec_hint = f"TOTAL · {len(display_items)} ITEMS"
    sec = _sec("01", "养成素材合计", sec_hint, scope_bar + mat_grid)

    tip = (
        "<div class='tp' style='margin-top:10px;padding:8px 12px'>"
        "<span class='tp-b'>范围筛选</span>"
        "<span style='font-size:11px;color:#aeb7c6'>支持按阶段自由组合 · 例：/方舟 总计 维什戴尔 精二 三技能 模组</span>"
        "</div>"
    )
    width = 660
    rows_n = max(1, (len(display_items) + 7) // 8)
    est_h = 300 + rows_n * 102
    body = _hdr(op, "养成材料总账", av_uri) + "<div class='bd'>" + sec + tip + "</div>"
    return _doc(f"{name} 养成材料总账", body, ac, soft, width, icons.css), width, est_h, complete


async def build_voice_card_html(
    prts: PRTSClient,
    data: dict[str, Any],
) -> tuple[str, int, int, bool]:
    """构建黑曜石金箔风格「干员语音记录」卡片（纯文字台词彩蛋），返回 (html, width, est_height, complete)。"""
    op: dict[str, Any] = data["op"]
    entries: list[tuple[str, str]] = list(data.get("entries") or [])
    kw = str(data.get("keyword") or "").strip()

    ac, soft = _PALETTE.get(_stars(op), _PALETTE[6])
    name = op["name"]
    av = [f"头像_{name}_2.png", f"头像_{name}.png"]
    uris, complete = await prts.fetch_file_data_uris(av, thumb_width=160)
    av_uri = uris.get(av[0]) or uris.get(av[1]) or ""

    cards_html = "".join(
        f"<div class='sk'>"
        f"<div class='sh'><span class='sn'>{_esc(lbl)}</span>"
        f"<div class='sp'><span class='p g'>中文台词</span></div></div>"
        f"<div class='sd'>「{_esc(txt)}」</div></div>"
        for lbl, txt in entries
    )
    sec_title = f"语音记录 · {kw}" if kw else "精选语音台词"
    sec = _sec("01", sec_title, f"VOICE LINES · {len(entries)}", cards_html)
    tip = (
        "<div class='tp' style='margin-top:10px;padding:8px 12px'>"
        "<span class='tp-b'>仅文字无语音</span>"
        "<span style='font-size:11px;color:#aeb7c6'>支持关键词：戳一戳 · 信赖 · 交谈 · 问候 · 报到 · 随机</span>"
        "</div>"
    )
    width = 660
    est_h = 260 + sum(95 + (len(t) // 36) * 20 for _, t in entries)
    body = _hdr(op, "干员语音记录", av_uri) + "<div class='bd'>" + sec + tip + "</div>"
    return _doc(f"{name} 语音记录", body, ac, soft, width), width, max(340, est_h), complete


async def build_lore_card_html(
    prts: PRTSClient,
    data: dict[str, Any],
) -> tuple[str, int, int, bool]:
    """构建黑曜石金箔风格「干员机密档案」卡片，返回 (html, width, est_height, complete)。"""
    op: dict[str, Any] = data["op"]
    profile: list[tuple[str, str]] = list(data.get("profile") or [])
    entries: list[tuple[str, str]] = list(data.get("entries") or [])

    ac, soft = _PALETTE.get(_stars(op), _PALETTE[6])
    name = op["name"]
    av = [f"头像_{name}_2.png", f"头像_{name}.png"]
    uris, complete = await prts.fetch_file_data_uris(av, thumb_width=160)
    av_uri = uris.get(av[0]) or uris.get(av[1]) or ""

    parts: list[str] = []
    if profile:
        pills = "".join(
            f"<span class='t{' a' if k == '感染情况' else ''}'>{_esc(k)} · {_esc(v)}</span>"
            for k, v in profile
        )
        parts.append(_sec("01", "基础履历", "PROFILE", f"<div class='tg' style='margin:0'>{pills}</div>"))

    story_cards: list[str] = []
    for heading, raw_body in entries:
        trimmed = raw_body if len(raw_body) <= 280 else raw_body[:276].rstrip() + "…"
        story_cards.append(
            f"<div class='sk'>"
            f"<div class='sh'><span class='sn'>{_esc(heading)}</span></div>"
            f"<div class='sd'>{_esc(trimmed)}</div></div>"
        )
    if story_cards:
        idx = f"{len(parts) + 1:02d}"
        parts.append(_sec(idx, "机密档案与记录", "ARCHIVE FILES", "".join(story_cards)))

    width = 680
    est_h = 260 + (90 if profile else 0) + sum(100 + (min(len(b), 280) // 36) * 20 for _, b in entries)
    body = _hdr(op, "干员机密档案", av_uri) + "<div class='bd'>" + "".join(parts) + "</div>"
    return _doc(f"{name} 机密档案", body, ac, soft, width), width, max(360, est_h), complete
