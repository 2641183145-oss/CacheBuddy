# -*- coding: utf-8 -*-
"""Claude Code Cache Buddy — keeps a Claude Code CLI session's prompt cache
warm (5-minute TTL) while you're away from the keyboard.

Devin Cache Buddy types a heartbeat into the Devin window. The CLI has no
composer to type into, so this one sits in front of the API instead: a
local pass-through proxy (ANTHROPIC_BASE_URL) remembers the last request
each session sent, and when the TTL is about to run out it replays that
request upstream with a one-line note appended after the last cache
breakpoint, asking for a one-character reply. The replay reads the whole
cached prefix, which renews its TTL. It goes out from the buddy, not from
Claude Code: nothing is added to the conversation, the terminal is never
touched (so a user scrolled up in history isn't yanked to the bottom), and
the transcript never sees it.

Run:     pythonw cc_buddy.py              (panel + proxy on 127.0.0.1:47821)
         python  cc_buddy.py --headless   (proxy + monitor, no window)
Launch:  ccb [claude args...]   — starts the buddy if needed, then runs
         `claude --settings <overlay>` with ANTHROPIC_BASE_URL -> the proxy
"""
import hashlib
import http.client
import json
import os
import re
import shutil
import ssl
import subprocess
import sys
import threading
import time
import traceback
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

import cache_buddy as cb          # shared: _WinCtl, run_webview, _family, log
import devin_cli                  # Devin CLI proxy + its env route
import droid_buddy                # Droid (Factory) harness
import droid_accounts             # Droid account switching
import quota                      # Devin / Factory plan quota
import usage                      # token usage per model, per harness
import bridge                     # carry a session to another harness

CLAUDE_DIR = (os.environ.get("CLAUDE_CONFIG_DIR")
              or os.path.join(os.path.expanduser("~"), ".claude"))
PROJECTS_DIR = os.path.join(CLAUDE_DIR, "projects")   # <proj>/<sid>.jsonl
SESSIONS_DIR = os.path.join(CLAUDE_DIR, "sessions")   # <pid>.json registry
SETTINGS = os.path.join(CLAUDE_DIR, "settings.json")
BUDDY_DIR = os.path.join(CLAUDE_DIR, "cache-buddy")
CONF_FILE = os.path.join(BUDDY_DIR, "config.json")
CTL_FILE = os.path.join(BUDDY_DIR, "buddy.ctl")
LEDGER_FILE = os.path.join(BUDDY_DIR, "ledger.json")
LAUNCH_SETTINGS = os.path.join(BUDDY_DIR, "launch-settings.json")

PORT = 47821
DEFAULT_UPSTREAM = "https://api.anthropic.com"
TTL = cb.TTL            # Claude Code's default cache_control TTL is 5m too
DEFAULT_IDLE = 270      # replay this long after the last cache refresh
MAX_IDLE = 30 * 60      # auto-stop this long after the last real turn by
                        # default — the ⚙ 缓存维护时间 (CcMonitor.maint)
POLL = 1

# the shared panel code logs and saves its dock position through these
# module globals — point them at our own files so a Devin buddy running
# side by side keeps its own log/ui.json and its own window
cb.LOG_FILE = os.path.join(BUDDY_DIR, "buddy.log")
cb.UI_STATE_FILE = os.path.join(BUDDY_DIR, "ui.json")
cb.SETTINGS_FILE = os.path.join(BUDDY_DIR, "settings.json")   # ⚙, both sides
cb.WIN_TITLE = "Cache Buddy CC"
cb.DEMO = False
cb._WinCtl.PANEL_H = 620              # room for the savings heatmap
log = cb.log
devin_cli.log = log                   # the watchdog heals Devin's route too


