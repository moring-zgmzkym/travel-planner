# 方向二优化方案：HTML 设计师 Agent + 渲染工具链（已审核修订 v2）

> 背景：小组讨论后决定双线并行——方向一沿用固定模板（已完成 6 模板体系），方向二舍弃套模板，由一个**专门的 HTML 设计 Agent** 在 agent group 收集汇总完信息后编写效果出众的 HTML，再调用工具完成 HTML → PDF 渲染。本文档为方向二的详细方案与任务清单。
> 日期：2026-09-02 初稿 ｜ 2026-09-03 多方位审核修订（问题清单见 §八）｜ 预估总工期：5–8 天 ｜ 状态：修订稿待审批

---

## 一、技术选型（已在本机核实 2026-09-03）

| 渲染器 | 本机状态 | 评估 | 结论 |
|---|---|---|---|
| **Playwright + Chromium** | ✅ `playwright 1.58` 已装（D:\Python311），Chromium 内核已下载（`ms-playwright/chromium-1234`） | 打印级 CSS 支持最全（@page/flex/grid/SVG/webfont），`page.pdf()` 可控页边距与页眉页脚，API 成熟 | **首选** |
| Edge headless CLI | ✅ `C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe` | `--headless --print-to-pdf` 零依赖兜底；局限见 §八 #12（须独立 `--user-data-dir`、`--no-pdf-header-footer`、失败可能静默、非 ASCII 输出路径需实测） | 备选（降级通道） |
| WeasyPrint | ❌ 曾在本项目 Windows 环境因缺 GTK 失败（requirements.txt 有记录） | 不重蹈覆辙 | 排除 |
| wkhtmltopdf/pdfkit | — | 上游已归档停止维护 | 排除 |

**本机核实补充**：中文字体齐备（微软雅黑 msyh.ttc / 等线 / 宋体），字体栈 `"Microsoft YaHei", "DengXian", "SimSun", sans-serif` 可直接用；PyMuPDF (fitz) 1.27 已装但 **requirements.txt 未声明**——任务 3.4 须补 `playwright` 与 `PyMuPDF` 两项。

**依赖代价（如实声明）**：队友机器需 `pip install -r requirements.txt && playwright install chromium`（约 150MB 一次性下载），写入启动指南；Playwright 不可用时自动降级 Edge CLI，再不可用降级回方向一模板体系——**渲染链三级兜底，任何一级失败都不影响交付 PDF**（兜底保证见 D5 修订条文）。

## 二、总体架构（v2 修订：Designer 不进群聊）

```
用户确认草稿（现有流程不变）
        │
  _deliver_final（定稿入口，team.py 唯一交付点）
        │  basic_info.template == "designer" ?
        ▼
  ┌──────────────────────────────────────────┐
  │ Designer 子编排（确定性外循环，非群聊成员）      │
  │  第 6 Agent = 独立 AssistantAgent（带工具），   │
  │  输入=黑板分区快照（非对话历史）                 │
  │  循环：生成 HTML(body 片段) → 消毒套壳 → 渲染   │
  │       → 诊断不达标 → 摘要反馈重试（≤2 轮）      │
  │  熔断：整链 asyncio.wait_for(600s)；           │
  │        逐轮 check_budget()；                  │
  │        CancelledError（用户停止）原样放行        │
  └──────────────────────────────────────────┘
        │ 消毒后的完整 HTML（落盘 outputs/html/）
        ▼
  render_html_pdf 工具（确定性，非 LLM）
    Playwright Chromium → Edge CLI → 失败
        │                            │
        ▼                            ▼
     PDF 产物               回退 build_pdf（模板注册表，
                            回退前过滤非法模板名，见 D5）
```

**为何不进 SelectorGroupChat（v2 关键修订，替代原 3.2）**：现有 `_selector` 是确定性状态机，其推进分支只识别 4 个既有 Agent 的工具调用——Designer 进群聊一次发言机会都拿不到；强行加分支则触碰 2026-08-31 实测死锁史的同一故障面；`consecutive>3 强制收敛 Planner` 规则会拦腰打断设计修正循环；finalize 阶段 `SectionReadyTermination` 监听 final 分区——Planner 按现提示词抢调 deliver_final 会直接终止群聊让 Designer 没上场，Designer 写 final 分区则当场终止砍掉渲染诊断。而 Designer 本就不需要对话历史（输入是黑板快照），确定性外循环在状态机风险、终止条件交互、上下文精确性、可测试性四维全胜。**演示叙事不受损**：Designer 仍是真实的第 6 个 Agent，经 StatusBus 推送工作状态、时间线与徽章可见、AUDIT 全程留痕。

