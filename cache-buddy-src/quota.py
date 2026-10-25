# -*- coding: utf-8 -*-
"""Plan quota for the panel: how much of the Devin and Factory (Droid) plans
is left, read the way each CLI's own /usage (Devin) and /status (droid) do.

Devin: windsurf_api_key from %APPDATA%\\devin\\credentials.toml (the CLI's
long-lived key, never refreshed), one Connect JSON POST to
SeatManagementService/GetUserStatus.

Factory: a Factory API key (fk-...) the user gave the panel, kept in Windows
Credential Manager (DPAPI, the user's own login), sent as a Bearer to
GET /api/billing/limits. Keys are per account: each is stored under
"CacheBuddy/factory-api-key/<orgId>" and the one matching the signed-in
account (droid_accounts.live_org) is used, so the card follows a switch.
The single original entry (".../factory-api-key") is migrated on sight to
its key's own org — nothing is deleted. droid's own login is never read:
its refresh token rotates, so spending it here would sign droid out.

Secrets stay in memory: never logged, never written to the cache. The cache
holds only percentages, times and balances, so a fresh start shows the last
numbers at once and refreshes in the background.
"""
import ctypes
import ctypes.wintypes as wt
import json
import os
import sys
import threading
import time
import tomllib
import urllib.error
import urllib.request

FACTORY_API = "https://api.factory.ai"
FACTORY_VERSION = "0.233.0"            # the droid release requests say they are
DEVIN_SERVER = "https://server.codeium.com"
DEVIN_VERSION = "3000.11.3"
CRED_TARGET = "CacheBuddy/factory-api-key"   # the first, account-less key
TIMEOUT = 15
REFRESH = 5 * 60                       # quota moves slowly; spare the limits
# the signed-in Droid account's org — cc_buddy points this at
# droid_accounts.live_org once that module is loaded
factory_org = lambda: None


def key_target(org):
    return CRED_TARGET + "/" + org


def log(msg):                          # replaced by the buddy's own log
    return msg


# ------------------------------------------------- Windows Credential Manager

class _CRED(ctypes.Structure):
    _fields_ = [("Flags", wt.DWORD), ("Type", wt.DWORD),
                ("TargetName", wt.LPWSTR), ("Comment", wt.LPWSTR),
                ("LastWritten", wt.FILETIME),
                ("CredentialBlobSize", wt.DWORD),
                ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)),
                ("Persist", wt.DWORD), ("AttributeCount", wt.DWORD),
                ("Attributes", ctypes.c_void_p),
                ("TargetAlias", wt.LPWSTR), ("UserName", wt.LPWSTR)]


_GENERIC, _PERSIST_LOCAL = 1, 2


def _advapi():
    a = ctypes.WinDLL("advapi32", use_last_error=True)
    a.CredWriteW.argtypes = [ctypes.POINTER(_CRED), wt.DWORD]
    a.CredWriteW.restype = wt.BOOL
    a.CredReadW.argtypes = [wt.LPCWSTR, wt.DWORD, wt.DWORD,
                            ctypes.POINTER(ctypes.POINTER(_CRED))]
    a.CredReadW.restype = wt.BOOL
    a.CredDeleteW.argtypes = [wt.LPCWSTR, wt.DWORD, wt.DWORD]
    a.CredDeleteW.restype = wt.BOOL
    a.CredFree.argtypes = [ctypes.c_void_p]
    return a


def cred_set(secret, target=CRED_TARGET):
    blob = secret.encode("utf-8")
    buf = (ctypes.c_ubyte * len(blob)).from_buffer_copy(blob)
    c = _CRED(Type=_GENERIC, TargetName=target, UserName="Cache Buddy",
              CredentialBlobSize=len(blob), CredentialBlob=buf,
              Persist=_PERSIST_LOCAL)
    if not _advapi().CredWriteW(ctypes.byref(c), 0):
        raise OSError(ctypes.get_last_error(), "CredWrite failed")


