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
  lan: ["GET", "/neo/lan"], token: ["POST", "/neo/token"], net: ["GET", "/neo/net"],
  connect: ["GET", "/neo/connect"], connect_write: ["POST", "/neo/connect"],
  wizard: ["GET", "/neo/wizard"], wizard_run: ["POST", "/neo/wizard"],
  catalog: ["GET", "/neo/catalog"], install: ["POST", "/neo/install"],
  set_source: ["POST", "/neo/source"],
};

function note(text) {
  const el = $("diag");
  if (!el) return;
  el.classList.toggle("hidden", !text);
  if (text) el.textContent = "提示：" + text;
}

async function api(action, payload, query) {
  const [method, path] = ROUTES[action] || ["GET", "/neo/status"];
  const opt = { method, headers: { "content-type": "application/json" } };
  if (method === "POST") opt.body = JSON.stringify(payload === undefined ? {} : payload);
  const r = await fetch(path + (query || "") + (action === "log" ? "?tail=60000" : ""), opt);
  const data = await r.json();
  if (!r.ok) note(`${action} 没成（HTTP ${r.status}：${data.error || "无详情"}）`);
  return data;
}

// 按钮动作：同一个动作在飞的时候不再发第二次，请求失败要说出来，不让按钮点了没反应。
const inflight = new Set();
async function act(action, payload, query) {
  if (inflight.has(action)) return null;
  inflight.add(action);
  try {
    return await api(action, payload, query);
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
  if (name === "models") { loadModels(); loadCatalog(); }
  if (name === "settings") loadConfig();
  if (name === "connect") loadConnect();
}
document.querySelectorAll(".tab").forEach((t) => t.onclick = () => showPage(t.dataset.page));

function pill(el, state) {
  el.className = "pill " + state;
  el.textContent = STATE_TEXT[state] || state;
}

function num(v, digits = 1) {
  return (v === null || v === undefined || isNaN(v)) ? "—" : Number(v).toFixed(digits).replace(/\.0$/, "");
}

// 整数计数。列表与对象一律 "—"：/metrics 顶层的 requests 是最近 12 条请求的记录列表，
// 把它直接写进文字就是"请求 [object Object],[object Object]"（用户 2026-10-12 的截图）。
function count(v) {
  if (v === null || v === undefined || v === "" || typeof v === "object") return "—";
  const n = Number(v);
  return Number.isFinite(n) ? String(Math.round(n)) : "—";
}

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
  // 蓝色的主按钮跟着"此刻能按的那个"走：服务在跑（含别人启动的、启动中、停止中、
  // 自动重启中）就该是"停止"亮着；停着才是"启动"亮着；熔断了是"重试"。
  // 以前 primary 写死在"启动"上，CSS 里又没有 :disabled，运行中看着仍是"启动"在等人点。
  const busy = st === "running" || st === "starting" || st === "stopping" ||
               st === "restarting" || st === "external";
  $("btn-start").classList.toggle("primary", !busy && s.state !== "halted");
  $("btn-stop").classList.toggle("primary", busy);
  $("btn-retry").classList.toggle("primary", s.state === "halted");

  $("guide").classList.toggle("hidden", !!s.installed);
  const diag = s.diagnosis || "";
  $("diag").classList.toggle("hidden", !diag);
  if (diag) $("diag").textContent = "提示：" + diag;

  const m = s.metrics || {};
  $("m-toks").textContent = num(m.tokens_per_s);
  $("m-step").textContent = num(m.decode_step_ms, 1);
  $("m-vram").textContent = m.gpu_used_mib === undefined ? "—" : num(m.gpu_used_mib, 0);
  $("m-vram-s").textContent = m.gpu_total_mib
    ? `已用 / ${num(m.gpu_total_mib, 0)} MiB${m.gpu_name ? " · " + m.gpu_name : ""}` : "已用 / 总量";
  $("m-ram").textContent = m.ram_used_gb === undefined ? "—" : num(m.ram_used_gb, 1);
  $("m-ram-s").textContent = m.ram_total_gb ? `已用 / ${num(m.ram_total_gb, 1)} GB` : "已用 / 总量";
  $("m-active").textContent = count(m.active);
  $("m-queued").textContent = `排队 ${count(m.queued)}`;
  $("m-ctx").textContent = s.max_context ? s.max_context.toLocaleString() : "—";
  $("m-reqs").textContent = `请求 ${count(m.requests)}`;

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
$("guide-models").onclick = () => showPage("models");
$("guide-reload").onclick = poll;
$("btn-refresh").onclick = poll;
$("btn-start").onclick = async () => { const s = await act("start"); if (s) render(s); };
$("btn-stop").onclick = async () => { const s = await act("stop"); if (s) render(s); };
$("btn-retry").onclick = async () => { const s = await act("retry"); if (s) render(s); };

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
    const cur = m.id === data.current;
    // 服务此刻在不在跑（模型页每 5 秒跟着状态页一起刷，last 就是最近一次的 /neo/status）
    const st = last ? (last.display_state || last.state) : "idle";
    const busy = st === "running" || st === "starting" || st === "stopping" ||
                 st === "restarting" || st === "external";
    const btn = document.createElement("button");
    // 换模型走 supervisor.set_model：它自己先 stop 再 start，用户不用先按停止。
    // 但服务在跑时点它，正在跑的请求会被切断，所以按钮写"换过去"，鼠标上去说清楚它先停谁。
    btn.textContent = cur ? "正在用" : (busy ? "换过去" : "启动它");
    btn.disabled = cur || !m.installed;
    btn.title = cur ? "当前服务正在用这个模型"
      : (!m.installed ? "模型文件不在，起不来"
        : (busy ? `会先停掉当前服务（正在跑的请求会被切断），再用「${m.name}」重新起`
                : `用「${m.name}」起服务`));
    btn.onclick = async () => { const s = await act("set_model", { model: m.id }); if (s) render(s); };
    row.append(info, btn);
    box.append(row);
  });
  if (!(data.models || []).length) box.innerHTML = '<p class="muted">根目录没有 strata-*.json。</p>';
}

