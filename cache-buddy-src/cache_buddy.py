# -*- coding: utf-8 -*-
"""Devin Cache Buddy — floating monitor + UIA heartbeat injector.

Watches sessions.db for sessions with recent real user activity (cache warm).
When a warm session's prompt-cache TTL is about to expire (~45s before 300s),
injects a heartbeat message into the open Devin Desktop window via UI
Automation — the client itself runs the turn, so no session-lock conflict.

Run:  pythonw cache_buddy.py      (or DevinCacheBuddy.exe)
Test: python  cache_buddy.py --inject "Anything"
"""
import json
import os
import re
import sys
import time
import sqlite3
import threading
import traceback
from datetime import datetime, timezone

import devin_cli                  # CLI sessions: proxy + heartbeat

DB = os.path.join(os.environ.get("APPDATA", ""), "devin", "cli", "sessions.db")
TTL = 300               # server-side prompt-cache lifetime, seconds
INJECT_BEFORE = 45      # inject this many seconds before expiry (idle ~255s)
COVERAGE = 30 * 60      # stop watching a session 30min after last real user msg
POLL = 5                # monitor loop interval
HEARTBEAT = ("automated heartbeat from the local cache-sentinel plugin; "
             "renew this session's prompt-cache TTL; this is NOT a user "
             "request and needs no action; reply with exactly one "
             "character: .")
HEARTBEAT_MARK = "automated heartbeat"
LOG_FILE = os.path.join(os.environ.get("APPDATA", ""), "devin",
                        "cache-sentinel", "buddy.log")
CTL_FILE = os.path.join(os.path.dirname(LOG_FILE), "buddy.ctl")
# fixed here, not derived from LOG_FILE at use time: cc_buddy repoints
# LOG_FILE at import, and Devin's savings must not land in Claude's ledger
LEDGER_FILE = os.path.join(os.path.dirname(LOG_FILE), "ledger.json")
# the panel's settings (⚙): language + how long a session is kept warm.
# cc_buddy repoints it — one panel, one settings file for both backends
SETTINGS_FILE = os.path.join(os.path.dirname(LOG_FILE), "settings.json")
MAINT_CHOICES = (15, 30, 45)      # 缓存维护时间, minutes
SETTINGS_DEFAULT = {"lang": "en", "maint_min": 30}


def load_settings():
    try:
        d = json.load(open(SETTINGS_FILE, encoding="utf-8"))
    except (OSError, ValueError):
        d = {}
    out = dict(SETTINGS_DEFAULT)
    if d.get("lang") in ("zh", "en"):
        out["lang"] = d["lang"]
    if d.get("maint_min") in MAINT_CHOICES:
        out["maint_min"] = d["maint_min"]
    if d.get("onboarded") is True:    # the first-launch hint was dismissed
        out["onboarded"] = True
    if d.get("factory_cdp") is True:  # agreed: Factory shortcuts keep the
        out["factory_cdp"] = True     # debug port (droid_buddy)
    return out


def save_settings(d):
    try:
        os.makedirs(os.path.dirname(SETTINGS_FILE), exist_ok=True)
        tmp = SETTINGS_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(d, f, indent=1)
        os.replace(tmp, SETTINGS_FILE)
    except OSError as e:
        log("settings write failed: %s" % e)

# ---------------------------------------------------------------- db helpers

def log(msg):
    line = time.strftime("%H:%M:%S ") + msg
    try:
        os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass
    return line


_FIRST_MSG_CACHE = {}


def _first_user_msg(db, sid):
    """First real user message — Devin derives the window title from it,
    which can differ from sessions.title after renaming."""
    if sid in _FIRST_MSG_CACHE:
        return _FIRST_MSG_CACHE[sid]
    msg = ""
    try:
        r = db.execute(
            """SELECT chat_message FROM message_nodes
                WHERE session_id = ? AND chat_message LIKE '%"role":"user"%'
                ORDER BY created_at ASC LIMIT 1""", (sid,)).fetchone()
        if r:
            import json
            msg = (json.loads(r[0]).get("content") or "")[:80]
    except Exception:
        pass
    _FIRST_MSG_CACHE[sid] = msg
    return msg


STATE_DB = os.path.join(os.environ.get("APPDATA", ""),
                        "Devin", "User", "globalStorage", "state.vscdb")
_spaces_warned = False


_REPLY_CACHE = {}                         # sid -> (last_activity_at, out)
_DESK_SIDS = (0, None)                    # (read at, sids Desktop shows)
_HELD_SIDS = (0, {})                      # (read at, {sid: holder kind})


def session_source(sid):
    """"desktop" / "cli" / None (not known yet). Attribute read only — the
    sets are refreshed by list_spaces and the warm-scan worker. Who holds
    the session's lock wins (a Desktop session resumed in a terminal is
    cli, whatever its origin); the origin set says where it was made."""
    held = _HELD_SIDS[1].get(sid)
    if held:
        return held
    desk = _DESK_SIDS[1]
    if desk is None:
        return None
    return "desktop" if sid in desk else "cli"


def _reply_info(db, sid, la):
    """(epoch of the last real assistant reply, context tokens of the
    newest turn) — heartbeat answers don't count as replies. Cached until
    the session's last_activity_at moves."""
    hit = _REPLY_CACHE.get(sid)
    if hit and hit[0] == la:
        return hit[1]
    inner = "json_extract(chat_message,'$.metadata.created_at')"
    mt = "json_extract(chat_message,'$.metadata.metrics.%s')"
    rows = db.execute(
        "SELECT * FROM (SELECT json_extract(chat_message,'$.role'),"
        "       CASE WHEN json_extract(chat_message,'$.role')='user'"
        "            THEN chat_message LIKE ? END,"
        f"      {inner}, {mt % 'input_tokens'},"
        f"      {mt % 'cache_read_tokens'}, {mt % 'cache_creation_tokens'}"
        "  FROM message_nodes WHERE session_id=?"
        "   AND json_extract(chat_message,'$.role') IN ('user','assistant')"
        f" ORDER BY {inner} DESC LIMIT 400) ORDER BY 3",
        ("%" + HEARTBEAT_MARK + "%", sid)).fetchall()
    reply, ctx, hb = None, None, False
    for role, is_hb, ts, i, cr, cw in rows:
        if role == "user":
            hb = bool(is_hb)
            continue
        if i is None and cr is None:
            continue                      # no metrics: not a finished turn
        ctx = (i or 0) + (cr or 0) + (cw or 0)   # a heartbeat reads it too
        if not hb:
            reply = _inner_ts({"created_at": ts}) or reply
    out = (reply, ctx or None)
    _REPLY_CACHE[sid] = (la, out)
    return out


def list_spaces(state_db=None):
    """Sidebar "Spaces" grouping, mirroring Devin's own sidebar:
    [{"type": "group", "id": spaceId, "name": <space title else first
      item's title>, "collapsed": bool, "items": [S, ...]}, ...,
     {"type": "session", **S}, ...]
    where S = {sid, title, first_msg, last_activity, model}.
    Groups = spaces flagged isGrouped in state.vscdb that still have a
    visible session, ordered by spaceId (its ms prefix = creation time),
    items in resourceToSpace order. Every other visible session goes
    top-level after the groups, newest activity first."""
    state_db = state_db or STATE_DB
    import json
    if not os.path.exists(DB):
        return []
    try:
        db = sqlite3.connect("file:%s?mode=ro" % DB, uri=True)
        rows = db.execute(
            "SELECT id, title, last_activity_at, model FROM sessions"
            " WHERE hidden = 0").fetchall()
        info = {}
        for sid, title, la, model in rows:
            la = la or 0
            if la > 1e12:                       # tolerate ms epochs
                la //= 1000
            info[sid] = {"sid": sid, "title": title or sid,
                         "first_msg": _first_user_msg(db, sid),
                         "last_activity": int(la), "model": model or ""}
            # opening a session bumps last_activity_at: show the last
            # real reply instead, and the context size the tree flips to
            rts, ctx = _reply_info(db, sid, int(la))
            if rts:
                info[sid]["last_activity"] = int(rts)
            info[sid]["ctx"] = ctx
        db.close()
    except sqlite3.Error as e:
        log("db error: %s" % e)
        return []
    global _spaces_warned
    grouped = {}                                # spaceId -> [sid, ...]
    desk = None                                 # sids Desktop knows
    gmeta = {}                                  # spaceId -> metadata entry
    in_group = set()
    try:
        sdb = sqlite3.connect("file:%s?mode=ro" % state_db,
                              uri=True, timeout=1)
        r2s = sdb.execute("SELECT value FROM ItemTable WHERE key=?",
                          ("windsurfSpace.resourceToSpace",)).fetchone()
        meta = sdb.execute("SELECT value FROM ItemTable WHERE key=?",
                           ("windsurfSpace.metadata",)).fetchone()
        # one row per local session Desktop knows — current versions
        # don't put every session in a Space, so resourceToSpace alone
        # misses most of them (cloud sessions live under other keys)
        srows = sdb.execute(
            "SELECT key, value FROM ItemTable WHERE key LIKE"
            " 'windsurf.acp.sessioninfo.session.acp/devin-cli/%'").fetchall()
        sdb.close()
        r2s = json.loads(r2s[0]) if r2s else {}
        meta = json.loads(meta[0]) if meta else {}
        desk = {m.group(1) for res in r2s.values() for m in
                (re.search(r"cascade-acp/devin-cli/([^/?#]+)$", r)
                 for r in res) if m}
        for k, v in srows:
            try:
                kind = ((json.loads(v) or {}).get("location")
                        or {}).get("kind")
            except ValueError:
                kind = None
            if kind and kind != "local":
                continue                  # a cloud session, not Desktop's
            desk.add(k.rsplit("/", 1)[-1])
        global _DESK_SIDS
        _DESK_SIDS = (time.time(), desk)
        for sp, resources in r2s.items():
            sids = [m.group(1) for m in
                    (re.search(r"cascade-acp/devin-cli/([^/?#]+)$", r)
                     for r in resources)
                    if m and m.group(1) in info]
            md = meta.get(sp) or {}
            if md.get("isGrouped") and sids:
                grouped[sp] = sids
                gmeta[sp] = md
                in_group.update(sids)
    except Exception as e:
        if not _spaces_warned:                  # unreadable -> all flat,
            _spaces_warned = True               # like before spaces existed
            log("spaces: state.vscdb unreadable: %s" % e)
    # which devin.exe holds each open session right now — same cadence
    # as the origin set (this read), and the tag's holder side
    global _HELD_SIDS
    try:
        _HELD_SIDS = (time.time(), devin_cli.lock_holder_kinds())
    except Exception:
        pass
    # Desktop opens every session it shows as a cascade-acp resource; one
    # it never registered was started from the terminal (Devin CLI)
    if desk is not None:
        held = _HELD_SIDS[1]
        for s in info.values():
            s["source"] = held.get(s["sid"]) or (
                "desktop" if s["sid"] in desk else "cli")
    out = []
    for sp in sorted(grouped):
        items = [info[sid] for sid in grouped[sp]]
        t = gmeta[sp].get("title")              # renameSpace stores this
        out.append({"type": "group", "id": sp,
                    "name": (t if isinstance(t, str) and t
                             else items[0]["title"]),
                    "collapsed": bool(gmeta[sp].get("collapsed")),
                    "items": items})
    rest = [s for sid, s in info.items() if sid not in in_group]
    rest.sort(key=lambda s: -s["last_activity"])
    out.extend(dict(s, type="session") for s in rest)
    return out


def list_sessions():
    """All visible sessions for the picker: [(sid, title, first_msg)]."""
    out = []
    for e in list_spaces():
        if e["type"] == "group":
            out.extend((s["sid"], s["title"], s["first_msg"])
                       for s in e["items"])
        else:
            out.append((e["sid"], e["title"], e["first_msg"]))
    return out


def _inner_ts(md):
    """True UTC timestamp of a turn: metadata.created_at keeps the original
    turn time even when session replay rewrites the row's created_at column.
    Returns epoch seconds or None."""
    s = md.get('created_at')
    if not s:
        return None
    try:
        s = s.rstrip('Z')
        if '.' in s:
            head, frac = s.split('.', 1)
            s = head + '.' + frac[:6]
        return datetime.fromisoformat(s).replace(
            tzinfo=timezone.utc).timestamp()
    except Exception:
        return None


_EFF_SFX = re.compile(r"-(low|medium|high|xhigh|max|fast)$")


def _strip_eff(m):
    """Drop a trailing effort/speed suffix: swe-2-high -> swe-2."""
    return _EFF_SFX.sub("", (m or "").lower())


def _model_match(gen, want):
    """generation_model vs a sessions.model role part — exact match,
    else equal after stripping the effort suffix from both sides."""
    g, w = (gen or "").lower(), (want or "").lower()
    return bool(g) and bool(w) and (g == w or _strip_eff(g) == _strip_eff(w))


def session_roles(model):
    """[(role, model)] for a sessions.model value. fusion-* sessions run a
    lead model plus a sidekick: 'fusion-<lead>-sidekick-<side>'. A plain
    model is a single lead; empty/unknown -> []."""
    m = (model or "").strip()
    if m.startswith("fusion-"):
        lead, sep, side = m[len("fusion-"):].partition("-sidekick-")
        out = [("lead", lead)] if lead else []
        if side:
            out.append(("sidekick", side))
        if out:
            return out
    return [("lead", m)] if m else []


def _sess_model_db(sid):
    """sessions.model for a sid (plain PK lookup)."""
    if not os.path.exists(DB):
        return ""
    try:
        db = sqlite3.connect("file:%s?mode=ro" % DB, uri=True, timeout=1)
        r = db.execute("SELECT model FROM sessions WHERE id=?",
                       (sid,)).fetchone()
        db.close()
        return (r[0] or "") if r else ""
    except sqlite3.Error:
        return ""


def _metrics_batch(sid, since=0, limit=80):
    """Replay-safe metrics rows for a session, newest-first. Each item is
    the metrics dict plus generation_model/request_id/_ts (epoch of the
    inner metadata.created_at, immune to session-replay row rewrites)."""
    import json
    if not os.path.exists(DB):
        return []
    try:
        db = sqlite3.connect("file:%s?mode=ro" % DB, uri=True, timeout=1)
        db.execute("PRAGMA busy_timeout=500")
        # Replay signature: reopening a session rewrites every node with a
        # fresh row created_at, but metadata.created_at still says when the
        # turn really happened. Filter AND order by that inner timestamp —
        # replay rows carry old inner values and can never outrank live
        # rows, so a burst can't starve the window no matter its size.
        lo = datetime.fromtimestamp(max(since, 0), timezone.utc
                                    ).strftime("%Y-%m-%dT%H:%M:%S")
        hi = datetime.fromtimestamp(time.time() + 120, timezone.utc
                                    ).strftime("%Y-%m-%dT%H:%M:%S")
        inner = "json_extract(chat_message,'$.metadata.created_at')"
        rows = db.execute(
            "SELECT chat_message FROM message_nodes"
            " WHERE session_id=? AND chat_message LIKE '%\"metrics\"%'"
            f" AND {inner} >= ? AND {inner} <= ?"
            f" ORDER BY {inner} DESC LIMIT {int(limit)}",
            (sid, lo, hi)).fetchall()
        db.close()
    except sqlite3.Error:
        return []
    out = []
    for blob, in rows:
        try:
            d = json.loads(blob)
            md = d.get('metadata') or {}
            m = md.get('metrics') or {}
            its = _inner_ts(md)
        except Exception:
            continue
        if its is None or not (m.get('cache_read_tokens')
                               or m.get('input_tokens')):
            continue
        m['generation_model'] = md.get('generation_model')
        m['request_id'] = md.get('request_id')
        m['_ts'] = int(its)
        out.append(m)
    return out


def _metrics_for_model(sid, model):
    """Targeted fallback: newest metrics row for one generation_model, for
    when the role's row fell out of the shared batch (a busy sidekick can
    churn hundreds of rows between two lead turns). Matches the model
    exactly or with the effort suffix stripped."""
    import json
    if not os.path.exists(DB):
        return None
    try:
        db = sqlite3.connect("file:%s?mode=ro" % DB, uri=True, timeout=1)
        db.execute("PRAGMA busy_timeout=500")
        hi = datetime.fromtimestamp(time.time() + 120, timezone.utc
                                    ).strftime("%Y-%m-%dT%H:%M:%S")
        inner = "json_extract(chat_message,'$.metadata.created_at')"
        gen = "json_extract(chat_message,'$.metadata.generation_model')"
        rows = db.execute(
            "SELECT chat_message FROM message_nodes"
            " WHERE session_id=? AND chat_message LIKE '%\"metrics\"%'"
            f" AND {inner} <= ?"
            f" AND ({gen} IN (?, ?) OR {gen} LIKE ?)"
            f" ORDER BY {inner} DESC LIMIT 12",
            (sid, hi, model, _strip_eff(model),
             _strip_eff(model) + "-%")).fetchall()
        db.close()
    except sqlite3.Error:
        return None
    for blob, in rows:
        try:
            d = json.loads(blob)
            md = d.get('metadata') or {}
            m = md.get('metrics') or {}
            its = _inner_ts(md)
        except Exception:
            continue
        if not _model_match(md.get('generation_model'), model):
            continue
        if its is None or not (m.get('cache_read_tokens')
                               or m.get('input_tokens')):
            continue
        m['generation_model'] = md.get('generation_model')
        m['request_id'] = md.get('request_id')
        m['_ts'] = int(its)
        return m
    return None


def session_metrics(sid, since=0):
    """Latest per-turn metrics for the session (turns newer than `since`,
    epoch seconds). metrics = {input/output/cache_read/cache_creation
    tokens, request_id} embedded in chat_message.metadata."""
    rows = _metrics_batch(sid, since, 24)
    return rows[0] if rows else None


def session_tracks(sid, model=None):
    """One cache track per model role: [{role, model, family, metrics,
    refresh_ts}]. fusion-* sessions get a lead + sidekick track; a plain
    session a single 'lead' track; an unknown/empty model falls back to
    one 'single' track from the newest metrics row of any model.
    refresh_ts = inner_ts - total_time_ms of the role's newest row."""
    if model is None:
        model = _sess_model_db(sid)
    roles = session_roles(model)
    batch = _metrics_batch(sid, 0, 80)
    tracks = []
    for role, mdl in roles:
        m = next((r for r in batch
                  if _model_match(r.get('generation_model'), mdl)), None)
        if m is None and mdl:
            m = _metrics_for_model(sid, mdl)
        tracks.append({
            "role": role, "model": mdl,
            "family": _family(mdl, fused=len(roles) > 1),
            "metrics": m,
            "refresh_ts": (m['_ts'] - (m.get('total_time_ms') or 0) / 1000.0
                           if m else None),
        })
    if not roles and batch:
        m = batch[0]
        gm = m.get('generation_model')
        tracks.append({
            "role": "single", "model": gm, "family": _family(gm),
            "metrics": m,
            "refresh_ts": (m['_ts']
                           - (m.get('total_time_ms') or 0) / 1000.0),
        })
    return tracks


