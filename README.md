<div align="center">
  <img src="https://fastly.jsdelivr.net/gh/Allure-Gui/maibot-arknights-wiki@main/assets/icon.png" width="76" alt="明日方舟wiki插件图标" />
  <h1>明日方舟wiki插件</h1>
  <p>适用于 <b>MaiBot（麦麦）</b> 的《明日方舟》PRTS Wiki 查询与群聊工具插件：低延迟黑话语境感知、2x 黑曜石金箔战术卡片、公开招募半身像计算与截图识图、当期寻访/活动海报、高清立绘直发与定时到期提醒。</p>

  <p>
    <img src="https://img.shields.io/badge/Version-v0.3.2-C9A25B?style=for-the-badge&labelColor=171B24" alt="Version v0.3.2" />
    <img src="https://img.shields.io/badge/MaiCore-1.4.6+-4FA374?style=for-the-badge&labelColor=171B24" alt="MaiCore 1.4.6+" />
    <img src="https://img.shields.io/badge/Data-PRTS%20Wiki-3B7DD8?style=for-the-badge&labelColor=171B24" alt="PRTS Wiki" />
    <img src="https://img.shields.io/badge/License-MIT-E8B44C?style=for-the-badge&labelColor=171B24" alt="MIT License" />
  </p>

  <p>
    <a href="https://github.com/Allure-Gui/maibot-arknights-wiki/releases">发行版下载</a>
    &nbsp;·&nbsp;
    <a href="https://github.com/Allure-Gui/maibot-arknights-wiki/blob/main/CHANGELOG.md">更新日志</a>
    &nbsp;·&nbsp;
    <a href="https://github.com/Allure-Gui/maibot-arknights-wiki/blob/main/config.example.toml">配置样例</a>
    &nbsp;·&nbsp;
    <a href="https://github.com/Allure-Gui/maibot-arknights-wiki/issues">问题反馈</a>
  </p>
</div>

<p align="center">
  <a href="https://fastly.jsdelivr.net/gh/Allure-Gui/maibot-arknights-wiki@main/assets/help-preview.png">
    <img src="https://fastly.jsdelivr.net/gh/Allure-Gui/maibot-arknights-wiki@main/assets/help-preview.png" alt="明日方舟wiki插件 · 指令手册" width="680" />
  </a>
  <br>
  <sub>点击图片可查看大图</sub>
</p>

---

## 核心功能

- **黑话语境感知与自定义昵称**
  - 群聊命中方舟黑话或干员外号时，在当前轮次末尾注入简短临时参考（≤180 字），不写入 `MaiBot.db`，不破坏提示词前缀缓存。
  - 内置双档防误触门控（日常提及“叔叔”“小羊”“山”等短词不会误触发）；支持在插件设置页配置自定义黑话（兼容 `外号=干员名`、`外号:干员名`、`外号 干员名`），管理员也可通过 `/方舟 别名 添加` 在群内热维护。
- **2x 高清「黑曜石与金箔」战术卡片图**
  - **干员档案与养成材料**：查总览、技能、模组、精二/专精材料时自动渲染含 PRTS 真实材料图标的高清卡片；发图后默认直接结束本轮，不追加冗余文字。
  - **公开招募半身像卡片 & 截图识图**：支持手打词条，也支持**带图发 `/公招` 或回复公招截图发 `/公招`**（仅在显式触发时定向调用宿主 VLM 提取词条并经官方 28 词条白名单校验，平时群聊图片零触发、零 Token 消耗），输出全干员半身像保底组合图与招募时长提示（`9:00` / `3:50`）。
  - **BOSS / 敌人档案卡片**：查询敌人与领袖的血攻防抗、11 项控制免疫、姿态特性与技能详情，自动处理同名敌方页跳转（如 `塔露拉` → `塔露拉(敌方)`）与多档级别切换。
  - **当期寻访卡池 & 活动海报卡片**：`/方舟 卡池` 与 `/方舟 活动` 分别拉取 PRTS 国服实时寻访与活动官方宣传横幅，拼合为单张高清海报卡，附带精确剩余倒计时、即将结束赤红高亮与进度条。
- **实用轻量工具与定时提醒**
  - **养成材料总账**：`/方舟 总计` 一键汇总精二、专三、模组所需的龙门币与各类素材。
  - **高清立绘直发**：`/立绘` 支持精二、初始、皮肤与列表查看，默认 1600px 且原图 ≤3.5MB 直出无损原图。
  - **干员台词彩蛋**：`/方舟 语音` 查询干员中文语音台词（仅发送文字台词，不含音频文件）。
  - **活动到期定时提醒（可选）**：设置页新增「活动到期提醒」开关（默认关闭），开启后于每日设定时间（默认北京时间 09:00）向符合群聊黑/白名单的群推送即将关闭的限时活动与卡池提醒，本地持久化防重不刷屏。
