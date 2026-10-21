import tempfile
import unittest
from pathlib import Path

from sessionbridge import devin, ir
from tests.fixtures import make_db, rich_session


class DevinReaderTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = make_db(Path(self.tmp.name) / "sessions.db", {
            "rich": rich_session(),
            "older": {"cwd": "C:\\work\\proj", "title": "old", "head": 0, "updated": 1791000000,
                      "nodes": [(0, None, {"role": "user", "content": "hi", "metadata": {}}, None)]},
            "elsewhere": {"cwd": "C:\\other", "title": "x", "head": 0, "updated": 1793000000,
                          "nodes": [(0, None, {"role": "user", "content": "yo", "metadata": {}}, None)]},
        })
        self.ctx = devin.connect(self.db)
        self.con = self.ctx.__enter__()

    def tearDown(self):
        self.ctx.__exit__(None, None, None)
        self.tmp.cleanup()

    def test_follows_main_chain_not_stale_copies(self):
        chain = devin.main_chain(self.con, "rich")
        self.assertEqual(len(chain), 14)  # nodes 10..23
        self.assertEqual(chain[0][0]["content"], "You are Devin...")

    def test_drops_devin_prompt_keeps_summary(self):
        s = devin.read_session(self.con, "rich")
        kinds = [type(t).__name__ for t in s.turns]
        self.assertEqual(kinds[0], "Summary")
        self.assertNotIn("<system_info", str([getattr(t, "blocks", "") for t in s.turns]))
        self.assertEqual(s.cwd, "C:\\work\\proj")
        self.assertEqual(s.title, "Build the thing")

    def test_turn_contents(self):
        s = devin.read_session(self.con, "rich")
        user = [t for t in s.turns if isinstance(t, ir.UserTurn)]
        self.assertEqual(user[0].blocks[0].text, "build the thing")
        self.assertEqual([type(b).__name__ for b in user[1].blocks], ["Text", "Image", "Image"])
        first = next(t for t in s.turns if isinstance(t, ir.AssistantTurn))
        self.assertEqual([type(b).__name__ for b in first.blocks], ["Thinking", "Text", "ToolCall", "ToolCall"])
        self.assertEqual(first.blocks[0].signature_type, "anthropic")
        results = [t for t in s.turns if isinstance(t, ir.ToolResult)]
        self.assertEqual([r.is_error for r in results], [False, True, False, False])

    def test_pick(self):
        self.assertEqual(devin.pick(self.con, None, None), "elsewhere")
        self.assertEqual(devin.pick(self.con, None, "C:\\work\\proj"), "rich")
        self.assertEqual(devin.pick(self.con, "older"), "older")
        with self.assertRaises(LookupError):
            devin.pick(self.con, None, "C:\\nowhere")

    def test_broken_chain(self):
        db = make_db(Path(self.tmp.name) / "broken.db", {
            "b": {"cwd": "C:\\x", "head": 5, "nodes": [(5, 4, {"role": "user", "content": "x"}, None)]}})
        with devin.connect(db) as con, self.assertRaises(ValueError):
            devin.main_chain(con, "b")

    def test_connection_closed_after(self):
        with devin.connect(self.db) as con:
            pass
        with self.assertRaises(Exception):
            con.execute("select 1")

    def test_missing_db(self):
        with self.assertRaises(FileNotFoundError), devin.connect(Path(self.tmp.name) / "nope.db"):
            pass


if __name__ == "__main__":
    unittest.main()
