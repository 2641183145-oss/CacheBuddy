/* Cache Buddy — app logic. Polls pywebview api.state() every second;
   spaces list refreshes on expand and every 30s. */
"use strict";

const api = () => window.pywebview.api;
const $ = id => document.getElementById(id);

const MODE_COLOR = {
  guarding: "#3FA46A", busy: "#E0A042", held: "#E0A042",
  injecting: "#3FA46A",
  sent: "#3FA46A", retry: "#D14343", failed: "#D14343",
  none: "#9CA3AF", loading: "#9CA3AF", expired: "#9CA3AF",
};

// armed but waiting on the user's next message (no capture yet, or the
// cache lapsed): its own calm blue, so it never reads as plain Idle
const WAIT_COLOR = "#5B8DD6";
const isWaiting = s => !!(s && s.target && (s.mode === "loading" || s.mode === "expired"));

let S = null;                 // last api.state()
let dueLeft = null;           // local countdown for 守护中 · m:ss
let curSide = "right";        // dock side, set by setView
let backend = "";             // "cc" | "devin" when the backend has both
const selKey = () => "cb.sel" + (backend ? "." + backend : "");
let sel = localStorage.getItem(selKey()) || null;
let groupOpen = JSON.parse(localStorage.getItem("cb.groups") || "{}");
// a load that would open more than this many sessions starts with every
// folder closed; the user opens what they want (per harness, per load)
const AUTO_FOLD_ROWS = 10;
const autoFolded = {};
// sessions and token use are two tabs of one card (the panel has room for
// one stretchy card); clicking the tab that is already showing folds the
// card to its head, clicking it again opens it
let view = localStorage.getItem("cb.view") === "tok" ? "tok" : "sess";
let sessFolded = localStorage.getItem("cb.sessFolded") === "1";
let spaces = null;

let trackSig = "";            // cache-card structure signature
// a rescan rolls every session name up and back (Roll MS, see app.css).
// The list rebuilds on every poll, so this is a wall-clock window, not a
// per-element animation: rows built inside it get a negative delay off
// rollAt (their place in the roll) and the class comes off when it ends.
const ROLL_MS = 1150;
let rollAt = 0;
// folded cache card: remembered like the group folds (cb.groups)
let cacheFolded = localStorage.getItem("cb.cacheFolded") === "1";

function setFolded(on) {
  cacheFolded = on;
  localStorage.setItem("cb.cacheFolded", on ? "1" : "0");
  $("cacheCard").classList.toggle("folded", on);
  $("cacheHead").setAttribute("aria-expanded", on ? "false" : "true");
  // always repaint: removing .folded restores the .card white background
  // even when S is null (no state yet from the backend)
  if (S) render(); else {
    $("cacheCard").style.cssText = "";  // force the .card rule to re-apply
  }
}
let trackEls = [];            // per-track DOM roots
let trackMascots = [];        // their Mascot instances
let cacheLefts = [];          // per-track local cache-life countdown
let trackModes = [];          // per-track state (one guarded session each)
let peekSig = "";
let peekMascots = [];
let foldSig = "";             // folded cache card: one circle per track
let foldMascots = [];
let foldRings = [];
let foldLefts = [];

const ROLE_BADGE = { lead: "Lead", sidekick: "Sidekick" };  // single: none

/* ------------------------------------------------- helpers */
const fmtTok = n => n >= 1e6 ? (n / 1e6).toFixed(2) + "M" : (n / 1000).toFixed(1) + "K";
const fmtAge = s => s < 90 ? T("{0}s ago", s) : T("{0}m ago", Math.floor(s / 60));
const relTime = ts => {
  const d = Date.now() / 1000 - ts;
  if (d < 60) return T("just now");
  if (d < 3600) return T("{0}m ago", Math.floor(d / 60));
  if (d < 86400) return T("{0}h ago", Math.floor(d / 3600));
  return T("{0}d ago", Math.floor(d / 86400));
};
const money = v => "$" + (v >= 0.01 || v === 0 ? v.toFixed(2) : v.toFixed(3));
const mmss = s => Math.floor(s / 60) + ":" + String(s % 60).padStart(2, "0");

/* ------------------------------------------------- language (⚙) */
/* English is the source text; T() swaps in Chinese when lang is "zh".
   {0} {1} are filled from the arguments. Static markup carries
   data-i18n / data-i18n-title and is re-labelled by applyLang(). */
let lang = "en";
const ZH = {
  // launch splash
  "Starting…": "启动中…",
  "Ready": "已就绪",
  "SPLASH_TITLE": "Cache Buddy 会停靠在屏幕边缘",
  "Hover": "悬停",
  "the edge strip to peek": "边缘小条，展开小卡片",
  "Double-click": "双击",
  "the peek to open the panel": "小卡片，打开完整面板",
  "Tray icon": "托盘图标",
  "open or quit any time": "随时打开或退出",
  "Got it": "知道了，开始使用",
  "Idle": "空闲",
  "Reading session…": "读取会话…",
  "Session busy · waiting": "会话运行中 · 等待结束",
  "Held · attachment in composer": "暂停 · 输入框里有附件",
  "Renewing…": "续期中…",
  "Renewed ✓": "已续期 ✓",
  "Stopped after 3 failures": "连续失败 3 次，已停止",
  "Cache expired · waiting for your next message": "缓存已过期 · 等你下一条消息",
  "This cycle": "本轮",
  "Last cycle": "上轮",
  "Cached": "缓存中",
  "Waiting for its next message": "等它的下一条消息",
  "Held · session not running": "暂停 · 会话没在运行",
  "CLI session · waiting for its next message (run it via devb)":
    "CLI 会话 · 等它的下一条消息（用 devb 启动）",
  "CLI session · proxy not running": "CLI 会话 · 代理没启动",
  "CLI opened before Buddy · reopen the terminal to guard":
    "CLI 在 Buddy 之前启动 · 重开终端才能守护",
  "droid command not found · install the Droid CLI":
    "找不到 droid 命令 · 先装 Droid CLI",
  "Plan quota": "套餐额度",
  "Carry to {0}": "转到 {0}",
  "Carry over": "转过去",
  "Cancel": "取消",
  "Reading the session…": "正在读取会话…",
  "Carrying…": "正在转换…",
  "{0} messages · {1} tool calls · ≈{2} tokens": "{0} 条消息 · {1} 次工具调用 · 约 {2} tokens",
  "{0} old read-only calls left out": "省掉 {0} 次早期只读调用",
  "A new session is written; this one stays as it is. Nothing is sent until you reply there.":
    "会新建一个会话，原会话不动。在那边回复前不会发送任何内容。",
  "Carried · it's in the list, open it in {0} to continue": "已转过去 · 在 {0} 里打开它接着聊",
  "Couldn't carry: {0}": "转换失败：{0}",
  "Account {0}": "账号 {0}",
  "Copied to account {0}": "已复制到账号 {0}",
  "Couldn't copy: {0}": "复制失败：{0}",
  "session-bridge not found": "找不到 session-bridge",
  "Large context: the first reply there pays to read all of it": "上下文很大：在那边的第一句回复要按全价读一遍",
  "The working folder no longer exists": "原来的工作目录已经不存在",
  "5 hours": "5 小时",
  "Today": "今天",
  "This week": "本周",
  "This month": "本月",
  "Core · ": "Core · ",
  "Monthly": "月度",
  "Weekly": "每周",
  "Daily": "每日",
  "ACU": "ACU",
  "in {0}d": "{0} 天后",
  "in {0}h": "{0} 小时后",
  "in {0}m": "{0} 分钟后",
  "resets {0}": "{0}重置",
  "Plan ends {0}": "套餐 {0} 到期",
  "Extra usage {0}": "超额余额 {0}",
  "checked {0}": "{0}查过",
  "Not signed in to Devin · run devin auth login":
    "Devin 未登录 · 运行 devin auth login",
  "Devin's sign-in expired · run devin auth login":
    "Devin 登录过期 · 运行 devin auth login",
  "This account has no API key": "当前账号没有 API key",
  "Add key": "添加 key",
  "Factory refused the API key · make a new one":
    "Factory 拒绝了这个 API key · 重新建一个",
  "Couldn't reach the server · showing the last numbers":
    "连不上服务器 · 显示的是上次的数字",
  "Factory Desktop isn't open · open it to guard this session":
    "Factory 桌面版没打开 · 打开它才能守护这个会话",
  "Factory Desktop needs its debug port · start it with --remote-debugging-port=9223":
    "Factory 桌面版需要调试端口 · 带 --remote-debugging-port=9223 启动",
  "First time with Buddy · Factory needs one restart":
    "首次使用需要重启一次 Factory",
  "Factory needs one restart": "需要重启一次 Factory",
  "FIX_TIP": "给 Factory 的快捷方式加上调试端口（更新后也会保持），然后重启它。会话不会丢。",
  "Restart Factory": "重启 Factory",
  "Restarting…": "重启中",
  "Retry restart": "再试一次",
  "Restarting Factory…": "正在重启 Factory…",
  "Couldn't restart Factory · open it yourself": "没能重启 Factory · 请手动打开",
  "attachment in the Factory composer · waiting":
    "Factory 输入框里有附件 · 等清空",
  "Switch to {0}": "切换到 {0}",
  "Factory will close and reopen; a reply in progress is interrupted. Sessions are local and stay.":
    "Factory 会关闭再重开，正在回复的会被打断。会话都在本地，不会丢。",
  "Switch": "切换",
  "Switching…": "切换中…",
  "Switched to {0}": "已切换到 {0}",
  "Couldn't switch: {0}": "切换失败：{0}",
  "Couldn't launch Factory · open it yourself": "没能启动 Factory · 请手动打开",
  "{0} droid CLI running · they could write the old account back":
    "有 {0} 个 droid CLI 在运行 · 可能把旧账号写回去",
  "Close them and switch": "关掉它们再切换",
  "Close them and start": "关掉它们再继续",
  "Add account": "添加账号",
  "Add a Droid account": "添加 Droid 账号",
  "Buddy saves the current account, then restarts Factory on its sign-in page. Sign in with the new account's email and code once — switching after that needs no code.":
    "Buddy 会先保存当前账号，然后重启 Factory 到登录页。用新账号的邮箱和验证码登录一次，以后切换都不用再输。",
  "Don't use Factory's Log out — it can invalidate saved accounts.":
    "别在 Factory 里点退出登录 — 那会让已保存的账号失效。",
  "Start": "开始",
  "Waiting for sign-in…": "等待登录…",
  "Sign in in the Factory window. This continues on its own.":
    "在 Factory 窗口里登录就行，这里会自动继续。",
  "Cancel (restore {0})": "取消（恢复 {0}）",
  "previous account": "上一个账号",
  "Restored {0}": "已恢复 {0}",
  "Added {0} · account {1}": "已添加 {0} · 账号 {1}",
  "Already saved · updated {0}": "这个账号已保存过 · 已更新 {0}",
  "Factory API key · account {0}": "Factory API key · 账号 {0}",
  "One per account · the quota card follows the account you're on.":
    "每个账号一把 · 额度卡片会跟着当前账号走。",
  "Get a key on Factory": "去 Factory 申请 key",
  "Skip": "跳过",
  "Save key": "保存 key",
  "Saving…": "保存中…",
  "a Factory API key starts with fk-": "Factory API key 以 fk- 开头",
  "This key belongs to account {0}": "这把 key 是账号 {0} 的",
  "Factory refused this key": "Factory 拒绝了这把 key",
  "Couldn't reach Factory": "连不上 Factory",
  "Saved · the quota card now follows this account": "已保存 · 额度卡片会跟着这个账号",
  "No API key · quota won't show": "没有 API key · 不显示额度",
  "CDP send failed": "CDP 发送失败",
  "UI automation stuck": "界面自动化卡住",
  "upstream HTTP {0}": "上游 HTTP {0}",
  "send failed": "发送失败",
  "Guarding · next in {0}": "守护中 · {0} 后续期",
  "Retrying · {0}": "重试中 · {0}",
  "Stop guarding this session": "停止守护这个会话",
  "Cache expired": "缓存已过期",
  "Not renewable yet": "暂时无法续期",
  "Cache {0} left": "缓存剩 {0}",
  "No cache data yet": "还没有缓存数据",
  "waiting for first turn": "等第一轮对话",
  "{0} cached · not guarded": "{0} 个有缓存 · 未守护",
  "Buddy spent {0}": "Buddy 花了 {0}",
  "Buddy saved {0}": "Buddy 省了 {0}",
  "{0} rescued": "救回 {0} 轮",
  "no rescue yet": "还没救回",
  "{0} silent renew": "后台续期 {0} 次",
  "{0} silent renews": "后台续期 {0} 次",
  "Spent {0}": "花了 {0}",
  "Saved {0}": "省了 {0}",
  "w/o cache {0}": "无缓存 {0}",
  "{0} cached": "{0} 个有缓存",
  "{0} of {1} cached": "{0}/{1} 个有缓存",
  "cache expired": "缓存都过期了",
  "Cache": "缓存",
  "Stop": "停止",
  "Stop all": "全部停止",
  "Guard this too": "也守护这个",
  "Start guard": "开始守护",
  "Spent more on heartbeats than rescued": "心跳花的比救回的多",
  "Saved by keeping the cache alive": "保住缓存省下的钱",
  "No guarding": "没有守护",
  "{0} rescued · {1} renews": "救回 {0} 轮 · 续期 {1} 次",
  "Proxied · can renew": "已走代理 · 可续期",
  "Running · not via proxy yet": "运行中 · 还没走代理",
  "Last reply {0} · context {1} tokens": "上次回复 {0} · 上下文 {1} tokens",
  "{0} · switch harness": "{0} · 切换",
  "just now": "刚刚",
  "{0}s ago": "{0} 秒前",
  "{0}m ago": "{0} 分钟前",
  "{0}h ago": "{0} 小时前",
  "{0}d ago": "{0} 天前",
  "hit {0}%": "命中 {0}%",
  "≈{0}/turn": "≈{0}/轮",
  "Lead": "主模型",
  "Sidekick": "副模型",
  "LOCKED_HINT": "这个终端是在 Buddy 打开之前启动的，它的对话没经过代理，没法续期。\n" +
    "请关掉这个终端，新开一个，运行 devin -r {0}，再点上面的 ⟳ 重新检测。",
  "Settings": "设置",
  "Language": "语言",
  "Keep-alive time": "缓存维护时间",
  "15 min": "15 分钟",
  "30 min": "半小时",
  "45 min": "45 分钟",
  "Sessions": "会话",
  "Refresh": "刷新",
  "Savings": "省下的",
  "Days shown": "显示天数",
  "Days shown · drag or click": "显示天数 · 拖动或点击",
  "Previous month": "上个月",
  "Next month": "下个月",
  "Renew now": "立即续期",
  "Quit": "退出",
  "Minimize": "最小化",
  "Close": "关闭",
  "Prompt-cache heartbeat": "缓存心跳",
  "Switch harness": "切换",
  "Tokens": "Token 用量",
  "7 days": "7 天",
  "30 days": "30 天",
  "Cache read": "缓存读",
  "Read": "缓存读",
  "Write": "缓存写",
  "Cache write": "缓存写",
  "Input": "输入",
  "Output": "输出",
  "No tokens used in this period": "这段时间没有用量",
  "{0} requests": "{0} 次请求",
  "{0} turns": "{0} 轮",
  "Cache hit {0}%": "缓存命中 {0}%",
  "≈ {0} at list price": "按官方价约 {0}",
  "no list price": "无官方价",
  "Total": "合计",
  "Now": "现在",
  "{0} total": "共 {0} 个",
  "hit": "命中",
  "Token use": "Token 用量",
  "Standard": "标准",
  "{0} · {1}% used": "{0} · 已用 {1}%",
  "Droid account {0}": "Droid 账号 {0}",
  "Expand": "展开",
  "Collapse": "收起",
};
const EN = {
  "SPLASH_TITLE": "Buddy docks to the edge of your screen",
  "FIX_TIP": "Adds the debug port to Factory's shortcuts (kept after updates) and restarts it. Your sessions stay.",
  "7 days": "7d",                // the period switch shares a line with two tabs
  "30 days": "30d",
  "LOCKED_HINT": "This terminal was opened before Buddy, so its turns skip " +
    "the proxy and can't be renewed.\nClose the terminal, open a new one, " +
    "run devin -r {0}, then click ⟳ above to re-check.",
};

function T(s, ...a) {
  const t = (lang === "zh" ? ZH[s] : EN[s]) || s;
  return t.replace(/\{(\d)\}/g, (m, i) => a[i] != null ? a[i] : m);
}

function applyLang() {
  document.documentElement.lang = lang === "zh" ? "zh-CN" : "en";
  for (const el of document.querySelectorAll("[data-i18n]"))
    el.textContent = T(el.dataset.i18n);
  for (const el of document.querySelectorAll("[data-i18n-title]"))
    el.title = T(el.dataset.i18nTitle);
  for (const el of document.querySelectorAll("[data-i18n-aria]"))
    el.setAttribute("aria-label", T(el.dataset.i18nAria));
  for (const b of $("segLang").children)
    b.classList.toggle("on", b.dataset.lang === lang);
}

/* ------------------------------------------------- state render */
/* all UI text derives from `mode` plus structured fields — the
   backend's `status` string is only used to pick a retry reason */
const STATUS_TEXT = {
  none: "Idle",
  loading: "Reading session…",
  busy: "Session busy · waiting",
  held: "Held · attachment in composer",
  injecting: "Renewing…",
  sent: "Renewed ✓",
  failed: "Stopped after 3 failures",
  expired: "Cache expired · waiting for your next message",
};

const CYCLE_KIND = { "本轮": "This cycle", "上轮": "Last cycle",
  "缓存中": "Cached" };

/* loading/held can carry a structured reason (Claude Code backend) */
const REASON_TEXT = {
  no_capture: "Waiting for its next message",
  expired: "Cache expired · waiting for your next message",
  not_running: "Held · session not running",
  cli_wait: "CLI session · waiting for its next message (run it via devb)",
  cli_no_proxy: "CLI session · proxy not running",
  cli_locked: "CLI opened before Buddy · reopen the terminal to guard",
  droid_missing: "droid command not found · install the Droid CLI",
  droid_desktop_closed: "Factory Desktop isn't open · open it to guard this session",
  droid_desktop_cdp: "First time with Buddy · Factory needs one restart",
  droid_desktop_restart: "Factory needs one restart",
  droid_desktop_restarting: "Restarting Factory…",
  droid_attach: "attachment in the Factory composer · waiting",
};

function retryReason(status) {
  const s = (status || "").toLowerCase();
  if (s.includes("cdp")) return T("CDP send failed");
  if (s.includes("uia") || s.includes("卡死")) return T("UI automation stuck");
  const http = s.match(/^http (\d{3})/);
  if (http) return T("upstream HTTP {0}", http[1]);
  return T("send failed");
}

function renderStatus() {
  const s = S; if (!s) return;
  let txt;
  if (s.mode === "guarding")
    txt = T("Guarding · next in {0}",
      dueLeft != null ? mmss(Math.max(0, dueLeft)) : s.left);
  else if (s.mode === "retry")
    txt = T("Retrying · {0}", retryReason(s.status));
  else
    txt = T(REASON_TEXT[s.reason] || STATUS_TEXT[s.mode] || "");
  // the panel's own restart: say only that, whatever the guard is doing
  const restarting = fixBusy || s.reason === "droid_desktop_restarting";
  if (restarting) txt = T("Restarting Factory…");
  $("status").textContent = txt;
  $("status").style.color = restarting || isWaiting(s) ? WAIT_COLOR
    : MODE_COLOR[s.mode] || "#3A332C";
  $("target").textContent = s.target ? s.target.title : "";
  renderFix(s);
}

/* Droid Desktop without CDP: one button restarts Factory with its port.
   While it restarts the button turns into a progress strip and the status
   says only that — the guard sends nothing, so no CDP errors show */
let fixBusy = false, fixErr = false;
const FIX_ICON = '<svg viewBox="0 0 16 16" aria-hidden="true"><path d="M13.2 8a5.2 5.2 0 1 1-1.6-3.75"/><path d="M13.4 2.6v3.1h-3.1"/></svg>';
function renderFix(s) {
  const b = $("fixBtn");
  const restarting = fixBusy || s.reason === "droid_desktop_restarting";
  const need = s.reason === "droid_desktop_cdp" || s.reason === "droid_desktop_restart";
  if (!need) fixErr = false;
  b.hidden = !need && !restarting;
  if (b.hidden) return;
  b.disabled = restarting;
  b.classList.toggle("busy", restarting);
  b.classList.toggle("err", fixErr && !restarting);
  const label = restarting ? T("Restarting…")
    : fixErr ? T("Retry restart") : T("Restart Factory");
  const sig = label + restarting;
  if (b.dataset.sig !== sig) {
    b.dataset.sig = sig;
    b.innerHTML = (restarting ? '<i class="fxbar"></i>' : FIX_ICON) +
      "<span>" + label + "</span>";
  }
  b.title = fixErr && !restarting ? T("Couldn't restart Factory · open it yourself")
    : T("FIX_TIP");
}

