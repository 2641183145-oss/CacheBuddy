# -*- coding: utf-8 -*-
"""Token usage per model, per harness — read from what each harness
already writes to disk. No network, no credentials.

  Claude Code  ~/.claude/projects/**/*.jsonl (subagents too): one usage per
               assistant message. A message streams as several rows with the
               same message.id — the first carries provisional numbers
               (output 1, no cache split), so the LAST row of an id wins.
  Devin        sessions.db message_nodes: metadata.metrics per model call.
               One call is copied onto several nodes (and again on replay),
               all with the same request_id — counted once.
  Droid        <sid>.settings.json only keeps the session's running total
               (tokenUsage), no per-call rows. Each scan books the growth
               since the last one, at that hour, under the session's newest
               reply model; what was seen is kept in droid-usage.json so a
               restart doesn't count it twice. A session seen for the first
               time is booked at its last reply.

Buddy's own heartbeats: Claude Code's go through the proxy and never reach
a transcript, Devin CLI's leave no row in sessions.db — neither is counted.
Droid's are `droid exec` turns and show up in its totals like any turn.

Money is an estimate at official list prices (cc_buddy's table for Claude
Code, cache_buddy's for Devin/Droid), not what a plan bills.
"""
import json
import os
import re
import sqlite3
import threading
import time
from datetime import date, datetime, timedelta, timezone

KINDS = ("input", "output", "cache_read", "cache_write")
EVERY = 30                        # seconds between scans
KEEP_DAYS = 31


def log(msg):                     # replaced by the buddy's own log
    return msg


def _hour(ts):
    """Local 'YYYY-MM-DD HH' of an epoch."""
    return time.strftime("%Y-%m-%d %H", time.localtime(ts))


def _iso(s):
    """ISO-8601 (any fraction length, Z or offset) -> epoch, or None."""
    if not isinstance(s, str) or not s:
        return None
    s = s.strip().replace("Z", "+00:00")
    m = re.match(r"^(.*T\d\d:\d\d:\d\d)(\.\d+)?(.*)$", s)
    if m:                         # fromisoformat wants <= 6 fraction digits
        s = m.group(1) + (m.group(2) or "")[:7] + m.group(3)
    try:
        d = datetime.fromisoformat(s)
    except ValueError:
        return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return d.timestamp()


_EFF = re.compile(r"-(low|medium|high|xhigh|max|fast|thinking)$")


def canon(model):
    """One key per model across harnesses and spellings: lower case,
    gateways' opus-5.5 = opus-5-5, the effort/speed tier dropped (it is
    the same model; swe-2-max and swe-2-low are one row)."""
    m = (model or "").strip().lower()
    if not m:
        return "unknown"
    if m.startswith("claude"):
        m = m.replace(".", "-")
        m = re.sub(r"-\d{8}$", "", m)          # dated snapshot ids
    while _EFF.search(m):
        m = _EFF.sub("", m)
    return m or "unknown"


_UP = {"swe": "SWE", "gpt": "GPT", "glm": "GLM"}


def pretty(m):
    """Display name: claude-opus-5-5 -> Opus 5.5, swe-1-7-lightning ->
    SWE-1.7 Lightning, gpt-6-luna -> GPT-6 Luna, gemini-4-argon ->
    Gemini 4 Argon, kimi-k3 -> Kimi K3."""
    if m in ("", "unknown"):
        return "?"
    if m == "auto":
        return "Auto"
    c = re.match(r"^claude-(opus|sonnet|haiku)-(\d+)(?:-(\d+))?$", m)
    if c:
        return "%s %s" % (c.group(1).title(),
                          c.group(2) + ("." + c.group(3) if c.group(3) else ""))
    parts = m.split("-")
    head = parts[0]
    if head in _UP:               # SWE-1.7 / GPT-6.1 / GLM-5
        i, nums = 1, []
        while i < len(parts) and re.match(r"^\d+(\.\d+)?$", parts[i]):
            nums.append(parts[i])
            i += 1
        name = _UP[head] + ("-" + ".".join(nums) if nums else "")
        rest = parts[i:]
    else:
        name, rest = head.title(), parts[1:]
    words = [w.upper() if re.match(r"^[a-z]\d", w) else w.title()
             for w in rest]
    return " ".join([name] + words)


