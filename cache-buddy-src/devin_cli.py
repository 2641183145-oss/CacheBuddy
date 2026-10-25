# -*- coding: utf-8 -*-
"""Devin CLI heartbeat: a 127.0.0.1 pass-through in front of Devin's API
server. Devin Desktop gets its heartbeat typed into the window; the CLI has
no window, so — like cc_buddy for Claude Code — the buddy remembers the
last GetChatMessage each session sent and, before the cache TTL runs out,
re-sends it with ONE extra user message appended after the last one. The
conversation prefix stays byte-identical, so it is read and its TTL
renewed. Never replays a turn verbatim (that re-generates the whole reply).
The heartbeat never reaches the CLI and leaves no row in sessions.db.

Wire (Connect + protobuf, learned with _devin_probe.py):
  POST /exa.api_server_pb.ApiServerService/GetChatMessage
  request  = one envelope frame; top-level field 3 = message list, each
             {1: uuid, 2: role (1 user, 2 assistant, 4 tool), 3: text};
             21 = model, 28 = session id
  response = data frames; field 7 = usage {2 input, 3 output, 5 cache
             read}; the end-of-stream frame has flag 2 (JSON, may carry
             {"error": ...})
A resized body must not keep the original Content-Length — the server
answers invalid_argument or resets the connection.

Credentials ride in the captured headers: memory only, used for the
heartbeat, never logged or written to disk. Listens on 127.0.0.1 only.

Launch:  devb [devin args...]  — sets WINDSURF_API_SERVER_URL to this proxy
"""
import gzip
import http.client
import json
import os
import re
import ssl
import subprocess
import sys
import threading
import time
import traceback
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

PORT = 47822                      # cc_buddy's proxy is 47821
DEFAULT_UPSTREAM = "https://server.codeium.com"
CHAT = "/GetChatMessage"
CFG = "/GetCliModelConfigs"
CFG_FRESH = 30 * 60               # refresh the model list in the background
HB_TEXT = ("automated heartbeat from the local cache-sentinel plugin; it only "
           "keeps the prompt cache warm and needs no action; reply with "
           "exactly one character: .")


def log(msg):                     # replaced by the buddy's own log on attach
    return msg


# ------------------------------------------------------------- protobuf

def _varint(b, i):
    v = s = 0
    while True:
        c = b[i]
        i += 1
        v |= (c & 0x7F) << s
        s += 7
        if c < 0x80:
            return v, i


def _enc_varint(v):
    out = b""
    while True:
        c = v & 0x7F
        v >>= 7
        if not v:
            return out + bytes([c])
        out += bytes([c | 0x80])


def _ld(field, payload):
    """A length-delimited protobuf field."""
    return _enc_varint(field << 3 | 2) + _enc_varint(len(payload)) + payload


def top_fields(b):
    """[(field, wiretype, start, end, value)] of a message's top level;
    value = payload bytes (wiretype 2) or int (varint), else None."""
    out, i = [], 0
    while i < len(b):
        s0 = i
        key, i = _varint(b, i)
        f, wt = key >> 3, key & 7
        val = None
        if wt == 0:
            val, i = _varint(b, i)
        elif wt == 1:
            i += 8
        elif wt == 5:
            i += 4
        elif wt == 2:
            n, i = _varint(b, i)
            val = b[i:i + n]
            i += n
        else:
            raise ValueError("wire type %d" % wt)
        if i > len(b):
            raise ValueError("truncated")
        out.append((f, wt, s0, i, val))
    return out


def frames(b):
    """Connect envelopes: flag(1) len(4) msg ... -> ([(flag, msg)], rest)
    where rest = the bytes of an incomplete trailing frame."""
    out, i = [], 0
    while i + 5 <= len(b):
        n = int.from_bytes(b[i + 1:i + 5], "big")
        if i + 5 + n > len(b):
            break
        out.append((b[i], b[i + 5:i + 5 + n]))
        i += 5 + n
    return out, b[i:]