def session_state(sid):
    """{'busy', 'refresh_ts', 'why'} for scheduling heartbeats.

    busy: the newest row (by inner created_at) is NOT a finished assistant
    turn end — a turn is still running or one is about to start.
    refresh_ts: epoch when this session's prompt cache was last refreshed
    — the start (inner_ts - total_time_ms) of the newest request that
    produced metrics, or the newest user row (a request just started),
    whichever is later. None = nothing seen / can't tell.
    Returns None only on a db error."""
    if not os.path.exists(DB):
        return None
    try:
        db = sqlite3.connect("file:%s?mode=ro" % DB, uri=True, timeout=1)
        db.execute("PRAGMA busy_timeout=500")
        inner = "json_extract(chat_message,'$.metadata.created_at')"
        hi = datetime.fromtimestamp(time.time() + 120, timezone.utc
                                    ).strftime("%Y-%m-%dT%H:%M:%S")
        r1 = db.execute(
            "SELECT json_extract(chat_message,'$.role'),"
            "       json_extract(chat_message,'$.metadata.finish_reason'),"
            f"      {inner}"
            "  FROM message_nodes WHERE session_id=?"
            f"   AND {inner} <= ?"
            f" ORDER BY {inner} DESC LIMIT 1",
            (sid, hi)).fetchone()
        # Heartbeats are answered by the lead model only — its rows are
        # the ones that renew the cache we can actually refresh.
        sm = db.execute("SELECT model FROM sessions WHERE id=?",
                        (sid,)).fetchone()
        roles = session_roles((sm[0] or "") if sm else "")
        lead = roles[0][1] if roles else None
        gen = "json_extract(chat_message,'$.metadata.generation_model')"
        r2 = None
        if lead:
            r2rows = db.execute(
                f"SELECT {inner},"
                "       json_extract(chat_message,"
                "                    '$.metadata.metrics.total_time_ms'),"
                f"      {gen}"
                "  FROM message_nodes WHERE session_id=?"
                f"   AND {inner} <= ?"
                "   AND json_extract(chat_message,"
                "                    '$.metadata.metrics.total_time_ms')"
                "       IS NOT NULL"
                f"   AND ({gen} IN (?, ?) OR {gen} LIKE ?)"
                f" ORDER BY {inner} DESC LIMIT 12",
                (sid, hi, lead, _strip_eff(lead),
                 _strip_eff(lead) + "-%")).fetchall()
            r2 = next((r for r in r2rows
                       if _model_match(r[2], lead)), None)
        if not r2:                       # lead has no rows -> any model
            r2 = db.execute(
                f"SELECT {inner},"
                "       json_extract(chat_message,"
                "                    '$.metadata.metrics.total_time_ms')"
                "  FROM message_nodes WHERE session_id=?"
                f"   AND {inner} <= ?"
                "   AND json_extract(chat_message,"
                "                    '$.metadata.metrics.total_time_ms')"
                "       IS NOT NULL"
                f" ORDER BY {inner} DESC LIMIT 1",
                (sid, hi)).fetchone()
        db.close()
    except sqlite3.Error:
        return None
    if not r1:
        return {'busy': False, 'refresh_ts': None, 'why': 'no rows'}
    role, fr, ts_s = r1
    newest_ts = _inner_ts({'created_at': ts_s})
    busy = not (role == 'assistant'
                and fr in ('stop', 'max_tokens', 'length', 'end_turn'))
    why = "%s/%s" % (role, fr) if fr else (role or "?")
    metrics_start = None
    if r2 and r2[1] is not None:
        its = _inner_ts({'created_at': r2[0]})
        if its is not None:
            metrics_start = its - r2[1] / 1000.0
    cands = [metrics_start]
    if role == 'user':
        cands.append(newest_ts)          # a user row = a request starting
    refresh_ts = max([t for t in cands if t is not None], default=None)
    return {'busy': busy, 'refresh_ts': refresh_ts, 'why': why}


# USD per 1M tokens: (input, output, cache_read, cache_write) — official
# list prices, shared by every harness (Devin, Droid) so a model costs the
# same wherever it runs. Longest matching prefix wins, so -fast/-flex tiers
# fall back to their base model's price.
# source: docs.devinenterprise.com/desktop/models (swe, claude, kimi);
# openai.com/api/pricing, Standard, short context, 2026-10-15 (gpt);
# fable / gemini-4-argon: the docs page's self-serve cost table, 2026-10-26
_PRICES = {
    'claude-fable-5-1':  (10.0, 50.0, 0.25, 12.5),
    'claude-5-fable':    (10.0, 50.0, 1.0, 12.5),
    'gemini-4-argon':    (2.0, 10.0, 0.1, 2.0),
    'swe-2':      (0.75, 3.75, 0.075, 0.0),
    'swe-1-7-lightning': (2.5, 12.5, 1.0, 0.0),
    'swe-1-7':    (0.5, 2.5, 0.2, 0.0),
    'swe-1-6':    (0.5, 2.5, 0.2, 0.0),
    'claude-opus-5-5':   (4.0, 20.0, 0.2, 5.0),
    'claude-sonnet-5-5': (2.0, 10.0, 0.2, 2.5),
    'claude-sonnet-5':   (2.0, 10.0, 0.2, 2.5),
    'kimi-k3':    (3.0, 15.0, 0.3, 0.0),
    'gpt-6-astra':  (10.0, 50.0, 1.0, 12.5),
    'gpt-6.1-sol':  (2.0, 10.0, 0.1, 2.5),
    'gpt-6-luna':   (0.1, 0.5, 0.01, 0.125),
    'gpt-6-sol':    (2.0, 10.0, 0.2, 2.5),
    'gpt-5.6-sol':  (4.0, 20.0, 0.4, 5.0),
    'gpt-5.6-terra': (2.0, 12.0, 0.2, 2.5),
    'gpt-5.6-luna': (0.2, 1.2, 0.02, 0.25),
    'gpt-5.5':      (5.0, 30.0, 0.5, 0.0),   # no cache-write price listed
}


def _price_for(model):
    if not model:
        return None
    model = model.lower()
    for k in sorted(_PRICES, key=len, reverse=True):   # longest prefix wins
        if model.startswith(k):
            return _PRICES[k]
    return None


def turn_cost(m):
    """(cost_with_cache, cost_without_cache) in USD for one turn."""
    p = _price_for(m.get('generation_model'))
    if not p:
        return None
    in_p, out_p, cr_p, cw_p = p
    inp = m.get('input_tokens') or 0
    out = m.get('output_tokens') or 0
    cr = m.get('cache_read_tokens') or 0
    cw = m.get('cache_creation_tokens') or 0
    with_c = inp * in_p + out * out_p + cr * cr_p + cw * cw_p
    without = (inp + cr + cw) * in_p + out * out_p
    return with_c / 1e6, without / 1e6


def fmt_tok(m):
    """'126.1K hit 88%' from a metrics dict."""
    cr = int(m.get('cache_read_tokens') or 0)
    tot = (int(m.get('input_tokens') or 0) + cr
           + int(m.get('cache_creation_tokens') or 0))
    s = "%.1fK" % (cr / 1000) if cr < 1e6 else "%.2fM" % (cr / 1e6)
    if tot:
        s += " hit %.0f%%" % (100.0 * cr / tot)
    if m.get('generation_model'):
        s += " " + m['generation_model']
    return s


def rescue_value(m):
    """USD a rescued turn saved: without Buddy its cached prefix would have
    been written again (cache-write price, or full input where the model
    has none) instead of read."""
    p = _price_for(m.get('generation_model'))
    if not p:
        return 0.0
    in_p, _, cr_p, cw_p = p
    cr = m.get('cache_read_tokens') or 0
    return max(0.0, cr * ((cw_p or in_p) - cr_p) / 1e6)