def load_conf():
    try:
        return json.load(open(CONF_FILE, encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_conf(d):
    try:
        os.makedirs(BUDDY_DIR, exist_ok=True)
        with open(CONF_FILE, "w", encoding="utf-8") as f:
            json.dump(d, f, indent=2)
    except OSError:
        pass


def _is_self(url, port):
    u = urlsplit(url or "")
    return (u.hostname in ("127.0.0.1", "localhost", "::1")
            and (u.port or 80) == port)


def resolve_upstream(cli_url, port):
    """Where requests really go. Once Claude Code points at us its own
    ANTHROPIC_BASE_URL is ours, so the real one is remembered in
    config.json. Order: --upstream, CC_BUDDY_UPSTREAM, settings.json env,
    process env, remembered, api.anthropic.com."""
    conf = load_conf()
    try:
        senv = json.load(open(SETTINGS, encoding="utf-8")).get("env") or {}
    except (OSError, ValueError):
        senv = {}
    for src, url in (("--upstream", cli_url),
                     ("CC_BUDDY_UPSTREAM", os.environ.get("CC_BUDDY_UPSTREAM")),
                     ("settings.json", senv.get("ANTHROPIC_BASE_URL")),
                     ("env", os.environ.get("ANTHROPIC_BASE_URL")),
                     ("config.json", conf.get("upstream"))):
        if url and not _is_self(url, port):
            url = url.rstrip("/")
            if url != conf.get("upstream"):
                conf["upstream"] = url
                save_conf(conf)
            return url, src
    return DEFAULT_UPSTREAM, "default"


# ------------------------------------------------------------- pricing

# USD per 1M tokens: (input, output). Cache read 0.1x input, 5m write
# 1.25x, 1h write 2x — Anthropic's standard multipliers. Override or add
# models in config.json: {"prices": {"claude-opus-5-5": [4, 20]}}
_PRICES = {
    "claude-opus-5-5": (4.0, 20.0),
    "claude-opus-5": (5.0, 25.0),
    "claude-opus-4": (15.0, 75.0),
    "claude-sonnet-5": (2.0, 10.0),
    "claude-sonnet-4": (3.0, 15.0),
    "claude-haiku-4-5": (1.0, 5.0),
    "claude-haiku-4": (1.0, 5.0),
}


_price_table = None


def _price_for(model):
    global _price_table
    if _price_table is None:              # config.json overrides, read once
        _price_table = dict(_PRICES)
        for k, v in (load_conf().get("prices") or {}).items():
            _price_table[k.lower()] = tuple(v)
    table = _price_table
    m = (model or "").lower().replace(".", "-")   # gateways say opus-5.5
    for k in sorted(table, key=len, reverse=True):  # longest prefix wins
        if m.startswith(k):
            return table[k]
    return None


def usage_cost(model, u):
    """(cost_with_cache, cost_without_cache) USD for one request's usage,
    or None for an unpriced model."""
    p = _price_for(model)
    if not p or not u:
        return None
    inp, out = p[0] / 1e6, p[1] / 1e6
    cr = u.get("cache_read_input_tokens") or 0
    cw = u.get("cache_creation_input_tokens") or 0
    cc = u.get("cache_creation") or {}
    cw1h = cc.get("ephemeral_1h_input_tokens") or 0
    cw5m = max(0, cw - cw1h)
    i = u.get("input_tokens") or 0
    o = u.get("output_tokens") or 0
    with_c = i * inp + o * out + cr * inp * .1 + cw5m * inp * 1.25 \
        + cw1h * inp * 2
    without = (i + cr + cw) * inp + o * out
    return with_c, without


def rescue_value(model, u):
    """USD a rescued turn saved: without Buddy its cached prefix would
    have been re-written (1.25x input) instead of read (0.1x)."""
    p = _price_for(model)
    cr = (u or {}).get("cache_read_input_tokens") or 0
    return cr * p[0] / 1e6 * (1.25 - 0.1) if p else 0.0


Ledger = cb.Ledger          # shared with Devin's monitor


def _merge_usage(dst, src):
    for k, v in (src or {}).items():
        if v is not None:
            dst[k] = v
    return dst


# ---------------------------------------------------------------- lanes

_SID_RE = re.compile(r"session_([0-9a-fA-F-]{36})")
_SUBAGENT_TOOLS = ("Agent", "Task")   # only the main thread can spawn one


def request_sid(headers, body):
    """Claude Code session id of a request: X-Claude-Code-Session-Id
    header, else the session_<uuid> inside metadata.user_id."""
    for k, v in headers.items():          # node sends header names lowercase
        if k.lower() == "x-claude-code-session-id" and v:
            return v.strip()
    uid = str(((body or {}).get("metadata") or {}).get("user_id") or "")
    m = _SID_RE.search(uid)
    if m:
        return m.group(1)
    try:
        return json.loads(uid).get("session_id") or "?"
    except (ValueError, AttributeError):
        return "?"


def _effort(body):
    """Thinking effort of a request, if the body says (output_config)."""
    oc = body.get("output_config")
    e = oc.get("effort") if isinstance(oc, dict) else None
    return e if isinstance(e, str) else None


MAX_RENEWS = 40                   # ~3h of heartbeats on one strip


class Lane:
    """One request stream inside a session — the main thread, a subagent,
    a title/classifier call. Same model + system + tools = same lane; the
    messages grow, the cacheable prefix stays."""

    def __init__(self, sid, key, model, agent):
        self.sid, self.key, self.model, self.agent = sid, key, model, agent
        self.n = 0                # real requests seen
        self.hb = 0               # heartbeats sent
        self.inflight = 0
        self.first_ts = self.last_start = time.time()
        self.refresh_ts = None    # last request that renewed the cache
        self.real_ts = None       # last real (non-heartbeat) success
        self.real_start = None    # when that request started
        self.usage = None         # usage of the last real request
        self.usage_ts = None
        self.hb_usage = None      # usage of the last heartbeat
        self.hb_ts = None
        self.renews = []          # epoch of every cache renewal (oldest
                                  # first) — the pulse strip draws these
        self.capture = None       # (path, headers, body bytes) to replay
        self.effort = None        # output_config.effort of the last request

    def note_renew(self, t):
        """Remember a cache renewal (a real turn or a heartbeat)."""
        if not t:
            return
        self.renews.append(float(t))
        if len(self.renews) > MAX_RENEWS:
            del self.renews[:-MAX_RENEWS]


class Ticket:
    __slots__ = ("lane", "t0", "path", "headers", "raw")

    def __init__(self, lane, t0, path, headers, raw):
        self.lane, self.t0 = lane, t0
        self.path, self.headers, self.raw = path, headers, raw


class Hub:
    """Everything the proxy learned, by session id."""

    def __init__(self):
        self.lock = threading.Lock()
        self.lanes = {}           # sid -> {key: Lane}
        self.reqs = 0             # /v1/messages requests proxied this run
        self.last_req_ts = None
        self.on_turn = None       # callback(lane, usage, model) per success
        self._shaped = set()

    def begin(self, path, headers, raw):
        """A /v1/messages request is going out. Returns a Ticket or None
        when the body isn't a messages request we understand."""
        try:
            body = json.loads(raw)
        except ValueError:
            return None
        if not isinstance(body, dict) or "messages" not in body:
            return None
        sid = request_sid(headers, body)
        model = body.get("model") or "?"
        tools = [t.get("name") for t in body.get("tools") or []
                 if isinstance(t, dict)]
        key = hashlib.sha1(json.dumps(
            [model, body.get("system"), tools], sort_keys=True,
            ensure_ascii=False, default=str).encode("utf-8")).hexdigest()[:12]
        agent = any(n in _SUBAGENT_TOOLS for n in tools)
        now = time.time()
        with self.lock:
            lanes = self.lanes.setdefault(sid, {})
            ln = lanes.get(key)
            if ln is None:
                ln = lanes[key] = Lane(sid, key, model, agent)
            ln.inflight += 1
            ln.last_start = now
            ln.effort = _effort(body) or ln.effort
            self.reqs += 1
            self.last_req_ts = now
            if len(self.lanes) > 64:          # forget the stalest sessions
                old = sorted(self.lanes, key=lambda s: max(
                    l.last_start for l in self.lanes[s].values()))
                for s in old[:len(self.lanes) - 64]:
                    del self.lanes[s]
        if key not in self._shaped:           # one line per lane — no
            self._shaped.add(key)             # header values, no content
            th = body.get("thinking") or {}
            log("lane %s sid=%s model=%s tools=%d agent=%s max_tokens=%s "
                "thinking=%s effort=%s stream=%s sid_hdr=%s keys=%s" % (
                    key, sid[:8], model, len(tools), agent,
                    body.get("max_tokens"),
                    "%s/%s" % (th.get("type"), th.get("budget_tokens")),
                    _effort(body), body.get("stream"),
                    any(k.lower() == "x-claude-code-session-id"
                        for k in headers),
                    ",".join(sorted(body))))
        return Ticket(ln, now, path, headers, raw)

    def end(self, tk, ok, usage, model):
        """Request finished. ok = upstream said 200 and the stream got as
        far as message_start — the prefill happened, so the cache was
        renewed (even if the user hit Esc a moment later)."""
        ln = tk.lane
        cb_ = None
        rescued = False
        with self.lock:
            ln.inflight = max(0, ln.inflight - 1)
            if ok:
                # rescued: the previous real turn's cache would have died
                # (> TTL ago), a heartbeat went out in between, and this
                # turn still read the cache — Buddy's own contribution
                prev = ln.real_start
                rescued = bool(
                    prev and tk.t0 - prev > TTL
                    and (ln.hb_ts or 0) > prev
                    and (usage or {}).get("cache_read_input_tokens"))
                ln.real_start = tk.t0
                ln.n += 1
                ln.note_renew(tk.t0)
                ln.refresh_ts = max(ln.refresh_ts or 0, tk.t0)
                ln.real_ts = time.time()
                ln.capture = (tk.path, tk.headers, tk.raw)
                if usage:
                    ln.usage, ln.usage_ts = dict(usage), time.time()
                if model:
                    ln.model = model
                cb_ = self.on_turn
        if cb_:
            try:
                cb_(ln, usage, model, rescued)
            except Exception:
                log("on_turn error:\n" + traceback.format_exc())

    def lead(self, sid):
        """The session's main-thread lane: the newest lane that can spawn
        subagents (subagents can't, title/classifier calls have no tools);
        if no lane has that tool, the one with the most requests."""
        with self.lock:
            lanes = [l for l in (self.lanes.get(sid) or {}).values()
                     if l.capture]
        if not lanes:
            return None
        main = [l for l in lanes if l.agent]
        if main:
            return max(main, key=lambda l: l.last_start)
        return max(lanes, key=lambda l: (l.n, l.last_start))

    def heartbeat_done(self, ln, t0, usage):
        with self.lock:
            ln.hb += 1
            ln.note_renew(t0)
            ln.refresh_ts = max(ln.refresh_ts or 0, t0)
            ln.hb_usage, ln.hb_ts = dict(usage or {}), time.time()

    def inflight(self):
        with self.lock:
            return sum(l.inflight for lanes in self.lanes.values()
                       for l in lanes.values())

    def sids(self):
        with self.lock:
            return set(self.lanes)


class Sniffer:
    """Watches a /v1/messages response go by: model + usage from
    message_start / message_delta (SSE), or from the JSON body."""

    def __init__(self, sse):
        self.sse = sse
        self.buf = b""
        self.started = False      # message_start seen (or JSON parsed)
        self.stopped = False      # message_stop seen — usage is final
        self.model = None
        self.usage = {}

    def feed(self, data):
        self.buf += data
        if not self.sse:
            if len(self.buf) > 4 << 20:       # not worth holding onto
                self.buf = b""
            return
        while b"\n" in self.buf:
            line, self.buf = self.buf.split(b"\n", 1)
            line = line.strip()
            if not line.startswith(b"data:") or not (
                    b"message_start" in line or b"message_delta" in line
                    or b"message_stop" in line):
                continue
            try:
                ev = json.loads(line[5:])
            except ValueError:
                continue
            if ev.get("type") == "message_start":
                msg = ev.get("message") or {}
                self.started = True
                self.model = msg.get("model") or self.model
                _merge_usage(self.usage, msg.get("usage"))
            elif ev.get("type") == "message_delta":
                _merge_usage(self.usage, ev.get("usage"))
            elif ev.get("type") == "message_stop":
                self.stopped = True

    def finish(self):
        if self.sse or not self.buf:
            return
        try:
            d = json.loads(self.buf)
        except ValueError:
            return
        if isinstance(d, dict) and d.get("type") == "message":
            self.started = True
            self.model = d.get("model")
            _merge_usage(self.usage, d.get("usage"))


# ------------------------------------------------------------ upstream

class Upstream:
    """Keep-alive connections to the real API base URL."""

    def __init__(self, base):
        u = urlsplit(base)
        self.base = base
        self.https = u.scheme != "http"
        self.host = u.hostname
        self.port = u.port or (443 if self.https else 80)
        self.prefix = u.path.rstrip("/")
        self.ctx = ssl.create_default_context() if self.https else None
        self.pool = []
        self.lock = threading.Lock()

    def new_conn(self, timeout=900):
        if self.https:
            return http.client.HTTPSConnection(
                self.host, self.port, timeout=timeout, context=self.ctx)
        return http.client.HTTPConnection(self.host, self.port,
                                          timeout=timeout)

    def open(self, method, path, headers, body):
        """Send one request; returns (conn, response). A pooled
        connection the server already dropped is retried once fresh."""
        with self.lock:
            conn = self.pool.pop() if self.pool else None
        for attempt in (0, 1):
            fresh = conn is None
            if fresh:
                conn = self.new_conn()
            try:
                conn.request(method, self.prefix + path, body=body,
                             headers=headers)
                return conn, conn.getresponse()
            except (http.client.RemoteDisconnected, ConnectionResetError,
                    BrokenPipeError, ConnectionAbortedError):
                conn.close()
                if fresh:
                    raise
                conn = None

    def release(self, conn, resp):
        if resp.will_close or not resp.isclosed():
            conn.close()
            return
        with self.lock:
            if len(self.pool) < 8:
                self.pool.append(conn)
                return
        conn.close()


HB_NOTE = ("[automated cache heartbeat from a local plugin, not from the "
           "user — reply with exactly one character: .]")


def heartbeat_body(raw):
    """The lane's last request with a one-line note appended AFTER its
    final cache breakpoint: everything Claude Code marked cacheable is
    byte-identical (so it's read, and its TTL renewed), the note is the
    only new input, and the reply is one character. model / thinking /
    max_tokens stay untouched — changing thinking would drop the cached
    messages. None when the request doesn't end on a user turn."""
    body = json.loads(raw)
    msgs = body.get("messages") or []
    if not msgs:
        return None
    if msgs[-1].get("role") != "user":
        # ends on an assistant turn: a new user turn after it leaves the
        # whole cached prefix untouched just the same
        body["messages"] = msgs + [{"role": "user", "content": [
            {"type": "text", "text": HB_NOTE}]}]
        body["stream"] = True
        return body
    last = dict(msgs[-1])
    content = last.get("content")
    if isinstance(content, str):
        content = [{"type": "text", "text": content}]
    last["content"] = list(content or []) + [
        {"type": "text", "text": HB_NOTE}]
    body["messages"] = msgs[:-1] + [last]
    body["stream"] = True
    return body


def send_heartbeat(up, hub, ln):
    """Replay the lane's last request (plus HB_NOTE) and read the short
    reply to the end — the final usage only arrives in message_delta, and
    a gateway may cancel the upstream call if we hang up early. Never
    reaches Claude Code: nothing is added to its conversation or
    transcript. Returns (ok, detail, usage)."""
    path, headers, raw = ln.capture
    body = heartbeat_body(raw)
    if body is None:
        return False, "captured request has no messages", None
    t0 = time.time()
    ok, detail, usage = _probe(up, path, headers, body)
    if ok:
        hub.heartbeat_done(ln, t0, usage)
    return ok, detail, usage


def _probe(up, path, headers, body, limit=90):
    data = json.dumps(body, ensure_ascii=False,
                      separators=(",", ":")).encode("utf-8")
    conn = up.new_conn(timeout=limit)
    try:
        conn.request("POST", up.prefix + path, body=data, headers=headers)
        resp = conn.getresponse()
        if resp.status != 200:
            err = resp.read(600).decode("utf-8", "replace")
            return False, "HTTP %d %s" % (resp.status, err.strip()), None
        sn = Sniffer(sse=True)
        deadline = time.time() + limit
        while not sn.stopped and time.time() < deadline:
            chunk = resp.read1(8192)
            if not chunk:
                break
            sn.feed(chunk)
        if not sn.started:
            return False, "no message_start", None
        return True, "ok" if sn.stopped else "ok (cut at %ds)" % limit,             sn.usage
    except Exception as e:
        return False, "%s: %s" % (type(e).__name__, e), None
    finally:
        conn.close()


# --------------------------------------------------------------- proxy

_HOP = {"connection", "keep-alive", "proxy-connection", "proxy-authorization",
        "proxy-authenticate", "te", "trailer", "trailers",
        "transfer-encoding", "upgrade", "host", "content-length",
        "accept-encoding", "content-encoding"}


class _Proxy(BaseHTTPRequestHandler):
    """Pass-through to the upstream base URL. Only /v1/messages is looked
    at; everything else (count_tokens, models, ...) just goes through.
    Credentials are forwarded as-is, kept in memory for the replay, never
    logged or written to disk."""
    protocol_version = "HTTP/1.1"
    hub = up = None                       # set by start_proxy

    def log_message(self, fmt, *args):    # no stderr spam per request
        pass

    def do_GET(self):
        if self.path.startswith("/__cc_buddy/ping"):
            return self._json(200, {"ok": True, "app": "cc-buddy"})
        self._forward()

    do_POST = do_PUT = do_PATCH = do_DELETE = do_HEAD = do_OPTIONS = \
        lambda self: self._forward()

    def _json(self, code, obj):
        data = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _read_body(self):
        n = self.headers.get("Content-Length")
        if n:
            return self.rfile.read(int(n))
        if "chunked" in (self.headers.get("Transfer-Encoding") or "").lower():
            out = b""
            while True:
                size = int(self.rfile.readline().split(b";")[0].strip(), 16)
                if not size:
                    while self.rfile.readline().strip():   # trailers
                        pass
                    return out
                out += self.rfile.read(size)
                self.rfile.readline()
        return None

    def _forward(self):
        body = self._read_body()
        if self.path.startswith("/__cc_buddy/"):
            return self._json(404, {"error": "unknown"})
        headers = [(k, v) for k, v in self.headers.items()
                   if k.lower() not in _HOP]
        headers.append(("Accept-Encoding", "identity"))  # so we can read it
        hdict = {k: v for k, v in headers}
        is_msg = (self.command == "POST" and body
                  and urlsplit(self.path).path.rstrip("/") == "/v1/messages")
        tk = self.hub.begin(self.path, hdict, body) if is_msg else None
        status, sn, done = 0, None, []

        def finish():                 # book it before the last chunk goes
            if tk and not done:       # out, so the lane is settled by the
                done.append(1)        # time Claude Code sees the end
                ok = status == 200 and bool(sn and sn.started)
                self.hub.end(tk, ok, sn.usage if sn else None,
                             sn.model if sn else None)
                if status and status != 200:
                    log("messages -> HTTP %d (sid %s)"
                        % (status, tk.lane.sid[:8]))
        try:
            try:
                conn, resp = self.up.open(self.command, self.path, hdict, body)
            except Exception as e:
                log("upstream error %s %s: %s" % (self.command, self.path, e))
                self.close_connection = True
                return self._json(502, {"type": "error", "error": {
                    "type": "api_error",
                    "message": "cc-buddy: upstream unreachable: %s" % e}})
            status = resp.status
            if tk and status == 200:
                ctype = (resp.getheader("Content-Type") or "").lower()
                sn = Sniffer(sse="event-stream" in ctype)
            self._relay(conn, resp, sn, finish)
        finally:
            finish()

    def _relay(self, conn, resp, sn, finish):
        """Stream the upstream response back chunk by chunk — SSE must
        reach Claude Code as it arrives, never buffered."""
        self.send_response_only(resp.status, resp.reason)
        for k, v in resp.getheaders():
            if k.lower() not in _HOP:
                self.send_header(k, v)
        bodyless = (self.command == "HEAD" or resp.status in (204, 304)
                    or 100 <= resp.status < 200)
        if not bodyless:
            self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        if bodyless:
            resp.read()
            self.up.release(conn, resp)
            return
        try:
            while True:
                chunk = resp.read1(65536)
                if not chunk:
                    break
                if sn:
                    sn.feed(chunk)
                self.wfile.write(b"%x\r\n%s\r\n" % (len(chunk), chunk))
                self.wfile.flush()
            if sn:
                sn.finish()
            finish()
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
        except Exception:                  # Esc in Claude Code, or upstream
            conn.close()                   # died — drop both ends
            self.close_connection = True
            return
        self.up.release(conn, resp)


def start_proxy(hub, up, port):
    """Bind 127.0.0.1 only. Raises OSError when the port is taken."""
    h = type("Proxy", (_Proxy,), {"hub": hub, "up": up})

    class Server(ThreadingHTTPServer):
        daemon_threads = True
        # Windows SO_REUSEADDR lets a second server share the port
        # silently — a second buddy must fail to bind instead
        allow_reuse_address = os.name != "nt"

        def handle_error(self, request, addr):
            # Claude Code drops idle keep-alive sockets all the time —
            # that's not worth a traceback on stderr
            e = sys.exc_info()[1]
            if not isinstance(e, (ConnectionError, TimeoutError)):
                log("proxy error from %s:\n%s" % (addr,
                                                  traceback.format_exc()))

    srv = Server(("127.0.0.1", port), h)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


# ------------------------------------------------------------ sessions

_TX = {}                  # transcript path -> parsed info, read incrementally
_TX_LOCK = threading.Lock()


def _iso_ts(s):
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except (ValueError, AttributeError):
        return None


def _prompt_text(content):
    if isinstance(content, str):
        return content
    for c in content or []:
        if isinstance(c, dict) and c.get("type") == "text":
            return c.get("text") or ""
    return ""                 # tool_result-only rows


def _scan_line(info, d):
    t = d.get("type")
    if t == "custom-title":
        info["custom"] = d.get("customTitle") or info["custom"]
    elif t == "ai-title":
        info["ai"] = d.get("aiTitle") or info["ai"]
    elif t == "summary":
        info["summary"] = d.get("summary") or info["summary"]
    elif t == "last-prompt":
        info["last_prompt"] = d.get("lastPrompt") or info["last_prompt"]
    elif t in ("user", "assistant") and not d.get("isSidechain"):
        ts = _iso_ts(d.get("timestamp"))
        if ts:
            info["last_ts"] = max(info["last_ts"], ts)
        info["cwd"] = d.get("cwd") or info["cwd"]
        msg = d.get("message") or {}
        # Claude Code writes placeholder assistant rows that no model made
        # ("No response requested." after /model, API-error stubs): model
        # "<synthetic>", every token count 0. They are not a reply and
        # must not wipe out the last real turn's usage.
        u = msg.get("usage") or {}
        real = t == "assistant" and msg.get("model") != "<synthetic>" and (
            u.get("input_tokens") or u.get("cache_read_input_tokens")
            or u.get("cache_creation_input_tokens") or u.get("output_tokens"))
        if real and ts:
            info["reply_ts"] = max(info["reply_ts"] or 0, ts)
        if real:
            info["usage"], info["model"] = u, msg.get("model")
            info["usage_ts"] = ts
            info["effort"] = (d.get("perTurnEffort") or d.get("effort")
                              or info["effort"])
        elif (t == "user" and not info["first"] and not d.get("isMeta")
              and (d.get("origin") or {}).get("kind", "human") == "human"):
            txt = _prompt_text(msg.get("content")).strip()
            if txt and not txt.startswith("<"):   # skip command wrappers
                info["first"] = txt[:80]


def transcript_info(path):
    """Title / cwd / last activity / last main-thread usage of one
    <sid>.jsonl. Only the bytes appended since the last call are read."""
    try:
        st = os.stat(path)
    except OSError:
        return None
    with _TX_LOCK:
        info = _TX.get(path)
        if info is None or st.st_size < info["off"]:     # new or rewritten
            info = _TX[path] = dict(
                off=0, custom="", ai="", summary="", first="",
                last_prompt="", cwd="", last_ts=st.st_mtime, usage=None,
                model=None, usage_ts=None, effort=None,
                reply_ts=None)
        if st.st_size == info["off"]:
            return info
        try:
            with open(path, "rb") as f:
                f.seek(info["off"])
                data = f.read()
        except OSError:
            return info
        end = data.rfind(b"\n") + 1        # leave a half-written line
        for line in data[:end].splitlines():
            try:
                d = json.loads(line)
            except ValueError:
                continue
            if isinstance(d, dict):
                _scan_line(info, d)
        info["off"] += end
        return info


def _pid_alive(pid):
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if os.name == "nt":                    # os.kill(pid, 0) is CTRL_C here
        import ctypes
        k = ctypes.windll.kernel32
        h = k.OpenProcess(0x1000, False, pid)   # QUERY_LIMITED_INFORMATION
        if not h:
            return False
        code = ctypes.c_ulong()
        ok = k.GetExitCodeProcess(h, ctypes.byref(code))
        k.CloseHandle(h)
        return bool(ok) and code.value == 259   # STILL_ACTIVE
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def live_sessions():
    """sid -> registry entry for Claude Code processes still running
    (~/.claude/sessions/<pid>.json)."""
    out = {}
    try:
        names = os.listdir(SESSIONS_DIR)
    except OSError:
        return out
    for n in names:
        if not n.endswith(".json"):
            continue
        try:
            d = json.load(open(os.path.join(SESSIONS_DIR, n),
                               encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if d.get("sessionId") and _pid_alive(d.get("pid")):
            out[d["sessionId"]] = d
    return out


def _ctx_of(u):
    """Context size of a turn: everything the prompt held — new input plus
    the cached prefix it read or wrote. None without usage."""
    if not u:
        return None
    return (int(u.get("input_tokens") or 0)
            + int(u.get("cache_read_input_tokens") or 0)
            + int(u.get("cache_creation_input_tokens") or 0))


def _title(info, sid):
    return (info.get("custom") or info.get("ai") or info.get("summary")
            or info.get("first") or info.get("last_prompt") or sid[:8])


def list_spaces(ready=(), days=7, limit=60):
    """Session tree in the same shape Devin's list_spaces returns, grouped
    by project (cwd). Items also carry `live` (process running) and
    `ready` (the proxy holds a request it can replay)."""
    live = live_sessions()
    files = []
    try:
        projs = os.listdir(PROJECTS_DIR)
    except OSError:
        projs = []
    cutoff = time.time() - days * 86400
    for p in projs:
        d = os.path.join(PROJECTS_DIR, p)
        try:
            names = os.listdir(d)
        except OSError:
            continue
        for n in names:
            if not n.endswith(".jsonl"):
                continue
            fp = os.path.join(d, n)
            try:
                mt = os.path.getmtime(fp)
            except OSError:
                continue
            sid = n[:-6]
            if mt >= cutoff or sid in live or sid in ready:
                files.append((mt, sid, fp))
    files.sort(reverse=True)
    files = [f for i, f in enumerate(files)
             if i < limit or f[1] in live or f[1] in ready]
    sess = {}
    for mt, sid, fp in files:
        info = transcript_info(fp) or {}
        reg = live.get(sid) or {}
        sess[sid] = {
            "sid": sid, "title": _title(info, sid),
            "first_msg": info.get("first") or "",
            # the last time the session replied — opening a session or
            # typing into it doesn't count, only the model answering
            "last_activity": int(info.get("reply_ts") or info.get("last_ts")
                                 or mt),
            # its context: the whole prompt of its last main-thread turn
            "ctx": _ctx_of(info.get("usage")),
            "model": info.get("model") or "",
            "cwd": info.get("cwd") or reg.get("cwd") or "",
            "live": sid in live, "ready": sid in ready, "path": fp,
            # when the last main-thread request touched the cache, from
            # the transcript (the proxy's own view wins, see CcMonitor)
            "usage_ts": info.get("usage_ts")}
    for sid, reg in live.items():           # running, nothing written yet
        if sid not in sess:
            sess[sid] = {
                "sid": sid, "title": reg.get("name") or sid[:8],
                "first_msg": "",
                "last_activity": int(int(reg.get("updatedAt") or 0) / 1000),
                "model": "", "cwd": reg.get("cwd") or "",
                "live": True, "ready": sid in ready, "path": None}
    groups = {}
    for s in sess.values():
        groups.setdefault(os.path.normcase(s["cwd"] or "?"), []).append(s)
    out = []
    for key, items in groups.items():
        items.sort(key=lambda s: -s["last_activity"])
        cwd = items[0]["cwd"]
        name = os.path.basename(cwd.rstrip("\\/")) or cwd or "(unknown)"
        out.append({"type": "group", "id": key, "name": name,
                    "collapsed": not any(s["live"] or s["ready"]
                                         or time.time() - s["last_activity"]
                                         < 86400 for s in items),
                    "items": items})
    out.sort(key=lambda g: -g["items"][0]["last_activity"])
    return out


def find_session(sid, ready=()):
    for g in list_spaces(ready):
        for s in g["items"]:
            if s["sid"] == sid:
                return s
    return None


class Guard:
    """One guarded session. Its own schedule, failure count and cycle
    totals, so several sessions can be kept warm at once."""

    def __init__(self, sid, title, first_msg="", path=None):
        self.sid, self.title, self.first_msg = sid, title, first_msg
        self.path = path
        self.last_prompt = ""             # last user message text (for hover)
        self.fails = 0
        self.manual = False               # Renew now — skip the schedule
        self.next_due = 0                 # retry floor after a failure
        self.due_at = 0
        self.injecting = False
        self.last_inject = 0
        self.cycle_since = time.time()
        self.cycle_saved = 0.0            # USD: rescues − heartbeat cost
        self.cycle_rescued = 0
        self.prev_saved = 0.0
        self.prev_rescued = 0


# ------------------------------------------------------------- monitor

class CcMonitor(threading.Thread):
    """Keeps any number of picked Claude Code sessions warm, each on its
    own schedule. Keeps the attribute names the shared panel pokes
    (idle_secs, request_inject, clear_target) plus a per-session map."""

    def __init__(self, hub, up, idle_secs, port, ledger=None):
        super().__init__(daemon=True)
        self._ledger = ledger or Ledger(LEDGER_FILE)
        self._hub, self._up = hub, up     # underscored: pywebview's js_api
        self.port = port                  # walker recurses public attrs
        self.idle_secs = idle_secs
        self.maint = cb.load_settings()["maint_min"] * 60
        self.stop_flag = threading.Event()
        self.quit_flag = threading.Event()   # ctl "quit"
        self.guards = {}                  # sid -> Guard, insertion order
        self.rows = {}                    # sid -> last row it produced
        self.round = {}                   # sid -> summary, this round:
                                          # all sessions guarded since the
                                          # guard set was last empty
        self.last_log = ""
        self.rescues = []                 # recent rescues, newest last
        self._live = {}                   # sid -> (checked_at, running)
        self.warm = {}                    # sid -> (path, title), cache alive
        self._warm_at = 0                 # last scan_warm
        hub.on_turn = self._on_turn

    # ------------------------------------------------- per-session state
    def guard(self, sid):
        return self.guards.get(sid)

    def targets(self):
        return list(self.guards.values())

    def set_target(self, s):
        """Add (or refresh) a session's guard. Several can run at once."""
        sid = s["sid"]
        if not self.guards:               # first guard after an empty set:
            self.round = {}               # a new round starts
        g = self.guards.get(sid)
        if g is None:
            g = Guard(sid, s.get("title") or sid[:8],
                      s.get("first_msg") or "", s.get("path"))
            self.guards[sid] = g
        else:
            g.path = s.get("path") or g.path
        g.fails = 0
        g.manual = False
        g.next_due = g.due_at = 0
        g.cycle_since = time.time()
        g.cycle_saved = 0.0
        g.cycle_rescued = 0
        self.rows[sid] = {"kind": "loading"}
        log("guard -> %s (%s) [%d guarded]"
            % (g.title, sid[:8], len(self.guards)))

    def clear_target(self, sid=None):
        """Stop one session's guard, or all of them."""
        if sid is None:
            for s in list(self.guards):
                self.clear_target(s)
            return
        g = self.guards.pop(sid, None)
        self.rows.pop(sid, None)
        if g:
            g.prev_saved = g.cycle_saved
            g.prev_rescued = g.cycle_rescued
            self.round[sid] = (g.path, g.title, g.prev_saved,
                               g.prev_rescued)
            log("guard stopped: %s [%d left]"
                % (g.title, len(self.guards)))

    def request_inject(self, sid=None):
        for s, g in self.guards.items():
            if sid is None or s == sid:
                g.manual = True

    def _on_turn(self, ln, usage, model, rescued=False):
        """Proxy thread: a real request on some lane just succeeded.
        Every main-thread turn feeds the ledger's cache_saved (what
        caching itself saved); a rescued one is Buddy's own saving."""
        if ln is not self._hub.lead(ln.sid):
            return                        # subagent / title call
        model = model or ln.model
        c = usage_cost(model, usage)
        kv = {"cache_saved": c[1] - c[0]} if c else {}
        if rescued:
            v = rescue_value(model, usage)
            kv.update(saved=v, rescues=1)
            g = self.guards.get(ln.sid)
            if g:
                g.cycle_saved += v
                g.cycle_rescued += 1
                self.rescues.append({"sid": ln.sid, "title": g.title,
                                     "usd": v, "at": time.time(),
                                     "seen": False})
                del self.rescues[:-12]
            log("rescued turn (%s): cache kept alive, saved $%.4f"
                % (ln.sid[:8], v))
        if kv:
            self._ledger.add(**kv)

    def _running(self, sid):
        """Is the Claude Code process for sid still up? Cached ~5s."""
        at, r = self._live.get(sid, (0, False))
        if time.time() - at > 5:
            r = sid in live_sessions()
            self._live[sid] = (time.time(), r)
        return r

    def _tick_one(self, g):
        """Run one guard's pass and remember whatever row it produced, so
        self.rows always reflects the last tick whoever drove it."""
        row = self._tick_row(g)
        if row is not None:
            self.rows[g.sid] = row
        return row

    def _tick_row(self, g):
        """This guard's row for the pass, or None when it just stopped."""
        now = time.time()
        sid = g.sid
        if g.fails >= 3:
            return {"kind": "failed"}
        ln = self._hub.lead(sid)
        if ln is None:                    # proxy hasn't seen a turn yet
            return {"kind": "loading", "reason": "no_capture"}
        if not self._running(sid):
            return {"kind": "held", "reason": "not_running"}
        if ln.inflight:                   # a turn is renewing it right now
            return {"kind": "busy"}
        idle = now - max(ln.real_ts or 0, g.cycle_since)
        if idle > self.maint and not g.manual:
            log("auto-stop: %s idle %d min" % (g.title, idle // 60))
            self.clear_target(sid)
            return None
        refresh = ln.refresh_ts or 0
        if now - refresh >= TTL:
            # the cache is already gone: a heartbeat now would only pay
            # to re-write it, which the next real turn pays anyway. Keep
            # the guard and wait — that turn renews refresh_ts and the
            # schedule picks up again on its own. Renew now included.
            g.manual = False
            g.due_at = 0
            return {"kind": "expired", "reason": "expired"}
        if g.manual:
            due = now
        else:
            due = max(refresh + self.idle_secs, g.next_due)
        if now < due:
            g.due_at = due
            return {"kind": "guarding"}
        g.due_at = 0
        g.manual = False
        g.injecting = True
        try:
            return self._fire(g, ln)
        finally:
            g.injecting = False

    def _fire(self, g, ln):
        self.last_log = log("heartbeat -> %s" % g.title)
        try:
            ok, detail, usage = send_heartbeat(self._up, self._hub, ln)
        except Exception as e:
            ok, detail, usage = False, "%s: %s" % (type(e).__name__, e), None
        now = time.time()
        if ok:
            g.fails = 0
            g.last_inject = now
            g.next_due = 0
            c = usage_cost(ln.model, usage)
            if c:
                g.cycle_saved -= c[0]         # the heartbeat isn't free
            self._ledger.add(hb=1, hb_cost=c[0] if c else 0)
            u = usage or {}
            self.last_log = log(
                "heartbeat sent -> %s ✓ read %s write %s%s" % (
                    g.title, u.get("cache_read_input_tokens"),
                    u.get("cache_creation_input_tokens"),
                    " ≈$%.4f" % c[0] if c else ""))
            return {"kind": "sent"}
        g.fails += 1
        g.next_due = now + 20             # retry soon, not a full round
        self.last_log = log("heartbeat failed -> %s: %s" % (g.title, detail))
        return {"kind": "retry", "status": detail}

    def scan_warm(self):
        """sid -> (path, title) for every session whose cache is still
        alive: the proxy's refresh time when it has one, else the
        transcript's last main-thread turn. Cheap — only transcripts
        written within the TTL are opened."""
        now = time.time()
        out = {}
        for sid in self._hub.sids():
            ln = self._hub.lead(sid)
            if ln and ln.refresh_ts and now - ln.refresh_ts < TTL:
                out[sid] = (None, sid[:8])
        try:
            projs = os.listdir(PROJECTS_DIR)
        except OSError:
            projs = []
        for p in projs:
            d = os.path.join(PROJECTS_DIR, p)
            try:
                names = os.listdir(d)
            except OSError:
                continue
            for n in names:
                if not n.endswith(".jsonl"):
                    continue
                fp = os.path.join(d, n)
                try:
                    if now - os.path.getmtime(fp) > TTL:
                        continue          # untouched since the TTL: cold
                except OSError:
                    continue
                sid = n[:-6]
                info = transcript_info(fp) or {}
                ln = self._hub.lead(sid)
                touched = (ln.refresh_ts if ln and ln.refresh_ts
                           else info.get("usage_ts"))
                if touched and now - touched < TTL:
                    out[sid] = (fp, _title(info, sid))
                elif sid in out:          # the proxy said warm; add a name
                    out[sid] = (fp, _title(info, sid))
        return out

    def pop_rescues(self):
        """Rescues the panel hasn't shown yet, oldest first."""
        return [r for r in self.rescues if not r["seen"]]

    def mark_rescues(self, upto):
        for r in self.rescues:
            if r["at"] <= upto:
                r["seen"] = True

    def run(self):
        while not self.stop_flag.is_set():
            try:
                self._ctl_poll()
                if time.time() - self._warm_at > 5:
                    self._warm_at = time.time()
                    try:
                        self.warm = self.scan_warm()
                    except Exception:
                        log("warm scan error:\n" + traceback.format_exc())
                for g in self.targets():
                    try:
                        self._tick_one(g)
                    except Exception:
                        log("tick error (%s):\n%s"
                            % (g.sid[:8], traceback.format_exc()))
                        self.rows[g.sid] = {"kind": "retry",
                                            "status": "internal error"}
            except Exception:
                log("loop error:\n" + traceback.format_exc())
            self.stop_flag.wait(POLL)

    def _ctl_poll(self):
        """buddy.ctl for scripted checks: stop [sid] | inject [sid] |
        start <title or sid prefix> | view dock|peek|panel."""
        try:
            if not os.path.exists(CTL_FILE):
                return
            cmd = open(CTL_FILE, encoding="utf-8").read().strip()
            os.remove(CTL_FILE)
        except OSError:
            return
        head, _, rest = cmd.partition(" ")
        rest = rest.strip()
        if head == "stop":
            log("ctl: stop %s" % (rest or "all"))
            self.clear_target(rest or None)
        elif head == "inject":
            log("ctl: inject %s" % (rest or "all"))
            self.request_inject(rest or None)
        elif head == "quit":              # clean exit: settings.json restored
            log("ctl: quit")
            self.quit_flag.set()
            c = getattr(self, "_winctl", None)
            if c:
                c.stop = True
                if c._halo:
                    c._halo.close()
                c.win.destroy()
        elif head == "view":
            c = getattr(self, "_winctl", None)
            if c and rest in ("dock", "peek", "panel"):
                if rest == "peek":
                    c.peek_hold(6)        # a scripted peek wants to be seen
                if rest == "panel":
                    c.open_from_tray()    # a second launch's click is outside
                else:
                    c.set_view(rest)
        elif head == "start":
            want = rest.lower()
            for grp in list_spaces(self._hub.sids()) if want else []:
                for sess in grp["items"]:
                    if (sess["sid"].startswith(want)
                            or want in sess["title"].lower()):
                        log("ctl: start -> %s" % sess["title"])
                        self.set_target(sess)
                        return
            log("ctl: no session matching %r" % want)

    def _track(self, g):
        """One cache-card row for a guarded session: the proxy's view of
        its main lane, else the transcript's last main-thread turn."""
        now = time.time()
        sid = g.sid
        ln = self._hub.lead(sid)
        info = transcript_info(g.path) if g.path else None
        effort = (ln.effort if ln else None) or (info or {}).get("effort")
        hb = 0
        if ln and (ln.usage or ln.hb_usage):
            real = ln.usage or ln.hb_usage
            seen = (ln.hb_usage if (ln.hb_ts or 0) > (ln.usage_ts or 0)
                    else real)
            model = ln.model
            age_ts = max(ln.usage_ts or 0, ln.hb_ts or 0)
            refresh = ln.refresh_ts
            this_cycle = age_ts >= g.cycle_since
            hb = ln.hb
        else:
            if not info or not info.get("usage"):
                return None
            real = seen = info["usage"]
            model = info.get("model")
            age_ts = refresh = info.get("usage_ts")
            this_cycle = False
        cr = int(seen.get("cache_read_input_tokens") or 0)
        tot = (cr + int(seen.get("input_tokens") or 0)
               + int(seen.get("cache_creation_input_tokens") or 0))
        cost = usage_cost(model, real)
        row = self.rows.get(sid) or {}
        if g.injecting:
            mode = "injecting"
        elif g.last_inject and now - g.last_inject < 4:
            mode = "sent"
        else:
            mode = row.get("kind") or "none"
        return {
            "sid": sid, "title": g.title,
            "role": "single", "model": model, "family": cb._family(model),
            "last_prompt": (g.last_prompt or "")[:80],
            "read": cr, "hit": (100.0 * cr / tot) if tot else None,
            # the size of the cached prefix the last request touched: a
            # turn that wrote a fresh cache reads 0 but still cached a lot
            "cached": cr + int(seen.get("cache_creation_input_tokens") or 0),
            "age_s": int(now - age_ts) if age_ts else None,
            "cache_left": (max(0, int(TTL - (now - refresh)))
                           if refresh else None),
            "cost": cost[0] if cost else None,
            "nocache_cost": cost[1] if cost else None,
            "this_cycle": this_cycle,
            "effort": effort,             # thinking effort: low...max
            "hb": hb,                     # silent renewals sent so far
            "renews": list(ln.renews) if ln else [],   # ECG history
            "mode": mode,
            "reason": row.get("reason") or "",
            "due_in": (max(0, int(g.due_at - now))
                       if mode == "guarding" and g.due_at else None),
            "saved": g.cycle_saved, "rescued": g.cycle_rescued,
            "waiting_first": bool(not this_cycle),
            "guarded": sid in self.guards,
        }

    def ui_state(self):
        """Same shape as cache_buddy.Monitor.ui_state, one track per
        guarded session, plus `reason`, `brand`, `link` and the rescue
        notice the panel pops the peek open for."""
        tracks = []
        for g in self.targets():
            try:
                t = self._track(g)
            except Exception:
                log("track error (%s):\n%s"
                    % (g.sid[:8], traceback.format_exc()))
                t = None
            if t:
                tracks.append(t)
        # the card: every guarded session, then — so nothing that still
        # holds a cache is hidden — every other session whose cache is
        # alive, and (once nothing is guarded) last round's sessions even
        # if their cache has since run out
        extras = []
        seen = {t["sid"] for t in tracks}

        def _extra(sid, path, title, saved=0.0, rescued=0):
            try:
                t = self._track(Guard(sid, title, "", path))
            except Exception:
                t = None
            if t:
                t["saved"], t["rescued"] = saved, rescued
                t["mode"] = "none"        # not guarded
                t["guarded"] = False
                extras.append(t)
                seen.add(sid)

        if not self.guards:
            for sid, (path, title, saved, rescued) in self.round.items():
                if sid not in seen:
                    _extra(sid, path, title, saved, rescued)
        for sid, (path, title) in self.warm.items():
            if sid not in seen:
                _extra(sid, path, title)
        extras.sort(key=lambda t: -(t["cache_left"] or 0))
        cache = None
        if tracks or extras:
            mine = tracks or [t for t in extras if t["sid"] in self.round]
            cache = {"kind": ("本轮" if tracks else
                              "上轮" if mine else "缓存中"),
                     "buddy": True, "rows": True,
                     "tracks": tracks + extras,
                     "saved": sum(t["saved"] for t in mine),
                     "rescued": sum(t["rescued"] for t in mine)}
        # the row the header describes: the one that needs attention, so a
        # session still waiting for its first proxied turn (or one that is
        # held/stopped) can't hide a session that is actively guarding
        active = ("guarding", "busy", "injecting", "sent", "retry")

        def _rank(t):
            return (0 if t["mode"] in active else 1,
                    t["cache_left"] if t["cache_left"] is not None else 1e9)

        hot = min(tracks, key=_rank, default=None)
        fresh = self.pop_rescues()
        hub = self._hub
        return {
            "mode": (hot or {}).get("mode") or "none",
            "target": ({"sid": hot["sid"], "title": hot["title"],
                        "count": len(tracks)} if hot else None),
            "status": (hot or {}).get("reason") or "",
            "reason": (hot or {}).get("reason") or "",
            "left": "",
            "due_in": (hot or {}).get("due_in"),
            "idle_secs": self.idle_secs,
            "cdp": bool(hub.reqs),
            "brand": "Cache Buddy · Claude Code",
            "link": {"label": "Proxy", "ok": bool(hub.reqs),
                     "title": "127.0.0.1:%d → %s · %s" % (
                         self.port, self._up.base,
                         "%d requests" % hub.reqs if hub.reqs
                         else "no traffic yet")},
            "last_log": self.last_log,
            "cache": cache,
            "guarded": [t["sid"] for t in tracks],   # tree marks these
            "rescued": fresh[-1] if fresh else None,   # pop-out notice
            "ledger": self._ledger.month(),      # savings heatmap
        }


# ------------------------------------------------------------------ ui

class CcApi(cb._BuddyApi):
    """pywebview js_api — Devin's, with Claude Code sessions."""

    def state(self):
        return self.mon.ui_state()

    def spaces(self):
        return list_spaces(self.mon._hub.sids())

    def start(self, sid):
        s = find_session(sid, self.mon._hub.sids())
        if not s:
            return False
        self.mon.set_target(s)
        return True

    def stop_one(self, sid):
        self.mon.clear_target(sid)

    def set_interval(self, secs):
        # the schedule runs off the last refresh, so a new interval
        # takes effect on its own — no floor to push out
        self.mon.idle_secs = int(secs)
        log("interval -> %ds" % self.mon.idle_secs)


class MultiApi(cb._BuddyApi):
    """One panel, three backends: Claude Code (proxy), Devin (CDP/UIA)
    and Droid (droid exec). Every monitor keeps running; the header
    toggle only picks which one the panel shows and drives."""
    devin = None                          # cache_buddy.Monitor, set by main
    droid = None                          # droid_buddy.DroidMonitor
    quota = None                          # quota.Poller (Devin/Droid plans)
    tokens = None                         # usage.Tracker (every harness)

    def __init__(self, mon, env):
        super().__init__(mon, env)
        self._apis = {"cc": CcApi(mon, env)}
        if self.devin is not None:
            self._apis["devin"] = cb._BuddyApi(self.devin, env)
        if self.droid is not None:
            self._apis["droid"] = droid_buddy.DroidApi(self.droid, env)
        b = load_conf().get("backend")
        self._cur = b if b in self._apis else "cc"

    def _api(self):
        return self._apis[self._cur]

    def state(self):
        s = dict(self._api().state())
        s["backend"], s["backends"] = self._cur, list(self._apis)
        s["brand"] = "Cache Buddy"
        if self.quota is not None:        # the plan of the harness shown
            s["quota"] = self.quota.state(self._cur)
        if self.tokens is not None:       # its models' token use
            s["usage"] = self.tokens.state(self._cur)
        return s

    def quota_refresh(self):
        if self.quota is not None:
            self.quota.refresh()

    def factory_fix(self):
        """Droid's "fix and restart Factory" button (see droid_buddy)."""
        a = self._apis.get("droid")
        return a.factory_fix() if a else {"ok": False, "detail": "no droid"}

    # ---- carry a session into another harness (right-click in the list)
    def carry_targets(self):
        """For the harness shown: where its sessions can be carried to
        (only targets installed here)."""
        return [t for t in bridge.targets(self._cur) if t in self._apis]

    def carry_preview(self, sid, target):
        return bridge.preview(self._cur, sid, target, load_conf())

    def carry(self, sid, target):
        """Write the new session, then show the target harness with it
        selected. Nothing is sent: it has no cache until its first turn."""
        r = bridge.carry(self._cur, sid, target, load_conf())
        if r.get("written") and target in self._apis:
            self.set_backend(target)          # its list reads the db fresh
        return r

    # ---- copy a Droid session to the other Factory account
    def droid_accounts(self):
        """Factory accounts with Buddy's letters; [] off the Droid tab."""
        a = self._apis.get("droid")
        return a.accounts() if a and self._cur == "droid" else []

    def droid_copy(self, sid, org):
        return bridge.copy_droid(sid, org, load_conf())

    # ---- Droid account switching (flyout on the harness picker)
    def droid_profiles(self):
        """Saved Factory accounts for the flyout: letter, email, current,
        has_key. [] without the Droid monitor."""
        if not self.droid:
            return []
        return droid_accounts.list_accounts(self.droid.names_file)

    def _droid_move(self, fn):
        """Run a switch/add step with the monitor told Factory is
        restarting: it holds heartbeats (no CDP error) then gives the
        window a few seconds to load."""
        m = self.droid
        if m is None:
            return {"ok": False, "error": "no_droid"}
        m.fd_restarting = True
        try:
            r = fn(m.names_file)
        finally:
            m.fd_restarting = False
        if r.get("ok") or r.get("state") == "done":
            m._fd_hold, m.fails = None, 0
            m.fd_grace = time.time() + 6
            m.next_due = 0
            if self.quota is not None:     # the new account's own plan
                self.quota.refresh()
            if self.tokens is not None:    # and its own token usage
                self.tokens.refresh()
        return r

    def droid_switch(self, name, force=False):
        return self._droid_move(
            lambda nf: droid_accounts.switch(name, force=bool(force),
                                             names_file=nf))

    def droid_add_begin(self, force=False):
        return self._droid_move(
            lambda nf: droid_accounts.begin_add(force=bool(force)))

    def droid_add_poll(self):
        m = self.droid
        if m is None:
            return {"state": "waiting"}
        r = droid_accounts.poll_add(m.names_file)
        if r.get("state") == "done":
            m._fd_hold, m.fails = None, 0
            m.fd_grace = time.time() + 6
            m.next_due = 0
            if self.quota is not None:
                self.quota.refresh()
            if self.tokens is not None:
                self.tokens.refresh()
        return r

    def droid_add_cancel(self):
        m = self.droid
        if m is None:
            return {"ok": False, "error": "no_droid"}
        return self._droid_move(
            lambda nf: droid_accounts.cancel_add(names_file=nf))

    def droid_set_key(self, org, key):
        """A Factory API key for one account (checked against whoami)."""
        r = quota.set_key(org, key)
        if r.get("ok") and self.quota is not None:
            self.quota.refresh()
        return r

    def open_url(self, url):
        """The API-keys page in the browser — the only outbound link the
        panel offers."""
        if url != "https://app.factory.ai/settings/api-keys":
            return False
        import webbrowser
        webbrowser.open(url)
        return True

    def spaces(self):
        return self._api().spaces()

    def rescan(self):
        return self._api().rescan()

    def _monitors(self):                  # one keep-alive for every side
        return [self.mon] + [m for m in (self.devin, self.droid)
                             if m is not None]

    def start(self, sid):
        return self._api().start(sid)

    def stop(self):
        return self._api().stop()

    def inject(self):
        return self._api().inject()

    def set_interval(self, secs):
        return self._api().set_interval(secs)

    def ledger_month(self, ym):
        return self._api().ledger_month(ym)

    def rescue_pop(self, at):
        self._api().rescue_pop(at)

    def stop_one(self, sid):
        self._api().stop_one(sid)

    def set_backend(self, name):
        if name not in self._apis:
            return False
        self._cur = name
        conf = load_conf()
        conf["backend"] = name
        save_conf(conf)
        log("panel -> %s" % name)
        return True


def make_devin_monitor(idle):
    """Devin's own monitor, or None when Devin isn't installed here."""
    if not os.path.exists(cb.DB):
        return None
    m = cb.Monitor(on_state=None, idle_secs=idle)
    m.on_state = lambda rows, last: setattr(m, "pending_rows", rows)
    m.attach_cli()                        # Devin CLI sessions via devb
    m.start()
    return m


# -------------------------------------------------------------- routing

def _settings_rw(fn):
    """Load ~/.claude/settings.json, let fn(d) edit it, write it back
    atomically. fn returns False to skip the write."""
    try:
        d = json.load(open(SETTINGS, encoding="utf-8"))
    except (OSError, ValueError) as e:
        log("settings.json unreadable: %s" % e)
        return False
    if fn(d) is False:
        return True
    tmp = SETTINGS + ".cc-buddy.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, indent=2, ensure_ascii=False)
    os.replace(tmp, SETTINGS)
    return True


def route_on(port):
    """Point every Claude Code launch at the proxy (settings.json env),
    remembering what was there. A leftover from a crash is kept as is."""
    me = "http://127.0.0.1:%d" % port
    conf = load_conf()

    def edit(d):
        env = d.setdefault("env", {})
        cur = env.get("ANTHROPIC_BASE_URL")
        if cur == me:
            return False
        conf["routed"] = {"had": "ANTHROPIC_BASE_URL" in env, "url": cur}
        save_conf(conf)
        shutil.copyfile(SETTINGS, SETTINGS + ".bak-cache-buddy")
        env["ANTHROPIC_BASE_URL"] = me
    ok = _settings_rw(edit)
    if ok:
        log("route on: settings.json ANTHROPIC_BASE_URL -> %s" % me)
    return ok


def route_off(port):
    """Undo route_on — only if the value is still ours (the user or a
    provider-profile switch may have changed it since)."""
    me = "http://127.0.0.1:%d" % port
    conf = load_conf()
    r = conf.pop("routed", None)

    def edit(d):
        env = d.setdefault("env", {})
        if env.get("ANTHROPIC_BASE_URL") != me:
            return False
        if r and r.get("had"):
            env["ANTHROPIC_BASE_URL"] = r.get("url")
        elif r:
            env.pop("ANTHROPIC_BASE_URL", None)
        else:                             # no record: fall back to upstream
            env["ANTHROPIC_BASE_URL"] = conf.get("upstream") \
                or DEFAULT_UPSTREAM
    ok = _settings_rw(edit)
    save_conf(conf)
    if ok:
        log("route off: settings.json restored")
    return ok


def _routed_port():
    """The port settings.json points at, when it's ours (localhost)."""
    try:
        with open(SETTINGS, encoding="utf-8") as f:
            url = ((json.load(f).get("env") or {})
                   .get("ANTHROPIC_BASE_URL") or "")
    except (OSError, ValueError):
        return None
    m = re.fullmatch(r"http://127\.0\.0\.1:(\d+)/?", url)
    return int(m.group(1)) if m else None


def heal(force=False):
    """settings.json still routes Claude Code through a buddy that isn't
    there (crash, task kill, shutdown, power cut): every request would hit
    a dead port. Put the real upstream back. force = restore even though
    the buddy still answers (it's about to die with the session)."""
    port = _routed_port()
    if port is None:
        return False
    if not force and _ping(port, 0.5):
        return False                      # alive: nothing to heal
    ok = route_off(port)
    if ok:
        log("heal: settings.json pointed at a dead buddy (:%d), restored"
            % port)
    return ok


def heal_all(force=False):
    """Both routes: Claude Code's settings.json and Devin's user env."""
    a = heal(force)
    b = devin_cli.heal(force) if os.name == "nt" else False
    return a or b


def watchdog(pid, port):
    """--watchdog: a tiny detached process that outlives the panel. When
    the panel exits without route_off (crash, End task) it heals
    settings.json; a hidden window hears shutdown/logoff, which can kill
    the panel before its own cleanup runs, and restores right then."""
    import ctypes
    from ctypes import wintypes as wt
    k32, u32 = ctypes.windll.kernel32, ctypes.windll.user32
    k32.OpenProcess.restype = wt.HANDLE
    h = k32.OpenProcess(0x00100000, False, int(pid))    # SYNCHRONIZE
    if not h:
        heal_all()                        # already gone
        return
    LRESULT = ctypes.c_ssize_t
    WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wt.HWND, wt.UINT, wt.WPARAM,
                                 wt.LPARAM)
    u32.DefWindowProcW.restype = LRESULT
    u32.DefWindowProcW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
    u32.MsgWaitForMultipleObjects.restype = wt.DWORD
    u32.MsgWaitForMultipleObjects.argtypes = [
        wt.DWORD, ctypes.POINTER(wt.HANDLE), wt.BOOL, wt.DWORD, wt.DWORD]
    u32.PeekMessageW.argtypes = [ctypes.POINTER(wt.MSG), wt.HWND, wt.UINT,
                                 wt.UINT, wt.UINT]
    k32.CloseHandle.argtypes = [wt.HANDLE]

    def proc(hwnd, msg, wp, lp):
        if msg == 0x0011:                 # WM_QUERYENDSESSION: let it go
            return 1
        if msg == 0x0016 and wp:          # WM_ENDSESSION, really ending
            heal_all(force=True)
            return 0
        return u32.DefWindowProcW(hwnd, msg, wp, lp)
    wndproc = WNDPROC(proc)               # kept alive for the loop

    class WC(ctypes.Structure):
        _fields_ = [("style", wt.UINT), ("lpfnWndProc", WNDPROC),
                    ("cbClsExtra", ctypes.c_int),
                    ("cbWndExtra", ctypes.c_int),
                    ("hInstance", wt.HINSTANCE), ("hIcon", wt.HICON),
                    ("hCursor", wt.HANDLE), ("hbrBackground", wt.HBRUSH),
                    ("lpszMenuName", wt.LPCWSTR),
                    ("lpszClassName", wt.LPCWSTR)]
    wc = WC(lpfnWndProc=wndproc, lpszClassName="CacheBuddyRouteGuard")
    u32.RegisterClassW(ctypes.byref(wc))
    u32.CreateWindowExW.restype = wt.HWND
    # a plain hidden top-level window: message-only windows don't get
    # the WM_ENDSESSION broadcast
    hwnd = u32.CreateWindowExW(0, "CacheBuddyRouteGuard",
                               "Cache Buddy route guard", 0, 0, 0, 0, 0,
                               None, None, None, None)
    msg = wt.MSG()
    hs = (wt.HANDLE * 1)(h)
    while True:
        r = u32.MsgWaitForMultipleObjects(1, hs, False, 0xFFFFFFFF,
                                          0x04FF)        # QS_ALLINPUT
        if r == 0:                        # the panel exited
            break
        while u32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 1):
            u32.TranslateMessage(ctypes.byref(msg))
            u32.DispatchMessageW(ctypes.byref(msg))
    k32.CloseHandle(h)
    if hwnd:
        u32.DestroyWindow(hwnd)
    # a clean quit already restored it — then this finds nothing to do
    heal_all()


def _spawn_watchdog(port):
    if os.name != "nt":
        return
    if getattr(sys, "frozen", False):
        cmd = [sys.executable]
    else:
        w = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
        cmd = [w if os.path.exists(w) else sys.executable,
               os.path.abspath(__file__)]
    cmd += ["--watchdog", str(os.getpid()), "--port", str(port)]
    try:
        subprocess.Popen(cmd, creationflags=0x00000008 | 0x00000200,
                         close_fds=True, stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL)
    except OSError as e:
        log("watchdog didn't start: %s" % e)


# -------------------------------------------------------------- launch

def _ping(port, timeout=1.0):
    try:
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
        c.request("GET", "/__cc_buddy/ping")
        r = c.getresponse()
        ok = r.status == 200 and b"cc-buddy" in r.read()
        c.close()
        return ok
    except (OSError, http.client.HTTPException):
        return False


def _spawn_buddy(port):
    """Start the panel detached from this console."""
    if getattr(sys, "frozen", False):
        cmd = [sys.executable]
    else:
        exe = sys.executable
        w = os.path.join(os.path.dirname(exe), "pythonw.exe")
        cmd = [w if os.path.exists(w) else exe, os.path.abspath(__file__)]
    cmd += ["--port", str(port)]
    flags = 0
    if os.name == "nt":
        flags = 0x00000008 | 0x00000200   # DETACHED_PROCESS|NEW_PROCESS_GROUP
    subprocess.Popen(cmd, creationflags=flags, close_fds=True,
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL)


def ensure(port):
    """--ensure (used by ccb.cmd): buddy up, settings overlay written.
    Prints the overlay path on success; exit 1 = run claude unproxied."""
    if not _ping(port):
        _spawn_buddy(port)
        deadline = time.time() + 10
        while time.time() < deadline and not _ping(port, 0.5):
            time.sleep(0.3)
        if not _ping(port):
            sys.stderr.write("cc-buddy: proxy didn't come up on :%d (see "
                             "%s) — running claude without it\n"
                             % (port, cb.LOG_FILE))
            return 1
    os.makedirs(BUDDY_DIR, exist_ok=True)
    with open(LAUNCH_SETTINGS, "w", encoding="utf-8") as f:
        json.dump({"env": {"ANTHROPIC_BASE_URL":
                           "http://127.0.0.1:%d" % port}}, f)
    print(LAUNCH_SETTINGS)
    return 0


def _arg(name, default=None):
    if name in sys.argv:
        i = sys.argv.index(name)
        if i + 1 < len(sys.argv):
            return sys.argv[i + 1]
    return default


def main():
    port = int(_arg("--port", PORT))
    if "--ensure" in sys.argv:
        sys.exit(ensure(port))
    if "--watchdog" in sys.argv:
        watchdog(int(_arg("--watchdog")), port)
        return
    if "--heal" in sys.argv:              # login task / manual fix
        if heal():
            print("cc-buddy: settings.json restored")
        if os.name == "nt" and devin_cli.heal():
            print("cc-buddy: %s restored" % devin_cli.ENV_KEY)
        return
    idle = int(_arg("--idle", DEFAULT_IDLE))
    base, src = resolve_upstream(_arg("--upstream"), port)
    hub, up = Hub(), Upstream(base)
    try:
        srv = start_proxy(hub, up, port)
    except OSError as e:
        running = _ping(port)
        log("port %d busy (%s)%s" % (port, e, " — buddy already running"
                                     if running else ""))
        if running and "--headless" not in sys.argv:
            # double-clicked again: bring the running panel up instead of
            # exiting without a word (its ctl poll picks this up in ~1s)
            try:
                with open(CTL_FILE, "w", encoding="utf-8") as f:
                    f.write("view panel")
            except OSError:
                pass
        sys.exit(1)
    log("cc buddy start: proxy 127.0.0.1:%d -> %s (%s), heartbeat at %ds"
        % (port, base, src, idle))
    # a command left for a buddy that's gone (`quit` sent as it exited, or
    # to one that crashed) must not land on this one before its window is up
    for stale in (CTL_FILE, cb.CTL_FILE):
        try:
            os.remove(stale)
            log("ctl: dropped a stale %s" % os.path.basename(stale))
        except OSError:
            pass
    mon = CcMonitor(hub, up, idle, port)
    mon.start()
    routed = "--no-route" not in sys.argv and route_on(port)
    MultiApi.devin = make_devin_monitor(idle)
    MultiApi.droid = droid_buddy.make_monitor(
        idle, os.path.join(BUDDY_DIR, "droid-ledger.json"))
    # plan quota for the harnesses installed here; cached numbers show at
    # once, the first fresh answer lands a second or two later
    which = tuple(n for n, m in (("devin", MultiApi.devin),
                                 ("droid", MultiApi.droid)) if m is not None)
    if which and os.name == "nt":
        quota.log = log
        quota.factory_org = droid_accounts.live_org   # quota follows the
        MultiApi.quota = quota.Poller(                # signed-in account
            os.path.join(BUDDY_DIR, "quota-cache.json"), which)
        MultiApi.quota.start()
    # token use per model for every harness here, read from their own
    # transcripts / sessions.db / settings files (no network)
    bridge.log = log
    usage.log = log
    MultiApi.tokens = usage.Tracker(
        ("cc",) + tuple(n for n, m in (("devin", MultiApi.devin),
                                       ("droid", MultiApi.droid))
                        if m is not None),
        cc_dir=PROJECTS_DIR, devin_db=cb.DB,
        droid_dir=droid_buddy.SESS_DIR,
        droid_file=os.path.join(BUDDY_DIR, "droid-usage.json"),
        cc_cost=usage_cost, cost=cb.turn_cost, family=cb._family,
        droid_org=droid_buddy.signed_in_org)
    MultiApi.tokens.start()
    # plain `devin` goes through the proxy too — only when we own its port
    dv_routed = bool("--no-route" not in sys.argv and os.name == "nt"
                     and MultiApi.devin and MultiApi.devin.cli
                     and devin_cli.route_on())
    if routed or dv_routed:
        _spawn_watchdog(port)             # restores them if we die hard
    try:
        ui = "--headless" not in sys.argv
        if ui:
            try:
                cb.run_webview(mon, idle, api_cls=MultiApi)
            except Exception as e:
                log("panel unavailable (%s) — running headless" % e)
                ui = False
        if not ui:
            if sys.stdout:
                print("cc buddy: proxy on 127.0.0.1:%d -> %s  (Ctrl+C quits)"
                      % (port, base))
            while not mon.quit_flag.wait(1):
                pass
    except KeyboardInterrupt:
        pass
    finally:
        if routed:
            route_off(port)
        if dv_routed:
            devin_cli.route_off()
        # new requests already go straight upstream (settings.json is
        # hot-reloaded); let the ones mid-stream through us finish
        dv = MultiApi.devin and MultiApi.devin.cli    # devb turns too
        busy = lambda: hub.inflight() + (dv[0].inflight() if dv else 0)
        deadline = time.time() + 60
        while busy() and time.time() < deadline:
            time.sleep(0.25)
        if busy():
            log("quit: %d request(s) still streaming, cut" % busy())
        mon.stop_flag.set()
        if MultiApi.devin:
            MultiApi.devin.stop_flag.set()
        if MultiApi.droid:
            MultiApi.droid.stop_flag.set()
        if MultiApi.quota:
            MultiApi.quota.stop_flag.set()
            MultiApi.quota.kick.set()
        if MultiApi.tokens:
            MultiApi.tokens.stop_flag.set()
            MultiApi.tokens.kick.set()
        srv.shutdown()
        log("cc buddy stop")


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except BaseException:
        # pythonw has no console: without this a startup crash leaves no
        # trace at all — the panel just never appears
        log("crash:\n" + traceback.format_exc())
        raise
