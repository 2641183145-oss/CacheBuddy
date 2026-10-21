# Devin Cache Buddy

Windows 守护面板：对选中的 Devin 会话发送后台心跳，保住 prompt cache（5 分钟 TTL），不抢焦点、不建新会话。

## 环境（新机器）

- Windows 11 + Python 3.13
- Devin Desktop 安装并登录，CDP 调试端口 `9222` 可用
- 依赖：`pip install pywebview uiautomation websocket-client pillow pyinstaller`

## 运行

```bash
python cache_buddy.py            # 面板（默认 4.5min 档，面板内可切 15s）
python cache_buddy.py --idle 15  # 指定心跳间隔（秒）
python cache_buddy.py --tk       # Tk 后备界面
```

## 结构

- `cache_buddy.py` — 全部后端：SQLite 读会话/缓存指标、CDP 注入、UIA 回退、pywebview 窗口、停靠/拖拽
- `ui/` — 面板前端（app.js / app.css / mascots.js），`design.html` 为吉祥物设计稿（`python -m http.server 8765` 于 ui/ 下启动）

## 运行时文件

- 日志/控制文件：`%APPDATA%\devin\cache-sentinel\buddy.log` / `buddy.ctl`
- 临时保管的图片附件：`%APPDATA%\devin\cache-sentinel\held-attachments`
- Devin 数据：`%APPDATA%\devin\cli\sessions.db`、`%APPDATA%\devin\User\globalStorage\state.vscdb`

窗口标题固定为 `Cache Buddy`（不带 Devin，避免被会话查找误认）；demo 模式 `BUDDY_UI_DEMO=1` 用独立标题和控制文件。

## Devin CLI 会话（`devb`）

终端里跑的 Devin CLI 没有窗口可以打字，改走代理（`devin_cli.py`，和 Claude Code 版同一个思路）：

- 面板打开时把用户环境变量 `WINDSURF_API_SERVER_URL` 设为 `http://127.0.0.1:47822`（原值记在 `%APPDATA%\devin\cache-sentinel\route.json`），Quit 时还原；崩溃 / 结束进程 / 关机由看门狗还原，`python cc_buddy.py --heal` 也会一起修。之后**新开的终端**里直接敲 `devin` 就走代理；Buddy 打开前就开着的终端拿不到，要新开一个
- Devin Desktop 如果在面板打开期间启动，它的 agent 可能也走这个代理（它启动的是同一个 devin.exe）
- `devb [devin 参数...]`：在旧终端里用，或面板没开时先拉起 Buddy 再启动 devin
- Buddy 打开时（以及点会话列表的 ⟳ 时）检查一次：正开着、但所在终端没拿到这个变量的 CLI 会话（终端是 Buddy 打开前开的），在列表里用 🔒 代替心形，悬停或点 Start guard 会提示「关掉终端、新开一个、`devin -r <id>`，再点 ⟳」。靠的是 `session_locks/<id>.lock` 被哪个进程占着（Restart Manager，只查询）和那个进程自己的 `WINDSURF_API_SERVER_URL`；Desktop 会话不受影响（它在窗口里打字续期，不靠这个变量）
- 代理记住每个会话最后一次 `GetChatMessage`；快到期时在最后一条消息后面追加一条心跳用户消息再发一次，读到结束帧。前缀不变 → 缓存被读取、TTL 续上。心跳不经过 CLI，也不写进 sessions.db
- 面板里 CLI 会话带 `CLI` 标签，Start guard 后和 Desktop 会话一样调度；SWE 模型也守护
- 会话没走代理、或 Buddy 重启后还没有新的一轮 → 显示 `waiting for its next message (run it via devb)`
- Buddy 退出后，走代理开着的 CLI 会连不上（devin 不会热加载，地址在它启动时就定了），重新打开 Buddy 就恢复
- 上游默认 `https://server.codeium.com`，可用 `DEVIN_BUDDY_UPSTREAM` 覆盖；凭据只在内存里，不写日志、不落盘，只监听 127.0.0.1

---

# Claude Code 版（`cc_buddy.py`）

同一个面板，守护 Claude Code CLI 会话的 prompt cache（默认 5 分钟 TTL）。

CLI 没有可以注入文字的输入框，所以换了个思路：本地起一个透传代理（`ANTHROPIC_BASE_URL` → `127.0.0.1:47821` → 你原来的上游），记住每个会话最后一次请求；快到期时由 buddy 自己把这条请求重放一次，并在最后一个 cache 断点**之后**追加一行心跳说明、要求回一个字符。缓存前缀逐字节不变 → 被读取、TTL 续上。

心跳不经过 Claude Code：不进对话上下文、不写 transcript、不碰终端，所以你在 CLI 里往上翻历史时不会被拉到底部。

## 安装

```bat
pip install pywebview uiautomation websocket-client pillow
powershell -NoProfile -ExecutionPolicy Bypass -File install.ps1
```

`install.ps1` 在桌面放一个 `Cache Buddy` 快捷方式（Ctrl+Alt+B），并把本目录加进用户 PATH（`ccb`）。

## 使用

双击 `Cache Buddy` → 选会话 → Start guard。**可以同时守护多个会话**：在会话树里再选一个、再点 Start guard 就行。

