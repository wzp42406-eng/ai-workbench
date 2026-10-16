# 月之工作台

个人 AI 工作台：AI 情报、AI 使用分析、Prompt Doctor。GitHub Pages 静态部署，没有后端。

## 文件
- `index.html`：整个页面（样式和脚本都在里面），用 `#overview` `#intel` `#usage` `#doctor` 切换视图。
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
- 任何 API Key、Token、聊天记录都不能提交到仓库。

## 本地运行
```bash
python3 -m http.server 8000   # 打开 http://localhost:8000/
```
改动后在桌面（1280px）和手机（390px）宽度、深浅色下各看一遍四个视图，并确认控制台没有报错。
