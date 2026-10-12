// Neo Strata 控制台。所有动作只走控制台服务的 HTTP 接口（neo/console_server.py）。
//
// 刻意不碰 pywebview 的 js_api 桥（实测 2026-10-12，pywebview 6.2.1 / WinForms + WebView2）：
// 一次点击要窗口线程两次收消息 —— on_script_notify 收页面的 PostWebMessageAsJson
// （platforms/edgechromium.py:225，在窗口线程上），方法跑完后 evaluate_js 再用
// webview.Invoke + semaphore 把结果同步投回窗口线程（edgechromium.py:137）。窗口线程
// 有一阵不在消息循环里，第一次点击的 promise 就永不返回，脚本消息队列堵住，后面每次
// 点击都排在它后面 —— 用户看到的就是"一点就卡死"。摘掉这座桥，点击路径上一行 Python
// 都不跑，窗口线程只管收消息。详见 docs/NEO_DESKTOP.md。
const $ = (id) => document.getElementById(id);
const STATE_TEXT = { idle: "未启动", starting: "启动中", running: "运行中",
                     restarting: "自动重启中", stopping: "停止中", halted: "已停止（熔断）",
                     external: "运行中（外部启动）" };
const ROUTES = {
  status: ["GET", "/neo/status"], models: ["GET", "/neo/models"], log: ["GET", "/neo/log"],
  config: ["GET", "/neo/config"], start: ["POST", "/neo/start"], stop: ["POST", "/neo/stop"],
  retry: ["POST", "/neo/retry"], set_model: ["POST", "/neo/model"], save_config: ["POST", "/neo/config"],
  open_page: ["POST", "/neo/open"], open_log: ["POST", "/neo/open-log"],
};

function note(text) {
  const el = $("diag");
  if (!el) return;
  el.classList.toggle("hidden", !text);
  if (text) el.textContent = "提示：" + text;
}

async function api(action, payload) {
  const [method, path] = ROUTES[action] || ["GET", "/neo/status"];
  const opt = { method, headers: { "content-type": "application/json" } };
  if (method === "POST") opt.body = JSON.stringify(payload === undefined ? {} : payload);
  const r = await fetch(path + (action === "log" ? "?tail=60000" : ""), opt);
  const data = await r.json();
  if (!r.ok) note(`${action} 没成（HTTP ${r.status}：${data.error || "无详情"}）`);
  return data;
}

// 按钮动作：同一个动作在飞的时候不再发第二次，请求失败要说出来，不让按钮点了没反应。
const inflight = new Set();
async function act(action, payload) {
  if (inflight.has(action)) return null;
  inflight.add(action);
  try {
    return await api(action, payload);
  } catch (e) {
    note(`${action} 没送到服务：${e}`);
    return null;
  } finally {
    inflight.delete(action);
  }
}

let last = null;

// ---------------- 导航 ----------------
function showPage(name) {
  document.querySelectorAll(".page").forEach((p) => p.classList.remove("on"));
  document.querySelectorAll(".tab").forEach((t) => t.classList.toggle("on", t.dataset.page === name));
  $("page-" + name).classList.add("on");
  if (name === "chat") ensureFrame("chat");
  if (name === "monitor") ensureFrame("monitor");
  if (name === "logs") loadLog();
  if (name === "models") loadModels();
  if (name === "settings") loadConfig();
}
document.querySelectorAll(".tab").forEach((t) => t.onclick = () => showPage(t.dataset.page));

function pill(el, state) {
  el.className = "pill " + state;
  el.textContent = STATE_TEXT[state] || state;
}

