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
// 中英切换在 i18n.js（先于本文件加载）。t() 把动态拼出来的中文换成英文，表里没有就原样回；
// applyLang() 把带 data-i18n 的元素整块换掉。中文是默认，zh 时两个函数都是原样。
const I18N = window.NEO_I18N || {};
const t = (s) => (I18N.t ? I18N.t(s) : s);
const applyLang = (el) => { if (I18N.applyLang) I18N.applyLang(el); };
const isEn = () => I18N.neoLang ? I18N.neoLang() === "en" : false;
// 服务端拼出来的句子带数字（"装不下：需要约 48 GB 内存，这台机器 32 GB"），逐字表认不了，
// 所以带数字的那几条走模板。模板没覆盖到的原样回，最多是那一句还是中文。
const NUM_TEMPLATES = [
  [/^装得下：约 (\d+) GiB 专家留在内存里，其余从 SSD 读（需要 (\d+) GB 内存）$/,
    (a, b) => `Fits: ~${a} GiB of experts stay in RAM, the rest reads from the SSD (needs ${b} GB RAM)`],
  [/^装不下：需要约 (\d+) GB 内存，这台机器 ([\d.]+) GB$/,
    (a, b) => `Won't fit: needs ~${a} GB RAM, this machine has ${b} GB`],
  [/^装不下：需要 (\d+) GB 内存，这台机器 ([\d.]+) GB$/,
    (a, b) => `Won't fit: needs ${a} GB RAM, this machine has ${b} GB`],
  [/^装得下（需要约 (\d+) GB 内存）$/, (a) => `Fits (needs ~${a} GB RAM)`],
  [/^有点紧：需要约 (\d+) GB 内存，这台机器 ([\d.]+) GB$/,
    (a, b) => `Tight: needs ~${a} GB RAM, this machine has ${b} GB`],
];
function tNum(s) {
  const text = String(s === undefined || s === null ? "" : s);
  if (!isEn()) return text;
  for (const [re, make] of NUM_TEMPLATES) {
    const hit = text.match(re);
    if (hit) return make(...hit.slice(1));
  }
  return t(text);
}
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
  engine: ["GET", "/neo/engine"], engine_run: ["POST", "/neo/engine"],
  catalog: ["GET", "/neo/catalog"], install: ["POST", "/neo/install"],
  set_source: ["POST", "/neo/source"],
  upstream: ["GET", "/neo/upstream"], remote: ["GET", "/neo/remote"],
  ms_get: ["GET", "/neo/model/settings"], ms_set: ["POST", "/neo/model/settings"],
};

function note(text) {
  const el = $("diag");
  if (!el) return;
  el.classList.toggle("hidden", !text);
  if (text) el.textContent = t("提示：") + text;
}

async function api(action, payload, query) {
  const [method, path] = ROUTES[action] || ["GET", "/neo/status"];
  const opt = { method, headers: { "content-type": "application/json" } };
  if (method === "POST") opt.body = JSON.stringify(payload === undefined ? {} : payload);
  const r = await fetch(path + (query || "") + (action === "log" ? "?tail=60000" : ""), opt);
  const data = await r.json();
  if (!r.ok) note(`${action} ` + t("没成（HTTP ") + `${r.status}：${data.error || t("无详情")}）`);
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
    note(`${action} ` + t("没送到服务：") + `${e}`);
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
  if (name === "models") { loadModels(); loadCatalog(); loadUpstream(); loadRemote(); }
  if (name === "settings") { loadConfig(); loadModelSettings(); }
  if (name === "connect") loadConnect();
}
document.querySelectorAll(".tab").forEach((tab) => tab.onclick = () => showPage(tab.dataset.page));