def _plain(flag, msg):
    """Frame payload, gunzipped when the compressed bit (1) is set."""
    return gzip.decompress(msg) if flag & 1 else msg


def _text(v):
    try:
        return v.decode("utf-8") if isinstance(v, bytes) else None
    except UnicodeDecodeError:
        return None


def parse_request(body):
    """(sid, model, n_messages) of a GetChatMessage body, or None when it
    isn't one uncompressed-or-gzip envelope we can read."""
    try:
        fr, rest = frames(body)
        if len(fr) != 1 or rest or fr[0][0] & 2:
            return None
        tf = top_fields(_plain(*fr[0]))
    except (ValueError, IndexError, OSError, EOFError):
        return None
    sid = model = None
    n = 0
    for f, wt, a, b, v in tf:
        if f == 3 and wt == 2:
            n += 1
        elif f == 28 and wt == 2:
            sid = _text(v)
        elif f == 21 and wt == 2:
            model = _text(v)
    if not n:
        return None
    return sid or "?", model or "?", n


def heartbeat_body(body):
    """The captured request with one user message (HB_TEXT) right after
    its last message: every byte before it is unchanged. Re-enveloped
    uncompressed, whatever the original used."""
    fr, _ = frames(body)
    msg = _plain(*fr[0])
    last = max(e for f, wt, s, e, v in top_fields(msg) if f == 3)
    hb = (_ld(1, str(uuid.uuid4()).encode()) + _enc_varint(2 << 3)
          + _enc_varint(1) + _ld(3, HB_TEXT.encode()))
    new = msg[:last] + _ld(3, hb) + msg[last:]
    return b"\x00" + len(new).to_bytes(4, "big") + new


class Sniffer:
    """Watches a GetChatMessage response stream go by: data frames seen,
    usage (field 7), and the end-of-stream frame's error, if any."""

    def __init__(self):
        self.buf = b""
        self.data = 0             # data frames seen
        self.ended = False        # end-of-stream frame seen
        self.error = None         # its {"error": ...}, if any
        self.usage = {}

    def feed(self, chunk):
        self.buf += chunk
        fr, self.buf = frames(self.buf)
        for flag, msg in fr:
            if flag & 2:
                self.ended = True
                try:
                    err = json.loads(_plain(flag, msg) or b"{}").get("error")
                except (ValueError, OSError, EOFError, AttributeError):
                    err = None
                if err:
                    self.error = (err.get("code") or "error"
                                  if isinstance(err, dict) else str(err))
                continue
            self.data += 1
            try:
                self._usage(_plain(flag, msg))
            except (ValueError, IndexError, OSError, EOFError):
                pass                  # not a message we understand

    def _usage(self, msg):
        for f, wt, a, b, v in top_fields(msg):
            if f != 7 or wt != 2:
                continue
            u = {}
            for g, wt2, c, d, w in top_fields(v):
                k = {2: "input_tokens", 3: "output_tokens",
                     5: "cache_read_tokens"}.get(g)
                if k and wt2 == 0:
                    u[k] = w
            if u:
                self.usage.update(u)

    @property
    def ok(self):
        """The server took the request and answered: the prefill ran, so
        the cache was renewed (even if the reply was cut short)."""
        return self.data > 0 and not self.error


# ---------------------------------------------------------------- lanes

_EFF = re.compile(r"-(low|medium|high|xhigh|max|fast)$")   # = cache_buddy's


def _same_model(a, b):
    a, b = (a or "").lower(), (b or "").lower()
    return bool(a) and bool(b) and (a == b or _EFF.sub("", a) == _EFF.sub("", b))


class Lane:
    """One model's request stream inside a Devin CLI session — a fusion
    session runs a lead and a sidekick side by side."""

    def __init__(self, sid, model):
        self.sid, self.model = sid, model
        self.n = 0                # real turns seen
        self.hb = 0               # heartbeats sent
        self.inflight = 0
        self.last_start = time.time()
        self.refresh_ts = None    # last request that renewed the cache
        self.real_start = None    # start of the last real turn
        self.usage = None         # usage of the last real turn
        self.hb_ts = None         # start of the last heartbeat
        self.hb_usage = None
        self.capture = None       # (path, headers, body) to re-send


