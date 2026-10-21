import json
import os
import tempfile
import unittest
from pathlib import Path

from sessionbridge import devin, droid, droid_build, droid_check, ir
from tests.fixtures import make_db, rich_session


def messages(events):
    return [e for e in events if e["type"] == "message"]


class BuildTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        db = make_db(Path(cls.tmp.name) / "s.db", {"rich": rich_session()})
        with devin.connect(db) as con:
            cls.session = devin.read_session(con, "rich")

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def build(self, **kw):
        return droid_build.build(self.session, droid_build.Options(owner="me", organization_id="org-me", **kw))

    def test_valid_and_start(self):
        events, settings, r = self.build(host_id="h1", model="claude-opus-5-5")
        self.assertEqual(droid_check.check(events), [])
        start = events[0]
        self.assertEqual(start["type"], "session_start")
        self.assertEqual((start["owner"], start["hostId"], start["cwd"]), ("me", "h1", "C:\\work\\proj"))
        # droid fills a missing one in on the first message by rewriting the file,
        # which fails on Windows and drops that message
        self.assertEqual(start["organizationId"], "org-me")
        self.assertEqual(start["sessionBridgeSource"], "devin:rich")
        self.assertEqual(start["title"], "[devin] Build the thing")
        self.assertEqual(settings, {"model": "claude-opus-5-5"})
        self.assertNotIn("providerLock", settings)  # a mismatched lock would flatten history to text

    def test_chain_and_order(self):
        events, _, _ = self.build()
        ms = messages(events)
        self.assertEqual([m["seq"] for m in ms], list(range(len(ms))))
        self.assertNotIn("parentId", ms[0])
        for a, b in zip(ms, ms[1:]):
            self.assertEqual(b["parentId"], a["id"])
            self.assertLessEqual(a["timestamp"], b["timestamp"])
        self.assertEqual(ms[0]["timestamp"], "2026-10-14T00:59:00.000Z")  # the summary keeps its own time

    def test_summary_kept_for_model(self):
        ms = messages(self.build()[0])
        self.assertEqual(ms[0]["message"]["visibility"], "llm_only")
        self.assertIn("Summary: earlier we set up the repo.", ms[0]["message"]["content"][0]["text"])

    def test_parallel_calls_answered_together_in_order(self):
        ms = messages(self.build()[0])
        call = next(m for m in ms if any(b["type"] == "tool_use" for b in m["message"]["content"]))
        uses = [b for b in call["message"]["content"] if b["type"] == "tool_use"]
        self.assertEqual([u["name"] for u in uses], ["Execute", "Read"])
        self.assertTrue(all(droid_check.ID.match(u["id"]) for u in uses))
        self.assertEqual(uses[0]["input"], {"command": "ls", "timeout": 120})
        self.assertEqual(uses[1]["input"], {"file_path": "C:\\work\\proj\\a.py"})
        answer = ms[ms.index(call) + 1]["message"]["content"]
        self.assertEqual([b["tool_use_id"] for b in answer], [u["id"] for u in uses])
        self.assertEqual([b["is_error"] for b in answer], [False, True])

    def test_orphan_result_kept_after_results(self):
        ms = messages(self.build()[0])
        m = next(m for m in ms if any("stray output" in (b.get("text") or "") for b in m["message"]["content"]))
        kinds = [b["type"] for b in m["message"]["content"]]
        self.assertEqual(kinds, ["tool_result", "text"])
        self.assertEqual(m["message"]["content"][0]["content"], "1 passed")
        call = ms[ms.index(m) - 1]["message"]["content"][0]
        self.assertEqual(call["input"]["command"], 'cd "C:\\other"; npm test')

    def test_images(self):
        events, _, r = self.build()
        user = next(m for m in messages(events) if any(b["type"] == "image" for b in m["message"]["content"]))
        kinds = [b["type"] for b in user["message"]["content"]]
        self.assertEqual(kinds, ["text", "image", "text"])
        self.assertIn("image/bmp", user["message"]["content"][2]["text"])
        self.assertEqual((r.images, r.images_as_text), (1, 1))

    def test_unfinished_call_marked_interrupted(self):
        events, _, r = self.build(note=False)
        last = messages(events)[-1]["message"]
        self.assertEqual(last["content"][0]["is_error"], True)
        self.assertEqual(last["content"][0]["content"], droid_build.INTERRUPTED)
        self.assertEqual(r.interrupted, 1)
        self.assertEqual(r.unmapped, {"get_output": 1})

    def test_turn_outcomes(self):
        events, _, _ = self.build()
        outcomes = [e for e in events if e["type"] == "agent_turn_outcome"]
        # the first turn ended in an answer; the second stopped mid-call
        self.assertEqual(len(outcomes), 1)
        idx = events.index(outcomes[0])
        self.assertEqual(events[idx - 1]["message"]["content"][0]["text"], "Tests pass.")

    def test_note(self):
        ms = messages(self.build()[0])
        note = ms[-1]["message"]
        self.assertEqual(note["visibility"], "llm_only")
        text = note["content"][0]["text"]
        self.assertIn("exec -> Execute", text)
        self.assertIn("get_output", text)
        self.assertIn("1 tool call(s) stopped", text)
        self.assertEqual(len(messages(self.build(note=False)[0])), len(ms) - 1)

    def test_thinking_modes(self):
        def thinking(events):
            return [b for m in messages(events) for b in m["message"]["content"] if b["type"] == "thinking"]

        down = thinking(self.build()[0])
        self.assertEqual([(b["signature"], b["signatureProvider"]) for b in down], [("", "unknown")] * 2)
        # stored as said; droid itself wraps unverifiable thinking in <thinking> tags when sending
        self.assertEqual(down[0]["thinking"], "plan it")
        keep = thinking(self.build(thinking="keep")[0])
        self.assertEqual([b["signatureProvider"] for b in keep], ["anthropic", "google"])
        self.assertEqual(keep[0]["signature"], "c2lnbmVk")
        self.assertEqual(thinking(self.build(thinking="drop")[0]), [])

    def test_repeated_source_ids_stay_unique(self):
        s = ir.Session("devin", "x", "C:\\p", turns=[
            ir.UserTurn([ir.Text("go")]),
            ir.AssistantTurn([ir.ToolCall("call_1", "exec", {"command": "a"})]),
            ir.ToolResult("call_1", "A"),
            ir.AssistantTurn([ir.ToolCall("call_1", "exec", {"command": "b"})]),
            ir.ToolResult("call_1", "B"),
        ])
        events, _, _ = droid_build.build(s, droid_build.Options(owner="me", organization_id="o", note=False))
        self.assertEqual(droid_check.check(events), [])
        ms = messages(events)
        self.assertNotEqual(ms[1]["message"]["content"][0]["id"], ms[3]["message"]["content"][0]["id"])
        self.assertEqual(ms[4]["message"]["content"][0]["content"], "B")


