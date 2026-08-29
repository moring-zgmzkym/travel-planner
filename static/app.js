/* TripMate 前端：WebSocket 双向通信 + 状态时间线 + 草稿/成品渲染（原生 JS，§3.7） */
"use strict";

const $ = (id) => document.getElementById(id);
const chat = $("chat"), timeline = $("timeline"), input = $("input"), sendBtn = $("send");

let ws = null;
let pingTimer = null;
let busyTimer = null;
let busy = false; // Chatter 处理中（等待回复期间禁止重复发送）

const AGENT_NAMES = {
  Chatter: "聊天管家", InformationProcessor: "信息处理", Researcher: "信息收集",
  BookingButler: "MCP 专项", Planner: "计划规划", TeamRunner: "调度器",
};

/* ---------- WebSocket ---------- */
function connect() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  ws = new WebSocket(`${proto}://${location.host}/ws`);
  ws.onopen = () => {
    $("conn-dot").classList.add("on");
    if (pingTimer) clearInterval(pingTimer);
    pingTimer = setInterval(() => {  // 心跳 30s（§2.3）
      if (ws.readyState === 1) ws.send(JSON.stringify({ type: "ping", ts: Date.now() }));
    }, 25000);
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
function handleMsg(m) {
  if (m.type === "status" || m.type === "AGENT_MESSAGE") {
    addTimeline(m.agent, m.kind || m.type, m.text);
    flashAgent(m.agent);
  } else if (m.type === "chat") {
    if (m.role === "user") return; // 客户端已乐观渲染，跳过回显
    addChat(m.role, m.text);
    setBusy(false);
  } else if (m.type === "draft") {
    addDraft(m.html);
  } else if (m.type === "final") {
    addFinal(m);
    renderOrders(m.orders, m.total_price);
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
  const div = document.createElement("div");
  div.className = "final-card";
  div.innerHTML = `<div class="final-title">🎉 行程计划已生成</div>
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
  const pct = Math.min(100, (u.total_tokens / (u.limit || 200000)) * 100);
  $("usage-fill").style.width = pct + "%";
  $("usage-text").textContent = `${u.total_tokens.toLocaleString()} / ${(u.limit / 1000) + "K"}（${pct.toFixed(1)}%）`;
}

function flashAgent(name) {
  const chip = document.querySelector(`.agent-chip[data-agent="${name}"]`);
  if (!chip) return;
  chip.classList.add("on");
  clearTimeout(chip._t);
  chip._t = setTimeout(() => chip.classList.remove("on"), 4000);
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

connect();
