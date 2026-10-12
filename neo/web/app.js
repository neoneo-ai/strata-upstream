// Neo Strata 控制台。数据一律走控制台服务的 HTTP 接口；在 pywebview 壳里按钮走 js_api，
// 在浏览器里走 POST，两条路调的是同一批监督器方法。
const $ = (id) => document.getElementById(id);
const STATE_TEXT = { idle: "未启动", starting: "启动中", running: "运行中",
                     restarting: "自动重启中", stopping: "停止中", halted: "已停止（熔断）" };
const ROUTES = {
  status: ["GET", "/neo/status"], models: ["GET", "/neo/models"], log: ["GET", "/neo/log"],
  config: ["GET", "/neo/config"], start: ["POST", "/neo/start"], stop: ["POST", "/neo/stop"],
  retry: ["POST", "/neo/retry"], set_model: ["POST", "/neo/model"], save_config: ["POST", "/neo/config"],
};

async function api(action, payload) {
  const js = window.pywebview && window.pywebview.api;
  const camel = action.replace(/_(\w)/g, (m, c) => c.toUpperCase());
  const name = js && (js[action] ? action : (js[camel] ? camel : null));
  if (js && name) {
    try { return await js[name](payload); } catch (e) { /* 壳调不通就走 HTTP */ }
  }
  const [method, path] = ROUTES[action] || ["GET", "/neo/status"];
  const opt = { method, headers: { "content-type": "application/json" } };
  if (method === "POST") opt.body = JSON.stringify(payload || {});
  const r = await fetch(path + (action === "log" ? "?tail=60000" : ""), opt);
  return r.json();
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
  pill($("state-pill"), s.state);
  pill($("nav-state"), s.state);
  $("head-model").textContent = s.label || s.model || "";
  $("btn-retry").classList.toggle("hidden", s.state !== "halted");
  $("btn-start").disabled = s.state === "running" || s.state === "starting";
  $("btn-stop").disabled = s.state === "idle" || s.state === "halted";

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

  $("s-state").textContent = (STATE_TEXT[s.state] || s.state) +
    (s.healthy ? (s.loaded ? " · 模型已就绪" : " · 服务在应答，模型未加载")
               : (s.listening ? " · 端口在听，还没应答" : ""));
  $("s-model").textContent = s.served_model || s.model || "—";
  $("s-port").textContent = `${s.host}:${s.port}` + (s.adopted ? "（接手已在运行的服务）" : "");
  $("s-pid").textContent = s.pid ? `${s.pid}${s.adopted ? "（别人启动的）" : ""}` : "—";
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
  try { render(await api("status")); } catch (e) { /* 控制台没起来：下一轮再试 */ }
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
$("chat-open").onclick = () => api("open_page", "chat");
$("monitor-open").onclick = () => api("open_page", "monitor");
$("btn-chat").onclick = () => api("open_page", "chat");
$("btn-monitor").onclick = () => api("open_page", "monitor");
$("btn-log").onclick = () => api("open_log");
$("guide-log").onclick = () => api("open_log");
$("guide-reload").onclick = poll;
$("btn-refresh").onclick = poll;
$("btn-start").onclick = async () => render(await api("start"));
$("btn-stop").onclick = async () => render(await api("stop"));
$("btn-retry").onclick = async () => render(await api("retry"));

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
    btn.onclick = async () => render(await api("set_model", { model: m.id }));
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
$("log-open").onclick = () => api("open_log");

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
  await api("save_config", body);
  $("set-note").textContent = "已存到 neo/state/launcher.json（端口改了要重启 Neo 才生效）";
  setTimeout(() => { $("set-note").textContent = ""; }, 8000);
};

poll();
setInterval(poll, 2000);
setInterval(() => { if ($("page-logs").classList.contains("on")) loadLog(); }, 5000);