// ---------------- 模型页：可安装清单（GET /neo/catalog） ----------------
// 清单就是 setup.py 的安装菜单：neo/models.py 用 ast 读它的 MODELS / FAMILIES 两张表
// （不 import、不跑 setup.py），装没装对根目录的 strata-*.json，推荐按这台机器的内存
// （docs/AI_SETUP.md 第 3 步与 docs/MODELS.md 的 Pick by RAM）。
// 点"安装" = POST /neo/install = 后台跑 setup.py --yes --no-start --family X --model Y
// （加上"下载源"那一排选中的源：--source modelscope，或 HF_ENDPOINT=https://hf-mirror.com）。
// Neo 不自己下载模型：下载、校验、写配置文件全是 setup.py 的事。
let catalogData = null;

function sourceItem(sources) {
  const src = sources || {};
  const list = src.choices || [];
  return list.find((c) => c.key === src.chosen) || list[0] || { prefix: "", suffix: "" };
}

function sourceTitle(sources, key) {
  const item = ((sources || {}).choices || []).find((c) => c.key === key);
  return item ? item.title : (key || "自动");
}

function installCmd(m, sources) {
  const c = sourceItem(sources);
  return `${c.prefix || ""}python setup.py --yes --no-start --family ${m.family} --model ${m.model}${c.suffix || ""}`;
}

function catTag(text, cls) {
  const span = document.createElement("span");
  span.className = "tag" + (cls ? " " + cls : "");
  span.textContent = text;
  return span;
}

