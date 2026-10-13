// Neo Strata 控制台的中英切换与明暗主题。
//
// 两条路，各管一半的文案：
//   静态的（index.html 里的标题、按钮、说明）—— 元素带 data-i18n="键"，表里给英文，
//         整块换 innerHTML（带 <code> 的说明连标记一起换）。
//   动态的（app.js 拼出来的那些）—— 拼的时候过一遍 t("中文")，表里没有就原样回。
//
// 默认中文 + 暗色：localStorage 里没有记录时页面与以前逐字一样（i18n.js 不改一处文字，
// 只认 data-i18n 与 t()）。记忆键 neo.lang / neo.theme；窗口是 http://127.0.0.1 的安全
// 上下文，pywebview（WebView2）与浏览器里 localStorage 都能用。
//
// 主题只换 CSS 变量：style.css 里 html[data-theme="light"] 那一节覆盖 :root 的配色，
// 组件一律用 var(--…)。硬编码的深色（日志框、命令框）跟着换成 var(--code-bg)。
"use strict";

// 整个文件包在一个 IIFE 里，只往外递一个 window.NEO_I18N。
//
// 为什么必须包：index.html 里 <script src="/i18n.js"> 与 <script src="/app.js"> 是
// 同一个全局作用域的两段经典脚本。i18n.js 顶层的 function t / function applyLang 与
// app.js 顶层的 const t / const applyLang 撞名，浏览器在**解析** app.js 时就抛
// "Identifier 't' has already been declared" —— app.js 一行都不执行：poll() 没跑起来
// （状态胶囊永远停在"未启动"、卡片全是"—"），document.querySelectorAll(".tab") 那一批
// onclick 一个都没挂上（任何点击都没反应）。node --check 逐文件查，看不见跨文件重名，
// 所以 385 条测试全绿、窗口却是死的。实测 2026-10-13 的 NeoStrata.exe（13,981,433 字节）。
// 防回归：neo/tests/test_ui.py 的
// TestLanguageAndTheme.test_the_two_scripts_do_not_share_top_level_names。
(() => {
"use strict";

const LANG_KEY = "neo.lang";
const THEME_KEY = "neo.theme";
const PHRASES = {};          // 动态文案：中文原文 -> 英文
const BLOCKS = {};           // 静态文案：data-i18n 键 -> 英文（HTML）
const ATTRS = {};            // placeholder / title：中文原文 -> 英文

function neoLang() {
  let got = "zh";
  try { got = localStorage.getItem(LANG_KEY) || "zh"; } catch (e) { got = "zh"; }
  return got === "en" ? "en" : "zh";
}

function neoTheme() {
  let got = "dark";
  try { got = localStorage.getItem(THEME_KEY) || "dark"; } catch (e) { got = "dark"; }
  return got === "light" ? "light" : "dark";
}

function applyTheme() {
  document.documentElement.dataset.theme = neoTheme();
  const btn = document.getElementById("btn-theme");
  if (btn) btn.textContent = neoTheme() === "dark" ? "☀" : "☾";
  const lang = document.getElementById("btn-lang");
  if (lang) lang.textContent = neoLang() === "zh" ? "EN" : "中";
}

// 动态文案：认得就换，认不得原样回 —— 漏一条只是那一句还是中文，不会白屏
function t(text) {
  if (neoLang() === "zh") return text;
  const got = PHRASES[text];
  return got === undefined ? text : got;
}

function attrText(name) {
  if (neoLang() === "zh") return name;
  const got = ATTRS[name];
  return got === undefined ? name : got;
}

// 整块换静态文案。zh 时什么都不做（页面就是 index.html 里那份中文，一字不差）。
// app.js 每次重渲染完再调一次，动态生成的那一块也认 data-i18n。
function applyLang(root) {
  const scope = root || document;
  if (neoLang() === "zh") return;
  scope.querySelectorAll("[data-i18n]").forEach((el) => {
    const html = BLOCKS[el.dataset.i18n];
    if (html !== undefined) el.innerHTML = html;
  });
  scope.querySelectorAll("[placeholder]").forEach((el) => {
    el.placeholder = attrText(el.getAttribute("placeholder"));
  });
  scope.querySelectorAll("[title]").forEach((el) => {
    el.title = attrText(el.getAttribute("title"));
  });
}

function setLang(next) {
  const lang = next === "en" ? "en" : "zh";
  try { localStorage.setItem(LANG_KEY, lang); } catch (e) { /* 隐私模式：这一次不记 */ }
  document.documentElement.lang = lang === "zh" ? "zh-CN" : "en";
  applyLang(document);
  applyTheme();
}

function setTheme(next) {
  try { localStorage.setItem(THEME_KEY, next === "light" ? "light" : "dark"); } catch (e) { /* 同上 */ }
  applyTheme();
}
// ---------------- 动态文案（状态页 / 服务行）----------------
// 键是 app.js 里出现过的中文原文；认不得的原样回，不会白屏。
Object.assign(PHRASES, {
  '未启动': 'Idle',
  '启动中': 'Starting',
  '运行中': 'Running',
  '自动重启中': 'Restarting',
  '停止中': 'Stopping',
  '已停止（熔断）': 'Stopped (tripped)',
  '运行中（外部启动）': 'Running (external)',
  '提示：': 'Note:',
  '无详情': 'no details',
  '此刻能按的那个': 'whichever is available',
  '停止': 'Stop',
  '启动': 'Start',
  '重试': 'Retry',
  '刷新': 'Refresh',
  '已用 / 总量': 'used / total',
  '排队 ': 'queued ',
  '请求 ': 'requests ',
  ' · 模型已就绪': ' · model ready',
  ' · 服务在应答，模型未加载': ' · server answering, model not loaded',
  ' · 端口在听，还没应答': ' · port listening, no answer yet',
  '（别人启动的，Neo 认下了它）': ' (started by someone else, Neo adopted it)',
  '（别人启动的）': ' (started by someone else)',
  ' 次 · 已达上限 ': ' restarts · limit reached ',
  ' 次 · 上限 ': ' restarts · limit ',
  ' 次退避 ': ' restarts, backoff ',
});

// ---------------- 动态文案（模型页 / 安装 / 接入）----------------
Object.assign(PHRASES, {
  ' 上下文 · ': ' ctx · ',
  '上下文 ': 'context ',
  '端口 ': 'port ',
  '视觉+文本 · ': 'vision+text · ',
  '文本 · ': 'text · ',
  '文件在': 'file present',
  '文件不在': 'file missing',
  '换过去': 'Switch to it',
  '正在用': 'In use',
  '启动它': 'Start it',
  '当前服务正在用这个模型': 'The running service already uses this model',
  '模型文件不在，起不来': 'Model file missing, cannot start',
  '会先停掉当前服务（正在跑的请求会被切断），再用「': 'This stops the running service first (in-flight requests are cut), then starts ',
  '」重新起': ' again',
  '用「': 'Start ',
  '」起服务': ' as the service',
  '安装': 'Install',
  '已装好': 'Installed',
  '安装中…': 'Installing…',
  '装不下': "Won't fit",
  '下载源': 'Download source',
  '自动': 'Auto',
  '推荐': 'recommended',
  '也可': 'also fine',
  '已装': 'installed',
  '装好一半': 'partly installed',
  '低内存模式': 'low-RAM mode',
  '实验性': 'experimental',
  '只 NVIDIA': 'NVIDIA only',
  ' GB 下载 · ': ' GB download · ',
  '已装好（': 'Installed (',
  '）': ')',
  '在后台跑：': 'Running in the background: ',
  '（下载要几十分钟）': ' (downloads take tens of minutes)',
  '开始装 ': 'Starting the install of ',
  '：输出进 neo/state/logs/wizard.log': ': output goes to neo/state/logs/wizard.log',
  '（建议）': ' (suggested)',
  '连得上': 'reachable',
  '这个地址现在连不上': 'not reachable right now',
  '推荐 ': 'Recommended ',
  '读不到安装菜单：': 'Cannot read the install menu: no MODELS / FAMILIES tables in ',
  '里的 setup.py 没有 MODELS / FAMILIES 两张表': "'s setup.py has neither the MODELS nor the FAMILIES table",
  '读不到可安装清单：': 'Cannot read the installable list: no setup.py in ',
  '里没有 setup.py，': "'s root has no setup.py, ",
  '或它的模型表读不出来（这不是 Strata 的根目录？）': 'or its model tables cannot be parsed (is this not a Strata root?)',
  '（安装刚起步，还没有输出）': '(install just started, no output yet)',
  '（还没有安装日志）': '(no install log yet)',
  '（还没有日志）': '(no log yet)',
  '下载源记下了：': 'Download source saved: ',
  '，下一次安装就用它': ', the next install will use it',
  '没记下这个源': 'Source not saved',
  '已复制': 'Copied',
  '复制失败': 'Copy failed',
  '复制推荐命令': 'Copy the suggested command',
  '复制': 'Copy',
  '写进配置': 'Write into config',
  '合并进 ': 'Merged into ',
  '（现有的先备份成 .neo-bak）': ' (the current file is backed up as .neo-bak first)',
  '已写入': 'Written',
  '没写': 'Not written',
  '收起': 'Collapse',
  '命令：': 'Command:',
  '开始安装': 'Start install',
  '没有 token': 'No token',
  '复制 token': 'Copy token',
  '复制服务地址': 'Copy base URL',
  '复制防火墙命令': 'Copy firewall command',
  '探测中…': 'Probing…',
  '重新探测网络': 'Re-probe the network',
  '安装已在后台跑，输出进 neo/state/logs/wizard.log': 'Install is running in the background; output goes to neo/state/logs/wizard.log',
  '复制安装命令': 'Copy the install command',
  '装到哪了、装好没有': 'Install progress',
});
// ---------------- 动态文案（设置页 / 局域网 / 模型设置页）----------------
Object.assign(PHRASES, {
  '已设置（留空表示不改）': 'Set (leave blank to keep it)',
  '留空 = 本机免密钥': 'Blank = no key on localhost',
  '已存到 neo/state/launcher.json（端口改了要重启 Neo 才生效）': 'Saved to neo/state/launcher.json (a new port needs a Neo restart)',
  '没找到局域网地址（这台机器没连上网？）': 'No LAN address found (is this machine offline?)',
  '未开放局域网': 'LAN not opened',
  '没设（只绑本机时不需要）': 'Not set (not needed when bound to localhost only)',
  '不需要（没绑局域网）': 'Not needed (not bound to the LAN)',
  '细节': 'Details',
  '服务 ': 'Service ',
  ' 上下文）': ' context)',
  ' · 配置文件 ': ' · config file ',
  ' · 服务没设 token': ' · the service has no token set',
  '本机免密钥': 'no key on localhost',
  '本机': 'localhost',
  '局域网': 'LAN',
  '没在听': 'not listening',
  '在听': 'listening',
  '应答': 'answering',
  '没应答': 'no answer',
  '已开': 'open',
  '未开': 'closed',
  '就绪': 'ready',
  '未就绪': 'not ready',
  '在跑': 'running',
  '空闲': 'idle',
  '装好': 'installed',
  '没装': 'not installed',
  '半装': 'partly installed',
  '这台机器上的模型': 'Models on this machine',
  '这台机器': 'This machine',
  '上游服务': 'Upstream service',
  '远程接入': 'Remote access',
  '模型设置': 'Model settings',
  '安装模型': 'Install a model',
  '接入': 'Connect',
  '状态': 'Status',
  '日志': 'Logs',
  '监控': 'Monitor',
  '对话': 'Chat',
  '保存': 'Save',
  '重启服务': 'Restart service',
  '恢复默认': 'Restore defaults',
  '应用并重启': 'Apply and restart',
  '估算': 'Estimate',
  '显存': 'VRAM',
  '内存': 'RAM',
  '磁盘': 'Disk',
  '空闲显存': 'Free VRAM',
  '总显存': 'Total VRAM',
  '视觉': 'Vision',
  '开': 'on',
  '关': 'off',
  '未知': 'unknown',
  '无': 'none',
  '有': 'present',
  '在': 'on',
  '不在': 'off',
  '默认': 'default',
  '引擎': 'engine',
  '覆盖': 'overlay',
  '没有可设置的模型': 'No model to configure',
  '读不到这个模型的设置': 'Cannot read this model\'s settings',
  '已保存': 'Saved',
  '没保存': 'Not saved',
  '已恢复 setup.py 的原样': 'Restored the setup.py defaults',
  '没恢复': 'Not restored',
  '保存后点「重启并应用」': 'Press "Restart and apply" after saving',
  '服务在跑，改动要重启才生效': 'The service is running; a restart applies the change',
  '服务没在跑，启动时就带上这份设置': 'The service is stopped; the next start uses these settings',
  '取本机 token': 'Take this machine\'s token',
  '复制这一行': 'Copy this line',
  '这一行已复制': 'Line copied',
  '先填服务地址': 'Fill in the service URL first',
  '本机没有 token（只绑本机时不需要）': 'No local token (not needed when bound to localhost)',
  '重测连通性': 'Re-test connectivity',
  '生成 token': 'Generate token',
  '打开聊天页': 'Open chat page',
  '打开监控页': 'Open monitor page',
  '打开日志文件': 'Open log file',
  '重载': 'Reload',
  '在浏览器打开': 'Open in browser',
  '用记事本打开': 'Open in Notepad',
  '去装模型': 'Go install a model',
  '看安装日志': 'View the install log',
  '重新检查': 'Re-check',
});

// ---------------- placeholder / title ----------------
Object.assign(ATTRS, {
  '留空 = 本机免密钥': 'Blank = no key on localhost',
  '中英切换': 'Switch language',
  '明暗切换': 'Switch theme',
  'http://192.168.1.20:8080': 'http://192.168.1.20:8080',
  'sk-neo-…': 'sk-neo-…',
});
// ---------------- 静态文案（导航 / 状态页）----------------
// 值是 HTML：带 <code> 的说明整块换，标记不丢。zh 时 applyLang 不动页面。
Object.assign(BLOCKS, {
  'nav.status': 'Status',
  'nav.models': 'Models',
  'nav.modelset': 'Model settings',
  'nav.chat': 'Chat',
  'nav.monitor': 'Monitor',
  'nav.logs': 'Logs',
  'nav.connect': 'Connect',
  'nav.settings': 'Settings',
  'h.start': 'Start',
  'h.stop': 'Stop',
  'h.retry': 'Retry',
  'h.refresh': 'Refresh',
  'guide.h2': 'This machine has no model installed',
  'guide.p': 'Neo is the manager for Strata; a model has to be installed first. Open the ' +
    '<b>Models</b> page to see which one fits this machine\'s RAM and press <b>Install</b>; ' +
    'you can also run <code>START-HERE.bat</code> (Windows) or <code>./setup.sh</code> (Linux) ' +
    'in the project folder, then come back to this window to start it.',
  'guide.models': 'Go install a model',
  'guide.log': 'View the install log',
  'guide.reload': 'Re-check',
  'card.toks': 'Generation speed',
  'card.step': 'Decode step',
  'card.step.s': 'ms / step',
  'card.vram': 'VRAM',
  'card.ram': 'RAM',
  'card.active': 'Active requests',
  'card.ctx': 'Context',
  'panel.svc': 'Service',
  'svc.state': 'State',
  'svc.model': 'Model',
  'svc.port': 'Port',
  'svc.pid': 'Process',
  'svc.restarts': 'Auto-restart',
  'svc.log': 'Log',
  'btn.chat': 'Open chat page',
  'btn.monitor': 'Open monitor page',
  'btn.log': 'Open log file',
  'panel.events': 'What happened',
  'panel.install': 'Download & install a model',
  'src.label': 'Download source',
  'src.retest': 'Re-test connectivity',
  'cat.refresh': 'Refresh',
  'cat.copy': 'Copy the suggested command',
  'cat.note1': 'The list comes from <code>setup.py</code>\'s install menu (Neo only reads those ' +
    'two tables, it never runs it). Pressing <b>Install</b> runs ' +
    '<code>setup.py --yes --no-start --family … --model …</code> in the background: downloads ' +
    'take tens of minutes and output goes to <code>neo/state/logs/wizard.log</code>. When it ' +
    'finishes, start it from "Models on this machine" below. The recommendation is by RAM; the ' +
    'tiers are step 3 of <code>docs/AI_SETUP.md</code> and <code>docs/MODELS.md</code>.',
  'cat.note2': 'The download source follows the probe by default: both sources reachable means ' +
    '<code>Hugging Face</code> (the upstream default); only <code>ModelScope</code> reachable ' +
    'means <code>--source modelscope</code>. The row above lets you pick yourself; the choice ' +
    'is saved in <code>neo/state/source.json</code> and the next install uses it. The mirror ' +
    'route sets <code>HF_ENDPOINT=https://hf-mirror.com</code> for <code>setup.py</code> ' +
    '(<code>--source</code> has no mirror option).',
  'panel.models': 'Models on this machine',
  'models.note': 'The list comes from the strata-*.json files in the project root, most ' +
    'recently used first. Switching model restarts the service.',
  'panel.up': 'Running service',
  'up.reload': 'Refresh',
  'up.open': 'Open the monitor page',
  'up.note': 'These two panels read the service\'s own <code>/v1/models</code> and ' +
    '<code>/status</code>: the page runs on 8083, so asking the 8080 panel endpoints directly ' +
    'fails (the upstream sends no <code>Access-Control-Allow-Origin</code>) — Neo asks on the ' +
    'page\'s behalf, the token stays with Neo, and the page only ever sees this JSON.',
  'panel.remote': 'Remote load',
  'rm.url': 'Service URL',
  'rm.key': 'token',
  'rm.token': 'Take this machine\'s token',
  'rm.copy': 'Copy this line',
  'rm.note': 'Remote load is two environment variables: <code>NEO_SERVER_URL</code> and ' +
    '<code>NEO_API_KEY</code>. Set them, run the line below, and the agent connects to that ' +
    'Strata while this machine\'s own service stays untouched — the same command as the ' +
    'Connect page, only pointed at someone else.',
});
// ---------------- 静态文案（模型设置 / 页框 / 接入 / 设置）----------------
Object.assign(BLOCKS, {
  'panel.modelset': 'Model settings',
  'ms.model': 'Model',
  'ms.restart': 'Restart to apply',
  'ms.context': 'Context length',
  'ms.kv': 'KV cache type',
  'ms.kvres': 'KV resident units',
  'ms.reserve': 'VRAM reserve (MiB)',
  'ms.vision': 'Images (vision encoder)',
  'ms.save': 'Save',
  'ms.apply': 'Restart and apply',
  'ms.reset': 'Restore setup.py defaults',
  'ms.th.layers': 'Layers',
  'ms.th.kvsize': 'KV resident',
  'ms.th.cache': 'Expert cache',
  'ms.th.vision': 'Vision encoder',
  'ms.th.reserve': 'VRAM reserve',
  'ms.th.total': 'Total (estimated)',
  'ms.th.gpu': 'GPU',
  'ms.th.live': 'Engine report',
  'ms.note': 'All four are parameters the engine already understands ' +
    '(<code>--max-context</code> / <code>--kv</code> / <code>--vision</code> / ' +
    '<code>--vram-reserve-mib</code>). Neo changes neither <code>serve/server.py</code> nor the ' +
    '<code>strata-*.json</code> that <code>setup.py</code> wrote: changes go into an overlay ' +
    'config under <code>neo/state/configs/</code> and the launch points <code>--config</code> ' +
    'at it. Delete that file and you are back to the original.',
  'frame.reload': 'Reload',
  'frame.open': 'Open in browser',
  'log.reload': 'Refresh',
  'log.open': 'Open in Notepad',
  'panel.lan': 'LAN & token',
  'lan.th.state': 'State',
  'lan.th.loopback': 'Local address',
  'lan.th.bases': 'LAN address',
  'lan.th.key': 'token',
  'lan.th.firewall': 'Firewall',
  'lan.token': 'Generate token',
  'lan.copy.key': 'Copy token',
  'lan.copy.base': 'Copy base URL',
  'lan.copy.fw': 'Copy firewall command',
  'lan.note': 'The bind address is changed on the <b>Settings</b> page (0.0.0.0 = also listen ' +
    'on the LAN). Once bound to the LAN a token is mandatory: any web page on this machine can ' +
    'reach this port. After generating a token press <b>Stop</b> then <b>Start</b> so the ' +
    'running service honours that key. "Copy token" returns the key currently in effect; only ' +
    'requests from this machine (127.0.0.1) get the full key, and the full key is shown in this ' +
    'one input box.',
  'panel.agents': 'One-line coding agent access',
  'agents.note': 'Each line is a single command: copy it into a terminal and Neo works out the ' +
    'base URL, token, model and context window and launches the agent straight away (add ' +
    '<code>--no-launch</code> to only print the command). Codex / OpenCode / OpenClaw read ' +
    'config files; <b>Write into config</b> backs the existing file up as <code>.neo-bak</code> ' +
    'and merges (Neo leaves a file it cannot parse alone). To reach a remote Strata: set the ' +
    '<code>NEO_SERVER_URL</code> and <code>NEO_API_KEY</code> environment variables and run ' +
    'the same command.',
  'panel.wizard': 'Install wizard',
  'wizard.refresh': 'Re-probe the network',
  'wizard.run': 'Start install',
  'wizard.copy': 'Copy the install command',
  'panel.settings': 'Launch settings',
  'set.port': 'Strata port',
  'set.host': 'Bind address',
  'set.key': 'API key',
  'set.save': 'Save',
  'set.note': 'A new port needs a Neo restart. Binding to 0.0.0.0 lets people on the LAN use ' +
    'this model, and then an API key is required (LAN opening and the install wizard are M3).',
});

// ---------------- 动态文案（app.js 里带数字之外的零散句与标签）----------------
Object.assign(PHRASES, {
  '没成（HTTP ': ' failed (HTTP ',
  '没送到服务：': ' never reached the console: ',
  '控制台服务没应答：': 'Console not answering: ',
  '已用 / ': 'used / ',
  '重新起': ' again',
  '起服务': ' as the service',
  '（': ' (',
  '）': ')',
  '根目录没有 strata-*.json。': 'No strata-*.json in the project root.',
  '模型': 'Model',
  '模型 ': 'Model ',
  '上下文': 'Context',
  '生成速度': 'Generation speed',
  '在跑的请求': 'Active requests',
  '引擎实报': 'Engine report',
  '就绪': 'Ready',
  '未就绪': 'Not ready',
  '上游没答：': 'Upstream not answering: ',
  '空闲显存 ': 'Free VRAM ',
  'KV 单元 ': 'KV cells ',
  '常驻': 'resident',
  '整段': 'full context',
  '引擎自算': 'engine picks',
  '余量 ': 'headroom ',
  '服务没在跑': 'service idle',
  '覆盖配置：': 'Overlay config: ',
  '这份是 setup.py 写的原样：': 'Unchanged setup.py config: ',
  '已填上': 'Filled in',
  '没找到局域网地址（这台机器没连上网？）': 'No LAN address found (is this machine offline?)',
});

// ---------------- 启动 ----------------
// 窗口一开就把记忆的语言与主题落到位；没有记忆时 applyLang 什么都不做，页面就是中文暗色。
function bootPrefs() {
  document.documentElement.lang = neoLang() === "zh" ? "zh-CN" : "en";
  applyTheme();
  applyLang(document);
  const lang = document.getElementById("btn-lang");
  if (lang) lang.addEventListener("click", () => setLang(neoLang() === "zh" ? "en" : "zh"));
  const theme = document.getElementById("btn-theme");
  if (theme) theme.addEventListener("click", () => setTheme(neoTheme() === "dark" ? "light" : "dark"));
}

if (typeof document !== "undefined") {
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", bootPrefs);
  } else {
    bootPrefs();
  }
}

// 给 app.js 用：window.NEO_I18N = { t, applyLang, setLang, setTheme, neoLang, neoTheme }
window.NEO_I18N = { t, attrText, applyLang, setLang, setTheme, neoLang, neoTheme, PHRASES, BLOCKS, ATTRS };
})();



