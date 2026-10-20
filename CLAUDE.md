# 月之工作台

个人 AI 工作台：AI 情报、AI 玩法、AI 用量、方法库，Ctrl/⌘+K 打开命令面板。GitHub Pages 静态部署，没有后端。

## 文件
- `index.html`：整个页面（样式和脚本都在里面），左侧导航栏（电脑上可收起，状态存 localStorage `wb-side`；手机上从左边滑出），用 `#overview` `#intel` `#playground` `#usage` `#playbook` 切换视图。
- `tools/picks.py` + `.github/workflows/picks.yml`：每天北京时间 10 点找最近 30 天新建的 AI 项目，读 README，用 Claude（仓库 Secret `ANTHROPIC_API_KEY`，
  模型可用 Variable `PICKS_MODEL` 改，默认 claude-opus-5-5）翻译简介并总结“能做什么、对 Peter 的用处、怎么上手、适合度 1-5”，
  结果 `picks.json` 强推到 `picks` 分支（只留一份，不碰 main），页面从 raw.githubusercontent.com 读。没有 Key 时只输出项目列表、没有中文总结。
  本机没有 Key 但装了 Kimi CLI 时用 `tools/kimi/agent.yaml`（不带工具）。总结过的项目 14 天内不重复调用模型。
- `data.json`：用量数据，由本机脚本每小时生成并推送。**不要手改、不要改字段结构**，页面只读。
  - `periods.{day,week,month,year,all}`：各区间合计，`all` 含全部历史。
  - `daily[]`：按天的明细；没有出现的日期表示那天没有记录（页面显示“无记录”，不当 0）。
  - `codex_api.token_usage`：Codex app-server 返回的账户累计。
  - `quota`：额度数据，页面不再显示，但保留字段。
- 本机的 claude-quota-ring 插件（`usage-api/publish.py`，launchd）只推 `data.json`；2026-10-17 起不再推 `index.html`，页面只在 GitHub 上改。

## 数据规则
- 拿不到的数据显示“无记录”，不编造、不补假数据。
- 桌面对话没有 Token；终端 Token 是实际值；网页 Token 是估算。
- AI 情报在浏览器里直接读 AIHOT 公开 API（https://aihot.news/api/v1，字段见其 openapi-v1.json），
  不把内容存进仓库或 localStorage（AIHOT 条款不允许公开副本），不使用 AIHOT 的名字和 Logo 做品牌。
- AI 情报的“今日推荐”从最近 24 小时精选里按 AIHOT 评分取 5-10 条（不低于最高分的 80%），评分不够时沿用 AIHOT 的顺序，不自己编分数。
- AI 玩法只放能直接上手的：每天生成的 GitHub 精选（按适合度、再按星数排，适合度 1 分的不显示）；精选读不到或超过 3 天没更新时，浏览器直接搜 GitHub（localStorage `wb-gh-cache` 缓存 6 小时，只有英文简介）。另有 AIHOT 本周教程里有开源地址或上手步骤的。
- picks.json 只放 GitHub 公开数据和模型写的总结，不放 AIHOT 的任何内容。
- AI 玩法的收藏、已尝试、备注存在 localStorage（`wb-play-marks`），存条目 id、标题、状态、备注和原文链接（Peter 同意存标题），不存摘要。
- 方法库存在 localStorage（`wb-playbook`），页面上可以导出/导入 JSON 备份。
- 任何 API Key、Token、聊天记录都不能提交到仓库。

## 本地运行
```bash
python3 -m http.server 8000   # 打开 http://localhost:8000/
python3 tools/picks.py --out picks.json   # 本机试跑 GitHub 精选（需要 ANTHROPIC_API_KEY 或 Kimi CLI 才有中文总结）
python3 -m unittest discover tools
```
改动后在桌面（1280px）和手机（390px）宽度、深浅色下各看一遍所有视图，并确认控制台没有报错。