function catRow(m, running) {
  const row = document.createElement("div");
  row.className = "cat-row" + (m.recommended ? " rec" : "") + (m.installed ? " has" : "")
    + (m.fit === "no" ? " off" : "");
  const info = document.createElement("div");
  info.className = "cat-info";
  const head = document.createElement("div");
  head.className = "name";
  head.textContent = `${m.family_title} · ${m.model}`;
  if (m.recommended) head.append(catTag("推荐", "rec"));
  else if (m.also) head.append(catTag("也可"));
  if (m.installed) head.append(catTag("已装", "ok"));
  else if (m.broken) head.append(catTag("装好一半", "warn"));
  if (m.budget) head.append(catTag("低内存模式"));
  if (m.experimental) head.append(catTag("实验性", "warn"));
  if (m.nvidia_only) head.append(catTag("只 NVIDIA"));
  const meta = document.createElement("div");
  meta.className = "meta";
  meta.textContent = `${m.download_gb} GB 下载 · ${m.fit_text}` + (m.about ? ` · ${m.about}` : "");
  info.append(head, meta);
  const btn = document.createElement("button");
  btn.textContent = m.installed ? "已装好" : (running ? "安装中…" : (m.installable ? "安装" : "装不下"));
  btn.disabled = m.installed || running || !m.installable;
  if (!m.installed && m.installable && !running) btn.classList.add("primary");
  btn.title = m.installed ? `已装好（${m.config}）`
    : (m.installable ? `在后台跑：${installCmd(m, catalogData && catalogData.sources)}（下载要几十分钟）` : m.fit_text);
  btn.onclick = async () => {
    const source = (catalogData && catalogData.sources && catalogData.sources.chosen) || "probe";
    const res = await act("install", { family: m.family, model: m.model, source });
    if (!res) return;
    note(res.started ? `开始装 ${m.family_title} ${m.model}（${sourceTitle(catalogData, source)}）：输出进 neo/state/logs/wizard.log`
                     : (res.notes || []).join("；"));
    setTimeout(loadCatalog, 3000);
  };
  row.append(info, btn);
  return row;
}

function renderSources(data) {
  const src = data.sources || {};
  const chosen = src.chosen || "probe";
  const sel = $("cat-src-select");
  sel.innerHTML = "";
  (src.choices || []).forEach((c) => {
    const opt = document.createElement("option");
    opt.value = c.key;
    opt.textContent = `${c.title}${c.ms ? ` ${Math.round(c.ms)} ms` : ""}`
      + (c.key === src.suggested ? "（建议）" : "");
    opt.title = `${c.note} · ${c.reachable ? "连得上" : "这个地址现在连不上"}`;
    sel.append(opt);
  });
  sel.value = chosen;
  $("cat-src-note").textContent = src.note || "—";
}

function renderCatalog(data) {
  catalogData = data;
  $("cat-machine").textContent = (data.machine || {}).note || "—";
  renderSources(data);
  const rec = data.recommendation || {};
  $("cat-rec").textContent = rec.why
    ? `推荐 ${rec.family} / ${rec.model}：${rec.why}。${rec.note || ""}`
    : `读不到安装菜单：${data.root} 里的 setup.py 没有 MODELS / FAMILIES 两张表`;
  $("cat-cmd").textContent = rec.model ? installCmd(rec, data.sources) : "";
  const running = !!(data.wizard && data.wizard.running);
  const box = $("cat-list");
  box.innerHTML = "";
  (data.models || []).forEach((m) => box.append(catRow(m, running)));
  if (!(data.models || []).length) {
    box.innerHTML = `<p class="muted">读不到可安装清单：${data.root} 里没有 setup.py，`
      + "或它的模型表读不出来（这不是 Strata 的根目录？）</p>";
  }
  const log = $("cat-log");
  log.textContent = data.log_tail || (running ? "（安装刚起步，还没有输出）" : "（还没有安装日志）");
  log.scrollTop = log.scrollHeight;
}

async function loadCatalog(fresh) {
  const data = await api("catalog", undefined, fresh ? "?fresh=1" : "");
  if (data && !data.error) renderCatalog(data);
}

$("cat-refresh").onclick = () => loadCatalog(true);
$("cat-src-retest").onclick = () => loadCatalog(true);
$("cat-src-select").onchange = async () => {
  const source = $("cat-src-select").value;
  const res = await act("set_source", { source });
  if (!res) return;
  note(res.ok ? `下载源记下了：${sourceTitle(res, source)}，下一次安装就用它`
              : (res.error || "没记下这个源"));
  loadCatalog();
};
$("cat-copy").onclick = async () => {
  const rec = (catalogData && catalogData.recommendation) || {};
  const ok = await copyText(rec.model ? installCmd(rec, catalogData.sources) : "");
  $("cat-copy").textContent = ok ? "已复制" : "复制失败";
  setTimeout(() => { $("cat-copy").textContent = "复制推荐命令"; }, 4000);
};

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

