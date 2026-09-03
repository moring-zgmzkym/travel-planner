"""10 段组件素材（Designer 提示词的 few-shot 素材，docs/html-designer-plan.md 任务 1.2）。

每个片段与 print.css 的类名一一对应；数据均为示意，Agent 用黑板快照数据仿写。
"""

COMPONENT_FRAGMENTS = """【组件片段库（print.css 类名已配好，按快照数据仿写，不要自创类名）】

1) 封面英雄区（必须第一个组件）：
<section class="cover">
  <div class="cover-kicker">TRIPMATE 行程规划书</div>
  <h1 class="cover-title">成都 · 三日慢游</h1>
  <p class="cover-sub">2026-10-01 → 2026-10-03 · 高铁往返 · 2 人 · 休闲节奏</p>
  <img class="cover-img" src="{封面图file_uri}" alt="封面配图（示意/实拍按来源标注）">
  <div class="cover-meta"><span class="tag">预算 ¥5000</span><span class="tag">必去：大熊猫基地</span></div>
</section>

2) 行程总览表：
<section class="sec"><h2>行程总览</h2>
<table class="ov-table"><thead><tr><th>日期</th><th>上午</th><th>下午</th><th>晚上</th></tr></thead>
<tbody>
<tr><td>10-01 周四</td><td>乘 D636 赴蓉</td><td>入住春熙路</td><td>太古里夜景</td></tr>
</tbody></table></section>

3) 订单卡（车票/酒店各一，reference_only=true 时名称后加 <span class="ref-tag">参考值</span>）：
<div class="ord-card"><div class="ord-head"><div><span class="ord-type">车票</span>
  <span class="ord-name">D636 上海虹桥→成都东</span></div>
  <span class="ord-price">¥1218</span></div>
  <div class="ord-meta">09:15 出发 · 22:40 到达 · 往返 × 2 人</div>
  <div class="ord-reason">推荐理由：出发时间黄金窗口，价格最低</div></div>

4) 逐日行程卡（每天一张）：
<div class="day-card"><div class="day-head"><span class="day-badge">D1</span>
  <span class="day-date">2026-10-01 周四</span></div>
  <div class="day-body">
    <div class="time-row"><span class="time-label">上午</span><span class="time-text">乘 D636 高铁赴蓉</span></div>
    <div class="spots"><span class="spot-chip">春熙路太古里</span><span class="spot-chip">大熊猫基地</span></div>
  </div></div>

5) 垂直时间线（景点亮点/贴士）：
<div class="tl"><div class="tl-item"><span class="tl-dot"></span>
  <div class="tl-title">大熊猫基地</div>
  <div class="tl-text">开园即入，上午熊猫最活跃；地铁 3 号线熊猫大道站转景区摆渡车</div></div></div>

6) 预算图表（inline SVG 环图 + 明细表；数据只能来自快照 budget）：
<div class="budget-wrap">
<svg width="100%" height="150" viewBox="0 0 480 150">
  <circle cx="75" cy="75" r="52" fill="none" stroke="#e3e6ee" stroke-width="22"/>
  <circle cx="75" cy="75" r="52" fill="none" stroke="#2f6fed" stroke-width="22"
          stroke-dasharray="164 327" transform="rotate(-90 75 75)"/>
  <text x="75" y="80" text-anchor="middle" font-size="17" font-weight="bold" fill="#23232a">¥4981</text>
  <g font-size="11" fill="#3a3f4b">
    <rect x="180" y="34" width="12" height="12" rx="3" fill="#2f6fed"/><text x="200" y="44">交通 ¥1218</text>
    <rect x="180" y="60" width="12" height="12" rx="3" fill="#6c4de0"/><text x="200" y="70">住宿 ¥976</text>
    <rect x="180" y="86" width="12" height="12" rx="3" fill="#22a06b"/><text x="200" y="96">门票/餐饮/备用金 ¥2787</text>
  </g>
</svg>
<table class="budget-table"><tbody>
<tr><td>交通</td><td>D636 往返 × 2 人</td><td>¥1218</td></tr>
</tbody></table>
<p class="budget-note">注：门票与餐饮为预算口径估算，以实际为准。</p></div>

7) 实景图墙（图片 src 只用快照 images[].uri）：
<section class="sec"><h2>实景预览</h2>
<div class="wall"><figure><img src="{图1file_uri}" alt="大熊猫基地">
  <figcaption>大熊猫基地 · 实拍</figcaption></figure></div></section>

8) 酒店卡（勾选酒店，含宣传图与评价摘要）：
<div class="hotel-card"><img src="{酒店图file_uri}" alt="酒店宣传图">
  <div class="hotel-info"><div class="hotel-name">亚朵酒店（天府广场店）</div>
  <div class="hotel-facts"><span>¥488/晚</span><span>距市中心 0.6km</span><span>评分 4.8</span></div>
  <div class="hotel-review">住客评价摘要…</div></div></div>

9) 美食卡（左图右文；无实拍图时用 <div class="food-img-missing">示意</div>）：
<div class="food-card"><img src="{美食图file_uri}" alt="火锅">
  <div class="food-info"><div class="food-name">成都火锅</div>
  <div class="food-text">牛油九宫格配香油蒜泥碟，微辣起试；本地人推荐午后排队更短</div></div></div>

10) 结尾页（必须最后一个组件，后接系统页脚）：
<section class="ending"><div class="big">祝旅途愉快 🎒</div>
<p class="small">订单请在官方渠道逐项确认并支付 · 价格与班次以出票时为准</p></section>
"""
