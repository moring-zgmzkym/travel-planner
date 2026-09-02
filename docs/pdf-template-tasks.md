# PDF 模板化改造 · 详细任务清单

> 依据：`pdf-template-plan.md`（方案）与 `pdf-template-candidates.md`（决策记录）。
> 定案路线：路线 B —— 视觉参考 + reportlab 复刻，4–5 个固定模板，不引入新渲染依赖。
> 日期：2026-09-01 ｜ 总工期估计：5–6 天

负责人栏留空，供小组认领。每项含验收标准，完成即打勾。

---

## 阶段 0：准备（约 0.5 天）

| # | 任务 | 说明 | 验收标准 | 负责人 |
|---|---|---|---|---|
| 0.1 | 收集视觉参考图 | 每个新模板收集 2–3 张参考：Minimal 参考 [pandoc-css 仓库](https://github.com/craigbass76/pandoc-css-weasyprint-template) CSS；Card 参考 [RohitBernard](https://github.com/RohitBernard/itinerary-template) + [WPS 行程单](https://zh-hant.wps.com/office-solutions/zh-CN-10-free-fascinating-itinerary-template-download-edit/)；Warm/Journal 参考 [Canva 行程模板](https://www.canva.com/planners/templates/itinerary/)、[Canva 手账](https://www.canva.cn/learn/travel-hand-account/) | 参考图存入 `docs/references/<模板名>/`，每模板 ≥2 张 | |
| 0.2 | 梳理现有积木清单 | 通读 `tripmate/pdf_gen.py`（重点 30–160 行的样式常量与工具函数、411–693 行 `build_pdf`），列出可复用积木：字体注册、`_style`、`_section`、`_table`、页眉页脚、`_crop_43`、`_day_strip`、`_make_cover`、`compute_budget` | 输出一页积木清单（写进本文档附录即可） | |

## 阶段 1：模板架构重构（约 1 天）⚠️ 关键路径

| # | 任务 | 说明 | 验收标准 | 负责人 |
|---|---|---|---|---|
| 1.1 | 定义模板接口 | 新建模板基类（建议 `tripmate/pdf_templates/` 包）：`BaseTripTemplate`，声明 `name`、`description`、`scenes` 元数据与 `render(profile, run_id) -> Path` 方法 | 接口定义评审通过 | |
| 1.2 | 抽公共积木到基类 | 字体注册、通用样式工厂、表格/章节/页眉页脚、图片裁剪与回退逻辑移入基类，子类只覆写"配色 + 布局"部分 | 基类可独立被任意子类复用 | |
| 1.3 | 迁移现有逻辑为 ClassicTemplate | 现 `build_pdf` 全部逻辑迁入 `ClassicTemplate`，**零行为变化** | 与旧版输出逐节对比无差异（可用文本抽取对比） | |
| 1.4 | 注册表与入口分发 | `build_pdf()` 改为按模板名从注册表取模板；注册 `classic` 为默认；非法模板名报清晰错误 | 不传参数时行为与现状完全一致 | |
| 1.5 | 回归验证 | 跑 `tests/test_pdf.py`；用同一 profile 渲染新旧两版对比 | 测试全绿 + 输出无视觉回归 | |

## 阶段 2：制作固定模板（约 3–4 天，可多人并行）

每个模板统一要求：固定章节顺序与布局、配色常量集中声明、图片缺失/数据为空/超预算均有兜底（复用现有回退机制）、复用基类积木。

| # | 任务 | 说明 | 验收标准 | 负责人 |
|---|---|---|---|---|
| 2.1 | Minimal 模板（0.5–1 天） | 极简黑白、大留白、细线分隔；去色封面 + 纯文字排版 | 样张渲染成功，风格与参考图一致 | |
| 2.2 | Card 模板（1 天） | 信息卡片式：每日行程/酒店/订单用色底卡片分块（reportlab Table + 背景色，注意 `KeepTogether` 防跨页断裂） | 样张渲染成功，卡片不跨页错乱 | |
| 2.3 | Warm 模板（1 天） | 暖色系配色、圆角色块（PIL 圆角矩形）、大封面图 | 样张渲染成功 | |
| 2.4 | Journal 手账模板（可选，1 天） | 拍立得照片框（白边 + 阴影）、装饰分隔线；若 2.1–2.3 超期则砍掉此项 | 样张渲染成功 | |
| 2.5 | 边界用例测试 | 构造极端 profile：无图片、单日行程、超预算、超长文本 | 全部模板渲染不抛错、不溢出版面 | |
| 2.6 | 样张对比输出 | 同一 profile 渲染全部模板，PDF 首页导出 PNG 预览 | `docs/references/` 或 `outputs/` 下有对比图，供评审 | |

**评审关卡（go/no-go）**：小组评审 2.6 的样张。通过 → 进阶段 3；不达标 → 按决策记录回退评估路线 A（HTML + Playwright）。

## 阶段 3：模板选择接入（约 0.5 天）

| # | 任务 | 说明 | 验收标准 | 负责人 |
|---|---|---|---|---|
| 3.1 | 后端参数透传 | 入口/黑板增加模板字段（如 `TravelProfile.template`），`_deliver_final()` 传入注册表 | 指定模板名时输出对应样式 | |
| 3.2 | 前端选择入口 | 前端增加模板下拉选择（列出注册表中模板的名称与风格描述），默认 classic | 前端可选并生效 | |
| 3.3 | （可选）Agent 自动推荐 | 协调者 Agent 按旅行风格从模板元数据选模板写入黑板 | 推荐结果合理，用户可覆盖 | |

## 阶段 4：验证与交付（约 0.5–1 天）

| # | 任务 | 说明 | 验收标准 | 负责人 |
|---|---|---|---|---|
| 4.1 | 全模板冒烟测试 | 每模板一个测试用例（同 profile 渲染不抛错、页数合理） | `pytest` 全绿 | |
| 4.2 | 端到端走查 | 完整跑一次真实对话流程（含图片生成），确认模板选择贯穿全链路 | 最终 PDF 为所选模板样式 | |
| 4.3 | 文档更新 | 更新 `启动指南`、README（模板列表与选择方式）、本清单勾选状态 | 文档与实现一致 | |
| 4.4 | 提交与备份 | 提交代码，导出样张留档 | 仓库状态干净，样张可查 | |

---

## 分工建议（3 人组示例）

- 成员 A：阶段 1 全部（架构，关键路径，需最熟悉 `pdf_gen.py` 的人）
- 成员 B：阶段 2 的 Minimal + Warm；成员 C：Card + Journal
- 阶段 0 / 3 / 4 谁先空闲谁认领；评审关卡全员参与

## 风险提醒

- 阶段 1 是阻塞项：阶段 2 所有模板依赖基类积木，务必先完成 1.5 回归再并行开工。
- reportlab 圆角/阴影等效果要靠 PIL 预渲染图片实现（现有 `_make_cover` 已有先例），新模板遇到表达力瓶颈先降级设计，不要卡死。
- 每完成一个模板立即跑 2.5 边界用例，避免最后集中返工。

---

## 进度记录（2026-09-01 执行）

| 阶段 | 状态 | 说明 |
|---|---|---|
| 0.2 积木清单 | ✅ | 梳理结果直接落为 `tripmate/pdf_templates/base.py`（字体/样式/表格/章节/页眉页脚/图片/封面全部积木化、主题参数化） |
| 1.1–1.5 架构重构 | ✅ | `pdf_templates/` 包：`BaseTripTemplate` + `ClassicTemplate`（零行为迁移）+ 注册表分发；`pdf_gen.build_pdf(profile, run_id, template=None)` 向后兼容，原冒烟测试通过 |
| 2.1–2.4 模板制作 | ✅ | 6 模板齐备：classic（现有）/ guide（慢游图文路书，复刻用户提供的参考 PDF `docs/references/samples/陕西3天2晚懒人版旅行路书_图文版.pdf`：夜空金月封面+米白纸底+垂直时间线+居中总页码）/ minimal（极简黑白）/ card（青绿靛蓝卡片）/ warm（珊瑚暖色）/ journal（墨绿手账+拍立得框），均由子代理并行完成并自验 |
| 2.5 边界用例 | ✅ | `tests/test_templates.py`：极端画像（无图/无订单/单日/超预算）× 全模板渲染不抛错 |
| 2.6 样张输出 | ✅ | `python scripts/make_template_samples.py` → `docs/references/samples/`（6 模板 × 封面+内页 PNG 与 PDF），**待小组评审（go/no-go 关卡）** |
| 3.1–3.2 模板选择 | ✅ | `BasicInfo.template` → `_deliver_final` 透传 → `/api/templates` → WS `template` 消息写黑板 → 前端会话栏"路书样式"下拉；`build_pdf` 级贯穿已验证 |
| 3.3 Agent 自动推荐 | ⬜ 可选未做 | 保持可选项 |
| 4.1 冒烟测试 | ✅ | PDF 层 14 项全绿（6 模板 × 冒烟+边界 + 注册表 + 原冒烟）；其余依赖 autogen 的用例需在完整环境运行 |
| 4.2 端到端走查 | ⚠️ 部分 | 本机缺 `.venv`/autogen，`team.py` 级链路无法在本会话运行；需在有依赖的环境跑 `run.bat` 走一次完整对话并切换模板验证 |
| 4.3 文档更新 | ✅ | README 目录/测试数已更新；本清单进度记录同步 |
| 4.4 提交留档 | ⬜ | 未提交（等小组评审样张后再提交） |

**遗留事项**：① 小组评审样张（不达标则按决策记录回退路线 A）；② 完整环境端到端走查（前端下拉 → 定稿 PDF 样式）；③ 可选的 Agent 自动推荐模板。
