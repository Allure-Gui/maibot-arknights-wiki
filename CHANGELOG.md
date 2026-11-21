# 更新日志 · 明日方舟wiki插件（maibot-arknights-wiki）

## v0.2.0 (2026-11-22)
- 正式更名为 **明日方舟wiki插件**（底层插件 ID 保持 `feiyu.arknights-wiki` 不变）。
- 新增罗德岛工业风 2x Retina 卡片图渲染（`cards.py`），支持干员总览、专三技能、模组与养成材料卡，内嵌 PRTS 带框材料图标与干员头像。
- 新增 L1 内存热缓存与 6 并发图标拉取，发图完成后支持 `stop_after_execution=True` 阻断多余文字回复；`/方舟` 与 `/立绘` 返回 `intercept=2` 直接截断不进 Planner。
- 立绘清晰度默认提升至 `1600px`（原图 ≤3.5MB 直出无损原图）。
- 新增 `/方舟 缓存` 与 `/方舟 清理缓存` 指令，并为群聊清缓存加上管理员权限校验（`commands.admin_users` + 自动读取宿主 `master_users`）。
- 修复 `store.py` 中 SQLite 连接未显式关闭以及多线程并发写图片缓存临时文件可能冲突的问题。

## v0.1.0 (2026-11-21)
- 初始版本：PRTS MediaWiki 客户端、双档置信度方舟黑话语境感知、`ak_wiki_query` / `ak_send_illustration` 双可见工具与 `/方舟` / `/立绘` 快捷指令。
