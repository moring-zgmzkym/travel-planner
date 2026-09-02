/* TripMate 前端：WebSocket 双向通信 + 状态时间线 + 草稿/成品渲染（原生 JS，§3.7） */
"use strict";

const $ = (id) => document.getElementById(id);
const chat = $("chat"), timeline = $("timeline"), input = $("input"), sendBtn = $("send");

let ws = null;
let pingTimer = null;
let busyTimer = null;
let busy = false; // Chatter 处理中（等待回复期间禁止重复发送）
let reconnected = false; // 是否发生过断线重连（首页首连不提示）
let sid = localStorage.getItem("tm_sid") || "default"; // 当前会话（需求 2 对话管理）
let etaRange = null; // 当前阶段预计耗时 [下限, 上限] 分钟（STATUS_PHASE 锚定）

const AGENT_NAMES = {
  Chatter: "聊天管家", InformationProcessor: "信息处理", Researcher: "信息收集",
  BookingButler: "MCP 专项", Planner: "计划规划", Designer: "版面设计", TeamRunner: "调度器",
};

/* ---------- WebSocket ---------- */
function connect() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  ws = new WebSocket(`${proto}://${location.host}/ws?sid=${encodeURIComponent(sid)}`);
  ws.onopen = () => {
    $("conn-dot").classList.add("on");
    if (pingTimer) clearInterval(pingTimer);
    pingTimer = setInterval(() => {  // 心跳 30s（§2.3）
      if (ws.readyState === 1) ws.send(JSON.stringify({ type: "ping", ts: Date.now() }));
    }, 25000);
    if (reconnected) {  // 断线重连：解除"思考中"死锁（回复可能已随断线丢失，2026-08-30）
      setBusy(false);
      addTimeline("System", "INFO", "连接已恢复，若刚发送的消息没有响应，请重新发送一次。");
    }
    reconnected = true;
    refreshSessions();
  };
  ws.onclose = () => {
    $("conn-dot").classList.remove("on");
    setTimeout(connect, 2000);  // 自动重连 + 服务端补发（风险 #7）
  };
  ws.onmessage = (e) => handleMsg(JSON.parse(e.data));
}

function sendMsg(text) {
  if (ws && ws.readyState === 1) ws.send(JSON.stringify({ type: "chat", text }));
}

/* ---------- 消息处理 ---------- */
/* ---------- 会话管理（需求 2） ---------- */
async function refreshSessions() {
  try {
    const r = await fetch("/api/sessions");
    const list = await r.json();
    const sel = $("session-select");
    sel.innerHTML = "";
    for (const it of list) {
      const opt = document.createElement("option");
      opt.value = it.sid;
      opt.textContent = it.title;
      sel.appendChild(opt);
    }
    sel.value = sid;
    if (sel.selectedIndex < 0) { sel.value = "default"; sid = "default"; }
  } catch (e) { /* 列表刷新失败不影响主流程 */ }
}

function switchSession(nextSid) {
  if (nextSid === sid) return;
  sid = nextSid;
  localStorage.setItem("tm_sid", sid);
  $("chat").innerHTML = "";
  const div = document.createElement("div");
  div.className = "msg sys";
  div.textContent = "已切换对话。该对话的规划进展与成果如下方所示（历史消息不跨对话保留）。";
  $("chat").appendChild(div);
  $("timeline").innerHTML = "";
  hideEtaChip(); // 新会话无运行中阶段
  setBusy(false);
  if (ws) ws.close(); // onclose 自动以新 sid 重连，服务端补播该会话状态
}

async function createSession() {
  try {
    const r = await fetch("/api/sessions", { method: "POST" });
    const it = await r.json();
    await refreshSessions();
    switchSession(it.sid);
  } catch (e) { addTimeline("System", "STATUS_ERROR", "新对话创建失败，请重试。"); }
}

/* ---------- 路书样式（PDF 模板选择） ---------- */
async function loadTemplates() {
  try {
    const r = await fetch("/api/templates");
    const data = await r.json();
    const sel = $("template-select");
    const current = sel.value;
    sel.innerHTML = "";
    for (const t of data.templates || []) {
      const opt = document.createElement("option");
      opt.value = t.name;
      opt.textContent = `🎨 路书样式：${t.display_name}`;
      opt.title = `${t.description}（适用：${t.scenes}）`;
      sel.appendChild(opt);
    }
    sel.value = current || "classic";
  } catch (e) { /* 模板列表加载失败保留默认选项 */ }
}

