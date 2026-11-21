"""明日方舟wiki插件：罗德岛工业风战术卡片 HTML 渲染器。

使用 MaiBot 宿主原生 render.html2png（2x Retina, allow_network=False）离线下发高质量图片，
所有干员头像、半身像与带框材料图标均通过 PRTS 批量接口礼貌拉取并缓存为 Base64 Data URI 内嵌。
"""

from __future__ import annotations

import html
from typing import Any

from .prts import PRTSClient

_STAR_PALETTE: dict[int, tuple[str, str]] = {
    6: ("#FF9838", "rgba(255, 152, 56, 0.16)"),
    5: ("#F4C850", "rgba(244, 200, 80, 0.15)"),
    4: ("#B48EFF", "rgba(180, 142, 255, 0.15)"),
    3: ("#38BDF8", "rgba(56, 189, 248, 0.15)"),
    2: ("#A3E635", "rgba(163, 230, 53, 0.15)"),
    1: ("#94A3B8", "rgba(148, 163, 184, 0.15)"),
}


def _esc(val: Any) -> str:
    return html.escape(str(val or ""), quote=True)


def _base_css(accent: str, accent_soft: str, width: int) -> str:
    return f"""
    * {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{
      background: transparent;
      font-family: "Microsoft YaHei", "PingFang SC", "Noto Sans CJK SC", "Segoe UI", sans-serif;
      color: #E6ECF5;
      -webkit-font-smoothing: antialiased;
    }}
    .card {{
      width: {width}px;
      background:
        radial-gradient(circle at 92% 6%, {accent_soft}, transparent 38%),
        radial-gradient(circle at 8% 95%, rgba(35, 173, 229, 0.10), transparent 36%),
        linear-gradient(160deg, #12151C 0%, #171B24 55%, #11141A 100%);
      border: 1px solid rgba(255, 255, 255, 0.10);
      border-top: 3px solid {accent};
      border-radius: 14px;
      padding: 20px 22px 16px;
      position: relative;
      overflow: hidden;
    }}
    .card::before {{
      content: "";
      position: absolute;
      inset: 0;
      background-image: radial-gradient(rgba(255, 255, 255, 0.04) 1px, transparent 1px);
      background-size: 16px 16px;
      pointer-events: none;
    }}
    .inner {{ position: relative; z-index: 1; }}
    .hdr {{
      display: flex;
      align-items: center;
      gap: 14px;
      padding-bottom: 14px;
      border-bottom: 1px solid rgba(255, 255, 255, 0.09);
      margin-bottom: 14px;
    }}
    .avatar-wrap {{
      width: 66px;
      height: 66px;
      border-radius: 10px;
      border: 1.5px solid {accent};
      background: #0D1016;
      overflow: hidden;
      flex-shrink: 0;
      box-shadow: 0 4px 14px rgba(0, 0, 0, 0.45);
    }}
    .avatar-wrap img {{ width: 100%; height: 100%; object-fit: cover; display: block; }}
    .avatar-fallback {{
      width: 100%; height: 100%;
      display: flex; align-items: center; justify-content: center;
      font-size: 26px; font-weight: 800; color: {accent};
    }}
    .hdr-meta {{ flex: 1; min-width: 0; }}
    .name-row {{ display: flex; align-items: baseline; gap: 10px; flex-wrap: wrap; }}
    .op-name {{ font-size: 24px; font-weight: 800; color: #FFFFFF; letter-spacing: 0.5px; }}
    .op-en {{ font-size: 12px; color: #8B97AD; letter-spacing: 0.8px; text-transform: uppercase; font-weight: 600; }}
    .stars {{ color: {accent}; font-size: 14px; letter-spacing: 2px; margin-top: 2px; }}
    .badges {{ display: flex; gap: 6px; flex-wrap: wrap; margin-top: 6px; }}
    .badge {{
      font-size: 11px;
      font-weight: 600;
      padding: 2px 8px;
      border-radius: 4px;
      background: rgba(255, 255, 255, 0.07);
      border: 1px solid rgba(255, 255, 255, 0.10);
      color: #CBD5E1;
    }}
    .badge-accent {{
      background: {accent_soft};
      border-color: {accent};
      color: {accent};
    }}
    .badge-blue {{
      background: rgba(35, 173, 229, 0.14);
      border-color: rgba(35, 173, 229, 0.45);
      color: #67D5FF;
    }}
    .tag-corner {{
      text-align: right;
      flex-shrink: 0;
    }}
    .tag-corner .code {{
      font-size: 10px;
      font-weight: 700;
      color: #23ADE5;
      letter-spacing: 1.4px;
    }}
    .tag-corner .sub {{
      font-size: 12px;
      font-weight: 700;
      color: #E2E8F0;
      margin-top: 3px;
      padding: 2px 8px;
      background: rgba(35, 173, 229, 0.16);
      border-left: 2.5px solid #23ADE5;
      border-radius: 2px;
    }}
    .sec {{
      background: rgba(255, 255, 255, 0.032);
      border: 1px solid rgba(255, 255, 255, 0.075);
      border-radius: 10px;
      padding: 12px 14px;
      margin-bottom: 12px;
    }}
    .sec-title {{
      display: flex;
      align-items: center;
      justify-content: space-between;
      font-size: 13px;
      font-weight: 700;
      color: #F8FAFC;
      margin-bottom: 10px;
      padding-left: 8px;
      border-left: 3px solid {accent};
    }}
    .sec-title .hint {{ font-size: 11px; font-weight: 500; color: #8B97AD; }}
    .stage-row {{
      display: flex;
      align-items: flex-start;
      gap: 12px;
      padding: 8px 0;
      border-top: 1px dashed rgba(255, 255, 255, 0.08);
    }}
    .stage-row:first-of-type {{ border-top: none; padding-top: 2px; }}
    .stage-label {{
      width: 58px;
      flex-shrink: 0;
      font-size: 12px;
      font-weight: 800;
      color: #23ADE5;
      background: rgba(35, 173, 229, 0.12);
      border: 1px solid rgba(35, 173, 229, 0.30);
      border-radius: 5px;
      text-align: center;
      padding: 5px 4px;
      margin-top: 10px;
    }}
    .mat-grid {{
      display: flex;
      flex-wrap: wrap;
      gap: 10px 12px;
      flex: 1;
    }}
    .mat-item {{
      width: 68px;
      display: flex;
      flex-direction: column;
      align-items: center;
      text-align: center;
    }}
    .mat-icon-box {{
      width: 56px;
      height: 56px;
      position: relative;
      border-radius: 8px;
      background: rgba(10, 13, 18, 0.65);
      border: 1px solid rgba(255, 255, 255, 0.08);
      display: flex;
      align-items: center;
      justify-content: center;
      overflow: hidden;
    }}
    .mat-icon-box img {{ width: 54px; height: 54px; object-fit: contain; display: block; }}
    .mat-icon-text {{ font-size: 11px; color: #94A3B8; padding: 4px; line-height: 1.2; }}
    .mat-cnt {{
      position: absolute;
      right: 2px;
      bottom: 2px;
      background: rgba(10, 12, 16, 0.88);
      color: #FFFFFF;
      font-size: 11px;
      font-weight: 800;
      padding: 1px 5px;
      border-radius: 4px;
      border: 1px solid rgba(255, 255, 255, 0.18);
      line-height: 1.2;
    }}
    .mat-name {{
      font-size: 10.5px;
      color: #CBD5E1;
      margin-top: 4px;
      line-height: 1.25;
      word-break: break-all;
    }}
    .kv-block {{
      font-size: 12.5px;
      line-height: 1.55;
      color: #DCE4F0;
      margin-bottom: 7px;
    }}
    .kv-block:last-child {{ margin-bottom: 0; }}
    .kv-tag {{
      display: inline-block;
      font-size: 11px;
      font-weight: 700;
      color: {accent};
      background: {accent_soft};
      padding: 1px 6px;
      border-radius: 4px;
      margin-right: 6px;
    }}
    .kv-tag-blue {{
      display: inline-block;
      font-size: 11px;
      font-weight: 700;
      color: #67D5FF;
      background: rgba(35, 173, 229, 0.15);
      padding: 1px 6px;
      border-radius: 4px;
      margin-right: 6px;
    }}
    .overview-top {{
      display: flex;
      gap: 14px;
      margin-bottom: 12px;
    }}
    .portrait-card {{
      width: 148px;
      min-height: 240px;
      border-radius: 10px;
      border: 1px solid rgba(255, 255, 255, 0.12);
      background: linear-gradient(180deg, #0D1016 0%, #1B2130 100%);
      overflow: hidden;
      flex-shrink: 0;
      display: flex;
      align-items: center;
      justify-content: center;
    }}
    .portrait-card img {{ width: 100%; height: 100%; object-fit: cover; display: block; }}
    .overview-right {{ flex: 1; min-width: 0; display: flex; flex-direction: column; gap: 10px; }}
    .stat-grid {{
      display: grid;
      grid-template-columns: repeat(4, 1fr);
      gap: 8px;
    }}
    .stat-box {{
      background: rgba(255, 255, 255, 0.04);
      border: 1px solid rgba(255, 255, 255, 0.08);
      border-radius: 8px;
      padding: 7px 8px;
      text-align: center;
    }}
    .stat-box .k {{ font-size: 10.5px; color: #8B97AD; }}
    .stat-box .v {{ font-size: 16px; font-weight: 800; color: #FFFFFF; margin-top: 2px; }}
    .skill-card {{
      padding: 9px 11px;
      border-radius: 8px;
      background: rgba(11, 14, 20, 0.55);
      border: 1px solid rgba(255, 255, 255, 0.07);
      margin-bottom: 8px;
    }}
    .skill-card:last-child {{ margin-bottom: 0; }}
    .skill-hdr {{
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 8px;
      margin-bottom: 5px;
      flex-wrap: wrap;
    }}
    .skill-name {{ font-size: 13.5px; font-weight: 800; color: #FFFFFF; }}
    .skill-pills {{ display: flex; gap: 5px; flex-wrap: wrap; }}
    .pill {{
      font-size: 10.5px;
      font-weight: 700;
      padding: 1px 6px;
      border-radius: 4px;
      background: rgba(255, 255, 255, 0.08);
      color: #CBD5E1;
    }}
    .pill-sp {{ background: rgba(35, 173, 229, 0.16); color: #7DD3FC; }}
    .pill-gold {{ background: {accent_soft}; color: {accent}; }}
    .skill-desc {{ font-size: 12px; line-height: 1.52; color: #D5DFEC; white-space: pre-line; }}
    .footer {{
      display: flex;
      justify-content: space-between;
      align-items: center;
      margin-top: 6px;
      padding-top: 8px;
      border-top: 1px solid rgba(255, 255, 255, 0.07);
      font-size: 10px;
      color: #6B778D;
      letter-spacing: 0.4px;
    }}
    """


