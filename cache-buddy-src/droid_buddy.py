# -*- coding: utf-8 -*-
"""Droid (Factory) harness for Cache Buddy: session list + heartbeat.

Sessions live in ~/.factory/sessions/<cwd-slug>/<sid>.jsonl (append-only
transcript) next to <sid>.settings.json (model, lastCallTokenUsage).
sessions-index.json lists only some of them — the directories are the truth.

Heartbeat = `droid exec -s <sid>` with one extra user message, run from the
session's cwd. No -m/-r/--auto: the session's own model, effort and
autonomy apply (its settings file is unchanged afterwards) and the request
keeps the same prefix, so it is read from the prompt cache (exec after
exec: 7479 of 7523 prompt tokens read). Not verified yet: a session last
driven by the TUI sharing that prefix with exec.

Sessions whose newest reply came from Factory Desktop take the CDP path
instead (send_desktop_heartbeat below): exec's tool set differs from
Desktop's, so an exec heartbeat misses Desktop's cached prefix. With
Factory started via --remote-debugging-port=9223, the heartbeat is
typed into that session's composer in the open window — hash-routed to
it and back, draft and image attachments pulled off and restored, and
the transcript's scroll position held while the reply lands.

Money: tokens x the official per-token list prices in cache_buddy's table
(Devin's), so both harnesses count the same way. That is the provider
price, not the Factory credits a plan is charged.

Transcript lines (learned from real sessions):
  session_start       {id, title, cwd, ...}
  message             {id, parentId, timestamp, message: {role, content,
                       modelId?, userMessageSource?}} — "context-…" ids are
                       the system-reminder block sent ahead of a prompt
  agent_turn_outcome  {turnId, reason, resultKind} — no timestamp; the file
                       is append-only, so order alone says a turn ended
Replies sent from Factory Desktop carry userMessageSource "desktop";
exec/CLI ones carry none.

Accounts: session_start.organizationId ties a session to the account that
wrote it, and droid lists and loads only the signed-in account's (plus
ones with no organization). All accounts share one sessions folder, so the
panel applies the same rule; the signed-in account is the orgId in
org-managed-settings.cache.json, which droid rewrites when it loads an
account's policy — a switch shows up there while Buddy runs.
"""
import json
import os
import shutil
import subprocess
import threading
import time
from datetime import datetime

import cache_buddy as cb

FACTORY_DIR = os.path.join(os.path.expanduser("~"), ".factory")
SESS_DIR = os.path.join(FACTORY_DIR, "sessions")
INDEX_FILE = os.path.join(FACTORY_DIR, "sessions-index.json")
ORG_FILE = os.path.join(FACTORY_DIR, "org-managed-settings.cache.json")
HB_TEXT = ("automated heartbeat from the local cache-sentinel plugin; it only "
           "keeps the prompt cache warm and needs no action; reply with "
           "exactly one character: .")
HB_MARK = cb.HEARTBEAT_MARK
TAIL = 256 * 1024          # first look at a big transcript: its tail only
EXEC_TIMEOUT = 90


def droid_exe():
    """droid.exe — PATH first; a Buddy started from Explorer doesn't get
    the ~/bin a shell adds, so the installer's spots are tried too."""
    p = shutil.which("droid")
    if p:
        return p
    for d in ("bin", os.path.join(".local", "bin"),
              os.path.join(".factory", "bin")):
        c = os.path.join(os.path.expanduser("~"), d, "droid.exe")
        if os.path.isfile(c):
            return c
    return None


def _ts(s):
    """ISO 'YYYY-MM-DDTHH:MM:SS.mmmZ' -> epoch seconds, or None."""
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except (AttributeError, TypeError, ValueError):
        return None


# ------------------------------------------------------------ transcript

class _Parse:
    """What the scheduler needs from one transcript, fed line by line."""

    def __init__(self):
        self.title = self.cwd = None
        self.org = None          # session_start.organizationId (the account)
        self.busy = False        # a prompt went out, no outcome line yet
        self.refresh_ts = None   # newest request start: any user-role line
        self.real_ts = None      # newest real prompt / tool result
        self.reply_ts = None     # newest real (non-heartbeat) reply
        self.answer_ts = None    # newest reply of any kind
        self.replies = 0
        self.model = None        # modelId of the newest reply
        self.desktop = False     # some reply came from Factory Desktop
        self._hb = False         # the turn running now answers a heartbeat

    def feed(self, d):
        t = d.get("type")
        if t == "session_start":
            self.title = d.get("title") or self.title
            self.cwd = d.get("cwd") or self.cwd
            # as written now: session-bridge's account move rewrites it
            self.org = d.get("organizationId") or None
            return
        if t == "agent_turn_outcome":
            self.busy = False
            return
        if t != "message":
            return
        m = d.get("message") or {}
        ts = _ts(d.get("timestamp"))
        role = m.get("role")
        # a model-only user line is never a request by itself: droid's own
        # reminders ride ahead of the real prompt, and session-bridge's
        # "carried over from …" note is written alone at the end of a carried
        # session — counting it showed a 5-minute cache that no request made
        if role == "user" and m.get("visibility") == "llm_only":
            return
        if role == "user":
            if ts:
                self.refresh_ts = max(self.refresh_ts or 0, ts)
            if str(d.get("id", "")).startswith("context-"):
                return           # reminders ahead of a prompt: not the prompt
            c = m.get("content")
            texts = ([x.get("text") or "" for x in c
                      if isinstance(x, dict) and x.get("type") == "text"]
                     if isinstance(c, list) else [str(c or "")])
            if texts:            # a typed prompt (tool results carry none)
                self._hb = any(HB_MARK in x for x in texts)
            self.busy = True
            if ts and not self._hb:
                self.real_ts = max(self.real_ts or 0, ts)
        elif role == "assistant":
            self.replies += 1
            self.model = m.get("modelId") or self.model
            if m.get("userMessageSource") == "desktop":
                self.desktop = True
            if ts:
                self.answer_ts = max(self.answer_ts or 0, ts)
                if not self._hb:
                    self.reply_ts = max(self.reply_ts or 0, ts)


_CACHE = {}                      # path -> [_Parse, offset, mid_line]
_LOCK = threading.Lock()


def _head(path, p):
    """session_start is the first line — title and cwd, even when only
    the tail of a long transcript was read."""
    try:
        with open(path, "rb") as f:
            p.feed(json.loads(f.readline(64 * 1024)))
    except (OSError, ValueError):
        pass


def parse(path):
    """Up-to-date _Parse for a transcript. Only bytes appended since the
    last call are read; a big file seen for the first time is read from
    its last TAIL bytes (the partial first line skipped). A file that
    shrank was rewritten — start over."""
    try:
        size = os.path.getsize(path)
    except OSError:
        return None
    with _LOCK:
        ent = _CACHE.get(path)
        if ent is None or size < ent[1]:
            start = max(0, size - TAIL)
            ent = _CACHE[path] = [_Parse(), start, start > 0]
        p, off, skip = ent
        if size == off:
            return p
        try:
            with open(path, "rb") as f:
                f.seek(off)
                buf = f.read(size - off)
        except OSError:
            return p
        end = buf.rfind(b"\n") + 1           # leave a half-written line
        lines = buf[:end].split(b"\n") if end else []
        if skip and lines:
            lines, skip = lines[1:], False   # cut mid-line by the tail seek
        for raw in lines:
            if raw.strip():
                try:
                    p.feed(json.loads(raw))
                except ValueError:
                    pass
        ent[1], ent[2] = off + end, skip
        _head(path, p)                       # the title may have been renamed
        return p