def cred_get(target=CRED_TARGET):
    """The stored secret, or None when there is none."""
    a, p = _advapi(), ctypes.POINTER(_CRED)()
    if not a.CredReadW(target, _GENERIC, 0, ctypes.byref(p)):
        return None
    try:
        c = p.contents
        return bytes(c.CredentialBlob[:c.CredentialBlobSize]).decode("utf-8")
    finally:
        a.CredFree(p)


def cred_delete(target=CRED_TARGET):
    return bool(_advapi().CredDeleteW(target, _GENERIC, 0))


# ------------------------------------------------------------------- parse

def _num(v):
    """A number proto-JSON gives as a number or a string; None if absent."""
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _iso(s):
    """ISO time -> epoch seconds, or None."""
    from datetime import datetime
    if not isinstance(s, str) or not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def devin_quota(ps):
    """GetUserStatus planStatus -> {plan, until, windows, extra}. JSON
    leaves a 0 out: on a quota-billed plan a quota that has a reset time
    but no share left is used up (magpie's devinQuota, same rule)."""
    info = ps.get("planInfo") or {}
    by_quota = info.get("billingStrategy") == "BILLING_STRATEGY_QUOTA"
    out = {"plan": info.get("planName") or "", "until": _iso(ps.get("planEnd")),
           "windows": [], "extra": None}
    for key, left_k, reset_k, hide_k in (
            ("day", "dailyQuotaRemainingPercent", "dailyQuotaResetAtUnix",
             "hideDailyQuota"),
            ("week", "weeklyQuotaRemainingPercent", "weeklyQuotaResetAtUnix",
             "hideWeeklyQuota")):
        left, at = _num(ps.get(left_k)), _num(ps.get(reset_k))
        if info.get(hide_k) or (left is None and not (by_quota and at)):
            continue
        out["windows"].append({"key": key, "used": min(100, max(0, 100 - (left or 0))),
                               "resets_at": at or None})
    limit = _num(ps.get("acuLimit")) or 0
    if limit > 0:
        used = _num(ps.get("acuConsumed")) or 0
        out["windows"].append({"key": "acu", "used": min(100, 100 * used / limit),
                               "resets_at": out["until"],
                               "detail": "%g / %g ACU" % (round(used, 2), round(limit, 2))})
    extra = _num(ps.get("overageBalanceMicros")) or 0
    if extra > 0:
        out["extra"] = round(extra / 1e6, 2)
    return out


def factory_quota(lim):
    """/api/billing/limits -> {plan, until, windows, extra}. Standard's
    5-hour/weekly/monthly windows; Droid Core's (open models Factory hosts)
    only once it has been used — idle, they're three 0% rows of noise."""
    out = {"plan": "", "until": None, "windows": [], "extra": None}
    pools = lim.get("limits") or {}
    for pool, core in (("standard", False), ("core", True)):
        p = pools.get(pool) or {}
        ws = [(k, p.get(n)) for k, n in (("5h", "fiveHour"), ("week", "weekly"),
                                         ("month", "monthly")) if p.get(n)]
        if core and not any((w.get("usedPercent") or 0) > 0 or w.get("windowEnd")
                            for _, w in ws):
            continue
        for k, w in ws:
            end = w.get("windowEnd")
            at = (_iso(end) if isinstance(end, str)
                  else (end / 1000 if isinstance(end, (int, float)) and end > 0 else None))
            out["windows"].append({"key": k, "core": core,
                                   "used": min(100, max(0, w.get("usedPercent") or 0)),
                                   "resets_at": at})
    cents = _num(lim.get("extraUsageBalanceCents")) or 0
    if cents > 0:
        out["extra"] = round(cents / 100, 2)
    return out


# ------------------------------------------------------------------- fetch

def _call(req):
    """(status, parsed JSON or None). Raises only on network failure."""
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            body = json.loads(e.read() or b"{}")
        except ValueError:
            body = None
        return e.code, body


