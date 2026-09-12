/* TripMate 前端：WebSocket 双向通信 + 状态时间线 + 草稿/成品渲染（原生 JS，§3.7）
   多用户版（2026-09-12）：登录/注册、令牌鉴权、30s WS 票据、PDF/分享/偏好记忆 */
"use strict";

const $ = (id) => document.getElementById(id);
const chat = $("chat"), timeline = $("timeline"), input = $("input"), sendBtn = $("send");

let ws = null;
let pingTimer = null;
let reconnectTimer = null;
let reconnectDelay = 2000; // 意外断线的重连退避：2s 起 ×2 递增，30s 封顶（连接成功后重置）
let manualClose = false;   // 主动断开（切换会话）：不走退避，立即重连
let missedPongs = 0;       // 连续未收到 pong 的心跳次数（空闲期 ≥2 判定半开连接，主动重连）
let outbox = [];           // 离线待发消息 [{sid, text}]：断连期间发送不再静默丢弃，重连后自动补发
let welcomeNode = null;    // 欢迎/示例输入（进入页面时保存引用，清空聊天面板后统一放回——示例是使用指导）
let busyTimer = null;
let busy = false; // Chatter 处理中（等待回复期间禁止重复发送）
let reconnected = false; // 是否发生过断线重连（首页首连不提示）
let sid = localStorage.getItem("tm_sid") || "default"; // 当前会话（每个账号独立命名空间）
let etaRange = null; // 当前阶段预计耗时 [下限, 上限] 分钟（STATUS_PHASE 锚定）
let token = localStorage.getItem("tm_token") || ""; // 登录令牌（多用户版）
let me = null;
let authMode = "login";

const AGENT_NAMES = {
  Chatter: "聊天管家", InformationProcessor: "信息处理", Researcher: "信息收集",
  BookingButler: "MCP 专项", Planner: "计划规划", TeamRunner: "调度器",
};

/* ---------- 认证（多用户版） ---------- */
function showLogin(msg) {
  manualClose = true;
  if (ws) { try { ws.close(); } catch (e) { /* 已断开 */ } }
  ws = null;
  $("login-overlay").style.display = "flex";
  $("auth-error").textContent = msg || "";
}

function hideLogin() {
  $("login-overlay").style.display = "none";
  $("auth-error").textContent = "";
}

async function authFetch(url, opts = {}) {
  opts.headers = Object.assign({}, opts.headers || {},
    token ? { Authorization: `Bearer ${token}` } : {});
  const r = await fetch(url, opts);
  if (r.status === 401) {
    token = "";
    localStorage.removeItem("tm_token");
    showLogin("登录已过期，请重新登录");
    throw new Error("401");
  }
  return r;
}

async function submitAuth() {
  const u = $("auth-username").value.trim(), p = $("auth-password").value;
  if (!u || !p) { $("auth-error").textContent = "请输入用户名和密码"; return; }
  $("auth-submit").disabled = true;
  try {
    const r = await fetch(`/api/${authMode}`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ username: u, password: p }),
    });
    const data = await r.json().catch(() => ({}));
    if (r.status !== 200) { $("auth-error").textContent = data.detail || "操作失败，请重试"; return; }
    token = data.token;
    localStorage.setItem("tm_token", token);
    hideLogin();
    await boot();
  } finally {
    $("auth-submit").disabled = false;
  }
}

function setAuthMode(m) {
  authMode = m;
  $("tab-login").classList.toggle("on", m === "login");
  $("tab-register").classList.toggle("on", m === "register");
  $("auth-submit").textContent = m === "login" ? "登录" : "注册";
  $("auth-error").textContent = "";
}

async function boot() {
  if (!token) { showLogin(); return; }
  let r;
  try {
    r = await authFetch("/api/me");
  } catch (e) { return; } // 401 已弹登录
  if (r.status !== 200) { showLogin(); return; }
  me = await r.json();
  $("user-name").textContent = me.username + (me.is_admin ? "（管理员）" : "");
  $("user-bar").style.display = "";
  refreshMemory();
  connect();
}

