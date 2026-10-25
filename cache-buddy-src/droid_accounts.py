# -*- coding: utf-8 -*-
"""Droid (Factory) account switching: saved logins under
~/.factory-profiles, swapped into ~/.factory.

The layout is shared with the standalone droid-switch.ps1 tool so an
account saved by either side works in the other:

    ~/.factory-profiles/profiles/<name>/<auth files>   one folder per account
    ~/.factory-profiles/current.txt                  the account live now
    ~/.factory-profiles/backups/<yyyyMMdd-HHMMSS-fff>/  pre-switch copies

A profile holds Factory's auth files (whichever exist): auth.v2.keyring
is AES-256-GCM ciphertext (`iv_b64:tag_b64:ct_b64`) whose 32-byte key sits
in Windows Credential Manager as "Factory CLI/auth-encryption-key" — read
only to decode the access_token JWT's payload (email, external_org_id,
exp). Tokens never leave the machine: nothing is logged, sent or cached,
and the refresh token is never used (spending it would sign droid out).

Switching = close Factory Desktop (it holds and rewrites the files),
back up the live files, fold them back into the account they belong to
(a refreshed token would otherwise be lost), put the target's files in
and relaunch. Other droid.exe processes (a CLI running elsewhere) could
write the old account back — they block the switch unless forced.

"Add account" clears the auth files so Factory opens its sign-in page
without signing the old account out (its own Log out can revoke the
server session and invalidate the saved copy). When the new login lands
it is saved as a profile; cancelling restores the previous one.
"""
import base64
import json
import os
import re
import subprocess
import threading
import time

import cache_buddy as cb
import droid_buddy
import quota

FACTORY_DIR = droid_buddy.FACTORY_DIR
PROFILES_DIR = os.path.join(os.path.expanduser("~"), ".factory-profiles")
KEY_TARGET = "Factory CLI/auth-encryption-key"
AUTH_NAMES = ("auth.v2.keyring", "auth.v2.file", "auth.v2.key",
              "auth.v2.loginkeychain", "auth.encrypted", "auth.json")
NAME_RE = re.compile(r"^[A-Za-z0-9._@-]+$")

_lock = threading.Lock()                 # one switch/add at a time
_busy = False                            # a switch/add is mid-flight:
                                         # sync() stays out of the files
_pending = {}                            # add-account flow: {"prev": name}


def _profiles_root():
    return os.path.join(PROFILES_DIR, "profiles")


def _current_file():
    return os.path.join(PROFILES_DIR, "current.txt")


def _backups_root():
    return os.path.join(PROFILES_DIR, "backups")


def auth_files(dirpath):
    """Existing Factory auth files in a directory."""
    return [os.path.join(dirpath, n) for n in AUTH_NAMES
            if os.path.isfile(os.path.join(dirpath, n))]


def get_current():
    try:
        with open(_current_file(), encoding="utf-8") as f:
            return f.read().strip() or None
    except OSError:
        return None


def _replace(tmp, dst):
    """os.replace with a small retry — Factory or an AV scan can hold the
    file open for a moment on Windows."""
    for _ in range(5):
        try:
            os.replace(tmp, dst)
            return
        except PermissionError:
            time.sleep(0.1)
    os.replace(tmp, dst)


def _set_current(name):
    if get_current() == name:
        return
    os.makedirs(PROFILES_DIR, exist_ok=True)
    tmp = _current_file() + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(name)
    _replace(tmp, _current_file())


def _clear_current():
    try:
        os.remove(_current_file())
    except OSError:
        pass


# ------------------------------------------------------------- identity

def _enc_key():
    """The keyring's 32-byte AES key from Credential Manager, or None."""
    try:
        blob = quota.cred_get(KEY_TARGET)
    except Exception:
        return None
    if not blob:
        return None
    try:
        key = base64.b64decode(blob.strip())
    except ValueError:
        return None
    return key if len(key) == 32 else None


def _b64url(s):
    s += "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s)


_ident_cache = {}                        # path -> (mtime, size, identity)


