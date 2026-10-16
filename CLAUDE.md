# 月之工作台

个人 AI 工作台：AI 情报、AI 使用分析、Prompt Doctor、任务复盘、方法库。GitHub Pages 静态部署，没有后端。

## 文件
- `index.html`：整个页面（样式和脚本都在里面），用 `#overview` `#intel` `#usage` `#doctor` `#review` `#playbook` 切换视图。
- `tools/review.py`：任务复盘的本机分析脚本，读 ~/.claude/projects 和 ~/.codex/sessions，
  结果写到 ~/.ai-workbench/review.json（仓库外），由它起的 127.0.0.1 服务以 `/review.local.json` 提供给页面。
  同一个服务读写方法库 ~/.ai-workbench/playbook.json（`/playbook.local.json`，PUT 要求 `X-Workbench: 1` 和本机 Host，防止别的网站跨域写入）。
  `--serve-only` 只开服务不重新分析。
  `--kimi` 时把长对话交给本机 Kimi CLI（`tools/kimi/agent.yaml`，不带工具），结果缓存在 ~/.ai-workbench/kimi-cache.json。
- `data.json`：用量数据，由本机脚本每小时生成并推送。**不要手改、不要改字段结构**，页面只读。
  - `periods.{day,week,month,year,all}`：各区间合计，`all` 含全部历史。
  - `daily[]`：按天的明细；没有出现的日期表示那天没有记录（页面显示“无记录”，不当 0）。
  - `codex_api.token_usage`：Codex app-server 返回的账户累计。
  - `quota`：额度数据，页面不再显示，但保留字段。
- 本机脚本也会推送 `index.html`（提交信息“更新工作台页面”）。改页面后要同步回本机那份，否则会被覆盖。

## 数据规则
- 拿不到的数据显示“无记录”，不编造、不补假数据。
- 桌面对话没有 Token；终端 Token 是实际值；网页 Token 是估算。
- AI 情报在浏览器里直接读 AIHOT 公开 API（https://aihot.news/api/v1，字段见其 openapi-v1.json），
  不把内容存进仓库或 localStorage（AIHOT 条款不允许公开副本），不使用 AIHOT 的名字和 Logo 做品牌。
- Prompt Doctor 只做规则检查，不调模型、不保存输入。
- 任务复盘的数据（含对话原文）永远不能进仓库或公开网站；只保存用户消息，不保存 AI 回复；归类要附原文依据，Kimi 给的引用对不上原文就丢掉。
- 归类优先级：用户手动修改 > Kimi > 关键词规则。没有明确的完成标记时不统计“成功率”。
- 任何 API Key、Token、聊天记录都不能提交到仓库。

## 本地运行
```bash
python3 -m http.server 8000   # 打开 http://localhost:8000/
python3 tools/review.py        # 任务复盘，打开 http://127.0.0.1:8787/#review
python3 -m unittest discover tools
```
改动后在桌面（1280px）和手机（390px）宽度、深浅色下各看一遍所有视图，并确认控制台没有报错。