function num(v, digits = 1) {


// ---------------- 状态页 ----------------
function render(s) {
  last = s;
  // display_state：端口上有一个脚本先起来的服务在跑时是 "external"（运行中·外部启动），
  // 那时 Neo 手里没有子进程，但服务确实在应答 —— 写"未启动"是骗人的。
  const st = s.display_state || s.state;
  const byOther = !!(s.adopted || s.external);
  pill($("state-pill"), st);
  pill($("nav-state"), st);
  $("head-model").textContent = s.served_model || s.label || s.model || "";
  $("btn-retry").classList.toggle("hidden", s.state !== "halted");
  $("btn-start").disabled = st === "running" || st === "starting";
  $("btn-stop").disabled = st === "idle" || s.state === "halted";

  $("guide").classList.toggle("hidden", !!s.installed);
  const diag = s.diagnosis || "";
  $("diag").classList.toggle("hidden", !diag);
  if (diag) $("diag").textContent = "提示：" + diag;

  const m = s.metrics || {};
  $("m-toks").textContent = num(m.tokens_per_s);
  $("m-step").textContent = num(m.decode_step_ms, 1);
  $("m-vram").textContent = m.gpu_used_mib ? num(m.gpu_used_mib, 0) : "—";
  $("m-vram-s").textContent = m.gpu_total_mib ? `已用 / ${num(m.gpu_total_mib, 0)} MiB` : "已用 / 总量";
  $("m-ram").textContent = m.ram_used_gb ? num(m.ram_used_gb, 1) : "—";
  $("m-ram-s").textContent = m.ram_total_gb ? `已用 / ${num(m.ram_total_gb, 1)} GB` : "已用 / 总量";
  $("m-active").textContent = m.active === undefined ? "—" : m.active;
  $("m-queued").textContent = `排队 ${m.queued === undefined ? 0 : m.queued}`;
  $("m-ctx").textContent = s.max_context ? s.max_context.toLocaleString() : "—";
  $("m-reqs").textContent = `请求 ${m.requests === undefined ? 0 : m.requests}`;

  $("s-state").textContent = (STATE_TEXT[st] || st) +
    (s.healthy ? (s.loaded ? " · 模型已就绪" : " · 服务在应答，模型未加载")
               : (s.listening ? " · 端口在听，还没应答" : ""));
  $("s-model").textContent = s.served_model || s.model || "—";
  $("s-port").textContent = `${s.host}:${s.port}` + (byOther ? "（别人启动的，Neo 认下了它）" : "");
  $("s-pid").textContent = s.pid ? `${s.pid}${byOther ? "（别人启动的）" : ""}` : "—";
  $("s-restarts").textContent = s.state === "halted"
    ? `${s.restarts.length} 次 · 已达上限 ${s.max_restarts}`
    : `${s.restarts.length} 次 · 上限 ${s.max_restarts} · 退避 ${s.backoff.map((x) => x + "s").join("/")}`;
  $("s-log").textContent = s.log_path || "—";

  const box = $("events");
  box.innerHTML = "";
  (s.events || []).slice().reverse().forEach((e) => {
    const row = document.createElement("div");
    const t = document.createElement("time");
    t.textContent = new Date(e.at * 1000).toTimeString().slice(0, 8);
    const span = document.createElement("span");
    span.className = e.kind || "info";
    span.textContent = e.text;
    row.append(t, span);
    box.append(row);
  });

  const pages = s.pages || {};
  $("chat-url").textContent = pages.chat || "";
  $("monitor-url").textContent = pages.monitor || "";
  if ($("page-chat").classList.contains("on")) ensureFrame("chat");
  if ($("page-monitor").classList.contains("on")) ensureFrame("monitor");
}

async function poll() {
  try { render(await api("status")); } catch (e) { note(`控制台服务没应答：${e}`); }
}

// ---------------- 内嵌页与快捷按钮 ----------------
function ensureFrame(which) {
  if (!last) return;
  const url = (last.pages || {})[which] || "";
  const frame = $(which + "-frame");
  if (url && frame.src === "about:blank") frame.src = url;
}
$("chat-reload").onclick = () => { const f = $("chat-frame"); f.src = "about:blank"; ensureFrame("chat"); };
$("monitor-reload").onclick = () => { const f = $("monitor-frame"); f.src = "about:blank"; ensureFrame("monitor"); };
$("chat-open").onclick = () => act("open_page", { page: "chat" });
$("monitor-open").onclick = () => act("open_page", { page: "monitor" });
$("btn-chat").onclick = () => act("open_page", { page: "chat" });
$("btn-monitor").onclick = () => act("open_page", { page: "monitor" });
$("btn-log").onclick = () => act("open_log");
$("guide-log").onclick = () => act("open_log");
$("guide-reload").onclick = poll;
$("btn-refresh").onclick = poll;
$("btn-start").onclick = async () => { const s = await act("start"); if (s) render(s); };
$("btn-stop").onclick = async () => { const s = await act("stop"); if (s) render(s); };
$("btn-retry").onclick = async () => { const s = await act("retry"); if (s) render(s); };

  return (v === null || v === undefined || isNaN(v)) ? "—" : Number(v).toFixed(digits).replace(/\.0$/, "");
}