核心原则不变：**创意在 LLM、确定性在工具**。Agent 只产出 HTML 内容片段；消毒、套壳、渲染、诊断、缓存、回退全部是确定性代码，LLM 无法跳过。

## 三、关键设计决策

### D1 设计系统先行（质量稳定的核心手段）
不让 Agent 自由发挥写 HTML，而是提供一套"印刷级设计系统"让它**组合**：
- `tripmate/design/print.css`（v2 修订位置：Python 包资源目录，非 static/）：@page A4 边距、分页控制（`break-inside: avoid` 卡片不跨页）、中文字体栈（已核实本机字体）、3–4 套主题变量 class、组件样式。**由套壳代码内联注入 `<style>`**，不经 LLM、不耗 token、无相对路径问题
- 组件片段库（`tripmate/design/fragments.py`，few-shot 素材）：封面英雄区、行程总览表、订单卡、垂直时间线、预算 SVG 环图/柱状图、实景图墙、酒店卡、美食卡（左图右文）、结尾页
- **金样 HTML ×1**（人工打磨，成都 fixture 数据；原计划 2 份，为控制隐性 token 成本收敛为 1 份），注入生成轮提示词（修正轮不带），体积控制在 3–5K token（有单测体积回归锁）——系统提示词每次模型调用都重发，多份全注入的隐性成本是每轮 +5–10K token
- **图片规则（v2 修订）**：黑板 `images[].path` / `hotels[].image_path` 存的是 **Windows 绝对路径**（如 `D:\CODE\...\outputs\images\xxx.jpg`，非 file:// 也非相对路径）。由确定性代码在快照阶段统一 `Path.as_uri()` 转为 `file:///` URI（正确处理中文/空格/反斜杠的百分号编码）后注入快照，Agent 照抄，禁止自行拼路径；消毒器校验 file URI 解析回的本机路径必须位于 `IMAGE_DIR`（outputs/images，含 crops 子目录）白名单内（见 D2）

### D2 输出契约与安全消毒（v2 改写为允许列表条文）
- **输出契约（v2 修订，应对 max_tokens=8192 上限）**：Agent 输出 **body 内容片段**（首个 `<section>` 起），以哨兵注释 `<!--TRIPMATE-END-->` 结尾；可选首行 `THEME: theme-xxx` 声明主题。确定性套壳代码注入 `<!DOCTYPE html>`/`<html>`/`<meta charset="utf-8">`/`<title>`/print.css 全文/Agent 补充样式——骨架与字符集永不依赖 LLM 自觉。`write_html` 检测哨兵缺失即判**截断失败**（无需等渲染），第一次自动提示精简重试，仍失败升级分节输出（每节独立调用拼装，拼装器由确定性代码实现）
- **消毒 = 允许列表（allowlist），禁止黑名单正则剥标签**（大小写混淆/嵌套拼接绕过是经典漏洞）。用 `html.parser` 重建文档：
  - 标签白名单：结构（div/section/header/footer/main/h1-h6/p/ul/ol/li/table/thead/tbody/tr/td/th/figure/figcaption/span/a/strong/em/b/i/br/hr/img）+ SVG 全套（svg/g/path/circle/rect/ellipse/line/polyline/polygon/text/tspan/defs/linearGradient/stop/use/title）+ style；白名单外整节点删除（script/iframe/object/embed/applet/base/meta/link/source/picture/form/input/button/… 一律不保留）
  - 属性白名单：每标签固有权 + class/id/style/colspan/rowspan/viewBox/d/fill/stroke/…；**任何 `on*` 事件属性、`srcset` 直接剥除**
  - URL 属性规则：`img src` 仅允许 `file:///`（白名单校验见下）与 `data:image/png|jpeg;base64`（单图 ≤2MB、全文档 data URI 总量 ≤4MB，`data:image/svg+xml` 与 `data:text/*` 一律拒绝）；`a href` 仅允许 `http(s)://`（订单直达链接是黑板数据，`<a>` 只生成链接注释不触发资源加载，安全保留；`javascript:`/`file:` scheme 剥 href 留文本）；`svg *` 的 href/xlink:href 禁止外部引用
  - `<style>` 内容与 style 属性单独清洗：禁 `@import`；`url()` 仅允许白名单内 file URI 与 data:image/png|jpeg，其余剥除；消毒动作记入诊断标记，**inspect_render 区分「消毒器主动拦截」与「意外资源加载失败」，前者不计入重试判定**
  - `file:///` 白名单：URI 解析回本机路径，`Path.resolve()` 后必须 `is_relative_to(IMAGE_DIR)`；UNC 路径（`\\server\share`，SMB 凭据外泄面）直接拒绝；白名单外剥除并记标记。渲染启动参数保持 Chromium 默认沙箱（不加 `--allow-file-access-from-files`）
