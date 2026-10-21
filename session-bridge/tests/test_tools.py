import unittest

from sessionbridge.tools import devin_to_droid

CWD = "C:\\work\\proj"


class ToolMapTest(unittest.TestCase):
    def test_exec(self):
        name, a, ok = devin_to_droid("exec", {"command": "ls", "shell_flavor": "bash", "timeout": 120000, "workdir": CWD}, CWD)
        self.assertEqual((name, ok), ("Execute", True))
        self.assertEqual(a, {"command": "ls", "timeout": 120})
        _, a, _ = devin_to_droid("exec", {"command": "ls", "workdir": "C:\\other"}, CWD)
        self.assertEqual(a["command"], 'cd "C:\\other"; ls')
        _, a, _ = devin_to_droid("exec", {"command": "ls", "workdir": "\\\\?\\C:\\WORK\\proj\\"}, CWD)
        self.assertEqual(a["command"], "ls")  # same folder, other spelling
        _, a, _ = devin_to_droid("exec", {"command": "ls", "timeout": 0}, CWD)
        self.assertNotIn("timeout", a)

    def test_read_strips_extended_prefix(self):
        _, a, _ = devin_to_droid("read", {"file_path": "\\\\?\\C:\\a\\b.py", "offset": 3, "limit": 9})
        self.assertEqual(a, {"file_path": "C:\\a\\b.py", "offset": 3, "limit": 9})
        _, a, _ = devin_to_droid("read", {"file_path": "\\\\?\\UNC\\srv\\share\\x"})
        self.assertEqual(a["file_path"], "\\\\?\\UNC\\srv\\share\\x")

    def test_grep(self):
        name, a, _ = devin_to_droid("grep", {"pattern": "x", "path": "C:\\a", "output_mode": "files_with_matches",
                                              "case_insensitive": True, "max_results": 5})
        self.assertEqual(name, "Grep")
        self.assertEqual(a, {"pattern": "x", "path": "C:\\a", "output_mode": "file_paths", "case_insensitive": True, "head_limit": 5})

    def test_edit_write_find(self):
        name, a, _ = devin_to_droid("edit", {"file_path": "f", "old_string": "a", "new_string": "b", "replace_all": True})
        self.assertEqual((name, a), ("Edit", {"file_path": "f", "old_str": "a", "new_str": "b", "change_all": True}))
        name, a, _ = devin_to_droid("write", {"file_path": "f", "content": "c"})
        self.assertEqual((name, a), ("Create", {"file_path": "f", "content": "c"}))
        name, a, _ = devin_to_droid("find_file_by_name", {"path": "C:\\a", "pattern": "*.py"})
        self.assertEqual((name, a), ("Glob", {"patterns": ["*.py"], "folder": "C:\\a"}))

    def test_web_and_skill(self):
        self.assertEqual(devin_to_droid("web_search", {"query": "q"})[:2], ("WebSearch", {"query": "q"}))
        self.assertEqual(devin_to_droid("webfetch", {"url": "u"})[:2], ("FetchUrl", {"url": "u"}))
        self.assertEqual(devin_to_droid("skill", {"command": "invoke", "skill": "s"})[:2], ("Skill", {"skill": "s"}))

    def test_unknown_args_kept(self):
        _, a, _ = devin_to_droid("read", {"file_path": "f", "encoding": "utf-16"})
        self.assertEqual(a["encoding"], "utf-16")

    def test_unmapped_keeps_name(self):
        self.assertEqual(devin_to_droid("get_output", {"shell_id": "x"}), ("get_output", {"shell_id": "x"}, False))

    def test_does_not_mutate_input(self):
        src = {"command": "ls", "workdir": "C:\\o", "timeout": 5000}
        devin_to_droid("exec", src, CWD)
        self.assertEqual(src, {"command": "ls", "workdir": "C:\\o", "timeout": 5000})


if __name__ == "__main__":
    unittest.main()