// ---------------- 接入页（M3：局域网 / token / agent；M2：安装向导） ----------------
async function copyText(text) {
  // WebView2 里 navigator.clipboard 要安全上下文；窗口是 http://127.0.0.1，
  // 算安全上下文，但运行时缺权限时照样会 reject，所以两条路都留着。
  try {
    if (navigator.clipboard && window.isSecureContext) {
      await navigator.clipboard.writeText(text);
      return true;
    }
  } catch (e) { /* 走兜底 */ }
  const ta = document.createElement("textarea");
  ta.value = text;
  ta.style.position = "fixed";
  ta.style.opacity = "0";
  document.body.append(ta);
  ta.select();
  let ok = false;
  try { ok = document.execCommand("copy"); } catch (e) { ok = false; }
  ta.remove();
  return ok;
}

let lanData = null;
let revealedKey = "", revealedMask = "";

function setRevealedKey(key, mask) {
  // 完整 key 只活在页面内存和这一个只读输入框里；掩码变了就清掉，别复制到过期的那把
  revealedKey = key || "";
  revealedMask = mask || "";
  const box = $("lan-key-value");
  box.value = revealedKey;
  box.classList.toggle("hidden", !revealedKey);
}

async function revealKey() {
  // 刚生成过的直接用手里那把；没有就向服务要一次（action=reveal 只读，不写任何文件，
  // 而且只认本机来的请求 —— 见 neo/console_server.py 的 make_token）
  if (revealedKey) return revealedKey;
  const res = await act("token", { action: "reveal" });
  if (!res) return "";
  if (!res.ok) {
    note(res.note || res.error || "");
    return "";
  }
  setRevealedKey(res.api_key, res.api_key_masked);
  return revealedKey;
}

function renderLan(lan) {
  lanData = lan;
  $("lan-note").textContent = lan.note || "—";
  $("lan-loopback").textContent = lan.loopback_base || "—";
  $("lan-bases").textContent = (lan.lan_bases || []).join("  ") ||
    (lan.lan_open ? "没找到局域网地址（这台机器没连上网？）" : "未开放局域网");
  $("lan-key").textContent = lan.has_api_key ? lan.api_key_masked
    : "没设（只绑本机时不需要）";
  $("lan-firewall").textContent = lan.firewall_hint || "不需要（没绑局域网）";
  if (!lan.has_api_key) setRevealedKey("", "");
  else if (revealedKey && lan.api_key_masked !== revealedMask) setRevealedKey("", "");
}

function renderAgents(data) {
  const box = $("agents");
  box.innerHTML = "";
  (data.recipes || []).forEach((r) => {
    const card = document.createElement("div");
    card.className = "agent";
    const head = document.createElement("div");
    head.className = "agent-head";
    const title = document.createElement("span");
    title.className = "name";
    title.textContent = r.title;
    const meta = document.createElement("span");
    meta.className = "meta";
    meta.textContent = `${r.base} · 模型 ${r.model}` +
      (r.context ? `（${r.context.toLocaleString()} 上下文）` : "") +
      (data.has_api_key ? "" : " · 服务没设 token");
    head.append(title, meta);
    const pre = document.createElement("pre");
    pre.className = "cmd";
    pre.textContent = (r.powershell || []).join("\n");
    const row = document.createElement("div");
    row.className = "row";
    const copy = document.createElement("button");
    copy.textContent = "复制命令";
    copy.onclick = async () => {
      const ok = await copyText((r.powershell || []).join("\n"));
      copy.textContent = ok ? "已复制" : "复制失败";
      setTimeout(() => { copy.textContent = "复制命令"; }, 4000);
    };
    const write = document.createElement("button");
    write.textContent = "写进配置";
    write.title = `合并进 ${r.config_path}（现有的先备份成 .neo-bak）`;
    write.onclick = async () => {
      const res = await act("connect_write", { agent: r.agent });
      if (!res) return;
      write.textContent = res.ok ? "已写入" : "没写";
      if (res.notes && res.notes.length) note(res.notes.join("；"));
      setTimeout(() => { write.textContent = "写进配置"; }, 6000);
    };
    row.append(copy, write);
    card.append(head, pre, row);
    box.append(card);
  });
}