class Hub:
    """Everything the proxy learned, by Devin session id."""

    def __init__(self):
        self.lock = threading.Lock()
        self.lanes = {}           # sid -> {model: Lane}
        self.reqs = 0
        self.cfg = {}             # path -> (status, headers, data, ts)
        self.cfg_wait = {}        # path -> Event of the download running

    def begin(self, path, headers, body):
        """A GetChatMessage is going out: (lane, t0), or None when the
        body isn't one we can read (it's still forwarded as is)."""
        p = parse_request(body)
        if p is None:
            return None
        sid, model, n = p
        if sid == "?":
            # no field 28: Devin Desktop's agent (it sends no session id
            # and fresh per-request message ids) — nothing to renew, so
            # its body and credentials aren't kept
            return None
        now = time.time()
        with self.lock:
            lanes = self.lanes.setdefault(sid, {})
            ln = lanes.get(model)
            if ln is None:
                ln = lanes[model] = Lane(sid, model)
                log("devin cli lane sid=%s model=%s messages=%d bytes=%d"
                    % (sid, model, n, len(body)))
            ln.inflight += 1
            ln.last_start = now
            self.reqs += 1
            if len(self.lanes) > 64:          # forget the stalest sessions
                old = sorted(self.lanes, key=lambda s: max(
                    l.last_start for l in self.lanes[s].values()))
                for s in old[:len(self.lanes) - 64]:
                    del self.lanes[s]
        return ln, now

    def end(self, tk, ok, usage, path, headers, body):
        ln, t0 = tk
        with self.lock:
            ln.inflight = max(0, ln.inflight - 1)
            if ok:
                ln.n += 1
                ln.real_start = t0
                ln.refresh_ts = max(ln.refresh_ts or 0, t0)
                ln.capture = (path, headers, body)
                if usage:
                    ln.usage = dict(usage)

    def heartbeat_done(self, ln, t0, usage):
        with self.lock:
            ln.hb += 1
            ln.hb_ts = t0
            ln.refresh_ts = max(ln.refresh_ts or 0, t0)
            ln.hb_usage = dict(usage or {})

    def lead(self, sid, model=None):
        """The lane to renew: the one running `model` (the session's lead),
        else the one with the most turns."""
        with self.lock:
            lanes = [l for l in (self.lanes.get(sid) or {}).values()
                     if l.capture]
        if not lanes:
            return None
        hit = [l for l in lanes if _same_model(l.model, model)]
        return max(hit or lanes, key=lambda l: (l.n, l.last_start))

    def recent(self, since):
        """Sessions with a real turn through the proxy since `since`."""
        with self.lock:
            return {s for s, lanes in self.lanes.items()
                    if any((l.real_start or 0) >= since and l.capture
                           for l in lanes.values())}

    def inflight(self, sid=None):
        with self.lock:
            return sum(l.inflight for s, lanes in self.lanes.items()
                       if sid is None or s == sid for l in lanes.values())


# ------------------------------------------------------------ upstream

class Upstream:
    """The real API server. A fresh connection per request: the link to
    server.codeium.com is flaky, and a pooled socket it dropped would
    cost a turn."""

    def __init__(self, base):
        u = urlsplit(base)
        self.base = base.rstrip("/")
        self.https = u.scheme != "http"
        self.host = u.hostname
        self.port = u.port or (443 if self.https else 80)
        self.prefix = u.path.rstrip("/")
        self.ctx = ssl.create_default_context() if self.https else None

    def open(self, method, path, headers, body, timeout=900):
        if self.https:
            conn = http.client.HTTPSConnection(self.host, self.port,
                                               timeout=timeout, context=self.ctx)
        else:
            conn = http.client.HTTPConnection(self.host, self.port,
                                              timeout=timeout)
        try:
            conn.request(method, self.prefix + path, body=body,
                         headers=headers)
            return conn, conn.getresponse()
        except BaseException:
            conn.close()
            raise