- **范围控制与多平台兼容**
  - 支持群聊/私聊总开关与黑白名单（覆盖 Hook、Tool、指令与定时提醒），兼容 Telegram 父群/话题名单；QQ 官方消息自动启用去链接策略，ClawBot 支持单聊发图并在失败时降级文本，本地控制台自动降级为纯文本。

---

## 指令速查

| 指令 | 说明 | 示例 |
| --- | --- | --- |
| `/方舟帮助` | 发送终端指令手册卡片图（`/方舟` 或 `/方舟 帮助` 均可） | `/方舟帮助` |
| `/方舟 干员 [板块]` | 查总览 · 技能 · 模组 · 材料 · 档案（别名 `/prts`、`/ark`） | `/方舟 ew 模组` |
| `/公招 词条 或 附图` | 算公招保底半身像卡；支持带图或回复公招截图识图 | `/公招 资深 狙击` |
| `/方舟 敌人 名称 [级别]` | 查 BOSS / 敌人数值、控制免疫与机制（别名 `boss`、`领袖`） | `/方舟 敌人 爱国者` |
| `/方舟 卡池` | 查看国服当期寻访卡池官方海报图（别名 `寻访`） | `/方舟 卡池` |
| `/方舟 活动` | 查看国服当期活动与常驻官方海报图 | `/方舟 活动` |
| `/立绘 干员 [版本]` | 发高清精二 · 初始 · 皮肤或查看列表（别名 `/ak图`） | `/立绘 水陈 列表` |
| `/方舟 总计 干员 [范围]` | 汇总精二、专三、模组养成材料总账 | `/方舟 总计 维什戴尔 精二` |
| `/方舟 语音 干员 [词]` | 查干员中文台词彩蛋（仅文字，不含语音） | `/方舟 语音 叔叔 戳一戳` |
| `/方舟 别名 [列表\|增删]` | 查看或热增删自定义黑话（增删限管理员） | `/方舟 别名 添加 牢猫 凯尔希` |
| `/方舟 缓存` / `清理缓存` | 查看或清空本地缓存（群聊清理限管理员） | `/方舟 缓存` |

> 日常聊天无需输入斜杠指令，直接问「ew 模组开哪个」「玛恩纳精二要什么材料」「看看水陈皮肤」即可自动响应。

---

## 安装与配置

1. **市场安装（推荐）**：在麦麦插件市场搜索「明日方舟wiki插件」并安装对应兼容版本。
2. **手动安装**：从 [GitHub Releases](https://github.com/Allure-Gui/maibot-arknights-wiki/releases) 下载对应标签的源码包，解压至 MaiBot 的 `plugins/arknights_wiki/`，确保 `_manifest.json` 与 `plugin.py` 位于该目录根部。
3. **主要配置项**（完整示例见 [`config.example.toml`](https://github.com/Allure-Gui/maibot-arknights-wiki/blob/main/config.example.toml)，已在 MaiBot `1.4.6`、`1.4.8` 与 Plugin SDK `2.23.0` 上完成离线回归）：
   - **生效范围与黑白名单**（`[scope]`）：控制群聊/私聊开关及「不限制 / 仅白名单 / 排除黑名单」。
   - **活动到期提醒**（`[reminder]`）：默认关闭，可按需开启并设置每日提醒小时（默认 `9`）、提前提醒时长（默认 `24` 小时）及是否包含卡池。
   - **自定义黑话与昵称**（`[glossary]`）：按行填写 `外号=干员名` 即可优先覆盖内置词典。
   - **管理员权限**（`[commands].admin_users`）：填写管理员 ID（支持 `platform:用户ID`）；留空时读取宿主 `plugin.permission`，均未配置时仅允许已识别的私聊执行清理缓存与增删黑话。

---

## 开源协议与版权声明

- **插件代码**：本仓库全部代码采用 [MIT License](https://github.com/Allure-Gui/maibot-arknights-wiki/blob/main/LICENSE) 开源，**不包含**任何游戏解包文件、立绘图片、Wiki 快照或预置数据库。
- **游戏版权**：《明日方舟》相关角色立绘、设定与文本版权归 **上海鹰角网络科技有限公司** 及其关联公司所有，本插件仅供爱好者学习与非商业交流使用。
- **PRTS Wiki 署名**：实时查询并缓存在本地的 Wiki 数据来自 [PRTS Wiki](https://prts.wiki)，遵循 **CC BY-NC-SA 4.0（署名-非商业性使用-相同方式共享）** 协议，使用与分享时请保留来源署名并遵守非商业条款。