def _render_header(op: dict[str, Any], card_label: str, avatar_uri: str) -> str:
    stars_n = max(1, min(6, int(op.get("stars") or 6)))
    stars_str = "★" * stars_n
    avatar_html = (
        f'<img src="{avatar_uri}" alt="avatar" />'
        if avatar_uri
        else f'<div class="avatar-fallback">{_esc(op["name"][:1])}</div>'
    )
    badges = [
        f'<span class="badge badge-accent">{_esc(op["profession"])} · {_esc(op["sub_profession"])}</span>'
        if op.get("profession")
        else "",
        f'<span class="badge">{_esc(op["position"])}</span>' if op.get("position") else "",
        f'<span class="badge badge-blue">{_esc(op["org"])}</span>' if op.get("org") else "",
    ]
    return f"""
    <div class="hdr">
      <div class="avatar-wrap">{avatar_html}</div>
      <div class="hdr-meta">
        <div class="name-row">
          <span class="op-name">{_esc(op["name"])}</span>
          <span class="op-en">{_esc(op.get("en_name", ""))}</span>
        </div>
        <div class="stars">{stars_str}</div>
        <div class="badges">{"".join(b for b in badges if b)}</div>
      </div>
      <div class="tag-corner">
        <div class="code">PRTS // TERMINAL</div>
        <div class="sub">{_esc(card_label)}</div>
      </div>
    </div>
    """