async function factoryFix() {
  if (fixBusy) return;
  fixBusy = true; fixErr = false;
  if (S) renderFix(S);
  let r = null;
  try { r = await api().factory_fix(); } catch (e) { }
  fixBusy = false;
  fixErr = !(r && r.ok);
  if (S) renderFix(S);
  poll();
}

/* ------------------------------------------------ per-role track rows */
function rebuildTracks(tracks) {
  for (const m of trackMascots) if (m) Mascots.destroy(m);
  trackMascots = [];
  trackEls = [];
  const box = $("tracks");
  box.innerHTML = "";
  // several guarded sessions: the rows get compact, so the name and the
  // model mark lead and the token count stops shouting
  box.classList.toggle("multi", tracks.length > 1);
  for (const t of tracks) {
    const div = document.createElement("div");
    div.className = "track";
    div.innerHTML =
      '<div class="crow">' +
      '<div class="mascot"></div>' +
      '<span class="badge trole"></span>' +
      '<span class="ttitle"></span>' +
      '<span class="big"></span>' +
      '<span class="pill"></span>' +
      '<button class="tdrop" title="' + T("Stop guarding this session") + '">×</button>' +
      "</div>" +
      '<div class="crow sub"><span class="srctag" hidden>CLI</span>' +
      '<span class="tmodel"></span>' +
      '<span class="tmeta"></span></div>' +
      '<div class="crow cbarrow"><div class="cbar"><i></i></div>' +
      '<span class="tleft mono"></span></div>';
    box.appendChild(div);
    const fam = t.family && t.family !== "generic" ? t.family : "";
    const slot = div.querySelector(".mascot");
    trackMascots.push(fam ? Mascots.create(slot, {
      family: fam, mode: t.mode || "none", side: curSide
    }) : null);
    if (fam) {
      /* drawn above */
    } else if (tracks.length > 1) {
      slot.style.visibility = "hidden";   // keep the column aligned
    } else {
      slot.style.display = "none";
    }
    // Claude Code: the row is a session, so name it. Devin's rows are
    // model roles within one session, so they keep the role badge.
    const name = div.querySelector(".ttitle");
    name.textContent = t.title || "";
    name.style.display = t.title ? "" : "none";
    const rb = div.querySelector(".trole");
    const badge = t.title ? "" : T(ROLE_BADGE[t.role] || "");
    rb.textContent = badge;
    rb.style.display = badge ? "" : "none";
    // every row picks its session; double-click guards one that isn't
    if (t.sid) {
      const sid = t.sid;
      div.onclick = () => { sel = sid; localStorage.setItem(selKey(), sel);
        render(); };
      div.ondblclick = () => {
        const cur = ((S && S.cache && S.cache.tracks) || [])
          .find(x => x.sid === sid);
        if (cur && cur.guarded === false) api().start(sid);
      };
    }
    // one guarded session: nothing to disambiguate, so the × is noise
    const drop = div.querySelector(".tdrop");
    drop.onclick = e => {
      e.stopPropagation();
      if (t.sid) api().stop_one(t.sid);
    };
    trackEls.push(div);
  }
}

function fillTracks(tracks) {
  tracks.forEach((t, i) => {
    const el = trackEls[i];
    if (!el) return;
    el.classList.toggle("sel", !!t.sid && t.sid === sel);
    // a warm session nobody guards: grey; it can be guarded with a
    // double-click. Refreshed every frame — guarding starts later.
    const free = t.guarded === false;
    el.classList.toggle("unguarded", free);
    el.title = "";
    el.querySelector(".tdrop").style.display =
      tracks.length > 1 && t.sid && !free ? "" : "none";
    el.querySelector(".big").textContent =
      (t.cached != null ? t.cached : t.read) != null
        ? fmtTok(t.cached != null ? t.cached : t.read) : "—";
    const pill = el.querySelector(".pill");
    pill.textContent = t.hit != null ? T("hit {0}%", Math.round(t.hit)) : "";
    pill.style.display = t.hit != null ? "" : "none";
    el.querySelector(".crow.sub .srctag").hidden = t.source !== "cli";
    el.querySelector(".tmodel").textContent =
      (t.model || "?") + (t.effort ? " · " + t.effort : "");
    const meta = [];
    if (t.cost != null) meta.push(T("≈{0}/turn", "$" + t.cost.toFixed(4)));
    meta.push(t.age_s != null ? fmtAge(t.age_s) : "—");
    el.querySelector(".tmeta").textContent = meta.join(" · ");
    cacheLefts[i] = t.cache_left != null ? t.cache_left : null;
    trackModes[i] = t.mode || (S ? S.mode : "none");
    if (trackMascots[i]) trackMascots[i].setMode(trackModes[i]);
  });
}

function updateBars() {
  trackEls.forEach((el, i) => {
    // each row carries its own state, so a busy session doesn't recolour
    // a healthy one's bar
    const col = MODE_COLOR[trackModes[i]] || MODE_COLOR[S ? S.mode : ""] ||
      "#9CA3AF";
    const left = cacheLefts[i];
    const bar = el.querySelector(".cbar i");
    const lab = el.querySelector(".tleft");
    lab.classList.remove("expired");
    if (left == null) {
      bar.style.width = "0%";
      lab.textContent = "";
    } else if (left <= 0) {
      bar.style.width = "0%";
      lab.textContent = T("Cache expired");
      lab.classList.add("expired");
    } else {
      bar.style.width = Math.max(0, Math.min(100, left / 300 * 100)) + "%";
      bar.style.background = col;
      lab.textContent = trackModes[i] === "loading"
        ? T("Not renewable yet") : T("Cache {0} left", mmss(left));
    }
  });
}

function rebuildPeek(tracks) {
  // one ring per session (Devin: per model role), index-aligned with the
  // cache card so the ring drains with the same countdown as the row
  const sig = tracks.map(t => (t.sid || t.role || "") + ":" +
    (t.family || "")).join("|");
  if (sig !== peekSig) {
    peekSig = sig;
    for (const m of peekMascots) if (m) Mascots.destroy(m);
    peekMascots = [];
    peekRings = [];
    const pm = $("peekMascots");
    pm.innerHTML = "";
    for (const t of tracks) {
      const ring = document.createElement("div");
      ring.className = "pring";
      ring.title = t.title || t.model || "";
      ring.innerHTML =
        '<svg viewBox="0 0 38 38"><circle class="rt" cx="19" cy="19" r="' +
        RING_R + '"/><circle class="rf" cx="19" cy="19" r="' + RING_R +
        '" stroke-dasharray="' + RING_C.toFixed(2) + '"/></svg>';
      const fam = t.family && t.family !== "generic" ? t.family : "";
      if (fam) {
        const d = document.createElement("div");
        d.className = "mascot";
        ring.appendChild(d);
        peekMascots.push(Mascots.create(d, {
          family: fam, mode: t.mode || (S ? S.mode : "none"), side: curSide
        }));
      } else {
        const dot = document.createElement("i");
        dot.className = "pdot";
        ring.appendChild(dot);
        peekMascots.push(null);
      }
      pm.appendChild(ring);
      peekRings.push(ring.querySelector(".rf"));
    }
  }
  tracks.forEach((t, i) => {
    if (peekMascots[i]) peekMascots[i].setMode(t.mode || (S ? S.mode : "none"));
  });
  updateRings();
  // python sizes the peek card by ring count — keep it posted
  try { api().peek_mascots(tracks.length); } catch (e) { }
}

/* each ring is its session's cache left out of the 5-minute TTL */
const RING_R = 16.5;
const RING_C = 2 * Math.PI * RING_R;
let peekRings = [];
let peekRescue = null;        // {usd, until} while "+$x.xx" is shown

function updateRings() {
  peekRings.forEach((el, i) => {
    const left = cacheLefts[i];
    const frac = left == null ? 0 : Math.max(0, Math.min(1, left / 300));
    el.style.strokeDashoffset = (RING_C * (1 - frac)).toFixed(2);
    const mode = trackModes[i] || "none";
    el.style.stroke = frac > 0 && mode !== "loading"
      ? (MODE_COLOR[mode] || "#9CA3AF") : "#D1D5DB";
  });
}

/* folded cache card: each session a circle — its model inside, a ring
   that drains with its cache (same countdown as the row it replaces),
   and the m:ss under the model when the circle is big enough. Fewer
   sessions, bigger circles; built only while folded, so the mascots
   aren't animating out of sight. */
const BALL_R = 16.2;                    // in the 36-unit viewBox
const BALL_C = 2 * Math.PI * BALL_R;

function ballSize(n) {
  // folded, the circles have the panel's whole ~316px row to themselves:
  // 1–2 → 52px … 6 → 38px; past 6 they wrap to a second row at 36px
  // rather than shrink into specks
  // index = session count
  return n <= 6 ? [52, 52, 52, 48, 44, 40, 38][n] : 36;
}

function rebuildFold(tracks) {
  const box = $("cacheFold");
  if (!cacheFolded) {                   // unfolded: nothing to keep alive
    if (foldSig) {
      for (const m of foldMascots) if (m) Mascots.destroy(m);
      foldMascots = []; foldRings = []; foldLefts = [];
      box.innerHTML = "";
      foldSig = "";
    }
    return;
  }
  const sig = tracks.map(t => (t.sid || t.role || "") + ":" +
    (t.family || "")).join("|");
  const few = tracks.length <= 3;       // hover pill only up to 3 circles
  const size = ballSize(tracks.length);
  if (sig !== foldSig) {
    foldSig = sig;
    for (const m of foldMascots) if (m) Mascots.destroy(m);
    foldMascots = []; foldRings = []; foldLefts = [];
    box.innerHTML = "";
    box.classList.toggle("few", few);
    tracks.forEach((t, i) => {
      const b = document.createElement("div");
      b.className = "cball";
      b.innerHTML =
        '<svg viewBox="0 0 36 36"><circle class="rt" cx="18" cy="18" r="' +
        BALL_R + '"/><circle class="rf" cx="18" cy="18" r="' + BALL_R +
        '" stroke-dasharray="' + BALL_C.toFixed(2) + '"/></svg>' +
        '<span class="cleft"></span>';
      const fam = t.family && t.family !== "generic" ? t.family : "";
      if (fam) {
        const d = document.createElement("div");
        d.className = "mascot";
        b.appendChild(d);
        foldMascots.push(Mascots.create(d, {
          family: fam, mode: t.mode || "none", side: curSide }));
      } else {
        const dot = document.createElement("i");
        dot.className = "pdot";
        b.appendChild(dot);
        foldMascots.push(null);
      }
      // hover pill
      if (few) {
        b.addEventListener("mouseenter", () => foldHoverIn(box, b, i, t));
        b.addEventListener("mouseleave", () => foldHoverOut(box));
      }
      // a circle opens the card on its session: unfold, then select it
      b.onclick = e => {
        e.stopPropagation();
        if (t.sid) { sel = t.sid; localStorage.setItem(selKey(), sel); }
        setFolded(false);
      };
      box.appendChild(b);
      foldRings.push(b.querySelector(".rf"));
      foldLefts.push(b.querySelector(".cleft"));
    });
  }
  const roomy = size >= 40;             // room for m:ss under the model
  tracks.forEach((t, i) => {
    const b = box.children[i];
    if (!b) return;
    b.style.setProperty("--cs", size + "px");
    b.classList.toggle("roomy", roomy);
    b.removeAttribute("title");          // hover pill replaces the browser tooltip
    if (foldMascots[i]) foldMascots[i].setMode(t.mode || "none");
  });
  updateFoldRings();
}

function updateFoldRings() {
  foldRings.forEach((el, i) => {
    const left = cacheLefts[i];
    const frac = left == null ? 0 : Math.max(0, Math.min(1, left / 300));
    el.style.strokeDashoffset = (BALL_C * (1 - frac)).toFixed(2);
    const mode = trackModes[i] || "none";
    el.style.stroke = frac > 0 && mode !== "loading"
      ? (MODE_COLOR[mode] || "#9CA3AF") : "#D1D5DB";
    const ball = el.closest(".cball");
    ball.classList.toggle("cold", !(left > 0));
    if (foldLefts[i]) foldLefts[i].textContent = left > 0 ? mmss(left) : "—";
  });
}

/* folded card hover pill: 1–3 circles only. The hovered circle stays;
   siblings shift away with translateX; a pill slides out on the open
   side showing title, model, and (if there is room) the last prompt. */
function foldHoverIn(box, b, i, t) {
  const balls = [...box.querySelectorAll(".cball")];
  const n = balls.length;
  const size = parseFloat(b.style.getPropertyValue("--cs") || "48");
  const shove = size * 0.6 + 10;

  balls.forEach((el, j) => {
    if (j < i) el.style.transform = "translateX(-" + shove + "px)";
    else if (j > i) el.style.transform = "translateX(" + shove + "px)";
    else el.style.transform = "";
  });

  const old = box.querySelector(".cpill");
  if (old) old.remove();

  const pill = document.createElement("div");
  pill.className = "cpill";
  pill.innerHTML = '<div class="ptitle"></div><div class="pmodel"></div>' +
    (t.last_prompt ? '<div class="pprompt"></div>' : "");
  pill.querySelector(".ptitle").textContent = t.title || t.model || "";
  pill.querySelector(".pmodel").textContent = t.model || t.family || "";
  if (t.last_prompt) pill.querySelector(".pprompt").textContent = t.last_prompt;

  const bRect = b.getBoundingClientRect();
  const boxRect = box.getBoundingClientRect();
  const panelW = ($("panel") || box).getBoundingClientRect().width || 320;

  // open side = wherever more room is
  const spaceRight = boxRect.right - bRect.right;
  const spaceLeft = bRect.left - boxRect.left;
  const goRight = spaceRight >= spaceLeft;
  const gap = 10;

  if (goRight) {
    const fromLeft = bRect.right - boxRect.left + gap;
    pill.style.left = fromLeft + "px";
    pill.style.right = "auto";
    pill.style.transformOrigin = "left center";
    pill.classList.add("right");
    pill.style.maxWidth = Math.max(80, Math.min(180, panelW - fromLeft - gap)) + "px";
  } else {
    const fromRight = boxRect.right - bRect.left + gap;
    pill.style.right = fromRight + "px";
    pill.style.left = "auto";
    pill.style.transformOrigin = "right center";
    pill.classList.add("left");
    pill.style.maxWidth = Math.max(80, Math.min(180, panelW - fromRight - gap)) + "px";
  }
  pill.style.top = "50%";
  box.appendChild(pill);
  requestAnimationFrame(() => pill.classList.add("show"));
}
function foldHoverOut(box) {
  const balls = [...box.querySelectorAll(".cball")];
  balls.forEach(el => el.style.transform = "");
  const pill = box.querySelector(".cpill");
  if (pill) {
    pill.classList.remove("show");
    setTimeout(() => pill.remove(), 240);
  }
}


function updatePeekLabel() {
  const lab = $("peekLabel");
  const now = Date.now() / 1000;
  if (peekRescue && now < peekRescue.until) {
    lab.textContent = "+" + money(peekRescue.usd);
    lab.classList.add("rescue");
    return;
  }
  peekRescue = null;
  lab.classList.remove("rescue");
  if (!S || !S.target) { lab.textContent = ""; return; }
  const tracks = (S.cache && S.cache.tracks) || [];
  let i = tracks.findIndex(t => t.sid === S.target.sid);
  if (i < 0 && tracks.length) i = 0;
  const left = i >= 0 ? cacheLefts[i] : null;
  lab.textContent = left == null ? "" : left > 0 ? mmss(left) : "expired";
}

/* the window's own 1px frame takes the state colour in dock/peek, so the
   card has an edge instead of a flat grey outline */
let frameSig = "";
function updateFrame(s) {
  const edge = !document.body.classList.contains("v-panel");
  const want = edge ? "none" : "";
  if (want === frameSig) return;
  frameSig = want;
  try { api().frame_accent(want || null); } catch (e) { /* not wired */ }
}

function render() {
  const s = S; if (!s) return;
  if (s.due_in != null) dueLeft = s.due_in;
  renderStatus();
  // heart carries the state now: mode class on <body> styles it
  for (const c of [...document.body.classList])
    if (c.startsWith("m-")) document.body.classList.remove(c);
  document.body.classList.add("m-" + s.mode);
  // lub-dub speeds up in the last 10s of dueLeft
  document.body.style.setProperty("--hr",
    dueLeft != null && dueLeft > 0 && dueLeft < 10 ? ".48s" : "1s");
  Mascots.setMode(s.mode);
  Mascots.setDue(dueLeft);
  $("dockDot").style.background = MODE_COLOR[s.mode] || "#9CA3AF";


  // cache card: one track per model role (fusion splits into lead +
  // sidekick); rebuild the DOM only when the track set changes
  const c = s.cache;
  const tracks = (c && c.tracks) || [];
  const empty = tracks.length === 0;
  $("cacheCard").classList.toggle("empty", empty);
  const sig = tracks
    .map(t => (t.sid || "") + ":" + (t.role || "") + ":" +
      (t.family || "") + ":" + (t.model || "") + ":" +
      (t.title || "")).join("|");
  if (sig !== trackSig) {
    trackSig = sig;
    cacheLefts = tracks.map(t => t.cache_left);
    trackModes = tracks.map(t => t.mode || "none");
    rebuildTracks(tracks);
  }
  fillTracks(tracks);
  rebuildPeek(tracks);
  updateBars();

  if (empty) {
    // no track rows at all — the message alone, centered in the card
    $("cacheFoot").innerHTML = T("No cache data yet") +
      (s.target ? '<div class="wait">' + T("waiting for first turn") + '</div>' : "");
  } else if (c && c.buddy && c.kind === "缓存中") {
    const n = tracks.filter(t => t.cache_left > 0).length;
    $("cacheFoot").textContent = T("{0} cached · not guarded", n);
  } else if (c && c.buddy) {
    // Claude Code: Buddy's own saving — turns whose cache would have
    // died without the heartbeat, minus what the heartbeats cost
    const hb = tracks.reduce((a, t) => a + (t.hb || 0), 0);
    const sv = c.saved || 0;
    const parts = [T(CYCLE_KIND[c.kind] || c.kind),
      T(sv < 0 ? "Buddy spent {0}" : "Buddy saved {0}", money(Math.abs(sv)))];
    if (c.rescued) parts.push(T("{0} rescued", c.rescued));
    else if (hb) parts.push(T("no rescue yet"));
    if (hb) parts.push(T(hb > 1 ? "{0} silent renews" : "{0} silent renew", hb));
    if (c.waiting_first) parts.push(T("waiting for first turn"));
    $("cacheFoot").textContent = parts.join(" · ");
  } else if (c) {
    const noco = tracks.reduce((a, t) => a + (t.nocache_cost || 0), 0);
    $("cacheFoot").textContent =
      T(CYCLE_KIND[c.kind] || c.kind) +
      ((c.saved || 0) < 0       // heartbeats cost more than turns saved
        ? " · " + T("Spent {0}", "$" + (-c.saved).toFixed(3))
        : " · " + T("Saved {0}", "$" + (c.saved || 0).toFixed(3))) +
      (noco ? " · " + T("w/o cache {0}", "$" + noco.toFixed(4)) : "") +
      (c.waiting_first ? " · " + T("waiting for first turn") : "");
  }
  // the folded card's own line: how many are cached, and one mark per
  // track (dim when its cache is gone), so folding hides the rows, not
  // the state
  const warmN0 = tracks.filter(t => t.cache_left > 0).length;
  $("cacheSum").textContent = empty ? "" : !warmN0 ? T("cache expired")
    : warmN0 === tracks.length ? T("{0} cached", warmN0)
      : T("{0} of {1} cached", warmN0, tracks.length);
  rebuildFold(tracks);
  $("cacheHead").setAttribute("aria-label",
    T("Cache") + " · " + $("cacheSum").textContent);
  renderHeat(s.ledger);
  renderQuota(s.quota);
  renderTokens(s.usage);
  renderEcg(s);
  const warmN = tracks.filter(t => t.cache_left > 0).length;
  $("sessCount").textContent = c && c.buddy && warmN ? T("{0} cached", warmN) : "";
  if (warmN !== warmCount) { warmCount = warmN; updateSessN(); }
  rescueNotice(s);
  updatePeekLabel();
  updateFrame(s);

  // controls — the button acts on the session selected in the tree:
  // not guarded yet -> add it (others keep running); guarded -> stop just
  // that one. Nothing selected but something guarded -> stop them all.
  const guarding = !!s.target;
  const act = guardAction(s);
  rollGuardLabel(T(act.label));
  $("btnGuard").classList.toggle("stop", act.stop);
  $("btnGuard").disabled = act.disabled;
  $("btnNow").disabled = !guarding;
  for (const b of $("segIv").querySelectorAll("button"))
    b.classList.toggle("on", Number(b.dataset.iv) === s.idle_secs);
  moveIvThumb();

  $("logline").textContent = s.last_log || "";
  // footer chip: CDP for Devin; backends may name their own link
  const link = s.link || { label: "CDP", ok: !!s.cdp, title: "CDP" };
  $("cdpLabel").textContent = link.label;
  $("cdp").title = link.title || link.label;
  $("cdp").classList.toggle("ok", !!link.ok);
  if (s.brand) $("brand").textContent = s.brand;
  renderBackends(s);

  markGuarded();
}

