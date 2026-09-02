# PDF 固定模板方案调研与计划

> 背景：小组讨论决定通过制作固定 PDF 模板，让智能体生成 PDF 时直接套用已有模板，提升生成质量。本文档记录调研结论与实施计划。
> 日期：2026-09-01

## 一、现状分析

当前项目（`tripmate/pdf_gen.py`）的 PDF 生成方式：

| 项目 | 现状 |
|---|---|
| 技术栈 | reportlab（platypus 排版引擎）+ Pillow 处理图片 |
| 样式/布局 | 全部硬编码在 `build_pdf()`（约 280 行），颜色常量、样式工厂、表格、页眉页脚均内联 |
| 数据来源 | 黑板 `TravelProfile`（pydantic），各 Agent 通过 LLM 工具调用写入 |
| 拼装方式 | 确定性模板拼装：封面 → 行程总览 → 订单清单 → 逐日行程 → 预算 → 实景 → 酒店 → 美食/注意事项 |
| 字体 | 微软雅黑/黑体/宋体自动探测注册 |

**核心问题**：只有一种固定版式，样式与逻辑耦合在一个函数里，无法更换风格，也难以扩展新模板。

## 二、外部调研结论

### 2.1 类似开源项目

- [Agentic-AI-Trip-Planner-CrewAI](https://github.com/Ratnesh-181998/Agentic-AI-Trip-Planner-CrewAI)：CrewAI 多角色（地点专家/攻略专家/规划专家）生成旅行计划，**最终只输出 Markdown**，没有 PDF 排版层。说明"结构化数据 → 套模板渲染"这一层在多数 Agent 旅行项目里是缺失的，正是我们的差异化点。
- [awesome-llm-apps](https://github.com/Shubhamsaboo/awesome-llm-apps)：收录大量 LLM 应用，其中报告/文档类普遍采用"LLM 只产内容、渲染交给模板引擎"的分工，验证了这一架构是主流做法。

### 2.2 模板化技术路线对比

| 路线 | 代表 | 优点 | 缺点 | 适合度 |
|---|---|---|---|---|
| **A. reportlab 模板化重构** | 现项目沿用 | 零新依赖、已跑通中文字体与图片、确定性强 | 设计表达力受限于代码 | ★★★★★ |
| B. HTML+CSS 模板 → PDF | [WeasyPrint](https://m.blog.csdn.net/gitblog_01098/article/details/156078542)、Playwright headless Chromium | 设计自由度最高、模板即 HTML 文件、业界[批量报告主流做法](https://blog.csdn.net/gitblog_00817/article/details/152063915) | WeasyPrint 在本项目 Windows 环境曾因缺 GTK 失败（requirements.txt 有记录）；Playwright 依赖重 | ★★★ |
| C. Typst 模板 | [Typst Universe](https://typst.app/universe/) | 现代排版引擎、模板生态丰富、[样式系统强大](https://m.blog.csdn.net/gitblog_00703/article/details/156093049) | 需额外安装 typst 二进制、团队学习成本、中文生态一般 | ★★ |
| D. LLM 直接生成排版代码 | 部分实验项目 | 灵活 | 排版不稳定、质量不可控，与本方案"固定模板"目标相悖 | ★ |

**结论：推荐路线 A（reportlab 模板化重构）**——零新依赖、复用现有字体/图片管线，把"硬编码版式"重构为"可插拔模板"，与小组决定完全吻合。若后续追求更强视觉效果，可将 HTML+Playwright 作为第二期演进方向（模板数据接口不变，只换渲染器）。

> **定案（2026-09-01）**：小组确认走此路线，即候选清单中的"路线 B：视觉参考 + reportlab 复刻"——网络收集的设计模板不直接套代码，而是作为视觉/布局参考用 reportlab 复刻；若样张效果评审不达标再回退评估 HTML+Playwright。详见 `pdf-template-candidates.md` 决策记录。

### 2.3 可借鉴的设计模式

1. **模板注册表（Template Registry）**：每个模板是一个独立类/模块，声明元数据（名称、风格描述、适用场景），由渲染入口统一发现与调用。
2. **数据与版式分离**：Agent 只负责往 `TravelProfile` 填内容（现状已满足）；模板决定内容如何呈现。LLM 永不参与排版，保证输出确定性。
3. **模板选择**：可由用户指定，或让协调者 Agent 根据目的地/旅行风格从模板元数据中选择（作为工具调用的一部分），选错也无副作用，仅影响外观。

## 三、实施计划

### 阶段 1：模板架构重构（约 1 天）

- 抽象 `BaseTripTemplate` 接口：输入 `(TravelProfile, run_id)` → 输出 PDF 字节/文件；定义通用积木（封面、章节标题、斑马表格、页眉页脚、图片条）作为可复用基类方法。
- 把现有 `pdf_gen.py` 的逻辑迁入第一个模板 `ClassicTemplate`（当前旅行手册风格），保证输出与现状一致。
- `build_pdf()` 改为入口分发：按模板名从注册表取模板渲染；默认模板保持现行为，向后兼容。
- 验收：`tests/test_pdf.py` 冒烟测试通过，默认输出无视觉回归。

### 阶段 2：制作 4–5 个固定模板（约 3–4 天）

按定案路线（视觉参考 + reportlab 复刻），每个新模板先收集 2–3 张设计参考图，再复刻为 reportlab 版式。参考来源见 `pdf-template-candidates.md`。

| 模板 | 风格定位 | 视觉参考来源 | 适用场景 |
|---|---|---|---|
| Classic（现有） | 深蓝旅行手册 | 无需参考（现有版式） | 默认 |
| Minimal | 极简黑白、大留白、细线分隔 | [pandoc-css-weasyprint-template](https://github.com/craigbass76/pandoc-css-weasyprint-template) 的 CSS 配色版式 | 商务/短途 |
| Card | 信息卡片式布局、分块色底 | [RohitBernard/itinerary-template](https://github.com/RohitBernard/itinerary-template) 布局 + [WPS 行程单模板](https://zh-hant.wps.com/office-solutions/zh-CN-10-free-fascinating-itinerary-template-download-edit/) | 城市游/多段交通 |
| Warm | 暖色系、圆角色块、大封面图 | [Canva 行程规划器模板集](https://www.canva.com/planners/templates/itinerary/) | 亲子/休闲度假 |
| Journal（可选） | 手账风、拍立得照片框、手写感装饰 | [Canva 旅游手账风格](https://www.canva.cn/learn/travel-hand-account/) | 毕业旅行/纪念向 |

每个模板交付：固定章节顺序与布局、配色常量、封面样式；图片缺失/超预算等边界均有兜底（复用现有回退机制）。阶段末产出各模板样张，**由小组评审视觉效果，不达标则按决策记录回退评估路线 A**。

### 阶段 3：模板选择接入（约 0.5 天）

- 前端/入口增加模板选择参数；不传时默认 Classic。
- （可选）协调者 Agent 根据旅行风格自动推荐模板，写入黑板的模板字段。

### 阶段 4：验证与交付

- 为每个模板补冒烟测试（同一 profile 渲染不抛错、页数合理）。
- 生成样张供小组对比评审；更新 `启动指南` 与 README。

### 风险与对策

| 风险 | 对策 |
|---|---|
| reportlab 表达力不足，模板间差异度有限 | 差异集中在配色、封面、分节样式；确有瓶颈再评估 HTML+Playwright 二期 |
| 重构引入现有输出回归 | 阶段 1 先做"零行为变化"迁移，用现有测试把关 |
| 模板过多导致维护成本 | 首版控制在 4–5 个以内（已定案），公共积木收敛在基类，配色/封面差异参数化 |

## 四、参考来源

- [Agentic-AI-Trip-Planner-CrewAI](https://github.com/Ratnesh-181998/Agentic-AI-Trip-Planner-CrewAI)
- [awesome-llm-apps](https://github.com/Shubhamsaboo/awesome-llm-apps)
- [WeasyPrint 多模板并行生成实战](https://blog.csdn.net/gitblog_00817/article/details/152063915)
- [WeasyPrint 使用指南](https://m.blog.csdn.net/gitblog_01098/article/details/156078542)
- [Typst 样式系统实战](https://m.blog.csdn.net/gitblog_00703/article/details/156093049)
- [HTML 排版与工程落地](https://m.php.cn/faq/2816776.html)
- [PDF Export for AI Agents (PDFCrowd)](https://pdfcrowd.com/mcp-pdf-export/)