function pill(el, state) {
  el.className = "pill " + state;
  el.textContent = t(STATE_TEXT[state] || state);
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
  if (diag) $("diag").textContent = t("提示：") + diag;

  const m = s.metrics || {};
  $("m-toks").textContent = num(m.tokens_per_s);
  $("m-step").textContent = num(m.decode_step_ms, 1);
  $("m-vram").textContent = m.gpu_used_mib === undefined ? "—" : num(m.gpu_used_mib, 0);
  $("m-vram-s").textContent = m.gpu_total_mib
    ? `${t("已用 / ")}${num(m.gpu_total_mib, 0)} MiB${m.gpu_name ? " · " + m.gpu_name : ""}` : t("已用 / 总量");
  $("m-ram").textContent = m.ram_used_gb === undefined ? "—" : num(m.ram_used_gb, 1);
  $("m-ram-s").textContent = m.ram_total_gb ? `${t("已用 / ")}${num(m.ram_total_gb, 1)} GB` : t("已用 / 总量");
  $("m-active").textContent = count(m.active);
  $("m-queued").textContent = `${t("排队 ")}${count(m.queued)}`;
  $("m-ctx").textContent = s.max_context ? s.max_context.toLocaleString() : "—";
  $("m-reqs").textContent = `${t("请求 ")}${count(m.requests)}`;

  $("s-state").textContent = t(STATE_TEXT[st] || st) +
    (s.healthy ? (s.loaded ? t(" · 模型已就绪") : t(" · 服务在应答，模型未加载"))
               : (s.listening ? t(" · 端口在听，还没应答") : ""));
  $("s-model").textContent = s.served_model || s.model || "—";
  $("s-port").textContent = `${s.host}:${s.port}` + (byOther ? t("（别人启动的，Neo 认下了它）") : "");
  $("s-pid").textContent = s.pid ? `${s.pid}${byOther ? t("（别人启动的）") : ""}` : "—";
  $("s-restarts").textContent = s.state === "halted"
    ? `${s.restarts.length}${t(" 次 · 已达上限 ")}${s.max_restarts}`
    : `${s.restarts.length}${t(" 次 · 上限 ")}${s.max_restarts} · ${s.backoff.map((x) => x + "s").join("/")}`;
  $("s-log").textContent = s.log_path || "—";

  const box = $("events");
  box.innerHTML = "";
  (s.events || []).slice().reverse().forEach((e) => {
    const row = document.createElement("div");
    const when = document.createElement("time");
    when.textContent = new Date(e.at * 1000).toTimeString().slice(0, 8);
    const span = document.createElement("span");
    span.className = e.kind || "info";
    span.textContent = e.text;
    row.append(when, span);
    box.append(row);
  });

  const pages = s.pages || {};
  $("chat-url").textContent = pages.chat || "";
  $("monitor-url").textContent = pages.monitor || "";
  if ($("page-chat").classList.contains("on")) ensureFrame("chat");
  if ($("page-monitor").classList.contains("on")) ensureFrame("monitor");
}