/* ---------------------------------------------- control flourishes */
// the interval thumb slides under the chosen button
function moveIvThumb() {
  const on = $("segIv").querySelector("button.on");
  const th = $("ivThumb");
  th.style.opacity = on ? 1 : 0;
  if (!on) return;
  th.style.width = on.offsetWidth + "px";
  th.style.transform = "translateX(" + (on.offsetLeft - 2) + "px)";
}

// heart flight / beat glow / row flash: wall-clock windows, so the 1s
// rebuilds pick up where they are instead of starting over
const GUARD_FX_MS = 1400;
let guardFx = { sid: null, at: 0 };

/* Start guard: a green heart lifts off the button, arcs up to the
   heartbeat in the header and lands as a beat (a ring of light), while
   the session's row in the list flashes green as its ♥ appears. */
function guardLaunch(sid) {
  guardFx = { sid, at: Date.now() };
  const b = $("btnGuard").getBoundingClientRect();
  const e = $("ecg").getBoundingClientRect();
  const row = sid && document.querySelector('.sitem[data-sid="' + CSS.escape(sid) + '"]');
  const h = document.createElement("i");
  h.className = "fxheart";
  h.innerHTML = '<svg viewBox="0 0 16 16"><path d="M8 13.6S2.2 10.2 2.2 6.1A2.9 2.9 0 0 1 8 4.8a2.9 2.9 0 0 1 5.8 1.3C13.8 10.2 8 13.6 8 13.6z"/></svg>';
  document.body.appendChild(h);
  const x0 = b.left + 24, y0 = b.top + b.height / 2;
  const x1 = e.left + e.width / 2, y1 = e.top + e.height / 2;
  // by way of the row, when it's on screen: the heart "picks it up"
  const r = row && row.getBoundingClientRect();
  const mx = r && r.height ? r.left + 14 : (x0 + x1) / 2 + 40;
  const my = r && r.height ? r.top + r.height / 2 : (y0 + y1) / 2;
  const path = [[x0, y0, .6, 0], [mx, my, 1.15, .45], [x1, y1, .9, 1]];
  h.animate(path.map(([x, y, sc, o]) => ({
    transform: "translate(" + (x - 9) + "px," + (y - 9) + "px) scale(" + sc + ")",
    offset: o })), { duration: 760, easing: "cubic-bezier(.45,0,.25,1)" })
    .onfinish = () => { h.remove(); beatGlow(); };
  renderTree();                      // the row picks up its flash window
}

// the beat landing on the header's heartbeat: a ring opens and fades
function beatGlow() {
  const head = $("head");
  head.classList.remove("fxbeat");
  head.getBoundingClientRect();
  head.classList.add("fxbeat");
  setTimeout(() => head.classList.remove("fxbeat"), 700);
}

/* Renew now: a light runs along every cache bar (and round the folded
   circles), and the header beats — the renewal, seen where it lands */
function renewFlourish() {
  const card = $("cacheCard");
  card.classList.remove("fxrenew");
  card.getBoundingClientRect();
  card.classList.add("fxrenew");
  setTimeout(() => card.classList.remove("fxrenew"), 950);
  setTimeout(beatGlow, 160);
}

/* Stop: each stopped session's ♥ comes loose and drops out of its row */
function stopFlourish(sids) {
  for (const sid of sids) {
    const h = document.querySelector('.sitem[data-sid="' + CSS.escape(sid) + '"] .gheart');
    if (!h) continue;
    const r = h.getBoundingClientRect();
    const f = document.createElement("i");
    f.className = "fxdrop";
    f.textContent = "♥";
    f.style.left = r.left + "px";
    f.style.top = r.top + "px";
    document.body.appendChild(f);
    f.animate([{ transform: "none", opacity: 1 },
      { transform: "translate(4px, 16px) rotate(35deg) scale(.6)", opacity: 0 }],
      { duration: 620, easing: "cubic-bezier(.5,0,.8,.5)" }).onfinish = () => f.remove();
  }
}

/* any control press: a soft ripple from where it was pressed */
function pressRipple(e, btn) {
  if (btn.disabled) return;
  const r = btn.getBoundingClientRect();
  const d = Math.max(r.width, r.height) * 2.2;
  const w = document.createElement("i");
  w.className = "pressfx";
  w.style.width = w.style.height = d + "px";
  w.style.left = (e.clientX ? e.clientX - r.left : r.width / 2) - d / 2 + "px";
  w.style.top = (e.clientY ? e.clientY - r.top : r.height / 2) - d / 2 + "px";
  btn.appendChild(w);
  w.animate([{ transform: "scale(0)", opacity: .5 }, { transform: "scale(1)", opacity: 0 }],
    { duration: 520, easing: "cubic-bezier(.2,.8,.2,1)" }).onfinish = () => w.remove();
}

// the guard button's word changes after a press: it rolls over to the
// new one (the names' roll), never on a plain refresh
let guardPressAt = 0, guardWord = "";
function rollGuardLabel(word) {
  const el = $("guardLabel");
  if (word === guardWord) return;
  const roll = guardWord && Date.now() - guardPressAt < 4000;
  guardWord = word;
  el.textContent = word;
  if (!roll) return;
  el.animate([{ transform: "translateY(70%)", opacity: 0 },
    { transform: "none", opacity: 1 }], { duration: 340, easing: "cubic-bezier(.2,.9,.3,1.2)" });
}

/* What the guard button does right now. Claude Code sends `guarded` (many
   sessions at once); Devin guards one, so fall back to its target. */
function guardAction(s) {
  const guarded = s.guarded || (s.target ? [s.target.sid] : []);
  const multi = !!s.guarded;
  if (sel && guarded.includes(sel))
    return { kind: multi ? "stop_one" : "stop", label: "Stop",
      stop: true, disabled: false };
  if (sel)
    return { kind: "start", label: guarded.length && multi
      ? "Guard this too" : "Start guard", stop: false, disabled: false };
  if (guarded.length)
    return { kind: "stop", label: guarded.length > 1 ? "Stop all" : "Stop",
      stop: true, disabled: false };
  return { kind: "start", label: "Start guard", stop: false, disabled: true };
}

async function poll() {
  try {
    S = await api().state();
  } catch (e) { return; /* webview not ready yet */ }
  // outside the try: a bug in render() must surface, not pass for
  // "not ready" and silently freeze half the panel
  render();
}

/* --------------------------------------------- heartbeat cell */
/* A 44px ECG where a bright current sweeps left to right on a loop. The
   line spikes at the beat while guarding; when nothing is being kept
   warm it goes flat but the sweep keeps running. */
let ecg = null;               // Ecg instance (ui/ecg.js), header
let peekEcg = null;           // the same, on the peek card
let lastBeat = 0;             // epoch of the last renewal that glowed

function initEcg() {
  if (!window.Ecg) return;
  if (!ecg) ecg = new Ecg($("ecg"), { size: 44, color: "#9CA3AF", beats: [] });
  if (!peekEcg) peekEcg = new Ecg($("peekEcg"),
    { size: 40, color: "#9CA3AF", beats: [] });
}

function renderEcg(s) {
  initEcg();
  if (!ecg) return;
  // the beat follows the session the header describes
  const tracks = (s.cache && s.cache.tracks) || [];
  const t = (s.target && tracks.find(x => x.sid === s.target.sid)) ||
    tracks[0];
  const opts = {
    beats: ((t && t.renews) || []).slice(),   // Devin: none, no glow
    color: isWaiting(s) ? WAIT_COLOR : MODE_COLOR[s.mode] || "#9CA3AF",
    // flat while nothing is being kept warm: not guarded, stopped after
    // failures, or held; waiting for a message gets the standby ripple
    flat: !s.target ||
      ["failed", "retry", "none", "held"].includes(s.mode),
    // busy: broad and low; guarding: a sharp QRS; waiting: standby
    wave: isWaiting(s) ? "wait" : s.mode === "busy" ? "wide" : "qrs",
    speed: isWaiting(s) ? 2.6 : 1.7,
    rest: isWaiting(s) ? 1.3 : .95,
  };
  Object.assign(ecg.opts, opts);
  if (peekEcg) Object.assign(peekEcg.opts, opts);
}

function ecgFrame() {
  const now = Date.now() / 1000;
  if (ecg) {
    ecg.draw(now);
    const bs = ecg.opts.beats;
    const last = bs.length ? bs[bs.length - 1] : 0;
    if (last > lastBeat && now - last < 2.5) lastBeat = last;  // glow
  }
  if (peekEcg && document.body.classList.contains("v-peek"))
    peekEcg.draw(now);
  requestAnimationFrame(ecgFrame);
}

/* --------------------------------------------- rescue notice */
/* Buddy just saved a turn whose cache would have died: pop the peek open
   once so it is noticed, show "+$x.xx" there, and remember we showed it. */
let toldRescue = 0;

function rescueNotice(s) {
  const r = s.rescued;
  if (!r || !(r.at > toldRescue)) return;
  toldRescue = r.at;
  peekRescue = { usd: r.usd || 0, until: Date.now() / 1000 + 7 };
  updatePeekLabel();
  try { api().rescue_pop(r.at); } catch (e) { /* backend without it */ }
}

/* ------------------------------------------- savings heatmap */
/* one calendar month, 2 rows of 16 cells (days 1-16 / 17-31); depth of
   green = what Buddy saved that day, net of its own heartbeat cost */
let heatMonth = null;                 // "2026-10"
let heatFirst = null;                 // earliest month with data
let heatSig = "";
let heatDays = [];

/* Depth is relative to the busiest day this month, so the scale always
   uses the full range no matter the absolute size of the savings. */
function heatLevel(v, max) {          // 0 = no saving, 1..4 = depth
  if (!(v > 0) || !(max > 0)) return 0;
  return Math.min(4, Math.ceil(v / max * 4));
}

const monthLabel = ym => new Date(ym + "-01T12:00:00").toLocaleDateString(
  lang === "zh" ? "zh-CN" : "en-US", { month: "long", year: "numeric" });

function shiftMonth(ym, n) {          // "2026-10" +/- n months
  const y = Number(ym.slice(0, 4)), m = Number(ym.slice(5, 7)) - 1 + n;
  return new Date(y, m, 1).toLocaleDateString("en-CA").slice(0, 7);
}

/* ------------------------------------------------- plan quota */
/* A refresh may come back with the same numbers, so the ⟳ and the bars
   themselves say "working": busy(class) drives the CSS, and every path
   that starts one clears it (the answer, a timeout, or the click itself
   when the call fails). */
let quotaAsked = 0;                    // when the last ⟳ went out

/* Looping "working" motion that never snaps: when the work ends, a loop
   runs out the round it is in and stops where it started, instead of
   jumping back from wherever it was. */
const calm = () => matchMedia("(prefers-reduced-motion: reduce)").matches;

// a loop whose every round eases in and out (the rings' turn, the
// breathing): ending = finish this round
function loopFx(el, frames, opts, on) {
  const a = el._loop;
  if (on) {
    if (calm()) return;
    if (a && a.playState === "running") {      // winding down: keep going
      a.effect.updateTiming({ iterations: Infinity });
      return;
    }
    el._loop = el.animate(frames, Object.assign({ iterations: Infinity }, opts));
  } else if (a && a.playState === "running") {
    const it = a.effect.getComputedTiming().currentIteration || 0;
    a.effect.updateTiming({ iterations: it + 1 });
  }
}

// the ⟳: turns at an even speed while busy; ending = it slows down and
// comes to rest on a full turn (where its resting angle is)
function spinFx(el, on) {
  const a = el._spin;
  if (on) {
    if (calm() || el._spinning) return;
    // pressed again while slowing down: carry on from the angle it is at
    let deg = 0;
    if (a && a.playState === "running") {
      const m = new DOMMatrix(getComputedStyle(el).transform);
      deg = (Math.atan2(m.b, m.a) * 180 / Math.PI + 360) % 360;
      a.cancel();
    }
    el._spin = el.animate([{ transform: "rotate(0deg)" }, { transform: "rotate(360deg)" }],
      { duration: 900, iterations: Infinity });
    el._spin.currentTime = deg / 360 * 900;
    el._spinning = true;
  } else if (a && el._spinning) {
    el._spinning = false;
    const t = a.effect.getComputedTiming();
    const deg = ((t.progress || 0) * 360) % 360;
    a.cancel();
    const left = 360 - deg;                     // to the next full turn
    el._spin = el.animate([{ transform: "rotate(" + deg + "deg)" },
      { transform: "rotate(360deg)" }],
      { duration: 250 + left / 360 * 700, easing: "cubic-bezier(.2,.6,.3,1)" });
  }
}

function setBusy(what, on) {
  const card = what === "quota" ? $("quotaCard") : $("sessCard");
  const was = card.classList.contains("busy");
  card.classList.toggle("busy", on);
  const b = what === "quota" ? $("btnQuota") : $("btnRefresh");
  spinFx(b, on);
  if (what === "quota") {
    for (const sv of $("quotaRows").querySelectorAll(".qring svg"))
      loopFx(sv, [{ transform: "rotate(-90deg)" }, { transform: "rotate(270deg)" }],
        { duration: 1200, easing: "cubic-bezier(.5,0,.5,1)" }, on);
    loopFx($("quotaRows"), [{ opacity: 1 }, { opacity: .62 }, { opacity: 1 }],
      { duration: 1400, easing: "ease-in-out" }, on);
    // folded: a light sweeps along the bar while asking, and the answer
    // grows the bar in again from empty
    $("quotaTick").classList.toggle("busy", on);
    if (was && !on) tickRegrow = true;
  }
  if (on) {
    clearTimeout(setBusy["_" + what]);
    // a stuck request mustn't leave it turning forever
    setBusy["_" + what] = setTimeout(() => setBusy(what, false), 8000);
  }
}

const QUOTA_LABEL = { "5h": "5 hours", day: "Today", week: "This week",
  month: "This month", acu: "ACU" };
// the folded line's shorter names
const QUOTA_FOLD = { "5h": "5h", day: "Daily", week: "Weekly",
  month: "Monthly", acu: "ACU" };
const QUOTA_ERROR = {
  no_login: "Not signed in to Devin · run devin auth login",
  signed_out: "Devin's sign-in expired · run devin auth login",
  no_key: "This account has no API key",
  bad_key: "Factory refused the API key · make a new one",
};

// time until an epoch: "in 3h" / "in 2d" / "in 12m"
const inTime = ts => {
  const d = Math.max(0, ts - Date.now() / 1000);
  if (d >= 86400) return T("in {0}d", Math.round(d / 86400));
  if (d >= 3600) return T("in {0}h", Math.round(d / 3600));
  return T("in {0}m", Math.max(1, Math.round(d / 60)));
};

/* Plan quota as concentric rings: the longest window outside, each
   ring in its own calm colour that warms to amber and then red as it
   fills (the track takes the same tint, so the whole ring turns). A
   legend beside it carries the numbers. Pools (Droid's Core) get their
   own cluster. Built once per set of windows, then only updated, so the
   rings can grow in and ease between values. */
const RING_BASE = ["#3FA46A", "#5B8DD6", "#8B7FD6"];   // outer → inner
const RING_ORDER = { month: 0, acu: 0, week: 1, day: 2, "5h": 3 };
let quotaSig = "";

// a ring's colour for how full it is: its own hue, amber from 70%, red
// from 90% (CSS eases between them; a straight blend went muddy olive)
function ringColor(base, used) {
  return used >= 90 ? "#D9534F" : used >= 70 ? "#E0A042" : base;
}

/* Folded: the card is its title line, and that line shows ONE window at
   a time — dot, short name, a thin bar — taking turns every 15s (the
   next one slides up into place, its bar grows in). No number: the bar
   says it, the tooltip has it. The pager dots on the right say how many
   there are and which is up. It starts on the fullest window, waits
   while the pointer is over it, and a click on it skips to the next.
   Same order and colours as the rings. Remembered like the other folds. */
const TICK_MS = 15000;
let quotaFolded = localStorage.getItem("cb.quotaFolded") === "1";
let tickWs = [], tickAt = -1, tickSig = "", tickLoc = "en-US", tickHover = false;
let tickRegrow = false;                  // a refresh landed: grow in again

/* Folding morphs instead of snapping: the ring of the window the line
   will show unrolls like tape off a roll — the unrolled part a straight
   line, the rest still a ring, shrinking as it rolls along — until it
   is that window's bar. The other rings and the legend fade out, the
   line's name and pager fade in, and the card's height eases with it
   (the window's bottom edge too, on the same clock: see fitPanel).
   Unfolding plays it backwards. */
const MORPH_MS = 460;
let morph = null;

function setQuotaFolded(on, animate) {
  if (morph) morph.finish();
  const card = $("quotaCard");
  const go = animate && !calm() && !card.hidden && on !== quotaFolded &&
    document.body.classList.contains("v-panel") && !!tickWs[tickAt];
  let before = null;
  if (go) {
    before = quotaGeo(card);
    if (on) before.ghost = $("quotaRows").cloneNode(true);   // fades out
  }
  quotaFolded = on;
  localStorage.setItem("cb.quotaFolded", on ? "1" : "0");
  card.classList.toggle("folded", on);
  $("quotaHead").setAttribute("aria-expanded", on ? "false" : "true");
  if (go) morphQuota(card, before, quotaGeo(card), on);
}

// where the shown window's ring and bar sit, in the card's own px
function quotaGeo(card) {
  const t = tickWs[tickAt], key = (t.w.core ? "c" : "") + t.w.key;
  const cr = card.getBoundingClientRect();
  const ox = cr.left + card.clientLeft, oy = cr.top + card.clientTop;
  const g = { h: cr.height, t, key };
  const arc = card.querySelector('#quotaRows .qarc[data-k="' + key + '"]');
  if (arc && arc.getClientRects().length) {
    const sr = arc.ownerSVGElement.getBoundingClientRect(), k = sr.width / 58;
    g.ring = { cx: sr.left + sr.width / 2 - ox, cy: sr.top + sr.height / 2 - oy,
      r: Number(arc.getAttribute("r")) * k, sw: Number(arc.getAttribute("stroke-width")) * k };
    const rr = $("quotaRows").getBoundingClientRect();
    g.rows = { x: rr.left - ox, y: rr.top - oy, w: rr.width };
  }
  const bar = card.querySelector(".qtbar");
  if (bar && bar.getClientRects().length) {
    const br = bar.getBoundingClientRect();
    g.bar = { x: br.left - ox, y: br.top + br.height / 2 - oy, w: br.width, sw: br.height };
  }
  return g;
}

// the first `upto` px of the tape: a line from P of length a, then the
// coil — a circle of radius rho hanging under the line's end, wound
// clockwise from its top (where the ring's arc starts)
function coilPath(P, a, rho, upto) {
  let d = "M" + P.x.toFixed(2) + " " + P.y.toFixed(2) +
    "L" + (P.x + Math.min(upto, a)).toFixed(2) + " " + P.y.toFixed(2);
  if (upto > a && rho > .05) {
    const cx = P.x + a, cy = P.y + rho, phi = (upto - a) / rho;
    const n = Math.max(6, Math.ceil(phi / .12));
    for (let i = 1; i <= n; i++) {
      const f = phi * i / n;
      d += "L" + (cx + rho * Math.sin(f)).toFixed(2) + " " + (cy - rho * Math.cos(f)).toFixed(2);
    }
  }
  return d;
}