- **数据诚实约束（写入提示词 + 抽检）**：只许使用快照 JSON 里的数据，价格/时间/来源不得编造；图片来源沿用「示意/实拍」标注体系
- **资源限额表（v2 新增）**：HTML 片段 ≤ 512KB（超限判交付失败进重试）；图片引用 ≤ 40 张；单次渲染 goto 30s + page.pdf 60s；消毒失败/截断/渲染失败/诊断不达标**共享** ≤2 轮重试预算（非各自 2 轮）；进程级渲染 Semaphore(1) 防多会话并发 finalize 内存峰值

### D3 渲染-诊断-修正循环（v2 修订）
`inspect_render`（合入 render 步骤返回，减少一次 LLM 往返）返回确定性诊断（不含 LLM 判断）：
- PDF 头合法（fitz 可打开）、页数落在按天数动态的警告带（超界仅警告不判失败：`2..days*6+6`）
- 必备章节关键词存在性（fitz 文本抽取：行程/预算/订单）
- 溢出信号：fitz `get_text("blocks")` 坐标超页界的块计数（**best-effort 信号，仅明显越界**；正常分页截断由 print.css 预防，不作为硬门槛）
- 消毒标记回传（主动拦截 ≠ 资源错误，不误判进重试）
- **v2 砍掉首页 PNG 视觉自查**：共享客户端 `model_info.vision=False`（llm.py `_MODEL_INFO`），AssistantAgent 拒收多模态消息；缩略图仍落盘供人工排查
不达标 → 有界诊断摘要（≤500 字符）反馈给 Agent 修正，**共享重试预算 ≤2 轮**，仍失败 → 走回退链。整链受 600s 熔断约束（见 D7）。

### D4 Token 预算、延迟与缓存（v2 修订）
- **体量目标（修订）**：Agent 输出 = body 片段，典型 3–6k output tokens，处于共享单例 client 的 `max_tokens=8192` 安全区内（原稿「全文 8–20k」与全局上限自相矛盾，且全局改大影响所有 Agent、独立 client 又绕开 500K 熔断记账——`total_usage()` 只读共享单例，llm.py:255-304）。哨兵检测 + 分节升级兜底（见 D2）
- **记账与审计**：Designer 必须复用 `get_model_client()` 单例（主备切换免费继承、记账天然准确）；链路前后取 `total_usage()` 差值单列 AUDIT；链内逐轮 `check_budget()`（外循环在 `_stream_team` 之外，无自动检查点），`TokenBudgetExceeded` → 走回退而非终止交付
- **上下文膨胀控制（v2 新增）**：`reflect_on_tool_use=False`、`max_tool_iterations=3-4`（写→渲→修一轮内完成）；`write_html` 只返回元数据（路径/字节数/消毒标记），**永不回显全文**；render 无参（用已落盘文件）；诊断只回摘要；每轮修正新建 Agent 实例（旧版全文不陪跑）。朴素实现最坏 8–10×全文重发 ≈ 80–100K prompt token/次，措施全开后 ≈ 1–2×/版
- **延迟口径（修订）**：典型 2–4 分钟；finalize 阶段 ETA 分模式推送——designer (4,8) 分钟、模板 (1,2) 分钟；600s 熔断后自动回退（reportlab 秒级）
- **缓存（v2 改为内容寻址）**：键 = 进 Designer 提示词的分区投影（draft/勾选 tickets/勾选 hotels/images/weather/basic_info/detail_info，剔除 fetched_at 等时间戳字段）+ `DESIGNER_PIPELINE_VERSION` 的 sha256；**不用 run_id+黑板版本号**（`_deliver_final` 写 final 分区自身 +1 版本导致自失效、无关分区写入假 miss、跨次定稿永不命中）。取指纹时机在写 final 之前。存储 `outputs/htmlcache/{key}.html`，命中只重渲染不重新生成；HTML 源文件另存 `outputs/html/trip_{run_id前8}.html` 留档