async function poll() {
  try { render(await api("status")); } catch (e) { note(`${t("控制台服务没应答：")}${e}`); }
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
    meta.textContent = `${m.context ? m.context.toLocaleString() + t(" 上下文 · ") : ""}${t("端口 ")}${m.port} · ` +
      `${m.vision ? t("视觉+文本 · ") : t("文本 · ")}${m.installed ? t("文件在") : t("文件不在")}`;
    info.append(name, meta);
    const cur = m.id === data.current;
    // 服务此刻在不在跑（模型页每 5 秒跟着状态页一起刷，last 就是最近一次的 /neo/status）
    const st = last ? (last.display_state || last.state) : "idle";
    const busy = st === "running" || st === "starting" || st === "stopping" ||
                 st === "restarting" || st === "external";
    const btn = document.createElement("button");
    // 换模型走 supervisor.set_model：它自己先 stop 再 start，用户不用先按停止。
    // 但服务在跑时点它，正在跑的请求会被切断，所以按钮写"换过去"，鼠标上去说清楚它先停谁。
    btn.textContent = cur ? t("正在用") : (busy ? t("换过去") : t("启动它"));
    btn.disabled = cur || !m.installed;
    btn.title = cur ? t("当前服务正在用这个模型")
      : (!m.installed ? t("模型文件不在，起不来")
        : (busy ? t("会先停掉当前服务（正在跑的请求会被切断），再用「") + `${m.name}」` + t("重新起")
                : t("用「") + `${m.name}」` + t("起服务")));
    btn.onclick = async () => { const s = await act("set_model", { model: m.id }); if (s) render(s); };
    row.append(info, btn);
    box.append(row);
  });
  if (!(data.models || []).length) box.innerHTML = `<p class="muted">${t("根目录没有 strata-*.json。")}</p>`;
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
  if (m.recommended) head.append(catTag(t("推荐"), "rec"));
  else if (m.also) head.append(catTag(t("也可")));
  if (m.installed) head.append(catTag(t("已装"), "ok"));
  else if (m.broken) head.append(catTag(t("装好一半"), "warn"));
  if (m.budget) head.append(catTag(t("低内存模式")));
  if (m.experimental) head.append(catTag(t("实验性"), "warn"));
  if (m.nvidia_only) head.append(catTag(t("只 NVIDIA")));
  const meta = document.createElement("div");
  meta.className = "meta";
  meta.textContent = `${m.download_gb}${t(" GB 下载 · ")}${tNum(m.fit_text)}` + (m.about ? ` · ${m.about}` : "");
  info.append(head, meta);
  const btn = document.createElement("button");
  btn.textContent = m.installed ? t("已装好") : (running ? t("安装中…") : (m.installable ? t("安装") : t("装不下")));
  btn.disabled = m.installed || running || !m.installable;
  if (!m.installed && m.installable && !running) btn.classList.add("primary");
  btn.title = m.installed ? `${t("已装好（")}${m.config}${t("）")}`
    : (m.installable ? `${t("在后台跑：")}${installCmd(m, catalogData && catalogData.sources)}${t("（下载要几十分钟）")}` : tNum(m.fit_text));
  btn.onclick = async () => {
    const source = (catalogData && catalogData.sources && catalogData.sources.chosen) || "probe";
    const res = await act("install", { family: m.family, model: m.model, source });
    if (!res) return;
    note(res.started ? `${t("开始装 ")}${m.family_title} ${m.model}${t("（")}${sourceTitle(catalogData, source)}${t("）")}${t("：输出进 neo/state/logs/wizard.log")}`
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
      + (c.key === src.suggested ? t("（建议）") : "");
    opt.title = `${c.note} · ${c.reachable ? t("连得上") : t("这个地址现在连不上")}`;
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
    ? `${t("推荐 ")}${rec.family} / ${rec.model}：${rec.why}。${rec.note || ""}`
    : `${t("读不到安装菜单：")}${data.root} ${t("里的 setup.py 没有 MODELS / FAMILIES 两张表")}`;
  $("cat-cmd").textContent = rec.model ? installCmd(rec, data.sources) : "";
  const running = !!(data.wizard && data.wizard.running);
  const box = $("cat-list");
  box.innerHTML = "";
  (data.models || []).forEach((m) => box.append(catRow(m, running)));
  if (!(data.models || []).length) {
    box.innerHTML = `<p class="muted">${t("读不到可安装清单：")}${data.root} ${t("里没有 setup.py，")}`
      + `${t("或它的模型表读不出来（这不是 Strata 的根目录？）")}</p>`;
  }
  const log = $("cat-log");
  log.textContent = data.log_tail || (running ? t("（安装刚起步，还没有输出）") : t("（还没有安装日志）"));
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
  note(res.ok ? `${t("下载源记下了：")}${sourceTitle(res, source)}${t("，下一次安装就用它")}`
              : (res.error || t("没记下这个源")));
  loadCatalog();
};
$("cat-copy").onclick = async () => {
  const rec = (catalogData && catalogData.recommendation) || {};
  const ok = await copyText(rec.model ? installCmd(rec, catalogData.sources) : "");
  $("cat-copy").textContent = ok ? t("已复制") : t("复制失败");
  setTimeout(() => { $("cat-copy").textContent = t("复制推荐命令"); }, 4000);
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
  text = text || t("（还没有日志）");
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
  $("set-key").placeholder = c.has_api_key ? t("已设置（留空表示不改）") : t("留空 = 本机免密钥");
}
$("set-save").onclick = async () => {
  const body = {};
  const port = Number($("set-port").value);
  if (port) body.port = port;
  if ($("set-host").value.trim()) body.host = $("set-host").value.trim();
  if ($("set-key").value.trim()) body.api_key = $("set-key").value.trim();
  const c = await act("save_config", body);
  if (!c) return;
  $("set-note").textContent = t("已存到 neo/state/launcher.json（端口改了要重启 Neo 才生效）");
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
    (lan.lan_open ? t("没找到局域网地址（这台机器没连上网？）") : t("未开放局域网"));
  $("lan-key").textContent = lan.has_api_key ? lan.api_key_masked
    : t("没设（只绑本机时不需要）");
  $("lan-firewall").textContent = lan.firewall_hint || t("不需要（没绑局域网）");
  if (!lan.has_api_key) setRevealedKey("", "");
  else if (revealedKey && lan.api_key_masked !== revealedMask) setRevealedKey("", "");
}

// 一个 agent 一行：名字 + 那一行命令 + 复制。细节（服务地址、要设的变量、
// 配置文件路径、整块粘贴内容）折在"细节"后面，默认不占地方。
function agentDetails(r, data) {
  const box = document.createElement("div");
  box.className = "agent-more hidden";
  const meta = document.createElement("p");
  meta.className = "muted";
  meta.textContent = `${t("服务 ")}${r.base} · ${t("模型 ")}${r.model}` +
    (r.context ? `（${r.context.toLocaleString()}${t(" 上下文）")}` : "") +
    `${t(" · 配置文件 ")}${r.config_path}` + (data.has_api_key ? "" : t(" · 服务没设 token"));
  const pre = document.createElement("pre");
  pre.className = "cmd";
  pre.textContent = (r.powershell || []).join("\n");
  box.append(meta, pre);
  return box;
}

function renderAgents(data) {
  const box = $("agents");
  box.innerHTML = "";
  (data.recipes || []).forEach((r) => {
    const card = document.createElement("div");
    card.className = "agent";
    const name = document.createElement("span");
    name.className = "name";
    name.textContent = r.title;
    const code = document.createElement("code");
    code.className = "quick";
    code.textContent = r.quick || `python -m neo.start ${r.agent}`;
    const copy = document.createElement("button");
    copy.textContent = t("复制");
    copy.onclick = async () => {
      const ok = await copyText(code.textContent);
      copy.textContent = ok ? t("已复制") : t("复制失败");
      setTimeout(() => { copy.textContent = t("复制"); }, 4000);
    };
    const write = document.createElement("button");
    write.textContent = t("写进配置");
    write.title = `${t("合并进 ")}${r.config_path}${t("（现有的先备份成 .neo-bak）")}`;
    write.onclick = async () => {
      const res = await act("connect_write", { agent: r.agent });
      if (!res) return;
      write.textContent = res.ok ? t("已写入") : t("没写");
      if (res.notes && res.notes.length) note(res.notes.join("；"));
      setTimeout(() => { write.textContent = t("写进配置"); }, 6000);
    };
    const more = document.createElement("button");
    more.textContent = t("细节");
    const details = agentDetails(r, data);
    more.onclick = () => {
      details.classList.toggle("hidden");
      more.textContent = details.classList.contains("hidden") ? t("细节") : t("收起");
    };
    card.append(name, code, copy);
    if (r.config_only) card.append(write);
    card.append(more);
    box.append(card, details);
  });
}

let wizardCmd = "";

function renderWizard(w) {
  const box = $("wizard-steps");
  box.innerHTML = "";
  (w.steps || []).forEach((s) => {
    const line = document.createElement("div");
    line.className = "step" + (s.ok ? " ok" : " warn");
    line.textContent = `${s.ok ? "✓" : "—"} ${s.name}：${s.text}`;
    box.append(line);
  });
  wizardCmd = w.command || "";
  $("wizard-cmd").textContent = t("命令：") + wizardCmd;
  $("wizard-log").textContent = w.log_tail || t("（还没有安装日志）");
  $("wizard-run").disabled = !!(w.run && w.run.running);
  $("wizard-run").textContent = (w.run && w.run.running) ? t("安装中…") : t("开始安装");
}

let engineCmd = "";

function renderEngine(e) {
  const box = $("engine-steps");
  box.innerHTML = "";
  (e.steps || []).forEach((s) => {
    const line = document.createElement("div");
    line.className = "step" + (s.ok ? " ok" : " warn");
    line.textContent = `${s.ok ? "✓" : "—"} ${s.name}：${s.text}`;
    box.append(line);
  });
  engineCmd = e.clone_command || "";
  $("engine-cmd").textContent = `${e.tag || "?"} · ${(e.sha || "").slice(0, 12)} · ${e.state || "?"}：`
    + engineCmd;
  $("engine-log").textContent = e.log_tail || t("（还没有引擎日志）");
  $("engine-run").disabled = !!(e.run && e.run.running);
  $("engine-run").textContent = (e.run && e.run.running) ? t("安装中…") : t("按 pin 安装引擎");
}

async function loadConnect() {
  const [lan, conn, wizard, engine] = await Promise.all([
    api("lan"), api("connect"), api("wizard"), api("engine"),
  ]);
  if (lan && !lan.error) renderLan(lan);
  if (conn && !conn.error) renderAgents(conn);
  if (wizard && !wizard.error) renderWizard(wizard);
  if (engine && !engine.error) renderEngine(engine);
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
    btn.textContent = t("没有 token");
    setTimeout(() => { btn.textContent = t("复制 token"); }, 4000);
    return;
  }
  const ok = await copyText(key);
  btn.textContent = ok ? t("已复制") : t("复制失败");
  setTimeout(() => { btn.textContent = t("复制 token"); }, 4000);
};
$("lan-copy-base").onclick = async () => {
  const ok = await copyText((lanData && lanData.base) || "");
  $("lan-copy-base").textContent = ok ? t("已复制") : t("复制失败");
  setTimeout(() => { $("lan-copy-base").textContent = t("复制服务地址"); }, 4000);
};
$("lan-copy-fw").onclick = async () => {
  const ok = await copyText((lanData && lanData.firewall_hint) || "");
  $("lan-copy-fw").textContent = ok ? t("已复制") : t("复制失败");
  setTimeout(() => { $("lan-copy-fw").textContent = t("复制防火墙命令"); }, 4000);
};
$("wizard-refresh").onclick = async () => {
  $("wizard-refresh").textContent = t("探测中…");
  await fetch("/neo/net?fresh=1", { headers: { accept: "application/json" } });
  const w = await api("wizard");
  if (w && !w.error) renderWizard(w);
  $("wizard-refresh").textContent = t("重新探测网络");
};
$("wizard-run").onclick = async () => {
  const res = await act("wizard_run", {});
  if (!res) return;
  note(res.started ? t("安装已在后台跑，输出进 neo/state/logs/wizard.log")
                   : (res.notes || []).join("；"));
  setTimeout(async () => { const w = await api("wizard"); if (w && !w.error) renderWizard(w); }, 3000);
};
$("wizard-copy").onclick = async () => {
  const ok = await copyText(wizardCmd);
  $("wizard-copy").textContent = ok ? t("已复制") : t("复制失败");
  setTimeout(() => { $("wizard-copy").textContent = t("复制安装命令"); }, 4000);
};
$("engine-run").onclick = async () => {
  const res = await act("engine_run", { action: "install" });
  if (!res) return;
  note(res.started ? t("引擎已在后台跑，输出进 neo/state/logs/engine.log")
                   : (res.error || (res.notes || []).join("；")));
  setTimeout(async () => { const e = await api("engine"); if (e && !e.error) renderEngine(e); }, 3000);
};
$("engine-copy").onclick = async () => {
  const ok = await copyText(engineCmd);
  $("engine-copy").textContent = ok ? t("已复制") : t("复制失败");
  setTimeout(() => { $("engine-copy").textContent = t("复制 clone 命令"); }, 4000);
};