// e = 0: the ring (R); e = 1: the bar (B). The tape's length goes from
// the ring's circumference to the bar's, the unrolled share is e, and
// the coil's radius shrinks to nothing, so it reads as rolling out.
function drawCoil(track, fill, R, B, e, used) {
  const lerp = (x, y) => x + (y - x) * e;
  const Lc = Math.max(1, B.w - B.sw);          // the bar between its round caps
  const L = lerp(2 * Math.PI * R.r, Lc);
  const a = e * L, rho = R.r * (1 - e);
  const P = { x: lerp(R.cx, B.x + B.sw / 2), y: lerp(R.cy - R.r, B.y) };
  const sw = lerp(R.sw, B.sw).toFixed(2);
  // the used share: of the ring's arc, and of the bar's (caps included)
  const f = Math.min(1, lerp(used, Math.max(0, (used * B.w - B.sw) / Lc)));
  track.setAttribute("d", coilPath(P, a, rho, L));
  track.setAttribute("stroke-width", sw);
  track.setAttribute("stroke-opacity", lerp(.13, .2).toFixed(3));
  fill.setAttribute("d", coilPath(P, a, rho, L * f));
  fill.setAttribute("stroke-width", sw);
  fill.style.opacity = used > .004 ? 1 : 0;
}

function morphQuota(card, a, b, on) {
  const open = on ? a : b, shut = on ? b : a;  // unfolded / folded geometry
  if (!open.ring || !shut.bar) return;
  const used = a.t.used / 100, col = a.t.col;
  const clamp = x => Math.max(0, Math.min(1, x));
  const ov = document.createElement("div");
  ov.className = "qmorph";
  const ghost = on ? a.ghost : null;           // the rings as they were
  if (ghost) {
    ghost.removeAttribute("id");
    ghost.className = "qghost";
    Object.assign(ghost.style, { left: a.rows.x + "px", top: a.rows.y + "px",
      width: a.rows.w + "px" });
    for (const el of ghost.querySelectorAll('[data-k="' + a.key + '"]'))
      el.style.visibility = "hidden";
    ov.appendChild(ghost);
  }
  const NS = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(NS, "svg");
  svg.setAttribute("class", "qmsvg");
  const track = document.createElementNS(NS, "path");
  const fill = document.createElementNS(NS, "path");
  for (const p of [track, fill]) {
    p.setAttribute("fill", "none");
    p.setAttribute("stroke", col);
    p.setAttribute("stroke-linecap", "round");
    svg.appendChild(p);
  }
  ov.appendChild(svg);
  card.appendChild(ov);
  // the real pieces the tape stands in for stay out of sight meanwhile
  const hide = on ? [card.querySelector(".qtbar")]
    : [...card.querySelectorAll('#quotaRows [data-k="' + a.key + '"]')];
  const fadeIn = on ? [".qtdot", ".qtlab", ".qtpage"].map(q => card.querySelector(q))
    : [$("quotaRows"), $("quotaMeta")];
  for (const el of hide) el.style.visibility = "hidden";
  for (const el of fadeIn) if (el) el.style.opacity = 0;
  // the window's bottom edge: the end state is laid out now, so measure
  // it and send it with this animation's length, then hold the observers
  fitPanel(MORPH_MS);
  fitHold = true;
  card.style.overflow = "hidden";
  card.style.height = a.h + "px";
  const t0 = performance.now();
  const frame = now => {
    const p = clamp((now - t0) / MORPH_MS);
    const k = 1 - Math.pow(1 - p, 3);          // = the window's ease
    card.style.height = (a.h + (b.h - a.h) * k).toFixed(2) + "px";
    drawCoil(track, fill, open.ring, shut.bar, on ? k : 1 - k, used);
    if (ghost) ghost.style.opacity = 1 - clamp(p / .4);
    const fi = on ? clamp((p - .55) / .45) : clamp((p - .3) / .7);
    for (const el of fadeIn) if (el) el.style.opacity = fi;
    if (p < 1) morph.raf = requestAnimationFrame(frame); else morph.finish();
  };
  morph = {
    raf: 0,
    finish() {
      cancelAnimationFrame(this.raf);
      morph = null;
      ov.remove();
      card.style.height = card.style.overflow = "";
      for (const el of hide) el.style.visibility = "";
      for (const el of fadeIn) if (el) el.style.opacity = "";
      fitHold = false;
      fitSoon();
    },
  };
  frame(t0);                                   // the first frame now: no gap
}

function renderQuotaTick(windows, loc) {
  tickLoc = loc;
  // the rings' order (longest outside, pools apart), each with its
  // ring's colour — the colour index runs per pool, outer first
  const ws = windows.slice().sort((x, y) =>
    (x.core ? 1 : 0) - (y.core ? 1 : 0) ||
    (RING_ORDER[x.key] ?? 9) - (RING_ORDER[y.key] ?? 9));
  const idx = {};
  tickWs = ws.map(w => {
    const pool = w.core ? "c" : "s";
    const i = idx[pool] = (idx[pool] ?? -1) + 1;
    const used = Math.min(100, Math.max(0, w.used));
    return { w, used, col: ringColor(RING_BASE[i] || RING_BASE[2], used) };
  });
  const sig = ws.map(w => (w.core ? "c" : "") + w.key).join(",");
  if (sig !== tickSig) {
    tickSig = sig;
    // a new set of windows: open on the one closest to running out
    tickAt = tickWs.reduce((b, t, k) => t.used > tickWs[b].used ? k : b, 0);
    $("quotaTick").innerHTML = '<span class="qtin"><i class="qtdot"></i>' +
      '<span class="qtlab"></span><span class="qtbar"><i></i></span>' +
      '</span><span class="qtpage"></span>';
    paintTick(true);
  } else paintTick(tickRegrow);
  tickRegrow = false;
}

// fill the line with window tickAt; `grow` restarts its bar from empty
function paintTick(grow) {
  const box = $("quotaTick");
  const t = tickWs[tickAt];
  if (!t || !box.firstChild) return;
  const { w, used, col } = t;
  box.querySelector(".qtdot").style.background = col;
  box.querySelector(".qtlab").textContent =
    (w.core && !poolBoth ? T("Core · ") : "") + T(QUOTA_FOLD[w.key] || w.key);
  const bar = box.querySelector(".qtbar"), fill = bar.firstChild;
  bar.style.setProperty("--c", col);
  bar.classList.toggle("hot", used >= 90);
  if (grow) {
    fill.style.transition = "none";
    fill.style.width = "0";
    void fill.offsetWidth;
    fill.style.transition = "";
  }
  fill.style.width = used + "%";
  // pager: one dot per window in its colour, the current one a dash
  const pg = box.querySelector(".qtpage");
  pg.hidden = tickWs.length < 2;
  if (pg.children.length !== tickWs.length)
    pg.innerHTML = tickWs.map(() => "<i></i>").join("");
  [...pg.children].forEach((d, k) => {
    d.style.background = tickWs[k].col;
    d.classList.toggle("on", k === tickAt);
  });
  box.title = (w.core ? T("Core · ") : "") + T(QUOTA_LABEL[w.key] || w.key) +
    " · " + Math.round(w.used) + "%" + (w.resets_at ? " · " + T("resets {0}",
      new Date(w.resets_at * 1000).toLocaleString(tickLoc, { month: "short",
        day: "numeric", hour: "2-digit", minute: "2-digit" })) : "");
}

// the next window: the old one lifts away, the new one rises in
function stepTick() {
  if (tickWs.length < 2) return;
  const inner = $("quotaTick").querySelector(".qtin");
  const swap = () => { tickAt = (tickAt + 1) % tickWs.length; paintTick(true); };
  if (!inner || matchMedia("(prefers-reduced-motion: reduce)").matches) return swap();
  const out = inner.animate([{ opacity: 1, transform: "none" },
    { opacity: 0, transform: "translateY(-7px)" }],
    { duration: 220, easing: "ease-in", fill: "forwards" });
  out.onfinish = () => {
    swap();
    out.cancel();
    inner.animate([{ opacity: 0, transform: "translateY(7px)" },
      { opacity: 1, transform: "none" }],
      { duration: 320, easing: "cubic-bezier(.2,.7,.3,1)" });
  };
}

/* the S/C pool switch: plans with both pools show a small segment in
   the head; one pool means no toggle and the windows show as today.
   poolBoth tells the legend to drop its "Core ·" prefix (the toggle
   already names the pool). */
let quotaPool = localStorage.getItem("cb.quotaPool") || "s";
let poolBoth = false;

function quotaWindows(windows) {
  const s = windows.filter(w => !w.core), c = windows.filter(w => w.core);
  poolBoth = s.length > 0 && c.length > 0;
  $("segPool").hidden = !poolBoth;
  if (!poolBoth) return windows;
  if (quotaPool !== "c") quotaPool = "s";   // a gone pool falls back to S
  const ws = quotaPool === "c" ? c : s;
  const seg = $("segPool");
  for (const b of seg.querySelectorAll("button"))
    b.classList.toggle("on", b.dataset.pool === quotaPool);
  movePoolThumb();
  const pools = { s, c };
  // the hidden pool at >=90% warns on its letter — Core can fill up
  // while the card shows S
  const other = quotaPool === "c" ? "s" : "c";
  const oHot = (pools[other] || []).reduce((m, w) => Math.max(m, w.used || 0), 0);
  const ob = seg.querySelector('button[data-pool="' + other + '"]');
  const dot = ob.querySelector(".pdot");
  dot.hidden = oHot < 90;
  if (!dot.hidden) {
    dot.style.setProperty("--c", ringColor(RING_BASE[2], oHot));
    ob.title = T("{0} · {1}% used", T(other === "c" ? "Droid Core" : "Standard"),
                 Math.round(oHot));
  }
  return ws;
}

function movePoolThumb() {
  const on = $("segPool").querySelector("button.on"), th = $("poolThumb");
  th.style.opacity = on ? 1 : 0;
  if (!on) return;
  th.style.width = on.offsetWidth + "px";
  const t = "translateX(" + (on.offsetLeft - 2) + "px)";
  th.style.transform = t;
  th.style.setProperty("--t", t);      // the squash keyframes keep it
}

let tickTimer = 0;
function armTick() {
  clearInterval(tickTimer);
  tickTimer = setInterval(() => {
    // only while folded, seen, and not being read
    if (quotaFolded && !tickHover && !morph && !document.hidden &&
      !$("quotaCard").hidden) stepTick();
  }, TICK_MS);
}

function renderQuotaRings(windows, loc) {
  const rows = $("quotaRows");
  const pools = [windows.filter(w => !w.core), windows.filter(w => w.core)]
    .filter(g => g.length)
    .map(g => g.slice().sort((x, y) =>
      (RING_ORDER[x.key] ?? 9) - (RING_ORDER[y.key] ?? 9)));
  const sig = pools.map(g => g.map(w => (w.core ? "c" : "") + w.key).join(",")).join("|");
  const fresh = sig !== quotaSig;
  if (fresh) {
    quotaSig = sig;
    rows.innerHTML = "";
    for (const g of pools) {
      const n = g.length;
      const sw = n <= 2 ? 6 : 5, gap = 1.5, R = 29 - sw / 2 - 1;
      let svg = '<svg viewBox="0 0 58 58" aria-hidden="true">';
      g.forEach((w, i) => {
        const r = R - i * (sw + gap);
        // data-k: the window, so the fold can find the ring it unrolls
        const k = ' data-k="' + (w.core ? "c" : "") + w.key + '"';
        svg += '<circle class="qtrack"' + k + ' cx="29" cy="29" r="' + r + '" stroke-width="' + sw + '"/>' +
          '<circle class="qarc"' + k + ' cx="29" cy="29" r="' + r + '" stroke-width="' + sw +
          '" stroke-dasharray="0 999"/>' +
          '<circle class="qtip"' + k + ' cx="29" cy="29" r="' + (sw / 2 - 1.6) + '"/>';
      });
      svg += "</svg>";
      const box = document.createElement("div");
      box.className = "qring";
      box.innerHTML = svg + '<div class="qlegend">' + g.map(() =>
        '<div class="ql"><i></i><span class="qlab"></span><small class="qreset"></small>' +
        '<span class="qpct"></span></div>').join("") + "</div>";
      rows.appendChild(box);
    }
  }
  [...rows.children].forEach((box, gi) => {
    const g = pools[gi];
    const arcs = box.querySelectorAll(".qarc"), tracks = box.querySelectorAll(".qtrack"),
      tips = box.querySelectorAll(".qtip"), ls = box.querySelectorAll(".ql");
    g.forEach((w, i) => {
      const used = Math.min(100, Math.max(0, w.used));
      const col = ringColor(RING_BASE[i] || RING_BASE[2], used);
      const r = Number(arcs[i].getAttribute("r"));
      const C = 2 * Math.PI * r, len = C * used / 100;
      const set = () => {
        arcs[i].style.strokeDasharray = len.toFixed(1) + " " + C.toFixed(1);
        // a small light dot rides the arc's end; rotates with it
        tips[i].style.transform = "rotate(" + (used * 3.6).toFixed(1) + "deg) translateX(" + r + "px)";
      };
      arcs[i].style.stroke = col;
      arcs[i].style.opacity = used > 0 ? 1 : 0;
      tips[i].style.opacity = used > 4 ? 1 : 0;
      tracks[i].style.stroke = col;
      arcs[i].classList.toggle("hot", used >= 90);
      // first build: start empty, then grow to the value
      if (fresh) requestAnimationFrame(() => requestAnimationFrame(set)); else set();
      const l = ls[i];
      l.querySelector("i").style.background = col;
      l.querySelector(".qlab").textContent =
        (w.core && !poolBoth ? T("Core · ") : "") + T(QUOTA_LABEL[w.key] || w.key);
      const pct = l.querySelector(".qpct");
      pct.textContent = Math.round(w.used) + "%";
      pct.style.color = used >= 70 ? col : "";
      l.querySelector(".qreset").textContent = w.detail ||
        (w.resets_at ? T("resets {0}", inTime(w.resets_at)) : "");
      l.title = w.resets_at ? T("resets {0}", new Date(w.resets_at * 1000)
        .toLocaleString(loc, { month: "short", day: "numeric", hour: "2-digit",
          minute: "2-digit" })) : "";
    });
  });
}

function renderQuota(q) {
  const card = $("quotaCard");
  card.hidden = !q;
  if (!q) return;
  // a ⟳ is out until the answer lands (a newer `at`), or 8s pass
  if (quotaAsked && (q.at > quotaAsked || Date.now() / 1000 - quotaAsked > 8))
    setBusy("quota", false);
  $("quotaPlan").textContent = q.plan || "";
  card.classList.toggle("stale", !!q.stale);
  const loc = lang === "zh" ? "zh-CN" : "en-US";
  // title line, right side: how old the numbers are once that matters
  // (kept from a miss or a restart, or a few rounds old), else the
  // balance and the plan's end — the full set stays in the tooltip
  const bits = [];
  if (q.extra) bits.push(T("Extra usage {0}", money(q.extra)));
  if (q.until) bits.push(T("Plan ends {0}", new Date(q.until * 1000)
    .toLocaleDateString(loc, { month: "short", day: "numeric" })));
  const old = q.at && (q.stale || Date.now() / 1000 - q.at > 600);
  const meta = $("quotaMeta");
  meta.textContent = old ? T("checked {0}", relTime(q.at)) : bits.join(" · ");
  meta.title = bits.join(" · ");
  const ws = quotaWindows(q.windows || []);
  renderQuotaRings(ws, loc);
  renderQuotaTick(ws, loc);
  const note = $("quotaNote");
  const why = q.error && (QUOTA_ERROR[q.error] ||
    "Couldn't reach the server · showing the last numbers");
  note.hidden = !why;
  note.textContent = why ? T(why) : "";
  if (q.error === "no_key" && backend === "droid") {
    // the key is per account: offer to add this account's right here
    const b = document.createElement("button");
    b.className = "qbtn";
    b.textContent = T("Add key");
    b.onclick = droidKeyAsk;
    note.appendChild(b);
  }
}

/* ------------------------------------------------ token usage */
/* Per model, for the harness shown (api state `usage`, rescanned every
   30s in the background). The chart is one column per hour (today) or
   day (7/30 days), stacked by model; each row splits the model's tokens
   into cache read / cache write / input / output. */
let tokPeriod = localStorage.getItem("cb.tokPeriod") || "week";
let tokSig = "";
let tokOrg = null;              // droid account org the chart shows
let tokAnim = true;             // grow in: first paint and period switches
let tokCur = null;              // the period's data the tooltips read
let tokColors = {};             // model -> colour, stable across periods
let tokOpen = null;             // model whose detail row is unfolded

// a hue per family; a family's 2nd/3rd model is a darker/lighter shade
const TOK_HUE = {
  claude: "#D97757", gpt: "#10A37F", gemini: "#4285F4", devin: "#2F9E8F",
  devinmark: "#2F9E8F", kimi: "#7C5CFF", glm: "#3859FF", deepseek: "#4D6BFE",
  generic: "#8B95A1",
};
const TOK_SHADE = [0, -22, 26, -38, 40];

function shade(hex, pct) {
  const n = parseInt(hex.slice(1), 16);
  const f = c => {
    const v = pct < 0 ? c * (1 + pct / 100) : c + (255 - c) * pct / 100;
    return Math.round(Math.max(0, Math.min(255, v)));
  };
  return "#" + [n >> 16, n >> 8 & 255, n & 255]
    .map(c => f(c).toString(16).padStart(2, "0")).join("");
}

// colours follow the 30-day order, so switching period never repaints a
// model in someone else's colour
function tokPalette(u) {
  const seen = {};
  tokColors = {};
  for (const r of (u.month && u.month.rows) || []) {
    const fam = TOK_HUE[r.family] ? r.family : "generic";
    const k = seen[fam] = (seen[fam] || 0) + 1;
    tokColors[r.model] = shade(TOK_HUE[fam], TOK_SHADE[(k - 1) % TOK_SHADE.length]);
  }
}
const tokColor = r => tokColors[r.model] ||
  TOK_HUE[r.family] || TOK_HUE.generic;

const fmtBig = n => n >= 1e9 ? (n / 1e9).toFixed(n >= 1e10 ? 1 : 2) + "B"
  : n >= 1e6 ? (n / 1e6).toFixed(n >= 1e8 ? 0 : n >= 1e7 ? 1 : 2) + "M"
  : n >= 1e3 ? (n / 1e3).toFixed(n >= 1e5 ? 0 : 1) + "K" : String(n);
const fmtUsd = v => v >= 1000 ? "$" + Math.round(v).toLocaleString("en-US")
  : v >= 100 ? "$" + v.toFixed(0) : money(v);

/* Today's columns. 8:00–18:00 is where the work is: one column an hour.
   Before and after, two hours share one. A run of empty off-hours
   already gone (the night) collapses into a single slim column, so the
   width goes to the hours that say something. */
let tokBins = [];
const WORK = [8, 18];
function dayBins(p, hour) {
  const raw = [];
  for (let h = 0; h < 24;) {
    const w = h >= WORK[0] && h < WORK[1] ? 1 : 2;
    raw.push({ a: h, b: Math.min(23, h + w - 1) });
    h += w;
  }
  const used = b => p.rows.some(r => {
    for (let h = b.a; h <= b.b; h++) if (r.series[h]) return true;
    return false;
  });
  const off = b => b.b < WORK[0] || b.a >= WORK[1];
  const out = [];
  for (const b of raw) {
    const idle = off(b) && b.b < hour && !used(b);
    const last = out[out.length - 1];
    if (idle && last && last.merged && last.b + 1 === b.a) last.b = b.b;
    else out.push(idle ? { a: b.a, b: b.b, merged: true } : b);
  }
  return out;
}

/* The way into tokens, on the sessions face: this week's 7 real columns,
   tiny, in the busiest model's colour. Rebuilt only when they move. */
let sparkSig = "";
function renderSpark(u) {
  const w = u.week;
  if (!w) return;
  const cols = w.keys.map((_, i) => w.rows.reduce((a, r) => a + r.series[i], 0));
  const sig = cols.join(",") + "|" + (w.rows[0] && w.rows[0].model);
  if (sig === sparkSig) return;
  sparkSig = sig;
  if (!Object.keys(tokColors).length) tokPalette(u);
  const max = Math.max(1, ...cols);
  const box = $("sparkBars");
  box.innerHTML = "";
  box.style.setProperty("--c", w.rows[0] ? tokColor(w.rows[0]) : TOK_HUE.generic);
  cols.forEach((v, i) => {
    const b = document.createElement("i");
    b.style.height = (v ? Math.max(14, v / max * 100) : 8) + "%";
    if (!v) b.classList.add("z");
    if (i === cols.length - 1) b.classList.add("now");
    box.appendChild(b);
  });
  $("toTok").setAttribute("aria-label", T("Token use") + " · " +
    T("7 days") + " " + fmtBig(w.total));
}

