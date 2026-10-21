# Cache Buddy — setup notes for an AI assistant

You are helping someone set up **Cache Buddy** on Windows. The person uses **Devin**
(Devin Desktop and/or the Devin CLI). These notes say what it needs, how to check
each piece, and how to fix the usual problems. Run the checks yourself where you can
(PowerShell), and tell the person in plain words what you found.

**Never print, log or paste a credential.** Two files below hold secrets
(`credentials.toml`, and the Factory API key if they use Droid). Only test whether
they *exist* or *have a key*. Don't show their contents.

## What it is

A small panel docked to the right screen edge (`CacheBuddy.exe`, no install). AI coding
agents keep a prompt cache that expires after ~5 minutes of idling; the next message
then pays full price to re-read the whole context. Cache Buddy sends a tiny heartbeat
before the cache expires, so coming back later stays cheap. The panel also shows:

- **Plan quota** (Devin): today's and this week's usage as two rings, with reset times.
- **Token use per model**: click the little 7-column chart next to the "Sessions" title.

## Requirements

1. Windows 10/11, 64-bit.
2. **Microsoft Edge WebView2 Runtime.** Windows 11 has it. Check:
   ```powershell
   Get-ItemProperty 'HKLM:\SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}' -ErrorAction SilentlyContinue | Select-Object pv
   ```
   No output → install "WebView2 Runtime (Evergreen)" from Microsoft's site.
3. Devin installed. The Devin tab only appears if this file exists:
   `%APPDATA%\devin\cli\sessions.db` (created by Devin Desktop or the CLI after the first session).

## Starting it

Double-click `CacheBuddy.exe`. On "Windows protected your PC" (the exe is not code-signed):
**More info → Run anyway**. Then pick **Devin** with the icon right of the "Cache Buddy" title,
select a session in the list, and press **Start guard**.

## Devin plan quota — how it gets the numbers

No setup inside Cache Buddy. It reads the key the Devin CLI saved when the person logged in,
and asks Devin's server for the plan status every 5 minutes (the ⟳ on the quota card refreshes now).

**The one requirement: log in with the Devin CLI once.** Devin Desktop alone may not create the file.

1. Check whether the login file exists (does not print it):
   ```powershell
   Test-Path "$env:APPDATA\devin\credentials.toml"
   ```
2. If `False`, or the quota card says **"Not signed in to Devin"** / **"Devin's sign-in expired"**,
   have the person run (it opens a browser to sign in):
   ```powershell
   & "$env:LOCALAPPDATA\devin\cli\bin\devin.exe" auth login
   ```
   If `devin` is on PATH, `devin auth login` works too. Check with `devin auth status`.
   If the CLI is missing, install the Devin CLI from Devin's site first.
3. Back in the panel, press ⟳ on the quota card. The numbers land in a second or two.

Other messages on the card:
- **"Couldn't reach the server · showing the last numbers"**: network problem; it retries on its own.
  The last good numbers are cached (numbers only, no keys) in `%USERPROFILE%\.claude\cache-buddy\quota-cache.json`.
- The card only shows when the **Devin** (or Droid) tab is selected. Claude Code has no plan card.

## What it changes while open (restored on quit)

- **Devin CLI:** sets the user env var `WINDSURF_API_SERVER_URL=http://127.0.0.1:47822` (a local
  pass-through, 127.0.0.1 only) so CLI turns can be renewed. Only terminals opened *after* Buddy
  started pick it up. A session in a terminal opened earlier shows a **lock** icon: close that
  terminal, open a new one, `devin -r <session id>`, then press ⟳ on the Sessions card.
- **Claude Code** (if installed): points `ANTHROPIC_BASE_URL` in `~/.claude/settings.json` at
  `127.0.0.1:47821` (backup: `settings.json.bak-cache-buddy`).
- Quit / crash / shutdown all restore both. After a hard power cut, if a CLI says it can't connect,
  start Cache Buddy once and press **Quit**.

## Devin Desktop heartbeats (optional)

For Desktop sessions the heartbeat is typed into the Devin window. To do that in the background
without pulling Devin to the front, Devin needs its debug port: add
`"remote-debugging-port": "9222"` (the value **must be a string**, in quotes) to
`%USERPROFILE%\.devin\argv.json`, then restart Devin. Without it the footer shows **CDP** grey and
heartbeats fall back to UI automation, which briefly steals focus. Note: with the port open, other
programs on this PC can also drive the Devin window — ask the person before enabling it.

## Factory Desktop (Droid) heartbeats (optional)

Sessions last driven by **Factory Desktop** (the Droid app) are renewed by typing the heartbeat
into that session's composer in the open Factory window over CDP — never through `droid exec`,
which uses a different tool set and would miss Desktop's cached prefix. For this, Factory needs
its debug port: append ` --remote-debugging-port=9223` to the Factory desktop shortcut's target
(e.g. `C:\Users\<you>\Desktop\Factory.lnk` → `...\Factory\factory-desktop.exe
--remote-debugging-port=9223`). The Squirrel stub should forward the argument; if it doesn't,
launch `%LOCALAPPDATA%\Factory\app-<ver>\factory-desktop.exe --remote-debugging-port=9223`
directly instead. Without the port the session is held ("Factory Desktop needs its debug port").
Same security note as above: with the port open, other programs on this PC can drive the Factory
window — ask the person before enabling it. There is no UI-automation fallback for Factory.

## Where things are

- Log: `%USERPROFILE%\.claude\cache-buddy\buddy.log` (first place to look when something's off;
  lines like `quota devin: no_login` explain the quota card).
- Settings / savings ledger: same folder, and `%APPDATA%\devin\cache-sentinel\`.
- Uninstall: quit, delete `CacheBuddy.exe` and those two folders.

## Costs, privacy

Each heartbeat costs about one cached read; the panel's "saved" figures already subtract it.
Token/money figures are estimates at official list prices, not the plan's actual billing.
Keys are used in memory only. Nothing is uploaded anywhere except the same API calls the
agent itself makes.