def _render_mat_grid(items: list[tuple[str, str]], icon_uris: dict[str, str]) -> str:
    cells: list[str] = []
    for name, cnt in items:
        uri = icon_uris.get(f"道具_带框_{name}.png") or icon_uris.get(f"道具_{name}.png") or ""
        img_part = (
            f'<img src="{uri}" alt="{_esc(name)}" />'
            if uri
            else f'<div class="mat-icon-text">{_esc(name[:4])}</div>'
        )
        cells.append(
            f'<div class="mat-item">'
            f'<div class="mat-icon-box">{img_part}<span class="mat-cnt">×{_esc(cnt)}</span></div>'
            f'<div class="mat-name">{_esc(name)}</div>'
            f"</div>"
        )
    return f'<div class="mat-grid">{"".join(cells)}</div>'


def _collect_material_files(stage_lists: list[list[dict[str, Any]]]) -> list[str]:
    names: list[str] = []
    for stages in stage_lists:
        for st in stages:
            for mname, _ in st.get("items", []):
                names.append(f"道具_带框_{mname}.png")
    return list(dict.fromkeys(names))


def _wrap_doc(title: str, css: str, body_html: str) -> str:
    return (
        "<!DOCTYPE html><html lang='zh-CN'><head><meta charset='utf-8'/>"
        f"<title>{_esc(title)}</title><style>{css}</style></head>"
        f"<body><div class='card'><div class='inner'>{body_html}"
        "<div class='footer'><span>DATA: PRTS WIKI (CC BY-NC-SA 4.0)</span>"
        "<span>GAME ASSETS © HYPERGRYPH · MAIPRTS</span></div>"
        "</div></div></body></html>"
    )