/* Which parts of the token view fit the slot it got. The slot's height
   comes from the panel (flex), not from what is in it, so toggling these
   never feeds back into the measurement. */
function fitTok() {
  const card = $("sessCard"), body = $("tokBody");
  if (!card.classList.contains("v-tok") || sessFolded) return;
  // the body's height is the slot flex gave it (min-height 0, it grows
  // and shrinks with the card), not its content's
  const h = body.clientHeight;
  card.classList.toggle("tk-flat", h <= 170);
  card.classList.toggle("tk-nolegend", h <= 135);
  card.classList.toggle("tk-rowsonly", h <= 66);
  card.classList.toggle("tk-none", h <= 26);
}

// a grow-in plays once: when it ends the element drops it, so nothing
// that restyles the panel later can replay it
function oneShot(el) {
  el.addEventListener("animationend", () => { el.style.animation = "none"; },
    { once: true });
}

function renderTokens(u) {
  const had = hasTok();
  $("toTok").hidden = !u;
  if (!u) {                     // harness without usage: sessions only
    if (had) applyView();
    return;
  }
  if (!had) applyView();
  renderSpark(u);
  tkSync();
  const p = u[tokPeriod] || u.week;
  // the calendar's strip: the busiest model's colour this period
  $("segTok").style.setProperty("--c", p.rows.length ? tokColor(p.rows[0]) : "#3FA46A");
  tokCur = p;
  // header: the total and its list-price estimate, even when folded
  $("tokTotal").textContent = p.total ? fmtBig(p.total) : "0";
  $("tokUsd").textContent = p.usd != null && p.total
    ? "≈" + fmtUsd(p.usd) + (p.partial ? "+" : "") : "";
  // the droid account these numbers belong to: its letter, popping once
  // on a switch (and the chart regrows below via tokAnim)
  const acct = $("tokAcct"), org = u.account && u.account.org;
  if (org !== tokOrg) {
    if (org && u.account.name) {
      acct.textContent = u.account.name;
      acct.title = T("Droid account {0}", u.account.name);
      acct.hidden = false;
      acct.classList.remove("pop");
      void acct.offsetWidth;
      acct.classList.add("pop");
    }
    tokAnim = true;                            // the chart regrows
    tokOrg = org || null;
  }
  acct.hidden = !org;
  // rebuild only when the numbers (or the hour) move, so nothing jumps
  const hour = new Date().getHours();
  const sig = tokPeriod + "|" + lang + "|" + hour + "|" +
    p.rows.map(r => r.model + ":" + r.total + ":" + r.series.join(",")).join("|");
  if (sig === tokSig) return;
  tokSig = sig;
  tokPalette(u);
  const anim = tokAnim;
  tokAnim = false;

  // chart: one column per bin, models stacked biggest at the bottom
  const chart = $("tokChart");
  chart.innerHTML = "";
  const n = p.keys.length;
  const bins = tokPeriod === "day" ? dayBins(p, hour)
    : p.keys.map((_, i) => ({ a: i, b: i }));
  const sum = (r, b) => { let v = 0; for (let h = b.a; h <= b.b; h++) v += r.series[h]; return v; };
  const cols = bins.map(b => p.rows.reduce((a, r) => a + sum(r, b), 0));
  const max = Math.max(1, ...cols);
  tokBins = bins;
  bins.forEach((bin, i) => {
    const col = document.createElement("div");
    const now = tokPeriod === "day" ? bin.a <= hour && hour <= bin.b : i === n - 1;
    col.className = "tkcol" + (now ? " now" : "") +
      (tokPeriod === "day" && bin.a > hour ? " future" : "") +
      (bin.merged ? " gap" : "");
    col.dataset.i = i;
    if (cols[i]) {
      // % of the busiest column, so the chart follows the slot's height;
      // a quiet bucket still shows as a sliver
      for (const r of p.rows) {
        const v = sum(r, bin);
        if (!v) continue;
        const seg = document.createElement("i");
        seg.dataset.m = r.model;
        seg.style.setProperty("--c", tokColor(r));
        seg.style.height = "max(1px, " + (v / max * 100).toFixed(2) + "%)";
        if (anim) { seg.style.animationDelay = (i * 12) + "ms"; oneShot(seg); }
        else seg.style.animation = "none";
        col.appendChild(seg);
      }
    }
    chart.appendChild(col);
  });
  // axis: a few ticks, never crowded
  const ax = $("tokAxis");
  ax.innerHTML = "";
  ax.classList.toggle("abs", tokPeriod === "day");
  if (tokPeriod === "day") {
    // the bins aren't even: each tick sits under the column its hour is in
    // a merged night is a slim spacer: no tick of its own, and ticks
    // keep 30px apart so two never print over each other
    const cs = chart.children;
    let lastX = -99;
    for (const h of [0, 8, 12, 18]) {
      const k = bins.findIndex(b => b.a <= h && h <= b.b);
      if (k < 0 || bins[k].merged || bins[k].a !== h) continue;
      const x = cs[k].offsetLeft;
      if (x - lastX < 30) continue;
      lastX = x;
      const t = document.createElement("span");
      t.textContent = h + ":00";
      t.style.left = x + "px";
      ax.appendChild(t);
    }
  } else {
  const ticks = tokPeriod === "day" ? [0, 6, 12, 18, 23]
    : tokPeriod === "week" ? [0, 1, 2, 3, 4, 5, 6] : [0, 7, 14, 21, 29];
  for (const i of ticks) {
    const t = document.createElement("span");
    t.textContent = tokPeriod === "day" ? (i === 23 ? "24" : String(i))
      : i === n - 1 ? T("Today")
        : p.keys[i].replace(/^0/, "").replace("-", "/");
    ax.appendChild(t);
  }
  }

  // rows: dot · name · split bar (width = share of the top model) · total
  const rows = $("tokRows");
  rows.innerHTML = "";
  if (!p.rows.length) {
    const em = document.createElement("div");
    em.className = "tkempty";
    em.textContent = T("No tokens used in this period");
    rows.appendChild(em);
  }
  const top = p.rows.length ? p.rows[0].total : 1;
  p.rows.forEach((r, k) => {
    const el = document.createElement("div");
    el.className = "tkrow";
    el.dataset.k = k;
    el.tabIndex = 0;
    el.setAttribute("role", "button");
    el.setAttribute("aria-expanded", "false");
    el.style.setProperty("--c", tokColor(r));
    el.innerHTML = '<i class="tkdot"></i><span class="tkname"></span>' +
      '<div class="tkbar"><span></span></div><span class="tknum"></span>';
    el.querySelector(".tkname").textContent = r.name;
    const bar = el.querySelector(".tkbar>span");
    bar.style.width = Math.max(2, r.total / top * 100) + "%";
    if (anim) { bar.style.animationDelay = (k * 60) + "ms"; oneShot(bar); }
    else bar.style.animation = "none";
    // order on the bar: cache read, cache write, input, output
    const parts = [[r.tok[2], "k-cr"], [r.tok[3], "k-cw"],
      [r.tok[0], "k-in"], [r.tok[1], "k-out"]];
    for (const [v, cls] of parts) {
      if (!v) continue;
      const seg = document.createElement("i");
      seg.className = cls;
      seg.style.width = (v / r.total * 100) + "%";
      bar.appendChild(seg);
    }
    const num = el.querySelector(".tknum");
    num.textContent = fmtBig(r.total);
    if (r.usd != null) {
      const sm = document.createElement("small");
      sm.textContent = "≈" + fmtUsd(r.usd);
      num.appendChild(sm);
    }
    // a mouse click shouldn't leave the keyboard focus ring on the row
    el.onclick = e => { if (e.detail) el.blur(); toggleTokDetail(r.model); };
    el.onkeydown = e => {
      if (e.key === " " || e.key === "Enter") { e.preventDefault(); toggleTokDetail(r.model); }
    };
    rows.appendChild(el);
    // a rebuild keeps the open one open, already unfolded (no replay)
    if (r.model === tokOpen) openTokDetail(el, r, false);
  });
  if (tokOpen && !p.rows.some(r => r.model === tokOpen)) tokOpen = null;
  tokFocus();
}

/* ---- a model's detail: a light panel that unfolds under its row and
   pushes the rows below it down; clicking the row again folds it away.
   One open at a time. */
function tokDetailEl(r) {
  const det = document.createElement("div");
  det.className = "tkdet";
  det.style.setProperty("--c", tokColor(r));
  const inAll = r.tok[0] + r.tok[2] + r.tok[3];
  const hit = inAll ? Math.round(r.tok[2] / inAll * 100) : null;
  // the ring: the four kinds around a 100-unit circle (r = 15.915),
  // clockwise from the top, a hair of gap between them
  const kinds = [[r.tok[2], "k-cr"], [r.tok[3], "k-cw"], [r.tok[0], "k-in"],
    [r.tok[1], "k-out"]].filter(k => k[0] > 0);
  // round caps reach half the stroke (4.4 -> 2.2 units of 100) past each
  // end: a visible gap needs that much on both sides plus a bit
  const gap = kinds.length > 1 ? 6.4 : 0;
  let at = 0, arcs = "";
  kinds.forEach(([v, cls], i) => {
    const len = v / r.total * 100;
    const draw = Math.max(0.6, len - gap);
    arcs += '<circle class="' + cls + '" r="15.915" cx="18" cy="18" ' +
      'style="--len:' + draw.toFixed(2) + ";--at:" + (-at).toFixed(2) +
      ";--d:" + (90 + i * 70) + 'ms"/>';
    at += len;
  });
  const cell = (cls, label, v, i) => '<div class="tkdc" style="--d:' +
    (120 + i * 45) + 'ms"><i class="' + cls + '"></i><span>' + T(label) +
    '</span><b data-v="' + v + '">' + fmtBig(v) + "</b></div>";
  const chips = [];
  if (r.n) chips.push(T(backend === "droid" ? "{0} turns" : "{0} requests", r.n));
  chips.push(r.usd != null ? "≈ " + money(r.usd) : T("no list price"));
  det.innerHTML = '<div class="tkdin"><div class="tkdbox">' +
    '<div class="tkring"><svg viewBox="0 0 36 36"><circle class="trk" r="15.915"' +
    ' cx="18" cy="18"/>' + arcs + '</svg><div class="tkhit"><b>' +
    (hit == null ? "—" : hit + "<small>%</small>") + "</b><span>" +
    T("hit") + '</span></div></div><div class="tkdr"><div class="tkdg">' +
    // the bubble is narrow: the four kinds by their short names
    cell("k-cr", "Read", r.tok[2], 0) + cell("k-cw", "Write", r.tok[3], 1) +
    cell("k-in", "Input", r.tok[0], 2) + cell("k-out", "Output", r.tok[1], 3) +
    '</div><div class="tkdf">' + chips.map(c => "<em>" + esc(c) + "</em>").join("") +
    "</div></div></div></div>";
  det.title = r.model;                      // the raw id, for the curious
  return det;
}

// the numbers run up to their value as the card pops (450ms, ease-out)
function tokCount(det) {
  const els = [...det.querySelectorAll(".tkdc b")];
  const t0 = performance.now();
  const step = now => {
    const k = Math.min(1, (now - t0) / 450);
    const e = 1 - Math.pow(1 - k, 3);
    for (const b of els) b.textContent = fmtBig(Math.round(Number(b.dataset.v) * e));
    if (k < 1 && det.isConnected) requestAnimationFrame(step);
  };
  requestAnimationFrame(step);
}

function openTokDetail(row, r, animate) {
  const det = tokDetailEl(r);
  row.classList.add("open");
  row.setAttribute("aria-expanded", "true");
  row.after(det);
  if (!animate) { det.classList.add("open", "still"); return; }
  det.getBoundingClientRect();              // start from folded
  det.classList.add("open", "pop");
  tokCount(det);
  setTimeout(() => tokReveal(row, det), 380);   // once it has unfolded
  // settled: drop the pop so a later restyle can never replay it
  setTimeout(() => det.classList.replace("pop", "still"), 900);
}

// scroll the list so the row and its detail both show; when they don't
// fit together the row stays at the top — never scrolled away
function tokReveal(row, det) {
  const box = $("tokRows");
  // .tkrows is positioned: offsets are already relative to it
  const top = row.offsetTop;
  const bottom = det.offsetTop + det.offsetHeight;
  let y = box.scrollTop;
  if (bottom > y + box.clientHeight) y = bottom - box.clientHeight;
  if (top < y) y = top;
  box.scrollTo({ top: y, behavior: "smooth" });
}

function closeTokDetail(row) {
  row.classList.remove("open");
  row.setAttribute("aria-expanded", "false");
  const det = row.nextElementSibling;
  if (!det || !det.classList.contains("tkdet")) return;
  det.classList.remove("still", "pop", "open");
  det.classList.add("shut");                // the card shrinks back into the row
  setTimeout(() => det.remove(), 320);
}

function toggleTokDetail(model) {
  const rows = $("tokRows");
  const cur = rows.querySelector(".tkrow.open");
  if (cur) closeTokDetail(cur);
  if (tokOpen === model) { tokOpen = null; tokFocus(); return; }
  tokOpen = model;
  const p = tokCur;
  const k = p ? p.rows.findIndex(r => r.model === model) : -1;
  const row = rows.querySelector('.tkrow[data-k="' + k + '"]');
  if (row) openTokDetail(row, p.rows[k], true);
  tokFocus();
}

// the chart follows the open model: its share stays, the rest fades
function tokFocus() {
  const ch = $("tokChart");
  // the detail names the four kinds itself: the legend makes room
  $("sessCard").classList.toggle("tk-detail", !!tokOpen);
  ch.classList.toggle("focus", !!tokOpen);
  for (const seg of ch.querySelectorAll("i"))
    seg.classList.toggle("hi", seg.dataset.m === tokOpen);
}

function tokTipAt(e, html) {
  const tip = $("tokTip");
  tip.innerHTML = html;
  tip.hidden = false;
  const card = $("sessCard").getBoundingClientRect();
  const w = tip.offsetWidth, h = tip.offsetHeight;
  const x = Math.max(4, Math.min(card.width - w - 4,
    e.clientX - card.left - w / 2));
  let y = e.clientY - card.top - h - 12;     // above the pointer…
  if (y + card.top < 4) y = e.clientY - card.top + 16;   // …or below it
  tip.style.left = x + "px";
  tip.style.top = y + "px";
}