function chooseTemplate(name) {
  if (ws && ws.readyState === 1) ws.send(JSON.stringify({ type: "template", name }));
}

function handleMsg(m) {
  if (m.type === "session") {
    sid = m.sid;
    localStorage.setItem("tm_sid", sid);
    const sel = $("session-select");
    if (sel.value !== sid) { refreshSessions(); }
    return;
  }
  if (m.type === "status" || m.type === "AGENT_MESSAGE") {
    addTimeline(m.agent, m.kind || m.type, m.text);
    flashAgent(m.agent);
    updateEtaChip(m);
  } else if (m.type === "chat") {
    if (m.role === "user") return; // 客户端已乐观渲染，跳过回显
    addChat(m.role, m.text);
    setBusy(false);
  } else if (m.type === "draft") {
    addDraft(m.html);
  } else if (m.type === "final") {
    addFinal(m);
    renderOrders(m.orders, m.total_price);
    hideEtaChip(); // 成品已到达，预计等待结束
  } else if (m.type === "profile") {
    renderProfile(m.profile);
  } else if (m.type === "usage") {
    renderUsage(m.usage);
  }
  // "pong" 忽略
}

/* ---------- 渲染 ---------- */
function addChat(role, text) {
  const div = document.createElement("div");
  div.className = "msg " + role;
  div.textContent = text;
  chat.appendChild(div);
  chat.scrollTop = chat.scrollHeight;
}

function addTimeline(agent, kind, text) {
  const div = document.createElement("div");
  div.className = "tl-item k-" + kind;
  div.innerHTML = `<span class="tl-time">${now()}</span>
    <div class="tl-body">
      <div class="tl-agent a-${agent}">${AGENT_NAMES[agent] || agent}</div>
      <div class="tl-text"></div>
    </div>`;
  div.querySelector(".tl-text").textContent = text;
  timeline.prepend(div);
  while (timeline.children.length > 120) timeline.lastChild.remove();
}

function addDraft(html) {
  document.querySelectorAll(".draft-card").forEach((n) => n.remove());
  const div = document.createElement("div");
  div.className = "draft-card";
  div.innerHTML = html;
  chat.appendChild(div);
  chat.scrollTop = chat.scrollHeight;
}

function addFinal(m) {
  document.querySelectorAll(".final-card").forEach((n) => n.remove());
  const srcNote = m.render_source === "designer" ? "✨ AI 设计师排版"
    : m.render_source && m.render_source.startsWith("template") ? "🎨 模板排版"
    : "";
  const srcHtml = srcNote ? `<span class="chip chip-ok" style="margin-left:8px">${srcNote}</span>` : "";
  const div = document.createElement("div");
  div.className = "final-card";
  div.innerHTML = `<div class="final-title">🎉 行程计划已生成${srcHtml}</div>
    <a class="btn-pdf" href="${m.pdf_url}" target="_blank">📄 打开 PDF 行程计划</a>
    <div style="font-size:12px;color:var(--sub)">已勾选订单合计约 ${m.total_price} 元，请在官方渠道逐项确认并支付。</div>`;
  chat.appendChild(div);
  chat.scrollTop = chat.scrollHeight;
}

function renderProfile(p) {
  $("profile-ver").textContent = "v" + (p.version || 0);
  const b = p.basic_info || {}, d = p.detail_info || {};
  const rows = [];
  const kv = (k, v) => v ? rows.push(`<span class="k">${k}</span> ${esc(String(v))}`) : null;
  kv("出发地", b.origin); kv("目的地", b.destination); kv("天数", b.days);
  kv("日期", (b.travel_dates || []).join(" ~ ") || b.date_text);
  kv("方式", b.travel_mode); kv("风格", (b.style || []).join("/"));
  kv("人数", b.party_size); kv("预算", b.budget); kv("预算上限", b.budget_max);
  if (d.hotel && (d.hotel.location_pref || (d.hotel.price_range || []).length))
    rows.push(`<span class="k">酒店</span> ${esc(d.hotel.location_pref || "")} ${(d.hotel.price_range || []).join("-")}元/晚`);
  kv("必去", (d.must_visit || []).join("、"));
  kv("忌口", (d.food_restrictions || []).join("、"));
  kv("节奏", d.pace);
  if ((b.defaults_applied || []).length)
    rows.push(`<div style="color:var(--warn);font-size:11px">默认值：${esc(b.defaults_applied.join("；"))}</div>`);
  $("profile-body").innerHTML = rows.length ? rows.join("<br>") : "等待信息录入…";
}