def _read_identity(path, key):
    """auth.v2.keyring -> {email, org, exp}; None when unreadable. Only
    the JWT payload is decoded — token bytes stay inside this call."""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    try:
        with open(path, encoding="ascii") as f:
            iv_b64, tag_b64, ct_b64 = f.read().split(":")
        iv, tag, ct = (base64.b64decode(x) for x in (iv_b64, tag_b64, ct_b64))
        plain = json.loads(AESGCM(key).decrypt(iv, ct + tag, None))
        tok = plain.get("access_token") or ""
        pay = json.loads(_b64url(tok.split(".")[1])) if tok.count(".") >= 2 else {}
        org = pay.get("external_org_id") or plain.get("active_organization_id")
        email = pay.get("email") or plain.get("email")
        if not (org or email):
            return None
        return {"email": email, "org": org, "exp": pay.get("exp")}
    except Exception:
        return None


def identity(dirpath):
    """{email, org, exp} for the auth.v2.keyring in a directory, cached by
    file mtime/size (reading it decrypts, so it stays cheap)."""
    path = os.path.join(dirpath, "auth.v2.keyring")
    try:
        st = os.stat(path)
    except OSError:
        return None
    hit = _ident_cache.get(path)
    if hit and hit[0] == st.st_mtime and hit[1] == st.st_size:
        return hit[2]
    key = _enc_key()
    out = _read_identity(path, key) if key else None
    _ident_cache[path] = (st.st_mtime, st.st_size, out)
    return out


def live_org():
    """The org of the account Factory is signed in to right now."""
    i = identity(FACTORY_DIR)
    return i and i["org"]


# --------------------------------------------------------- file moves

def _copy_to_profile(name):
    """Live auth files -> profiles/<name>, replacing: files the profile
    has that the live dir doesn't are removed, and a file whose bytes
    already match is left untouched (a token that didn't change doesn't
    need a rewrite — and doesn't risk the file lock)."""
    dst = os.path.join(_profiles_root(), name)
    os.makedirs(dst, exist_ok=True)
    src_names = {os.path.basename(p) for p in auth_files(FACTORY_DIR)}
    for n in AUTH_NAMES:
        p = os.path.join(dst, n)
        if n not in src_names and os.path.isfile(p):
            os.remove(p)
    for n in src_names:
        with open(os.path.join(FACTORY_DIR, n), "rb") as f:
            data = f.read()
        out = os.path.join(dst, n)
        try:
            with open(out, "rb") as f:
                if f.read() == data:
                    continue                # identical: nothing to do
        except OSError:
            pass
        tmp = out + ".tmp"
        with open(tmp, "wb") as f:
            f.write(data)
        _replace(tmp, out)


def _backup():
    files = auth_files(FACTORY_DIR)
    if not files:
        return None
    bdir = os.path.join(_backups_root(),
                        time.strftime("%Y%m%d-%H%M%S") +
                        "-%03d" % (time.time() * 1000 % 1000))
    os.makedirs(bdir, exist_ok=True)
    for p in files:
        with open(p, "rb") as f:
            data = f.read()
        with open(os.path.join(bdir, os.path.basename(p)), "wb") as f:
            f.write(data)
    return bdir


def _profile_names():
    root = _profiles_root()
    try:
        return [d for d in os.listdir(root)
                if os.path.isdir(os.path.join(root, d))]
    except OSError:
        return []


def _sync():
    """Fold the live login back into its profile. Returns
    (name, email, org, created) or None — the live account's profile
    (matched by org, then email) gets the live files so a refreshed
    token isn't lost; an unknown account is saved under its email."""
    i = identity(FACTORY_DIR)
    if not i:
        return None
    found = None
    for name in _profile_names():
        pi = identity(os.path.join(_profiles_root(), name)) or {}
        if i["org"] and pi.get("org") == i["org"]:
            found = name
            break
        if not found and i["email"] and pi.get("email") == i["email"]:
            found = name
    created = False
    if found is None:
        name = i["email"] or ""
        if not NAME_RE.match(name):
            name = "acct-" + (i["org"] or "unknown")[:8]
        base, n = name, 2
        while name in _profile_names():
            name, n = "%s-%d" % (base, n), n + 1
        found, created = name, True
    _copy_to_profile(found)
    _set_current(found)
    return found, i["email"], i["org"], created


