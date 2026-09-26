# AGENTS.md

本文件是给 AI 编程助手（ZCode 等）的项目级指令。人类协作者请读 `文件修改注意事项.md`。

## 项目一句话

TripMate：基于 AutoGen 0.7.5（SelectorGroupChat 四 Agent 对等协同）的旅行规划系统。
一次对话式交互 → 产出可执行行程 PDF（HTML+Chromium 主路径 / reportlab 降级）+ 勾选后的车票/酒店订单清单。
入口 `run.py`（或 `run.bat`，注意其中硬编码了仓库外的共享 venv 路径）。

## 动手前必做

1. **先读 `文件修改注意事项.md`**：找到你要改的文件卡片，读"修改注意点"；涉及 WebSocket 消息、黑板分区名、状态机步骤名、subagent 通道名、PDF 主题名、工具返回 dict 键、MCP 关键词、配置项中任何一类，必须再读该文档第 2 节"高危耦合点速查表"和第 3 节"共享词汇表"。
2. **改完按第 5 节"分类修改检查清单"逐项自查**。

## 红线（不可擅自改动）

- WebSocket 消息字段/类型名（`tripmate/status.py` 与 `tripmate/gateway/app.py`、`web/src/types/ws.ts` + `web/src/views/PlanView.vue` 四处契约）；
- 黑板分区名（= `tripmate/models.py` TravelProfile 字段名，联动持久化、清理扫描、前端、两套 PDF 模板）；
- 状态机步骤名/协议标记词（`tripmate/team.py` SPEAKER 表 ↔ `_selector` 分支 ↔ `tripmate/prompts.py` 三处必须同步）；
- subagent 通道名 guides/covers/foods_img/spots_img/tickets/hotels/weather/route（后端 ↔ 前端灯位 ↔ 测试一致）；
- MCP 工具匹配关键词（`tools/*.py` 中各调用点 + 测试桩）；
- `TokenBudgetExceeded` 必须上浮，不得被任何降级链吞掉；
- `relay_team_event` 与 `notify` 回调绝不可抛异常。

## 修改后必做

- 跑相关测试：`python -m pytest tests/ -q`（全量 228 项；HTML 渲染用例在无 Chromium 环境自动 skip，降级路径仍被守护）。
- 若改了任何共享词汇/新增文件/修复问题 → 同步更新 `文件修改注意事项.md`（第 2/3/4/6 节）与 README/启动指南中的相关数字。
- 改了 `web/` 下前端源码 → 必须重新 `npm run build`（= `vite build && node scripts/sync-static.mjs`）把产物同步进 `static/`；Vite 产物自带 hash 文件名，旧的 `?v=` 缓存击破规则已废止。
- 改了 `config.py` 的环境变量 → 同步 `.env.example`。
- 只改文档不跑全量测试时可只跑直接相关的测试文件。

## 项目事实速查

- 测试数量：228（`python -m pytest tests/ -q`）。
- PDF 双链路：`pdf_gen.build_pdf` → `pdf_html`（Jinja2+Playwright）主路径；异常或 `PDF_RENDERER=reportlab` → `pdf_templates` cartoon 降级。
- 关键数据流：`web/src/views/PlanView.vue` ↔(WS，经 `web/src/composables/useSocket.ts`) `gateway/app.py` ↔ `session.py`（黑板+总线+Chatter+TeamRunner）↔ `team.py`（四 Agent）→ `blackboard.py`（TravelProfile）→ `pdf_gen.py`。
- 外部依赖全部有降级通道：Tavily（攻略/图）/ 高德+12306+酒店 MCP / Open-Meteo / LLM 主备。
- `scripts/ws_status_smoke.py` 当前失效（缺 WS 换票），不要用它验证 WS 改动；用 `python -m pytest tests/test_chat_delivery_persistence.py -q`。