class CheckTest(unittest.TestCase):
    def start(self):
        return {"type": "session_start", "id": "s", "owner": "me", "organizationId": "o", "cwd": "C:\\p"}

    def msg(self, i, role, content, parent=None):
        e = {"type": "message", "id": f"m{i}", "timestamp": "2026-10-14T00:00:00.000Z", "message": {"role": role, "content": content}}
        if parent:
            e["parentId"] = parent
        return e

    def test_catches(self):
        use = {"type": "tool_use", "id": "t1", "name": "Read", "input": {}}
        res = {"type": "tool_result", "tool_use_id": "t1", "content": "x"}
        ok = [self.start(), self.msg(1, "user", [{"type": "text", "text": "a"}]),
              self.msg(2, "assistant", [use], "m1"), self.msg(3, "user", [res], "m2")]
        self.assertEqual(droid_check.check(ok), [])
        cases = {
            "unknown key": [ok[0], self.msg(1, "user", [{"type": "text", "text": "a", "extra": 1}])],
            "is not [A-Za-z0-9_-]+": [ok[0], ok[1], self.msg(2, "assistant", [dict(use, id="a:b#c")], "m1"),
                                      self.msg(3, "user", [dict(res, tool_use_id="a:b#c")], "m2")],
            "never answered": ok[:3] + [self.msg(3, "user", [{"type": "text", "text": "b"}], "m2")],
            "unanswered": ok[:3],
            "must come first": ok[:3] + [self.msg(3, "user", [{"type": "text", "text": "b"}, res], "m2")],
            "parentId": ok[:2] + [self.msg(2, "assistant", [{"type": "text", "text": "x"}], "nope")],
            "media_type": [ok[0], self.msg(1, "user", [{"type": "image", "source": {"type": "base64", "data": "x", "media_type": "image/bmp"}}])],
            "not session_start": ok[1:],
            "organizationId must be": [{k: v for k, v in ok[0].items() if k != "organizationId"}] + ok[1:],
        }
        for want, events in cases.items():
            with self.subTest(want):
                errs = droid_check.check(events)
                self.assertTrue(any(want in e for e in errs), errs)