def _vec(i=0, o=0, cr=0, cw=0):
    return [int(i or 0), int(o or 0), int(cr or 0), int(cw or 0)]


class Tracker(threading.Thread):
    """Scans every EVERY seconds in the background; state(harness) is the
    last summary, ready to send to the panel as is."""

    def __init__(self, which, cc_dir=None, devin_db=None, droid_dir=None,
                 droid_file=None, cc_cost=None, cost=None, family=None,
                 every=EVERY):
        super().__init__(daemon=True, name="usage")
        self.which = tuple(which)
        self.cc_dir, self.devin_db = cc_dir, devin_db
        self.droid_dir, self.droid_file = droid_dir, droid_file
        self.cc_cost = cc_cost    # (model, usage dict) -> (usd, _) or None
        self.cost = cost          # (metrics dict) -> (usd, _) or None
        self.family = family or (lambda m: "generic")
        self.every = every
        self.stop_flag = threading.Event()
        self.kick = threading.Event()
        self._cc = {}             # message id -> (hour, model, vec, usd)
        self._cc_off = {}         # path -> bytes read
        self._dv = {}             # request id -> (hour, model, vec, usd)
        self._dv_row = 0          # message_nodes.row_id read up to
        self._dr = self._droid_load()
        self._sum = {}            # harness -> summary

    # ---------------------------------------------------- Claude Code
    def scan_cc(self):
        if not self.cc_dir or not os.path.isdir(self.cc_dir):
            return
        cut = time.time() - KEEP_DAYS * 86400
        seen = set()
        for root, _, names in os.walk(self.cc_dir):
            for n in names:
                if not n.endswith(".jsonl"):
                    continue
                p = os.path.join(root, n)
                try:
                    st = os.stat(p)
                except OSError:
                    continue
                if st.st_mtime < cut:
                    continue
                seen.add(p)
                off = self._cc_off.get(p, 0)
                if st.st_size < off:          # rewritten: read it again
                    off = 0
                if st.st_size == off:
                    continue
                try:
                    with open(p, "rb") as f:
                        f.seek(off)
                        data = f.read(st.st_size - off)
                except OSError:
                    continue
                end = data.rfind(b"\n") + 1   # leave a half-written line
                for line in data[:end].split(b"\n"):
                    if b'"usage"' in line:    # cheap filter before json
                        self._cc_line(line)
                self._cc_off[p] = off + end
        for p in list(self._cc_off):
            if p not in seen:
                del self._cc_off[p]

    def _cc_line(self, line):
        try:
            d = json.loads(line)
        except ValueError:
            return
        if not isinstance(d, dict) or d.get("type") != "assistant":
            return
        m = d.get("message") or {}
        u = m.get("usage") or {}
        model = m.get("model") or ""
        if model == "<synthetic>" or not isinstance(u, dict):
            return
        v = _vec(u.get("input_tokens"), u.get("output_tokens"),
                 u.get("cache_read_input_tokens"),
                 u.get("cache_creation_input_tokens"))
        ts = _iso(d.get("timestamp"))
        if not any(v) or ts is None:
            return
        key = m.get("id") or d.get("requestId") or d.get("uuid")
        usd = None
        if self.cc_cost:
            try:
                c = self.cc_cost(model, u)
                usd = c[0] if c else None
            except Exception:
                usd = None
        # a later row of the same message replaces the provisional one
        self._cc[key] = (_hour(ts), canon(model), v, usd)

    # ---------------------------------------------------------- Devin
    def scan_devin(self):
        if not self.devin_db or not os.path.exists(self.devin_db):
            return
        try:
            db = sqlite3.connect("file:%s?mode=ro" % self.devin_db,
                                 uri=True, timeout=1)
            db.execute("PRAGMA busy_timeout=500")
            top = db.execute("SELECT MAX(row_id) FROM message_nodes"
                             ).fetchone()[0] or 0
            if top < self._dv_row:              # a new database
                self._dv, self._dv_row = {}, 0
            rows = db.execute(
                "SELECT row_id, chat_message FROM message_nodes"
                " WHERE row_id > ? AND row_id <= ?"
                " AND chat_message LIKE '%\"metrics\"%'",
                (self._dv_row, top)).fetchall()
            db.close()
        except sqlite3.Error as e:
            log("usage: devin db: %s" % e)
            return
        for rid, blob in rows:
            self._devin_row(blob)
        self._dv_row = top

    def _devin_row(self, blob):
        try:
            d = json.loads(blob)
        except ValueError:
            return
        md = (d.get("metadata") if isinstance(d, dict) else None) or {}
        m = md.get("metrics") or {}
        if not isinstance(m, dict):
            return
        v = _vec(m.get("input_tokens"), m.get("output_tokens"),
                 m.get("cache_read_tokens"), m.get("cache_creation_tokens"))
        ts = _iso(md.get("created_at"))
        if not any(v) or ts is None:
            return
        model = md.get("generation_model") or ""
        key = md.get("request_id") or "%s|%s" % (md.get("created_at"), model)
        if key in self._dv:                     # the same call, copied
            return
        self._dv[key] = (_hour(ts), canon(model), v,
                         self._usd(model, v))

    def _usd(self, model, v):
        if not self.cost:
            return None
        try:
            c = self.cost({"generation_model": model, "input_tokens": v[0],
                           "output_tokens": v[1], "cache_read_tokens": v[2],
                           "cache_creation_tokens": v[3]})
            return c[0] if c else None
        except Exception:
            return None

    # ---------------------------------------------------------- Droid
    def _droid_load(self):
        d = None
        if self.droid_file:
            try:
                with open(self.droid_file, encoding="utf-8") as f:
                    d = json.load(f)
            except (OSError, ValueError):
                d = None
        if not isinstance(d, dict):
            d = {}
        return {"seen": d.get("seen") or {}, "hours": d.get("hours") or {}}

    def _droid_save(self):
        if not self.droid_file:
            return
        cut = _hour(time.time() - KEEP_DAYS * 86400)
        self._dr["hours"] = {h: v for h, v in self._dr["hours"].items()
                             if h >= cut}
        tmp = self.droid_file + ".tmp"
        try:
            os.makedirs(os.path.dirname(self.droid_file), exist_ok=True)
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self._dr, f)
            os.replace(tmp, self.droid_file)
        except OSError as e:
            log("usage: droid save: %s" % e)

    def scan_droid(self, parse=None):
        """parse(path) -> droid_buddy._Parse (newest reply model/time and
        reply count); injectable for tests."""
        if not self.droid_dir or not os.path.isdir(self.droid_dir):
            return
        if parse is None:
            import droid_buddy
            parse = droid_buddy.parse
        seen, changed = self._dr["seen"], False
        try:
            dirs = [os.path.join(self.droid_dir, n)
                    for n in os.listdir(self.droid_dir)]
        except OSError:
            return
        for d in dirs:
            if not os.path.isdir(d):
                continue
            try:
                names = os.listdir(d)
            except OSError:
                continue
            for n in names:
                if not n.endswith(".settings.json"):
                    continue
                sid = n[:-len(".settings.json")]
                try:
                    with open(os.path.join(d, n), encoding="utf-8") as f:
                        st = json.load(f)
                except (OSError, ValueError):
                    continue
                tu = st.get("tokenUsage") or {}
                cur = _vec(tu.get("inputTokens"), tu.get("outputTokens"),
                           tu.get("cacheReadTokens"),
                           tu.get("cacheCreationTokens"))
                tx = os.path.join(d, sid + ".jsonl")
                p = parse(tx) if os.path.exists(tx) else None
                replies = getattr(p, "replies", 0) or 0
                old = seen.get(sid)
                if old and old.get("tok") == cur and \
                        old.get("n", 0) == replies:
                    continue
                prev = (old or {}).get("tok") or [0, 0, 0, 0]
                delta = [max(0, a - b) for a, b in zip(cur, prev)]
                turns = max(0, replies - (old or {}).get("n", 0))
                seen[sid] = {"tok": cur, "n": replies}
                changed = True
                if not any(delta):
                    continue
                # first sight: the whole total at its last reply; after
                # that, growth lands at the hour it was noticed
                ts = time.time()
                if old is None and p is not None:
                    ts = p.reply_ts or p.answer_ts or ts
                model = (getattr(p, "model", None) or st.get("model")
                         or "")
                self._droid_book(_hour(ts), model, delta, turns)
        if changed:
            self._droid_save()

    def _droid_book(self, hour, model, v, turns):
        hrs = self._dr["hours"].setdefault(hour, {})
        k = canon(model)
        row = hrs.setdefault(k, {"tok": [0, 0, 0, 0], "n": 0, "usd": 0.0})
        row["tok"] = [a + b for a, b in zip(row["tok"], v)]
        row["n"] += turns
        usd = self._usd(model, v)
        if usd is None or row["usd"] is None:
            row["usd"] = None                   # no list price for it
        else:
            row["usd"] += usd

    # -------------------------------------------------------- summary
    def _rows(self, harness):
        """(hour, model, vec, requests, usd) for everything kept."""
        if harness == "cc":
            for h, m, v, usd in list(self._cc.values()):
                yield h, m, v, 1, usd
        elif harness == "devin":
            for h, m, v, usd in list(self._dv.values()):
                yield h, m, v, 1, usd
        elif harness == "droid":
            for h, ms in list(self._dr["hours"].items()):
                for m, r in ms.items():
                    yield h, m, r["tok"], r.get("n", 0), r.get("usd")

    def summarize(self, harness, now=None):
        """{day, week, month}: each {keys, total, usd, partial, rows}.
        day = today's 24 hours, week/month = the last 7/30 days, today
        included. rows = one per model, biggest first, each with its
        token split, request count, list-price estimate and one total
        per bucket (the chart's columns)."""
        now = now or time.time()
        today = date.fromtimestamp(now)
        days30 = [(today - timedelta(days=i)).isoformat()
                  for i in range(29, -1, -1)]
        per = {"day": ["%s %02d" % (today.isoformat(), h) for h in range(24)],
               "week": days30[-7:], "month": days30}
        idx = {p: {k: i for i, k in enumerate(keys)} for p, keys in per.items()}
        acc = {p: {} for p in per}
        for h, m, v, n, usd in self._rows(harness):
            for p in per:
                i = idx[p].get(h if p == "day" else h[:10])
                if i is None:
                    continue
                r = acc[p].get(m)
                if r is None:
                    r = acc[p][m] = {"model": m, "name": pretty(m),
                                     "family": self.family(m),
                                     "tok": [0, 0, 0, 0], "n": 0,
                                     "usd": 0.0,
                                     "series": [0] * len(per[p])}
                for j in range(4):
                    r["tok"][j] += v[j]
                r["n"] += n
                r["series"][i] += sum(v)
                r["usd"] = None if usd is None or r["usd"] is None \
                    else r["usd"] + usd
        out = {"at": int(now), "harness": harness}
        for p, keys in per.items():
            rows = sorted(acc[p].values(), key=lambda r: -sum(r["tok"]))
            for r in rows:
                r["total"] = sum(r["tok"])
                if r["usd"] is not None:
                    r["usd"] = round(r["usd"], 4)
            priced = [r["usd"] for r in rows if r["usd"] is not None]
            out[p] = {
                # what the panel labels the columns with
                "keys": [k[11:] if p == "day" else k[5:] for k in keys],
                "total": sum(r["total"] for r in rows),
                "usd": round(sum(priced), 4) if priced else None,
                "partial": len(priced) < len(rows),
                "rows": rows}
        return out

    def _prune(self):
        cut = _hour(time.time() - KEEP_DAYS * 86400)
        for d in (self._cc, self._dv):
            for k in [k for k, r in d.items() if r[0] < cut]:
                del d[k]

    def state(self, harness):
        return self._sum.get(harness)

    def scan_once(self):
        for h, fn in (("cc", self.scan_cc), ("devin", self.scan_devin),
                      ("droid", self.scan_droid)):
            if h not in self.which:
                continue
            t0 = time.time()
            try:
                fn()
                self._sum[h] = self.summarize(h)
            except Exception as e:              # one harness can't stop the rest
                log("usage: %s scan: %s: %s" % (h, type(e).__name__, e))
                continue
            dt = time.time() - t0
            if dt > 3:
                log("usage: %s scan took %.1fs" % (h, dt))
        self._prune()

    def refresh(self):
        self.kick.set()

    def run(self):
        while not self.stop_flag.is_set():
            self.scan_once()
            self.kick.wait(self.every)
            self.kick.clear()