// ---------------- 模型页 ----------------
async function loadModels() {
  const data = await api("models");
  const box = $("models");
  box.innerHTML = "";
  (data.models || []).forEach((m) => {
    const row = document.createElement("div");
    row.className = "model" + (m.id === data.current ? " cur" : "") + (m.installed ? "" : " gone");
    const info = document.createElement("div");
    const name = document.createElement("div");
    name.className = "name";
    name.textContent = m.name;
    const meta = document.createElement("div");
    meta.className = "meta";
    meta.textContent = `${m.context ? m.context.toLocaleString() + " 上下文 · " : ""}端口 ${m.port} · ` +
      `${m.vision ? "视觉+文本 · " : "文本 · "}${m.installed ? "文件在" : "文件不在"}`;
    info.append(name, meta);
    const btn = document.createElement("button");
    btn.textContent = m.id === data.current ? "正在用" : "启动它";
    btn.disabled = m.id === data.current || !m.installed;
    btn.onclick = async () => { const s = await act("set_model", { model: m.id }); if (s) render(s); };
    row.append(info, btn);
    box.append(row);
  });
  if (!(data.models || []).length) box.innerHTML = '<p class="muted">根目录没有 strata-*.json。</p>';
}

// ---------------- 日志页 ----------------
const LOG_RENDER_LINES = 400;   // 一次最多渲染多少行：一条几十万字符的行会把页面卡死
let logShown = "";

async function loadLog() {
  const data = await api("log");
  $("log-path").textContent = data.path || "";
  const body = $("log-body");
  const lines = (data.text || "").split("\n");
  let text = lines.length > LOG_RENDER_LINES ? lines.slice(-LOG_RENDER_LINES).join("\n") : (data.text || "");
  text = text || "（还没有日志）";
  if (text === logShown) return;              // 5 秒刷一次，内容没变就不重画
  logShown = text;
  body.textContent = text;
  body.scrollTop = body.scrollHeight;
}
$("log-reload").onclick = loadLog;
$("log-open").onclick = () => act("open_log");

// ---------------- 设置页 ----------------
async function loadConfig() {
  const c = await api("config");
  $("set-port").value = c.port || "";
  $("set-host").value = c.host || "127.0.0.1";
  $("set-key").placeholder = c.has_api_key ? "已设置（留空表示不改）" : "留空 = 本机免密钥";
}
$("set-save").onclick = async () => {
  const body = {};
  const port = Number($("set-port").value);
  if (port) body.port = port;
  if ($("set-host").value.trim()) body.host = $("set-host").value.trim();
  if ($("set-key").value.trim()) body.api_key = $("set-key").value.trim();
  const c = await act("save_config", body);
  if (!c) return;
  $("set-note").textContent = "已存到 neo/state/launcher.json（端口改了要重启 Neo 才生效）";
  setTimeout(() => { $("set-note").textContent = ""; }, 8000);
};

poll();
setInterval(poll, 2000);
setInterval(() => { if ($("page-logs").classList.contains("on")) loadLog(); }, 5000);