/* ---------- WebSocket ---------- */
async function connect() {
  if (!token) { showLogin(); return; }
  let ticket = "";
  try {
    const r = await authFetch("/api/ws-ticket", { method: "POST" }); // 30s 短时票据：长期令牌不进 URL
    if (r.status !== 200) return;
    ticket = (await r.json()).ticket;
  } catch (e) { return; } // 401 已弹登录
  const proto = location.protocol === "https:" ? "wss" : "ws";
  ws = new WebSocket(`${proto}://${location.host}/ws?sid=${encodeURIComponent(sid)}&ticket=${encodeURIComponent(ticket)}`);
  ws.onopen = () => {
    $("conn-dot").classList.add("on");
    const chatEl = $("chat");
    if (!welcomeNode) welcomeNode = chatEl.querySelector(".msg.sys");  // 首次保存欢迎/示例引用
    chatEl.innerHTML = "";  // 服务端将全量补播（状态+聊天历史），先清面板防重连重复渲染
    $("timeline").innerHTML = "";  // 同上：时间线由状态补播重建（修软重连重复叠加）
    if (welcomeNode) chatEl.appendChild(welcomeNode);  // 示例放回头部，补播消息接在其后
    reconnectDelay = 2000;  // 连接成功：重置退避
    missedPongs = 0;
    if (pingTimer) clearInterval(pingTimer);
    pingTimer = setInterval(() => {  // 心跳 25s（§2.3）
      if (!ws || ws.readyState !== 1) return;
      // 半开连接检测：空闲期连续 2 次（~50s）未收到 pong → 主动断开走重连。
      // busy（等待聊天回复）期间豁免：服务端接收循环被处理阻塞，pong 会积压到回合结束才回。
      if (!busy && missedPongs >= 2) {
        addTimeline("System", "STATUS_ERROR", "连接长时间无响应，正在重新连接…");
        ws.close();
        return;
      }
      missedPongs++;
      ws.send(JSON.stringify({ type: "ping", ts: Date.now() }));
    }, 25000);
    if (reconnected) {  // 断线重连：解除"思考中"死锁（回复可能已随断线丢失，2026-08-30）
      setBusy(false);
      addTimeline("System", "INFO", "连接已恢复，若刚发送的消息没有响应，请重新发送一次。");
    }
    reconnected = true;
    refreshSessions();
  };
  ws.onclose = (e) => {
    $("conn-dot").classList.remove("on");
    if (pingTimer) { clearInterval(pingTimer); pingTimer = null; }
    if (e.code === 4401) {  // 未认证：票据/令牌失效 → 停自动重连，弹登录
      token = "";
      localStorage.removeItem("tm_token");
      showLogin("登录状态已失效，请重新登录");
      return;
    }
    const wasManual = manualClose;
    manualClose = false;
    const delay = wasManual ? 0 : reconnectDelay;
    reconnectDelay = wasManual ? 2000 : Math.min(reconnectDelay * 2, 30000); // 指数退避（成功后重置）
    if (reconnectTimer) clearTimeout(reconnectTimer);
    reconnectTimer = setTimeout(connect, delay);  // 自动重连 + 服务端补发（风险 #7）
  };
  ws.onmessage = (e) => {
    try { handleMsg(JSON.parse(e.data)); }
    catch (err) { /* 单条非 JSON 帧丢弃，不断链 */ }
  };
}

function sendMsg(text) {
  if (ws && ws.readyState === 1) ws.send(JSON.stringify({ type: "chat", text }));
}