const esc = s => String(s).replace(/[&<>"]/g, c =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]);

function tokColTip(e) {
  const col = e.target.closest(".tkcol");
  const p = tokCur;
  if (!col || !p) { $("tokTip").hidden = true; return; }
  const i = Number(col.dataset.i);
  const bin = tokBins[i] || { a: i, b: i };
  const loc = lang === "zh" ? "zh-CN" : "en-US";
  const sum = r => { let v = 0; for (let h = bin.a; h <= bin.b; h++) v += r.series[h]; return v; };
  let head;
  if (tokPeriod === "day") head = String(bin.a).padStart(2, "0") + ":00 – " +
    String(bin.b).padStart(2, "0") + ":59";
  else {
    const d = new Date();
    d.setDate(d.getDate() - (p.keys.length - 1 - i));
    head = d.toLocaleDateString(loc, { weekday: "short", month: "short", day: "numeric" });
  }
  const parts = p.rows.filter(r => sum(r));
  let html = '<div class="hd">' + esc(head) + "</div>";
  if (!parts.length)
    html += '<span class="dim">' + T("No tokens used in this period") + "</span>";
  for (const r of parts)
    html += '<div class="tl"><em style="--c:' + tokColor(r) + '"><i></i>' +
      esc(r.name) + "</em><span>" + fmtBig(sum(r)) + "</span></div>";
  if (parts.length > 1)
    html += '<div class="tl ft"><em>' + T("Total") + "</em><span>" +
      fmtBig(parts.reduce((a, r) => a + sum(r), 0)) + "</span></div>";
  tokTipAt(e, html);
}


// the savings card starts folded; a click on its title opens the squares
let saveFolded = localStorage.getItem("cb.saveFolded") !== "0";
function setSaveFolded(on, flip) {
  if (flip) {
    const c = $("saveHead").querySelector(".scoin");
    c.classList.remove("flip");
    void c.getBoundingClientRect();
    c.classList.add("flip");
  }
  saveFolded = on;
  localStorage.setItem("cb.saveFolded", on ? "1" : "0");
  $("saveCard").classList.toggle("folded", on);
  $("saveHead").setAttribute("aria-expanded", on ? "false" : "true");
  if (on) $("heatTip").hidden = true;
}

function renderHeat(led) {
  const card = $("saveCard");
  card.hidden = !led;                // Devin's backend has no ledger
  if (!led) return;
  heatMonth = led.month;
  heatFirst = led.first;
  const days = led.days;
  // what Buddy itself saved: the turns the heartbeat rescued, minus every
  // heartbeat that went out — a day that cost more than it rescued counts
  // as a loss, not as zero
  const total = days.reduce((a, d) => a + (d.net || 0), 0);
  $("saveTotal").textContent = total > 0 ? money(total)
    : total < 0 ? "−" + money(-total) : "";
  card.classList.toggle("up", total > 0);
  $("saveTotal").title = T(total < 0 ? "Spent more on heartbeats than rescued"
    : "Saved by keeping the cache alive");
  $("mLabel").textContent = monthLabel(heatMonth);
  $("mPrev").disabled = heatMonth <= heatFirst;
  const cur = new Date().toISOString().slice(0, 7);
  $("mNext").disabled = heatMonth >= cur;
  const sig = heatMonth + "|" + days.map(d => (d.day || "") + ":" +
    (d.net || 0)).join(",");
  if (sig === heatSig) return;       // rebuild only when the numbers move
  heatSig = sig;
  heatDays = days;
  const max = Math.max(...days.map(d => Math.max(0, d.net || 0)));
  const box = $("heat");
  box.innerHTML = "";
  for (const d of days) {
    const el = document.createElement("div");
    el.className = "hc l" + heatLevel(d.net || 0, max) +
      (d.future ? " future" : "");
    el.dataset.day = d.day;
    box.appendChild(el);
  }
}

async function gotoMonth(ym) {
  if (!ym || ym === heatMonth) return;
  const led = await api().ledger_month(ym);
  if (!led) return;
  heatSig = "";
  renderHeat(led);
}

function heatTip(e) {
  const tip = $("heatTip");
  const el = e.target.closest(".hc");
  if (!el || el.classList.contains("future")) { tip.hidden = true; return; }
  const d = heatDays.find(x => x.day === el.dataset.day) || {};
  const date = new Date(d.day + "T12:00:00").toLocaleDateString(lang === "zh" ? "zh-CN" : "en-US",
    { weekday: "short", month: "short", day: "numeric" });
  const net = d.net || 0;
  let html = date + "<br>";
  if (d.rescues || d.hb) {          // what the cell's colour tracks
    html += T(net < 0 ? "Buddy spent {0}" : "Buddy saved {0}",
      "<b>" + money(Math.abs(net)) + "</b>") + "<br>" +
      '<span class="dim">' + T("{0} rescued · {1} renews", d.rescues || 0,
      d.hb || 0) + "</span>";
  } else {
    html += '<span class="dim">' + T("No guarding") + '</span>';
  }
  tip.innerHTML = html;
  tip.hidden = false;
  const card = $("saveCard").getBoundingClientRect();
  const r = el.getBoundingClientRect();
  const w = tip.offsetWidth, h = tip.offsetHeight;
  const x = Math.max(4, Math.min(card.width - w - 4,
    r.left - card.left + r.width / 2 - w / 2));
  let y = r.top - card.top - h - 6;    // above the cell…
  if (y < 2) y = r.bottom - card.top + 6;   // …or below it in the 1st row
  tip.style.left = x + "px";
  tip.style.top = y + "px";
}

/* -------------------------------------------- harness picker */
/* Claude: the orange burst of tapered rays from the centre */
function claudeMark() {
  const rays = [];
  for (let i = 0; i < 12; i++) {
    const a = (i / 12) * Math.PI * 2 - Math.PI / 2;
    const len = i % 2 ? 9.2 : 10.6, w = 0.5;   // alternating ray lengths
    const p = (r, d) => (12 + Math.cos(a + d) * r).toFixed(2) + "," +
      (12 + Math.sin(a + d) * r).toFixed(2);
    rays.push("M" + p(1.4, -w) + "L" + p(len, 0) + "L" + p(1.4, w) + "Z");
  }
  return '<svg viewBox="0 0 24 24"><path fill="#D97757" stroke="#D97757"' +
    ' stroke-width="1.3" stroke-linejoin="round" d="' + rays.join("") +
    '"/><circle cx="12" cy="12" r="2.2" fill="#D97757"/></svg>';
}
const HARNESS = {
  cc: { name: "Claude Code", icon: claudeMark },
  devin: { name: "Devin", icon: () => Mascots.devinLogo() },
  droid: { name: "Droid", icon: () => Mascots.factoryLogo() },
};

let account;                          // Droid's signed-in account, as last seen

function renderBackends(s) {
  const list = s.backends || [];
  $("hk").hidden = list.length < 2;
  if (s.backend && s.backend !== backend) {   // first frame, or switched
    backend = s.backend;
    account = s.account;
    sel = localStorage.getItem(selKey()) || null;
    spaces = null; renderTree(); refreshSpaces();
    const h = HARNESS[backend];
    $("hkIcon").innerHTML = h ? h.icon() : "";
    $("hkBtn").title = T("{0} · switch harness", h ? h.name : "");
  } else if (s.account !== account) {         // signed in to another account
    account = s.account;                      // (Droid): its sessions instead
    refreshSpaces();
  }
  for (const b of $("hkMenu").children) {
    b.classList.toggle("on", b.dataset.bk === backend);
    b.setAttribute("aria-checked", b.dataset.bk === backend);
    b.disabled = !list.includes(b.dataset.bk);
    // the Droid item carries the accounts flyout's chevron
    b.classList.toggle("hassub",
      b.dataset.bk === "droid" && list.includes("droid"));
  }
}

function setMenu(open) {
  $("hkMenu").hidden = !open;
  if (!open) $("droidAcctMenu").hidden = true;
  $("hkBtn").setAttribute("aria-expanded", open);
}

async function switchBackend(name) {
  setMenu(false);
  if (name === backend) return;
  try { await api().set_backend(name); } catch (e) { return; }
  dueLeft = null;
  await poll();                               // renderBackends reloads tree
}

/* ------------------------------------------------- sessions tree */
function itemEl(s) {
  const el = document.createElement("div");
  el.className = "sitem" + (s.sid === sel ? " sel" : "");
  el.dataset.sid = s.sid;
  const guarded = !!(S && ((S.guarded || []).includes(s.sid) ||
    (S.target && S.target.sid === s.sid)));
  // live/ready only come from the Claude Code backend: ready = the proxy
  // holds a request it can replay, live = the CLI process is running
  const dot = s.ready ? '<i class="sdot ready" title="' + T("Proxied · can renew") + '"></i>'
    : s.live ? '<i class="sdot live" title="' + T("Running · not via proxy yet") + '"></i>'
    : "";
  // Devin: sessions run from the terminal carry a small "CLI" tag right
  // after the name (Desktop is the default, so it stays unmarked)
  // locked: open in a terminal started before Buddy — the lock takes the
  // heart's place, guarded or not, since nothing can renew it yet
  if (s.locked) el.title = lockedHint(s.sid);
  el.innerHTML =
    (s.locked ? LOCK_ICON : guarded ? '<span class="gheart">♥</span>' : "") +
    '<span class="sname"><span class="stitle"></span>' +
    (s.source === "cli" ? '<span class="srctag">CLI</span>' : "") +
    "</span>" + dot + '<span class="stime"></span>';
  // just guarded: the row glows green and its ♥ pops in, on the wall
  // clock (--gd), so a rebuild mid-way carries on instead of restarting
  const gsince = guardFx.sid === s.sid ? Date.now() - guardFx.at : Infinity;
  if (gsince < GUARD_FX_MS) {
    el.classList.add("fxflash");
    el.style.setProperty("--gd", -gsince + "ms");
  }
  const title = el.querySelector(".stitle");
  const since = rollAt ? Date.now() - rollAt : Infinity;
  if (since < ROLL_MS) {
    // mid-roll: wrap the text so the keyframes can move it, and give the
    // row its offset (rows stagger a little so the list ripples rather
    // than snapping as one block)
    const inner = document.createElement("span");
    inner.textContent = s.title;
    title.appendChild(inner);
    title.classList.add("roll");
    title.style.setProperty("--rd",
      (-(since + Math.min(rollIdx++, 6) * 55)) + "ms");
  } else {
    title.textContent = s.title;
  }
  const st = el.querySelector(".stime");
  if (s.ctx) {
    // two faces in one slot: the last reply, and now and then (a roll
    // every FLIP_MS) the context size — no extra column needed
    st.classList.add("flip");
    st.innerHTML = '<span class="fa"></span><span class="fb"></span>';
    st.querySelector(".fa").textContent = relTime(s.last_activity);
    const fb = st.querySelector(".fb");
    fb.textContent = fmtTok(s.ctx);
    fb.classList.toggle("warn", s.ctx >= 600000);   // getting near 1M
    fb.classList.toggle("hot", s.ctx >= 800000);    // compaction is close
    st.title = T("Last reply {0} · context {1} tokens", relTime(s.last_activity),
      fmtTok(s.ctx));
    // the tree is rebuilt every second: start each row's roll where the
    // wall clock says it is, so it never restarts — rows staggered a bit
    st.style.setProperty("--fd",
      -((Date.now() + flipIdx++ * 700) % FLIP_MS) + "ms");
  } else {
    st.textContent = relTime(s.last_activity);
  }
  // right-click: carry it into another harness (where one is offered)
  el.oncontextmenu = e => { e.preventDefault(); openRowMenu(e, s); };
  // selecting re-renders so the guard button flips Stop/Guard right away
  el.onclick = () => { sel = s.sid; localStorage.setItem(selKey(), sel); render(); };
  el.ondblclick = () => { sel = s.sid; localStorage.setItem(selKey(), sel); startGuard(s.sid); };
  return el;
}

/* ------------------------------------------------- carry a session */
/* Right-click a row -> "Carry to <harness>" (only targets the backend
   offers) -> a card with what carrying would do (session-bridge's dry
   run) -> confirm -> the new session is written, the panel switches to
   the target harness with it selected. Nothing is sent: its cache only
   exists after the first real reply there. */
let carryTargets = null;              // per backend, asked once

function menuItem(icon, label) {
  const b = document.createElement("button");
  b.setAttribute("role", "menuitem");
  b.innerHTML = '<i class="hico"></i><span></span>';
  b.querySelector(".hico").innerHTML = icon;
  b.querySelector("span").textContent = label;
  return b;
}

function closeRowMenu() {
  $("rowMenu").hidden = true;
  $("acctMenu").hidden = true;
}

async function openRowMenu(e, s) {
  const m = $("rowMenu");
  if (!carryTargets || carryTargets.backend !== backend) {
    let list = [];
    try { list = (await api().carry_targets()) || []; } catch (err) { }
    carryTargets = { backend, list };
  }
  // Droid: the other Factory accounts, asked each time (a switch or a new
  // account shows up at once). One account only: no "Droid ▸" at all
  let others = [];
  if (backend === "droid") {
    try { others = ((await api().droid_accounts()) || []).filter(a => !a.current); } catch (err) { }
  }
  if (!carryTargets.list.length && !others.length) return;   // nothing to offer
  m.innerHTML = "";
  $("acctMenu").hidden = true;
  for (const t of carryTargets.list) {
    const b = menuItem(HARNESS[t] ? HARNESS[t].icon() : "",
      T("Carry to {0}", HARNESS[t] ? HARNESS[t].name : t));
    b.onclick = ev => { ev.stopPropagation(); closeRowMenu(); carryAsk(s, t); };
    b.onmouseenter = () => { $("acctMenu").hidden = true; };
    m.appendChild(b);
  }
  if (others.length) {
    const b = menuItem(HARNESS.droid ? HARNESS.droid.icon() : "", HARNESS.droid ? HARNESS.droid.name : "Droid");
    b.classList.add("hassub");
    b.setAttribute("aria-haspopup", "menu");
    const open = () => openAcctMenu(b, s, others);
    b.onmouseenter = open;
    b.onclick = ev => { ev.stopPropagation(); open(); $("acctMenu").querySelector("button").focus(); };
    m.appendChild(b);
  }
  // place by the pointer, inside the window (the card lets it out)
  const card = $("sessCard").getBoundingClientRect();
  const vw = document.documentElement.clientWidth, vh = document.documentElement.clientHeight;
  m.hidden = false;
  // viewport px, then relative to the card it's positioned in
  const x = Math.max(6, Math.min(e.clientX, vw - m.offsetWidth - 6));
  const y = Math.max(6, Math.min(e.clientY, vh - m.offsetHeight - 6));
  m.style.left = (x - card.left) + "px";
  m.style.top = (y - card.top) + "px";
  m.querySelector("button").focus();
}

/* a flyout beside a menu item (Droid ▸ in the row menu, Droid in the
   harness picker): to the menu's right, else its left; a narrow window
   fits neither, so the menu slides left until the flyout fits. `host`
   is the positioned ancestor the menu and flyout live in. */
function flyoutPlace(sub, item, menu, host) {
  const card = host.getBoundingClientRect();
  const vw = document.documentElement.clientWidth, vh = document.documentElement.clientHeight;
  sub.hidden = false;
  // offsetWidth/Height, not getBoundingClientRect: the rowmenu pops in
  // with a scale transform (tippop) and the rect is smaller mid-flight
  const w = sub.offsetWidth, h = sub.offsetHeight, gap = 2, edge = 6;
  const m = menu.getBoundingClientRect();
  const r = item.getBoundingClientRect();
  let x, y;                                    // viewport px
  if (m.right + gap + w <= vw - edge) {        // right of the menu
    x = m.right + gap;
    y = r.top - 4;
  } else if (m.left - gap - w >= edge) {       // left of it
    x = m.left - gap - w;
    y = r.top - 4;
  } else {                                     // too narrow: below the item
    x = Math.min(r.left, vw - edge - w);
    y = Math.min(m.bottom + gap, vh - edge - h);
  }
  x = Math.max(edge, Math.min(x, vw - edge - w));
  y = Math.max(edge, Math.min(y, vh - edge - h));
  sub.style.left = (x - card.left) + "px";
  sub.style.top = (y - card.top) + "px";
}

/* the flyout beside "Droid ▸": one row per other account */
function openAcctMenu(item, s, others) {
  const sub = $("acctMenu");
  sub.innerHTML = "";
  for (const a of others) {
    const b = menuItem("", T("Account {0}", a.name));
    b.querySelector(".hico").textContent = a.name;
    b.querySelector(".hico").classList.add("acct");
    b.onclick = ev => { ev.stopPropagation(); closeRowMenu(); copyAsk(s, a); };
    sub.appendChild(b);
  }
  flyoutPlace(sub, item, $("rowMenu"), $("sessCard"));
}

/* -------------------------------------- Droid account switching */
/* The harness picker's Droid item grows a flyout: saved accounts (the
   letter Buddy gave them, never the profile folder's name) + "add".
   The last list renders at once, the fresh one replaces it. */
let droidProfs = null;               // last rows droid_profiles() sent
let acctLock = false;                // add-step 2: scrim/Escape do nothing

function renderDroidAccts(rows) {
  const sub = $("droidAcctMenu");
  sub.innerHTML = "";
  for (const a of rows) {
    const b = menuItem("", a.email || a.name);
    const ico = b.querySelector(".hico");
    ico.textContent = a.letter || "?";
    ico.classList.add("acct");
    const sp = b.querySelector("span");
    sp.textContent = a.email || a.name;
    b.title = a.email || "";
    if (a.current) b.classList.add("on");
    if (!a.has_key) {
      const m = document.createElement("i");
      m.className = "nokey";
      m.textContent = "·";
      m.title = T("No API key · quota won't show");
      b.appendChild(m);
    }
    if (!a.current)
      b.onclick = ev => { ev.stopPropagation(); setMenu(false); droidSwitchAsk(a); };
    sub.appendChild(b);
  }
  const div = document.createElement("div");
  div.className = "rdiv";
  sub.appendChild(div);
  const add = menuItem("", "");
  add.querySelector(".hico").textContent = "+";
  add.querySelector("span").textContent = T("Add account");
  add.onclick = ev => { ev.stopPropagation(); setMenu(false); droidAddAsk(); };
  sub.appendChild(add);
}

function openDroidAccts(item) {
  const sub = $("droidAcctMenu");
  if (droidProfs) {
    renderDroidAccts(droidProfs);
    flyoutPlace(sub, item, $("hkMenu"), $("hk"));
  }
  api().droid_profiles().then(rows => {
    droidProfs = rows || [];
    if ($("hkMenu").hidden) return;       // menu closed while it asked
    renderDroidAccts(droidProfs);
    flyoutPlace(sub, item, $("hkMenu"), $("hk"));
  }).catch(() => {
    if (!droidProfs) sub.hidden = true;
  });
}

/* the account card: the carry card's grammar — head chips, title, stats,
   warn, two buttons — plus a password field and a link for the key step */
function acctCardShow() {
  const c = $("acctCard");
  c.classList.remove("done", "fail");
  c.hidden = false;
  $("carryScrim").hidden = false;
  return c;
}

function closeAcct() {
  $("acctCard").hidden = true;
  $("carryScrim").hidden = true;
  acctLock = false;
}

function acctCardReset(c) {
  const yes = c.querySelector(".ccyes"), no = c.querySelector(".ccno");
  yes.hidden = false; yes.disabled = false;
  no.textContent = T("Cancel");
  c.querySelector(".ccwarn").hidden = true;
  c.querySelector(".ccsub").hidden = true;
  c.querySelector(".cckey").hidden = true;
  c.querySelector(".cclink").hidden = true;
  c.querySelector(".ccspin").hidden = true;
  return { yes, no };
}

function acctHead(c, fromLetter, toLabel) {
  const ico = HARNESS.droid ? HARNESS.droid.icon() : "";
  c.querySelector(".ccfrom").innerHTML = ico + (fromLetter ? "<b>" + fromLetter + "</b>" : "");
  c.querySelector(".ccto").innerHTML = ico + "<b>" + toLabel + "</b>";
}

function acctDone(c, text) {
  c.classList.add("done");
  c.querySelector(".ccstats").textContent = text;
  c.querySelector(".ccwarn").hidden = true;
  const { yes, no } = { yes: c.querySelector(".ccyes"), no: c.querySelector(".ccno") };
  yes.hidden = true;
  no.textContent = "OK";
  no.onclick = () => { closeAcct(); acctCardReset(c); };
}

function curLetter() {
  const cur = (droidProfs || []).find(a => a.current);
  return cur ? cur.letter : "?";
}

function letterForOrg(org) {
  const a = (droidProfs || []).find(x => x.org === org);
  return a ? a.letter : "?";
}

/* switch to a saved account: confirm first — Factory restarts and a
   reply in progress is cut; a foreign droid.exe could write the old
   account back, so it's a warning with a force option, not a block */
function droidSwitchAsk(a) {
  const c = acctCardShow();
  const { yes, no } = acctCardReset(c);
  const stats = c.querySelector(".ccstats"), warn = c.querySelector(".ccwarn");
  acctHead(c, curLetter(), a.letter || "?");
  c.querySelector(".cctext").textContent = T("Switch to {0}", a.email);
  const what = T("Factory will close and reopen; a reply in progress is " +
    "interrupted. Sessions are local and stay.");
  stats.textContent = what;
  yes.textContent = T("Switch");
  no.onclick = closeAcct;
  let force = false;
  yes.onclick = async () => {
    yes.disabled = true;
    stats.textContent = T("Switching…");
    let r;
    try { r = await api().droid_switch(a.name, force); } catch (e) { r = { ok: false }; }
    if (c.hidden) return;
    if (r && r.ok) {
      droidProfs = null;
      acctDone(c, T("Switched to {0}", r.email) +
        (r.note ? " · " + T("Couldn't launch Factory · open it yourself") : ""));
    } else if (r && r.error === "cli_running") {
      warn.textContent = T("{0} droid CLI running · they could write the old account back", r.count);
      warn.hidden = false;
      stats.textContent = what;
      yes.textContent = T("Close them and switch");
      yes.disabled = false;
      force = true;
    } else {
      c.classList.add("fail");
      stats.textContent = T("Couldn't switch: {0}", (r && (r.error || r.detail)) || "error");
    }
  };
}

/* add an account: clear the live login so Factory opens its sign-in
   page (its own Log out could revoke the saved account); the new login
   is saved when it lands, cancel restores the previous one */
function droidAddAsk() {
  const c = acctCardShow();
  const { yes, no } = acctCardReset(c);
  const stats = c.querySelector(".ccstats"), warn = c.querySelector(".ccwarn"),
    sub = c.querySelector(".ccsub");
  acctHead(c, curLetter(), "+");
  c.querySelector(".cctext").textContent = T("Add a Droid account");
  const what = T("Buddy saves the current account, then restarts Factory " +
    "on its sign-in page. Sign in with the new account's email and code " +
    "once — switching after that needs no code.");
  stats.textContent = what;
  sub.textContent = T("Don't use Factory's Log out — it can invalidate saved accounts.");
  sub.hidden = false;
  yes.textContent = T("Start");
  no.onclick = closeAcct;
  let force = false;
  yes.onclick = async () => {
    yes.disabled = true;
    stats.textContent = T("Switching…");
    let r;
    try { r = await api().droid_add_begin(force); } catch (e) { r = { ok: false }; }
    if (c.hidden) return;
    if (r && r.ok) return addWait(c, r.prev_email);
    if (r && r.error === "cli_running") {
      warn.textContent = T("{0} droid CLI running · they could write the old account back", r.count);
      warn.hidden = false;
      stats.textContent = what;
      yes.textContent = T("Close them and start");
      yes.disabled = false;
      force = true;
    } else {
      c.classList.add("fail");
      stats.textContent = T("Couldn't switch: {0}", (r && (r.error || r.detail)) || "error");
    }
  };
}

let addPollTimer = 0;

async function addWait(c, prevEmail) {
  const { yes, no } = { yes: c.querySelector(".ccyes"), no: c.querySelector(".ccno") };
  const stats = c.querySelector(".ccstats"), warn = c.querySelector(".ccwarn");
  acctLock = true;                       // scrim click and Escape are inert
  c.querySelector(".ccspin").hidden = false;
  c.querySelector(".cctext").textContent = T("Waiting for sign-in…");
  stats.textContent = T("Sign in in the Factory window. This continues on its own.");
  warn.hidden = true;
  yes.hidden = true;
  no.textContent = T("Cancel (restore {0})", prevEmail || T("previous account"));
  clearTimeout(addPollTimer);
  let cancelled = false;
  no.onclick = async () => {
    if (cancelled) return;
    cancelled = true;
    clearTimeout(addPollTimer);
    acctLock = false;
    no.disabled = true;
    let r;
    try { r = await api().droid_add_cancel(); } catch (e) { r = { ok: false }; }
    no.disabled = false;
    if (c.hidden) return;
    c.querySelector(".ccspin").hidden = true;
    if (r && r.ok && r.restored) return acctDone(c, T("Restored {0}", r.restored));
    if (r && r.ok && r.kept) return addDone(c, { email: r.email, letter: r.letter, existing: true });
    c.classList.add("fail");
    stats.textContent = T("Couldn't switch: {0}", (r && (r.error || r.detail)) || "error");
  };
  const tick = async () => {
    if (c.hidden || cancelled) return;
    let r;
    try { r = await api().droid_add_poll(); } catch (e) { r = null; }
    if (c.hidden || cancelled) return;
    if (r && r.state === "done") {
      acctLock = false;
      c.querySelector(".ccspin").hidden = true;
      droidProfs = null;
      return addDone(c, r);
    }
    addPollTimer = setTimeout(tick, 1500);
  };
  addPollTimer = setTimeout(tick, 1500);
}

function addDone(c, r) {
  const stats = c.querySelector(".ccstats");
  c.classList.add("done");
  stats.textContent = r.existing
    ? T("Already saved · updated {0}", r.email)
    : T("Added {0} · account {1}", r.email, r.letter || "?");
  c.querySelector(".ccwarn").hidden = true;
  keyStep(c, { org: r.org, letter: r.letter || "?", email: r.email });
}

/* the API-key step: one per account — the quota card follows the
   account you're on. Also opened standalone from the quota card's
   "no API key" note. */
function keyStep(c, acct) {
  const { yes, no } = acctCardReset(c);
  const stats = c.querySelector(".ccstats"), warn = c.querySelector(".ccwarn"),
    sub = c.querySelector(".ccsub"), key = c.querySelector(".cckey"),
    link = c.querySelector(".cclink");
  c.querySelector(".cctext").textContent = T("Factory API key · account {0}", acct.letter);
  sub.textContent = T("One per account · the quota card follows the account you're on.");
  sub.hidden = false;
  key.hidden = false;
  key.value = "";
  link.hidden = false;
  link.textContent = T("Get a key on Factory");
  link.onclick = () => api().open_url("https://app.factory.ai/settings/api-keys");
  no.textContent = T("Skip");
  no.onclick = closeAcct;
  yes.textContent = T("Save key");
  yes.onclick = async () => {
    yes.disabled = true;
    stats.textContent = T("Saving…");
    warn.hidden = true;
    let r;
    try { r = await api().droid_set_key(acct.org, key.value.trim()); } catch (e) { r = { ok: false }; }
    if (c.hidden) return;
    yes.disabled = false;
    if (r && r.ok) {
      if (droidProfs) {                       // refresh the has_key mark
        const p = droidProfs.find(x => x.org === acct.org);
        if (p) p.has_key = true;
      }
      return acctDone(c, T("Saved · the quota card now follows this account"));
    }
    const err = r && r.error;
    warn.textContent = err === "wrong_account" ? T("This key belongs to account {0}", letterForOrg(r.key_org))
      : err === "bad_key" ? T("Factory refused this key")
      : err === "bad_format" ? T("a Factory API key starts with fk-")
      : T("Couldn't reach Factory");
    warn.hidden = false;
    stats.textContent = "";
  };
}

/* the quota card's no-key note: the per-account key is missing — a way
   to add it right there for the signed-in account */
async function droidKeyAsk() {
  let acct = (droidProfs || []).find(a => a.current);
  if (!acct) {
    try { droidProfs = (await api().droid_profiles()) || []; } catch (e) { }
    acct = (droidProfs || []).find(a => a.current);
  }
  if (!acct) return;
  const c = acctCardShow();
  acctCardReset(c);
  acctHead(c, curLetter(), acct.letter || "?");
  keyStep(c, acct);
}

/* Copy to the other account: no confirm. A see-through toast, the left
   Droid (this account) tosses a file to the right one; it keeps tossing
   while the copy runs, the right one catches, a tick, and it fades. On
   failure it stays a little longer with why. */
let xferBusy = false;

function xferToss(t) {
  const from = t.querySelector(".xfrom"), to = t.querySelector(".xto");
  const file = t.querySelector(".xfile");
  const dx = to.offsetLeft - from.offsetLeft;  // across the stage
  from.animate([{ transform: "none" }, { transform: "rotate(-10deg) scaleY(.9)", offset: .2 },
    { transform: "rotate(6deg)", offset: .38 }, { transform: "none" }],
  { duration: 900, easing: "ease-out" });
  to.animate([{ transform: "none" }, { transform: "none", offset: .74 },
    { transform: "scale(1.1, .88)", offset: .84 }, { transform: "none" }],
  { duration: 900, easing: "ease-out" });
  return file.animate([
    { opacity: 0, transform: "translate(0, 8px) rotate(0)" },
    { opacity: 1, transform: "translate(" + dx * .12 + "px, -4px) rotate(-30deg)", offset: .18 },
    { opacity: 1, transform: "translate(" + dx * .5 + "px, -20px) rotate(-200deg)", offset: .5 },
    { opacity: 1, transform: "translate(" + dx * .86 + "px, -2px) rotate(-340deg)", offset: .8 },
    { opacity: 0, transform: "translate(" + dx + "px, 10px) rotate(-360deg) scale(.6)" }],
  { duration: 900, easing: "cubic-bezier(.4, 0, .6, 1)" }).finished.catch(() => { });
}

async function copyAsk(s, a) {
  if (xferBusy) return;
  xferBusy = true;
  const t = $("xferToast");
  const accts = (await api().droid_accounts().catch(() => [])) || [];
  const cur = accts.find(x => x.current);
  for (const el of t.querySelectorAll(".xmark")) el.innerHTML = HARNESS.droid ? HARNESS.droid.icon() : "";
  t.querySelector(".xfrom b").textContent = cur ? cur.name : "";
  t.querySelector(".xto b").textContent = a.name;
  t.querySelector(".xmsg").textContent = "";
  t.classList.remove("done", "fail", "out");
  t.hidden = false;
  const still = matchMedia("(prefers-reduced-motion: reduce)").matches;
  let r = null;
  const job = api().droid_copy(s.sid, a.org)
    .then(x => { r = x; }, () => { r = { error: "failed" }; });
  // at least one whole toss, however fast the copy is
  do { if (!still) await xferToss(t); else await job; } while (r === null);
  const ok = r && !r.error && r.session_id;
  if (ok) {
    t.classList.add("done");
    t.querySelector(".xmsg").textContent = T("Copied to account {0}", a.name);
    if (!still) t.querySelector(".xto").animate([{ transform: "none" },
      { transform: "translateY(-5px) scale(1.06)", offset: .4 }, { transform: "none" }],
    { duration: 420, easing: "cubic-bezier(.3, 1.4, .5, 1)" });
  } else {
    t.classList.add("fail");
    const why = r && r.error === "no_bridge" ? T("session-bridge not found") : (r && r.detail) || "error";
    t.querySelector(".xmsg").textContent = T("Couldn't copy: {0}", why);
  }
  await new Promise(res => setTimeout(res, ok ? 1500 : 3200));
  t.classList.add("out");
  await new Promise(res => setTimeout(res, 260));
  t.hidden = true;
  t.classList.remove("out");
  xferBusy = false;
}

const CARRY_WARN = { large: "Large context: the first reply there pays to read all of it",
  cwd_missing: "The working folder no longer exists" };

function carryCard(s, t) {
  const c = $("carryCard");
  c.querySelector(".ccfrom").innerHTML = HARNESS[backend] ? HARNESS[backend].icon() : "";
  c.querySelector(".ccto").innerHTML = HARNESS[t] ? HARNESS[t].icon() : "";
  c.querySelector(".cctitle").textContent = s.title;
  c.classList.remove("done", "fail");
  c.hidden = false;
  $("carryScrim").hidden = false;
  return c;
}

async function carryAsk(s, t) {
  const c = carryCard(s, t);
  const stats = c.querySelector(".ccstats"), warn = c.querySelector(".ccwarn");
  const yes = c.querySelector(".ccyes"), no = c.querySelector(".ccno");
  stats.textContent = T("Reading the session…");
  warn.hidden = true;
  yes.disabled = true;
  no.onclick = closeCarry;
  let p;
  try { p = await api().carry_preview(s.sid, t); } catch (err) { p = { error: "failed" }; }
  if (c.hidden) return;                        // cancelled meanwhile
  if (!p || p.error) return carryFailed(c, p);
  const a = p.after || {};
  stats.innerHTML = "";
  const line = document.createElement("div");
  line.textContent = T("{0} messages · {1} tool calls · ≈{2} tokens",
    (a.user_messages || 0) + (a.assistant_messages || 0), a.tool_calls || 0,
    fmtBig(a.tokens || 0));
  const sub = document.createElement("div");
  sub.className = "ccsub";
  sub.textContent = (p.trimmed_calls ? T("{0} old read-only calls left out", p.trimmed_calls) + " · " : "") +
    T("A new session is written; this one stays as it is. Nothing is sent until you reply there.");
  stats.append(line, sub);
  const ws = (p.warnings || []).map(w => T(CARRY_WARN[w.kind] || w.message));
  warn.textContent = ws.join(" · ");
  warn.hidden = !ws.length;
  yes.disabled = false;
  yes.focus();
  yes.onclick = async () => {
    yes.disabled = true;
    stats.textContent = T("Carrying…");
    let r;
    try { r = await api().carry(s.sid, t); } catch (err) { r = { error: "failed" }; }
    if (!r || r.error || !r.written) return carryFailed(c, r);
    // the panel is on the target harness now: select the new session
    const nid = r.written.session_id;
    c.classList.add("done");
    stats.textContent = T("Carried · it's in the list, open it in {0} to continue",
      HARNESS[t] ? HARNESS[t].name : t);
    warn.hidden = true;
    no.textContent = "OK";
    yes.hidden = true;
    no.onclick = () => { closeCarry(); yes.hidden = false; no.textContent = T("Cancel"); };
    await poll();                               // renderBackends switches
    backend = t;
    sel = nid;
    localStorage.setItem(selKey(), sel);
    await refreshSpaces();
    const row = document.querySelector('.sitem[data-sid="' + CSS.escape(nid) + '"]');
    if (row) { row.scrollIntoView({ block: "center" }); row.classList.add("fxflash"); }
  };
}

function closeCarry() {
  $("carryCard").hidden = true;
  $("carryScrim").hidden = true;
}

function carryFailed(c, r) {
  c.classList.add("fail");
  const why = r && r.error === "no_bridge" ? T("session-bridge not found")
    : (r && r.detail) || "error";
  c.querySelector(".ccstats").textContent = T("Couldn't carry: {0}", why);
  c.querySelector(".ccyes").disabled = true;
}

const LOCK_ICON =
  '<svg class="glock" viewBox="0 0 24 24"><rect x="5" y="10.5" width="14"' +
  ' height="10" rx="2"/><path d="M8.5 10.5V7.8a3.5 3.5 0 0 1 7 0v2.7"/></svg>';

function lockedHint(sid) {
  return T("LOCKED_HINT", sid);
}

function findSession(sid) {
  for (const e of spaces || [])
    for (const s of e.type === "group" ? e.items : [e])
      if (s.sid === sid) return s;
  return null;
}

/* Start guard — unless the session is locked: then say why instead */
function startGuard(sid) {
  const s = findSession(sid);
  if (s && s.locked) { showLockNote(sid); return; }
  api().start(sid);
}

let lockNoteTimer = null;
function showLockNote(sid) {
  const n = $("lockNote");
  n.textContent = lockedHint(sid);
  n.hidden = false;
  clearTimeout(lockNoteTimer);
  lockNoteTimer = setTimeout(() => { n.hidden = true; }, 9000);
}

/* line-style folder icons, closed vs open — 14px, stroke #6B7280 1.5px */
const FOLDER_CLOSED =
  '<svg class="fo fo-closed" viewBox="0 0 24 24">' +
  '<path d="M4.8 5h4.5L11 7.2h8.2a1.8 1.8 0 0 1 1.8 1.8v8.2' +
  'a1.8 1.8 0 0 1-1.8 1.8H4.8a1.8 1.8 0 0 1-1.8-1.8V6.8' +
  'A1.8 1.8 0 0 1 4.8 5z"/></svg>';
const FOLDER_OPEN =
  '<svg class="fo fo-open" viewBox="0 0 24 24">' +
  '<path d="M4.8 5h4.5L11 7.2h8.2a1.8 1.8 0 0 1 1.8 1.8v1.6"/>' +
  '<path d="M3 9.2V17.2A1.8 1.8 0 0 0 4.8 19h12.9' +
  'a1.7 1.7 0 0 0 1.55-1l2.35-6.2a1.35 1.35 0 0 0-1.28-1.8H9.1' +
  'a1.7 1.7 0 0 0-1.6 1.1L4.4 19"/></svg>';

const FLIP_MS = 14000;         // keep in step with app.css .stime.flip
let flipIdx = 0
let rollIdx = 0;              // per-render stagger, see renderTree;

function renderTree() {
  const tree = $("tree");
  tree.innerHTML = "";
  flipIdx = 0; rollIdx = 0;
  for (const e of spaces || []) {
    if (e.type === "group") {
      // localStorage wins once the user has toggled; until then the
      // space's own collapsed flag (from Devin's metadata) decides
      const closed = e.id in groupOpen ? !groupOpen[e.id] : !!e.collapsed;
      const gh = document.createElement("div");
      gh.className = "ghead" + (closed ? " closed" : "");
      gh.innerHTML = '<span class="fold">' + FOLDER_CLOSED + FOLDER_OPEN +
        '</span><span class="gname"></span>';
      gh.querySelector(".gname").textContent = e.name;
      const gi = document.createElement("div");
      gi.className = "gitems";
      for (const it of e.items) gi.appendChild(itemEl(it));
      gh.onclick = () => {
        groupOpen[e.id] = closed;
        localStorage.setItem("cb.groups", JSON.stringify(groupOpen));
        renderTree();
      };
      tree.appendChild(gh); tree.appendChild(gi);
    } else {
      tree.appendChild(itemEl(e));
    }
  }
}

function markGuarded() { renderTree(); }

/* First list of a load (per harness): if the folders it would open hold
   more than AUTO_FOLD_ROWS sessions, start with all of them closed. Only
   in memory — the next load judges again from what the user left open. */
function autoFold(list) {
  if (!list || !list.length || autoFolded[backend]) return;
  autoFolded[backend] = true;
  let open = 0;
  for (const e of list)
    if (e.type === "group" &&
        !(e.id in groupOpen ? !groupOpen[e.id] : !!e.collapsed))
      open += e.items.length;
  if (open > AUTO_FOLD_ROWS)
    for (const e of list) if (e.type === "group") groupOpen[e.id] = false;
}

/* The period dial: a slim track with 1 · 7 · 30 (days) on it and a white
   knob you hold and drag between them. The knob runs on a spring: while
   held it trails the pointer a hair and is pulled toward the nearest
   stop like a detent; let go and it snaps there with a small overshoot.
   It stretches along its motion and squashes back as it lands, and the
   number inside rolls (odometer) as it crosses each stop. A plain click
   anywhere on the track springs it there; ← → / Home / End move it too.
   The period changes when the knob settles, not on every pixel. */
const TK_STOPS = ["day", "week", "month"];
const TK_X = [12, 40, 68];                   // stop centres, px on the track
const tk = { x: TK_X[1], v: 0, target: TK_X[1], held: false, raf: 0, tilt: 0,
  near: 1, grab: 0, moved: false, lastX: 0, lastT: 0, flick: 0 };

const tkIdx = () => Math.max(0, TK_STOPS.indexOf(tokPeriod));
const tkNearest = x => TK_X.reduce((b, c, i) => Math.abs(c - x) < Math.abs(TK_X[b] - x) ? i : b, 0);

function tkPaint() {
  const d = $("segTok");
  d.style.setProperty("--x", tk.x.toFixed(2) + "px");
  // stretch along the motion (speed), squash back when it lands
  const s = Math.min(.42, Math.abs(tk.v) / 1400);
  d.style.setProperty("--sx", (1 + s).toFixed(3));
  d.style.setProperty("--sy", (1 - s * .55).toFixed(3));
  // more days, more pages: the sheets behind fan out toward 30
  d.style.setProperty("--f", Math.max(0, Math.min(1, (tk.x - TK_X[0]) / (TK_X[2] - TK_X[0]))).toFixed(3));
  // the numbers under the knob hide; the others fade in with distance
  d.querySelectorAll(".tkstop").forEach((el, i) => {
    el.style.opacity = Math.min(1, Math.max(0, (Math.abs(TK_X[i] - tk.x) - 6) / 14)).toFixed(3);
  });
  const n = tkNearest(tk.x);
  if (n !== tk.near) {                       // crossed a stop: flip a page
    tkFlip(tk.near, n);
    tk.near = n;
  }
}

/* A new number, the way a desk calendar does it. Toward more days the
   top page lifts up off the binding and away, the new day already under
   it; toward fewer, a page swings down over the old one and lands with a
   little bounce. Dragging fast across stops just finishes the last flip
   first, so the pages never pile up. */
const TK_V = ["1", "7", "30"];
function tkFlip(from, to) {
  const win = $("segTok").querySelector(".tkwin"), num = win.querySelector(".tkday");
  for (const f of win.querySelectorAll(".tkflap"))
    for (const an of f.getAnimations()) an.finish();
  if (calm()) { num.textContent = TK_V[to]; return; }
  const fwd = to > from;
  const flap = document.createElement("i");
  flap.className = "tkflap";
  flap.innerHTML = "<b>" + TK_V[fwd ? from : to] + "</b>";
  if (fwd) num.textContent = TK_V[to];         // the new day is underneath
  win.appendChild(flap);
  const shut = { transform: "perspective(46px) rotateX(0deg)", filter: "brightness(1)" };
  // folds back toward the binding (away from you), so it shrinks up and
  // the day underneath shows; toward you it would grow and cover it
  const open = { transform: "perspective(46px) rotateX(-92deg)", filter: "brightness(.78)" };
  const an = flap.animate(fwd ? [shut, open] : [open, shut], fwd
    ? { duration: 230, easing: "cubic-bezier(.45,0,.9,.55)", fill: "forwards" }
    : { duration: 280, easing: "cubic-bezier(.2,.75,.35,1.25)", fill: "forwards" });
  an.onfinish = () => {
    if (!fwd) num.textContent = TK_V[to];
    flap.remove();
  };
}

function tkStep(now) {
  const dt = Math.min(.032, (now - (tk.t || now)) / 1000) || .016;
  tk.t = now;
  let goal = tk.target;
  if (tk.held) {                             // a detent: the stops pull a bit
    const n = TK_X[tkNearest(goal)], d = n - goal;
    goal += d * Math.max(0, 1 - Math.abs(d) / 9) * .55;
  }
  // held: stiff, close to the finger; released: softer, one small overshoot
  const k = tk.held ? 900 : 380, c = tk.held ? 48 : 22;
  tk.v += ((goal - tk.x) * k - tk.v * c) * dt;
  tk.x += tk.v * dt;
  tkPaint();
  if (tk.held || Math.abs(goal - tk.x) > .15 || Math.abs(tk.v) > 4) {
    tk.raf = requestAnimationFrame(tkStep);
  } else {
    tk.x = goal; tk.v = 0; tk.raf = 0; tk.t = 0;
    tkPaint();
  }
}

function tkRun() {
  if (calm()) { tk.x = tk.target; tk.v = 0; tkPaint(); return; }
  if (!tk.raf) { tk.t = 0; tk.raf = requestAnimationFrame(tkStep); }
}

// settle on stop i: the knob springs there, and the period follows
function tkGo(i) {
  i = Math.max(0, Math.min(2, i));
  tk.target = TK_X[i];
  tkRun();
  const p = TK_STOPS[i];
  if (p === tokPeriod) return;
  tokPeriod = p;
  localStorage.setItem("cb.tokPeriod", tokPeriod);
  tokSig = ""; tokAnim = true;
  tkSync();
  if (sessFolded) switchView("tok");           // a folded card opens
  if (S) renderTokens(S.usage);
}

// the period changed elsewhere (boot, a render): put the knob on it
function tkSync() {
  const d = $("segTok"), i = tkIdx();
  d.setAttribute("aria-valuenow", [1, 7, 30][i]);
  d.setAttribute("aria-valuetext", T(["Today", "7 days", "30 days"][i]));
  d.title = T("Days shown · drag or click");
  if (!tk.held && !tk.raf && tk.target !== TK_X[i]) {
    tk.target = tk.x = TK_X[i];
    tk.near = i;
    d.querySelector(".tkday").textContent = TK_V[i];
    tkPaint();
  }
}

function tkWire() {
  const d = $("segTok");
  const local = e => e.clientX - d.getBoundingClientRect().left;
  const clampX = x => Math.max(TK_X[0] - 1, Math.min(TK_X[2] + 1, x));
  d.addEventListener("pointerdown", e => {
    if (e.button !== 0) return;
    e.preventDefault();
    const x = local(e);
    tk.held = true;
    tk.moved = false;
    // grabbed the knob: keep the offset; elsewhere: it comes to the finger
    tk.grab = Math.abs(x - tk.x) < 12 ? x - tk.x : 0;
    tk.target = clampX(x - tk.grab);
    tk.lastX = x; tk.lastT = performance.now(); tk.flick = 0;
    d.classList.add("held");
    try { d.setPointerCapture(e.pointerId); } catch (err) { }
    tkRun();
  });
  d.addEventListener("pointermove", e => {
    if (!tk.held) return;
    const x = local(e), now = performance.now();
    if (Math.abs(x - tk.grab - tk.target) > 2) tk.moved = true;
    tk.flick = (x - tk.lastX) / Math.max(1, now - tk.lastT) * 1000;
    tk.lastX = x; tk.lastT = now;
    tk.target = clampX(x - tk.grab);
  });
  const up = e => {
    if (!tk.held) return;
    tk.held = false;
    d.classList.remove("held");
    try { d.releasePointerCapture(e.pointerId); } catch (err) { }
    // a flick carries on a little in its direction before picking a stop
    const aim = tk.moved ? tk.target + Math.max(-26, Math.min(26, tk.flick * .06)) : tk.target;
    tkGo(tkNearest(aim));
  };
  d.addEventListener("pointerup", up);
  d.addEventListener("pointercancel", up);
  d.addEventListener("keydown", e => {
    const i = tkIdx();
    const to = { ArrowLeft: i - 1, ArrowDown: i - 1, ArrowRight: i + 1, ArrowUp: i + 1,
      Home: 0, End: 2 }[e.key];
    if (to == null) return;
    e.preventDefault();
    tkGo(to);
  });
  tkSync();
}

function sessTotal() {
  let n = 0;
  for (const e of spaces || []) n += e.type === "group" ? e.items.length : 1;
  return n;
}

const hasTok = () => !$("toTok").hidden;     // this harness sends usage

// the drum: fold / unfold whichever face is showing
function toggleFold() {
  sessFolded = !sessFolded;
  localStorage.setItem("cb.sessFolded", sessFolded ? "1" : "0");
  $("tokTip").hidden = true;
  applyView();
}

/* The other face: the one showing rolls up and away, the drum turns, the
   new one rolls in from below (the same move as the names' roll). A
   folded card opens on the way. */
let rollT = 0;
function switchView(v) {
  if (v === view && !sessFolded) return;
  const card = $("sessCard");
  const was = sessFolded;
  view = v;
  sessFolded = false;
  localStorage.setItem("cb.view", view);
  localStorage.setItem("cb.sessFolded", "0");
  $("tokTip").hidden = true;
  if (v === "tok") { tokAnim = true; tokSig = ""; }
  clearTimeout(rollT);
  card.classList.remove("roll-in");
  if (was) { applyView(); rollIn(); return; }
  card.classList.add("roll-out");              // the face leaving
  rollT = setTimeout(() => {
    card.classList.remove("roll-out");
    applyView();
    rollIn();
  }, 190);
}

function rollIn() {
  const card = $("sessCard");
  if (view === "tok" && S) renderTokens(S.usage);
  card.getBoundingClientRect();
  card.classList.add("roll-in");
  // done: off again, so nothing later can replay it
  rollT = setTimeout(() => card.classList.remove("roll-in"), 520);
}

function applyView() {
  const v = hasTok() ? view : "sess";          // no usage: sessions
  const card = $("sessCard");
  card.classList.toggle("v-tok", v === "tok");
  card.classList.toggle("folded", sessFolded);
  $("drum").setAttribute("aria-expanded", sessFolded ? "false" : "true");
  $("drum").setAttribute("aria-label", (v === "tok" ? T("Token use") : T("Sessions")) +
    " · " + T(sessFolded ? "Expand" : "Collapse"));
  fitTok();
  updateSessN();
}

/* The sessions folder: one sheet sticking out per session (up to three),
   green while its cache is alive. Hovered they rise and fan; a refresh
   shuffles them. Drawn for both spots it lives in (the drum's sessions
   face and the way back from tokens), only when the counts change. */
let folderSig = "", warmCount = 0;
function folderSvg(total, warm) {
  const n = Math.min(3, total);
  // spread over the folder's width so even one or two read as sheets
  const pose = n === 1 ? [[-4, 0]] : n === 2 ? [[-10, -1.6], [8, 1.8]]
    : [[-13, -2.4], [0, 0], [12, 2.4]];          // [deg, x] per sheet
  let sheets = "";
  for (let i = 0; i < n; i++)
    sheets += '<g class="sh' + (i < warm ? " warm" : "") + '" style="--r:' +
      pose[i][0] + "deg;--x:" + pose[i][1] + "px;--i:" + i + '">' +
      '<rect x="5.6" y="2.8" width="6.8" height="8" rx="1"/>' +
      '<path d="M7.1 5.2h3.8M7.1 7.1h2.6"/></g>';   // two lines of "text"
  return '<svg class="fold3" viewBox="0 0 18 18" aria-hidden="true">' +
    '<path class="back" d="M2.2 6.4a1 1 0 0 1 1-1h3.2l1.3 1.4h7.1a1 1 0 0 1 1 1v6.4"/>' +
    sheets +
    // the pocket: its top edge dips in the middle, like a soft folder lip
    '<path class="front" d="M1.9 8.7c2.4 0 3.4.9 7.1.9s4.7-.9 7.1-.9l-.8 5.5a1 1 0 0 1-1 .9H3.7a1 1 0 0 1-1-.9z"/>' +
    "</svg>";
}

function renderFolders(total) {
  const warm = Math.min(warmCount, Math.min(3, total));
  const sig = Math.min(3, total) + ":" + warm;
  if (sig === folderSig) return;
  folderSig = sig;
  $("faceFolder").innerHTML = folderSvg(total, warm);
  $("backFolder").innerHTML = folderSvg(total, warm);
}

// folded, the sessions tab says how many sessions are behind it
function updateSessN() {
  const n = sessTotal();
  $("sessN").textContent = sessFolded && n &&
    !$("sessCard").classList.contains("v-tok") ? T("{0} total", n) : "";
  $("sessBadge").textContent = n ? (n > 99 ? "99+" : String(n)) : "";
  renderFolders(n);
  $("toSess").setAttribute("aria-label", T("Sessions") + (n ? " · " + n : ""));
}

async function refreshSpaces() {
  try {
    const list = await api().spaces();
    autoFold(list);
    spaces = list;
    renderTree();
    updateSessN();
  } catch (e) { /* not ready */ }
}

/* The panel window hugs its content when the sessions card is folded:
   with nothing left to stretch, the bottom edge comes up to the footer
   instead of leaving a blank stretch below it. Open, the sessions card
   takes the full height again. A popover that's showing (settings, the
   harness menu) only makes the window tall enough for itself — it
   almost always fits already. Python moves only the bottom edge, easing
   it over `ms` (a card's own fold animation passes its length, so the
   window and the cards move as one). */
let fitSent = -1, fitRaf = 0, fitHold = false;
function fitPanel(ms) {
  fitRaf = 0;
  if (fitHold || !document.body.classList.contains("v-panel")) return;
  let h = 0;                                   // 0 = the full height
  if (sessFolded && $("carryCard").hidden) {
    const panel = $("panel");
    const pr = panel.getBoundingClientRect();
    const pad = parseFloat(getComputedStyle(panel).paddingBottom) || 0;
    let bottom = 0;
    for (const el of panel.children) {
      const cs = getComputedStyle(el);
      if (el.hidden || cs.display === "none" || cs.position === "absolute") continue;
      bottom = Math.max(bottom, el.getBoundingClientRect().bottom);
    }
    for (const id of ["setPop", "hkMenu", "droidAcctMenu"])
                                               // room for an open popover
      if (!$(id).hidden) bottom = Math.max(bottom, $(id).getBoundingClientRect().bottom);
    h = Math.ceil(bottom - pr.top + pad);
  }
  if (h === fitSent) return;
  fitSent = h;
  try { api().panel_fit(h, typeof ms === "number" ? ms : 0); } catch (e) { }
}
const fitSoon = () => { if (!fitRaf) fitRaf = requestAnimationFrame(() => fitPanel()); };

/* ------------------------------------------------- launch splash */
// The logo is fixed over the column slot that holds its place (#splash
// ::before): while the window flies into the dock its offset from the
// centre shrinks with the window, so it ends centred in the capsule.
let spLeft = false, spDx = 0, spDy = 0;

function placeSplashLogo() {
  const sp = $("splash"), cs = getComputedStyle(sp);
  const logo = parseFloat(cs.getPropertyValue("--sp-logo")) || 76;
  const r = sp.getBoundingClientRect();
  // column content starts after padding; with justify-content:center the
  // slot sits half the leftover space lower
  const kids = [...sp.children].filter(el => el.id !== "spLogo" &&
                                       getComputedStyle(el).display !== "none");
  const used = logo + kids.reduce((n, el) => n + el.getBoundingClientRect().height +
                                  parseFloat(getComputedStyle(el).marginTop), 0);
  const padT = parseFloat(cs.paddingTop), padB = parseFloat(cs.paddingBottom);
  const free = Math.max(0, r.height - padT - padB - used);
  const top = padT + (cs.justifyContent === "center" ? free / 2 : 0);
  spDx = 0;
  spDy = top + logo / 2 - r.height / 2;
  sp.style.setProperty("--sp-dx", spDx + "px");
  sp.style.setProperty("--sp-dy", spDy + "px");
}

function leaveSplash() {
  if (spLeft || !document.body.classList.contains("v-splash")) return;
  spLeft = true;
  document.body.classList.add("sp-leave");
  const h0 = innerHeight, sp = $("splash");
  const follow = () => {               // offset ∝ window height -> centre
    if (!document.body.classList.contains("v-splash")) return;
    const k = Math.min(1, innerHeight / h0);
    sp.style.setProperty("--sp-dy", spDy * k + "px");
    requestAnimationFrame(follow);
  };
  setTimeout(() => {                   // the text is out of the way first
    requestAnimationFrame(follow);
    try { api().splash_done(); } catch (e) { }
  }, 170);
}

async function bootSplash() {
  let info = null;
  try { info = await api().splash_info(); } catch (e) { }
  if (!info || !info.splash) return;
  lang = info.lang === "en" ? "en" : "zh";
  applyLang();
  document.body.classList.toggle("first", !!info.first);
  placeSplashLogo();
  // two frames: the splash is laid out and painted before the window
  // (alpha 0 until now) shows it — then the animations and timers start
  await new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)));
  document.body.classList.add("sp-on");
  try { api().splash_shown(); } catch (e) { }
  if (info.first) {
    $("spGo").onclick = leaveSplash;
    setTimeout(() => $("spGo").focus({ preventScroll: true }), 400);
  } else {
    setTimeout(() => { $("splash").querySelector(".spsub").textContent = T("Ready");
                       $("splash").querySelector(".spsub").classList.add("ok"); }, 900);
    setTimeout(leaveSplash, 1350);
  }
  document.addEventListener("keydown", e => {
    if (e.key === "Escape" || (e.key === "Enter" && !info.first)) leaveSplash();
  });
}

