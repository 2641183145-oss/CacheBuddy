# -*- coding: utf-8 -*-
"""droid_buddy warm-scan: Factory auto-creates an empty "New Session"
transcript on every launch (user lines, content [], model "auto", never
answered). With no reply there is no cache to keep — it must not show up
in the cache card."""
import json
import os
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import cache_buddy as cb               # noqa: E402
import droid_buddy                     # noqa: E402


def _ts():
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _write(sess_dir, slug, sid, lines):
    d = os.path.join(sess_dir, slug)
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, sid + ".jsonl")
    with open(path, "w", encoding="utf-8") as f:
        for l in lines:
            f.write(json.dumps(l) + "\n")
    return path


class TestWarmPoll(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.F = os.path.join(self.tmp.name, "factory")
        self.sd = os.path.join(self.F, "sessions")
        os.makedirs(self.sd)
        pts = [mock.patch.object(droid_buddy, "SESS_DIR", self.sd),
               mock.patch.object(droid_buddy, "INDEX_FILE",
                                 os.path.join(self.F,
                                              "sessions-index.json")),
               mock.patch.object(droid_buddy, "ORG_FILE",
                                 os.path.join(self.F,
                                              "org-managed-settings.cache.json"))]
        for p in pts:
            p.start()
            self.addCleanup(p.stop)
        self.mon = droid_buddy.DroidMonitor(
            on_state=None, idle_secs=900,
            ledger_file=os.path.join(self.tmp.name, "ledger.json"))

    def test_empty_new_session_hidden(self):
        ts = _ts()
        # the exact shape Factory writes on every launch
        _write(self.sd, "ws1", "auto-empty", [
            {"type": "session_start", "id": "auto-empty",
             "title": "New Session", "cwd": "C:\\w",
             "organizationId": "orgA"},
            {"type": "message", "id": "u1", "timestamp": ts,
             "message": {"role": "user", "content": [], "modelId": "auto"}},
            {"type": "message", "id": "u2", "timestamp": ts,
             "message": {"role": "user", "content": [], "modelId": "auto"}}])
        # a real session for contrast: answered once
        _write(self.sd, "ws1", "real-one", [
            {"type": "session_start", "id": "real-one",
             "title": "real work", "cwd": "C:\\w",
             "organizationId": "orgA"},
            {"type": "message", "id": "u1", "timestamp": ts,
             "message": {"role": "user", "content":
                         [{"text": "hi"}], "modelId": "auto"}},
            {"type": "message", "id": "a1", "timestamp": ts,
             "message": {"role": "assistant", "content":
                         [{"text": "ok"}], "modelId": "swe"}}])
        self.mon._warm_poll()
        self.assertNotIn("auto-empty", self.mon.warm)
        self.assertIn("real-one", self.mon.warm)


if __name__ == "__main__":
    unittest.main()