def devin_credentials():
    """(key, server) from the CLI's credentials.toml, or (None, None)."""
    path = os.path.join(os.environ.get("APPDATA", ""), "devin", "credentials.toml")
    try:
        with open(path, "rb") as f:
            c = tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError):
        return None, None
    # WINDSURF_API_SERVER_URL is not honoured on purpose: while the panel
    # runs it points at the panel's own Devin CLI proxy
    server = c.get("api_server_url") or DEVIN_SERVER
    return c.get("windsurf_api_key") or None, server.rstrip("/")


def fetch_devin():
    key, server = devin_credentials()
    if not key:
        raise RuntimeError("no_login")
    body = json.dumps({"metadata": {
        "ideName": "devin-cli", "ideVersion": DEVIN_VERSION,
        "extensionName": "devin-cli", "extensionVersion": DEVIN_VERSION,
        "apiKey": key, "locale": "en", "os": "windows"}}).encode()
    req = urllib.request.Request(
        server + "/exa.seat_management_pb.SeatManagementService/GetUserStatus",
        data=body, method="POST",
        headers={"Content-Type": "application/json",
                 "Connect-Protocol-Version": "1"})
    st, js = _call(req)
    if st == 401 or (js or {}).get("code") == "unauthenticated":
        raise RuntimeError("signed_out")
    if st != 200 or not isinstance(js, dict):
        raise RuntimeError("http_%d" % st)
    return devin_quota((js.get("userStatus") or {}).get("planStatus") or {})


def _factory_headers(key):
    return {"Authorization": "Bearer " + key, "X-Factory-Client": "cli",
            "X-Client-Version": FACTORY_VERSION,
            "User-Agent": "factory-cli/" + FACTORY_VERSION,
            "Accept": "application/json"}


def whoami_org(key):
    """The orgId a Factory API key belongs to (GET /api/cli/whoami).
    bad_key on 401/403, network on anything else unusable."""
    req = urllib.request.Request(FACTORY_API + "/api/cli/whoami",
                                 headers=_factory_headers(key))
    st, js = _call(req)
    if st in (401, 403):
        raise RuntimeError("bad_key")
    if st != 200 or not isinstance(js, dict) or not js.get("orgId"):
        raise RuntimeError("network")
    return js["orgId"]


_legacy_key = []                         # [(key, org or None)] once asked


def _legacy():
    """The first account-less key, migrated on sight to its own org's
    target (whoami asked once per process). (key, org) or (None, None)."""
    if not _legacy_key:
        k = cred_get(CRED_TARGET)
        org = None
        if k:
            try:
                org = whoami_org(k)
            except RuntimeError:
                pass
            if org:                      # per-org target; legacy stays
                try:
                    cred_set(k, key_target(org))
                except OSError:
                    pass
        _legacy_key.append((k, org))
    return _legacy_key[0]


def _factory_key(org):
    """The API key for the signed-in account's org, or None."""
    if not org:
        return None
    k = cred_get(key_target(org))
    if k:
        return k
    k, lorg = _legacy()
    return k if k and lorg == org else None


def fetch_factory():
    org = factory_org()
    key = _factory_key(org)
    if not key:
        raise RuntimeError("no_key")
    req = urllib.request.Request(FACTORY_API + "/api/billing/limits",
                                 headers=_factory_headers(key))
    st, js = _call(req)
    if st in (401, 403):
        raise RuntimeError("bad_key")
    if st != 200 or not isinstance(js, dict) or not (js.get("limits") or {}).get("standard"):
        raise RuntimeError("http_%d" % st)
    out = factory_quota(js)
    out["org"] = org
    return out


def set_key(org, key):
    """Save an API key for one account. It must belong to that account —
    checked against /api/cli/whoami — so the card never shows the wrong
    plan."""
    key = (key or "").strip()
    if not key.startswith("fk-"):
        return {"ok": False, "error": "bad_format"}
    try:
        o = whoami_org(key)
    except RuntimeError as e:
        return {"ok": False, "error": str(e)}   # bad_key / network
    if o != org:
        return {"ok": False, "error": "wrong_account", "key_org": o}
    cred_set(key, key_target(org))
    return {"ok": True}


# ------------------------------------------------------------------ poller

