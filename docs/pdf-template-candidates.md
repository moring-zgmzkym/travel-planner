# PDF 固定模板候选收集清单

> 任务：从网络平台搜集可直接套用/改造的 PDF 模板，目标 4–5 个候选。
> 日期：2026-09-01（已定案：采用路线 B，见文末决策记录）

## 候选模板总览

| # | 模板 | 来源 | 风格 | 许可证 | 集成方式 | 改造成本 |
|---|---|---|---|---|---|---|
| 1 | [Gloo Itinerary HTML Template](https://github.com/harnishdesign/itinerary-html-template) | GitHub（13★） | 简洁干净的 Bootstrap 行程页，航班/日程排版成熟 | MIT（已核实） | HTML 改 Jinja2 模板 → Playwright 渲染 PDF | 中 |
| 2 | [Traveler Bootstrap 模板](https://github.com/technext/traveler) | GitHub | 旅行社风格，行程展示区配色丰富 | 免费（需核实具体条款） | 抽取行程页区块改模板 | 中偏高 |
| 3 | [RohitBernard/itinerary-template](https://github.com/RohitBernard/itinerary-template) | GitHub | 交互式行程模板，信息卡片式布局 | 待核实 | 参考布局改静态版 | 中 |
| 4 | [pandoc-css-weasyprint-template](https://github.com/craigbass76/pandoc-css-weasyprint-template) | GitHub（28★） | 极简文档风（适合 Minimal 模板），CSS 样式体系完整 | 未声明（需联系或参考性使用） | 借鉴 CSS 配色与版式 | 低（作设计参考） |
| 5 | [Canva 行程规划器模板集](https://www.canva.com/planners/templates/itinerary/) + [旅游手账风格](https://www.canva.cn/learn/travel-hand-account/) | Canva / [WPS 行程单模板](https://zh-hant.wps.com/office-solutions/zh-CN-10-free-fascinating-itinerary-template-download-edit/) | 视觉设计水准最高的现成行程单/手账样式（暖色、卡片、大留白等多种） | 平台授权，不可直接用于代码，仅作视觉参考 | 用 reportlab 复刻视觉（不引入新渲染依赖） | 中 |

## 关键权衡（需要审批的点）

**两条技术路线，决定后面怎么干：**

- **路线 A：HTML 模板 + 新渲染器（候选 1–4 走这条）**
  - 模板资源最丰富，视觉效果上限高，GitHub 上行程类 HTML 模板可直接套用。
  - ⚠️ 但本项目此前 WeasyPrint 在 Windows 因缺 GTK 失败过（`requirements.txt` 有记录），需要改用 **Playwright headless Chromium** 渲染，新增依赖约 150MB。
- **路线 B：视觉参考 + reportlab 复刻（候选 5 走这条）**
  - 零新依赖，复用现有字体/图片管线，稳定性最好。
  - 复刻工作量在样式代码上，视觉上限受 reportlab 表达力限制。

**我的建议（混合方案，共 4–5 个模板）：**
1. Classic（现有 reportlab 版）保留为默认 —— 保底；
2. 从候选 1（MIT、最成熟）改造一个 HTML 行程模板，用 Playwright 渲染 —— 质量担当；
3. 参考候选 4/5 的视觉，用 reportlab 复刻 2–3 个风格模板（极简 / 暖色手账风）。

## 诚实说明

- GitHub 上行程类模板整体星数不高（多为个位数到十几星），属于"够用"级别，非顶级精品；真正视觉好的行程单设计集中在 Canva/千库网等设计平台，但那些不可直接转为代码。
- 候选 2、3、4 的许可证未逐一核实，正式采用前需确认（尤其候选 4 无 license 声明，只能当设计参考）。
- 若小组决定走路线 A，建议先花半天做一个"HTML 模板 + Playwright 渲染"的最小验证，确认中文/图片/分页没问题再全面铺开。

## 决策记录（2026-09-01）

**小组决定：采用路线 B（视觉参考 + reportlab 复刻）。**

- 不引入 Playwright/WeasyPrint 等新渲染依赖，继续用现有 reportlab 管线。
- 候选 1–4 不再作为代码模板套用，降级为**布局与视觉参考**；候选 3（卡片式布局）和候选 4（极简文档风）的版式思路纳入复刻目标。
- 候选 5（Canva / WPS 设计模板）作为主要视觉参考来源。
- 模板数量定为 4–5 个（含现有 Classic）。
- **回退条件**：若复刻出的样张视觉效果经小组评审不达标，再重新评估路线 A（HTML 模板 + Playwright 渲染）。
- 具体实施计划见 `pdf-template-plan.md`。
