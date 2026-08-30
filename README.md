# TripMate · 多 Agent 协同旅游规划系统

> 实训课程大作业：基于 **AutoGen 0.7.5 AgentChat（SelectorGroupChat）** 的对等多 Agent 协同旅游规划系统。
> 一次对话式交互 → 产出 **可执行行程 PDF + 推荐订单清单**（车票/酒店已按用户要求筛选勾选）。

对应企划书《旅游规划多Agent协同系统企划书.md》，文中 §x 引用均指向该文档章节。

---

## 快速开始

```bash
# 1. 环境（Python 3.12，使用上级目录共享 venv 亦可）
pip install -r requirements.txt

# 2. 配置：复制 .env.example 为 .env，填入 LLM_API_KEY（必填）
#    其余 Key 可选——未配置的外部依赖自动走降级通道并明确标注（企划书 §7）

# 3. 启动
python run.py          # 或 Windows 双击 run.bat
# → 浏览器打开 http://127.0.0.1:8000
```

**验收用例输入**（企划书 §10.1，直接粘贴到聊天框）：

> 帮我规划十一成都 3 天游，10 月 1 号从上海出发，高铁往返，两个人，预算 6000 最多 7000，想休闲一点顺便吃吃喝喝，酒店想住春熙路附近 300 到 500 一晚的，必去大熊猫基地。

预期流程：Chatter 抽取画像 → 启动团队 → 状态时间线实时滚动（攻略搜索/车票酒店查询并行）→
草稿卡片（逐日行程 + 预算表）→ 聊天框提修改意见（如「第 2 天换成都江堰」）或回复「确认」→ 配图 + PDF + 订单清单。

## 架构一览

```
用户 ↔ Web 聊天界面（原生 HTML/JS，WebSocket）
        │
   FastAPI 网关（会话管理 + 消息桥接 + STATUS_* 状态推送）
        │
   聊天 Agent Chatter ── 常驻（闲置态只跑它，§3.2）
        │ 调用 Tool（§3.3 契约）
   Planning Team（SelectorGroupChat 四 Agent 对等协同，§3.4）
        InformationProcessor ─ Researcher
             │               ─ BookingButler
             └── Planner（草稿 → 图片 → PDF）
        │
   共享黑板 TravelProfile（版本号 + changelog + 写入串行化，§3.6）
        │
   外部能力层：Tavily 搜索/图片 / Wikimedia 图源（备用） / Open-Meteo 天气 /
              高德官方 MCP / 社区 12306-MCP（全部带降级通道，§7）
```

| 目录 | 内容 |
|---|---|
| `tripmate/agents`（预留） | Agent 相关扩展 |
| `tripmate/blackboard.py` | 共享黑板（§3.6） |
| `tripmate/team.py` | 四 Agent 工具集 + selector 状态机 + TeamRunner（阶段/检查点/增量重跑/护栏） |
| `tripmate/chatter.py` | 聊天 Agent（§4.1） |
| `tripmate/llm.py` | 模型客户端工厂：主备自动故障切换（主模型失败切次级、冷却后自动切回）+ token 成本控制 |
| `tripmate/tools/` | 搜索、图片、MCP 客户端基座、车票/酒店/天气适配层 |
| `tripmate/planning.py` | 变更影响分析（§5.3）、预算核算（§4.5）、草稿校验（可单测纯逻辑） |
| `tripmate/pdf_gen.py` | reportlab PDF（weasyprint 在 Windows 缺 GTK，按企划书备选方案切换） |
| `tripmate/gateway/app.py` | FastAPI + WebSocket 网关 |
| `static/` | 前端三件套（原生 JS） |
| `tests/` | 44 项单测（黑板/影响分析/打分/校验/selector/PDF/主备切换） |
| `scripts/` | 端到端冒烟脚本（e2e_step1/2/3） |
| `outputs/` | PDF 与配图产物 |
| `logs/tripmate.log` | ReAct 审计日志（Thought→Action→Observation→产出，验收 #16 证据） |

## 设计要点与企划书验收项对应