function renderOrders(orders, total) {
  $("orders-panel").style.display = "";
  $("orders-total").textContent = `合计 ¥${total}`;
  $("orders-body").innerHTML = (orders || []).map((o) => `
    <div class="order-item ${o.selected ? "sel" : ""}">
      <span class="o-type">${o.type}</span>${o.selected ? " <span class='chip chip-ok'>✅ 已勾选</span>" : ""}
      <span class="o-amount">¥${o.amount}</span>
      <div class="o-name">${esc(o.name)}</div>
      ${o.reference_only ? "<div class='ref-tag'>⚠ 参考值（降级数据）</div>" : ""}
      ${o.reason ? `<div class="o-reason">${esc(o.reason)}</div>` : ""}
      ${o.link ? `<a href="${esc(o.link)}" target="_blank">直达链接 ↗</a>` : ""}
    </div>`).join("");
}

function renderUsage(u) {
  if (!u) return;
  const pct = Math.min(100, (u.total_tokens / (u.limit || 500000)) * 100);
  $("usage-fill").style.width = pct + "%";
  $("usage-text").textContent = `${u.total_tokens.toLocaleString()} / ${(u.limit / 1000) + "K"}（${pct.toFixed(1)}%）`;
}

function flashAgent(name) {
  const chip = document.querySelector(`.agent-chip[data-agent="${name}"]`);
  if (!chip) return;
  chip.classList.add("on");
  clearTimeout(chip._t);
  // 35s 熄灭（> 服务端 30s 心跳 HEARTBEAT_S）：团队运行期间心跳持续续亮，徽章不闪灭
  chip._t = setTimeout(() => chip.classList.remove("on"), 35000);
}

/* ETA chip：STATUS_PHASE（含检查点重跑）锚定预计区间，STATUS_PROGRESS 刷新已进行，
   终态（完成/停止/错误）或成品卡片到达即隐藏。chatter 的 STATUS_PHASE 无 eta 字段，容忍缺失。 */
function updateEtaChip(m) {
  const chip = $("eta-chip");
  if (!chip) return;
  if (m.kind === "STATUS_COMPLETED" || m.kind === "STATUS_CANCELLED" || m.kind === "STATUS_ERROR") {
    etaRange = null;
    chip.style.display = "none";
    return;
  }
  if (Array.isArray(m.eta_min)) etaRange = m.eta_min;
  if (!etaRange) return;
  const elapsedMin = typeof m.elapsed_s === "number" ? Math.round(m.elapsed_s / 60) : null;
  const etaText = etaRange[0] === etaRange[1] ? `约 ${etaRange[0]} 分钟` : `${etaRange[0]}-${etaRange[1]} 分钟`;
  chip.textContent = `⏱ 预计 ${etaText}` + (elapsedMin !== null ? ` · 已进行 ${elapsedMin} 分` : "");
  chip.style.display = "";
}

function hideEtaChip() {
  etaRange = null;
  const chip = $("eta-chip");
  if (chip) chip.style.display = "none";
}

function setBusy(v) {
  busy = v;
  sendBtn.disabled = v;
  if (busyTimer) { clearInterval(busyTimer); busyTimer = null; }
  if (v) {
    const start = Date.now();
    const tick = () => {
      const sec = Math.round((Date.now() - start) / 1000);
      sendBtn.textContent = sec >= 60 ? `思考中… ${Math.floor(sec / 60)}分${sec % 60}秒` : `思考中… ${sec}秒`;
    };
    tick();
    busyTimer = setInterval(tick, 1000);
  } else {
    sendBtn.textContent = "发送";
  }
}

/* 停止规划（不走 LLM，服务端即时生效） */
$("stop-plan").addEventListener("click", () => {
  if (ws && ws.readyState === 1) ws.send(JSON.stringify({ type: "stop" }));
});

function now() {
  return new Date().toTimeString().slice(0, 8);
}
function esc(s) {
  return s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

/* ---------- 发送 ---------- */
function doSend() {
  const text = input.value.trim();
  if (!text || busy || !ws || ws.readyState !== 1) return;
  input.value = "";
  setBusy(true);
  sendMsg(text);
  addChat("user", text); // 乐观渲染（服务端回显时去重）
}
sendBtn.addEventListener("click", doSend);
input.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); doSend(); }
});

$("session-select").addEventListener("change", (e) => switchSession(e.target.value));
$("new-session").addEventListener("click", createSession);
$("template-select").addEventListener("change", (e) => chooseTemplate(e.target.value));

loadTemplates();
connect();