### D5 回退链（v2 补齐确定性细节）
- **回退做在 designer 分支内部**（v2 关键修订）：`_deliver_final` 中 `try: designer 链 except Exception: AUDIT + 状态提示 + 模板通道`——不能依赖外层异常抢救：`_rescue_deliverables("finalize")` 只补发已存在的 final，designer 崩溃时 final 未写入，救援为空，用户将拿不到任何 PDF。**`asyncio.CancelledError` 单独 re-raise**（用户点停止就是停止，不得转成回退继续跑）
- **回退入口过滤模板名**：`prof.basic_info.template` 可能为 `"designer"`，直接传 `build_pdf` 会被 `get_template` 抛 ValueError；回退前统一 `template if template in REGISTRY else None`。护栏 `_ensure_sections` 的 finalize 分支直接走模板通道（确定性兜底不得再进 LLM 循环）
- 整链 600s 熔断（D7）：超时即回退。**用户永远能拿到 PDF**；`FinalDelivery` 新增 `render_source` 字段（"designer" / "template:<name>"），designer 链 HTML 页脚由套壳代码追加「本路书由 AI 版面设计师生成」，模板回退的来源标注在前端 final 卡片与状态推送中呈现（不改 6 模板注册表接口）

### D6 图表策略（v2 修订 B 路）
预算环图/柱状图两条路：A. Agent 直接写 inline SVG（预算数据由黑板 JSON 计算好注入快照，Agent 只排版；静态检查：SVG ≤512KB、禁 foreignObject、禁外部 href，超限剥除并切 B 路，不烧重试）；B. 复用现有 `budget_charts`（pdf_templates/base.py:497）PIL 图，**以 `file:///` 本地路径 `<img>` 嵌入（v2 修订：原 base64 data URI 方案无谓消耗 4–7K token）**。金样先做 A（效果上限高），渲染不稳则切 B。

### D7 确定性外循环规范（v2 新增）
```
async def designer_chain(prof, run_id, agent_factory) -> DesignerResult
  t0 = now; key = fingerprint(prof); cache 命中 → 直接渲染缓存 HTML
  for attempt in 1..3:            # 1 生成 + ≤2 修正，共享预算
      check_budget()
      agent = agent_factory(...)  # 每轮新实例
      await agent.run(task_text)  # 工具闭包内 write_html/render_pdf
      if last_render_ok: break
      task_text = 诊断摘要(≤500 字符)
  return pdf_path or raise DesignerError
# 调用方：asyncio.wait_for(designer_chain(...), timeout=600)
#   TimeoutError → 回退模板链；CancelledError → raise（停止语义）
# 软检查点：t0+240s 仍无 HTML 落盘 → 跳过剩余修正轮直接回退
```
该函数与 AutoGen 解耦（`agent_factory` 注入），无 autogen 环境可全量单测（假 factory）。

## 四、任务清单

负责人栏留空供认领；每阶段末为验收关卡。

### 阶段 0：渲染器 Spike（0.5 天）⚠️ 决策门
| # | 任务 | 说明 | 验收标准 | 负责人 |
|---|---|---|---|---|
| 0.1 | Playwright POC | 独立脚本：含中文字体/本地图片（file:/// 中文路径）/SVG 图表/@page 边距/分页控制的 HTML → `page.pdf()` 出 A4 PDF；固定管线「落盘 HTML → goto(file://) → pdf()」 | PDF 中文正常、图片嵌入、卡片不跨页断裂；确认 @page 与 page.pdf(margin) 的优先级行为并写入决策记录 | |
| 0.2 | Edge CLI 兜底验证 | 同一 HTML 出 PDF；**v2 补测**：独立 `--user-data-dir`（否则转发给已开实例静默不产 PDF）、`--no-pdf-header-footer`、非 ASCII 输出路径、失败静默检测（事后校验 %PDF 头/大小/页数） | 兜底通道可用；差异记录在案；**兜底验收降级为「PDF 合法+文本可抽取」，不承诺版式一致** | |
| 0.3 | 选型确认 | 依 0.1/0.2 锁定主/备渲染器、超时参数与依赖安装步骤 | 写入本文档决策记录 | |