/* ---------- 消息处理 ---------- */
/* ---------- 会话管理（需求 2） ---------- */
async function refreshSessions() {
  try {
    const r = await authFetch("/api/sessions");
    if (!r.ok) return;
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
  const chatEl = $("chat");
  if (!welcomeNode) welcomeNode = chatEl.querySelector(".msg.sys");
  chatEl.innerHTML = "";
  if (welcomeNode) chatEl.appendChild(welcomeNode);  // 示例是使用指导：切换会话也要保留（2026-09-05）
  const div = document.createElement("div");
  div.className = "msg sys";
  div.textContent = "已切换对话。该对话的规划进展与成果如下方所示（历史消息不跨对话保留）。";
  chatEl.appendChild(div);
  $("timeline").innerHTML = "";
  hideEtaChip(); // 新会话无运行中阶段
  resetSubLights(); // 新会话灯回灰（防上一会话的状态串灯；重连补播会按序还原本会话灯态）
  setBusy(false);
  if (ws) { manualClose = true; ws.close(); } // 主动断开：立即以新 sid 重连（不走退避），服务端补播该会话状态
}

async function createSession() {
  try {
    const r = await authFetch("/api/sessions", { method: "POST" });
    if (!r.ok) return;
    const it = await r.json();
    await refreshSessions();
    switchSession(it.sid);
  } catch (e) { addTimeline("System", "STATUS_ERROR", "新对话创建失败，请重试。"); }
}

/* ---------- 偏好记忆（多用户版） ---------- */
async function refreshMemory() {
  try {
    const r = await authFetch("/api/memory");
    if (!r.ok) return;
    const data = await r.json();
    const prefs = data.preferences || [], trips = data.trips || [];
    const body = $("memory-body");
    if (!prefs.length && !trips.length) {
      body.innerHTML = '<span class="memory-empty">暂无沉淀，完成一次规划后自动提炼</span>';
      return;
    }
    body.innerHTML = prefs.map((p) =>
      `<div class="memory-item"><span>${esc(p.text)}</span>` +
      `<button class="mini-btn mem-del" data-id="${esc(p.id)}" title="删除该条">✕</button></div>`).join("")
      + trips.slice(0, 5).map((t) =>
        `<div class="memory-trip">🧭 ${esc(t.destination || "行程")} · ${esc(t.dates || "")}</div>`).join("");
    body.querySelectorAll(".mem-del").forEach((b) => b.addEventListener("click", async () => {
      await authFetch(`/api/memory/${b.dataset.id}`, { method: "DELETE" });
      refreshMemory();
    }));
  } catch (e) { /* 记忆面板失败不影响主流程 */ }
}

/* ---------- PDF 分享（多用户版） ---------- */
async function makeShare(card) {
  try {
    const r = await authFetch("/api/share", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ sid }),
    });
    const data = await r.json().catch(() => ({}));
    if (r.status !== 200) { addTimeline("System", "STATUS_ERROR", data.detail || "分享生成失败"); return; }
    const abs = location.origin + data.url;
    const row = card.querySelector(".share-row");
    row.style.display = "";
    row.innerHTML = `<input class="share-input" readonly value="${esc(abs)}">` +
      `<button class="mini-btn share-copy">复制</button>` +
      `<button class="mini-btn share-revoke">撤销</button>`;
    row.querySelector(".share-copy").addEventListener("click", (e) => {
      navigator.clipboard && navigator.clipboard.writeText(abs);
      e.target.textContent = "已复制";
    });
    row.querySelector(".share-revoke").addEventListener("click", async () => {
      await authFetch(`/api/share/${data.share_id}`, { method: "DELETE" });
      row.style.display = "none";
    });
  } catch (e) { /* 401 已弹登录；其余静默 */ }
}

/* 路书样式说明：定稿 PDF 统一为 HTML 唐风夜色路书（tripmate/pdf_html），
   渲染失败自动降级 reportlab cartoon——不再提供前端样式选择（2026-09-06）。 */