class Ledger:
    """Per-day savings, kept across runs (ledger.json):
    {"2026-10-03": {"saved": rescue USD, "hb_cost": USD, "rescues": n,
                    "hb": n, "cache_saved": USD vs no cache at all}}"""

    def __init__(self, path):
        self.path = path
        self.lock = threading.Lock()
        try:
            self.days = json.load(open(path, encoding="utf-8"))
        except (OSError, ValueError):
            self.days = {}

    def add(self, **kv):
        day = time.strftime("%Y-%m-%d")
        with self.lock:
            d = self.days.setdefault(day, {})
            for k, v in kv.items():
                d[k] = round(d.get(k, 0) + v, 6)
            data = json.dumps(self.days, indent=1)
        try:                              # small file: rewrite atomically
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(data)
            os.replace(tmp, self.path)
        except OSError as e:
            log("ledger write failed: %s" % e)

    def month(self, ym=None):
        """One calendar month for the heatmap: {"month": "2026-10",
        "days": [{"day", "net", "future", ...} for the 1st..last],
        "first": earliest month with data}."""
        today = time.strftime("%Y-%m-%d")
        ym = ym or today[:7]
        y, m = int(ym[:4]), int(ym[5:7])
        n = (datetime(y + m // 12, m % 12 + 1, 1) - datetime(y, m, 1)).days
        out = []
        with self.lock:
            for i in range(1, n + 1):
                day = "%s-%02d" % (ym, i)
                d = dict(self.days.get(day) or {})
                d["day"] = day
                d["net"] = round(d.get("saved", 0) - d.get("hb_cost", 0), 6)
                d["future"] = day > today
                out.append(d)
            first = min(self.days)[:7] if self.days else ym
        return {"month": ym, "days": out, "first": min(first, ym)}


def scan_warm(now=None):
    """sid -> (title, tracks) for every visible session whose lead cache
    is still alive (refreshed < TTL ago)."""
    if not os.path.exists(DB):
        return {}
    if time.time() - _DESK_SIDS[0] > 20:
        list_spaces()                     # keeps session_source fresh
    now = now or time.time()
    try:
        db = sqlite3.connect("file:%s?mode=ro" % DB, uri=True, timeout=1)
        rows = db.execute("SELECT id, title, last_activity_at, model"
                          "  FROM sessions WHERE hidden = 0").fetchall()
        db.close()
    except sqlite3.Error:
        return {}
    out = {}
    for sid, title, la, model in rows:
        la = (la or 0) // 1000 if (la or 0) > 1e12 else (la or 0)
        if now - la > TTL + 60:           # nothing since: can't be warm
            continue
        tracks = session_tracks(sid, model or "")
        rts = (tracks[0].get("refresh_ts") if tracks else None)
        if rts and now - rts < TTL:
            for t in tracks:
                t["sid"] = sid
            out[sid] = (title or sid, tracks)
    return out


def turn_log(sid, since=0):
    """The lead model's requests since `since` (epoch), oldest first:
    [{"rid", "start", "hb", "m"}]. hb = the request answers a heartbeat
    (the newest user row before it carries HEARTBEAT_MARK); start = inner
    created_at - total_time_ms, the moment the cache was read. Replay
    duplicates collapse on request_id."""
    if not os.path.exists(DB):
        return []
    try:
        db = sqlite3.connect("file:%s?mode=ro" % DB, uri=True, timeout=1)
        db.execute("PRAGMA busy_timeout=500")
        sm = db.execute("SELECT model FROM sessions WHERE id=?",
                        (sid,)).fetchone()
        inner = "json_extract(chat_message,'$.metadata.created_at')"
        lo = datetime.fromtimestamp(max(since, 0), timezone.utc
                                    ).strftime("%Y-%m-%dT%H:%M:%S")
        rows = db.execute(
            "SELECT json_extract(chat_message,'$.role'),"
            "       CASE WHEN json_extract(chat_message,'$.role')='user'"
            "            THEN chat_message LIKE ? END,"
            "       CASE WHEN json_extract(chat_message,'$.role')"
            "                 ='assistant' THEN chat_message END"
            "  FROM message_nodes WHERE session_id=?"
            "   AND json_extract(chat_message,'$.role')"
            "       IN ('user','assistant')"
            f"  AND {inner} >= ? ORDER BY {inner}",
            ("%" + HEARTBEAT_MARK + "%", sid, lo)).fetchall()
        db.close()
    except sqlite3.Error:
        return []
    roles = session_roles((sm[0] or "") if sm else "")
    lead = roles[0][1] if roles else None
    out, seen, hb = [], set(), False
    for role, is_hb, blob in rows:
        if role == "user":
            hb = bool(is_hb)
            continue
        try:
            md = json.loads(blob).get('metadata') or {}
        except (TypeError, ValueError):
            continue
        m = dict(md.get('metrics') or {})
        its = _inner_ts(md)
        rid = md.get('request_id')
        if (its is None or not rid or rid in seen
                or not (m.get('cache_read_tokens') or m.get('input_tokens'))):
            continue
        gm = md.get('generation_model')
        if lead and not _model_match(gm, lead):
            continue                      # sidekick: not what we renew
        seen.add(rid)
        m['generation_model'] = gm
        m['request_id'] = rid
        out.append({"rid": rid, "hb": hb, "m": m,
                    "start": its - (m.get('total_time_ms') or 0) / 1000.0})
    return out


def warm_sessions():
    """session_id -> (title, last_real_user_msg_ts, first_user_msg)."""
    if not os.path.exists(DB):
        return {}
    try:
        db = sqlite3.connect("file:%s?mode=ro" % DB, uri=True)
        now = int(time.time())
        cand = db.execute(
            """SELECT m.session_id, s.title, m.created_at,
                        json_extract(m.chat_message, '$.message_id')
                 FROM message_nodes m
                 JOIN sessions s ON s.id = m.session_id
                WHERE m.chat_message LIKE '%"role":"user"%'
                  AND m.chat_message NOT LIKE ?
                  AND s.hidden = 0
                  AND m.created_at > ?""",
            ("%" + HEARTBEAT_MARK + "%", now - COVERAGE),
        ).fetchall()
        # Opening a session REPLAYS its history into message_nodes with fresh
        # timestamps — several user rows land in the same instant. A cluster
        # of >=2 user rows within 5s is suspicious; confirm it's a replay by
        # checking whether any cluster member's message_id already exists at
        # an older timestamp (reload keeps ids but rewrites times).
        per = {}
        for sid, title, ts, mid in cand:
            per.setdefault(sid, [title, []])[1].append((ts, mid))
        last_real = {}
        for sid, (title, items) in per.items():
            items.sort()
            keep = []
            i = 0
            while i < len(items):
                j = i
                while j + 1 < len(items) and items[j + 1][0] - items[j][0] <= 5:
                    j += 1
                cluster = items[i:j + 1]
                if len(cluster) >= 2:
                    ids = tuple(m for _, m in cluster)
                    marks = ",".join("?" * len(ids))
                    older = db.execute(
                        """SELECT 1 FROM message_nodes
                            WHERE session_id = ?
                              AND created_at < ?
                              AND json_extract(chat_message,'$.message_id')
                                  IN (%s) LIMIT 1""" % marks,
                        (sid, min(t for t, _ in cluster) - 60) + ids,
                    ).fetchone()
                    if not older:          # no prior copies → real activity
                        keep.extend(t for t, _ in cluster)
                    # else: replay burst — drop the whole cluster
                else:
                    keep.append(cluster[0][0])
                i = j + 1
            if keep:
                last_real[sid] = (title, max(keep))
        out = {sid: [title, ts, None] for sid, (title, ts) in last_real.items()}
        # prompts the user just hit Enter on land in prompt_history
        # immediately — queued behind a running turn they may not reach
        # message_nodes for a while; count them as real activity too
        for sid, content, ts in db.execute(
            """SELECT session_id, content, MAX(timestamp)
                 FROM prompt_history
                WHERE content NOT LIKE ?
                GROUP BY session_id
               HAVING MAX(timestamp) > ?""",
            ("%" + HEARTBEAT_MARK + "%", now - COVERAGE),
        ).fetchall():
            if sid in out:
                out[sid][1] = max(out[sid][1], ts)
            else:
                t = db.execute("SELECT title FROM sessions WHERE id=?",
                               (sid,)).fetchone()
                out[sid] = [(t[0] if t else None) or sid, ts, None]
        for sid in list(out):
            out[sid][2] = _first_user_msg(db, sid)
        db.close()
        return {s: tuple(v) for s, v in out.items()}
    except sqlite3.Error as e:
        log("db error: %s" % e)
        return {}


# ------------------------------------------------------------- uia injection

def _sendkeys_escape(s):
    out = []
    for ch in s:
        if ch in "{}+^%~()[]":
            out.append("{" + ch + "}")
        else:
            out.append(ch)
    return "".join(out)


def _find_prompt_edit(win):
    import uiautomation as uia
    e = uia.EditControl(searchFromControl=win, searchDepth=60, Name="Prompt")
    return e if e.Exists(8) else None


def _edit_value(edit):
    try:
        return edit.GetValuePattern().Value or ""
    except Exception:
        return ""


def _edit_is_empty(v):
    """Prompt shows a placeholder like \ufeff\\nTip: ... when empty."""
    v = v.strip().lstrip("﻿").strip()
    return not v or v.lower().startswith("tip:")


def _win_session(win):
    return win.Name.rsplit(" - ", 1)[-1].strip()


def _name_matches(seen, title, first_msg):
    """A window/tab/display name may show sessions.title OR the first user
    message (window title). Trailing '…' truncation tolerated."""
    seen = (seen or "").strip().rstrip("…").rstrip(".").strip()
    title = (title or "").strip()
    fm = (first_msg or "").strip()
    for cand in (title, fm):
        if cand and (seen == cand or cand.startswith(seen)
                     or seen.startswith(cand[:25])):
            return True
    return False


def _find_tab(win, title, first_msg):
    import uiautomation as uia
    for name in {title, (first_msg or "")[:40]} - {""}:
        t = uia.TabItemControl(searchFromControl=win, searchDepth=60, Name=name)
        if t.Exists(4):
            return t
    return None


def _select_tab(win, tab):
    import uiautomation as uia
    try:
        win.SetActive()
        time.sleep(0.5)
    except Exception:
        pass
    r = tab.BoundingRectangle
    uia.Click((r.left + r.right) // 2, (r.top + r.bottom) // 2)
    time.sleep(2.0)


def _chat_root(win):
    """The tall ancestor of the composer that holds the whole chat column —
    scoping text searches here avoids same-named text in tab bars and
    side panels."""
    edit = _find_prompt_edit(win)
    if not edit:
        return None, None
    er = edit.BoundingRectangle
    p = edit
    for _ in range(15):                 # first ancestor tall enough to be
        p = p.GetParentControl()        # the whole chat column
        if not p:
            return None, er
        r = p.BoundingRectangle
        if (r.bottom - r.top) > 600:
            return p, er
    return None, er


def _msg_text(group):
    """(name, text-control) of the first visible text inside a 'User
    message' group — the row's identity handle."""
    found = []

    def walk(c, d):
        if found or d > 15:
            return
        try:
            if c.ControlType == 50020 and (c.Name or "").strip():
                found.append((c.Name.strip()[:40], c))
                return
        except Exception:
            pass
        try:
            for ch in c.GetChildren():
                walk(ch, d + 1)
        except Exception:
            pass

    walk(group, 0)
    return found[0] if found else (None, None)


def _user_msgs(root):
    """Mounted 'User message' group rows, top→bottom: [(top, key, text)].
    Includes rows in the virtualization buffer just offscreen."""
    out = []

    def walk(c, d):
        if d > 45:
            return
        try:
            if c.ControlType == 50026 and (c.Name or "") == "User message":
                r = c.BoundingRectangle
                k, t = _msg_text(c)
                if k:
                    out.append((r.top, k, t))
                return                  # no nested user-message groups
        except Exception:
            pass
        try:
            for ch in c.GetChildren():
                walk(ch, d + 1)
        except Exception:
            pass

    walk(root, 0)
    out.sort(key=lambda x: x[0])
    return out


def _top_anchor(win):
    """[(orig_top, key)] of 'User message' rows nearest the chat viewport,
    best first — inside it, or just above/below in the mounted buffer.
    Several candidates because virtualization unmounts fast: the best
    match may be gone from the tree by restore time, lower ones usually
    survive (the view lands next to them)."""
    root, er = _chat_root(win)
    if not root:
        return None
    rr = root.BoundingRectangle
    cands = []                         # (rank, top, key)
    for top, key, _t in _user_msgs(root):
        if rr.top <= top < er.top:
            rank = (0, -top)           # visible: prefer the lowest one
        else:
            rank = (1, rr.top - top if top < rr.top else top - er.top)
        cands.append((rank, top, key))
    cands.sort(key=lambda x: x[0])
    return [(top, key) for _r, top, key in cands[:10]] or None


def _clip_api():
    import ctypes
    u, k = ctypes.windll.user32, ctypes.windll.kernel32
    # 64-bit handles/pointers — default c_int restype truncates them and
    # blows up memmove with OverflowError
    k.GlobalAlloc.restype = ctypes.c_void_p
    k.GlobalAlloc.argtypes = [ctypes.c_uint, ctypes.c_size_t]
    k.GlobalLock.restype = ctypes.c_void_p
    k.GlobalLock.argtypes = [ctypes.c_void_p]
    k.GlobalUnlock.argtypes = [ctypes.c_void_p]
    u.GetClipboardData.restype = ctypes.c_void_p
    u.GetClipboardData.argtypes = [ctypes.c_uint]
    u.SetClipboardData.restype = ctypes.c_void_p
    u.SetClipboardData.argtypes = [ctypes.c_uint, ctypes.c_void_p]
    u.OpenClipboard.argtypes = [ctypes.c_void_p]
    return u, k


def _clip_get():
    """Current clipboard text (CF_UNICODETEXT only)."""
    import ctypes
    CF_UNICODETEXT = 13
    u, k = _clip_api()
    if not u.OpenClipboard(0):
        return None
    try:
        h = u.GetClipboardData(CF_UNICODETEXT)
        if not h:
            return None
        p = k.GlobalLock(h)
        if not p:
            return None
        try:
            return ctypes.wstring_at(p)
        finally:
            k.GlobalUnlock(h)
    finally:
        u.CloseClipboard()


def _clip_set(text):
    import ctypes
    CF_UNICODETEXT, GMEM_MOVEABLE = 13, 0x0002
    u, k = _clip_api()
    if not u.OpenClipboard(0):
        return False
    try:
        u.EmptyClipboard()
        data = (text + "\0").encode("utf-16-le")
        h = k.GlobalAlloc(GMEM_MOVEABLE, len(data))
        p = k.GlobalLock(h)
        ctypes.memmove(p, data, len(data))
        k.GlobalUnlock(h)
        if not u.SetClipboardData(CF_UNICODETEXT, h):
            k.GlobalFree(h)
            return False
        return True
    finally:
        u.CloseClipboard()


def _put_draft_back(win, draft):
    """Best-effort: paste the user's interrupted draft back into the
    composer after the heartbeat has been submitted. Never raises — the
    send already succeeded, a failure here must not trigger a resend."""
    try:
        _put_draft_back_impl(win, draft)
    except Exception as e:
        log("draft restore error: %s" % e)


def _put_draft_back_impl(win, draft):
    import uiautomation as uia
    edit = _find_prompt_edit(win)
    if not edit:
        return
    try:
        edit.Click()
        time.sleep(0.4)
    except Exception:
        return
    old = _clip_get()
    if not _clip_set(draft):
        return
    uia.SendKeys("{Ctrl}a")              # replace whatever sits there now
    time.sleep(0.2)
    uia.SendKeys("{Ctrl}v")
    time.sleep(0.8)
    if draft[:15] not in _edit_value(edit):
        log("draft paste-back failed")
    else:
        log("draft restored in composer")
    if old is not None:
        _clip_set(old)


def _restore_anchor(win, anchors):
    """anchors = [(orig_top, key)] of 'User message' rows, best first.
    Land on the first candidate still mounted — lower rows survive the
    jump-to-bottom longest. NEVER scroll to probe: if every candidate has
    been unmounted by virtualization, leave the view alone."""
    if not anchors:
        return
    import uiautomation as uia
    root, er = _chat_root(win)
    if not root:
        return
    mounted = _user_msgs(root)         # [(top, key, text-ctrl)]
    for orig_top, key in anchors:
        best = None                    # (|Δtop|, text-ctrl, top)
        for top, k, t in mounted:
            if k[:25] == key[:25]:
                d = abs(top - orig_top)  # dup texts (heartbeats): the
                if best is None or d < best[0]:  # nearest old spot wins
                    best = (d, t, top)
        if not best:
            continue                   # this row is gone — try next one
        try:
            p = best[1].GetPattern(uia.PatternId.ScrollItemPattern)
            if p:
                p.ScrollIntoView()
                log("restore done: user msg %r top %s->%s"
                    % (key[:20], orig_top, best[2]))
            return
        except Exception as e:
            log("restore error: %s" % e)
            return
    log("restore: all user msgs unmounted, leaving view as-is")


# ------------------------------------------------- cdp scroll hold
# Devin runs with --remote-debugging-port=9222 (argv.json). The guard below
# is installed in the workbench page and, while armed, drops the app's
# scroll-to-bottom writes on the transcript container — the user's reading
# position survives heartbeat sends anywhere in history, no UIA anchors
# needed. Kept verbatim from cdp-guard.js so the single-file exe carries it.

_CDP_GUARD_JS = r"""
(() => {
  const W = window;
  const find = () => document.querySelector('[aria-label="Session transcript"]')?.closest('.cascade-scrollbar');
  if (!W.__cbGuard) {
    const d = Object.getOwnPropertyDescriptor(Element.prototype, 'scrollTop');
    const g = W.__cbGuard = { holdUntil: 0, test: false, log: [], nudged: 0, drops: 0 };
    const rec = o => { g.drops++; g.log.push({ t: Date.now(), ...o }); if (g.log.length > 200) g.log.shift(); };
    const active = el => el === find() && (Date.now() < g.holdUntil ||
      (g.test && el.scrollHeight - d.get.call(el) - el.clientHeight > el.clientHeight));
    const toBottom = (el, v) => v >= el.scrollHeight - el.clientHeight - 2;
    const nudge = el => {
      if (Date.now() - g.nudged < 1500) return;
      g.nudged = Date.now();
      setTimeout(() => { d.set.call(el, d.get.call(el) - 3); setTimeout(() => d.set.call(el, d.get.call(el) + 3), 50); }, 0);
    };
    Object.defineProperty(Element.prototype, 'scrollTop', {
      configurable: true, get() { return d.get.call(this); },
      set(v) {
        if (active(this) && toBottom(this, v)) { rec({ drop: 'scrollTop=', v: Math.round(v), st: Math.round(d.get.call(this)) }); nudge(this); return; }
        d.set.call(this, v);
      }
    });
    for (const m of ['scrollTo', 'scroll']) {
      const o = Element.prototype[m];
      Element.prototype[m] = function (...a) {
        const v = typeof a[0] === 'object' ? a[0]?.top : a[1];
        if (v != null && active(this) && toBottom(this, v)) { rec({ drop: m, v: Math.round(v) }); nudge(this); return; }
        return o.apply(this, a);
      };
    }
  }
  const g = W.__cbGuard;
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
    if (s.armed) { g.holdUntil = Date.now() + ms; g.drops = 0; s.drops = 0; }
    return s;
  };
  return stat();
})()
"""

_CDP_LIST = "http://127.0.0.1:9222/json/list"
_cdp_logged = set()                # failure reasons already logged


class _Cdp:
    """One CDP websocket session to the Devin workbench page: the page
    whose title is `win_title`, else the first workbench.html page.
    Raises on any failure — callers decide what that means. Context
    manager; always closes the socket."""

    def __init__(self, win_title, timeout=3):
        import json
        import urllib.request
        import websocket
        self._json = json
        targets = json.loads(urllib.request.urlopen(
            _CDP_LIST, timeout=timeout).read())
        pages = [t for t in targets if t.get("type") == "page"]
        tgt = next((t for t in pages if t.get("title") == win_title), None)
        if tgt is None:
            tgt = next((t for t in pages
                        if "workbench.html" in (t.get("url") or "")), None)
        if tgt is None:
            raise RuntimeError("no workbench page target")
        self.ws = websocket.create_connection(tgt["webSocketDebuggerUrl"],
                                              timeout=timeout,
                                              suppress_origin=True)
        self._id = 0

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def close(self):
        try:
            self.ws.close()
        except Exception:
            pass

    def call(self, method, params=None):
        """One CDP method call; returns its `result` dict. Events and
        unrelated replies are skipped; protocol errors raise."""
        self._id += 1
        mid = self._id
        self.ws.send(self._json.dumps({"id": mid, "method": method,
                                       "params": params or {}}))
        while True:
            res = self._json.loads(self.ws.recv())
            if res.get("id") != mid:
                continue                      # skip events/other replies
            if res.get("error"):
                raise RuntimeError("%s: %s" % (method, res["error"]))
            return res.get("result") or {}

    def eval(self, expr):
        """Runtime.evaluate; returns the JSON value, raises on
        exceptionDetails."""
        r = self.call("Runtime.evaluate",
                      {"expression": expr, "returnByValue": True,
                       "awaitPromise": True})
        if "exceptionDetails" in r:
            raise RuntimeError("page exception: %s"
                               % self._json.dumps(
                                   r["exceptionDetails"])[:200])
        return (r.get("result") or {}).get("value")

    def dom_select(self, selector, all=False):
        """DOM query on this connection: returns a nodeId (or list)."""
        self.call("DOM.enable")
        doc = self.call("DOM.getDocument", {"depth": -1})
        root = (doc.get("root") or {}).get("nodeId")
        if all:
            r = self.call("DOM.querySelectorAll",
                          {"nodeId": root, "selector": selector})
            return r.get("nodeIds") or []
        return (self.call("DOM.querySelector",
                         {"nodeId": root, "selector": selector})
                or {}).get("nodeId")


def _cdp_eval(win_title, expr, timeout=3):
    """Runtime.evaluate `expr` in the Devin workbench page via CDP.
    Returns the JSON value, or None on any failure (logged once per
    distinct reason)."""
    try:
        with _Cdp(win_title, timeout) as c:
            return c.eval(expr)
    except Exception as e:
        reason = "%s: %s" % (type(e).__name__, e)
        if reason not in _cdp_logged:
            _cdp_logged.add(reason)
            log("cdp eval failed: %s" % reason)
        return None


def _cdp_stat(win_title):
    """Install the scroll guard (idempotent) and return its stat dict."""
    return _cdp_eval(win_title, "(" + _CDP_GUARD_JS.strip() + ")")


def _cdp_arm(win_title, ms):
    """Install if needed, then hold scroll position for `ms` milliseconds.
    Returns stat dict with 'armed' true only when the user is more than a
    screen above the bottom."""
    return _cdp_eval(win_title, "(%s, window.__cbGuard.arm(%d))"
                     % (_CDP_GUARD_JS.strip(), ms))


def _devin_win_title():
    """Name of the first Devin top-level window, or "" — the CDP fallback
    (first workbench.html page) covers the no-window case anyway."""
    try:
        import uiautomation as uia
        for w in uia.GetRootControl().GetChildren():
            if (w.ControlTypeName == "WindowControl"
                    and "Devin" in (w.Name or "")):
                return w.Name
    except Exception:
        pass
    return ""


# Composer send button: "Cancel" (running, empty composer), "Queue"
# (running, text queued), "Send" (idle — loading/reverting states add the
# cursor-wait class). "N message(s) queued" appears while items wait.
_CDP_BUSY_JS = r"""
(() => {
  const b = [...document.querySelectorAll('button[aria-label]')].find(e => ['Send','Cancel','Queue'].includes(e.getAttribute('aria-label')) && e.offsetParent);
  if (!b) return null;
  const t = document.querySelector('[aria-label="Session transcript"]');
  let box = b;
  for (let i = 0; i < 12 && box.parentElement && !(t && box.parentElement.contains(t)); i++) box = box.parentElement;
  return { label: b.getAttribute('aria-label'), wait: String(b.className).includes('cursor-wait'), queued: /\b\d+ messages? queued\b/.test(box.innerText || '') };
})()
"""


def _cdp_busy(title, first_msg):
    """True/False = the composer button of the window showing session
    `title` says a turn is running. None = can't tell: the session isn't
    displayed in any Devin window, or the eval failed."""
    import uiautomation as uia
    uia.SetGlobalSearchTimeout(5)
    try:
        wins = [w for w in uia.GetRootControl().GetChildren()
                if w.ControlTypeName == "WindowControl"
                and "Devin" in (w.Name or "")]
    except Exception:
        return None
    for w in wins:
        if _name_matches(_win_session(w), title, first_msg):
            r = _cdp_eval(w.Name, _CDP_BUSY_JS)
            if r is None:
                return None
            return (r.get("label") in ("Cancel", "Queue")
                    or bool(r.get("wait")) or bool(r.get("queued")))
    return None


# Composer state: the Slate prompt's real text (clone minus the
# placeholder minus the ﻿ sentinel) plus the send button's
# label/disabled. The whole send runs in-page — no window activation,
# no clipboard.
_CDP_STATE_JS = r"""
(() => {
  const p = document.querySelector('[aria-label="Prompt"][contenteditable=true]');
  const b = [...document.querySelectorAll('button[aria-label]')].find(e => ['Send','Cancel','Queue'].includes(e.getAttribute('aria-label')) && e.offsetParent);
  let text = '';
  if (p) {
    const c = p.cloneNode(true);
    c.querySelectorAll('[data-slate-placeholder]').forEach(e => e.remove());
    text = (c.innerText || '').replace(/\uFEFF/g, '').trim();
  }
  return { found: !!p, text, label: b ? b.getAttribute('aria-label') : null, disabled: b ? !!b.disabled : null };
})()
"""

_CDP_FOCUS_JS = ('document.querySelector(\'[aria-label="Prompt"]'
                 '[contenteditable=true]\').focus()')

# Composer attachment strip: pasted images are chips in
# div[class*="group/attachment"] next to the Slate editor — NOT part of
# the prompt text, so a heartbeat would otherwise submit the user's
# draft images along with it (and they'd be gone from the composer).
# __cbBox = nearest ancestor of the prompt owning both a send-family
# button and the hidden file input (the composer card — never the page).
_CDP_BOX_JS = r"""
var __cbP = document.querySelector('[aria-label="Prompt"][contenteditable=true]');
var __cbBox = null;
if (__cbP) {
  const isBtn = e => ['Send','Cancel','Queue'].includes(e.getAttribute('aria-label'));
  let b = __cbP;
  for (let i = 0; i < 14 && b.parentElement; i++) {
    b = b.parentElement;
    if ([...b.querySelectorAll('button[aria-label]')].some(isBtn)
        && b.querySelector('input[type=file]')) { __cbBox = b; break; }
  }
}
"""

_CDP_ATTS_JS = _CDP_BOX_JS + r"""
;(__cbBox ? {
  atts: [...__cbBox.querySelectorAll('div[class*="group/attachment"]')].map(it => {
    const img = it.querySelector('img');
    return img && String(img.src).startsWith('data:')
      ? {kind:'image', mime:(/^data:([^;,]+)/.exec(img.src)||[])[1]||'image/png',
         dataUrl: img.src, w: img.naturalWidth||0, h: img.naturalHeight||0}
      : {kind:'other'};
  }),
  fidx: [...document.querySelectorAll('input[type=file]')]
        .findIndex(i => __cbBox.contains(i))
} : null)
"""

_CDP_ATT_COUNT_JS = (_CDP_BOX_JS + r"""
;(__cbBox ? __cbBox
  .querySelectorAll('div[class*="group/attachment"]').length : 0)
""")

_CDP_CLICK_ONE_ATT_JS = _CDP_BOX_JS + r"""
;(__cbBox ? (() => {
  const b = __cbBox.querySelector('button[aria-label="Remove attachment"]');
  if (!b) return false;
  b.click();                     // one at a time — each removal re-renders
  return true;                   // the strip and stale buttons do nothing
})() : false)
"""

ATTACH_DIR = os.path.join(os.path.dirname(LOG_FILE), "held-attachments")
ATTACH_HOLD = "cdp: composer has attachments"

# "N message(s) queued" indicator inside the composer card — shown while a
# session turn is running and a send gets queued instead. We probe it to
# find any cancel control the UI exposes for the queued item.
_CDP_QUEUE_JS = _CDP_BOX_JS + r"""
;(__cbBox ? (() => {
  const hit = [...__cbBox.querySelectorAll('*')]
    .filter(e => /\b\d+ messages? queued\b/.test(e.innerText || '')
                 && e.children.length <= 6)
    .sort((a, b) => (a.innerText || '').length
                    - (b.innerText || '').length)[0];
  if (!hit) return {found: false};
  const btns = [...hit.querySelectorAll('button')].map(b =>
    (b.getAttribute('aria-label') || (b.innerText || '').trim())
      .slice(0, 60));
  return {found: true, text: (hit.innerText || '').slice(0, 120), btns};
})() : {found: false})
"""

# Click a queued-indicator button by index (same probe as _CDP_QUEUE_JS).
_CDP_QUEUE_CLICK_JS = _CDP_BOX_JS + r"""
;(__cbBox ? (() => {
  const hit = [...__cbBox.querySelectorAll('*')]
    .filter(e => /\b\d+ messages? queued\b/.test(e.innerText || '')
                 && e.children.length <= 6)
    .sort((a, b) => (a.innerText || '').length
                    - (b.innerText || '').length)[0];
  if (!hit) return 'no-indicator';
  const bs = [...hit.querySelectorAll('button')];
  const i = %d;
  if (i < 0 || i >= bs.length) return 'no-button';
  bs[i].click();
  return 'clicked';
})() : 'no-composer')
"""


def _save_atts(atts):
    """Persist each image chip's data URL so it can be re-attached after
    the heartbeat — the data URL is what Devin already processed (it
    transcodes big uploads itself), so byte-faithful restore comes free."""
    import base64
    os.makedirs(ATTACH_DIR, exist_ok=True)
    files = []
    ts = int(time.time())
    ext_of = {"image/png": "png", "image/jpeg": "jpg", "image/jpg": "jpg",
              "image/webp": "webp", "image/gif": "gif"}
    for i, a in enumerate(atts):
        data = a.get("dataUrl") or ""
        raw = (base64.b64decode(data.split(",", 1)[1])
               if "," in data else b"")
        fn = os.path.join(ATTACH_DIR,
                          "%d_%d.%s" % (ts, i, ext_of.get(
                              a.get("mime"), "png")))
        with open(fn, "wb") as f:
            f.write(raw)
        files.append(fn)
    log("attachments saved: %d -> %s" % (len(files), ATTACH_DIR))
    return files


def _cdp_attach(c, paths):
    """Attach `paths` one file at a time through the composer's own hidden
    file input — DOM.setFileInputFiles is the same code path a user paste
    exercises. One bulk call lands the chips in completion order (Devin
    transcodes asynchronously; a big jpeg can beat a small png), and some
    builds prepend new chips instead of appending — so each file waits
    for its own chip, and if the second chip lands at index 0 the attach
    is redone in reverse so the final order matches `paths`."""
    paths = list(paths)
    idx = (c.eval(_CDP_ATTS_JS) or {}).get("fidx")
    ids = c.dom_select("input[type=file]", all=True)
    if idx is None or not (0 <= idx < len(ids)):
        return False
    node = ids[idx]

    def srcs():
        return [a.get("dataUrl") for a in
                ((c.eval(_CDP_ATTS_JS) or {}).get("atts") or [])]

    def put(files):
        spots = []                       # index each new chip landed at
        before = srcs()
        for f in files:
            c.call("DOM.setFileInputFiles",
                   {"files": [f], "nodeId": node})
            pos = None
            deadline = time.time() + 4
            while time.time() < deadline:
                cur = srcs()
                # identify the chip only by "not in the pre-attach
                # snapshot" — Devin may re-encode, so the bytes differ
                new = [i for i, s in enumerate(cur) if s not in before]
                if new:
                    pos = new[0]
                    break
                time.sleep(0.2)
            if pos is None:
                return spots             # this file never chipped up
            spots.append(pos)
            before = cur
        return spots

    spots = put(paths)
    if len(spots) >= 2 and spots[1] == 0:
        # second chip landed at the head while a tail slot existed —
        # this build prepends: pull our chips off and redo it reversed
        log("attach order: prepend")
        for _ in spots:
            if not c.eval(_CDP_CLICK_ONE_ATT_JS):
                break
            time.sleep(0.3)
        spots = put(list(reversed(paths)))
    elif spots:
        log("attach order: append")
    return len(spots) == len(paths)


def _cdp_attach_missing(c, atts, files):
    """Re-attach only the saved files whose data URL is no longer among
    the composer chips — never duplicates a chip that stayed."""
    cur = {a.get("dataUrl") for a in
           ((c.eval(_CDP_ATTS_JS) or {}).get("atts") or [])}
    miss = [f for f, a in zip(files, atts)
            if a.get("dataUrl") not in cur]
    if miss:
        _cdp_attach(c, miss)
    return len(miss)


def _cdp_clear_text(c):
    """Select-all + Backspace in the composer, verified. Returns the
    remaining text."""
    c.eval(_CDP_FOCUS_JS)
    _cdp_key(c, "a", "KeyA", 65, modifiers=2)
    time.sleep(0.12)
    _cdp_key(c, "Backspace", "Backspace", 8)
    time.sleep(0.25)
    return ((c.eval(_CDP_STATE_JS) or {}).get("text") or "")


def _cdp_bounce_cleanup(c, text, draft, files):
    """Devin bounced the heartbeat back into the composer — a turn was
    still finishing, so the send never really went out (or it became a
    queued item). Scrub the heartbeat text, cancel the queued item if the
    UI exposes a control for it, restore draft + attachments, and report
    a busy hold so the caller retries later instead of counting a fail."""
    log("send bounced (busy)")
    q = c.eval(_CDP_QUEUE_JS)
    if q and q.get("found"):
        btns = q.get("btns") or []
        log("queued indicator: %r buttons=%r" % (q.get("text"), btns))
        hit = next((i for i, b in enumerate(btns)
                    if any(k in b.lower() for k in
                           ("cancel", "remove", "discard", "clear",
                            "delete", "abort"))), None)
        if hit is not None:
            r = c.eval(_CDP_QUEUE_CLICK_JS % hit)
            log("queued cancel -> %s" % r)
        else:
            log("queued indicator has no cancel control")
    cur = _cdp_clear_text(c)
    if cur.startswith(text[:30]):           # the clear lost a race — retry
        cur = _cdp_clear_text(c)
    if cur:
        log("bounce cleanup: composer still holds %r" % cur[:40])
    else:
        log("bounce cleanup: composer cleared")
    _cdp_restore(c, draft, files, text)
    return False, "cdp: session busy"


def _cdp_restore(c, draft, files, hb_text=None):
    """Put the composer back: draft text (as before), then re-attach the
    saved images through the composer's own hidden file input —
    DOM.setFileInputFiles is the same code path a user paste exercises.
    Any heartbeat text still sitting in the composer is scrubbed first —
    never leave it behind on any path.
    Returns the keep dir on attach failure, else None."""
    cur = ((c.eval(_CDP_STATE_JS) or {}).get("text") or "")
    if (hb_text and cur and cur != draft
            and cur.startswith(hb_text[:30])
            and not (draft or "").startswith(hb_text[:30])):
        log("composer had heartbeat text, cleared")
    _cdp_draft_back(c, draft)
    if hb_text:                             # an async bounce can re-insert
        cur = ((c.eval(_CDP_STATE_JS) or {}).get("text") or "")  # mid-restore
        if (cur and cur != draft and cur.startswith(hb_text[:30])
                and not (draft or "").startswith(hb_text[:30])):
            log("composer had heartbeat text, cleared")
            _cdp_draft_back(c, draft)
    if not files:
        return None
    ok = False
    try:
        if _cdp_attach(c, files):
            deadline = time.time() + 4
            while time.time() < deadline:
                if (c.eval(_CDP_ATT_COUNT_JS) or 0) == len(files):
                    ok = True
                    break
                time.sleep(0.2)
    except Exception as e:
        log("attach restore error: %s" % e)
    if ok:
        log("attachments restored (%d)" % len(files))
        for f in files:
            try:
                os.remove(f)
            except OSError:
                pass
        return None
    d = os.path.dirname(files[0])
    log("attachments kept at %s" % d)     # never lose the user's images
    return d


def _cdp_key(c, key, code, vk, modifiers=0, text=None):
    """One key press via Input.dispatchKeyEvent. Keys that must produce
    text (submit Enter) use keyDown with `text`; bare control chords use
    rawKeyDown."""
    down = {"type": "keyDown" if text else "rawKeyDown",
            "key": key, "code": code,
            "windowsVirtualKeyCode": vk, "modifiers": modifiers}
    if text:
        down["text"] = text
    c.call("Input.dispatchKeyEvent", down)
    c.call("Input.dispatchKeyEvent",
           {"type": "keyUp", "key": key, "code": code,
            "windowsVirtualKeyCode": vk, "modifiers": modifiers})


def _cdp_draft_back(c, draft):
    """Clear the composer (Ctrl+A, Backspace) and re-insert the user's
    draft. A multi-line draft goes back in one insertText — \n lands as
    a real line break (Shift+Enter key events don't reach Slate)."""
    c.eval(_CDP_FOCUS_JS)
    _cdp_key(c, "a", "KeyA", 65, modifiers=2)          # select all
    time.sleep(0.1)
    _cdp_key(c, "Backspace", "Backspace", 8)
    time.sleep(0.2)
    if draft:                                        # \n inside insertText
        c.call("Input.insertText", {"text": draft})  # lands as real line
        time.sleep(0.3)                              # breaks in Slate
    st = c.eval(_CDP_STATE_JS)
    cur = (st or {}).get("text") or ""
    if draft:
        log("draft restored in composer" if cur == draft
            else "draft restore mismatch")
    elif cur:
        log("composer not cleared: %r" % cur[:40])


def _cdp_send_body(c, text, submit):
    """The send proper — inside one connected _Cdp session with focus
    emulation already enabled."""
    st = c.eval(_CDP_STATE_JS)
    if not st or not st.get("found"):
        return False, "cdp: composer not found"
    if st.get("label") in ("Cancel", "Queue"):
        return False, "cdp: session busy"
    draft = st.get("text") or ""
    # attachments: the heartbeat must not carry the user's composer
    # images into the chat — pull them off first, put them back after
    att = c.eval(_CDP_ATTS_JS)
    files = []
    if att and att.get("atts"):
        if any(a.get("kind") != "image" for a in att["atts"]):
            return False, ATTACH_HOLD          # unknown chip: don't touch
        files = _save_atts(att["atts"])        # save BEFORE removing
        deadline = time.time() + 2
        n = len(att["atts"])
        while n and time.time() < deadline:
            if not c.eval(_CDP_CLICK_ONE_ATT_JS):
                break
            time.sleep(0.25)
            n = c.eval(_CDP_ATT_COUNT_JS) or 0
        log("attachments off: %d left" % n)
        if n != 0:                             # removal failed — put back
            try:                               # only what actually left
                _cdp_attach_missing(c, att["atts"], files)
            except Exception as e:
                log("attach back error: %s" % e)
            return False, ATTACH_HOLD
    log("send(cdp): composer ok%s%s"
        % (" +draft" if draft else "",
           " +%datt" % len(files) if files else ""))
    c.eval(_CDP_FOCUS_JS)
    if draft:
        _cdp_key(c, "a", "KeyA", 65, modifiers=2)      # replace the draft,
        time.sleep(0.15)                             # it goes back after
    c.call("Input.insertText", {"text": text})
    time.sleep(0.3)
    st = c.eval(_CDP_STATE_JS)
    cur = (st or {}).get("text") or ""
    if not cur.startswith(text[:20]):
        _cdp_restore(c, draft, files, text)
        return False, "heartbeat did not land in composer"
    if not submit:
        if os.environ.get("BUDDY_TEST_BOUNCE"):
            # test hook: fake Devin bouncing the message back — the
            # heartbeat text reappears right after the "sent" detection
            c.call("Input.insertText", {"text": text})
            time.sleep(0.3)
            log("test: simulated bounced heartbeat")
            return _cdp_bounce_cleanup(c, text, draft, files)
        kept = _cdp_restore(c, draft, files, text)
        return True, ("dry run · attachments kept at " + kept
                      if kept else "dry run")
    try:                                             # arm the scroll hold
        h = c.eval("(%s, window.__cbGuard.arm(15000))"  # in the same session
                   % _CDP_GUARD_JS.strip())
    except Exception:
        h = None
    if h is None:
        log("cdp: arm failed")
    elif h.get("armed"):
        log("cdp: armed st=%s dist=%s" % (h.get("st"), h.get("dist")))
    else:
        log("cdp: near bottom, no hold")
    t0 = time.time()
    clicked = c.eval(
        "(() => { const b ="
        " [...document.querySelectorAll('button[aria-label]')]"
        ".find(e => e.getAttribute('aria-label') === 'Send'"
        " && e.offsetParent);"
        " if (!b || b.disabled) return false; b.click(); return true; })()")
    if not clicked:
        _cdp_key(c, "Enter", "Enter", 13, text="\r")
    sent = False
    for _ in range(12):              # poll up to 3s — the composer going
        time.sleep(0.25)             # empty means the message left
        st = c.eval(_CDP_STATE_JS)
        if st and not (st.get("text") or ""):
            sent = True
            break
    if not sent:
        _cdp_restore(c, draft, files, text)
        return False, "enter did not submit"
    left = 1.5 - (time.time() - t0)  # same settle time as the uia path
    if left > 0:
        time.sleep(left)
    try:
        s = c.eval("window.__cbGuard.stat()")
    except Exception:
        s = None
    if s:
        log("cdp: after send st=%s dist=%s drops=%s"
            % (s.get("st"), s.get("dist"), s.get("drops")))
    # settle over — Devin may have bounced the send back into the composer
    # (a turn finishing under us, or it became a queued item). Re-check
    # before restoring anything: heartbeat text back means NOT sent.
    st = c.eval(_CDP_STATE_JS)
    cur = (st or {}).get("text") or ""
    bounced = cur.startswith(text[:30])
    if not bounced:
        bz = c.eval(_CDP_BUSY_JS) or {}
        bounced = bool(cur) and (bz.get("label") in ("Cancel", "Queue")
                                 or bz.get("queued"))
    if bounced:
        return _cdp_bounce_cleanup(c, text, draft, files)
    kept = _cdp_restore(c, draft, files, text)  # composer is empty — draft
    if kept:                              # images go back
        return True, "sent · attachments kept at " + kept
    return True, "sent +draft back" if draft else "sent"


def _cdp_send(win_title, text, submit=True):
    """Send `text` into the composer entirely in-page — no window
    activation, no clipboard. Returns (ok, detail), or None when CDP
    itself is unreachable / there's no page target, so the caller can
    fall back to UIA."""
    try:
        c = _Cdp(win_title)
    except Exception as e:
        reason = "%s: %s" % (type(e).__name__, e)
        if reason not in _cdp_logged:
            _cdp_logged.add(reason)
            log("cdp connect failed: %s" % reason)
        return None
    try:
        with c:
            c.call("Emulation.setFocusEmulationEnabled", {"enabled": True})
            try:
                return _cdp_send_body(c, text, submit)
            finally:
                try:
                    c.call("Emulation.setFocusEmulationEnabled",
                           {"enabled": False})
                except Exception:
                    pass
    except Exception as e:
        return False, "cdp error: %s" % e


def _send_into(win, text):
    """Type text into the composer of whatever session `win` is showing."""
    import uiautomation as uia
    edit = _find_prompt_edit(win)
    if not edit:
        return False, "composer not found"
    if (uia.ButtonControl(searchFromControl=win, searchDepth=40,
                          Name="Remove attachment").Exists(1.5)
            or uia.ButtonControl(searchFromControl=win, searchDepth=40,
                                 SubName="Remove attachment").Exists(1.5)):
        return False, ATTACH_HOLD   # can't hold attachments via UIA
    draft = _edit_value(edit)
    has_draft = not _edit_is_empty(draft)
    cdp = _cdp_stat(win.Name)
    if cdp and cdp.get("ok"):            # scroll guard live: no UIA anchors
        anchor = None
        log("cdp: st=%s dist=%s" % (cdp.get("st"), cdp.get("dist")))
    else:
        anchor = _top_anchor(win)
        log("cdp: unavailable, uia anchors")
    log("anchor: %r%s"
        % (("user msg %r@%d +%d" % (anchor[0][1][:18], anchor[0][0],
            len(anchor) - 1)) if anchor else "none",
           " +draft" if has_draft else ""))
    try:
        win.SetActive()                  # bring Devin to front — otherwise the
        time.sleep(0.8)                  # real click lands on whatever covers it
    except Exception:
        pass
    edit = _find_prompt_edit(win)        # re-resolve after focus change
    if not edit:
        return False, "composer lost after activate"
    edit.Click()                         # real click -> caret in editor
    time.sleep(0.6)
    draft = _edit_value(edit)            # freshest possible capture — the
    has_draft = not _edit_is_empty(draft)  # user may keep typing during setup
    log("send: composer ok%s" % (" +draft" if has_draft else ""))
    old_clip = _clip_get()
    log("send: clipboard read")
    pasted = False
    if _clip_set(text):
        for _ in range(2):               # paste is atomic — a user keystroke
            if has_draft:                # mid-type can't interleave, but a
                uia.SendKeys("{Ctrl}a")  # stray key may deselect; retry once
                time.sleep(0.15)
            uia.SendKeys("{Ctrl}v")
            time.sleep(0.4)
            if text[:20] in _edit_value(edit):
                pasted = True
                break
    if pasted:
        log("send: pasted, submitting")
    if not pasted:
        if has_draft:
            _put_draft_back(win, draft)
        if old_clip is not None:
            _clip_set(old_clip)
        return False, "heartbeat did not land in composer"
    if cdp and cdp.get("ok"):
        h = _cdp_arm(win.Name, 15000)    # drop scroll-to-bottom writes while
        if h is None:                    # the heartbeat turn lands
            log("cdp: arm failed")
        elif h.get("armed"):
            log("cdp: armed st=%s dist=%s" % (h.get("st"), h.get("dist")))
        else:
            log("cdp: near bottom, no hold")
    uia.SendKeys("{Enter}")
    time.sleep(1.5)
    if cdp and cdp.get("ok"):
        s = _cdp_stat(win.Name)
        if s:
            log("cdp: after send st=%s dist=%s drops=%s"
                % (s.get("st"), s.get("dist"), s.get("drops")))
    else:
        _restore_anchor(win, anchor)     # put the user back where they were
    if not _edit_is_empty(_edit_value(edit)):
        if has_draft:
            _put_draft_back(win, draft)
        if old_clip is not None:
            _clip_set(old_clip)
        return False, "enter did not submit"
    if has_draft:
        _put_draft_back(win, draft)      # give the user their half-typed
    if old_clip is not None:             # message back; clipboard restored
        _clip_set(old_clip)              # last, after every paste is done
    return True, "sent +draft back" if has_draft else "sent"


def _open_via_search(win, title, first_msg=""):
    """Open session `title` through the 'Search agents' palette — the only
    navigation that reaches sessions not currently open as tabs.
    Returns True when the composer ends up visible."""
    import uiautomation as uia
    btn = uia.ButtonControl(searchFromControl=win, searchDepth=30,
                            SubName="Search agents")
    if not btn.Exists(4):
        return False
    try:
        btn.GetInvokePattern().Invoke()
    except Exception:
        r = btn.BoundingRectangle
        uia.Click((r.left + r.right) // 2, (r.top + r.bottom) // 2)
    time.sleep(1.5)
    sb = uia.EditControl(searchFromControl=win, searchDepth=60,
                         SubName="Search sessions")
    if not sb.Exists(4):
        return False
    r = sb.BoundingRectangle
    uia.Click((r.left + r.right) // 2, (r.top + r.bottom) // 2)
    time.sleep(0.5)
    uia.SendKeys(_sendkeys_escape(title))
    time.sleep(2.0)
    uia.SendKeys("{Enter}")              # picks the best-matching result
    time.sleep(3.0)
    # verify we landed on the right session — a fuzzy mismatch must not
    # get a heartbeat typed into it
    if not _name_matches(_win_session(win), title, first_msg):
        uia.SendKeys("{Esc}")
        return False
    return _find_prompt_edit(win) is not None


def inject_heartbeat(title, first_msg="", text=HEARTBEAT, restore=None):
    """Send heartbeat text into session `title` via the open Devin window.

    Order: (1) the window already showing that session; (2) the session as
    an open tab — switch, send, switch back; (3) the Search-agents palette —
    open, send, reopen. `restore` = (title, first_msg) of the session with
    the most recent real user activity — where the user actually is — else
    fall back to whatever the window was showing. Never creates a session.
    Real mouse clicks: the composer is contenteditable.
    """
    import uiautomation as uia
    uia.SetGlobalSearchTimeout(5)
    wins = [w for w in uia.GetRootControl().GetChildren()
            if w.ControlTypeName == "WindowControl" and "Devin" in (w.Name or "")]
    if not wins:
        return False, "no Devin window"
    for w in wins:
        if _name_matches(_win_session(w), title, first_msg):
            try:
                r = _cdp_send(w.Name, text)   # in-page send: no focus steal
            except Exception:
                r = None
            if r is not None:
                log("send: via cdp (no focus)")
                return r
            log("send: via uia (window activated)")
            try:
                return _send_into(w, text)
            except Exception as e:
                return False, "uia error: %s" % e
    for w in wins:
        cur = _win_session(w)
        rt, rfm = restore if restore else (cur, cur)
        if _name_matches(rt, title, first_msg):
            rt, rfm = cur, cur            # injecting the most-recent session —
                                          # nothing to "go back" to
        tab = _find_tab(w, title, first_msg)
        if tab is not None:
            try:
                orig_anchor = _top_anchor(w)   # user's spot in CURRENT session
                _select_tab(w, tab)
                ok, detail = _send_into(w, text)
                back = _find_tab(w, rt, rfm)
                if back is not None:
                    _select_tab(w, back)
                    _restore_anchor(w, orig_anchor)
                return ok, detail
            except Exception as e:
                return False, "uia error: %s" % e
        try:
            orig_anchor = _top_anchor(w)
            if _open_via_search(w, title, first_msg):
                ok, detail = _send_into(w, text)
                if not _name_matches(_win_session(w), rt, rfm):
                    _open_via_search(w, rt, rfm)  # back to latest activity
                _restore_anchor(w, orig_anchor)
                return ok, detail
        except Exception as e:
            return False, "uia error: %s" % e
    return False, "session not open in a Devin window"


# ------------------------------------------------------------------ monitor

class Monitor(threading.Thread):
    """Keeps ONE user-picked session's cache warm on a fixed interval."""
    uses_cdp = True                       # probe Devin's CDP port in run()

    def __init__(self, on_state, idle_secs):
        super().__init__(daemon=True)
        self.on_state = on_state
        self.idle_secs = idle_secs
        self.stop_flag = threading.Event()
        self.target = None                # (sid, title, first_msg)
        self.next_due = 0                 # floor: never inject before this
        self.manual = False               # 立即续期 pressed — skip the floor
        self.sess = None                  # latest session_state of target
        self.sess_sid = None              # sid that sess belongs to
        self._busy_logged = False         # logged this busy episode already
        self._held_logged = False         # logged this attachment episode
        self._due_shown = -1              # last sched-logged due time
        self.fails = 0
        self.last_inject = 0
        self.last_log = ""
        self.cycle_since = 0              # epoch s when guard started
        self.cycle_tracks = None          # per-role tracks, current cycle
        self.prev_tracks = None           # tracks remembered after stop
        self.cycle_saved = 0.0            # USD: rescues − heartbeat cost
        self.prev_saved = 0.0             # previous cycle's figure
        self.cycle_rescued = self.prev_rescued = 0
        self._titles = {}                 # sid -> title of guarded ones
        self.cycle_hb = self.prev_hb = 0
        self.last_seen_tracks = None      # newest live tracks ever seen —
                                          # survives stop/restart of cycles
        self._ledger = Ledger(LEDGER_FILE)
        self._acct = {}                   # sid -> turn accounting state
        self.rescues = []                 # recent rescues, newest last
        self.warm = {}                    # sid -> (title, tracks): every
        self._warm_at = 0                 # session whose cache is alive
        self._warm_busy = False
        self.tok_text = ""
        self.pending_rows = []
        self._metrics_busy = False        # a slow db query must never
        self._metrics_tick = 0            # stall or pile up in the loop
        self.injecting = False            # inside _do_inject right now
        self.due_at = 0                   # epoch when the countdown fires
        self.cdp_ok = False               # startup CDP reachability check
        self.cli = None                   # (devin_cli.Hub, Upstream) once
                                          # attach_cli bound the proxy
        self._cli_hb = {}                 # sid -> start of the last CLI
                                          # heartbeat (no db row for it)
        self.maint = load_settings()["maint_min"] * 60   # 缓存维护时间:
                                          # stop guarding this long after
                                          # the last real turn
        self.cli_locked = {}              # sid -> pid: open in a terminal
        self._locked_at = 0               # that bypasses the proxy (scan
                                          # at start + on refresh only)

    def attach_cli(self, port=devin_cli.PORT):
        """Start the Devin CLI proxy (devb routes the CLI through it).
        Returns the server, or None when the port is taken (another buddy
        has it) — CLI sessions then show as unproxied."""
        devin_cli.log = log
        hub, up = devin_cli.Hub(), devin_cli.Upstream(
            devin_cli.resolve_upstream())
        try:
            srv = devin_cli.start_proxy(hub, up, port)
        except OSError as e:
            log("devin cli proxy: port %d busy (%s)" % (port, e))
            return None
        self.cli = (hub, up)
        self._cli_port = port
        log("devin cli proxy 127.0.0.1:%d -> %s" % (port, up.base))
        threading.Thread(target=self.rescan_cli, daemon=True).start()
        return srv

    def rescan_cli(self):
        """Which open CLI sessions bypass the proxy (terminal opened
        before the panel). Once at start and on the panel's refresh —
        nothing changes it in between except the user reopening one."""
        if not self.cli or os.name != "nt":
            return
        try:
            found = devin_cli.scan_unrouted(self._cli_port)
        except Exception as e:
            log("cli scan error: %s" % e)
            return
        if found != self.cli_locked:
            log("cli scan: %d session(s) opened before Buddy: %s"
                % (len(found), ", ".join(sorted(found)) or "-"))
        self.cli_locked, self._locked_at = found, time.time()

    def _last_real(self, sid):
        """Start of the guarded session's newest real (non-heartbeat) turn
        — sessions.db via _account, the proxy for CLI turns — never before
        the guard started."""
        a = self._acct.get(sid) or {}
        ln = self._cli_lane(sid)
        return max(self.cycle_since, a.get("real") or 0,
                   (ln.real_start or 0) if ln else 0)

    def _locked(self, sid):
        """Open in a terminal that bypasses the proxy — unless a turn has
        come through the proxy since the scan (reopened with the env)."""
        if sid not in self.cli_locked:
            return False
        ln = self._cli_lane(sid)
        if ln and (ln.real_start or 0) > self._locked_at:
            self.cli_locked.pop(sid, None)
            return False
        return True

    def _source(self, sid):
        """"desktop" / "cli" / None for the panel's tag — subclasses for
        other harnesses answer from their own session files."""
        return session_source(sid)

    def set_target(self, sid, title, first_msg):
        self.target = (sid, title, first_msg)
        self._titles[sid] = title
        self.fails = 0
        self.cycle_since = int(time.time())
        self.cycle_tracks = None
        self.cycle_saved = 0.0
        self.cycle_rescued = self.cycle_hb = 0
        self.sess = None
        self.sess_sid = None
        self.manual = False
        self.due_at = 0
        self.next_due = 0                 # when to fire is decided in _tick

    def request_inject(self):
        if self.target:
            self.manual = True

    def clear_target(self):
        prev = self.cycle_tracks or self.last_seen_tracks
        if prev:
            self.prev_tracks = prev

            self.prev_saved = self.cycle_saved
            self.prev_rescued, self.prev_hb = self.cycle_rescued, self.cycle_hb
        self.cycle_tracks = None
        self.cycle_saved = 0.0
        self.cycle_rescued = self.cycle_hb = 0
        self.target = None
        # Instant visual feedback — the next tick may be seconds away or
        # parked inside a slow UIA inject, so refresh the display right
        # here instead of waiting for the loop to come back around.
        self._refresh_tok_text()
        self.due_at = 0
        self.pending_rows = [dict(
            sid="", title="未选择会话（在上方列表选一个）",
            state="", left="", color="#888", kind="none")]

    def run(self):
        # prove the bundled websocket-client can reach Devin's CDP port —
        # in a worker: the UIA walk in _devin_win_title can hang for minutes
        # when Devin's UI thread is busy, and must never block the loop
        # re-checked every minute: a Devin update can rewrite argv.json and
        # drop the port, and heartbeats would quietly fall back to UIA
        def probe():
            prev = None
            while not self.stop_flag.is_set():
                try:
                    st = _cdp_stat(_devin_win_title())
                except Exception:
                    st = None
                self.cdp_ok = bool(st and st.get("ok"))
                if self.cdp_ok != prev:
                    prev = self.cdp_ok
                    self.last_log = log(
                        "cdp: " + ("available" if self.cdp_ok else
                                   "unavailable — heartbeats fall back to "
                                   "UIA (steals focus); check "
                                   "remote-debugging-port in "
                                   "~/.devin/argv.json"))
                self.stop_flag.wait(60)
        if self.uses_cdp:
            threading.Thread(target=probe, daemon=True).start()
        while not self.stop_flag.is_set():
            rows = []
            try:
                rows = self._tick()
            except Exception:
                log("tick error:\n" + traceback.format_exc())
            try:
                self.on_state(rows, self.last_log)
            except Exception:
                pass
            self.stop_flag.wait(POLL)

    def _ctl_poll(self):
        """Control file for scripted E2E checks: write 'stop', 'inject', or
        'start <title-substr>' to buddy.ctl next to the log. Demo runs use
        buddy.ctl.demo so a live exe can't eat the commands (or vice
        versa)."""
        path = CTL_FILE + ".demo" if DEMO else CTL_FILE
        try:
            if not os.path.exists(path):
                return
            cmd = open(path, encoding="utf-8").read().strip()
            os.remove(path)
        except OSError:
            return
        if cmd == "stop":
            log("ctl: stop")
            self.clear_target()
        elif cmd == "inject":
            log("ctl: inject")
            self.request_inject()
        elif cmd.startswith("view"):
            v = cmd[4:].strip()
            c = getattr(self, "_winctl", None)   # underscore: pywebview's
            # js_api walker skips private attrs — a public `winctl` makes it
            # recurse mon->ctl->win->native->Rectangle.Empty forever
            if c and v in ("dock", "peek", "panel"):
                log("ctl: view -> %s" % v)
                c.set_view(v)
        elif cmd.startswith("js:"):      # demo-only page eval for
            if DEMO:                     # screenshot scripting — not a
                c = getattr(self, "_winctl", None)  # production surface
                if c:
                    log("ctl: js")
                    c.eval_js(cmd[3:].strip())
        elif cmd.startswith("start"):
            want = cmd[5:].strip().lower()
            for sid, title, fm in list_sessions():
                if want in title.lower():
                    log("ctl: start -> %s" % title)
                    self.set_target(sid, title, fm)
                    return
            log("ctl: no session matching %r" % want)

    def _refresh_tok_text(self):
        now = int(time.time())
        cur = self.cycle_tracks
        kind = "本轮"
        if not cur:
            cur = self.prev_tracks or self.last_seen_tracks
            kind = "上轮"
        if cur:
            parts = []
            ages = []
            for t in cur:
                m = t.get('metrics')
                lbl = {"lead": "lead", "sidekick": "sidekick"
                       }.get(t.get('role'), "")
                if m:
                    s = fmt_tok(m)
                    ages.append(max(0, now - int(m.get('_ts') or now)))
                else:
                    s = (t.get('model') or "?") + " —"
                parts.append((lbl + " " + s) if lbl else s)
            line1 = "%s缓存 %s" % (kind, " | ".join(parts))
            if ages:
                age = min(ages)
                line1 += (" · %ds前" % age if age < 90
                          else " · %d分钟前" % (age // 60))
            if self.target and not self.cycle_tracks:
                line1 += " · 等本轮首条"
            costs = [turn_cost(t['metrics']) for t in cur
                     if t.get('metrics')]
            costs = [c for c in costs if c]
            line2 = ""
            if costs:
                saved = (self.cycle_saved if self.cycle_tracks
                         else self.prev_saved)
                line2 = ("≈$%.4f/轮 · 无缓存 $%.4f · 已省 $%.3f"
                         % (sum(c[0] for c in costs),
                            sum(c[1] for c in costs), saved))
            new_text = line1 + ("\n" + line2 if line2 else "")
        else:
            new_text = ("等待 metrics 落库…" if self.target
                        else "")
        if new_text != self.tok_text:
            self.tok_text = new_text
            if self.cycle_tracks:
                log("cache tokens: %s" % " | ".join(
                    fmt_tok(t['metrics']) for t in self.cycle_tracks
                    if t.get('metrics')))
            log("tok: %s" % new_text.replace("\n", " | "))

    def _tick(self):
        now = int(time.time())
        self._ctl_poll()
        if self.target:
            self._metrics_poll()         # worker drops into cycle_tracks
        if now - self._warm_at >= 10:
            self._warm_at = now
            self._warm_poll()
        self._refresh_tok_text()
        if not self.target:
            return [dict(sid="", title="未选择会话（在上方列表选一个）",
                         state="", left="", color="#888", kind="none")]
        sid, title, first_msg = self.target
        idle = now - self._last_real(sid)
        if idle > self.maint and not self.manual:
            # 缓存维护时间 is up: nobody has talked to it for that long —
            # stop paying heartbeats for a session that's been left
            self.last_log = log("auto-stop: %s idle %d min (keep-alive %d "
                                "min)" % (title, idle // 60, self.maint // 60))
            self.clear_target()
            return self.pending_rows
        if self._locked(sid):
            # in a terminal opened before Buddy: no proxy, no window —
            # nothing can renew it until it's reopened
            self.due_at = 0
            self.manual = False
            return [dict(sid=sid, title=title, state="CLI 在 Buddy 之前启动",
                         left="", color="#c90", kind="held",
                         reason="cli_locked")]
        if self._via_proxy(sid) or session_source(sid) == "cli":
            # running in a terminal (started there, or a Desktop session
            # resumed with `devin -r`): no window to type into — renewed
            # through the proxy instead
            return self._tick_cli(sid, title, now)
        if self.fails >= 3:
            return [dict(sid=sid, title=title, state="连续失败已停（重选可恢复）",
                         left="", color="#888", kind="failed")]
        if self.sess_sid != sid:         # session_state hasn't landed yet
            if now - self.cycle_since < 30:
                return [dict(sid=sid, title=title, state="读取会话状态…",
                             left="", color="#888", kind="loading")]
            sess = None                  # give up waiting — treat the cache
        else:                            # as dead and fire once below
            sess = self.sess
        refresh = (sess or {}).get('refresh_ts')
        if refresh is None or now - refresh >= TTL:
            # cache dead (or never seen alive): a heartbeat would only pay
            # to re-write it, which the next real turn pays anyway. Keep
            # the target and wait — that turn moves refresh_ts and the
            # schedule picks up on its own. Renew now included.
            return self._expired_row(sid, title)
        due = refresh + self.idle_secs
        self._expired_logged = False     # alive again: log the next expiry
        if self.manual:
            due = now                    # manual: fire now unless busy
        else:
            due = max(due, self.next_due)  # next_due = backoff floor only
        if abs(due - self._due_shown) > 2:
            self._due_shown = due
            log("sched: refresh=%s due=%s"
                % (time.strftime("%H:%M:%S", time.localtime(refresh))
                   if refresh else "--:--:--",
                   time.strftime("%H:%M:%S", time.localtime(due))))
        if now < due:
            self.due_at = due
            return [dict(sid=sid, title=title, state="守护中",
                         left="%d:%02d 后注入"
                         % divmod(int(due - now), 60),
                         color="#3a3", kind="guarding")]
        self.due_at = 0
        # due: a fresh synchronous check — the snapshot may be stale
        st = session_state(sid)
        if st is None:
            return [dict(sid=sid, title=title, state="读取会话状态…",
                         left="", color="#888", kind="loading")]
        self.sess, self.sess_sid = st, sid
        if not st['refresh_ts'] or now - st['refresh_ts'] >= TTL:
            return self._expired_row(sid, title)  # died since the schedule
        if (not self.manual and st['refresh_ts']
                and st['refresh_ts'] + self.idle_secs > now + 1):
            self.due_at = st['refresh_ts'] + self.idle_secs
            return [dict(sid=sid, title=title, state="守护中",
                         left="%d:%02d 后注入"
                         % divmod(int(st['refresh_ts']
                                      + self.idle_secs - now), 60),
                         color="#3a3", kind="guarding")]
        ui = _cdp_busy(title, first_msg)
        busy = ui if ui is not None else st['busy']
        if busy:
            if not self._busy_logged:
                self._busy_logged = True
                self.last_log = log("skip: session busy (%s)"
                                    % ("ui" if ui is not None
                                       else "db: " + st['why']))
            return [dict(sid=sid, title=title, state="会话运行中·等结束",
                         left="", color="#c90", kind="busy")]
        self._busy_logged = False
        self.manual = False
        rows = []
        self.injecting = True
        try:
            self._do_inject(sid, title, first_msg, rows)
        finally:
            self.injecting = False
        if not self.target:
            # stopped mid-inject — don't flash "已续期" over the stopped row
            return [dict(sid="", title="未选择会话（在上方列表选一个）",
                         state="", left="", color="#888", kind="none")]
        return rows

    def _expired_row(self, sid, title):
        """The target's cache is gone: no heartbeat, just wait for the
        user's next message to bring it back."""
        self.manual = False
        self.due_at = 0
        if not getattr(self, "_expired_logged", False):
            self._expired_logged = True
            self.last_log = log("cache expired -> %s: waiting for the next "
                                "message, no heartbeat" % title)
        return [dict(sid=sid, title=title, state="缓存已过期·等下一条消息",
                     left="", color="#888", kind="expired")]

    def _cli_lane(self, sid):
        """The proxy lane that renews this CLI session's lead model, or
        None (no proxy, or no turn through it since the buddy started)."""
        if not self.cli:
            return None
        tr = self.cycle_tracks or []      # the metrics worker's lead model
        model = (tr[0].get("model") if tr and tr[0].get("sid") == sid
                 else None)
        return self.cli[0].lead(sid, model)

    def _via_proxy(self, sid):
        """The session's latest turns go through the proxy: a capture in
        the last COVERAGE that sessions.db doesn't show anything newer
        than (a later turn there came from elsewhere — the capture would
        no longer match the cached prefix)."""
        ln = self._cli_lane(sid)
        if not ln or not ln.real_start or time.time() - ln.real_start > COVERAGE:
            return False
        if self.cli[0].inflight(sid):     # a turn through us right now
            return True
        db = ((self.sess or {}).get("refresh_ts")
              if self.sess_sid == sid else None)
        return not db or db <= ln.real_start + 30

    def _cli_refresh(self, sid):
        """Last cache renewal of a CLI session: sessions.db has the real
        turns, only the proxy knows about heartbeats."""
        ln = self._cli_lane(sid)
        db = ((self.sess or {}).get("refresh_ts")
              if self.sess_sid == sid else None)
        ts = [t for t in (db, ln and ln.refresh_ts) if t]
        return max(ts) if ts else None

    def _tick_cli(self, sid, title, now):
        """A terminal session: same schedule as Desktop's, but the
        heartbeat re-sends its last proxied turn (devin_cli). Busy = a
        request through the proxy right now — a CLI running a tool is
        between requests, and the heartbeat sits after its last one."""
        def row(kind, state, reason="", color="#888", left=""):
            return [dict(sid=sid, title=title, state=state, left=left,
                         color=color, kind=kind, reason=reason)]
        if not self.cli:
            self.due_at = 0
            return row("held", "CLI 会话·代理未启动", "cli_no_proxy",
                       "#c90")
        if self.fails >= 3:
            return row("failed", "连续失败已停（重选可恢复）")
        ln = self._cli_lane(sid)
        if ln is None:                    # not launched with devb, or no
            self.due_at = 0               # turn since the buddy started
            return row("loading", "等它下一条消息（用 devb 启动）",
                       "cli_wait")
        refresh = self._cli_refresh(sid)
        if refresh is None or now - refresh >= TTL:
            return self._expired_row(sid, title)
        self._expired_logged = False
        due = now if self.manual else max(refresh + self.idle_secs,
                                          self.next_due)
        if now < due:
            self.due_at = due
            return row("guarding", "守护中", color="#3a3",
                       left="%d:%02d 后注入" % divmod(int(due - now), 60))
        self.due_at = 0
        if self.cli[0].inflight(sid):
            if not self._busy_logged:
                self._busy_logged = True
                self.last_log = log("skip: CLI request in flight -> %s"
                                    % title)
            return row("busy", "会话运行中·等结束", color="#c90")
        self._busy_logged = False
        manual, self.manual = self.manual, False
        self.last_log = log("cli heartbeat -> %s (%s)%s"
                            % (title, ln.model, " (manual)" if manual else ""))
        self.injecting = True
        try:
            ok, detail, usage = devin_cli.send_heartbeat(self.cli[1],
                                                         self.cli[0], ln)
        finally:
            self.injecting = False
        if not self.target:               # stopped while it was out
            return [dict(sid="", title="未选择会话（在上方列表选一个）",
                         state="", left="", color="#888", kind="none")]
        now = int(time.time())
        if not ok:
            self.fails += 1
            self.next_due = now + 20      # retry soon, not a full interval
            self.last_log = log("cli heartbeat failed -> %s: %s"
                                % (title, detail))
            return row("retry", "失败 %s" % detail, color="#c55")
        self.fails = 0
        self.next_due = 0                 # the schedule runs off refresh
        self.last_inject = now
        self._book_cli_hb(sid, ln, usage)
        self.last_log = log("cli heartbeat sent -> %s ✓ %s read=%s"
                            % (title, detail,
                               (usage or {}).get("cache_read_tokens")))
        return row("sent", "已续期", color="#3a3",
                   left="%d:%02d" % divmod(self.idle_secs, 60))

    def _book_cli_hb(self, sid, ln, usage):
        """A CLI heartbeat has no sessions.db row for _account to find:
        book its cost here and tell the rescue rule it went out."""
        m = dict(usage or {}, generation_model=(
            ln.model if _price_for(ln.model) else
            (session_roles(_sess_model_db(sid)) or [("", "")])[0][1]))
        c = turn_cost(m)
        self._ledger.add(hb=1, hb_cost=c[0] if c else 0)
        self.cycle_hb += 1
        if c:
            self.cycle_saved -= c[0]
        self._cli_hb[sid] = ln.hb_ts
        a = self._acct.get(sid)
        if a is not None:
            a["hb"] = max(a["hb"] or 0, ln.hb_ts or 0)

    def _metrics_poll(self):
        """session_tracks can take ~10s on a busy db — run it in a worker
        and drop the result straight into cycle_tracks so a slow query can
        never lose a row to a later box-reset. The heartbeat loop never
        blocks on sqlite."""
        self._metrics_tick += 1
        if self._metrics_busy or self._metrics_tick % 2:
            return
        self._metrics_busy = True

        def work(sid, since):
            t0 = time.time()
            try:
                tracks = session_tracks(sid)
                if tracks:
                    self.last_seen_tracks = tracks
                    for t in tracks:
                        m = t.get('metrics')
                        t['sid'] = sid    # rows outlive the target
                        t['this_cycle'] = bool(
                            m and (m.get('_ts') or 0) >= since)
                    self.cycle_tracks = tracks
                self._account(sid)
                st = session_state(sid)
                if st:
                    self.sess, self.sess_sid = st, sid
                log("metrics worker: %.1fs -> %s"
                    % (time.time() - t0,
                       " | ".join(fmt_tok(t['metrics'])
                                  for t in tracks if t.get('metrics'))
                       if tracks else "none"))
            except Exception as e:
                log("metrics worker error: %s" % e)
            finally:
                self._metrics_busy = False
        threading.Thread(target=work,
                         args=(self.target[0], self.cycle_since),
                         daemon=True).start()

    def _warm_poll(self):
        """Worker: which sessions still hold a live cache, for the card.
        last_activity_at is only a cheap prefilter (opening a session bumps
        it); the lead track's refresh decides."""
        if self._warm_busy:
            return
        self._warm_busy = True

        def work():
            try:
                self.warm = scan_warm()
            except Exception as e:
                log("warm scan error: %s" % e)
            finally:
                self._warm_busy = False
        threading.Thread(target=work, daemon=True).start()

    def _account(self, sid):
        """Book the guarded session's new lead requests into the ledger
        (metrics worker thread). A heartbeat exchange costs; a real turn
        is a rescue when the previous real request is > TTL old, a
        heartbeat went out after it, and the turn still read the cache —
        same rule as Claude Code's. History that predates the first look
        is only marked seen, never credited."""
        a = self._acct.get(sid)
        now = time.time()
        if a is None:
            hist = turn_log(sid, now - 2 * COVERAGE)
            real = [e["start"] for e in hist if not e["hb"]]
            hbs = [e["start"] for e in hist if e["hb"]]
            if self._cli_hb.get(sid):     # CLI heartbeats leave no rows
                hbs.append(self._cli_hb[sid])
            a = self._acct[sid] = {
                "seen": {e["rid"]: e["start"] for e in hist},
                "real": max(real, default=None),
                "hb": max(hbs, default=None),
                "hi": max([e["start"] for e in hist], default=now)}
            return
        title = (self.target[1] if self.target and self.target[0] == sid
                 else sid)
        mine = bool(self.target and self.target[0] == sid)
        hb_open = False                   # inside one heartbeat exchange
        for e in turn_log(sid, a["hi"] - 600):
            if e["rid"] in a["seen"]:
                hb_open = e["hb"]
                continue
            a["seen"][e["rid"]] = e["start"]
            a["hi"] = max(a["hi"], e["start"])
            m, c = e["m"], turn_cost(e["m"])
            if e["hb"]:
                kv = {"hb_cost": c[0] if c else 0}
                if not hb_open:           # tool loops: one renew, not n
                    kv["hb"] = 1
                    if mine:
                        self.cycle_hb += 1
                if mine and c:
                    self.cycle_saved -= c[0]
                a["hb"] = max(a["hb"] or 0, e["start"])
                self._ledger.add(**kv)
                hb_open = True
                continue
            hb_open = False
            kv = {"cache_saved": c[1] - c[0]} if c else {}
            prev = a["real"]
            if (prev and e["start"] - prev > TTL
                    and (a["hb"] or 0) > prev
                    and m.get('cache_read_tokens')):
                v = rescue_value(m)
                kv.update(saved=v, rescues=1)
                if mine:
                    self.cycle_saved += v
                    self.cycle_rescued += 1
                self.rescues.append({"sid": sid, "title": title, "usd": v,
                                     "at": now, "seen": False})
                del self.rescues[:-12]
                self.last_log = log("rescued turn -> %s: cache kept alive, "
                                    "saved $%.4f" % (title, v))
            a["real"] = max(prev or 0, e["start"])
            if kv:
                self._ledger.add(**kv)
        if len(a["seen"]) > 400:          # keep the window, drop the rest
            lo = a["hi"] - 1200
            a["seen"] = {k: v for k, v in a["seen"].items() if v >= lo}

    def pop_rescues(self):
        """Rescues the panel hasn't shown yet, oldest first."""
        return [r for r in self.rescues if not r["seen"]]

    def mark_rescues(self, upto):
        for r in self.rescues:
            if r["at"] <= upto:
                r["seen"] = True

    def _do_inject(self, sid, title, first_msg, rows, manual=False):
        if getattr(self, "_inject_busy", False):
            # a previous inject is still inside a hung UIA/clipboard call —
            # never stack a second typing session on top of it
            self.next_due = int(time.time()) + 30
            self.last_log = log("inject still busy -> %s (uia hung?)" % title)
            return
        self.last_log = log("injecting -> %s%s" % (title, " (manual)" if manual else ""))
        done = {}

        def job():
            try:
                done["r"] = inject_heartbeat(title, first_msg)
            except Exception as e:
                done["r"] = (False, "uia error: %s" % e)
            finally:
                self._inject_busy = False
        self._inject_busy = True
        th = threading.Thread(target=job, daemon=True)
        th.start()
        th.join(90)                     # hard watchdog — UIA/clipboard calls
        if th.is_alive():               # can block forever on a busy Devin
            self.fails += 1
            self.next_due = int(time.time()) + 30
            self.last_log = log("inject timeout -> %s (uia stuck)" % title)
            rows.append(dict(sid=sid, title=title,
                             state="UIA 卡死，重试中", left="", color="#c55",
                             kind="retry"))
            return
        ok, detail = done.get("r", (False, "no result"))
        now = int(time.time())
        if ok:
            self.fails = 0
            self.last_inject = now
            self.next_due = now + self.idle_secs
            self.last_log = log("heartbeat sent -> %s ✓" % title)
            if detail and "attachments kept at" in detail:
                self.last_log = log(
                    "attachments kept at %s" %
                    detail.split("attachments kept at", 1)[1].strip())
            self._held_logged = False
            rows.append(dict(sid=sid, title=title, state="已续期",
                             left="%d:%02d" % divmod(self.idle_secs, 60),
                             color="#3a3", kind="sent"))
        elif detail == "cdp: session busy":
            self.next_due = now + 5         # turn started under us — not a
            self.last_log = log("inject deferred -> %s: busy" % title)  # fail
            rows.append(dict(sid=sid, title=title, state="会话运行中·等结束",
                             left="", color="#c90", kind="busy"))
        elif detail == ATTACH_HOLD:
            # an attachment the heartbeat can't safely carry (non-image)
            # or that refused to come off — wait it out, don't count it
            self.next_due = now + 30
            if not self._held_logged:
                self._held_logged = True
                self.last_log = log("inject held -> %s: attachment in "
                                    "composer" % title)
            else:
                log("inject held -> %s: attachment in composer" % title)
            rows.append(dict(sid=sid, title=title,
                             state="attachment in composer", left="",
                             color="#c90", kind="held"))
        else:
            self.fails += 1
            self.next_due = now + 20        # retry soon, not a full interval
            self.last_log = log("inject failed -> %s: %s" % (title, detail))
            rows.append(dict(sid=sid, title=title,
                             state="失败 %s" % detail, left="", color="#c55",
                             kind="retry"))

    def ui_state(self):
        """Everything the webview needs for one frame — attribute reads
        only, no db hits."""
        now = int(time.time())
        row = (self.pending_rows or [{}])[0]
        kind = row.get("kind") or "none"
        if self.injecting:
            mode = "injecting"
        elif self.last_inject and now - self.last_inject < 4:
            mode = "sent"
        else:
            mode = kind
        sid = title = None
        if self.target:
            sid, title = self.target[0], self.target[1]
        refresh = None
        if sid and self.sess_sid == sid and self.sess:
            refresh = self.sess.get("refresh_ts")
        if sid and self.cli and (self._via_proxy(sid)
                                 or session_source(sid) == "cli"):
            refresh = self._cli_refresh(sid)   # heartbeats count too
        # the tag says where the session runs: a lock means a live
        # holder (Desktop's agent or a terminal), else where it was made.
        # Being proxied no longer means "cli" — Desktop's own agent goes
        # through the proxy too when it was started after Buddy.
        def _src(s):
            return ("cli" if s in self.cli_locked else self._source(s))
        # the card: the guarded session (or, once stopped, last round's),
        # then every other session whose cache is still alive — Devin
        # guards one session, so the rest are listed but not guarded
        cache = None
        cur = self.cycle_tracks
        kind2 = "本轮"
        if not cur:
            cur = self.prev_tracks or self.last_seen_tracks
            kind2 = "上轮"
        own = (cur or [{}])[0].get("sid")
        own_title = self._titles.get(own, "")

        def _row(t, ttl, rts_floor=None, guarded=True, rmode=None):
            m = t.get('metrics') or {}
            cr = int(m.get('cache_read_tokens') or 0)
            tot = (cr + int(m.get('input_tokens') or 0)
                   + int(m.get('cache_creation_tokens') or 0))
            cost = turn_cost(m) if m else None
            rts = t.get('refresh_ts')
            # the session-level refresh includes "a user row just
            # arrived" — for the lead track that's the same clock the
            # heartbeat schedule runs on
            if t.get('role') == 'lead' and rts_floor is not None:
                rts = max(rts or 0, rts_floor)
            lead = t.get('role') != 'sidekick'
            return {
                "sid": t.get('sid'),
                "source": _src(t.get('sid')) if t.get('sid') else None,
                # a fusion session's sidekick row sits under its lead and
                # keeps the role badge; the lead row names the session
                "title": (ttl or "") if lead else "",
                "role": t.get('role'),
                "model": t.get('model'),
                "family": t.get('family'),
                "read": cr,
                "hit": (100.0 * cr / tot) if tot else None,
                "age_s": (max(0, now - int(m.get('_ts') or now))
                          if m else None),
                "cache_left": (max(0, TTL - int(now - rts))
                               if rts else None),
                "cost": cost[0] if cost else None,
                "nocache_cost": cost[1] if cost else None,
                "this_cycle": bool(t.get('this_cycle')),
                "mode": rmode,
                "guarded": guarded,
            }

        tracks = []
        if cur:
            live = bool(self.cycle_tracks)
            tracks = [_row(t, own_title, refresh,
                           guarded=bool(sid and own == sid),
                           rmode=(None if sid and own == sid else "none"))
                      for t in cur]
            tracks[0]["hb"] = self.cycle_hb if live else self.prev_hb
        seen = {t["sid"] for t in tracks if t["sid"]}
        groups = [[_row(t, wtitle, guarded=False, rmode="none")
                   for t in wtracks]
                  for wsid, (wtitle, wtracks) in list(self.warm.items())
                  if wsid not in seen and wtracks]
        groups.sort(key=lambda g: -(g[0]["cache_left"] or 0))
        extras = [t for g in groups for t in g]   # sidekick stays by its lead
        if tracks or extras:
            live = bool(self.cycle_tracks)
            cache = {
                "kind": kind2 if tracks else "缓存中",
                "buddy": True,            # saved = Buddy's own, not cache's
                "rows": len(tracks) + len(extras) > 1,
                "waiting_first": bool(self.target and not self.cycle_tracks),
                "saved": self.cycle_saved if live else self.prev_saved,
                "rescued": (self.cycle_rescued if live
                            else self.prev_rescued),
                "tracks": tracks + extras,
            }
        return {
            "mode": mode,
            "target": ({"sid": sid, "title": title} if sid else None),
            "status": row.get("state") or "",
            "reason": row.get("reason") or "",
            "left": row.get("left") or "",
            "due_in": (max(0, int(self.due_at - now))
                       if (mode == "guarding" and self.due_at) else None),
            "idle_secs": self.idle_secs,
            "cdp": self.cdp_ok,
            "last_log": self.last_log,
            "cache": cache,
            "rescued": (self.pop_rescues() or [None])[-1],  # pop-out notice
            "ledger": self._ledger.month(),      # savings heatmap
        }


def _family(model, fused=False):
    """Mascot family for a model name. fusion-* sessions never reach this —
    session_roles() splits them into lead/sidekick models first; fused =
    the model is one half of such a pair."""
    m = (model or "").lower()
    if "claude" in m:
        return "claude"
    if m.startswith("swe") or "devin" in m:
        # solo SWE is the otter; half of a fusion pair keeps the trefoil
        return "devinmark" if fused else "devin"
    if "gpt" in m or re.match(r"^o\d", m):
        return "gpt"
    if "gemini" in m:
        return "gemini"
    if "kimi" in m:
        return "kimi"
    if "glm" in m or "zhipu" in m or "zai" in m:
        return "glm"
    if "deepseek" in m:
        return "deepseek"
    return "generic"


# ----------------------------------------------------------------------- ui

def build_ui(monitor, idle):
    import tkinter as tk
    try:
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
        # physical pixels for both screen metrics and window geometry —
        # without this winfo_screenwidth lies (logical 1707 vs real 2560)
        # and edge-docking lands mid-screen
    except Exception:
        pass
    root = tk.Tk()
    root.title("Cache Buddy")              # no "Devin" — the UIA finders
                                           # match any window named *Devin*
    root.attributes("-topmost", True)
    try:
        root.attributes("-toolwindow", True)   # keep out of taskbar
    except tk.TclError:
        pass
    root.configure(bg="#1e1e1e")
    sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
    dock = {"side": "right", "y": 200}        # parked edge + height
    pos = {"x": None, "y": None}             # live position while dragging
    PANEL_W, NUB_W, NUB_H = 330, 16, 150
    PANEL_H = [250]                          # recomputed from reqheight
    collapsed = {"v": False}
    dragging = {"v": False, "dx": 0, "dy": 0}
    parts = {}                               # widgets hidden when collapsed

    def apply_geo():
        w = NUB_W if collapsed["v"] else PANEL_W
        h = NUB_H if collapsed["v"] else PANEL_H[0]
        if dragging["v"] and pos["x"] is not None:
            root.geometry("%dx%d+%d+%d" % (w, h, pos["x"], pos["y"]))
            return
        x = (sw - w - 2) if dock["side"] == "right" else 2
        root.geometry("%dx%d+%d+%d" % (w, h, x, dock["y"]))

    def set_collapsed(v):
        collapsed["v"] = v
        for w in parts["all"]:
            if v:
                w.pack_forget()
            else:
                w.pack(fill=w._fill, **w._kw)
        if v:
            minbtn.pack_forget()
            header["text"] = "●"
            header.pack_forget()
            header.pack(pady=(18, 0))          # centered dot in the nub
        else:
            header.pack_forget()
            header.pack(side="left")           # back to header row
            header["text"] = header_txt
            minbtn.pack(side="right")
        apply_geo()

    apply_geo()

    top = tk.Frame(root, bg="#1e1e1e")
    top.pack(fill="x", padx=8, pady=(4, 0))
    header_txt = "● Cache Buddy" + (" [%ds]" % idle if idle != 255 else "")
    header = tk.Label(top, text=header_txt,
                      fg="#7ec8ff", bg="#1e1e1e", anchor="w",
                      font=("Segoe UI", 9, "bold"))
    header.pack(side="left")

    minbtn = tk.Button(top, text="—", command=lambda: set_collapsed(True),
                       bg="#1e1e1e", fg="#aaa", relief="flat",
                       font=("Segoe UI", 9), width=3, bd=0,
                       activebackground="#333")
    minbtn.pack(side="right")

    # free drag on the nub/header; on release the window snaps to the
    # nearest vertical edge (left or right) — it never stays mid-screen
    def dstart(e):
        dragging["v"] = True
        dragging["dx"] = e.x_root - root.winfo_rootx()
        dragging["dy"] = e.y_root - root.winfo_rooty()
    def dmove(e):
        if dragging["v"]:
            pos["x"] = e.x_root - dragging["dx"]
            pos["y"] = max(0, min(e.y_root - dragging["dy"], sh - 180))
            apply_geo()
    def dend(e):
        if dragging["v"] and pos["x"] is not None:
            w = root.winfo_width()
            dock["side"] = ("left" if pos["x"] + w // 2 < sw // 2
                            else "right")
            dock["y"] = pos["y"]
        dragging["v"] = False
        pos["x"] = pos["y"] = None
        apply_geo()
    for wdg in (top, header):
        wdg.bind("<Button-1>", dstart)
        wdg.bind("<B1-Motion>", dmove)
        wdg.bind("<ButtonRelease-1>", dend)

    # edge-dock hover: pointer over the window expands it, leaving
    # retracts it back to the nub. Pointer polling, not Enter/Leave —
    # those fire on child-widget crossings and flicker.
    def hover_watch():
        x, y = root.winfo_pointerx(), root.winfo_pointery()
        gx, gy = root.winfo_rootx(), root.winfo_rooty()
        gw, gh = root.winfo_width(), root.winfo_height()
        inside = (gx - 8 <= x <= gx + gw + 8
                  and gy - 8 <= y <= gy + gh + 8)
        if inside and collapsed["v"]:
            set_collapsed(False)
        elif not inside and not collapsed["v"] and not dragging["v"]:
            set_collapsed(True)
        root.after(350, hover_watch)

    body = tk.Frame(root, bg="#1e1e1e")
    body.pack(fill="both", expand=True, padx=8)
    body._fill, body._kw = "both", {"expand": True, "padx": 8}

    picker = tk.Listbox(body, height=4, bg="#111", fg="#ccc",
                        selectbackground="#2d6a9f", relief="flat",
                        font=("Segoe UI", 9), activestyle="none")
    picker.pack(fill="x")
    sessions = list_sessions()
    for sid, title, fm in sessions:
        picker.insert("end", " %s" % title[:36])
    picker._fill, picker._kw = "x", {}

    iv_row = tk.Frame(body, bg="#1e1e1e")
    iv_row.pack(fill="x", pady=(4, 0))
    iv_row._fill, iv_row._kw = "x", {"pady": (4, 0)}
    tk.Label(iv_row, text="间隔:", fg="#aaa", bg="#1e1e1e",
             font=("Segoe UI", 8)).pack(side="left")

    iv_btns = {}

    def set_iv(secs):
        monitor.idle_secs = secs
        monitor.next_due = time.time() + secs   # interval applies from now
        for s, b in iv_btns.items():
            b["bg"] = "#2d6a9f" if s == secs else "#333"
        header["text"] = ("● Cache Buddy [%ds]" % secs
                          if secs != 255 else "● Cache Buddy")
        log("interval -> %ds" % secs)

    for label, secs in (("15s", 15), ("4.5min", 270)):
        b = tk.Button(iv_row, text=label, relief="flat",
                      bg="#2d6a9f" if monitor.idle_secs == secs else "#333",
                      fg="white", font=("Segoe UI", 8), bd=0,
                      activebackground="#444",
                      command=lambda s=secs: set_iv(s))
        b.pack(side="left", padx=2)
        iv_btns[secs] = b

    status = tk.Label(body, text="", fg="#3a3", bg="#1e1e1e", anchor="w",
                      font=("Consolas", 9))
    status.pack(fill="x", pady=(4, 0))
    status._fill, status._kw = "x", {"pady": (4, 0)}

    tok_lbl = tk.Label(body, text="", fg="#7ec8ff", bg="#1e1e1e",
                       anchor="w", font=("Consolas", 8))
    tok_lbl.pack(fill="x")
    tok_lbl._fill, tok_lbl._kw = "x", {}

    log_lbl = tk.Label(root, text="", fg="#aaa", bg="#1e1e1e", anchor="w",
                       font=("Consolas", 8))
    log_lbl.pack(fill="x", padx=8, pady=(2, 0))
    log_lbl._fill, log_lbl._kw = "x", {"padx": 8, "pady": (2, 0)}

    btns = tk.Frame(root, bg="#1e1e1e")
    btns.pack(fill="x", padx=8, pady=6)
    btns._fill, btns._kw = "x", {"padx": 8, "pady": 6}
    parts["all"] = [body, log_lbl, btns]

    def start_guard():
        sel = picker.curselection()
        if sel:
            sid, title, fm = sessions[sel[0]]
            monitor.set_target(sid, title, fm)

    tk.Button(btns, text="开始守护", command=start_guard,
              bg="#2d6a9f", fg="white", relief="flat",
              font=("Segoe UI", 8)).pack(side="left")
    tk.Button(btns, text="停止", command=monitor.clear_target,
              bg="#444", fg="white", relief="flat",
              font=("Segoe UI", 8)).pack(side="left", padx=4)
    tk.Button(btns, text="立即续期", command=monitor.request_inject,
              bg="#2d6a9f", fg="white", relief="flat",
              font=("Segoe UI", 8)).pack(side="left")
    tk.Button(btns, text="退出", command=root.destroy,
              bg="#444", fg="white", relief="flat",
              font=("Segoe UI", 8)).pack(side="right")

    def render(rows, last_log):
        if collapsed["v"]:
            # nub dot color = guard state at a glance
            if monitor.target:
                header["fg"] = "#3a3" if monitor.fails == 0 else "#c55"
            else:
                header["fg"] = "#888"
            return
        if rows:
            r = rows[0]
            status["text"] = "%s  %s  %s" % (r["title"][:20], r["state"], r["left"])
            status["fg"] = r["color"]
        else:
            status["text"] = ""
        tok_lbl["text"] = monitor.tok_text
        log_lbl["text"] = last_log[-90:] if last_log else "idle"

    def refresh():
        rows = getattr(monitor, "pending_rows", None)
        render(rows or [], monitor.last_log)
        root.after(1000, refresh)

    def on_state(rows, last_log):
        monitor.pending_rows = rows

    monitor.on_state = on_state
    root.after(500, refresh)
    root.after(600, hover_watch)
    root.update_idletasks()
    PANEL_H[0] = root.winfo_reqheight() + 10    # content decides height —
    apply_geo()                                 # keeps the button row visible
    return root


# ------------------------------------------------------------ webview ui

UI_STATE_FILE = os.path.join(os.path.dirname(LOG_FILE), "ui.json")
DEMO = bool(os.environ.get("BUDDY_UI_DEMO"))
WIN_TITLE = "Cache Buddy (demo)" if DEMO else "Cache Buddy"

try:
    import ctypes
    import ctypes.wintypes as _wt

    class _MONITORINFO(ctypes.Structure):
        _fields_ = [("cbSize", _wt.DWORD), ("rcMonitor", _wt.RECT),
                    ("rcWork", _wt.RECT), ("dwFlags", _wt.DWORD)]
        def __init__(self):
            self.cbSize = ctypes.sizeof(self)
except Exception:                                # non-windows safety net
    _MONITORINFO = None


def _ui_dir():
    base = getattr(sys, "_MEIPASS",
                   os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, "ui")


class _WinCtl:
    """Frameless webview window management, all in PHYSICAL px through
    Win32 on the HWND: toolwindow style, Win11 rounded corners, edge
    docking, drag-snap, three-state dock (dock sliver / peek card /
    full panel). pywebview logical coordinates are never mixed in.
    Sizes are logical px * window DPI scale."""

    PANEL_W, PANEL_H = 340, 540          # logical px
    DOCK_W, DOCK_H = 10, 56
    # peek: a capsule — ECG (40) + countdown line (21) + padding (30, the
    # round ends need it) = 91, then one 38px ringed mark per session
    PEEK_W, PEEK_BASE, PEEK_STEP = 64, 91, 44
    EDGE_GAP = 5                         # dock/peek float this far off the edge
    LAYER_ALPHA = 240                    # whole-window alpha (the glass tint)
    # launch splash, centred on the screen the window opened on: the
    # pinwheel spins, then the window flies into the dock. First launch:
    # taller, with the "double-click to open" hint, waits for its button
    # and lands as the peek so the user sees where it went
    SPLASH_W, SPLASH_H, SPLASH_FIRST_H = 280, 210, 312
    FLY_MS = 460
    SPLASH_WAIT = (4.0, 120.0)           # js never answered: fly anyway
                                         # (returning, first run)

    def __init__(self, win, splash=False, first_run=False):
        self.win = win
        self.hwnd = None
        self.scale = 1.0
        self.view = "splash" if splash else "dock"   # splash|dock|peek|panel
        self.first_run = first_run
        self._revealed = not splash      # only the splash starts at alpha 0
        self._flying = False             # splash -> dock animation running
        self._splash_go = threading.Event()   # the page said "fly now"
        self._grace = 0                  # click-away ignored until this epoch
        self.peek_n = 0                  # mascot slots -> peek height
        self._leave_since = None
        self._was_down = True            # mouse state last hover tick
        self._peek_hold = 0              # peek stays put until this epoch
        self._shape = None               # current window clip (w, h) or None
        self.fit_h = 0                   # panel height its page asked for
                                         # (logical px; 0 = PANEL_H)
        self._fit_gen = 0                # newer fit_panel() calls win
        self._halo = None                # edge_halo.EdgeHalo: the capsule's
                                         # anti-aliased rim, or None
        self._js_evt = threading.Event()  # setView calls are queued; a
        self._js_state = None             # dedicated worker owns evaluate_js
                                          # because it can block for seconds
        self.dragging = False
        self._post_drag = None         # a set_view deferred until drop
        self.stop = False
        self.side = "right"
        self.y = 200                     # physical px, restored below
        self._load_state()

    def _load_state(self):
        try:
            import json
            d = json.load(open(UI_STATE_FILE, encoding="utf-8"))
            self.side = "left" if d.get("side") == "left" else "right"
            self.y = int(d.get("y") or 200)
        except Exception:
            pass

    def _save_state(self):
        try:
            import json
            os.makedirs(os.path.dirname(UI_STATE_FILE), exist_ok=True)
            with open(UI_STATE_FILE, "w", encoding="utf-8") as f:
                json.dump({"side": self.side, "y": self.y}, f)
        except OSError:
            pass

    # -------------------------------------------------- win32 plumbing
    def attach(self):
        u = ctypes.windll.user32
        hwnd = u.FindWindowW(None, WIN_TITLE)
        if not hwnd:
            log("ui: hwnd not found")
            return False
        self.hwnd = hwnd
        ex = u.GetWindowLongW(hwnd, -20)                     # GWL_EXSTYLE
        u.SetWindowLongW(hwnd, -20, (ex | 0x80) & ~0x40000)  # +TOOLWINDOW
        try:                                               # -APPWINDOW
            ctypes.windll.dwmapi.DwmSetWindowAttribute(
                hwnd, 33, ctypes.byref(ctypes.c_int(2)), 4)  # round corners
        except Exception:
            pass
        try:   # see-through glass: HWND-hosted WebView2 never punches
               # transparent page regions through to the desktop, and a
               # WinForms TransparencyKey doesn't reach the webview child
               # — so the tint comes from a layered window alpha instead:
               # the whole window sits ~6% translucent over whatever is
               # behind it.  DWM frame extension keeps the rounded
               # corners' backdrop blurred.
            class _MARGINS(ctypes.Structure):
                _fields_ = [("cxLeftWidth", ctypes.c_int),
                            ("cxRightWidth", ctypes.c_int),
                            ("cyTopHeight", ctypes.c_int),
                            ("cyBottomHeight", ctypes.c_int)]
            m = _MARGINS(-1, -1, -1, -1)
            ctypes.windll.dwmapi.DwmExtendFrameIntoClientArea(
                hwnd, ctypes.byref(m))
            ctypes.windll.dwmapi.DwmSetWindowAttribute(
                hwnd, 38, ctypes.byref(ctypes.c_int(3)), 4)  # acrylic
            ex2 = u.GetWindowLongW(hwnd, -20)
            u.SetWindowLongW(hwnd, -20, ex2 | 0x80000)       # WS_EX_LAYERED
            # the splash stays invisible until its page has painted
            # (reveal()) — WebView2's cold start is a blank grey box
            u.SetLayeredWindowAttributes(
                hwnd, 0, 0 if self.view == "splash" else self.LAYER_ALPHA,
                0x2)                                         # LWA_ALPHA ~94%
            log("ui: layered alpha 240")
        except Exception as e:
            log("ui: layered alpha failed: %r" % e)
        dpi = u.GetDpiForWindow(hwnd)
        if dpi:
            self.scale = dpi / 96.0
        threading.Thread(target=self._js_worker, daemon=True).start()
        try:
            import edge_halo
            self._halo = edge_halo.EdgeHalo(alpha=self.LAYER_ALPHA)
        except Exception as e:           # no halo: the capsule edge just
            log("ui: edge halo off: %r" % e)   # stays stair-stepped
            self._halo = None
        self.apply_geo()
        self._js_view()
        if self.view == "splash":
            threading.Thread(target=self._splash_wait, daemon=True).start()
        return True

    def _rect(self):
        r = _wt.RECT()
        ctypes.windll.user32.GetWindowRect(self.hwnd, ctypes.byref(r))
        return r

    def _workarea(self):
        hmon = ctypes.windll.user32.MonitorFromWindow(self.hwnd, 2)
        mi = _MONITORINFO()
        ctypes.windll.user32.GetMonitorInfoW(hmon, ctypes.byref(mi))
        return mi.rcWork

    def _cursor(self):
        p = _wt.POINT()
        ctypes.windll.user32.GetCursorPos(ctypes.byref(p))
        return p

    def _ensure_hwnd(self):
        """WebView2 can recreate the HWND after attach() (e.g. while the
        controller finishes init); a dead hwnd makes GetWindowRect return
        zeros instead of raising, silently breaking every geometry check."""
        u = ctypes.windll.user32
        if self.hwnd and u.IsWindow(self.hwnd):
            return True
        h = u.FindWindowW(None, WIN_TITLE)
        if not h:
            return False
        self.hwnd = h
        dpi = u.GetDpiForWindow(h)
        if dpi:
            self.scale = dpi / 96.0
        return True

    def _geo(self, view):
        """(x, y, w, h) in physical px for `view` on the current screen."""
        s = self.scale
        wa = self._workarea()
        if view == "splash":
            w = int(round(self.SPLASH_W * s))
            h = int(round((self.SPLASH_FIRST_H if self.first_run
                           else self.SPLASH_H) * s))
            return ((wa.left + wa.right - w) // 2,
                    (wa.top + wa.bottom - h) // 2, w, h)
        if view == "dock":
            w = int(round(self.DOCK_W * s))
            h = int(round(self.DOCK_H * s))
            y = max(wa.top, min(self.y, wa.bottom - h))
        elif view == "peek":
            w = int(round(self.PEEK_W * s))
            h = int(round((self.PEEK_BASE + self.PEEK_STEP
                           * self.peek_n) * s))
            anchor = self.y + int(round(self.DOCK_H * s / 2))
            y = max(wa.top, min(anchor - h // 2, wa.bottom - h))
        else:
            w = int(round(self.PANEL_W * s))
            h = int(round(self.panel_h() * s))
            y = max(wa.top, min(self.y, wa.bottom - h))
        # the panel keeps a hairline off the edge; dock/peek are capsules
        # that float a few px off it
        gap = int(round((self.EDGE_GAP if view != "panel" else 2) * s))
        x = wa.right - w - gap if self.side == "right" else wa.left + gap
        return x, y, w, h

    def apply_geo(self):
        if self.dragging or self._flying:   # a mid-drag/flight reflow
            return                  # fights it — both re-apply at the end
        if not self._ensure_hwnd():
            return
        x, y, w, h = self._geo(self.view)
        ctypes.windll.user32.SetWindowPos(self.hwnd, 0, x, y, w, h,
                                          0x0004 | 0x0010)  # NOZORDER|NOACT
        self._apply_shape(w, h)

    def _apply_shape(self, w, h):
        """Dock and peek are capsules: clip the window to one (both ends
        fully round), which also drops the DWM frame and shadow. The panel
        and the splash get their normal rectangle — and DWM's rounding,
        glass and shadow — back."""
        u, g = ctypes.windll.user32, ctypes.windll.gdi32
        shape = (w, h) if self.view in ("dock", "peek") else None
        if shape != self._shape:
            self._shape = shape
            self._dwm_edge(shape is not None)
            try:
                if shape is None:
                    u.SetWindowRgn(self.hwnd, None, True)
                elif self._halo:
                    # a few px inside the capsule: the halo window behind
                    # draws the anti-aliased rim the 1-bit region can't
                    import edge_halo
                    edge_halo.set_capsule(self.hwnd, w, h)
                else:
                    d = min(w, h)                # corner = full half-circle
                    rgn = g.CreateRoundRectRgn(0, 0, w + 1, h + 1, d, d)
                    if not u.SetWindowRgn(self.hwnd, rgn, True):
                        g.DeleteObject(rgn)      # system owns it on success
            except Exception as e:
                log("ui: window shape failed: %r" % e)
        self._sync_halo()

    def _sync_halo(self):
        """The halo follows the window: shown behind dock/peek, hidden for
        the panel (which has DWM's own smooth corners)."""
        if not self._halo:
            return
        try:
            if self.view not in ("dock", "peek") or not self._shape:
                self._halo.hide()
            else:
                r = self._rect()
                self._halo.show(self.hwnd, r.left, r.top,
                                r.right - r.left, r.bottom - r.top)
        except Exception as e:
            log("ui: edge halo sync failed: %r" % e)

    def _dwm_edge(self, capsule):
        """DWM paints its acrylic backdrop, rounded frame and shadow on
        its own, ignoring the window region — that's the grey rectangle
        left around a clipped capsule. Turn all three off for dock/peek,
        and back on (the panel's glass look) for the panel."""
        try:
            d = ctypes.windll.dwmapi

            def attr(a, v):
                d.DwmSetWindowAttribute(self.hwnd, a,
                                        ctypes.byref(ctypes.c_int(v)), 4)

            class _M(ctypes.Structure):
                _fields_ = [("l", ctypes.c_int), ("r", ctypes.c_int),
                            ("t", ctypes.c_int), ("b", ctypes.c_int)]
            if capsule:
                attr(2, 1)                    # NCRENDERING_POLICY: disabled
                attr(33, 1)                   # corners: don't round
                attr(38, 1)                   # backdrop: none
                m = _M(0, 0, 0, 0)
            else:
                attr(2, 0)                    # policy: use window style
                attr(33, 2)                   # corners: round
                attr(38, 3)                   # backdrop: acrylic
                m = _M(-1, -1, -1, -1)
            d.DwmExtendFrameIntoClientArea(self.hwnd, ctypes.byref(m))
        except Exception as e:
            log("ui: dwm edge failed: %r" % e)

    def panel_h(self, fit=None):
        """The panel's logical height: what its page asked for (folded
        cards leave nothing to stretch), else the full PANEL_H."""
        if fit is None and self._fit_anim is not None:
            return self._fit_anim        # mid-ease (fit_panel)
        f = self.fit_h if fit is None else fit
        return max(160, min(self.PANEL_H, f)) if f else self.PANEL_H

    def fit_panel(self, h, ms=0):
        """The page's content height changed (0 = give me the full
        height). The bottom edge eases there over `ms` (default ~150ms;
        a card's fold animation passes its own length so the two move
        together); the top stays put. A newer call or a view change cuts
        it short."""
        try:
            h = max(0, int(h or 0))
            ms = max(0, min(1000, int(ms or 0))) or 150
        except (TypeError, ValueError):
            return
        if h == self.fit_h:
            return
        frm = self.panel_h()             # from where it is now, even mid-ease
        self._fit_gen += 1
        gen = self._fit_gen
        self.fit_h = h
        if self.view != "panel":
            return                       # applied when the panel opens
        to = self.panel_h()

        def run():
            t0, dur = time.time(), ms / 1000.0
            try:
                while True:
                    if gen != self._fit_gen:
                        return           # a newer ease owns _fit_anim
                    if self.view != "panel" or self.stop:
                        break
                    # by the clock, not frame counts (sleep is coarse on
                    # Windows): the page's animation runs on the same one
                    t = min(1.0, (time.time() - t0) / dur)
                    e = 1 - (1 - t) ** 3     # ease-out, = the page's
                    self._fit_anim = frm + (to - frm) * e
                    self.apply_geo()
                    if t >= 1:
                        break
                    time.sleep(0.012)
            except Exception as ex:
                log("ui: panel fit failed: %r" % ex)
            finally:
                if gen == self._fit_gen:
                    self._fit_anim = None
        threading.Thread(target=run, daemon=True).start()

    _fit_anim = None                     # mid-ease height, read by panel_h

    def set_view(self, v):
        if v == self.view or v not in ("dock", "peek", "panel"):
            return
        # leaving the splash any other way (tray, a second launch, a rescue
        # notice) cuts it short; the flight sees the view change and stops
        if self.dragging:            # view switches resize the window —
            self._post_drag = v      # defer until the button is released
            return
        if self.view == "splash" and self.hwnd:
            self.reveal()            # cut short before the page painted
        log("view: %s -> %s" % (self.view, v))
        self.view = v
        self._leave_since = None
        self.apply_geo()
        self._js_view()

    def _js_view(self):                  # never blocks the caller: the
        self._js_state = (self.view, self.side)   # latest state wins
        self._js_evt.set()

    def _js_worker(self):
        while not self.stop:
            self._js_evt.wait(0.5)
            self._js_evt.clear()
            st = self._js_state
            if st:
                try:
                    self.win.evaluate_js(
                        st[1] if st[0] == "__raw__"
                        else "setView('%s','%s')" % st)
                except Exception:
                    pass

    def eval_js(self, code):             # demo/test hook: queued on the
        self._js_state = ("__raw__", code)   # js worker like setView —
        self._js_evt.set()                   # never blocks the caller

    def collapse(self):                  # '—' button via api.collapse()
        self.set_view("dock")

    def open_panel(self):                # peek double-click
        self.set_view("panel")

    def open_from_tray(self):
        """Tray click / second launch. The click that got here landed
        outside the panel; the grace keeps it (and a double-click's second
        press) from reading as a click-away."""
        self._grace = time.time() + 0.8
        self.set_view("panel")

    def peek_hold(self, secs):
        """Keep the peek open for `secs` even with the cursor away — a
        notice (a rescue) is showing and should be seen."""
        self._peek_hold = time.time() + float(secs)
        self._leave_since = None

    def frame_accent(self, hexcol=None):
        """Tint the window's 1px DWM frame ("#3FA46A"), or give it back to
        Windows (None). Win11 only; anything older just ignores it."""
        if not self._ensure_hwnd():
            return
        DWMWA_BORDER_COLOR = 34
        DWMWA_COLOR_DEFAULT = 0xFFFFFFFF
        DWMWA_COLOR_NONE = 0xFFFFFFFE            # no frame drawn at all
        val = DWMWA_COLOR_DEFAULT
        if hexcol == "none":
            val = DWMWA_COLOR_NONE
        elif hexcol:
            h = hexcol.lstrip("#")
            r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
            val = (b << 16) | (g << 8) | r      # COLORREF is 0x00BBGGRR
        try:
            ctypes.windll.dwmapi.DwmSetWindowAttribute(
                self.hwnd, DWMWA_BORDER_COLOR,
                ctypes.byref(ctypes.c_uint(val)), 4)
        except Exception:
            pass

    # -------------------------------------------------------- splash
    def splash_info(self):
        return {"splash": self.view == "splash", "first": self.first_run}

    _revealed = False

    def reveal(self):
        """The page painted: fade the window in from alpha 0 (~120ms).
        Also the fallback when the page never says so."""
        if self._revealed or not self._ensure_hwnd():
            return
        self._revealed = True
        u = ctypes.windll.user32
        for i in range(1, 9):
            u.SetLayeredWindowAttributes(self.hwnd, 0,
                                         self.LAYER_ALPHA * i // 8, 0x2)
            time.sleep(0.015)

    def finish_splash(self):
        """The page is done with the splash (its spin played out, or the
        first-run hint's button): fly to the edge."""
        self._splash_go.set()

    def _splash_wait(self):
        """Runs from attach(): fly when the page says so — or after a
        fallback wait, so a page that never boots can't pin the splash
        to the middle of the screen."""
        self._splash_go.wait(self.SPLASH_WAIT[1 if self.first_run else 0])
        self.reveal()                    # never leave the window at alpha 0
        if self.view == "splash" and not self.stop:
            self._fly()

    def _fly(self):
        """Splash -> edge: the window eases from the screen centre into
        the dock (first run: the peek, held a few seconds so the user sees
        where Buddy lives). The rectangle keeps DWM's rounded glass while
        it flies; the capsule shape and its halo take over on landing."""
        land = "peek" if self.first_run else "dock"
        self._flying = True
        try:
            u = ctypes.windll.user32
            r0 = self._rect()
            x0, y0 = r0.left, r0.top
            w0, h0 = r0.right - r0.left, r0.bottom - r0.top
            x1, y1, w1, h1 = self._geo(land)
            t0, dur = time.time(), self.FLY_MS / 1000.0
            while not self.stop and self.view == "splash":
                t = min(1.0, (time.time() - t0) / dur)
                e = 1 - (1 - t) ** 3             # ease-out cubic
                u.SetWindowPos(self.hwnd, 0,
                               int(x0 + (x1 - x0) * e),
                               int(y0 + (y1 - y0) * e),
                               max(1, int(w0 + (w1 - w0) * e)),
                               max(1, int(h0 + (h1 - h0) * e)),
                               0x0004 | 0x0010)          # NOZORDER|NOACTIVATE
                if t >= 1:
                    break
                time.sleep(1.0 / 90)
        except Exception as e:
            log("ui: fly failed: %r" % e)
        finally:
            self._flying = False
        if self.stop:
            return
        if self.view == "splash":                # landed (not cut short)
            log("view: splash -> %s" % land)
            self.view = land
            self._leave_since = None
            if land == "peek":
                self.peek_hold(5)
        self.apply_geo()                         # the real shape + halo
        self._js_view()

    # ------------------------------------------------------- dragging
    def drag_start(self):                # header/mascot mousedown
        if self.dragging or not self.hwnd:
            return
        self.dragging = True
        threading.Thread(target=self._drag_loop, daemon=True).start()

    def _drag_loop(self):
        u = ctypes.windll.user32
        try:
            r0 = self._rect()
            # grab-point measured right here, at mousedown time — the JS
            # caller no longer waits for a >4px mousemove first
            p0 = self._cursor()
            dx, dy = p0.x - r0.left, p0.y - r0.top
            thr = max(4, int(4 * self.scale))      # dead-zone ~4 logical px
            moved = False
            wa = self._workarea()
            h = r0.bottom - r0.top
            while u.GetAsyncKeyState(0x01) & 0x8000:        # VK_LBUTTON
                p = self._cursor()
                if not moved:
                    if (abs(p.x - p0.x) <= thr
                            and abs(p.y - p0.y) <= thr):
                        time.sleep(0.03)     # inside the dead-zone — a
                        continue             # click so far, don't move
                    moved = True
                nx = p.x - dx
                ny = max(wa.top, min(p.y - dy, wa.bottom - h))  # clamped
                u.SetWindowPos(              # NOSIZE|NOZORDER|NOACTIVATE
                    self.hwnd, 0, nx, ny, 0, 0, 0x0001 | 0x0004 | 0x0010)
                if self._halo and self._shape:
                    self._halo.move(self.hwnd, nx, ny)
                time.sleep(1.0 / 60)
            self.dragging = False        # released — unblock apply_geo and
            if moved:                    # deferred set_view before using them
                r = self._rect()
                cx = (r.left + r.right) / 2.0
                self.side = ("left" if cx < (wa.left + wa.right) / 2.0
                             else "right")
                # keep the dropped y: dock/peek anchor to where the window
                # top was let go — work-area top is a legal drop (flush,
                # not past it)
                dock_h = self.DOCK_H * self.scale
                self.y = int(max(wa.top,
                                 min(r.top, wa.bottom - dock_h)))
                self.apply_geo()
                self._save_state()
                self._js_view()              # side may have flipped
            if self._post_drag:          # a dblclick/open landed mid-press —
                v, self._post_drag = self._post_drag, None  # apply it even
                self.set_view(v)         # when the press itself was a click
        finally:
            self.dragging = False

    # --------------------------------------------------- hover dock/peek
    def _hover_loop(self):
        while not self.stop:
            # panel polls faster: a quick click-away is ~80ms of press
            time.sleep(0.04 if self.view == "panel" else 0.15)
            if self.dragging or not self._ensure_hwnd():
                self._was_down = True        # drag release isn't a click
                continue
            if self.view == "splash" or self._flying:
                self._was_down = True        # its button press isn't a click-away
                continue
            s = self.scale
            p = self._cursor()
            r = self._rect()
            if self.view == "dock":
                # sliver rect + margin -> peek; the margin covers the gap
                # to the screen edge, so a cursor slammed there still opens
                m = (self.EDGE_GAP + 6) * s
                if (r.left - m <= p.x <= r.right + m
                        and r.top - m <= p.y <= r.bottom + m):
                    self.set_view("peek")
            elif self.view == "peek":
                if time.time() < self._peek_hold:
                    continue         # a notice is showing — don't collapse
                m = (self.EDGE_GAP + 8) * s
                inside = (r.left - m <= p.x <= r.right + m
                          and r.top - m <= p.y <= r.bottom + m)
                if inside:
                    self._leave_since = None
                else:
                    if self._leave_since is None:
                        self._leave_since = time.time()
                    elif time.time() - self._leave_since >= 0.4:
                        self.set_view("dock")
            else:
                # panel: a mouse press anywhere outside it -> back to dock
                down = bool(ctypes.windll.user32.GetAsyncKeyState(0x01)
                            & 0x8000 or ctypes.windll.user32
                            .GetAsyncKeyState(0x02) & 0x8000)
                if (_outside_press(self._was_down, down, p, r)
                        and time.time() >= self._grace):
                    log("ui: click outside panel -> dock")
                    self.set_view("dock")
                self._was_down = down
            if self.view != "panel":
                self._was_down = True    # a press carried into the panel
                                         # (the peek dblclick) isn't "outside"


def _outside_press(was_down, down, p, r):
    """True on the frame a mouse button goes down with the cursor outside
    rect r — the panel's click-away. Holding a button that was already
    down (a drag, the dblclick that opened the panel) never counts."""
    if was_down or not down:
        return False
    return not (r.left <= p.x < r.right and r.top <= p.y < r.bottom)


_demo_sid = None                       # first real session, resolved once


def _demo_state():
    """BUDDY_UI_DEMO=1: fake ui_state cycling modes/track sets so the UI
    can be screenshotted — never touches the real Monitor."""
    global _demo_sid
    if _demo_sid is None:
        _demo_sid = ""
        try:                                # guard an actual row so the
            for e in list_spaces():         # tree shows the inline heart
                items = (e.get("items") if e.get("type") == "group"
                         else [e])
                if items:
                    _demo_sid = items[0]["sid"]
                    break
        except Exception:
            pass
    seq = [("guarding", "claude"), ("guarding", "fusion"),
           ("sent", "devin"), ("injecting", "kimi"),
           ("retry", "gptglm"), ("failed", "claude"),
           ("none", "empty"), ("loading", "gemini"),
           ("guarding", "empty")]          # target set, no metrics yet
    t = int(time.time())
    mode, fam = seq[(t // 6) % len(seq)]
    # status is backend Chinese detail — the UI only reads it to pick a
    # short retry reason; every other mode derives its text from `mode`
    status = {"retry": "失败 cdp: no session tab"}.get(mode, "")

    def tr(role, model, read, hit, age, left, cost, noco):
        return {"role": role, "model": model,
                # the demo's two-track sets are fusion sessions
                "family": _family(model, fused=fam in ("fusion", "gptglm")),
                "read": read, "hit": hit, "age_s": age,
                "cache_left": left, "cost": cost, "nocache_cost": noco,
                "this_cycle": True}
    live = 200 - t % 200                 # ticking countdowns
    TRACKS = {
        "claude": [tr("lead", "claude-opus-5-5-high", 812400, 96,
                      12 + t % 40, live, 0.0211, 0.48)],
        "fusion": [tr("lead", "claude-opus-5-5-high", 812400, 96,
                      60 + t % 120, 0, 0.0211, 0.48),          # expired
                   tr("sidekick", "swe-2-high", 35600, 89,
                      8 + t % 30, live, 0.0027, 0.031)],
        "devin": [tr("lead", "swe-2-high", 35600, 89,
                     8 + t % 30, live, 0.0027, 0.031)],
        "kimi": [tr("lead", "kimi-k3", 144000, 91,
                    15 + t % 40, live, 0.0044, 0.05)],
        "gptglm": [tr("lead", "gpt-6-sol-high", 604200, 93,
                      20 + t % 60, live, 0.031, 0.62),
                   tr("sidekick", "glm-5.2-medium", 48200, 84,
                      5 + t % 20, max(1, live - 90), 0.0006, 0.008)],
        "gemini": [tr("lead", "gemini-3-pro", 92100, 90,
                      25 + t % 40, live, 0.018, 0.21)],
        "empty": [],
    }
    return {
        "mode": mode,
        "target": ({"sid": _demo_sid or "demo", "title": "Anything"}
                   if mode != "none" else None),
        "status": status,
        "left": "",
        "due_in": (222 - t % 222) if mode == "guarding" else None,
        "idle_secs": 15,
        "cdp": True,
        "last_log": "demo mode: %s/%s" % (mode, fam),
        "cache": (None if fam == "empty" else
                  {"kind": "上轮" if mode == "none" else "本轮",
                   "waiting_first": False, "saved": 0.42,
                   "tracks": TRACKS[fam]}),
    }


class _BuddyApi:
    """pywebview js_api — calls land on worker threads; every method only
    pokes monitor attributes, same as the Tk buttons did."""

    def __init__(self, mon, env):
        self.mon = mon
        self.env = env                   # {"win": w, "ctl": _WinCtl}

    def state(self):
        if DEMO:
            return _demo_state()
        return self.mon.ui_state()

    def spaces(self):
        out = list_spaces()
        cli = getattr(self.mon, "cli", None)
        if cli:
            for e in out:
                for s in (e["items"] if e["type"] == "group" else [e]):
                    if self.mon._locked(s["sid"]):
                        s["source"], s["locked"] = "cli", True
        return out

    def settings(self):
        return load_settings()

    def set_setting(self, key, value):
        """⚙: lang = "zh"/"en"; maint_min = 15/30/45 (缓存维护时间),
        applied to the running guard(s) right away."""
        if not ((key == "lang" and value in ("zh", "en")) or
                (key == "maint_min" and value in MAINT_CHOICES)):
            return None
        d = load_settings()
        d[key] = value
        save_settings(d)
        if key == "maint_min":
            for m in self._monitors():
                m.maint = value * 60
        log("settings: %s -> %s" % (key, value))
        return d

    def _monitors(self):
        return [self.mon]

    def rescan(self):
        """The ⟳ button: look for CLIs opened before Buddy again, then
        the list. The 30s auto-refresh only calls spaces()."""
        if hasattr(self.mon, "rescan_cli"):
            self.mon.rescan_cli()
        return self.spaces()

    def start(self, sid):
        for e in list_spaces():
            items = e["items"] if e["type"] == "group" else [e]
            for s in items:
                if s["sid"] == sid:
                    self.mon.set_target(sid, s["title"], s["first_msg"])
                    log("ui: start -> %s" % s["title"])
                    return True
        return False

    def stop(self):
        self.mon.clear_target()

    def stop_one(self, sid):
        """Stop guarding one session — Devin guards one at a time, so
        only when it's the guarded one."""
        t = self.mon.target
        if t and t[0] == sid:
            self.mon.clear_target()

    def inject(self):
        self.mon.request_inject()

    def ledger_month(self, ym):
        """Heatmap paging: another calendar month ("2026-09")."""
        if not (isinstance(ym, str) and re.fullmatch(r"\d{4}-\d{2}", ym)):
            return None
        return self.mon._ledger.month(ym)

    def rescue_pop(self, at):
        """The panel is showing the rescue notice: pop the peek open and
        remember it, so it is announced exactly once."""
        self.mon.mark_rescues(float(at))
        c = self.env.get("ctl")
        if c:
            c.peek_hold(7)
            c.set_view("peek")

    def set_interval(self, secs):
        secs = int(secs)
        self.mon.idle_secs = secs
        self.mon.next_due = time.time() + secs
        log("interval -> %ds" % secs)

    def collapse(self):
        c = self.env.get("ctl")
        if c:
            c.collapse()

    def open_panel(self):
        c = self.env.get("ctl")
        if c:
            c.open_panel()

    def sync_view(self):                 # page boot: re-push view+side so a
        c = self.env.get("ctl")          # setView dropped before JS ready is
        if c:                            # re-applied once the page is up
            c._js_view()

    def panel_fit(self, h, ms=0):        # the page's content height
        c = self.env.get("ctl")          # (0 = full), see _WinCtl.fit_panel
        if c:
            c.fit_panel(h, ms)

    def peek_mascots(self, n):
        c = self.env.get("ctl")
        if c:
            n = int(n or 0)
            if n != c.peek_n:
                c.peek_n = n
                if c.view == "peek":   # card height tracks mascot count
                    c.apply_geo()

    def frame_accent(self, hexcol=None):
        """The panel's view of its window frame colour (see _WinCtl)."""
        c = self.env.get("ctl")
        if c and (hexcol is None or hexcol == "none"
                  or (isinstance(hexcol, str)
                      and re.fullmatch(r"#[0-9A-Fa-f]{6}", hexcol))):
            c.frame_accent(hexcol)

    def drag_start(self):
        c = self.env.get("ctl")
        if c:
            c.drag_start()

    def splash_info(self):
        """Page boot: show the launch splash? first run (with the hint)?"""
        c = self.env.get("ctl")
        info = c.splash_info() if c else {"splash": False, "first": False}
        info["lang"] = load_settings()["lang"]
        return info

    def splash_shown(self):
        """The splash's first frame is on screen: show the window."""
        c = self.env.get("ctl")
        if c:
            c.reveal()

    def splash_done(self):
        """The splash played out (first run: its button) -> fly to the
        edge. A dismissed hint isn't shown again."""
        c = self.env.get("ctl")
        if not c:
            return
        if c.first_run:
            d = load_settings()
            if not d.get("onboarded"):
                d["onboarded"] = True
                save_settings(d)
                log("onboarded: first-run hint dismissed")
        c.finish_splash()

    def quit(self):
        c = self.env.get("ctl")
        if c:
            c.stop = True
            if c._halo:
                c._halo.close()
        w = self.env.get("win")
        if w:
            w.destroy()


def run_webview(monitor, idle, api_cls=None):
    """Frosted-glass webview UI on EdgeChromium. Any failure raises and
    the caller falls back to the Tk UI. api_cls: cc_buddy passes its own
    _BuddyApi subclass to drive the same panel."""
    import webview
    try:
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
        # all geometry below is physical px via Win32 — same reason as Tk
    except Exception:
        pass
    env = {}
    api = (api_cls or _BuddyApi)(monitor, env)
    # launch splash: every normal start (not the screenshot demo, not
    # BUDDY_NO_SPLASH); first run = the hint was never dismissed
    splash = not (DEMO or os.environ.get("BUDDY_NO_SPLASH")
                  or "--no-splash" in sys.argv)
    first = splash and ("--first-run" in sys.argv
                        or not load_settings().get("onboarded"))
    sw = _WinCtl.SPLASH_W if splash else 340
    sh = ((_WinCtl.SPLASH_FIRST_H if first else _WinCtl.SPLASH_H)
          if splash else 540)
    # created at the splash's size (pywebview centres it), so attach()
    # only nudges it — no 340x540 flash first
    w = webview.create_window(
        WIN_TITLE, url=os.path.join(_ui_dir(), "index.html"),
        js_api=api, width=sw, height=sh, min_size=(10, 40),
        frameless=True, easy_drag=False, on_top=True, shadow=True,
        resizable=False, transparent=True)
    ctl = _WinCtl(w, splash=splash, first_run=first)
    env.update(win=w, ctl=ctl)
    monitor._winctl = ctl                # lets the `view` ctl command in

    tray = None
    if os.name == "nt":
        try:
            import tray as _tray
            tray = _tray.Tray(on_open=ctl.open_from_tray, on_quit=api.quit,
                              lang=lambda: load_settings()["lang"], log=log)
            tray.start()
        except Exception as e:
            log("tray: unavailable: %r" % e)
            tray = None

    def shown():
        if ctl.attach():
            threading.Thread(target=ctl._hover_loop, daemon=True).start()
        else:
            log("ui: win32 attach failed; window stays undocked")

    w.events.shown += shown
    monitor.on_state = lambda rows, ll: setattr(monitor, "pending_rows",
                                               rows)
    try:
        webview.start()
    finally:
        ctl.stop = True
        if tray:
            tray.close()


def main():
    global INJECT_BEFORE
    idle = TTL - INJECT_BEFORE                    # default: inject at 255s idle
    if len(sys.argv) > 2 and sys.argv[1] == "--inject":
        ok, detail = inject_heartbeat(sys.argv[2])
        print("inject:", ok, detail)
        return
    if "--idle" in sys.argv:
        i = sys.argv.index("--idle")
        try:
            idle = int(sys.argv[i + 1])
        except (IndexError, ValueError):
            pass
    INJECT_BEFORE = max(0, TTL - idle)
    if not os.path.exists(DB):
        print("sessions.db not found:", DB)
        sys.exit(1)
    log("cache buddy start (inject every %ds)" % idle)
    try:                                          # left for a buddy that's
        os.remove(CTL_FILE)                       # gone — not for this one
    except OSError:
        pass
    mon = Monitor(on_state=lambda r, l: None, idle_secs=idle)
    mon.attach_cli()
    mon.start()
    try:
        tk = "--tk" in sys.argv
        if not tk:
            try:
                run_webview(mon, idle)
            except Exception as e:
                log("webview ui failed, falling back to tk: %s" % e)
                tk = True
        if tk:
            app = build_ui(mon, idle)
            app.mainloop()
    finally:
        mon.stop_flag.set()
        log("cache buddy stop")


if __name__ == "__main__":
    main()