def sync():
    """_sync for callers outside a switch: skipped while one is moving
    the files around (it syncs itself at the right moment)."""
    with _lock:
        if _busy:
            return None
        return _sync()


# ------------------------------------------------------------- processes

def _app_dir():
    return os.path.join(os.environ.get("LOCALAPPDATA", ""), "Factory")


def _droid_procs():
    """Running droid.exe processes as [{"pid", "path"}] — one PowerShell
    query, hidden. Ones under %LOCALAPPDATA%\\Factory belong to the
    desktop app; any other is a CLI that could write the account back."""
    try:
        out = droid_buddy._ps(
            "Get-CimInstance Win32_Process -Filter \"Name='droid.exe'\" |"
            " Select-Object ProcessId, ExecutablePath |"
            " ConvertTo-Json -Compress", {})
        r = json.loads(out or "[]")
        if isinstance(r, dict):
            r = [r]
        return [{"pid": p.get("ProcessId"), "path": p.get("ExecutablePath")}
                for p in r if p.get("ProcessId")]
    except Exception:
        return []


def _foreign_droids(procs):
    app = (_app_dir() + os.sep).lower()
    return [p for p in procs
            if p.get("path") and not p["path"].lower().startswith(app)]


def _in_app_droids(procs):
    app = (_app_dir() + os.sep).lower()
    return [p for p in procs
            if p.get("path") and p["path"].lower().startswith(app)]


def _close_factory(extra_pids=()):
    """Close Factory Desktop (shared helper in droid_buddy), then the
    droid.exe processes it owns plus any extra pids the caller forced."""
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    droid_buddy.close_factory()
    pids = [p["pid"] for p in _in_app_droids(_droid_procs())]
    pids += list(extra_pids)
    for pid in pids:
        try:
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                           capture_output=True, timeout=10,
                           creationflags=flags)
        except Exception:
            pass