| 验收项（§10.2） | 实现落点 |
|---|---|
| #2 闲置态判定 | 无规划需求时仅 Chatter 运行；`TeamRunner.start()` 校验三要素 + 明确意图（§3.2） |
| #3 Tool 封装 | 团队整体为 Chatter 的 `start_planning` 工具；入参画像引用、出参受理回执（§3.3） |
| #4/#5 对等协同 + 并行 | SelectorGroupChat + 确定性 selector（会议主持人，§3.4）；JobBoard 后台任务真并行（日志时间戳交叉） |
| #6 状态推送 | StatusBus → WebSocket，延迟 <2s；Agent 徽章高亮 + 时间线 |
| #9 订单勾选 | §4.4 打分公式（车票 0.5 时间+0.3 价格+0.2 历时；酒店 0.4 价格+0.3 距离+0.3 评分），top1 自动勾选 + 直达链接 |
| #10 草稿循环 | 草稿卡片预览 → 反馈修订（≤3 轮）→ 确认定稿（§4.5） |
| #11 中途修改/增量重跑 | 检查点比对黑板版本号 → 变更影响分析（§5.3 FIELD_IMPACT）→ 仅重跑受影响环节，未受影响通道复用缓存 |
| #13 降级演练 | 任意外部服务失败自动切换模拟数据并全程标注「参考值」（§7 分档） |
| #14 成本控制 | 共享模型客户端 `total_usage()` 聚合，200K 上限熔断（§2.3） |
| #16 ReAct | 全 Agent 统一 Thought→Action→Observation；Thought 只进审计日志不推送用户（§3.5/§3.9） |

## LLM 与外部依赖配置

| 依赖 | 真实通道 | 降级通道（自动，标注参考值） |
|---|---|---|
| LLM | 主：智谱 GLM（glm-5.3-flash，OpenAI 兼容 `/api/paas/v4`）；次级：OpenCode Zen 免费通道（nemotron-3.5-lightning-free，各 free 模型配额独立），主模型失败自动切换、冷却后自动切回（.env `LLM_FALLBACK_*`） | — |
| 攻略搜索 | Tavily API（site: 限定小红书/马蜂窝） | 内置知识库结构化摘要 |
| 图片 | Tavily 图片检索（实景图，逐图标注来源域名）→ Wikimedia Commons 备用（CC 授权实拍） | PIL 本地示意卡片（标注非实景；真图/占位可混排，mode=real/mixed/mock） |
| 天气 | Open-Meteo（免 Key 真实预报） | 模拟天气 |
| 车票 | 社区 12306-MCP（npx stdio 拉起，Node≥18） | 模拟班次表 |
| 酒店/距离 | 社区酒店 MCP / 高德官方 MCP（SSE） | 模拟酒店库 / 确定性参考距离 |

> 可靠性护栏（§7 精神外推）：团队阶段结束若黑板分区缺失，由确定性通道补齐并在状态时间线
> 与 changelog 中明示「护栏补齐」——LLM 协议执行不完美时端到端流程不中断。

## 测试

```bash
python -m pytest tests/ -q          # 44 项单测
python scripts/e2e_step1_chatter.py # 冒烟① Chatter 抽取与启动判定（需 LLM）
python scripts/e2e_step2_collect.py # 冒烟② 四 Agent 协同出草稿（需 LLM）
python scripts/e2e_step3_full.py    # 冒烟③ 中途改预算→反馈修订→确认→PDF 全流程（需 LLM）
python scripts/e2e_step4_finalize.py# 冒烟④ 定稿→PDF/订单清单（无需 LLM，可随时验证）
```

## 已知边界（如实声明）

- MVP 单用户单会话（§2.3）；多用户并发为二期。
- 支付不接触（需商户资质，§4.4/§7），只做订单参数汇总 + 直达链接。
- weasyprint 在 Windows 需 GTK 运行库，本项目按企划书备选方案采用 reportlab（HTML 模板仍用于网页端草稿预览）。
- 12306-MCP 为社区实现，可能随 12306 接口变动失效；失效自动降级并提示。