/* ------------------------------------------------- view (from py) */
window.setView = function (view, side) {
  document.body.classList.remove("v-splash", "v-dock", "v-peek", "v-panel");
  if (view !== "splash") document.body.classList.remove("sp-leave", "sp-on", "first");
  document.body.classList.add("v-" + view);
  document.body.classList.toggle("left", side === "left");
  curSide = side || "right";
  Mascots.setSide(curSide);
  if (view === "panel") { refreshSpaces(); fitSent = -1; fitSoon(); }  // refresh on open
  if (S) updateFrame(S);
};

/* ------------------------------------------------- boot */
// drag starts on mousedown: Python polls GetCursorPos itself and applies
// a >4px dead-zone, so a fast flick (e.g. upward, where the pointer leaves
// the webview before mousemove can fire) still picks up the drag, while a
// plain press stays a click. Waiting for JS mousemove first was what made
// upward drags stall.
function dragZone(el) {
  el.addEventListener("mousedown", e => {
    if (e.button !== 0 || e.target.closest("button")) return;
    try { api().drag_start(); } catch (err) { }
  });
}

window.addEventListener("pywebviewready", () => {
  api().sync_view();   // re-pull view+side in case an early setView dropped
  bootSplash();

  dragZone($("head"));
  dragZone($("peek"));
  $("peek").addEventListener("dblclick", () => api().open_panel());

  // ⚙ settings: language + 缓存维护时间 (how long a session is kept warm
  // after its last real turn — the backend stops guarding after that)
  const showSettings = st => {
    if (!st) return;
    lang = st.lang === "en" ? "en" : "zh";
    applyLang();
    for (const b of $("segMaint").children)
      b.classList.toggle("on", Number(b.dataset.min) === st.maint_min);
    heatSig = ""; spaces && renderTree(); if (S) render();
  };
  const setPop = open => {
    $("setPop").hidden = !open;
    $("btnSet").setAttribute("aria-expanded", open);
  };
  Promise.resolve(api().settings()).then(showSettings).catch(() => { });
  $("btnSet").onclick = e => { e.stopPropagation(); setPop($("setPop").hidden); };
  $("setPop").onclick = e => e.stopPropagation();
  for (const b of $("segLang").children)
    b.onclick = async () => showSettings(await api().set_setting("lang", b.dataset.lang));
  for (const b of $("segMaint").children)
    b.onclick = async () => showSettings(
      await api().set_setting("maint_min", Number(b.dataset.min)));
  document.addEventListener("click", () => setPop(false));
  document.addEventListener("keydown", e => { if (e.key === "Escape") setPop(false); });

  $("fixBtn").onclick = e => { e.stopPropagation(); factoryFix(); };
  $("btnMin").onclick = () => api().collapse();
  $("btnX").onclick = () => api().quit();
  $("btnExit").onclick = () => api().quit();
  // ⟳ also re-checks for CLIs opened before Buddy (the 30s refresh doesn't)
  $("btnRefresh").onclick = async () => {
    $("lockNote").hidden = true;
    setBusy("sess", true);
    // the names roll up and back (ROLL_MS): start the window, then redraw
    // so the rows build with their offsets. A rescan reads local files and
    // can be back before the eye registers it, so the spin is held for at
    // least the roll, whatever the scan costs.
    rollAt = Date.now();
    renderTree();
    const floor = new Promise(r => setTimeout(r, ROLL_MS + 120));
    try { spaces = await api().rescan(); autoFold(spaces); renderTree(); updateSessN(); }
    catch (e) { }
    await floor;
    rollAt = 0;
    renderTree();                       // back to plain text
    setBusy("sess", false);
  };
  $("lockNote").onclick = () => { $("lockNote").hidden = true; };
  // a picked row (grey) is for the next press of the guard buttons, not
  // forever: a click anywhere else on the panel lets it go
  document.addEventListener("click", e => {
    if (!sel || e.target.closest(".track, .sitem, .cball, #controls, #rowMenu, #acctMenu, #carryCard"))
      return;
    sel = null;
    localStorage.removeItem(selKey());
    for (const r of document.querySelectorAll(".sitem.sel")) r.classList.remove("sel");
    render();
  });
  // the row menu / carry card close on a click elsewhere or Escape
  document.addEventListener("click", e => {
    if (!e.target.closest("#rowMenu, #acctMenu")) closeRowMenu();
  });
  document.addEventListener("keydown", e => {
    if (e.key === "Escape") { closeRowMenu(); closeCarry(); }
  });
  $("carryScrim").onclick = () => {          // one scrim, two cards
    if (acctLock) return;                    // add-step 2 ignores it
    closeCarry();
    closeAcct();
  };
  $("tree").addEventListener("scroll", closeRowMenu);
  applyView();
  $("drum").onclick = toggleFold;
  $("toTok").onclick = () => switchView("tok");
  $("toSess").onclick = () => switchView("sess");
  // the slot changes with the cards above it (cache folded / more tracks)
  new ResizeObserver(fitTok).observe($("sessCard"));
  tkWire();                                    // the period dial
  $("tokChart").addEventListener("mousemove", tokColTip);
  $("tokChart").addEventListener("mouseleave", () => { $("tokTip").hidden = true; });
  setFolded(cacheFolded);
  $("cacheHead").onclick = () => setFolded(!cacheFolded);
  $("cacheHead").onkeydown = e => {
    if (e.key === " " || e.key === "Enter") { e.preventDefault(); setFolded(!cacheFolded); }
  };
  setQuotaFolded(quotaFolded);
  // the title folds/unfolds; folded, a click on the line skips ahead
  for (const b of $("segPool").querySelectorAll("button"))
    b.onclick = e => {
      e.stopPropagation();                    // never a card fold
      if (b.dataset.pool === quotaPool) return;
      const toCore = b.dataset.pool === "c";
      quotaPool = b.dataset.pool;
      localStorage.setItem("cb.quotaPool", quotaPool);
      // the droplet: squash once, stretching toward where it's going
      const th = $("poolThumb");
      th.style.transformOrigin = toCore ? "left center" : "right center";
      th.classList.remove("squash");
      void th.offsetWidth;
      th.classList.add("squash");
      for (const x of $("segPool").querySelectorAll("button"))
        x.classList.toggle("on", x === b);
      movePoolThumb();
      // the new pool's rings slide in from the toggle's direction
      const rows = $("quotaRows");
      rows.classList.remove("slide-r", "slide-l");
      void rows.offsetWidth;
      rows.classList.add(toCore ? "slide-r" : "slide-l");
      if (S && S.quota) renderQuota(S.quota);
      setTimeout(() => rows.classList.remove("slide-r", "slide-l"), 300);
    };
  setSaveFolded(saveFolded);
  $("saveHead").onclick = () => setSaveFolded(!saveFolded, true);
  $("saveHead").onkeydown = e => {
    if (e.key === " " || e.key === "Enter") { e.preventDefault(); setSaveFolded(!saveFolded, true); }
  };
  $("quotaHead").onclick = () => setQuotaFolded(!quotaFolded, true);
  $("quotaHead").onkeydown = e => {
    if (e.key === " " || e.key === "Enter") { e.preventDefault(); setQuotaFolded(!quotaFolded, true); }
  };
  const qt = $("quotaTick");
  qt.onclick = () => { stepTick(); armTick(); };
  qt.onmouseenter = () => { tickHover = true; };
  qt.onmouseleave = () => { tickHover = false; };
  armTick();
  // the window's height follows the cards (see fitPanel): any of them
  // changing size, folding, or a popover opening re-measures
  const ro = new ResizeObserver(fitSoon);
  for (const el of $("panel").children) ro.observe(el);
  const mo = new MutationObserver(fitSoon);
  for (const el of [...$("panel").children, $("setPop"), $("hkMenu")])
    mo.observe(el, { attributes: true, attributeFilter: ["hidden", "class"] });
  fitSoon();
  $("btnQuota").onclick = async () => {
    quotaAsked = Date.now() / 1000;
    setBusy("quota", true);
    try { await api().quota_refresh(); }
    catch (e) { setBusy("quota", false); }
    setTimeout(poll, 2000);            // the answer lands in a second or two
  };

  $("btnGuard").onclick = () => {
    if (!S) return;
    const act = guardAction(S);
    if (act.disabled) return;
    guardPressAt = Date.now();
    if (act.kind === "start") {
      const s0 = findSession(sel);
      if (!(s0 && s0.locked)) guardLaunch(sel);   // locked: the note says why
      startGuard(sel);
    }
    else if (act.kind === "stop_one") { stopFlourish([sel]); api().stop_one(sel); }
    else {
      stopFlourish(S.guarded || (S.target ? [S.target.sid] : []));
      api().stop();
    }
  };
  $("btnNow").onclick = () => { if (!$("btnNow").disabled) renewFlourish(); api().inject(); };
  addEventListener("resize", moveIvThumb);
  for (const b of $("controls").querySelectorAll("button"))
    b.addEventListener("pointerdown", e => pressRipple(e, b));
  for (const b of $("hkMenu").children) {
    b.querySelector(".hico").innerHTML = HARNESS[b.dataset.bk].icon();
    b.onclick = () => switchBackend(b.dataset.bk);
    // hovering Droid opens its accounts; hovering anything else folds it
    if (b.dataset.bk === "droid")
      b.onmouseenter = () => openDroidAccts(b);
    else
      b.onmouseenter = () => { $("droidAcctMenu").hidden = true; };
  }
  $("hkBtn").onclick = e => { e.stopPropagation(); setMenu($("hkMenu").hidden); };
  $("mPrev").onclick = () => gotoMonth(shiftMonth(heatMonth, -1));
  $("mNext").onclick = () => gotoMonth(shiftMonth(heatMonth, 1));
  $("heat").addEventListener("mousemove", heatTip);
  $("heat").addEventListener("mouseleave", () => { $("heatTip").hidden = true; });
  document.addEventListener("click", e => {
    if (!e.target.closest("#hk")) setMenu(false);
  });
  document.addEventListener("keydown", e => {
    if (e.key === "Escape") {
      setMenu(false);
      $("droidAcctMenu").hidden = true;
      if (!acctLock && !$("acctCard").hidden) closeAcct();
    }
  });
  for (const b of $("segIv").querySelectorAll("button"))
    b.onclick = () => api().set_interval(Number(b.dataset.iv));

  poll(); setInterval(poll, 1000);
  initEcg(); ecgFrame();
  refreshSpaces(); setInterval(refreshSpaces, 30000);
  setInterval(() => {
    if (dueLeft != null && dueLeft > 0) { dueLeft--; renderStatus(); }
    let ticked = false;
    for (let i = 0; i < cacheLefts.length; i++)
      if (cacheLefts[i] != null && cacheLefts[i] > 0) {
        cacheLefts[i]--; ticked = true;
      }
    if (ticked) { updateBars(); updateRings(); updateFoldRings(); }
    updatePeekLabel();
    Mascots.setDue(dueLeft);
  }, 1000);
});