### 阶段 1：设计系统（1.5–2 天）
| # | 任务 | 说明 | 验收标准 | 负责人 |
|---|---|---|---|---|
| 1.1 | print.css 框架 | @page、分页控制、字体栈、3–4 套主题变量、全部组件样式 | 金样引用后出片达标 | |
| 1.2 | 组件片段库 | 10 段组件素材（封面/总览/订单卡/逐日行程卡/时间线/预算图表/图墙/酒店卡/美食卡/结尾页） | 每组件可独立渲染正确 | |
| 1.3 | 金样 ×1 | 成都 fixture 数据手工打磨 1 份完整 HTML，对标参考 PDF 视觉水准；**裁剪至 3–5K token 注入提示词（有单测体积回归锁）** | 渲染 PDF 经小组目检认可 | |
| 1.4 | 消毒器 + 渲染工具 | `tripmate/tools/htmlpdf.py`：sanitize_html()（允许列表，D2 条文）+ wrap_html()（套壳注入 print.css/charset/页脚）+ render_html_pdf()（Playwright 主 / Edge 备 / 异常信号；**独立 `with sync_playwright()` 上下文 + try/finally 关闭 + 进程级 Semaphore(1) + goto/pdf 各自超时**；调用侧 `asyncio.to_thread`）+ inspect_pdf()（fitz 诊断，PyMuPDF 缺失时优雅降级跳过文本检查） | 单测：script/iframe/on*/meta refresh/base/srcset/data: 类型/CSS @import 与 url()/file 白名单/UNC 全覆盖；正常 HTML 出合法 PDF；中文与空格路径 as_uri 正确 | |

### 阶段 2：Designer Agent（1.5–2 天）
| # | 任务 | 说明 | 验收标准 | 负责人 |
|---|---|---|---|---|
| 2.1 | Agent 定义与提示词 | 第 6 个 Agent「Designer 版面设计师」：系统提示词注入设计系统说明 + 1 份金样 + body 片段输出契约（哨兵/THEME 行）+ 数据诚实约束；`reflect_on_tool_use=False`、`max_tool_iterations=3-4` | 提示词评审通过 | |
| 2.2 | 工具组 | `write_html(html)`（哨兵/限额校验 → 消毒 → 落盘 → 返回元数据不回显全文）、`render_pdf()`（无参，调 1.4 渲染+诊断，返回有界 JSON 摘要） | 工具单测全绿 | |
| 2.3 | 确定性外循环 | `tripmate/designer.py` designer_chain（D7：≤2 修正轮共享预算、600s 熔断、240s 软检查点、逐轮 check_budget、CancelledError 放行、与 AutoGen 解耦可假 factory 单测）；轮次与用量（total_usage 差值）进 AUDIT | 构造坏 HTML/截断 HTML/超时实测循环触发与截止；TokenBudgetExceeded 走回退 | |
| 2.4 | 快照与缓存 | 分区投影快照（图片路径 as_uri 预转换，不用 compact_json 全量 dump）；内容寻址缓存键（D4） | 同数据同键/改 draft 换键/写 final 不影响键（单测）；重复定稿日志验证不重新生成 | |