// ---------------- 模型页：在跑的服务（Neo 代发上游只读端点）----------------
// 页面开在 8083，直接问 8080 的面板端点读不到（上游不给跨源的 Access-Control-Allow-Origin），
// 所以走 /neo/upstream?what=models|status|metrics —— token 留在 Neo 手里。
function upRow(label, value) {
  const row = document.createElement("div");
  row.className = "up-row";
  const k = document.createElement("span");
  k.className = "k";
  k.textContent = label;
  const v = document.createElement("span");
  v.className = "v";
  v.textContent = (value === null || value === undefined || value === "") ? "—" : String(value);
  row.append(k, v);
  return row;
}

async function loadUpstream() {
  const box = $("up-list");
  const [models, status, metrics] = await Promise.all([
    api("upstream", undefined, "?what=models"),
    api("upstream", undefined, "?what=status"),
    api("upstream", undefined, "?what=metrics"),
  ]);
  box.innerHTML = "";
  const st = (status && status.data) || {};
  const m = (metrics && metrics.data) || {};
  const ids = ((models && models.data && models.data.data) || []).map((x) => x.id || x.name || "");
  box.append(upRow(t("模型"), st.model || ids.join(" / ")));
  box.append(upRow(t("上下文"), st.max_context ? Number(st.max_context).toLocaleString() : ""));
  box.append(upRow(t("就绪"), (status && status.ok) ? (st.loaded ? t("就绪") : t("未就绪")) : ""));
  box.append(upRow(t("生成速度"), `${num(m.tokens_per_s)} tok/s`));
  box.append(upRow(t("显存"), m.gpu_used_mib === undefined ? "" : `${num(m.gpu_used_mib, 0)} MiB`));
  box.append(upRow(t("在跑的请求"), count(m.active)));
  const eng = m.engine || {};
  box.append(upRow(t("引擎实报"), eng.expert_cache_mib === undefined ? ""
    : `${eng.expert_cache_mib} MiB · ${t("空闲显存 ")}${num(eng.vram_free_mib, 0)} MiB`));
  const bad = [models, status, metrics].find((x) => x && !x.ok);
  $("up-note").textContent = bad ? `${t("上游没答：")}${bad.error}` : ((models && models.base) || "—");
}
$("up-reload").onclick = loadUpstream;
$("up-open").onclick = () => act("open_page", { page: "monitor" });
// ---------------- 模型页：远程加载（两个环境变量 + 每个 agent 一行）----------------
// 与「接入」页那一行是同一个命令，连谁由 NEO_SERVER_URL / NEO_API_KEY 说了算。
// 页面上写的是占位符；点「复制」才把当前生效的 key 填进去（完整 key 只走本机请求）。
let remoteData = null;

