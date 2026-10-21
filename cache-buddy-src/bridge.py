# -*- coding: utf-8 -*-
"""Carry a session from one harness into another (Droid -> Devin for now),
through the user's session-bridge package. Cache Buddy only calls its api
(preview / carry); the conversion itself lives there.

Where session-bridge is looked for, first hit wins:
  1. "session_bridge" in ~/.claude/cache-buddy/config.json (its folder)
  2. a "session-bridge" folder next to cache-buddy-src (the dev layout)
  3. an installed `sessionbridge` package

Directions: both. Devin -> Droid was off while a carried session lacked
session_start.organizationId (droid then backfilled it with a tmp+rename
that fails on Windows and rolled the user's first message back); the
bridge writes it since 2026-10-18.

After a carry nothing is sent: a carried session has no prompt cache until
its first real turn, so a heartbeat would only pay to build one early.
The guard picks it up once the user replies.
"""
import os
import sys
import threading

HERE = os.path.dirname(os.path.abspath(__file__))
DIRECTIONS = {("droid", "devin"), ("devin", "droid")}   # offered in the menu
_LOCK = threading.Lock()              # one carry at a time
_api = None
_why = None


def log(msg):                         # replaced by the buddy's own log
    return msg


def _candidates(conf):
    p = (conf or {}).get("session_bridge")
    if p:
        yield p
    yield os.path.join(os.path.dirname(HERE), "session-bridge")


def load(conf=None):
    """session-bridge's api module, or None (and why in `reason()`)."""
    global _api, _why
    if _api is not None:
        return _api
    for d in _candidates(conf):
        if os.path.isfile(os.path.join(d, "sessionbridge", "api.py")):
            if d not in sys.path:
                sys.path.insert(0, d)
            break
    try:
        from sessionbridge import api      # noqa: E402
    except Exception as e:                 # not there, or too old
        _why = "%s: %s" % (type(e).__name__, e)
        return None
    _api = api
    return api


def reason():
    return _why


def targets(source):
    """The harnesses a session of `source` can be carried to."""
    return [t for s, t in sorted(DIRECTIONS) if s == source]


def _plan_view(p):
    """What the panel needs from a preview/carry dict (JSON-safe)."""
    keep = ("source", "target", "before", "after", "trimmed_calls",
            "warnings", "interrupted_calls", "written", "dry_run")
    return {k: p.get(k) for k in keep if k in p}


def preview(source, sid, target, conf=None):
    if (source, target) not in DIRECTIONS:
        return {"error": "unsupported"}
    api = load(conf)
    if api is None:
        return {"error": "no_bridge", "detail": _why}
    try:
        return _plan_view(api.preview(source, sid, target))
    except Exception as e:
        log("carry preview %s %s: %s: %s" % (source, sid, type(e).__name__, e))
        return {"error": "failed", "detail": str(e)}


def copy_droid(sid, org, conf=None):
    """Copy a Droid session to another Factory account (org) as a new
    session; the original stays. It shows up once that account is
    signed in."""
    api = load(conf)
    if api is None:
        return {"error": "no_bridge", "detail": _why}
    if not hasattr(api, "droid_copy"):
        return {"error": "failed", "detail": "session-bridge too old"}
    if not _LOCK.acquire(blocking=False):
        return {"error": "busy"}
    try:
        c = api.droid_copy(sid, org)
        log("droid copy %s -> %s (account %s)" % (sid, c["session_id"], org))
        return {"session_id": c["session_id"]}
    except Exception as e:
        log("droid copy %s: %s: %s" % (sid, type(e).__name__, e))
        return {"error": "failed", "detail": str(e)}
    finally:
        _LOCK.release()


def carry(source, sid, target, conf=None):
    if (source, target) not in DIRECTIONS:
        return {"error": "unsupported"}
    api = load(conf)
    if api is None:
        return {"error": "no_bridge", "detail": _why}
    if not _LOCK.acquire(blocking=False):
        return {"error": "busy"}
    try:
        p = _plan_view(api.carry(source, sid, target))
        w = p.get("written") or {}
        log("carry %s %s -> %s %s (%s turns)" % (
            source, sid, target, w.get("session_id"),
            (p.get("after") or {}).get("user_messages")))
        return p
    except Exception as e:
        log("carry %s %s: %s: %s" % (source, sid, type(e).__name__, e))
        return {"error": "failed", "detail": str(e)}
    finally:
        _LOCK.release()