### 阶段 3：管线集成（1–1.5 天）
| # | 任务 | 说明 | 验收标准 | 负责人 |
|---|---|---|---|---|
| 3.1 | _deliver_final 分流 | template=="designer" → `asyncio.wait_for(designer_chain, 600)`；**异常/超时在分支内部 catch 回退模板链（回退前过滤非法模板名），CancelledError 放行**；FinalDelivery 组装逻辑单一实现共用，新增 render_source；完成事件仍由 _deliver_final 单点发出 | 回退链实测：模拟 Designer 崩溃**与挂起超时**均仍产出模板 PDF；Cancel 立即停止 | |
| 3.2 | Designer 可视化 | 不进 SelectorGroupChat（见 §二）；Designer 状态经 StatusBus 推送「🎨 版面设计中…」「🖨 渲染 PDF…」；`models.py` WriterName 加 "designer"；eta 映射分模式：finalize 时 designer (4,8)/模板 (1,2)，STATUS_PHASE 文案分模式 | e2e 时间线可见 Designer 状态与徽章点亮 | |
| 3.3 | 前端与网关入口 | `/api/templates` 追加 designer 条目（独立伪模板元数据）；**WS template 处理特判放行 "designer"**（现经 get_template 校验会被拒，gateway/app.py:243-256）；`app.js` AGENT_NAMES 加 Designer；`index.html` 加 Designer 徽章；final 卡片显示 render_source 标注 | 前端可选、生效、可切回模板；WS 校验不拒绝 | |
| 3.4 | requirements/启动指南 | requirements.txt 加 `playwright`、`PyMuPDF`；启动指南写清 `playwright install chromium` 步骤、Edge 兜底说明、**边界声明：outputs/ 目录经 HTTP 静态可达（本机单用户设计）** | 新机器按指南可跑通 | |

### 阶段 4：验证与交付（1 天）
| # | 任务 | 说明 | 验收标准 | 负责人 |
|---|---|---|---|---|
| 4.1 | 测试套件 | **确定性链路全部无 LLM 单测**（消毒/套壳/渲染/诊断/缓存键/外循环假 factory/回退链——designer.py 与 htmlpdf.py 不 import autogen）；LLM 依赖部分单独冒烟脚本（跑 3 次取通过率） | 确定性单测全绿；冒烟成功率 ≥2/3，失败必回退成功 | |
| 4.2 | e2e 实测 | 完整对话流程（需 LLM 环境）走一次 designer 定稿 + 一次强制回退 | 两种路径都产出合法 PDF | |
| 4.3 | 样张评审 | designer 产出 vs 6 模板产出并排对比，小组评审 | 评审记录入档 | |
| 4.4 | 文档与提交 | README（架构表/依赖表/边界声明更新）、本清单勾选、分支提交推送 | 文档与实现一致 | |

## 五、风险与对策（v2 修订）

| 风险 | 等级 | 对策 |
|---|---|---|
| LLM 写 HTML 质量波动（溢出/断裂/丑陋） | 高 | D1 设计系统约束 + D3 诊断循环 + D5 回退链兜底；金样 few-shot 拉高下限 |
| **输出截断（max_tokens=8192）** | 高 | v2 新增：body 片段契约压体量至 3–6k + 哨兵检测 + 分节输出升级路径；独立 client 会绕开熔断记账，禁止裸用 |
| Token 成本与延迟上升 | 中 | D4 上下文膨胀控制五措施 + 内容寻址缓存 + 单列审计；ETA 分模式；600s 熔断 |
| 队友机器缺 Chromium 内核 | 中 | 启动指南写明安装命令；Edge CLI 零依赖兜底；再兜底回模板体系 |
| 安全（HTML 注入/外链追踪/本地文件越界） | 中 | D2 允许列表消毒 + file:// 白名单（IMAGE_DIR）+ UNC 拒绝 + data URI 类型/体积限制；渲染保持 Chromium 默认沙箱 |
| 每次运行设计不一致 | 低 | 定位为 feature（每份路书独一无二）；同数据重复定稿走缓存保持一致；需要稳定版式时用户选固定模板即可 |
| 分页控制不佳（卡片被拦腰截断） | 中 | print.css 预置 `break-inside: avoid`；inspect_render 溢出信号；金样验证 |
| **多会话并发定稿资源竞争** | 中 | v2 新增：渲染 Semaphore(1) + 每次独立 sync_playwright 上下文（sync API 线程亲和，禁止模块级单例复用）+ 产物命名含 run_id 防覆盖 |

## 六、与方向一的关系

- 方向一（6 模板体系）**全部保留**：作为默认低成本通道 + Designer 失败回退链
- 前端「路书样式」下拉统一承载两种模式：固定模板 ×6 + ✨ AI 设计师
- 课程演示卖点：同一份数据可 A/B 对比"确定性模板 vs Agent 创意设计"，正好呼应多 Agent 协同主题（Designer 经状态推送/审计全程可视化）

## 七、决策记录