async def build_operator_card_html(
    prts: PRTSClient,
    op: dict[str, Any],
    *,
    section: str = "materials",
    detail: str = "",
) -> tuple[str, int, int]:
    """为指定干员与视图构建离线可渲染的罗德岛战术卡片 HTML，返回 (html, width, est_height)。"""
    stars = max(1, min(6, int(op.get("stars") or 6)))
    accent, accent_soft = _STAR_PALETTE.get(stars, _STAR_PALETTE[6])
    op_name = op["name"]
    sec = (section or "materials").strip().lower()
    focus = (detail or "").strip().lower()

    # 需要拉取的图标清单（一次批量请求 + 本地缓存复用）
    avatar_candidates = [f"头像_{op_name}_2.png", f"头像_{op_name}.png"]
    need_files = list(avatar_candidates)

    if sec in ("modules", "module", "模组"):
        mods = op.get("modules") or []
        need_files.extend(_collect_material_files([m.get("stages", []) for m in mods]))
        uris = await prts.fetch_file_data_uris(need_files, thumb_width=160)
        avatar_uri = uris.get(avatar_candidates[0]) or uris.get(avatar_candidates[1]) or ""

        blocks = [_render_header(op, "专属模组档案", avatar_uri)]
        if not mods:
            blocks.append('<div class="sec"><div class="kv-block">该干员暂未实装专属模组。</div></div>')
        for idx, m in enumerate(mods, 1):
            stats_badges = "".join(f'<span class="badge badge-accent">{_esc(s)}</span>' for s in m.get("stats", []))
            mtype = f' [{m["type"]}]' if m.get("type") else ""
            inner_parts = []
            if stats_badges:
                inner_parts.append(f'<div class="badges" style="margin-bottom:9px;margin-top:0;">{stats_badges}</div>')
            if m.get("trait"):
                inner_parts.append(f'<div class="kv-block"><span class="kv-tag">Lv.1 特性</span>{_esc(m["trait"])}</div>')
            if m.get("talent2"):
                inner_parts.append(f'<div class="kv-block"><span class="kv-tag">Lv.2 天赋</span>{_esc(m["talent2"])}</div>')
            if m.get("talent3"):
                inner_parts.append(f'<div class="kv-block"><span class="kv-tag">Lv.3 天赋</span>{_esc(m["talent3"])}</div>')
            if m.get("tasks"):
                inner_parts.append(
                    f'<div class="kv-block"><span class="kv-tag-blue">解锁任务</span>{_esc("；".join(m["tasks"]))}</div>'
                )
            for st in m.get("stages", []):
                inner_parts.append(
                    f'<div class="stage-row"><div class="stage-label">{_esc(st["stage"])}</div>'
                    f'{_render_mat_grid(st["items"], uris)}</div>'
                )
            blocks.append(
                f'<div class="sec"><div class="sec-title"><span>模组 {idx} · {_esc(m["name"])}{_esc(mtype)}</span>'
                f'<span class="hint">含 Lv.1 ~ Lv.3 升级消耗</span></div>{"".join(inner_parts)}</div>'
            )
        width = 660
        est_h = 200 + max(1, len(mods)) * 420
        return _wrap_doc(f"{op_name} 模组", _base_css(accent, accent_soft, width), "".join(blocks)), width, est_h

    if sec in ("materials", "material", "材料", "升级材料", "专精材料"):
        focus_clean = focus.replace("精一", "").replace("精二", "").replace("精1", "").replace("精2", "")
        has_elite_kw = any(k in focus for k in ("精", "elite"))
        has_skill_kw = any(k in focus_clean for k in ("技", "专", "skill", "1", "2", "3", "一", "二", "三"))
        has_mod_kw = any(k in focus for k in ("模组", "mod"))
        show_all = not (has_elite_kw or has_skill_kw or has_mod_kw) or "全" in focus

        show_elite = show_all or has_elite_kw
        show_skill = show_all or has_skill_kw
        show_mod = show_all or has_mod_kw

        elite_mats = op.get("elite_mats", []) if show_elite else []
        mastery_groups = []
        if show_skill:
            specific_idx = [
                num for cn, num in (("一", 1), ("二", 2), ("三", 3))
                if cn in focus_clean or str(num) in focus_clean
            ]
            for g in op.get("skill_mastery_mats", []):
                if not specific_idx or g["index"] in specific_idx:
                    mastery_groups.append(g)
        mods = op.get("modules", []) if show_mod else []

        stage_lists: list[list[dict[str, Any]]] = [elite_mats]
        stage_lists.extend([g.get("stages", []) for g in mastery_groups])
        stage_lists.extend([m.get("stages", []) for m in mods])
        need_files.extend(_collect_material_files(stage_lists))

        uris = await prts.fetch_file_data_uris(need_files, thumb_width=160)
        avatar_uri = uris.get(avatar_candidates[0]) or uris.get(avatar_candidates[1]) or ""

        card_sub = f"材料清单 · {detail}" if detail.strip() else "养成升级材料"
        blocks = [_render_header(op, card_sub, avatar_uri)]
        rows_count = 0

        if elite_mats:
            rows = "".join(
                f'<div class="stage-row"><div class="stage-label">{_esc(st["stage"])}</div>'
                f'{_render_mat_grid(st["items"], uris)}</div>'
                for st in elite_mats
            )
            blocks.append(f'<div class="sec"><div class="sec-title"><span>精英化消耗</span><span class="hint">ELITE PROMOTION</span></div>{rows}</div>')
            rows_count += len(elite_mats)

        for g in mastery_groups:
            rows = "".join(
                f'<div class="stage-row"><div class="stage-label">{_esc(st["stage"])}</div>'
                f'{_render_mat_grid(st["items"], uris)}</div>'
                for st in g.get("stages", [])
            )
            blocks.append(
                f'<div class="sec"><div class="sec-title"><span>{g["index"]}技能 · {_esc(g["name"])}</span>'
                f'<span class="hint">MASTERY I ~ III</span></div>{rows}</div>'
            )
            rows_count += len(g.get("stages", []))

        for m in mods:
            if not m.get("stages"):
                continue
            mtype = f' [{m["type"]}]' if m.get("type") else ""
            rows = "".join(
                f'<div class="stage-row"><div class="stage-label">{_esc(st["stage"])}</div>'
                f'{_render_mat_grid(st["items"], uris)}</div>'
                for st in m["stages"]
            )
            blocks.append(
                f'<div class="sec"><div class="sec-title"><span>专属模组 · {_esc(m["name"])}{_esc(mtype)}</span>'
                f'<span class="hint">MODULE Lv.1 ~ Lv.3</span></div>{rows}</div>'
            )
            rows_count += len(m["stages"])

        if rows_count == 0:
            blocks.append('<div class="sec"><div class="kv-block">未匹配到对应阶段的养成材料数据。</div></div>')

        width = 660
        est_h = 220 + max(1, rows_count) * 105
        return _wrap_doc(f"{op_name} 材料", _base_css(accent, accent_soft, width), "".join(blocks)), width, est_h

    # overview / skills：干员总览 & 专三技能战术卡（内嵌半身卡面）
    portrait_candidates = [f"半身像_{op_name}_2.png", f"半身像_{op_name}_1.png"]
    need_files.extend(portrait_candidates)
    uris = await prts.fetch_file_data_uris(need_files, thumb_width=320)
    avatar_uri = uris.get(avatar_candidates[0]) or uris.get(avatar_candidates[1]) or ""
    portrait_uri = uris.get(portrait_candidates[0]) or uris.get(portrait_candidates[1]) or avatar_uri

    stats = op.get("stats") or {}
    sub_label = "专三技能档案" if sec in ("skills", "skill", "技能") else "干员战术档案"
    blocks = [_render_header(op, sub_label, avatar_uri)]

    # 上半区：半身像 + 白值 + 天赋
    stat_boxes = "".join(
        f'<div class="stat-box"><div class="k">{k}</div><div class="v">{_esc(stats.get(fk, "-"))}</div></div>'
        for k, fk in (("生命上限", "hp"), ("攻击", "atk"), ("防御", "def"), ("法术抗性", "res"))
    )
    extra_pills = "".join(
        f'<span class="badge">{k} {_esc(stats.get(fk, "-"))}</span>'
        for k, fk in (("费用", "cost"), ("阻挡", "block"), ("攻速", "speed"), ("再部署", "redeploy"))
        if stats.get(fk)
    )
    talents_html = "".join(
        f'<div class="kv-block"><span class="kv-tag">{_esc(t["name"])}</span>{_esc(t["effect"])}</div>'
        for t in (op.get("talents") or [])
    )
    portrait_img = f'<img src="{portrait_uri}" alt="portrait" />' if portrait_uri else ""
    blocks.append(
        f'<div class="overview-top">'
        f'<div class="portrait-card">{portrait_img}</div>'
        f'<div class="overview-right">'
        f'<div class="stat-grid">{stat_boxes}</div>'
        f'<div class="badges" style="margin-top:0;">{extra_pills}</div>'
        f'<div class="sec" style="margin-bottom:0;flex:1;">'
        f'<div class="sec-title"><span>特性与满级天赋</span><span class="hint">TRAIT & TALENTS</span></div>'
        f'<div class="kv-block"><span class="kv-tag-blue">特性</span>{_esc(op.get("trait", ""))}</div>'
        f'{talents_html}'
        f'</div></div></div>'
    )

    # 下半区：1~3 技能（专三/满级）
    skill_cards = []
    for sk in op.get("skills") or []:
        pills = [
            f'<span class="pill pill-gold">{_esc(sk["level"])}</span>' if sk.get("level") else "",
            f'<span class="pill">{_esc(sk["recovery"])}</span>' if sk.get("recovery") else "",
            f'<span class="pill">{_esc(sk["trigger"])}</span>' if sk.get("trigger") else "",
            f'<span class="pill pill-sp">初{sk["init_sp"]} / 消{sk["cost_sp"]}</span>' if sk.get("cost_sp") else "",
            f'<span class="pill pill-sp">持{sk["duration"]}</span>' if sk.get("duration") else "",
        ]
        skill_cards.append(
            f'<div class="skill-card">'
            f'<div class="skill-hdr"><span class="skill-name">{sk["index"]}. {_esc(sk["name"])}</span>'
            f'<div class="skill-pills">{"".join(p for p in pills if p)}</div></div>'
            f'<div class="skill-desc">{_esc(sk.get("desc", ""))}</div></div>'
        )
    if skill_cards:
        blocks.append(
            f'<div class="sec"><div class="sec-title"><span>核心技能（满级/专三数据）</span>'
            f'<span class="hint">SKILLS</span></div>{"".join(skill_cards)}</div>'
        )

    width = 680
    est_h = 460 + len(skill_cards) * 110
    return _wrap_doc(f"{op_name} 档案", _base_css(accent, accent_soft, width), "".join(blocks)), width, est_h