async function fillRemoteKey(line) {
  const url = $("rm-url").value.trim();
  const key = $("rm-key").value.trim() || await revealKey();
  const base = url || (remoteData && remoteData.base) || "";
  const ph = (remoteData && remoteData.placeholder) || "sk-neo-…（点「取 token」换上真的那把）";
  return String(line).split(ph).join(key || ph).replace(/NEO_SERVER_URL=[^\s"]+/, `NEO_SERVER_URL=${base}`);
}

function renderRemote(data) {
  remoteData = data;
  const box = $("rm-list");
  box.innerHTML = "";
  (data.rows || []).forEach((r) => {
    const card = document.createElement("div");
    card.className = "agent";
    const name = document.createElement("span");
    name.className = "name";
    name.textContent = r.title;
    const code = document.createElement("code");
    code.className = "quick";
    code.textContent = r.windows;
    const copy = document.createElement("button");
    copy.textContent = t("复制");
    copy.onclick = async () => {
      const ok = await copyText(await fillRemoteKey(r.windows));
      copy.textContent = ok ? t("已复制") : t("复制失败");
      setTimeout(() => { copy.textContent = t("复制"); }, 4000);
    };
    card.append(name, code, copy);
    box.append(card);
  });
  $("rm-note").textContent = (data.bases || []).join("  ") || data.note || "—";
}

async function loadRemote() {
  const data = await api("remote");
  if (data && !data.error) renderRemote(data);
}

$("rm-token").onclick = async () => {
  const btn = $("rm-token");
  const key = await revealKey();
  if (!key) {
    btn.textContent = t("没有 token");
    setTimeout(() => { btn.textContent = t("取本机 token"); }, 4000);
    return;
  }
  $("rm-key").value = key;
  btn.textContent = t("已填上");
  setTimeout(() => { btn.textContent = t("取本机 token"); }, 4000);
};
$("rm-copy").onclick = async () => {
  const btn = $("rm-copy");
  const rows = (remoteData && remoteData.rows) || [];
  if (!rows.length) return;
  const ok = await copyText(await fillRemoteKey(rows[0].windows));
  btn.textContent = ok ? t("这一行已复制") : t("复制失败");
  setTimeout(() => { btn.textContent = t("复制这一行"); }, 4000);
};
// ---------------- 设置页里的模型设置块（上下文 / KV / 图片 / 显存保留）----------------
// 四栏都是引擎本来就认的参数。Neo 不改 serve/server.py，也不改 setup.py 写的那份
// strata-*.json：改动写进 neo/state/configs/ 的一份覆盖配置，启动时 --config 指到它。
let msData = null;

function msFill(select, items, current) {
  select.innerHTML = "";
  (items || []).forEach((it) => {
    const opt = document.createElement("option");
    opt.value = it.value;
    opt.textContent = it.title + (it.note ? ` — ${it.note}` : "");
    if (String(it.value) === String(current)) opt.selected = true;
    select.append(opt);
  });
}

function msEst(v) {
  const e = v.estimate || {};
  $("ms-layers").textContent = e.layers ? `${e.layers} · ${t("KV 单元 ")}${(e.kv_cells || 0).toLocaleString()}` : "—";
  $("ms-kvsize").textContent = e.kv_gib === undefined ? "—"
    : `${e.kv_gib} GiB · ${e.kv_mode === "resident" ? t("常驻") : t("整段")} · ${e.kv_bytes_per_cell} B`;
  $("ms-cache").textContent = e.expert_cache === undefined ? "—"
    : (e.expert_cache === "auto" ? t("引擎自算") : `${e.expert_cache_mib} MiB`);
  $("ms-vision-size").textContent = e.vision_gib === undefined ? "—" : `${e.vision_gib} GiB`;
  $("ms-reserve-size").textContent = e.reserve_mib === undefined ? "—" : `${e.reserve_mib} MiB`;
  $("ms-total").textContent = e.total_gib === undefined ? "—"
    : `${e.total_gib} GiB` + ((e.headroom_gib === null || e.headroom_gib === undefined)
        ? "" : ` · ${t("余量 ")}${e.headroom_gib} GiB`);
  $("ms-gpu").textContent = e.gpu_name ? `${e.gpu_name} · ${e.gpu_total_gib} GiB` : "—";
  const live = e.live || {};
  $("ms-live").textContent = Object.keys(live).length
    ? `${t("空闲显存 ")}${num(live.vram_free_mib, 0)} MiB · ${t("上下文 ")}${live.max_context || "—"}`
    : t("服务没在跑");
  $("ms-est-note").textContent = e.note || "—";
}

function renderModelSettings(v) {
  msData = v;
  const sel = $("ms-model");
  if (!v.ok) {
    $("ms-note").textContent = v.error || t("读不到这个模型的设置");
    sel.innerHTML = "";
    (v.models || []).forEach((id) => {
      const opt = document.createElement("option");
      opt.value = id;
      opt.textContent = id;
      sel.append(opt);
    });
    return;
  }
  sel.innerHTML = "";
  const opt = document.createElement("option");
  opt.value = v.id;
  opt.textContent = `${v.model} (${v.id})`;
  opt.selected = true;
  sel.append(opt);
  const s = v.settings || {};
  const c = v.choices || {};
  msFill($("ms-context"), c.context, s.context);
  msFill($("ms-kv"), c.kv, s.kv);
  msFill($("ms-kvres"), c.kv_resident, s.kv_resident);
  msFill($("ms-reserve"), c.vram_reserve, s.vram_reserve_mib);
  const vis = $("ms-vision");
  vis.checked = !!s.vision;
  vis.disabled = !s.vision_available;
  $("ms-restart").classList.toggle("hidden", !v.restart_required);
  msEst(v);
  $("ms-cfg-note").textContent = v.overridden
    ? `${t("覆盖配置：")}${v.overlay}`
    : `${t("这份是 setup.py 写的原样：")}${v.config}`;
}

async function loadModelSettings(model) {
  const q = model ? `?model=${encodeURIComponent(model)}` : "";
  const v = await api("ms_get", undefined, q);
  if (v) renderModelSettings(v);
}
function msBody() {
  return {
    model: (msData && msData.id) || "",
    context: Number($("ms-context").value) || 0,
    kv: $("ms-kv").value,
    kv_resident: Number($("ms-kvres").value) || 0,
    vram_reserve_mib: Number($("ms-reserve").value) || 0,
    vision: $("ms-vision").checked ? "on" : "off",
  };
}

async function msSave() {
  const res = await act("ms_set", msBody());
  if (!res) return null;
  $("ms-note").textContent = res.ok ? t("已保存") : ((res.notes || [t("没保存")]).join("；"));
  if (res.ok && msData) loadModelSettings(msData.id);
  return res;
}

$("ms-save").onclick = msSave;
$("ms-apply").onclick = async () => {
  const res = await msSave();
  if (!res || !res.ok) return;
  // 换模型走 supervisor.set_model：它自己先 stop 再 start，启动时 --config 指到覆盖配置
  const s = await act("set_model", { model: (msData && msData.id) || "" });
  if (s) render(s);
};
$("ms-reset").onclick = async () => {
  const res = await act("ms_set", { model: (msData && msData.id) || "", action: "reset" });
  if (!res) return;
  $("ms-note").textContent = res.ok ? t("已恢复 setup.py 的原样") : ((res.notes || [t("没恢复")]).join("；"));
  if (msData) loadModelSettings(msData.id);
};
$("ms-model").onchange = () => { if ($("ms-model").value) loadModelSettings($("ms-model").value); };

poll();

setInterval(poll, 2000);
setInterval(() => { if ($("page-logs").classList.contains("on")) loadLog(); }, 5000);
// 模型页开着时跟着刷：安装是在后台跑的，页面要能看出"装到哪了、装好没有"
setInterval(() => { if ($("page-models").classList.contains("on")) loadCatalog(); }, 5000);
// 在跑的服务那一栏跟着状态页刷（2 秒一次），远程加载那一屏不常变，跟着 5 秒刷
setInterval(() => { if ($("page-models").classList.contains("on")) loadUpstream(); }, 5000);