def _is_self(url):
    return bool(re.fullmatch(r"http://(127\.0\.0\.1|localhost):\d+/?",
                             url or ""))


def resolve_upstream():
    """DEVIN_BUDDY_UPSTREAM, else the user's own WINDSURF_API_SERVER_URL
    from before route_on, else Devin's public API server."""
    for url in (os.environ.get("DEVIN_BUDDY_UPSTREAM"),
                (_load_route() or {}).get("url"),
                os.environ.get(ENV_KEY)):
        if url and not _is_self(url):
            return url.rstrip("/")
    return DEFAULT_UPSTREAM


# ------------------------------------------------------------- routing
# While the panel is open, the user environment (HKCU\Environment) points
# WINDSURF_API_SERVER_URL at the proxy, so a plain `devin` in any terminal
# opened from then on goes through it. Quit / crash / shutdown put it
# back (cc_buddy's watchdog heals both). A CLI already running keeps the
# URL it started with — devin has no hot reload.

ENV_KEY = "WINDSURF_API_SERVER_URL"
ROUTE_FILE = os.path.join(os.environ.get("APPDATA", ""), "devin",
                          "cache-sentinel", "route.json")


def _env_get():
    """(value, registry type) of the user variable, or (None, None)."""
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as k:
            return winreg.QueryValueEx(k, ENV_KEY)
    except FileNotFoundError:
        return None, None


def _env_set(value, kind=None):
    """Write (value) or delete (None) the user variable, then tell
    Explorer so terminals started from now on see it."""
    import winreg
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment", 0,
                        winreg.KEY_SET_VALUE) as k:
        if value is None:
            try:
                winreg.DeleteValue(k, ENV_KEY)
            except FileNotFoundError:
                pass
        else:
            winreg.SetValueEx(k, ENV_KEY, 0, kind or winreg.REG_SZ, value)
    try:
        import ctypes
        res = ctypes.c_size_t()
        ctypes.windll.user32.SendMessageTimeoutW(
            0xFFFF, 0x001A, 0, "Environment",       # HWND_BROADCAST,
            0x0002, 2000, ctypes.byref(res))        # WM_SETTINGCHANGE
    except (OSError, AttributeError):
        pass


