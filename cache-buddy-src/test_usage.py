# -*- coding: utf-8 -*-
"""usage.Tracker's per-account Droid usage: ledger v2 nests hours by org,
summaries follow the signed-in account (plus account-less sessions), and
state() flips right away on a switch. Temp dirs only, parse injected."""
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import usage                            # noqa: E402
import droid_buddy                      # noqa: E402


def _session(d, sid, tok, org):
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, sid + ".settings.json"), "w") as f:
        json.dump({"tokenUsage": {"inputTokens": tok[0],
                                  "outputTokens": tok[1],
                                  "cacheReadTokens": tok[2],
                                  "cacheCreationTokens": tok[3]},
                   "model": "swe-1"}, f)
    with open(os.path.join(d, sid + ".jsonl"), "w") as f:
        f.write(json.dumps({"type": "session_start", "id": sid,
                            "title": sid, "cwd": "C:\\w",
                            "organizationId": org} if org else
                           {"type": "session_start", "id": sid,
                            "title": sid, "cwd": "C:\\w"}) + "\n")
        from datetime import datetime, timezone
        ts = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        f.write(json.dumps({"type": "message", "id": "u1",
                            "timestamp": ts,
                            "message": {"role": "user",
                                        "content": [{"text": "hi"}]}}) + "\n")
        f.write(json.dumps({"type": "message", "id": "a1",
                            "timestamp": ts,
                            "message": {"role": "assistant",
                                        "content": [{"text": "ok"}],
                                        "modelId": "swe-1"}}) + "\n")


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.sd = os.path.join(self.tmp.name, "sessions")
        os.makedirs(self.sd)
        self.uf = os.path.join(self.tmp.name, "droid-usage.json")
        self.nf = os.path.join(self.tmp.name, "droid-accounts.json")
        with open(self.nf, "w") as f:
            json.dump({"orgA": "A", "orgB": "B"}, f)
        self.org = ["orgA"]
        self.tr = usage.Tracker(("droid",), droid_dir=self.sd,
                                droid_file=self.uf, cost=lambda *a: None,
                                droid_org=lambda: self.org[0])

    def totals(self, s):
        return s["week"]["total"]


class TestPerAccount(Base):
    def setUp(self):
        super().setUp()
        _session(os.path.join(self.sd, "ws" + "sa"), "sa",
                 (100, 50, 200, 40), "orgA")
        _session(os.path.join(self.sd, "ws" + "sb"), "sb",
                 (10, 5, 20, 4), "orgB")
        _session(os.path.join(self.sd, "ws" + "sn"), "sn",
                 (7, 3, 8, 2), None)

    def test_filter_by_org(self):
        self.tr.scan_once()
        a = self.totals(self.tr.state("droid"))
        self.assertEqual(a, 100 + 50 + 200 + 40 + 7 + 3 + 8 + 2)
        self.org[0] = "orgB"
        b = self.totals(self.tr.state("droid"))
        self.assertEqual(b, 10 + 5 + 20 + 4 + 7 + 3 + 8 + 2)

    def test_state_flips_without_scan(self):
        self.tr.scan_once()
        self.org[0] = "orgB"
        s = self.tr.state("droid")          # no scan_once in between
        self.assertEqual(self.totals(s),
                         10 + 5 + 20 + 4 + 7 + 3 + 8 + 2)
        self.assertEqual(s["account"], {"org": "orgB", "name": "B"})

    def test_none_org_shows_all(self):
        self.tr.scan_once()
        self.org[0] = None
        s = self.tr.state("droid")
        self.assertEqual(self.totals(s),
                         100 + 50 + 200 + 40 + 10 + 5 + 20 + 4 + 7 + 3 + 8 + 2)

    def test_seen_keeps_org(self):
        self.tr.scan_once()
        seen = self.tr._dr["seen"]
        self.assertEqual(seen["sa"]["org"], "orgA")
        self.assertEqual(seen["sn"]["org"], "")


class TestMigration(Base):
    def test_v1_file_rebooks_fresh(self):
        # a v1 ledger: flat seen/hours — can't be attributed, dropped
        with open(self.uf, "w") as f:
            json.dump({"seen": {"sa": {"tok": [1, 1, 1, 1], "n": 1}},
                       "hours": {"2025-01-01 10": {"swe-1":
                                {"tok": [1, 1, 1, 1], "n": 1, "usd": 0}}}}, f)
        tr = usage.Tracker(("droid",), droid_dir=self.sd,
                           droid_file=self.uf, cost=lambda *a: None,
                           droid_org=lambda: self.org[0])
        self.assertEqual(tr._dr["seen"], {})
        self.assertEqual(tr._dr["hours"], {})
        _session(os.path.join(self.sd, "ws" + "sa"), "sa",
                 (100, 50, 200, 40), "orgA")
        tr.scan_once()
        self.assertEqual(self.totals(tr.state("droid")), 100 + 50 + 200 + 40)
        tr.scan_once()                      # no double counting
        self.assertEqual(self.totals(tr.state("droid")), 100 + 50 + 200 + 40)

    def test_second_instance_no_rebook(self):
        _session(os.path.join(self.sd, "ws" + "sa"), "sa",
                 (100, 50, 200, 40), "orgA")
        self.tr.scan_once()
        tr2 = usage.Tracker(("droid",), droid_dir=self.sd,
                            droid_file=self.uf, cost=lambda *a: None,
                            droid_org=lambda: self.org[0])
        tr2.scan_once()
        self.assertEqual(self.totals(tr2.state("droid")),
                         100 + 50 + 200 + 40)


if __name__ == "__main__":
    unittest.main()
