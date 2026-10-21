"""The trim, the Droid reader, the Devin writer, round trips, account release."""

import json
import os
import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path

from sessionbridge import api, devin, devin_write, droid, droid_build, droid_check, droid_read, droid_reown, filter as filt, ir
from tests.fixtures import SCHEMA, make_db, rich_session


def calls(turns):
    return [b for t in turns if isinstance(t, ir.AssistantTurn) for b in t.blocks if isinstance(b, ir.ToolCall)]


class TrimTest(unittest.TestCase):
    def turns(self):
        early = [
            ir.UserTurn([ir.Text("fix calculate_total")]),
            ir.AssistantTurn([ir.Thinking("look around first"), ir.Text("Looking."),
                              ir.ToolCall("g", "Grep", {"pattern": "FIXME"}),
                              ir.ToolCall("r", "Read", {"file_path": "src/a.py"})]),
            ir.ToolResult("g", "utils/legacy.py:42: FIXME remove this"),
            ir.ToolResult("r", "def calculate_total(items):\n    return sum(items)"),
            ir.AssistantTurn([ir.ToolCall("w", "Create", {"file_path": "src/a.py", "content": "x"})]),
            ir.ToolResult("w", "ok"),
            ir.AssistantTurn([ir.ToolCall("x", "Execute", {"command": "cat nothing_used_later.txt"})]),
            ir.ToolResult("x", "zzzqqq output"),
            ir.AssistantTurn([ir.ToolCall("g2", "Glob", {"patterns": ["*.md"]})]),
            ir.ToolResult("g2", "README.md"),
            ir.AssistantTurn([ir.Text("calculate_total now handles empty lists.")]),
        ]
        tail = []
        for i in range(filt.TAIL // 2):
            tail += [ir.UserTurn([ir.Text(f"next {i}")]), ir.AssistantTurn([ir.Text(f"ok {i}")])]
        return early + tail

    def test_drops_only_unused_read_only_calls(self):
        out, n = filt.apply(self.turns())
        names = [c.name for c in calls(out)]
        self.assertEqual(n, 2)  # Grep (legacy.py never mentioned) and Glob (README never mentioned)
        self.assertEqual(names, ["Read", "Create", "Execute"])  # Read used; Create writes; Execute could change state

    def test_keeps_text_and_thinking_of_a_trimmed_message(self):
        out, _ = filt.apply(self.turns())
        first = next(t for t in out if isinstance(t, ir.AssistantTurn))
        self.assertEqual([type(b).__name__ for b in first.blocks], ["Thinking", "Text", "ToolCall"])

    def test_every_kept_call_keeps_its_result_and_order(self):
        out, _ = filt.apply(self.turns())
        ids = {c.id for c in calls(out)}
        res = [t.call_id for t in out if isinstance(t, ir.ToolResult)]
        self.assertEqual(set(res), ids)

    def test_note_where_calls_went(self):
        out, _ = filt.apply(self.turns())
        notes = [t for t in out if isinstance(t, ir.Note)]
        self.assertEqual(len(notes), 2)
        # a note never sits between a call and its result
        for i, t in enumerate(out):
            if isinstance(t, ir.Note):
                self.assertNotIsInstance(out[i + 1] if i + 1 < len(out) else None, ir.ToolResult)

    def test_user_turns_untouched(self):
        src = self.turns()
        out, _ = filt.apply(src)
        self.assertEqual([t for t in out if isinstance(t, ir.UserTurn)], [t for t in src if isinstance(t, ir.UserTurn)])

    def test_tail_untouched(self):
        src = [ir.UserTurn([ir.Text("go")])]
        for i in range(filt.TAIL):
            src += [ir.AssistantTurn([ir.ToolCall(f"c{i}", "Grep", {"pattern": "q"})]), ir.ToolResult(f"c{i}", "nothing")]
        out, n = filt.apply(src)
        self.assertEqual(len(calls(out[-filt.TAIL:])), filt.TAIL // 2)

    def test_short_session_unchanged(self):
        src = [ir.UserTurn([ir.Text("hi")]), ir.AssistantTurn([ir.Text("hello")])]
        self.assertEqual(filt.apply(src), (src, 0))

    def test_trimmed_session_still_builds(self):
        out, _ = filt.apply(self.turns())
        s = ir.Session("devin", "x", "C:\\p", turns=out)
        events, _, _ = droid_build.build(s, droid_build.Options(owner="me", organization_id="o"))
        self.assertEqual(droid_check.check(events), [])


class RoundTripTest(unittest.TestCase):
    """devin -> droid -> devin keeps the conversation."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.devin_db = make_db(root / "sessions.db", {"rich": rich_session()})
        self.droid = root / "droid"
        own = self.droid / "-C-elsewhere" / "native.jsonl"  # a session droid itself wrote
        own.parent.mkdir(parents=True)
        own.write_text(json.dumps({"type": "session_start", "id": "native", "owner": "me", "hostId": "h",
                                   "organizationId": "org-signed-in", "cwd": "C:\\elsewhere"}) + "\n", encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def test_droid_reader_reads_what_droid_build_wrote(self):
        r = api.carry("devin", "rich", devin_db=self.devin_db, droid_sessions=self.droid, trim=False)
        s = droid_read.read_session(Path(r["written"]["path"]))
        with devin.connect(self.devin_db) as con:
            orig = devin.read_session(con, "rich")
        self.assertEqual([b.text for t in s.turns if isinstance(t, ir.UserTurn) for b in t.blocks if isinstance(b, ir.Text)][:1],
                         ["build the thing"])
        self.assertEqual(sum(isinstance(t, ir.Summary) for t in s.turns), 1)
        self.assertEqual(len(calls(s.turns)), len(calls(orig.turns)))
        self.assertEqual([c.name for c in calls(s.turns)], ["Execute", "Read", "Execute", "get_output"])
        thinking = [b for t in s.turns if isinstance(t, ir.AssistantTurn) for b in t.blocks if isinstance(b, ir.Thinking)]
        self.assertEqual([b.text for b in thinking], ["plan it", "gemini thought"])  # unwrapped, as said
        # the bridge's own note is not read back as conversation
        self.assertFalse(any(isinstance(t, ir.UserTurn) and "carried over" in str(t.blocks) for t in s.turns))

    def test_back_into_devin(self):
        r = api.carry("devin", "rich", devin_db=self.devin_db, droid_sessions=self.droid, trim=False)
        back = api.carry("droid", r["written"]["session_id"], devin_db=self.devin_db, droid_sessions=self.droid, trim=False)
        sid = back["written"]["session_id"]
        self.assertEqual(back["written"]["resume"]["argv"], ["devin", "--resume", sid])
        with devin.connect(self.devin_db) as con:
            row = con.execute("select working_directory, backend_type, agent_mode, main_chain_id, title from sessions where id = ?", (sid,)).fetchone()
            nodes = con.execute("select node_id, parent_node_id, chat_message from message_nodes where session_id = ? order by node_id", (sid,)).fetchall()
            s = devin.read_session(con, sid)
        self.assertEqual(row[:3], ("C:\\work\\proj", "windsurf", "normal"))
        self.assertEqual(row[3], len(nodes) - 1)
        self.assertTrue(row[4].startswith("[droid] "))
        self.assertEqual([(n, p) for n, p, _ in nodes], [(i, i - 1 if i else None) for i in range(len(nodes))])
        msgs = [json.loads(m) for _, _, m in nodes]
        self.assertTrue(all("message_id" in m and "metadata" in m for m in msgs))
        self.assertEqual([c.name for c in calls(s.turns)], ["exec", "read", "exec", "get_output"])
        first = calls(s.turns)[0]
        self.assertEqual(first.arguments["timeout"], 120000)  # s -> ms again
        # every Devin call is answered by its own tool message
        ids = [c.id for c in calls(s.turns)]
        answered = [t.call_id for t in s.turns if isinstance(t, ir.ToolResult)]
        self.assertTrue(set(ids) <= set(answered))
        self.assertIn("carried over from droid", msgs[-1]["content"])
        self.assertEqual(msgs[-1]["role"], "system")  # for the model, not shown as the user's
        self.assertEqual(sum(isinstance(t, ir.Summary) for t in s.turns), 1)  # the summary survives both hops
        self.assertFalse(any("carried over" in str(getattr(t, "blocks", "")) for t in s.turns))
        th = [m["thinking"] for m in msgs if m.get("thinking")]
        self.assertEqual([t["signature"] for t in th], ["", ""])
        self.assertEqual(th[0]["thinking"], "plan it")

    def test_devin_writer_refuses_non_devin_db(self):
        other = Path(self.tmp.name) / "other.db"
        sqlite3.connect(other).execute("create table x(a)").connection.close()
        s = ir.Session("droid", "x", "C:\\p", turns=[ir.UserTurn([ir.Text("hi")])])
        with self.assertRaises(ValueError):
            devin_write.write(s, other)
        with self.assertRaises(FileNotFoundError):
            devin_write.write(s, Path(self.tmp.name) / "missing.db")

    def test_same_agent_refused(self):
        with self.assertRaises(ValueError):
            api.carry("devin", "rich", "devin", devin_db=self.devin_db)

    def test_dry_run_writes_nothing(self):
        p = api.preview("devin", "rich", devin_db=self.devin_db, droid_sessions=self.droid)
        self.assertTrue(p["dry_run"])
        self.assertNotIn("written", p)
        self.assertEqual([f.name for f in self.droid.rglob("*.jsonl")], ["native.jsonl"])

    def test_carry_takes_signed_in_org(self):
        r = api.carry("devin", "rich", devin_db=self.devin_db, droid_sessions=self.droid)
        start = droid_read._start(Path(r["written"]["path"]))
        self.assertEqual((start["organizationId"], start["owner"], start["hostId"]), ("org-signed-in", "me", "h"))
        self.assertEqual(r["droid_organization_id"], "org-signed-in")
        # a second carry still learns it from droid's own session, not from ours
        r2 = api.carry("devin", "rich", devin_db=self.devin_db, droid_sessions=self.droid)
        self.assertEqual(r2["droid_organization_id"], "org-signed-in")

    def test_carry_refuses_without_own_droid_session(self):
        shutil.rmtree(self.droid / "-C-elsewhere")
        with self.assertRaises(api.NoDroidAccount):
            api.carry("devin", "rich", devin_db=self.devin_db, droid_sessions=self.droid)
        self.assertEqual(list(self.droid.rglob("*.jsonl")), [])
        r = api.carry("devin", "rich", devin_db=self.devin_db, droid_sessions=self.droid, organization_id="given")
        self.assertEqual(r["droid_organization_id"], "given")


class DroidReaderTest(unittest.TestCase):
    def test_reads_droid_native_session(self):
        events = [
            {"type": "session_start", "id": "s1", "owner": "me", "organizationId": "org1", "cwd": "C:\\p"},
            {"type": "message", "id": "context-1", "timestamp": "2026-10-07T03:52:48.560Z",
             "message": {"role": "user", "content": [{"type": "text", "text": "<system-reminder>tools</system-reminder>"}], "visibility": "llm_only"}},
            {"type": "message", "id": "u1", "parentId": "context-1", "timestamp": "2026-10-07T03:52:49.000Z",
             "message": {"role": "user", "content": [{"type": "text", "text": "hey"}]}},
            {"type": "message", "id": "n1", "parentId": "u1", "timestamp": "2026-10-07T03:52:50.000Z",
             "message": {"role": "user", "content": [{"type": "text", "text": "No active subscription found."}], "visibility": "user_only"}},
            {"type": "compaction_state", "id": "c1", "summaryText": "x", "summaryTokens": 1, "removedCount": 1},
            {"type": "message", "id": "a1", "parentId": "n1", "timestamp": "2026-10-07T03:52:51.000Z",
             "message": {"role": "assistant", "modelId": "claude-opus-5-5", "content": [
                 {"type": "thinking", "thinking": "hmm", "signature": "sig", "signatureProvider": "anthropic"},
                 {"type": "redacted_thinking", "data": "xx"},
                 {"type": "tool_use", "id": "t1", "name": "Read", "input": {"file_path": "a"}}]}},
            {"type": "message", "id": "r1", "parentId": "a1", "timestamp": "2026-10-07T03:52:52.000Z",
             "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "is_error": True,
                                                      "content": [{"type": "text", "text": "nope"}]}]}},
            {"type": "agent_turn_outcome", "turnId": "u1", "reason": "completed", "resultKind": "text"},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "-C-p" / "s1.jsonl"
            f.parent.mkdir()
            f.write_text("".join(json.dumps(e) + "\n" for e in events), encoding="utf-8")
            s = droid_read.read_session(f)
            listing = droid_read.list_sessions(Path(tmp))
        self.assertEqual([type(t).__name__ for t in s.turns], ["UserTurn", "AssistantTurn", "ToolResult"])
        a = s.turns[1]
        self.assertEqual(a.model, "claude-opus-5-5")
        self.assertEqual([type(b).__name__ for b in a.blocks], ["Thinking", "ToolCall"])
        self.assertEqual(a.blocks[0].signature_type, "anthropic")
        self.assertEqual((s.turns[2].output, s.turns[2].is_error), ("nope", True))
        self.assertEqual(listing[0].organization_id, "org1")


class ReleaseTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.d = self.root / "-C-p"
        self.d.mkdir()
        # "own" is droid's own session under the signed-in account; a, b, c are
        # sessions of another account, another, and none
        for sid, org, t in (("a", "org1", 100), ("b", "org2", 200), ("c", None, 300), ("own", "org-now", 400)):
            f = self.d / f"{sid}.jsonl"
            start = {"type": "session_start", "id": sid, "owner": "me", "cwd": "C:\\p"}
            if org:
                start["organizationId"] = org
            f.write_text(json.dumps(start) + "\n" + '{"type": "message", "id": "m"}\r\n', encoding="utf-8", newline="")
            os.utime(f, (t, t))

    def tearDown(self):
        self.tmp.cleanup()

    def first(self, sid):
        return droid_reown.droid_first(self.d / f"{sid}.jsonl")

    def test_release_writes_signed_in_org_and_restore_undoes(self):
        self.assertEqual(droid_reown.organizations(self.root), {"org1": 1, "org2": 1, None: 1, "org-now": 1})
        dry = api.droid_release(droid_sessions=self.root, dry_run=True)
        self.assertEqual(sorted((c["session_id"], c["after"]) for c in dry), [("a", "org-now"), ("b", "org-now"), ("c", "org-now")])
        self.assertEqual(self.first("a")["organizationId"], "org1")  # dry run touched nothing

        before = (self.d / "a.jsonl").stat().st_mtime
        cs = api.droid_release(droid_sessions=self.root)
        self.assertEqual(sorted((c["session_id"], c["before"], c["after"]) for c in cs),
                         [("a", "org1", "org-now"), ("b", "org2", "org-now"), ("c", None, "org-now")])
        for sid, old in (("a", "org1"), ("b", "org2"), ("c", None)):
            self.assertEqual(self.first(sid)["organizationId"], "org-now")  # never left absent
            self.assertEqual(self.first(sid)[droid_reown.KEPT], old)
        self.assertNotIn(droid_reown.KEPT, self.first("own"))  # already on it: untouched
        self.assertGreater((self.d / "a.jsonl").stat().st_mtime, before)
        self.assertTrue((self.d / "a.jsonl").read_bytes().endswith(b'{"type": "message", "id": "m"}\r\n'))

        # releasing again keeps the first original, and the account's own
        # session is still what identity() goes by
        api.droid_release(session_id="a", droid_sessions=self.root, to_organization_id="org-later")
        self.assertEqual((self.first("a")["organizationId"], self.first("a")[droid_reown.KEPT]), ("org-later", "org1"))
        self.assertEqual(droid.identity(self.root).organization_id, "org-now")

        cs = api.droid_restore(droid_sessions=self.root)
        self.assertEqual(sorted((c["session_id"], c["after"]) for c in cs), [("a", "org1"), ("b", "org2"), ("c", None)])
        self.assertNotIn("organizationId", self.first("c"))
        for sid in "abc":
            self.assertNotIn(droid_reown.KEPT, self.first(sid))
        self.assertEqual(droid_reown.organizations(self.root), {"org1": 1, "org2": 1, None: 1, "org-now": 1})

    def test_release_some(self):
        api.droid_release(session_id="b", droid_sessions=self.root)
        self.assertEqual((self.first("a")["organizationId"], self.first("b")["organizationId"]), ("org1", "org-now"))
        api.droid_release(organization_id="org1", droid_sessions=self.root)
        self.assertEqual(self.first("a")["organizationId"], "org-now")
        self.assertNotIn("organizationId", self.first("c"))

    def test_copy_to_another_account(self):
        (self.d / "a.settings.json").write_text(json.dumps({
            "model": "gpt-6-sol", "tokenUsage": {"inputTokens": 900, "cacheReadTokens": 5000},
            "lastCallTokenUsage": {"cacheReadTokens": 4000}}), encoding="utf-8")
        with open(self.d / "a.jsonl", "a", encoding="utf-8", newline="") as f:
            f.write('{"type": "message", "id": "half')  # droid mid-write
        os.utime(self.d / "a.jsonl", (100, 100))
        c = api.droid_copy("a", "org-now", droid_sessions=self.root)
        new = c["session_id"]
        self.assertNotEqual(new, "a")
        self.assertEqual((c["before"], c["after"]), ("org1", "org-now"))
        start = self.first(new)
        self.assertEqual((start["id"], start["organizationId"], start[droid.BRIDGE_SOURCE]),
                         (new, "org-now", "droid:a"))
        body = (self.d / f"{new}.jsonl").read_bytes()
        self.assertTrue(body.endswith(b'{"type": "message", "id": "m"}\r\n'))  # half line left out
        st = json.loads((self.d / f"{new}.settings.json").read_text(encoding="utf-8"))
        self.assertEqual(st["tokenUsage"], {"inputTokens": 0, "cacheReadTokens": 0})
        self.assertEqual((st["model"], st["lastCallTokenUsage"]), ("gpt-6-sol", {"cacheReadTokens": 4000}))
        self.assertEqual(self.first("a")["organizationId"], "org1")  # original untouched
        # a copy is not evidence of who is signed in
        self.assertEqual(droid.identity(self.root).organization_id, "org-now")
        with self.assertRaises(FileNotFoundError):
            api.droid_copy("nope", "org-now", droid_sessions=self.root)

    def test_release_needs_a_signed_in_org(self):
        for sid in ("a", "b", "own"):  # leave only sessions that aren't droid's own with an org
            (self.d / f"{sid}.jsonl").unlink()
        with self.assertRaises(api.NoDroidAccount):
            api.droid_release(droid_sessions=self.root)
        self.assertNotIn("organizationId", self.first("c"))


if __name__ == "__main__":
    unittest.main()