# -------------------------------------------------------------- sessions

def _json(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _index_titles():
    """sid -> title from sessions-index.json (only some sessions are in
    it; it follows renames)."""
    d = _json(INDEX_FILE) or {}
    return {e.get("sessionId"): e.get("title")
            for e in d.get("entries") or [] if e.get("title")}


_ORG = [None, None]               # [mtime, orgId] of ORG_FILE


def signed_in_org():
    """The signed-in Factory account's organization, or None when droid
    hasn't written one. Read again only when the file changes, so it is
    cheap enough to ask every tick. Only orgId is read (the file holds
    the org's model policy, no credentials)."""
    try:
        mt = os.path.getmtime(ORG_FILE)
    except OSError:
        _ORG[:] = [None, None]
        return None
    if mt != _ORG[0]:
        org = (_json(ORG_FILE) or {}).get("orgId")
        _ORG[:] = [mt, org if isinstance(org, str) and org else None]
    return _ORG[1]


def accounts(names_file):
    """The Factory accounts seen here, each with a letter Buddy gave it
    (A, B, … — droid keeps no readable account name on disk). A letter,
    once given, is kept in names_file; new accounts get the next one in
    the order their first session was made. Accounts = the organizations
    on sessions plus the signed-in one."""
    first = {}
    for s in sessions().values():
        if s["org"]:
            try:                            # Windows: ctime = made at
                t = os.path.getctime(s["path"])
            except OSError:
                continue
            first[s["org"]] = min(first.get(s["org"], t), t)
    cur = signed_in_org()
    if cur:
        first.setdefault(cur, time.time())
    names = _json(names_file) or {}
    names = {k: v for k, v in names.items() if isinstance(v, str)}
    used = set(names.values())
    new = [o for o in sorted(first, key=first.get) if o not in names]
    for o in new:
        n = next(chr(c) for c in range(ord("A"), ord("Z") + 1)
                 if chr(c) not in used)
        names[o] = n
        used.add(n)
    if new:
        try:
            tmp = names_file + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(names, f)
            os.replace(tmp, names_file)
        except OSError as e:
            cb.log("droid accounts save: %s" % e)
    return [{"org": o, "name": names[o], "current": o == cur}
            for o in sorted(first, key=lambda o: names[o])]


def visible(s, org=None):
    """droid's own rule: a session with an organization only loads under
    that account; one without loads under any. No account known: all."""
    org = org or signed_in_org()
    return not (org and s.get("org") and s["org"] != org)


_TOT = ("inputTokens", "outputTokens", "cacheReadTokens",
        "cacheCreationTokens")


def _info(path, p, titles):
    sid = os.path.basename(path)[:-len(".jsonl")]
    st = _json(path[:-len(".jsonl")] + ".settings.json") or {}
    use = st.get("lastCallTokenUsage") or {}
    tot = st.get("tokenUsage") or {}
    ctx = sum(int(use.get(k) or 0) for k in
              ("inputTokens", "cacheReadTokens", "cacheCreationTokens"))
    return {
        "sid": sid, "path": path,
        "title": titles.get(sid) or p.title or sid,
        "cwd": p.cwd, "org": p.org, "busy": p.busy,
        "refresh_ts": p.refresh_ts, "real_ts": p.real_ts,
        "reply_ts": p.reply_ts, "answer_ts": p.answer_ts,
        "replies": p.replies,
        # newest reply's model; "auto" sessions resolve per turn
        "model": p.model or st.get("model") or "",
        "source": "desktop" if p.desktop else "cli",
        "usage": use, "ctx": ctx or None,
        # the session's running totals: a delta between two reads is
        # exactly what was sent in between, however many requests
        "total": {k: int(tot.get(k) or 0) for k in _TOT},
    }


def sessions(since=None):
    """sid -> info for every transcript under ~/.factory/sessions;
    since = skip files not written after that epoch (cheap warm scan)."""
    out = {}
    titles = _index_titles()
    try:
        dirs = [os.path.join(SESS_DIR, d) for d in os.listdir(SESS_DIR)]
    except OSError:
        return out
    for d in dirs:
        try:
            names = os.listdir(d)
        except OSError:
            continue
        for n in names:
            if not n.endswith(".jsonl"):
                continue
            path = os.path.join(d, n)
            if since is not None:
                try:
                    if os.path.getmtime(path) < since:
                        continue
                except OSError:
                    continue
            p = parse(path)
            if p is not None:
                i = _info(path, p, titles)
                out[i["sid"]] = i
    return out


def list_spaces():
    """The panel's session list, newest reply first — same item shape as
    Devin's (no groups). Never-answered sessions ("New Session") have no
    cache to keep and stay out, and so do other accounts' (droid won't
    load them, so a heartbeat can't reach them)."""
    out = []
    org = signed_in_org()
    for s in sessions().values():
        if not s["replies"] or not visible(s, org):
            continue
        out.append({"type": "session", "sid": s["sid"], "title": s["title"],
                    "first_msg": s["title"],
                    "last_activity": int(s["reply_ts"] or s["answer_ts"]
                                         or os.path.getmtime(s["path"])),
                    "model": s["model"], "ctx": s["ctx"],
                    "source": s["source"]})
    out.sort(key=lambda e: -e["last_activity"])
    return out


def _m(u, model, ts=0):
    """Factory's camelCase usage -> the shared metrics dict. Priced by
    cache_buddy's table of official per-token list prices, same as Devin;
    a model missing there (GPT for now) shows no cost."""
    return {"input_tokens": int(u.get("inputTokens") or 0),
            "output_tokens": int(u.get("outputTokens") or 0),
            "cache_read_tokens": int(u.get("cacheReadTokens") or 0),
            "cache_creation_tokens": int(u.get("cacheCreationTokens") or 0),
            "generation_model": model or None, "_ts": int(ts or 0)}


def metrics(s):
    """The card's metrics dict from the newest request's usage."""
    u = s.get("usage") or {}
    if not (u.get("inputTokens") or u.get("cacheReadTokens")):
        return None
    return _m(u, s["model"], s.get("answer_ts") or s.get("refresh_ts"))


def track(s):
    """One cache track per session — Droid runs a single model."""
    return {"sid": s["sid"], "role": "lead", "model": s["model"],
            "family": cb._family(s["model"]), "metrics": metrics(s),
            "refresh_ts": s["refresh_ts"]}


# ------------------------------------------------------------- heartbeat

def send_heartbeat(exe, sid, cwd):
    """One heartbeat turn into session `sid`. Returns (ok, detail).
    -o json only shapes droid's stdout — the request is the same."""
    try:
        r = subprocess.run(
            [exe, "exec", "-s", sid, "-o", "json", HB_TEXT],
            cwd=cwd if cwd and os.path.isdir(cwd) else None,
            stdin=subprocess.DEVNULL, capture_output=True,
            timeout=EXEC_TIMEOUT, encoding="utf-8", errors="replace",
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except subprocess.TimeoutExpired:
        return False, "droid exec timed out (%ds)" % EXEC_TIMEOUT
    except OSError as e:
        return False, "droid exec: %s" % e
    res = {}
    for line in reversed((r.stdout or "").splitlines()):
        try:
            res = json.loads(line)
            break
        except ValueError:
            continue
    if r.returncode == 0 and res.get("subtype") == "success":
        return True, "sent"
    why = (res.get("result") or r.stderr or r.stdout or "").strip()
    return False, "exit %d: %s" % (r.returncode, why[:120])


# ------------------------------------------------- Factory Desktop (CDP)
# A session driven by Factory Desktop can't be renewed through `droid
# exec`: exec runs a different tool set, so its heartbeat misses the
# Desktop prefix entirely (measured: 0 cache-read) and leaves a "these
# tools are no longer available" reminder in the transcript. The
# Desktop path types the heartbeat into the open Factory window over
# CDP instead — same prefix, same tools, userMessageSource "desktop".
# Needs Factory started with --remote-debugging-port=9223 (append it
# to the desktop shortcut's target, or launch app-<ver>/
# factory-desktop.exe directly). No UIA fallback: it would steal
# focus, can't tell a running turn reliably and dies on a locked PC.

FACTORY_CDP_PORTS = (9223,)
_factory_logged = set()               # failure reasons already logged


def _factory_ports():
    v = os.environ.get("CACHE_BUDDY_FACTORY_CDP_PORT")
    if v:
        try:
            return (int(v),)
        except ValueError:
            pass
    return FACTORY_CDP_PORTS


def _factory_page(targets):
    """The Factory main-window page among /json/list targets — the file:
    url of the app.asar renderer. Devin's workbench.html pages and the
    agent's browser webview are never it."""
    for t in targets:
        u = (t.get("url") or "").lower()
        if (t.get("type") == "page" and "main_window/index.html" in u
                and "factory" in u):
            return t
    return None


class _FactoryCdp(cb._Cdp):
    """cb._Cdp with Factory's port and page selection instead of
    Devin's."""

    def __init__(self, timeout=3):
        import urllib.request
        import websocket
        self._json = json
        last = RuntimeError("no Factory CDP port")
        for port in _factory_ports():
            try:
                targets = json.loads(urllib.request.urlopen(
                    "http://127.0.0.1:%d/json/list" % port,
                    timeout=timeout).read())
            except Exception as e:
                last = e
                continue
            tgt = _factory_page(targets)
            if tgt is None:
                last = RuntimeError("no Factory page target")
                continue
            self.ws = websocket.create_connection(
                tgt["webSocketDebuggerUrl"], timeout=timeout,
                suppress_origin=True)
            self._id = 0
            return
        raise last


# Factory's own shortcuts (Desktop, Start menu, pinned taskbar) start it
# with no arguments, so a normal launch never opens the debug port — and
# Squirrel rewrites them on every update. Nothing is touched until the
# user presses the panel's "fix and restart Factory" button once
# (settings "factory_cdp"): then the shortcuts get the flag, Factory is
# restarted with it, and after an update Buddy quietly puts it back.
_FACTORY_LNK_DIRS = (
    ("USERPROFILE", "Desktop"),
    ("PUBLIC", "Desktop"),
    ("APPDATA", r"Microsoft\Windows\Start Menu\Programs"),
    ("APPDATA", r"Microsoft\Internet Explorer\Quick Launch\User Pinned\TaskBar"),
)
_PS_READ_LNK = r"""
$sh = New-Object -ComObject WScript.Shell
$out = @()
foreach ($d in ($env:CB_LNK_DIRS -split '\|')) {
  if (-not (Test-Path -LiteralPath $d)) { continue }
  Get-ChildItem -LiteralPath $d -Filter *.lnk -Recurse -ErrorAction SilentlyContinue | ForEach-Object {
    $l = $sh.CreateShortcut($_.FullName)
    if ($l.TargetPath -like '*\Factory\*factory-desktop.exe') {
      $out += @{ path = $_.FullName; args = $l.Arguments } } } }
ConvertTo-Json -Compress -InputObject @($out)
"""
_PS_WRITE_LNK = r"""
$sh = New-Object -ComObject WScript.Shell
foreach ($e in (ConvertFrom-Json $env:CB_LNK_EDIT)) {
  try { $l = $sh.CreateShortcut($e.path); $l.Arguments = $e.args; $l.Save(); $e.path }
  catch { } }
"""


def _ps(script, env):
    script = ("[Console]::OutputEncoding = [Text.Encoding]::UTF8\n"
              + script)                  # paths can be Chinese
    r = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True, timeout=20, env=dict(os.environ, **env),
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    return r.stdout.decode("utf-8", "replace")


def _with_port(args, port):
    """Shortcut arguments with the debug port added, or None when some
    --remote-debugging-port is already there."""
    if "--remote-debugging-port" in (args or ""):
        return None
    flag = "--remote-debugging-port=%d" % port
    return ("%s %s" % (args.strip(), flag)) if (args or "").strip() else flag


def fix_factory_shortcuts():
    """Add the debug port to every Factory shortcut that lacks it (other
    arguments kept). Returns how many were changed."""
    if os.name != "nt":
        return 0
    dirs = [os.path.join(os.environ[k], sub) for k, sub in _FACTORY_LNK_DIRS
            if os.environ.get(k)]
    try:
        found = json.loads(_ps(_PS_READ_LNK, {"CB_LNK_DIRS": "|".join(dirs)})
                           or "[]")
        if isinstance(found, dict):
            found = [found]
        port = _factory_ports()[0]
        edit = [{"path": e["path"], "args": a} for e in found
                for a in [_with_port(e.get("args"), port)] if a]
        if not found:
            cb.log("factory shortcut: none found to add the debug port to")
        if not edit:
            return 0
        fixed = [l.strip() for l in _ps(_PS_WRITE_LNK, {
            "CB_LNK_EDIT": json.dumps(edit)}).splitlines() if l.strip()]
        for p in fixed:
            cb.log("factory shortcut: added --remote-debugging-port=%d -> %s"
                   % (port, p))
        return len(fixed)
    except Exception as e:
        cb.log("factory shortcut fix failed: %r" % e)
        return 0


def _consented():
    return bool(cb.load_settings().get("factory_cdp"))


_lnk_checked = [False]


def keep_shortcuts():
    """Once per Buddy run, after the user agreed: put the flag back on
    shortcuts a Factory update rewrote. Silent; nothing without consent."""
    if _lnk_checked[0] or not _consented():
        return 0
    _lnk_checked[0] = True
    return fix_factory_shortcuts()


def _factory_exe():
    """The versioned factory-desktop.exe a running Factory uses, else the
    Squirrel stub (which starts the newest version, args passed on)."""
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command",
             "(Get-CimInstance Win32_Process -Filter \"Name='factory-desktop.exe'\""
             " | Select-Object -First 1).ExecutablePath"],
            capture_output=True, text=True, timeout=15,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        p = (r.stdout or "").strip()
        if p and os.path.isfile(p):
            return p
    except Exception:
        pass
    stub = os.path.join(os.environ.get("LOCALAPPDATA", ""), "Factory",
                        "factory-desktop.exe")
    return stub if os.path.isfile(stub) else None


def restart_factory_with_cdp(timeout=20):
    """The panel's "fix and restart" button: the user agrees once, so the
    shortcuts get the flag (and keep it after updates), then Factory is
    closed and started again with the port. Sessions are local files, so
    nothing is lost. Returns {"ok", "shortcuts", "detail"}."""
    d = cb.load_settings()
    if not d.get("factory_cdp"):
        d["factory_cdp"] = True
        cb.save_settings(d)
        cb.log("factory cdp: user agreed to keep the debug port on")
    _lnk_checked[0] = True
    n = fix_factory_shortcuts()
    exe = _factory_exe()
    if not exe:
        return {"ok": False, "shortcuts": n, "detail": "factory not found"}
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        subprocess.run(["taskkill", "/IM", "factory-desktop.exe", "/T"],
                       capture_output=True, timeout=15, creationflags=flags)
        t0 = time.time()
        while _factory_running() and time.time() - t0 < 8:
            time.sleep(0.5)
        if _factory_running():                 # didn't close in time
            subprocess.run(["taskkill", "/IM", "factory-desktop.exe", "/T",
                            "/F"], capture_output=True, timeout=15,
                           creationflags=flags)
            time.sleep(1)
        subprocess.Popen([exe, "--remote-debugging-port=%d"
                          % _factory_ports()[0]],
                         creationflags=0x00000008 | 0x00000200,
                         close_fds=True, stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception as e:
        cb.log("factory restart failed: %r" % e)
        return {"ok": False, "shortcuts": n, "detail": str(e)}
    _factory_logged.clear()
    t0 = time.time()
    while time.time() - t0 < timeout:
        if _cdp_up():
            cb.log("factory restarted with the debug port (%s)" % exe)
            return {"ok": True, "shortcuts": n, "detail": ""}
        time.sleep(0.8)
    return {"ok": False, "shortcuts": n, "detail": "no cdp after restart"}


def _cdp_up():
    import urllib.request
    for port in _factory_ports():
        try:
            if _factory_page(json.loads(urllib.request.urlopen(
                    "http://127.0.0.1:%d/json/list" % port,
                    timeout=1).read())):
                return True
        except Exception:
            pass
    return False


def _factory_running():
    """factory-desktop.exe in the process list — tells 'Factory isn't
    open' apart from 'open without the debug port'."""
    try:
        r = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq factory-desktop.exe",
             "/NH"], capture_output=True, text=True, timeout=10,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return "factory-desktop.exe" in (r.stdout or "")
    except Exception:
        return True                     # can't check: assume it's open


# Same hold rule as Devin's _CDP_GUARD_JS: arm only when the user is
# more than a screen above the bottom, then drop scroll writes that
# would land at the bottom — armed for up to 120s so it covers a whole
# heartbeat turn, and released early (holdUntil = now + 2s) once the
# reply landed or the send failed. The transcript's scroller is the
# nearest ancestor of the virtualized list with overflow-y
# auto/scroll — its hashed class name changes per build.
_FACTORY_GUARD_JS = r"""
(() => {
  const W = window;
  const find = () => {
    const v = document.querySelector('[data-testid="virtualized-transcript"]');
    for (let e = v && v.parentElement; e; e = e.parentElement) {
      const o = getComputedStyle(e).overflowY;
      if (o === 'auto' || o === 'scroll') return e;
    }
    return null;
  };
  const d = Object.getOwnPropertyDescriptor(Element.prototype, 'scrollTop');
  const g = W.__cbFactoryGuard = W.__cbFactoryGuard ||
    { holdUntil: 0, test: false, log: [], nudged: 0, drops: 0, v: 0 };
  const rec = o => { g.drops++; g.log.push({ t: Date.now(), ...o }); if (g.log.length > 200) g.log.shift(); };
  const active = el => el === find() && (Date.now() < g.holdUntil ||
    (g.test && el.scrollHeight - d.get.call(el) - el.clientHeight > el.clientHeight));
  const toBottom = (el, v) => v >= el.scrollHeight - el.clientHeight - 2;
  const nudge = el => {
    if (Date.now() - g.nudged < 1500) return;
    g.nudged = Date.now();
    setTimeout(() => { d.set.call(el, d.get.call(el) - 3); setTimeout(() => d.set.call(el, d.get.call(el) + 3), 50); }, 0);
  };
  // v < current: re-run the install so a page that already carries an
  // older patch set picks up the new coverage (re-wrapping is safe)
  if (g.v < 2) {
    g.v = 2;
    Object.defineProperty(Element.prototype, 'scrollTop', {
      configurable: true, get() { return d.get.call(this); },
      set(v) {
        if (active(this) && toBottom(this, v)) { rec({ drop: 'scrollTop=', v: Math.round(v), st: Math.round(d.get.call(this)) }); nudge(this); return; }
        d.set.call(this, v);
      }
    });
    for (const m of ['scrollTo', 'scroll', 'scrollBy']) {
      const o = Element.prototype[m];
      Element.prototype[m] = function (...a) {
        const v = typeof a[0] === 'object' ? a[0]?.top : a[1];
        if (v != null && active(this) && toBottom(this, v)) { rec({ drop: m, v: Math.round(v) }); nudge(this); return; }
        return o.apply(this, a);
      };
    }
    // the virtualizer also snaps via scrollIntoView on an item or a
    // bottom sentinel — a native path the setters never see
    for (const m of ['scrollIntoView', 'scrollIntoViewIfNeeded']) {
      const o = Element.prototype[m];
      if (!o) continue;
      Element.prototype[m] = function (...a) {
        const sc = find();
        if (sc && sc !== this && sc.contains(this) && Date.now() < g.holdUntil) {
          rec({ drop: m }); nudge(sc); return;
        }
        return o.apply(this, a);
      };
    }
    // and via CSS scroll anchoring, invisible to every patched API —
    // pin it off on the scroller for the hold window
    if (!g.pin) {
      const pin = () => {
        const sc = find();
        if (!sc) { g.anchorTimer = null; return; }
        if (Date.now() < g.holdUntil) {
          if (sc.style.overflowAnchor !== 'none') sc.style.overflowAnchor = 'none';
          g.anchorTimer = setTimeout(pin, 500);
        } else {
          if (sc.style.overflowAnchor === 'none') sc.style.overflowAnchor = '';
          g.anchorTimer = null;
        }
      };
      g.pin = () => { if (!g.anchorTimer) g.anchorTimer = setTimeout(pin, 0); };
    }
  }
  g.test = false;
  g.drops = g.drops || 0;
  const stat = () => {
    const c = find();
    return c ? {
      ok: true, st: Math.round(c.scrollTop), dist: Math.round(c.scrollHeight - c.scrollTop - c.clientHeight),
      ch: c.clientHeight, drops: g.drops, holding: Date.now() < g.holdUntil
    } : { ok: false };
  };
  g.stat = stat;
  g.arm = ms => {
    const s = stat();
    s.armed = s.ok && s.dist > s.ch;
    if (s.armed) { g.holdUntil = Date.now() + ms; g.drops = 0; s.drops = 0; g.pin && g.pin(); }
    return s;
  };
  return stat();
})()
"""

# Composer state: the wrapper's contenteditable is the draft (innerText),
# "Send message" appears once there's content, "Stop agent" is the busy
# signal, and an archived session's chrome carries "read-only mode".
_FD_STATE_JS = r"""
(() => {
  const w = document.querySelector('[data-testid="chat-composer-wrapper"]');
  const p = w && w.querySelector('[contenteditable=true][role=textbox]');
  const btn = n => w && [...w.querySelectorAll('button')].find(
    b => (b.getAttribute('aria-label') || '') === n && b.offsetParent);
  const send = btn('Send message');
  const chrome = document.querySelector('[data-testid="session-detail-chrome"]');
  return {
    hash: location.hash,
    found: !!p,
    archived: !w && /read-only mode/.test((chrome && chrome.innerText) || ''),
    text: p ? (p.innerText || '') : '',
    canSend: send ? !send.disabled : null,
    busy: !!btn('Stop agent')
  };
})()
"""

_FD_FOCUS_JS = (r"""document.querySelector('[data-testid="chat-composer-"""
                r"""wrapper"] [contenteditable=true][role=textbox]').focus()""")

# Composer attachment chips: a "Remove <name>" button per chip; an image
# chip's <img alt="<name>" src="data:…"> sits in the same chip box. The
# wrapper owns its own hidden file input for re-attaching.
_FD_ATTS_JS = r"""
(() => {
  const w = document.querySelector('[data-testid="chat-composer-wrapper"]');
  if (!w) return null;
  const atts = [...w.querySelectorAll('button[aria-label^="Remove "]')]
    .map(rb => {
      const name = (rb.getAttribute('aria-label') || '').slice(7);
      let img = null, box = rb;
      for (let i = 0; i < 8 && box.parentElement && !img; i++) {
        box = box.parentElement;
        img = [...box.querySelectorAll('img')]
              .find(im => im.alt === name) || null;
      }
      return img && String(img.src).startsWith('data:')
        ? {kind: 'image', name: name || img.alt || 'image.png',
           mime: (/^data:([^;,]+)/.exec(img.src) || [])[1] || 'image/png',
           dataUrl: img.src}
        : {kind: 'other', name};
    });
  return {atts, fidx: [...document.querySelectorAll('input[type=file]')]
          .findIndex(i => w.contains(i))};
})()
"""

_FD_ATT_COUNT_JS = r"""
(() => {
  const w = document.querySelector('[data-testid="chat-composer-wrapper"]');
  return w ? w.querySelectorAll('button[aria-label^="Remove "]').length : 0;
})()
"""

_FD_CLICK_ONE_ATT_JS = r"""
(() => {
  const w = document.querySelector('[data-testid="chat-composer-wrapper"]');
  const b = w && w.querySelector('button[aria-label^="Remove "]');
  if (!b) return false;
  b.click();                         // one at a time — each removal
  return true;                       // re-renders
})()
"""


def _fd_save_atts(atts):
    """Persist each image chip's data URL under its original file name
    so the re-attached chip looks identical."""
    import base64
    d = os.path.join(cb.ATTACH_DIR, str(int(time.time())))
    os.makedirs(d, exist_ok=True)
    files, seen = [], set()
    for a in atts:
        data = a.get("dataUrl") or ""
        raw = (base64.b64decode(data.split(",", 1)[1])
               if "," in data else b"")
        name = os.path.basename(a.get("name") or "") or "image.png"
        if name in seen:
            name = "%d_%s" % (len(files), name)
        seen.add(name)
        fn = os.path.join(d, name)
        with open(fn, "wb") as f:
            f.write(raw)
        files.append(fn)
    cb.log("factory attachments saved: %d -> %s" % (len(files), d))
    return files


# Removing a chip leaves the file in the input's file list — a
# setFileInputFiles of the same file then fires no change event. Reset
# the value first, every time.
_FD_RESET_INPUT_JS = r"""
(() => {
  const w = document.querySelector('[data-testid="chat-composer-wrapper"]');
  if (!w) return false;
  w.querySelectorAll('input[type=file]').forEach(i => { i.value = ''; });
  return true;
})()
"""


def _fd_attach(c, paths):
    """Attach `paths` one file at a time through the composer wrapper's
    own file input — DOM.setFileInputFiles is the same code path a user
    pick exercises. Each file waits for its own chip."""
    fidx = (c.eval(_FD_ATTS_JS) or {}).get("fidx")
    ids = c.dom_select("input[type=file]", all=True)
    if fidx is None or not (0 <= fidx < len(ids)):
        return False
    node = ids[fidx]
    for f in paths:
        before = c.eval(_FD_ATT_COUNT_JS) or 0
        c.eval(_FD_RESET_INPUT_JS)
        c.call("DOM.setFileInputFiles", {"files": [f], "nodeId": node})
        deadline = time.time() + 4
        while time.time() < deadline:
            if (c.eval(_FD_ATT_COUNT_JS) or 0) > before:
                break
            time.sleep(0.2)
        else:
            return False
    return True


def _fd_attach_missing(c, atts, files):
    """Re-attach only the saved files whose data URL is no longer among
    the composer chips."""
    cur = {a.get("dataUrl") for a in
           ((c.eval(_FD_ATTS_JS) or {}).get("atts") or [])}
    miss = [f for f, a in zip(files, atts)
            if a.get("dataUrl") not in cur]
    if miss:
        _fd_attach(c, miss)
    return len(miss)


def _fd_clear_text(c):
    """Focus, select-all, Backspace in the composer — verified. Returns
    the remaining text."""
    c.eval(_FD_FOCUS_JS)
    cb._cdp_key(c, "a", "KeyA", 65, modifiers=2)
    time.sleep(0.12)
    cb._cdp_key(c, "Backspace", "Backspace", 8)
    time.sleep(0.25)
    return (c.eval(_FD_STATE_JS) or {}).get("text") or ""


def _fd_draft_back(c, draft):
    """Clear the composer and re-insert the user's draft. A multi-line
    draft goes back in one insertText — \n lands as a real line."""
    c.eval(_FD_FOCUS_JS)
    cb._cdp_key(c, "a", "KeyA", 65, modifiers=2)
    time.sleep(0.1)
    cb._cdp_key(c, "Backspace", "Backspace", 8)
    time.sleep(0.2)
    if draft:
        c.call("Input.insertText", {"text": draft})
        time.sleep(0.3)
    cur = (c.eval(_FD_STATE_JS) or {}).get("text") or ""
    if draft:
        cb.log("draft restored in composer" if cur == draft
               else "draft restore mismatch")
    elif cur.strip():
        cb.log("composer not cleared: %r" % cur[:40])


def _fd_restore(c, draft, files, hb_text=None):
    """Put the composer back: draft text, then re-attach the saved
    images. Any heartbeat text still there is scrubbed first — never
    leave it behind on any path. Returns the keep dir on attach
    failure, else None."""
    cur = (c.eval(_FD_STATE_JS) or {}).get("text") or ""
    if (hb_text and cur and cur != draft
            and cur.startswith(hb_text[:30])
            and not (draft or "").startswith(hb_text[:30])):
        cb.log("composer had heartbeat text, cleared")
    _fd_draft_back(c, draft)
    if hb_text:                             # an async bounce can re-insert
        cur = (c.eval(_FD_STATE_JS) or {}).get("text") or ""
        if (cur and cur != draft and cur.startswith(hb_text[:30])
                and not (draft or "").startswith(hb_text[:30])):
            cb.log("composer had heartbeat text, cleared")
            _fd_draft_back(c, draft)
    if not files:
        return None
    ok = False
    try:
        if _fd_attach(c, files):
            deadline = time.time() + 4
            while time.time() < deadline:
                if (c.eval(_FD_ATT_COUNT_JS) or 0) == len(files):
                    ok = True
                    break
                time.sleep(0.2)
    except Exception as e:
        cb.log("attach restore error: %s" % e)
    if ok:
        cb.log("attachments restored (%d)" % len(files))
        for f in files:
            try:
                os.remove(f)
            except OSError:
                pass
        try:
            os.rmdir(os.path.dirname(files[0]))
        except OSError:
            pass
        return None
    d = os.path.dirname(files[0])
    cb.log("attachments kept at %s" % d)     # never lose the user's files
    return d


def _fd_bounce_cleanup(c, text, draft, files):
    """The heartbeat bounced back into the composer — a turn was still
    finishing, so the send never really went out. Scrub it, restore
    draft + attachments, and report a busy hold."""
    cb.log("factory send bounced (busy)")
    cur = _fd_clear_text(c)
    if cur.startswith(text[:30]):           # the clear lost a race — retry
        cur = _fd_clear_text(c)
    if cur:
        cb.log("bounce cleanup: composer still holds %r" % cur[:40])
    else:
        cb.log("bounce cleanup: composer cleared")
    _fd_restore(c, draft, files, text)
    _fd_release(c)
    return False, "cdp: session busy"


def _fd_release(c, tail_ms=2000):
    """Shorten an armed scroll hold inside this session — a failed send
    shouldn't leave the user's view pinned for the full hold window."""
    try:
        c.eval("window.__cbFactoryGuard && "
               "(window.__cbFactoryGuard.holdUntil = Date.now() + %d, 1)"
               % tail_ms)
    except Exception:
        pass


def release_desktop_hold(tail_ms=2000):
    """Shorten an armed scroll hold to `tail_ms` — called on a fresh
    connection once the heartbeat reply has landed, so the transcript
    follows again. No-op when nothing is held; never raises."""
    try:
        with _FactoryCdp() as c:
            s = c.eval(
                "window.__cbFactoryGuard &&"
                " window.__cbFactoryGuard.holdUntil > Date.now()"
                " ? (window.__cbFactoryGuard.holdUntil ="
                " Date.now() + %d, window.__cbFactoryGuard.stat())"
                " : null" % tail_ms)
            if s:
                cb.log("cdp: hold released st=%s dist=%s drops=%s"
                       % (s.get("st"), s.get("dist"), s.get("drops")))
    except Exception:
        pass


def _fd_send_body(c, sid, text, submit):
    """The send proper — inside one connected _FactoryCdp session with
    focus emulation already enabled. Same shape as cache_buddy's
    _cdp_send_body, plus hash-routing to the target session."""
    target = "#/sessions/" + sid
    prev = c.eval("location.hash") or ""
    navigated = prev != target
    try:
        if navigated:
            cb.log("factory cdp: %s -> %s" % (prev or "(none)", target))
            c.eval("location.hash = %s" % json.dumps(target))
            time.sleep(1)
            st = None
            deadline = time.time() + 8
            while time.time() < deadline:
                st = c.eval(_FD_STATE_JS)
                if st and (st.get("found") or st.get("archived")):
                    break
                time.sleep(0.25)
        else:
            st = c.eval(_FD_STATE_JS)
        if st and st.get("archived"):
            return False, "cdp: session archived"
        if not st or not st.get("found"):
            return False, "cdp: composer not found"
        if st.get("busy"):
            return False, "cdp: session busy"
        if not navigated:
            # the hold only matters on the session the user is viewing.
            # Arm before touching anything: Factory's transcript snaps
            # to the bottom on composer DOM changes too (typing mounts
            # the Send button), not only when the reply lands.
            try:
                h = c.eval("(%s, window.__cbFactoryGuard.arm(120000))"
                           % _FACTORY_GUARD_JS.strip())
            except Exception:
                h = None
            if h is None:
                cb.log("cdp: arm failed")
            elif h.get("armed"):
                cb.log("cdp: armed st=%s dist=%s"
                       % (h.get("st"), h.get("dist")))
            else:
                cb.log("cdp: near bottom, no hold")
        draft = st.get("text") or ""
        # attachments: the heartbeat must not carry the user's composer
        # images into the chat — pull them off first, put them back after
        att = c.eval(_FD_ATTS_JS)
        files = []
        if att and att.get("atts"):
            if any(a.get("kind") != "image" for a in att["atts"]):
                _fd_release(c)                     # unknown chip: don't touch
                return False, cb.ATTACH_HOLD
            files = _fd_save_atts(att["atts"])     # save BEFORE removing
            deadline = time.time() + 2
            n = len(att["atts"])
            while n and time.time() < deadline:
                if not c.eval(_FD_CLICK_ONE_ATT_JS):
                    break
                time.sleep(0.25)
                n = c.eval(_FD_ATT_COUNT_JS) or 0
            cb.log("factory attachments off: %d left" % n)
            if n != 0:                             # removal failed — put back
                try:                               # only what actually left
                    _fd_attach_missing(c, att["atts"], files)
                except Exception as e:
                    cb.log("attach back error: %s" % e)
                _fd_release(c)
                return False, cb.ATTACH_HOLD
        cb.log("factory send(cdp): composer ok%s%s"
               % (" +draft" if draft else "",
                  " +%datt" % len(files) if files else ""))
        c.eval(_FD_FOCUS_JS)
        if draft:
            cb._cdp_key(c, "a", "KeyA", 65, modifiers=2)
            time.sleep(0.15)
            cb._cdp_key(c, "Backspace", "Backspace", 8)
            time.sleep(0.2)
        c.call("Input.insertText", {"text": text})
        time.sleep(0.3)
        st = c.eval(_FD_STATE_JS)
        cur = (st or {}).get("text") or ""
        if not cur.startswith(text[:20]):
            _fd_restore(c, draft, files, text)
            _fd_release(c)
            return False, "heartbeat did not land in composer"
        if not submit:
            kept = _fd_restore(c, draft, files, text)
            _fd_release(c)                      # nothing was sent — no
            return True, ("dry run · attachments kept at " + kept  # turn
                          if kept else "dry run")                  # coming
        t0 = time.time()
        clicked = c.eval(
            "(() => { const w = document.querySelector("
            "'[data-testid=\"chat-composer-wrapper\"]');"
            " const b = w && [...w.querySelectorAll('button')]"
            ".find(e => e.getAttribute('aria-label') === 'Send message'"
            " && e.offsetParent);"
            " if (!b || b.disabled) return false;"
            " b.click(); return true; })()")
        if not clicked:
            cb._cdp_key(c, "Enter", "Enter", 13, text="\r")
        sent = False
        for _ in range(12):          # poll up to 3s — the composer going
            time.sleep(0.25)         # empty means the message left
            st = c.eval(_FD_STATE_JS)
            if st and st.get("found") and not (st.get("text") or "").strip():
                sent = True
                break
        if not sent:
            _fd_restore(c, draft, files, text)
            _fd_release(c)
            return False, "enter did not submit"
        left = 1.5 - (time.time() - t0)   # same settle as the devin path
        if left > 0:
            time.sleep(left)
        if not navigated:
            try:
                st = c.eval("window.__cbFactoryGuard.stat()")
            except Exception:
                st = None
            if st:
                cb.log("cdp: after send st=%s dist=%s drops=%s"
                       % (st.get("st"), st.get("dist"), st.get("drops")))
        # settle over — a turn finishing under us can bounce the send
        # back into the composer. Heartbeat text back means NOT sent.
        st = c.eval(_FD_STATE_JS)
        cur = (st or {}).get("text") or ""
        bounced = cur.startswith(text[:30])
        if not bounced:
            bounced = (bool(cur.strip())
                       and bool((st or {}).get("busy")))
        if bounced:
            return _fd_bounce_cleanup(c, text, draft, files)
        kept = _fd_restore(c, draft, files, text)  # composer is empty —
        if kept:                              # draft + images go back
            return True, "sent · attachments kept at " + kept
        return True, "sent +draft back" if draft else "sent"
    finally:
        if navigated:
            try:
                c.eval("location.hash = %s" % json.dumps(prev))
            except Exception as e:
                cb.log("factory cdp: hash restore failed: %s" % e)


def send_desktop_heartbeat(sid, text=HB_TEXT, submit=True):
    """Type `text` into Factory Desktop's composer for session `sid`
    and submit it — the Desktop path of a heartbeat. Returns
    (ok, detail); the caller maps detail like Devin's path does."""
    try:
        c = _FactoryCdp()
    except Exception as e:
        if not _factory_running():
            return False, "factory not running"
        reason = "%s: %s" % (type(e).__name__, e)
        if reason not in _factory_logged:
            _factory_logged.add(reason)
            cb.log("factory cdp connect failed: %s" % reason)
        # running without the port. Agreed before (an update rewrote the
        # shortcuts, or it was opened some other way): put the flag back
        # and ask for a restart; otherwise the panel offers the fix button
        if _consented():
            keep_shortcuts()
            return False, "factory restart"
        return False, "factory no cdp"
    try:
        with c:
            c.call("Emulation.setFocusEmulationEnabled", {"enabled": True})
            try:
                return _fd_send_body(c, sid, text, submit)
            finally:
                try:
                    c.call("Emulation.setFocusEmulationEnabled",
                           {"enabled": False})
                except Exception:
                    pass
    except Exception as e:
        # an exception can hit after the hold was armed — shorten it on
        # a fresh connection (harmless if the guard is already gone)
        release_desktop_hold()
        return False, "cdp error: %s" % e


# --------------------------------------------------------------- monitor

class DroidMonitor(cb.Monitor):
    """Devin's single-target schedule and panel state, fed from Factory's
    session files; heartbeats go out through `droid exec` for CLI
    sessions and the Factory window's CDP for Desktop ones."""
    uses_cdp = False

    def __init__(self, on_state, idle_secs, ledger_file):
        super().__init__(on_state, idle_secs)
        self._ledger = cb.Ledger(ledger_file)
        # the letters accounts() gave (org id -> "A"), next to the ledger
        self.names_file = os.path.join(os.path.dirname(ledger_file),
                                       "droid-accounts.json")
        self.exe = droid_exe()
        self.info = None                  # sessions() entry of the target
        self._srcmap = {}                 # sid -> "desktop" / "cli"
        self._fd_hold = None
        # the panel's "restart Factory": no heartbeat while it restarts and
        # for a few seconds after (the page is still loading its sessions)
        self.fd_restarting = False
        self.fd_grace = 0

    def _source(self, sid):
        return self._srcmap.get(sid)

    def set_target(self, sid, title, first_msg):
        super().set_target(sid, title, first_msg)
        self.info = None
        self._snap = None                 # running totals at the last book
        self._real = None                 # real_ts at the last book
        self._hb_at = 0                   # last heartbeat we sent
        self._hb_read = 0                 # the prefix it read
        self._fd_hold = None              # Factory can't take a heartbeat
                                          # (reason), until a send works

    def _book(self, s, hb):
        """Book what the session sent since the last look: a heartbeat's
        cost, or a real stretch's cache saving. The first look only takes
        a baseline — history before it is never credited."""
        tot, snap = s["total"], self._snap
        self._snap = tot
        if snap is None or any(tot[k] < snap[k] for k in _TOT):
            return None                   # baseline, or the totals reset
        d = {k: tot[k] - snap[k] for k in _TOT}
        c = cb.turn_cost(_m(d, s["model"]))
        if c and hb:
            self._ledger.add(hb_cost=c[0])
            self.cycle_saved -= c[0]
        elif c and any(d.values()):
            self._ledger.add(cache_saved=c[1] - c[0])
        return d

    def _rescue(self, s, d):
        """A real turn after a gap past the TTL, with our heartbeat in
        between, that still read the cache instead of writing it again —
        Devin's rule. Valued at the prefix the heartbeat kept alive."""
        prev, self._real = self._real, s["real_ts"]
        if not (d and prev and s["real_ts"] and s["real_ts"] > prev):
            return
        if not (s["real_ts"] - prev > cb.TTL and self._hb_at > prev
                and d["cacheReadTokens"] and self._hb_read
                and d["cacheCreationTokens"] < self._hb_read / 2):
            return
        v = cb.rescue_value({"cache_read_tokens": self._hb_read,
                             "generation_model": s["model"]})
        self._ledger.add(saved=v, rescues=1)
        self.cycle_saved += v
        self.cycle_rescued += 1
        self.rescues.append({"sid": s["sid"], "title": self.target[1],
                             "usd": v, "at": time.time(), "seen": False})
        del self.rescues[:-12]
        self.last_log = cb.log("rescued turn -> %s: cache kept alive, saved "
                               "$%.4f" % (self.target[1], v))

    def _lookup(self, hb=False):
        """Bring the target's state up to date (incremental parse) and
        book what changed. hb = right after our own heartbeat."""
        sid = self.target[0]
        path = self.info["path"] if self.info else None
        if path:
            p = parse(path)
            s = _info(path, p, _index_titles()) if p else None
        else:
            s = sessions().get(sid)
        self.info = s
        if not s:
            return None
        d = self._book(s, hb)
        if hb:
            self._hb_at = time.time()
            self._hb_read = int((s["usage"] or {}).get("cacheReadTokens") or 0)
        elif self._real is None:
            self._real = s["real_ts"]     # baseline
        else:
            self._rescue(s, d)
        self._srcmap[sid] = s["source"]
        self.sess, self.sess_sid = {"refresh_ts": s["refresh_ts"],
                                    "busy": s["busy"]}, sid
        t = track(s)
        m = t["metrics"]
        t["this_cycle"] = bool(m and m["_ts"] >= self.cycle_since)
        self.cycle_tracks = self.last_seen_tracks = [t]
        return s

    def _warm_poll(self):
        """Other sessions with a live cache, for the card. Files untouched
        for a TTL can't be warm and aren't opened."""
        now = time.time()
        warm = {}
        org = signed_in_org()
        for sid, s in sessions(since=now - cb.TTL).items():
            self._srcmap[sid] = s["source"]
            if not visible(s, org):
                continue
            if s["refresh_ts"] and now - s["refresh_ts"] < cb.TTL:
                warm[sid] = (s["title"], [track(s)])
        self.warm = warm

    def _tick(self):
        now = int(time.time())
        if now - self._warm_at >= 10:
            self._warm_at = now
            try:
                self._warm_poll()
            except Exception as e:
                cb.log("droid warm scan error: %s" % e)
        s = self._lookup() if self.target else None
        self._refresh_tok_text()
        if not self.target:
            return [dict(sid="", title="未选择会话（在上方列表选一个）",
                         state="", left="", color="#888", kind="none")]
        sid, title, _fm = self.target

        def row(kind, state, reason="", color="#888", left=""):
            return [dict(sid=sid, title=title, state=state, left=left,
                         color=color, kind=kind, reason=reason)]
        if s is None:
            return row("loading", "读取会话状态…")
        if not visible(s):
            # signed in to another account: droid won't load this session
            # any more, so a heartbeat would only fail — stop guarding it
            self.last_log = cb.log("droid account switched: stop guarding "
                                   "%s (another account's)" % title)
            self.clear_target()
            return self.pending_rows
        idle = now - max(self.cycle_since, s["real_ts"] or 0)
        if idle > self.maint and not self.manual:
            self.last_log = cb.log("auto-stop: %s idle %d min (keep-alive "
                                   "%d min)" % (title, idle // 60,
                                                self.maint // 60))
            self.clear_target()
            return self.pending_rows
        desktop = s["source"] == "desktop"
        if not desktop and not self.exe:
            self.due_at = 0
            return row("held", "找不到 droid 命令", "droid_missing", "#c90")
        if self.fails >= 3:
            return row("failed", "连续失败已停（重选可恢复）")
        refresh = s["refresh_ts"]
        if refresh is None or now - refresh >= cb.TTL:
            return self._expired_row(sid, title)
        self._expired_logged = False
        due = now if self.manual else max(refresh + self.idle_secs,
                                          self.next_due)
        if desktop and (self.fd_restarting or now < self.fd_grace):
            self.due_at = 0
            return row("held", "正在重启 Factory", "droid_desktop_restarting",
                       "#5B8DD6")
        if desktop and self._fd_hold and now < due:
            # keep saying why between retries — the plain "guarding" row
            # would hide the fix button until the next failed send
            self.due_at = 0
            return row("held", "Factory 需要重启一次", self._fd_hold, "#c90")
        if not desktop:
            self._fd_hold = None
        if now < due:
            self.due_at = due
            return row("guarding", "守护中", color="#3a3",
                       left="%d:%02d 后注入" % divmod(int(due - now), 60))
        self.due_at = 0
        if s["busy"]:
            if not self._busy_logged:
                self._busy_logged = True
                self.last_log = cb.log("droid skip: turn running -> %s"
                                       % title)
            return row("busy", "会话运行中·等结束", color="#c90")
        self._busy_logged = False
        if getattr(self, "_inject_busy", False):
            # a previous send is still inside a hung CDP/exec call —
            # never stack a second typing session on top of it
            self.next_due = now + 30
            self.last_log = cb.log("droid heartbeat still busy -> %s"
                                   % title)
            return row("retry", "发送卡死·重试中", color="#c55")
        manual, self.manual = self.manual, False
        self.last_log = cb.log("droid heartbeat -> %s (%s)%s%s"
                               % (title, s["model"],
                                  " [desktop]" if desktop else "",
                                  " (manual)" if manual else ""))
        done = {}

        def job():
            try:
                if desktop:
                    done["r"] = send_desktop_heartbeat(sid)
                else:
                    done["r"] = send_heartbeat(self.exe, sid, s["cwd"])
            except Exception as e:
                done["r"] = (False, "send error: %s" % e)
            finally:
                self._inject_busy = False
        self._inject_busy = True
        self.injecting = True
        th = threading.Thread(target=job, daemon=True)
        th.start()
        try:
            th.join(90)             # hard watchdog — a hung CDP call can
        finally:                    # block forever; never let it
            self.injecting = False
        if th.is_alive():
            self.fails += 1
            self.next_due = now + 30
            self.last_log = cb.log("droid heartbeat timeout -> %s"
                                   % title)
            return row("retry", "发送卡死·重试中", color="#c55")
        ok, detail = done.get("r", (False, "no result"))
        if not self.target:               # stopped while it was out
            return [dict(sid="", title="未选择会话（在上方列表选一个）",
                         state="", left="", color="#888", kind="none")]
        now = int(time.time())
        if not ok and desktop and (self.fd_restarting or now < self.fd_grace):
            # a send that was already out when the restart closed Factory:
            # not a failure, and no CDP error on the panel
            self.next_due = now + 5
            return row("held", "正在重启 Factory", "droid_desktop_restarting",
                       "#5B8DD6")
        if not ok:
            if detail == "cdp: session busy":
                self.next_due = now + 5   # a turn started under us — not
                self.last_log = cb.log(   # a failure
                    "droid heartbeat deferred -> %s: busy" % title)
                return row("busy", "会话运行中·等结束", color="#c90")
            if detail == cb.ATTACH_HOLD:
                self.next_due = now + 30
                return row("held", "输入框有附件·等清空",
                           "droid_attach", "#c90")
            hold = {"factory not running": "droid_desktop_closed",
                    "factory no cdp": "droid_desktop_cdp",
                    "factory restart": "droid_desktop_restart"}.get(detail)
            if hold:
                self._fd_hold = hold
                self.next_due = now + 15
                return row("held", "Factory 未打开" if hold ==
                           "droid_desktop_closed" else "Factory 需要重启一次",
                           hold, "#c90")
            self.fails += 1
            self.next_due = now + 20      # retry soon, not a full interval
            self.last_log = cb.log("droid heartbeat failed -> %s: %s"
                                   % (title, detail))
            return row("retry", "失败 %s" % detail, color="#c55")
        if desktop:
            # the desktop send returns once the message left the
            # composer, before the reply lands — wait out the turn so
            # the booked cost is the finished request's
            path, base_ts = s["path"], s["refresh_ts"] or 0
            t0, seen_busy = time.time(), False
            while time.time() - t0 < 110:
                p = parse(path)
                if p is None:
                    break
                if p.busy:
                    seen_busy = True
                elif (seen_busy or (p.refresh_ts or 0) > base_ts
                        or time.time() - t0 > 8):
                    break
                time.sleep(1)
            try:                            # reply's in — let the view
                release_desktop_hold()      # follow again (armed 120s)
            except Exception:
                pass
        self.fails = 0
        self._fd_hold = None
        self.next_due = 0                 # the schedule runs off refresh
        self.last_inject = now
        self.cycle_hb += 1
        self._ledger.add(hb=1)
        s = self._lookup(hb=True) or s
        self.last_log = cb.log("droid heartbeat sent -> %s ✓ read=%s"
                               % (title, (s["usage"] or {}).get(
                                   "cacheReadTokens")))
        return row("sent", "已续期", color="#3a3",
                   left="%d:%02d" % divmod(self.idle_secs, 60))


class DroidApi(cb._BuddyApi):
    """pywebview js_api for the Droid harness."""

    def state(self):
        s = self.mon.ui_state()
        # the panel reloads its list when this changes (account switch)
        s["account"] = signed_in_org()
        return s

    def spaces(self):
        return list_spaces()

    def accounts(self):
        """The right-click "Droid ▸" submenu: every account, lettered."""
        return accounts(self.mon.names_file)

    def rescan(self):
        return self.spaces()

    def factory_fix(self):
        """The panel's button: shortcuts keep the debug port from now on,
        Factory restarted with it; the guard picks up a few seconds after
        (no heartbeat, and so no CDP error, while it restarts)."""
        m = self.mon
        if m.fd_restarting:
            return {"ok": False, "detail": "busy"}
        m.fd_restarting = True
        try:
            r = restart_factory_with_cdp()
        finally:
            m.fd_restarting = False
        if r.get("ok"):
            m._fd_hold, m.fails = None, 0
            m.fd_grace = time.time() + 6     # its page loads its sessions
            m.next_due = 0
        return r

    def start(self, sid):
        for s in list_spaces():
            if s["sid"] == sid:
                self.mon.set_target(sid, s["title"], s["first_msg"])
                cb.log("ui: droid start -> %s" % s["title"])
                return True
        return False


def make_monitor(idle, ledger_file):
    """The Droid monitor, or None when Factory isn't installed here."""
    if not os.path.isdir(SESS_DIR):
        return None
    m = DroidMonitor(on_state=None, idle_secs=idle, ledger_file=ledger_file)
    m.on_state = lambda rows, last: setattr(m, "pending_rows", rows)
    m.start()
    if _consented():                      # an update may have reset them
        threading.Thread(target=keep_shortcuts, daemon=True).start()
    return m