def _launch_factory(exe):
    """Factory Desktop again, detached; with the debug port when the
    user agreed to it so CDP heartbeats keep working."""
    args = [exe]
    if droid_buddy._consented():
        args.append("--remote-debugging-port=%d"
                    % droid_buddy._factory_ports()[0])
    subprocess.Popen(args, creationflags=0x00000008 | 0x00000200,
                     close_fds=True, stdin=subprocess.DEVNULL,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


# ------------------------------------------------------------- the API

def list_accounts(names_file):
    """Saved profiles for the flyout: {name, email, org, letter,
    current, has_key}, sorted by letter. Letters are Buddy's (the
    droid-accounts.json ones), never the folder name."""
    sync()
    live = live_org()
    rows = []
    for name in _profile_names():
        i = identity(os.path.join(_profiles_root(), name))
        if not i:
            continue
        rows.append({"name": name, "email": i["email"], "org": i["org"],
                     "current": bool(live and i["org"] == live),
                     "has_key": bool(i["org"] and
                                     quota.cred_get(quota.key_target(i["org"])))})
    orgs = [r["org"] for r in rows if r["org"]]
    letters = {a["org"]: a["name"]
               for a in droid_buddy.accounts(names_file, extra_orgs=orgs)}
    for r in rows:
        r["letter"] = letters.get(r["org"]) or "?"
    rows.sort(key=lambda r: r["letter"])
    return rows


def _letter_for(names_file, org):
    if not names_file or not org:
        return None
    for a in droid_buddy.accounts(names_file, extra_orgs=[org]):
        if a["org"] == org:
            return a["name"]
    return None


def switch(name, force=False, names_file=None):
    """Move the live login into its profile and put profile <name>'s in
    its place, relaunching Factory. {"ok", "email", "letter"} or
    {"ok": False, "error": ...}; cli_running lists foreign droid count
    and touches nothing."""
    global _busy
    if not NAME_RE.match(name or ""):
        return {"ok": False, "error": "bad_name"}
    pdir = os.path.join(_profiles_root(), name)
    if not os.path.isdir(pdir) or not auth_files(pdir):
        return {"ok": False, "error": "no_profile"}
    target = identity(pdir)
    if not target:
        return {"ok": False, "error": "no_identity"}
    foreign = _foreign_droids(_droid_procs())
    if foreign and not force:
        return {"ok": False, "error": "cli_running", "count": len(foreign)}
    exe = droid_buddy._factory_exe()       # found before anything closes
    with _lock:
        _busy = True
        try:
            _close_factory([p["pid"] for p in foreign] if force else ())
            _backup()
            _sync()                        # live token -> its profile
            for p in auth_files(FACTORY_DIR):
                os.remove(p)
            for p in auth_files(pdir):
                tmp = os.path.join(FACTORY_DIR,
                                   os.path.basename(p) + ".tmp")
                with open(p, "rb") as f:
                    data = f.read()
                with open(tmp, "wb") as f:
                    f.write(data)
                _replace(tmp, tmp[:-len(".tmp")])
            _set_current(name)
            launched = False
            if exe:
                try:
                    _launch_factory(exe)
                    launched = True
                except Exception as e:
                    cb.log("factory launch failed: %r" % e)
        finally:
            _busy = False
    r = {"ok": True, "email": target["email"], "org": target["org"],
         "letter": _letter_for(names_file, target["org"]),
         "launched": launched}
    if not launched:
        r["note"] = "open Factory yourself"
    return r


def begin_add(force=False):
    """Clear the live login so Factory opens its sign-in page — the old
    account is synced into its profile first, never signed out."""
    global _busy
    foreign = _foreign_droids(_droid_procs())
    if foreign and not force:
        return {"ok": False, "error": "cli_running", "count": len(foreign)}
    exe = droid_buddy._factory_exe()
    with _lock:
        _busy = True
        try:
            _close_factory([p["pid"] for p in foreign] if force else ())
            _backup()
            _sync()
            prev = get_current()
            prev_email = None
            if prev:
                pi = identity(os.path.join(_profiles_root(), prev))
                prev_email = pi and pi["email"]
            _pending.clear()
            _pending["prev"] = prev
            for p in auth_files(FACTORY_DIR):
                os.remove(p)
            _clear_current()
            launched = False
            if exe:
                try:
                    _launch_factory(exe)
                    launched = True
                except Exception as e:
                    cb.log("factory launch failed: %r" % e)
        finally:
            _busy = False
    r = {"ok": True, "prev_email": prev_email, "launched": launched}
    if not launched:
        r["note"] = "open Factory yourself"
    return r


def poll_add(names_file=None):
    """{"state": "waiting"} until the new login lands; then it's saved
    and {"state": "done", email, org, letter, existing} comes back."""
    global _busy
    i = identity(FACTORY_DIR)
    if not i:
        return {"state": "waiting"}
    with _lock:
        _busy = True
        try:
            got = _sync()
        finally:
            _busy = False
    name, email, org, created = got if got else (None, i["email"], i["org"], False)
    return {"state": "done", "email": email, "org": org,
            "letter": _letter_for(names_file, org),
            "existing": not created}


def cancel_add(names_file=None):
    """Nothing signed in yet: put the previous account back. Already
    signed in: keep it (it's saved by the same sync)."""
    global _busy
    prev = _pending.get("prev")
    _pending.clear()
    if not identity(FACTORY_DIR):
        if not prev:
            return {"ok": False, "error": "nothing_to_restore"}
        r = switch(prev, force=True, names_file=names_file)
        if r.get("ok"):
            r["restored"] = r.get("email")
        return r
    with _lock:
        _busy = True
        try:
            got = _sync()
        finally:
            _busy = False
    i = identity(FACTORY_DIR)
    org = (got[2] if got else None) or (i and i["org"])
    email = (got[1] if got else None) or (i and i["email"])
    return {"ok": True, "kept": True, "email": email, "org": org,
            "letter": _letter_for(names_file, org)}