- 2026-09-02：小组决定双线并行，方向二立项。渲染器 Spike（阶段 0）通过后进入开发；本文档待审批。
- 2026-09-03：多方位审核（代码库对照 + 安全健壮性 + AutoGen 集成与成本，共 24 项发现，见 §八）后修订为 v2：3.2 改确定性外循环、D2 改允许列表消毒、D4 改 body 片段契约与内容寻址缓存、D5 补回退确定性细节、新增 D7 与限额表；待 Spike 后开工。

## 八、审核问题清单（2026-09-03，24 项）

| # | 严重度 | 问题 | 修订落点 |
|---|---|---|---|
| 1 | 高 | HTML 全文 8–20k token 与全局 max_tokens=8192 自相矛盾，截断无人检测，designer 模式形同虚设 | D2 输出契约、D4 |
| 2 | 高 | Designer 进 SelectorGroupChat：selector 点不到、强制收敛打断循环、final 终止条件误触发（状态机有死锁史） | §二、3.2 |
| 3 | 高 | 回退时 template="designer" 传 build_pdf 抛 ValueError；外层崩溃抢救（_rescue_deliverables）救不回 final 缺失，违背"用户永远能拿到 PDF" | D5、3.1 |
| 4 | 高 | 消毒为黑名单描述：CSS 内 @import/url() 外链全漏、meta refresh/base/object/embed/srcset 未覆盖、正则剥标签可绕过 | D2（改允许列表） |
| 5 | 高 | file:// 无路径白名单：任意本地文件/UNC（SMB 凭据外泄）可被编进 PDF；Windows 中文路径手工拼 URI 会失败 | D2、D1 |
| 6 | 高 | Playwright sync API 线程亲和：模块级单例跨线程复用即崩；多会话并发 finalize 无信号量 | 1.4、五 |
| 7 | 高 | 渲染无超时闸门，to_thread 不可取消，挂死挂死整个 finalize | 1.4、D7（600s） |
| 8 | 中 | 缓存键 run_id+版本号三处失效（写 final 自身 +1 / 无关写入假 miss / 跨次永不命中） | D4（内容寻址） |
| 9 | 中 | 回退链降级验收缺失：Edge CLI 须独立 --user-data-dir、--no-pdf-header-footer、失败静默、非 ASCII 路径 | 0.2 |
| 10 | 中 | data: URI 无类型/体积规则，与 D6-B 冲突；data:image/svg+xml 可携带脚本 | D2 |
| 11 | 中 | <a href> 与资源加载未区分，订单链接可能被误剥或保留 javascript: scheme | D2 |
| 12 | 中 | HTML 全文在工具往返中重复 8–10×（reflect/复述/重抄），最坏 80–100K token/次 | D4 上下文控制 |
| 13 | 中 | 独立 model client 绕开 500K 熔断记账（total_usage 只读共享单例）；外循环不在 _stream_team 内无逐轮检查 | D4、D7 |
| 14 | 中 | finalize 无整体超时，ETA 恒 (1,2) 与 designer 实际延迟不符 | D7、3.2 |
| 15 | 中 | PyMuPDF 未进 requirements；inspect 依赖它做文本诊断 | 3.4 |
| 16 | 中 | write_html 落盘无 charset/UTF-8 保证，中文乱码烧重试预算 | D2（套壳保证） |
| 17 | 中 | 资源限额全无量化（HTML 大小/图片数/SVG 复杂度/重试预算共享） | D2 限额表、D6 |
| 18 | 中 | FinalDelivery 若自建组装易漏 orders/pdf_url 约定；页脚标注缺注入方式 | 3.1、D5 |
| 19 | 中 | 金样 2 份全注入 = 每调用 +5–10K token 隐性成本 | D1（注入 1 份） |
| 20 | 中 | 前端 6 处漏改：WriterName/AGENT_NAMES/agent-chip/eta 映射/WS template 校验/渲染线程化 | 3.2、3.3 |
| 21 | 低 | 视觉自查与 vision=False 冲突 | D3（砍掉） |
| 22 | 低 | Designer 输入复用 compact_json 会带 plan_input 数据副本与 changelog，多付 2–4K token/轮 | 2.4 |
| 23 | 低 | outputs/ 经 HTTP 静态可达需边界声明；_download_image 无大小上限（存量问题，顺带修） | 3.4、D2 |
| 24 | 低 | 消毒拦截与意外资源错误不分会导致诊断误判重试 | D3 |