- 缓存卡片列出**所有缓存还活着的会话**：守护中的排前面，没守护的显示成灰色，双击可以加入守护；全部停止后，上一轮守护过的会话即使缓存失效了也保留在卡片里。每行：模型图标、会话名、token 数、模型 / 思考强度 / 每轮成本、缓存剩余条。点一行就选中它，Start/Stop 按钮作用在选中的会话上
- 下面的会话列表显示目录里的全部会话，标题旁的 `N cached` 是现在有缓存的会话数。时间是**会话最后一次回复**的时间（打开会话、打字都不算）；每 14 秒滚动一次，显示几秒这个会话的上下文大小，超过 600K 变黄、超过 800K 变红（快要自动压缩了）
- 鼠标移到某行上会出现 `×`，点它只停这一个会话的守护；底部的 Stop 停全部
- 心跳真的救回一轮时（离开超过 5 分钟、缓存还在、这一轮命中了缓存），收起的面板会自动弹出来一次并停 7 秒，ECG 下方显示 `+$x.xx`
- 隐藏态 / 半隐藏态是胶囊形（两头圆、离屏幕边缘几像素悬浮），没有描边和发光。胶囊边缘的抗锯齿由 `edge_halo.py` 画：面板窗口本身只能用 1-bit 的窗口裁剪（会有锯齿），所以裁剪往里缩 2px，后面垫一个鼠标穿透的逐像素透明窗口，只画外圈那 1–3px 的平滑边，里面的内容不变。半隐藏态：顶部是同一个 ECG，下面是正在守护的会话的缓存倒计时，再下面每个会话一个带环的模型图标，环 = 该会话缓存剩余
- 重启 Cache Buddy 后，代理内存里的请求清空（凭据不落盘），被守护的会话要等它**下一次发消息**才能重新续期，期间显示 `Waiting for its next message`
- 面板打开时把 `~/.claude/settings.json` 的 `ANTHROPIC_BASE_URL` 指向代理（原值备份在 `settings.json.bak-cache-buddy`，并记在 `config.json`），Quit 时还原。Claude Code 会热加载 settings.json，**已经开着的会话下一次请求就会走代理**，不用重启
- 标题右边的图标 = 当前 harness（Claude Code / Devin），点开下拉切换；两边的守护互不影响，各自在后台跑
- 模型图标：Claude 是 Clawd；单独用 SWE 的 Devin 会话是抱电脑的小水獭（Cognition 的吉祥物是水獭，这只是本项目自己画的，不是官方素材）；Fusion 会话里的 SWE 一半仍用 Devin 的几何 logo。小水獭和 Clawd 一样有表情、会跳，几何 logo 只会转
- 会话树：实心绿点 = 走了代理、可续期；空心圈 = 进程在跑但还没走代理
- `--no-route`：不改 settings.json，只能靠 `ccb` 单次路由
- 面板没开时 settings.json 不会指向代理。buddy 没来得及正常退出时，也会自动还原：
  - 崩溃 / 任务管理器结束进程：一个很小的看门狗进程（`--watchdog`）会在 1 秒内把地址改回去
  - 关机 / 注销 / 重启：看门狗收到系统的关机通知，当场还原
- 安全：只监听 127.0.0.1；凭据原样转发，只留在内存里用于重放，不写日志、不落盘

## 注意（Claude Code CLI 用户）

Cache Buddy 打开期间，`~/.claude/settings.json` 里的 `ANTHROPIC_BASE_URL` 是 `http://127.0.0.1:47821`（本机代理），**所有** Claude Code 会话都经过它。正常退出（Quit）、崩溃、结束进程、关机都会自动还原；但在**突然断电、蓝屏**这类连看门狗都来不及动作的情况下，地址会停在 `127.0.0.1:47821`，而 buddy 已经不在了。

症状：Claude Code 每次请求都报连接错误（`ECONNREFUSED` / `Unable to connect to API`），而网络和账号都正常。

修复，任选一个：
- 重新打开 Cache Buddy（它会接管这个地址，Quit 时还原）
- 运行 `python cc_buddy.py --heal`：buddy 没在跑就把原地址写回去，在跑就什么都不做
- 手动：把 `settings.json` 里的 `ANTHROPIC_BASE_URL` 改回你原来的地址（原文件备份在 `~/.claude/settings.json.bak-cache-buddy`，原地址也记在 `~/.claude/cache-buddy/config.json`）

## 行为

- 只续期主线程（带 Agent 工具的那条 lane），subagent / 标题生成等请求不管
- 每个会话各自调度：谁快过期谁先发心跳，互不影响
- 缓存一旦过期就不再发心跳（Renew now 也不发），守护保留，等用户下一次发消息后自动接着守护。Devin 版同样如此
- 最近一轮请求还在进行中 → 不发；claude 进程已退出 → 暂停（`Held · session not running`）
- 超过「缓存维护时间」没有真实对话 → 这个会话自动停止守护，其他会话继续（Devin 版同样）。在右上角 ⚙ 里选 15 分钟 / 半小时（默认）/ 45 分钟；⚙ 里还能切换中文 / English。设置存在 `~/.claude/cache-buddy/settings.json`，两个后端共用
- 心跳成本 ≈ 前缀 × 0.1 倍输入价 + 一小段 thinking/输出；面板上的 Saved 已扣掉心跳本身的花费
- 价格表在 `_PRICES`，可在 `config.json` 里用 `{"prices": {"model": [in, out]}}` 覆盖

## 文件

- `~/.claude/cache-buddy/`：`buddy.log`、`buddy.ctl`（`stop [sid]` / `inject [sid]` / `start <标题或 sid 前缀>` / `view dock|peek|panel` / `quit`）、`config.json`（记住真实上游）、`ui.json`、`ledger.json`（按天的省钱账本）
- `ui/cc_demo.html`：不装 pywebview 预览 Claude Code 面板（`?mode=guarding|held|loading`）
