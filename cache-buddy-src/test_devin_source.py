# -*- coding: utf-8 -*-
"""Devin session-source tags: the "CLI"/"desktop" tag follows who runs
the session (its lock's holder), not where it was created and not which
route renewal takes. Temp state.vscdb + temp sessions.db; the Restart
Manager scan is patched — nothing real is opened."""
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import cache_buddy as cb               # noqa: E402
import devin_cli                       # noqa: E402


def _sessions_db(path):
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE sessions (id TEXT, title TEXT,"
               " last_activity_at INTEGER, model TEXT, hidden INTEGER)")
    db.execute("CREATE TABLE message_nodes (session_id TEXT,"
               " chat_message TEXT)")
    for sid in ("sid-space", "sid-info", "sid-cloud", "sid-term"):
        db.execute("INSERT INTO sessions VALUES (?,?,?,?,0)",
                   (sid, sid, 1700000000, "swe"))
    db.commit()
    db.close()


def _vscdb(path):
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE ItemTable (key TEXT, value TEXT)")
    db.execute("INSERT INTO ItemTable VALUES (?,?)",
               ("windsurfSpace.resourceToSpace",
                json.dumps({"sp1": ["cascade-acp/devin-cli/sid-space"]})))
    db.execute("INSERT INTO ItemTable VALUES (?,?)",
               ("windsurfSpace.metadata", json.dumps({})))
    # one row per session Desktop knows — the case that was missed
    db.execute("INSERT INTO ItemTable VALUES (?,?)",
               ("windsurf.acp.sessioninfo.session.acp/devin-cli/sid-info",
                json.dumps({"providerId": "devin-cli",
                            "location": {"kind": "local"}})))
    # a cloud session under the same prefix shape: ignored
    db.execute("INSERT INTO ItemTable VALUES (?,?)",
               ("windsurf.acp.sessioninfo.session.acp/devin-cli/sid-cloud",
                json.dumps({"providerId": "devin-cloud",
                            "location": {"kind": "cloud"}})))
    db.commit()
    db.close()


class TestSource(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = os.path.join(self.tmp.name, "sessions.db")
        self.vs = os.path.join(self.tmp.name, "state.vscdb")
        _sessions_db(self.db)
        _vscdb(self.vs)
        cb._DESK_SIDS = (0, None)
        cb._HELD_SIDS = (0, {})
        self.addCleanup(setattr, cb, "_DESK_SIDS", (0, None))
        self.addCleanup(setattr, cb, "_HELD_SIDS", (0, {}))
        p = mock.patch.object(cb, "DB", self.db)
        p.start()
        self.addCleanup(p.stop)

    def _list(self, holders):
        with mock.patch.object(devin_cli, "lock_holder_kinds",
                               lambda: holders):
            return cb.list_spaces(self.vs)

    def test_origin_set_includes_sessioninfo(self):
        self._list({})
        self.assertEqual(cb._DESK_SIDS[1], {"sid-space", "sid-info"})
        # cloud-kind sessioninfo row did not make it into the origin set
        self.assertNotIn("sid-cloud", cb._DESK_SIDS[1])

    def test_unheld_follows_origin(self):
        self._list({})
        self.assertEqual(cb.session_source("sid-info"), "desktop")
        self.assertEqual(cb.session_source("sid-term"), "cli")

    def test_holder_beats_origin(self):
        # a Desktop session resumed in a terminal: held by a plain
        # devin.exe -> cli, wherever it was created
        self._list({"sid-info": "cli"})
        self.assertEqual(cb.session_source("sid-info"), "cli")
        # Desktop's agent holding it: desktop even with no origin entry
        self._list({"sid-term": "desktop"})
        self.assertEqual(cb.session_source("sid-term"), "desktop")

    def test_spaces_tags(self):
        out = self._list({"sid-term": "desktop"})
        flat = {s["sid"]: s for s in out if s.get("type") != "group"}
        self.assertEqual(flat["sid-term"]["source"], "desktop")
        self.assertEqual(flat["sid-info"]["source"], "desktop")
        self.assertEqual(flat["sid-cloud"]["source"], "cli")

    def test_proxied_desktop_stays_desktop(self):
        """The bug: Desktop's agent goes through the proxy when it was
        started after Buddy — the session must still tag desktop (and
        _via_proxy, untouched, keeps the proxy-replay route)."""
        self._list({"sid-info": "desktop"})
        self.assertEqual(cb.session_source("sid-info"), "desktop")
        # routing inputs are unchanged: _via_proxy is still the decision
        import inspect
        self.assertIn("_via_proxy", inspect.getsource(
            cb.Monitor._tick))


if __name__ == "__main__":
    unittest.main()