function handleMsg(m) {
  if (m.type === "session") {
    sid = m.sid;
    localStorage.setItem("tm_sid", sid);
    const sel = $("session-select");
    if (sel.value !== sid) { refreshSessions(); }
    flushOutbox();  // 补播完成标记（session 在聊天历史之后发送）：此刻补发离线消息，DOM 顺序正确
    return;
  }
  if (m.type === "status" && m.kind === "STATUS_SUBAGENT") {
    // Subagent 指示灯专用事件：只驱动灯（黄=运行中/绿=完成/红=失败），不进时间线
    // ——查询过程叙述由 STATUS_COLLECT/STATUS_MCP 文本承担（2026-09-05）
    setSubLight(m.channel, m.state);
    return;
  }
  if (m.type === "status" || m.type === "AGENT_MESSAGE") {
    if (m.kind === "STATUS_PHASE") resetSubLights(); // 新阶段/检查点重跑：灯回灰，随查询重新点亮
    addTimeline(m.agent, m.kind || m.type, m.text);
    flashAgent(m.agent);
    updateEtaChip(m);
  } else if (m.type === "chat") {
    if (m.role === "user" && !m.replay) return; // 实时回显跳过（乐观渲染已画）；补播的需重画（刷新后 DOM 已重置）
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
  } else if (m.type === "pong") {
    missedPongs = 0; // 半开连接检测：收到 pong 即视为链路健康
  }
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
      <div class="tl-agent a-${esc(agent)}">${esc(AGENT_NAMES[agent] || agent)}</div>
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
  const pdfHref = esc(m.pdf_url) + (m.pdf_url.includes("?") ? "&" : "?")
    + "token=" + encodeURIComponent(token); // <a> 导航无法带 Authorization 头，/api/pdf 兼容 query 令牌
  div.innerHTML = `<div class="final-title">🎉 行程计划已生成</div>
    <a class="btn-pdf" href="${pdfHref}" target="_blank">📄 打开 PDF 行程计划</a>
    <button class="btn-share">🔗 生成分享链接</button>
    <div class="share-row" style="display:none"></div>
    <div class="final-note">已勾选订单合计约 ${esc(m.total_price)} 元，请在官方渠道逐项确认并支付。</div>`;
  div.querySelector(".btn-share").addEventListener("click", () => makeShare(div));
  chat.appendChild(div);
  chat.scrollTop = chat.scrollHeight;
  refreshMemory(); // 定稿后偏好记忆可能已沉淀
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
      <span class="o-type">${esc(o.type)}</span>${o.selected ? " <span class='chip chip-ok'>✅ 已勾选</span>" : ""}
      <span class="o-amount">¥${esc(o.amount)}</span>
      <div class="o-name">${esc(o.name)}</div>
      ${o.reference_only ? "<div class='ref-tag'>⚠ 参考值（降级数据）</div>" : ""}
      ${o.reason ? `<div class="o-reason">${esc(o.reason)}</div>` : ""}
      ${safeLink(o.link) ? `<a href="${safeLink(o.link)}" target="_blank" rel="noopener">直达链接 ↗</a>` : ""}
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

/* ---------- Subagent 指示灯（2026-09-05）：黄=运行中 / 绿=完成 / 红=失败 ---------- */
const SUB_PARENT = { guides: "Researcher", tickets: "BookingButler", hotels: "BookingButler", weather: "BookingButler", route: "BookingButler" };
const subStates = {}; // channel → "running" | "done" | "failed"

function setSubLight(channel, state) {
  if (!channel || !["guides", "tickets", "hotels", "weather", "route"].includes(channel)) {
    return; // 未知通道忽略（前向兼容：服务端新增通道前端不炸）
  }
  subStates[channel] = state;
  const chip = document.querySelector(`.sub-light[data-channel="${channel}"]`);
  if (chip) {
    chip.classList.remove("st-running", "st-done", "st-failed");
    if (state === "running") chip.classList.add("st-running");
    else if (state === "done") chip.classList.add("st-done");
    else if (state === "failed") chip.classList.add("st-failed");
  }
  // 父 Agent 徽章联动：本父任一通道运行中 → 黄（点亮）；其全部已运行通道完成 → 绿
  const parent = SUB_PARENT[channel];
  if (parent) {
    const mine = Object.entries(subStates).filter(([c]) => SUB_PARENT[c] === parent).map(([, s]) => s);
    const pchip = document.querySelector(`.agent-chip[data-agent="${parent}"]`);
    if (pchip) {
      pchip.classList.remove("done");
      if (mine.length && mine.every((s) => s === "done")) pchip.classList.add("done");
      else if (state === "running") flashAgent(parent);
    }
  }
}

function resetSubLights() {
  // 新阶段/检查点重跑/终态：灯回灰 + 清父徽章完成绿（防止上一阶段的残留状态误导）
  for (const ch of Object.keys(subStates)) delete subStates[ch];
  document.querySelectorAll(".sub-light").forEach((c) => c.classList.remove("st-running", "st-done", "st-failed"));
  document.querySelectorAll(".agent-chip.done").forEach((c) => c.classList.remove("done"));
}

/* ETA chip：STATUS_PHASE（含检查点重跑）锚定预计区间，STATUS_PROGRESS 刷新已进行，
   终态（完成/停止/错误）或成品卡片到达即隐藏。chatter 的 STATUS_PHASE 无 eta 字段，容忍缺失。 */
function updateEtaChip(m) {
  const chip = $("eta-chip");
  if (!chip) return;
  if (m.kind === "STATUS_COMPLETED" || m.kind === "STATUS_CANCELLED" || m.kind === "STATUS_ERROR") {
    etaRange = null;
    chip.style.display = "none";
    resetSubLights(); // 终态（完成/停止/错误）：被取消/中断的通道收不到 done，灯全部回灰防"仍在查询"误导
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
  return String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
}
function safeLink(u) {
  const s = String(u || "");
  return /^https?:\/\//i.test(s) ? esc(s) : ""; // 订单外链仅放行 http/https（防 javascript: 注入）
}

/* ---------- 发送 ---------- */
function flushOutbox() {
  if (!outbox.length) return;
  const pending = outbox.filter((o) => o.sid === sid);
  outbox = outbox.filter((o) => o.sid !== sid);  // 其他会话的离线消息保留，切回时再补发
  if (!pending.length) return;
  addTimeline("System", "INFO", `自动补发离线消息 ${pending.length} 条…`);
  for (const o of pending) {
    sendMsg(o.text);
    addChat("user", o.text);  // 重画回显：离线期间的乐观回显已随面板清空，服务端收到的才不重发
  }
  setBusy(true);  // 服务端 chatter_lock 串行处理，回复按序到达后逐条解锁
}

function doSend() {
  const text = input.value.trim();
  if (!text || busy) return;
  input.value = "";
  setBusy(true);
  if (ws && ws.readyState === 1) {
    sendMsg(text);
  } else {
    outbox.push({ sid, text });  // 离线不丢弃（要求 1：用户消息必有回复——重连后自动补发）
    addTimeline("System", "INFO", "当前离线，消息已保存，重连后自动发送。");
  }
  addChat("user", text); // 乐观渲染（服务端回显时去重）
}
sendBtn.addEventListener("click", doSend);
input.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); doSend(); }
});

$("session-select").addEventListener("change", (e) => switchSession(e.target.value));
$("new-session").addEventListener("click", createSession);
$("logout").addEventListener("click", () => {
  token = "";
  localStorage.removeItem("tm_token");
  location.reload();
});
$("tab-login").addEventListener("click", () => setAuthMode("login"));
$("tab-register").addEventListener("click", () => setAuthMode("register"));
$("auth-submit").addEventListener("click", submitAuth);
$("auth-password").addEventListener("keydown", (e) => {
  if (e.key === "Enter") submitAuth();
});
$("memory-clear").addEventListener("click", async () => {
  try {
    await authFetch("/api/memory", { method: "DELETE" });
    refreshMemory();
  } catch (e) { /* 401 已弹登录 */ }
});

boot();