function renderWizard(w) {
  const box = $("wizard-steps");
  box.innerHTML = "";
  (w.steps || []).forEach((s) => {
    const line = document.createElement("div");
    line.className = "step" + (s.ok ? " ok" : " warn");
    line.textContent = `${s.ok ? "✓" : "—"} ${s.name}：${s.text}`;
    box.append(line);
  });
  $("wizard-cmd").textContent = "命令：" + (w.command || "");
  $("wizard-log").textContent = w.log_tail || "（还没有安装日志）";
  $("wizard-run").disabled = !!(w.run && w.run.running);
  $("wizard-run").textContent = (w.run && w.run.running) ? "安装中…" : "开始安装";
}

async function loadConnect() {
  const [lan, conn, wizard] = await Promise.all([
    api("lan"), api("connect"), api("wizard"),
  ]);
  if (lan && !lan.error) renderLan(lan);
  if (conn && !conn.error) renderAgents(conn);
  if (wizard && !wizard.error) renderWizard(wizard);
}

$("lan-token").onclick = async () => {
  const res = await act("token");
  if (!res) return;
  if (res.ok) setRevealedKey(res.api_key, res.api_key_masked);
  note(res.note);
  const lan = await api("lan");
  if (lan && !lan.error) renderLan(lan);
};
$("lan-copy-key").onclick = async () => {
  const btn = $("lan-copy-key");
  const key = await revealKey();
  if (!key) {
    btn.textContent = "没有 token";
    setTimeout(() => { btn.textContent = "复制 token"; }, 4000);
    return;
  }
  const ok = await copyText(key);
  btn.textContent = ok ? "已复制" : "复制失败";
  setTimeout(() => { btn.textContent = "复制 token"; }, 4000);
};
$("lan-copy-base").onclick = async () => {
  const ok = await copyText((lanData && lanData.base) || "");
  $("lan-copy-base").textContent = ok ? "已复制" : "复制失败";
  setTimeout(() => { $("lan-copy-base").textContent = "复制服务地址"; }, 4000);
};
$("lan-copy-fw").onclick = async () => {
  const ok = await copyText((lanData && lanData.firewall_hint) || "");
  $("lan-copy-fw").textContent = ok ? "已复制" : "复制失败";
  setTimeout(() => { $("lan-copy-fw").textContent = "复制防火墙命令"; }, 4000);
};
$("wizard-refresh").onclick = async () => {
  $("wizard-refresh").textContent = "探测中…";
  const r = await fetch("/neo/net?fresh=1", { headers: { accept: "application/json" } });
  const w = await api("wizard");
  if (w && !w.error) renderWizard(w);
  $("wizard-refresh").textContent = "重新探测网络";
};
$("wizard-run").onclick = async () => {
  const res = await act("wizard_run", {});
  if (!res) return;
  note(res.started ? "安装已在后台跑，输出进 neo/state/logs/wizard.log"
                   : (res.notes || []).join("；"));
  setTimeout(async () => { const w = await api("wizard"); if (w && !w.error) renderWizard(w); }, 3000);
};
$("wizard-copy").onclick = async () => {
  const ok = await copyText($("wizard-cmd").textContent.replace(/^命令：/, ""));
  $("wizard-copy").textContent = ok ? "已复制" : "复制失败";
  setTimeout(() => { $("wizard-copy").textContent = "复制安装命令"; }, 4000);
};

poll();

setInterval(poll, 2000);
setInterval(() => { if ($("page-logs").classList.contains("on")) loadLog(); }, 5000);
// 模型页开着时跟着刷：安装是在后台跑的，页面要能看出"装到哪了、装好没有"
setInterval(() => { if ($("page-models").classList.contains("on")) loadCatalog(); }, 5000);