class FilesTest(unittest.TestCase):
    def test_project_key(self):
        if os.name == "nt":
            self.assertEqual(droid.project_key("C:\\Users\\me\\no-such-dir\\"), "-C-Users-me-no-such-dir")
        else:
            self.assertEqual(droid.project_key("/home/me/no-such-dir/"), "-home-me-no-such-dir")

    def test_project_key_matches_droid_folders(self):
        """Against droid's own output: each session it wrote sits in the
        folder project_key gives for its cwd (when that cwd still exists)."""
        checked = 0
        for f in droid.sessions_dir().glob("*/*.jsonl"):
            with open(f, encoding="utf-8") as fh:
                try:
                    start = json.loads(fh.readline())
                except ValueError:
                    continue
            cwd = start.get("cwd")
            if start.get("type") != "session_start" or not cwd or not os.path.isdir(cwd):
                continue
            with self.subTest(cwd=cwd):
                self.assertEqual(droid.project_key(cwd), f.parent.name)
            checked += 1
        if not checked:
            self.skipTest("no droid sessions on this machine to compare with")

    def test_write_and_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.assertEqual(droid.identity(root).organization_id, None)
            s = ir.Session("devin", "x", tmp, title="t", turns=[ir.UserTurn([ir.Text("hi")]), ir.AssistantTurn([ir.Text("yo")])])
            events, settings, r = droid_build.build(s, droid_build.Options(owner="alice", organization_id="o", host_id="h"))
            path = droid.write(events, settings, root)
            self.assertEqual(path.parent.name, droid.project_key(tmp))
            self.assertEqual(droid.load(path), events)
            self.assertEqual(json.loads((path.parent / f"{r.session_id}.settings.json").read_text()), {})
            # a session we wrote is never taken as the account's own
            self.assertEqual(droid.identity(root).organization_id, None)
            with self.assertRaises(FileExistsError):
                droid.write(events, settings, root)
            self.assertEqual([p.name for p in path.parent.iterdir() if p.name.startswith(".bridge-")], [])

    def test_identity_from_latest_own_session(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            d = root / "-C-p"
            d.mkdir()
            def put(name, start, t):
                f = d / f"{name}.jsonl"
                f.write_text(json.dumps({"type": "session_start", "id": name, **start}) + "\n", encoding="utf-8")
                os.utime(f, (t, t))
            put("old", {"owner": "me", "hostId": "h1", "organizationId": "org-old"}, 1000)
            put("new", {"owner": "me", "hostId": "h2", "organizationId": "org-new"}, 2000)
            put("noorg", {"owner": "me", "hostId": "h3"}, 3000)
            put("ours", {"owner": "x", "organizationId": "org-ours", "sessionBridgeSource": "devin:a"}, 4000)
            put("legacy", {"owner": "x", "organizationId": "org-legacy", "title": "[devin] t"}, 5000)
            put("released", {"owner": "x", "organizationId": "org-rel", "sessionBridgeOrganizationId": "org-new"}, 6000)
            me = droid.identity(root)
            self.assertEqual((me.owner, me.host_id, me.organization_id), ("me", "h2", "org-new"))
            # droid's org-policy cache follows an account switch, a session's org doesn't
            home = Path(tmp) / "home"
            home.mkdir()
            self.assertEqual(droid.identity(root, home).organization_id, "org-new")  # no cache yet
            (home / "org-managed-settings.cache.json").write_text(json.dumps({"orgId": "org-switched", "userId": "u"}), encoding="utf-8")
            self.assertEqual(droid.identity(root, home).organization_id, "org-switched")
            self.assertEqual(droid.identity(root, home).host_id, "h2")
            (home / "org-managed-settings.cache.json").write_text("not json", encoding="utf-8")
            self.assertEqual(droid.identity(root, home).organization_id, "org-new")

    @unittest.skipUnless(os.name == "nt", "8.3 short names are a Windows thing")
    def test_short_path_written_long(self):
        with tempfile.TemporaryDirectory() as tmp:
            import ctypes
            buf = ctypes.create_unicode_buffer(1024)
            ctypes.windll.kernel32.GetShortPathNameW(tmp, buf, 1024)
            short = buf.value
            s = ir.Session("devin", "x", short, turns=[ir.UserTurn([ir.Text("hi")]), ir.AssistantTurn([ir.Text("yo")])])
            events, _, _ = droid_build.build(s, droid_build.Options(owner="me", organization_id="o"))
            # droid keys its folder off session_start.cwd as written, so it must
            # be the long form our folder name is made from
            self.assertEqual(events[0]["cwd"], droid.canonical_cwd(tmp))
            self.assertNotIn("~", events[0]["cwd"])
            self.assertEqual(droid.project_key(short), droid.project_key(events[0]["cwd"]))


if __name__ == "__main__":
    unittest.main()