FETCH = {"devin": fetch_devin, "droid": fetch_factory}


class Poller(threading.Thread):
    """Keeps each harness's quota fresh in the background. state() is an
    attribute read — the panel never waits on the network. The last good
    answer is cached on disk (no secrets in it) so a restart shows numbers
    at once instead of an empty strip while the first request is out."""

    def __init__(self, cache_file, which=("devin", "droid"), every=REFRESH):
        super().__init__(daemon=True)
        self.cache_file = cache_file
        self.which = which
        self.every = every
        self.stop_flag = threading.Event()
        self.kick = threading.Event()
        self.busy = False                 # a poll is in flight right now
        self.data = {}                    # name -> {..., at, error, stale}
        try:
            with open(cache_file, encoding="utf-8") as f:
                for k, v in (json.load(f) or {}).items():
                    if k in which:
                        self.data[k] = dict(v, stale=True)
        except (OSError, ValueError):
            pass

    def state(self, name):
        d = self.data.get(name)
        if name == "droid" and d and d.get("org"):
            cur = factory_org()
            if cur and d["org"] != cur:
                # the previous account's numbers: hide them and ask again
                # — once per org, not on every panel tick (a miss keeps the
                # entry stamped with the new org, so this resolves itself)
                if getattr(self, "_kicked_org", None) != cur:
                    self._kicked_org = cur
                    self.refresh()
                return None
        return d

    def refresh(self):
        """Ask again now (the panel's refresh button). A click while a poll
        is already out is dropped: it is about to answer anyway."""
        if not self.busy:
            self.kick.set()

    def poll_once(self):
        self.busy = True
        try:
            self._poll_all()
        finally:
            self.busy = False
        self._save()

    def _poll_all(self):
        for name in self.which:
            prev = self.data.get(name) or {}
            try:
                q = FETCH[name]()
                q.update(at=time.time(), error=None, stale=False)
                self.data[name] = q
            except Exception as e:        # keep the last numbers on a miss
                why = str(e) if isinstance(e, RuntimeError) else "network"
                d = dict(prev, error=why, stale=bool(prev))
                if name == "droid":
                    cur = factory_org()
                    if cur and prev.get("org") != cur:
                        # the account switched: never carry the old one's
                        # numbers — just the error, stamped with the new org
                        d = {"error": why, "org": cur, "stale": False,
                             "at": prev.get("at")}
                    else:
                        d["org"] = cur or prev.get("org")
                self.data[name] = d
                if why != (prev.get("error") or ""):
                    log("quota %s: %s" % (name, why))

    def _save(self):
        good = {k: {x: y for x, y in v.items() if x not in ("error", "stale")}
                for k, v in self.data.items() if v.get("windows") is not None
                and v.get("at")}
        try:
            os.makedirs(os.path.dirname(self.cache_file), exist_ok=True)
            tmp = self.cache_file + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(good, f)
            os.replace(tmp, self.cache_file)
        except OSError:
            pass

    def run(self):
        while not self.stop_flag.is_set():
            self.poll_once()
            self.kick.wait(self.every)
            self.kick.clear()


# ----------------------------------------------------------------- the CLI

def main(argv):
    """python quota.py set-factory-key   (reads the key from stdin)
    python quota.py clear-factory-key
    python quota.py show"""
    cmd = argv[1] if len(argv) > 1 else "show"
    if cmd == "set-factory-key":
        k = sys.stdin.readline().strip()
        if not k.startswith("fk-"):
            print("a Factory API key starts with fk-")
            return 1
        try:
            org = whoami_org(k)
        except RuntimeError as e:
            print("key not saved: %s" % e)
            return 1
        cred_set(k, key_target(org))
        print("saved to Windows Credential Manager as %s" % key_target(org))
    elif cmd == "clear-factory-key":
        print("removed" if cred_delete() else "there was none")
    elif cmd == "show":
        for name, fn in FETCH.items():
            try:
                print(name, json.dumps(fn(), ensure_ascii=False))
            except Exception as e:
                print(name, "error:", e)
    else:
        print(main.__doc__)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