def _load_route():
    try:
        return json.load(open(ROUTE_FILE, encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _save_route(d):
    os.makedirs(os.path.dirname(ROUTE_FILE), exist_ok=True)
    if d is None:
        try:
            os.remove(ROUTE_FILE)
        except OSError:
            pass
        return
    with open(ROUTE_FILE, "w", encoding="utf-8") as f:
        json.dump(d, f)


def route_on(port=PORT):
    """Point the user environment at the proxy, remembering what was
    there. A leftover from a crash (already ours) keeps its record."""
    me = "http://127.0.0.1:%d" % port
    try:
        cur, kind = _env_get()
        if cur == me:
            return True
        if not _is_self(cur):
            _save_route({"had": cur is not None, "url": cur, "kind": kind})
        elif _load_route() is None:       # someone's proxy, no record:
            _save_route({"had": False})   # quit removes it
        _env_set(me)
    except OSError as e:
        log("devin route on failed: %s" % e)
        return False
    log("route on: user env %s -> %s" % (ENV_KEY, me))
    return True


def route_off(port=PORT):
    """Undo route_on — only while the value is still ours."""
    me = "http://127.0.0.1:%d" % port
    r = _load_route()
    try:
        cur, _ = _env_get()
        if cur != me:
            _save_route(None)
            return False
        if r and r.get("had"):
            _env_set(r.get("url"), r.get("kind"))
        else:
            _env_set(None)
    except OSError as e:
        log("devin route off failed: %s" % e)
        return False
    _save_route(None)
    log("route off: user env %s restored" % ENV_KEY)
    return True


def heal(force=False):
    """The user environment still points devin at a proxy that isn't
    answering (crash, End task, power cut): put it back. force = restore
    even though it answers (it's about to die with the session)."""
    try:
        cur, _ = _env_get()
    except OSError:
        return False
    m = re.fullmatch(r"http://127\.0\.0\.1:(\d+)/?", cur or "")
    if not m:
        return False
    port = int(m.group(1))
    if not force and ping(port, 0.5):
        return False
    ok = route_off(port)
    if ok:
        log("heal: %s pointed at a dead buddy (:%d), restored"
            % (ENV_KEY, port))
    return ok


# --------------------------------------------------------------- proxy

# hop-by-hop, plus the ones http.client sets itself (Host, Content-Length
# — a resized heartbeat body must never carry the old length)
_HOP = {"connection", "keep-alive", "proxy-connection", "proxy-authorization",
        "proxy-authenticate", "te", "trailer", "trailers",
        "transfer-encoding", "upgrade", "host", "content-length"}


def _req_headers(items):
    """Headers to forward: hop-by-hop dropped, and the response asked for
    unencoded so its frames can be read on the way through."""
    h = {k: v for k, v in items
         if k.lower() not in _HOP and k.lower() != "accept-encoding"}
    h["Accept-Encoding"] = "identity"
    return h


def model_configs(hub, up, path, headers, body, wait=600):
    """GetCliModelConfigs from memory. The ~670KB answer crawls in at
    10-25KB/s from here and devin gives up on it after a few seconds
    ("Model list unavailable") — and each retry started yet another
    download competing with the last. One download runs to the end in the
    background whoever hangs up; callers join it, later ones get the copy
    at once, refreshed every CFG_FRESH. Returns (status, headers, data)
    or None (upstream unreachable)."""
    with hub.lock:
        hit = hub.cfg.get(path)
        ev = hub.cfg_wait.get(path)
        start = ev is None and (hit is None
                                or time.time() - hit[3] > CFG_FRESH)
        if start:
            ev = hub.cfg_wait[path] = threading.Event()
    if start:
        def fetch():
            t0 = time.time()
            res = None
            try:
                conn, resp = up.open("POST", path, headers, body, timeout=120)
                try:
                    res = (resp.status, resp.getheaders(), resp.read())
                finally:
                    conn.close()
            except Exception as e:
                log("devin cli model configs: %s" % e)
            with hub.lock:
                if res and res[0] == 200:
                    hub.cfg[path] = res + (time.time(),)
                    log("devin cli model configs cached: %d bytes in %.0fs"
                        % (len(res[2]), time.time() - t0))
                hub.cfg_wait.pop(path, None)
            ev.res = res
            ev.set()
        threading.Thread(target=fetch, daemon=True).start()
    if hit:                               # stale is fine meanwhile
        return hit[:3]
    ev.wait(wait)
    return getattr(ev, "res", None)


class _Proxy(BaseHTTPRequestHandler):
    """Pass-through to Devin's API server. GetChatMessage is watched,
    GetCliModelConfigs served from memory; the rest just goes through."""
    protocol_version = "HTTP/1.1"
    hub = up = None                       # set by start_proxy

    def log_message(self, fmt, *args):
        pass

    def do_GET(self):
        if self.path.startswith("/__devin_buddy/ping"):
            data = b'{"ok": true, "app": "devin-buddy"}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        self._forward()

    do_POST = do_PUT = do_PATCH = do_DELETE = do_HEAD = do_OPTIONS = \
        lambda self: self._forward()

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
        headers = _req_headers(self.headers.items())
        if self.command == "POST" and urlsplit(self.path).path.endswith(CFG):
            return self._model_configs(headers, body)
        is_chat = (self.command == "POST" and body
                   and urlsplit(self.path).path.endswith(CHAT))
        tk = self.hub.begin(self.path, headers, body) if is_chat else None
        status, sn, done = 0, None, []

        def finish():                 # book it before the last chunk goes
            if tk and not done:       # out, so the lane is settled by the
                done.append(1)        # time the CLI sees the end
                ok = status == 200 and bool(sn and sn.ok)
                self.hub.end(tk, ok, sn.usage if sn else None,
                             self.path, headers, body)
                if not ok:
                    log("devin cli turn not booked (sid %s): HTTP %s%s"
                        % (tk[0].sid, status or "-",
                           " " + sn.error if sn and sn.error else ""))
        try:
            try:
                conn, resp = self.up.open(self.command, self.path, headers,
                                          body)
            except Exception as e:
                log("devin cli upstream error %s %s: %s"
                    % (self.command, urlsplit(self.path).path, e))
                self.close_connection = True
                self.send_error(502, "devin-buddy: upstream unreachable")
                return
            status = resp.status
            if tk and status == 200:
                sn = Sniffer()
            self._relay(conn, resp, sn, finish)
        finally:
            finish()

    def _model_configs(self, headers, body):
        res = model_configs(self.hub, self.up, self.path, headers, body)
        if res is None:
            self.close_connection = True
            self.send_error(502, "devin-buddy: upstream unreachable")
            return
        status, hdrs, data = res
        self.send_response_only(status)
        for k, v in hdrs:
            if k.lower() not in _HOP:
                self.send_header(k, v)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _relay(self, conn, resp, sn, finish):
        """Stream the upstream response back chunk by chunk."""
        self.send_response_only(resp.status, resp.reason)
        for k, v in resp.getheaders():
            if k.lower() not in _HOP:
                self.send_header(k, v)
        bodyless = (self.command == "HEAD" or resp.status in (204, 304)
                    or 100 <= resp.status < 200)
        if not bodyless:
            self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        try:
            if bodyless:
                resp.read()
                return
            while True:
                chunk = resp.read1(65536)
                if not chunk:
                    break
                if sn:
                    sn.feed(chunk)
                self.wfile.write(b"%x\r\n%s\r\n" % (len(chunk), chunk))
                self.wfile.flush()
            finish()
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
        except Exception:                 # Ctrl+C in the CLI, or upstream
            self.close_connection = True  # died — drop both ends
        finally:
            conn.close()


def start_proxy(hub, up, port):
    """Bind 127.0.0.1 only. Raises OSError when the port is taken."""
    h = type("DevinProxy", (_Proxy,), {"hub": hub, "up": up})

    class Server(ThreadingHTTPServer):
        daemon_threads = True
        allow_reuse_address = os.name != "nt"   # a 2nd buddy must fail

        def handle_error(self, request, addr):
            e = sys.exc_info()[1]
            if not isinstance(e, (ConnectionError, TimeoutError)):
                log("devin cli proxy error:\n" + traceback.format_exc())

    srv = Server(("127.0.0.1", port), h)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


# ------------------------------------------------------------ heartbeat

def send_heartbeat(up, hub, ln, limit=120):
    """Re-send the lane's last turn plus one heartbeat message and read
    the reply to its end-of-stream frame (a gateway may cancel the call
    if we hang up early). Never reaches the CLI. A reply cut short after
    the first data frame still counts: the prefill already read — and
    renewed — the cache. Returns (ok, detail, usage)."""
    path, headers, body = ln.capture
    try:
        data = heartbeat_body(body)
    except (ValueError, IndexError, OSError, EOFError) as e:
        return False, "capture unreadable: %s" % e, None
    t0 = time.time()
    try:
        conn, resp = up.open("POST", path, headers, data, timeout=60)
    except Exception as e:
        return False, "%s: %s" % (type(e).__name__, e), None
    sn, cut = Sniffer(), None
    try:
        if resp.status != 200:
            err = resp.read(300).decode("utf-8", "replace").strip()
            return False, "HTTP %d %s" % (resp.status, err), None
        deadline = t0 + limit
        while not sn.ended and time.time() < deadline:
            chunk = resp.read1(8192)
            if not chunk:
                break
            sn.feed(chunk)
        if not sn.ended:
            cut = "cut at %ds" % (time.time() - t0)
    except Exception as e:
        cut = "%s: %s" % (type(e).__name__, e)
    finally:
        conn.close()
    if sn.error:
        return False, "server error: %s" % sn.error, None
    if not sn.data:
        return False, cut or "no reply", None
    hub.heartbeat_done(ln, t0, sn.usage)
    return True, "ok" + (" (%s)" % cut if cut else ""), sn.usage


# --------------------------------------------------------- unrouted CLIs
# A terminal opened before the panel set the env var hands every devin it
# starts the old environment: those turns go straight upstream and can't
# be renewed. Checked once at start and on the panel's refresh: an open
# session's lock (session_locks/<sid>.lock) is held by its devin process;
# Restart Manager names that process, and its own environment says
# whether it was routed. Read-only — no process is touched.

LOCKS = os.path.join(os.environ.get("APPDATA", ""), "devin", "cli",
                     "session_locks")


def _lock_holders(path):
    """pids holding a file open (Restart Manager, list only)."""
    import ctypes
    from ctypes import wintypes as wt

    class UNIQ(ctypes.Structure):
        _fields_ = [("pid", wt.DWORD), ("start", wt.FILETIME)]

    class INFO(ctypes.Structure):
        _fields_ = [("proc", UNIQ), ("app", wt.WCHAR * 256),
                    ("svc", wt.WCHAR * 64), ("type", wt.DWORD),
                    ("status", wt.ULONG), ("ts", wt.DWORD),
                    ("restartable", wt.BOOL)]
    rm = ctypes.windll.rstrtmgr
    h, key = wt.DWORD(), ctypes.create_unicode_buffer(64)
    if rm.RmStartSession(ctypes.byref(h), 0, key):
        return []
    try:
        if rm.RmRegisterResources(h, 1, (wt.LPCWSTR * 1)(path), 0, None,
                                  0, None):
            return []
        need, n, why = wt.UINT(), wt.UINT(8), wt.DWORD()
        info = (INFO * 8)()
        if rm.RmGetList(h, ctypes.byref(need), ctypes.byref(n), info,
                        ctypes.byref(why)):
            return []
        return [info[i].proc.pid for i in range(n.value)]
    finally:
        rm.RmEndSession(h)


def _proc_env(pid, name):
    """(exe path, value of ONE environment variable) of another process —
    the rest of its environment is never kept. (None, None) when it can't
    be read (gone, elevated, ...)."""
    import ctypes
    from ctypes import wintypes as wt
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.OpenProcess.restype = wt.HANDLE
    k32.ReadProcessMemory.argtypes = [wt.HANDLE, ctypes.c_void_p,
                                      ctypes.c_void_p, ctypes.c_size_t,
                                      ctypes.POINTER(ctypes.c_size_t)]
    k32.QueryFullProcessImageNameW.argtypes = [
        wt.HANDLE, wt.DWORD, wt.LPWSTR, ctypes.POINTER(wt.DWORD)]
    h = k32.OpenProcess(0x0400 | 0x0010, False, pid)  # QUERY_INFO|VM_READ
    if not h:
        return None, None

    def read(addr, n):
        buf, got = ctypes.create_string_buffer(n), ctypes.c_size_t()
        if not k32.ReadProcessMemory(h, ctypes.c_void_p(addr), buf, n,
                                     ctypes.byref(got)):
            raise OSError("read")
        return buf.raw[:got.value]
    try:
        exe = ctypes.create_unicode_buffer(1024)
        sz = wt.DWORD(1024)
        k32.QueryFullProcessImageNameW(h, 0, exe, ctypes.byref(sz))
        pbi = (ctypes.c_void_p * 6)()     # PROCESS_BASIC_INFORMATION
        if ctypes.windll.ntdll.NtQueryInformationProcess(
                h, 0, pbi, ctypes.sizeof(pbi), None):
            return exe.value, None
        q = lambda a: int.from_bytes(read(a, 8), "little")   # 64-bit PEB
        params = q(pbi[1] + 0x20)         # PEB.ProcessParameters
        env, size = q(params + 0x80), q(params + 0x3F0)
        block = read(env, min(size or 65536, 1 << 20)).decode(
            "utf-16-le", "replace")
        key = name.lower() + "="
        val = next((x[len(key):] for x in block.split("\0")
                    if x.lower().startswith(key)), "")
        return exe.value, val
    except (OSError, ValueError):
        return exe.value, None
    finally:
        k32.CloseHandle(h)


def scan_unrouted(port=PORT):
    """{sid: pid} of sessions open in a terminal devin that does NOT go
    through this proxy. Desktop's own agent is skipped — Desktop sessions
    are renewed in their window, the env var doesn't matter there."""
    me = "http://127.0.0.1:%d" % port
    out = {}
    try:
        names = [f for f in os.listdir(LOCKS) if f.endswith(".lock")]
    except OSError:
        return out
    for f in names:
        path = os.path.join(LOCKS, f)
        try:
            with open(path, "rb") as fh:  # a byte-range lock: opening
                fh.read(1)                # works, reading doesn't
            continue                      # readable = nobody holds it
        except PermissionError:
            pass
        except OSError:
            continue
        for pid in _lock_holders(path):
            exe, val = _proc_env(pid, ENV_KEY)
            if not exe or "\\extensions\\windsurf\\" in exe.lower():
                continue                  # unreadable, or Desktop's agent
            if val is not None and val.rstrip("/") != me:
                out[f[:-5]] = pid
    return out


def lock_holder_kinds():
    """{sid: "desktop"|"cli"} — which devin.exe holds each open session's
    lock. \\extensions\\windsurf\\ in the holder's exe path is Desktop's own
    agent; any other devin.exe is a terminal. Same read-only scan as
    scan_unrouted."""
    out = {}
    try:
        names = [f for f in os.listdir(LOCKS) if f.endswith(".lock")]
    except OSError:
        return out
    for f in names:
        path = os.path.join(LOCKS, f)
        try:
            with open(path, "rb") as fh:
                fh.read(1)
            continue                      # readable = nobody holds it
        except PermissionError:
            pass
        except OSError:
            continue
        for pid in _lock_holders(path):
            exe, _val = _proc_env(pid, ENV_KEY)
            if not exe:
                continue
            out[f[:-5]] = ("desktop" if "\\extensions\\windsurf\\"
                           in exe.lower() else "cli")
            break
    return out


# -------------------------------------------------------------- launch

def ping(port=PORT, timeout=1.0):
    try:
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
        c.request("GET", "/__devin_buddy/ping")
        r = c.getresponse()
        ok = r.status == 200 and b"devin-buddy" in r.read()
        c.close()
        return ok
    except (OSError, http.client.HTTPException):
        return False


def _spawn_buddy():
    """Start the Cache Buddy panel (cc_buddy.py, which runs Devin's
    monitor and this proxy too) detached from the console."""
    here = os.path.dirname(os.path.abspath(__file__))
    exe = sys.executable
    w = os.path.join(os.path.dirname(exe), "pythonw.exe")
    cmd = [w if os.path.exists(w) else exe, os.path.join(here, "cc_buddy.py")]
    flags = 0x00000008 | 0x00000200 if os.name == "nt" else 0
    subprocess.Popen(cmd, creationflags=flags, close_fds=True,
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL)


def ensure(port=PORT):
    """--ensure (used by devb.cmd): buddy up, prints the proxy URL; exit 1
    = run devin without it."""
    if not ping(port):
        _spawn_buddy()
        deadline = time.time() + 15
        while time.time() < deadline and not ping(port, 0.5):
            time.sleep(0.3)
        if not ping(port):
            sys.stderr.write("devin-buddy: proxy didn't come up on :%d — "
                             "running devin without it\n" % port)
            return 1
    print("http://127.0.0.1:%d" % port)
    return 0


if __name__ == "__main__":
    if "--ensure" in sys.argv:
        sys.exit(ensure())
