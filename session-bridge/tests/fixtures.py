"""A Devin CLI sessions.db in miniature, with the schema the real one has."""

import json
import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE sessions (
  id TEXT PRIMARY KEY, working_directory TEXT NOT NULL, backend_type TEXT NOT NULL,
  model TEXT NOT NULL, agent_mode TEXT NOT NULL, created_at INTEGER NOT NULL,
  last_activity_at INTEGER NOT NULL, title TEXT, main_chain_id INTEGER,
  shell_last_seen_index INTEGER DEFAULT 0, cogs_json TEXT, workspace_dirs TEXT,
  hidden INTEGER NOT NULL DEFAULT 0, metadata TEXT);
CREATE TABLE message_nodes (
  row_id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL, node_id INTEGER NOT NULL,
  parent_node_id INTEGER, chat_message TEXT NOT NULL, created_at INTEGER NOT NULL, metadata TEXT,
  UNIQUE(session_id, node_id));
"""


def msg(role, content="", at="2026-10-14T01:00:00Z", **kw):
    m = {"message_id": f"m-{role}-{abs(hash((role, content, at))) % 10**8}", "role": role, "content": content,
         "metadata": {"created_at": at, **kw.pop("metadata", {})}}
    m.update(kw)
    return m


def call(cid, name, **args):
    return {"id": cid, "name": name, "arguments": args, "index": 0, "kind": "function"}


def tool(cid, content, ok=True, at="2026-10-14T01:00:01Z"):
    meta = {"success": True, "kind": "execute"} if ok else {"success": False, "failure_reason": "ToolError", "kind": "read"}
    return msg("tool", content, at, tool_call_id=cid, metadata={"extensions": {"chisel/tool_result_meta": meta}})


def make_db(path: Path, sessions: dict) -> Path:
    """sessions: id -> dict(cwd, title, nodes=[(node_id, parent, message, meta)], head)."""
    con = sqlite3.connect(path)
    con.executescript(SCHEMA)
    for sid, s in sessions.items():
        con.execute(
            "insert into sessions (id, working_directory, backend_type, model, agent_mode, created_at, last_activity_at, title, main_chain_id) values (?,?,?,?,?,?,?,?,?)",
            (sid, s["cwd"], "windsurf", s.get("model", "claude-sonnet-5-5-medium"), "normal",
             s.get("created", 1792000000), s.get("updated", 1792000100), s.get("title"), s["head"]),
        )
        for nid, parent, m, meta in s["nodes"]:
            con.execute(
                "insert into message_nodes (session_id, node_id, parent_node_id, chat_message, created_at, metadata) values (?,?,?,?,?,?)",
                (sid, nid, parent, json.dumps(m, ensure_ascii=False), 0, json.dumps(meta) if meta is not None else None),
            )
    con.commit()
    con.close()
    return path


PNG = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=="


def rich_session(cwd="C:\\work\\proj"):
    """A session that went through a prefix rebuild, with every case the
    converter handles. The first root (0-2) is the old copy; the chain from
    head runs through the second root (10...)."""
    sys_prompt = msg("system", "You are Devin...")
    first_user = msg("user", "build the thing", "2026-10-14T01:00:00Z")
    nodes = [
        (0, None, sys_prompt, None),
        (1, 0, msg("system", "<rules/>"), None),
        (2, 1, first_user, None),  # stale copy, not on the chain
        (10, None, sys_prompt, {"summarized_from": None, "is_system_prefix": True}),
        (11, 10, msg("system", "<system_info/>"), {"summarized_from": None, "extensions": {"compact/prior_node_ids": [1]}}),
        (12, 11, msg("system", "Summary: earlier we set up the repo.", "2026-10-14T00:59:00Z"), {"summarized_from": [0, 1]}),
        (13, 12, first_user, None),
        (14, 13, msg("assistant", "Looking first.", "2026-10-14T01:00:02Z",
                     thinking={"thinking": "plan it", "signature": "c2lnbmVk", "signature_type": "anthropic"},
                     tool_calls=[call("functions.exec:0#abc", "exec", command="ls", shell_flavor="bash", workdir=cwd, timeout=120000),
                                 call("functions.read:1#def", "read", file_path="\\\\?\\" + cwd + "\\a.py")]), None),
        (15, 14, tool("functions.exec:0#abc", "a.py\nb.py"), None),
        (16, 15, tool("functions.read:1#def", "no such file", ok=False), None),
        (17, 16, msg("assistant", "", "2026-10-14T01:00:05Z",
                     tool_calls=[call("functions.exec:2#ghi", "exec", command="npm test", workdir="C:\\other")]), None),
        (18, 17, tool("functions.get_output:9#zzz", "stray output"), None),  # orphan
        (19, 18, tool("functions.exec:2#ghi", "1 passed"), None),
        (20, 19, msg("assistant", "Tests pass.", "2026-10-14T01:00:09Z"), None),
        (21, 20, msg("system", "<system_info>refreshed</system_info>"), None),
        (22, 21, msg("user", "[Image 1: shot.png]\n\nwhat is this?", "2026-10-14T01:01:00Z",
                     images=[{"width": 1, "height": 1, "base64_data": PNG, "mime_type": "image/png", "source_path": "shot.png"},
                             {"width": 1, "height": 1, "base64_data": PNG, "mime_type": "image/bmp", "source_path": "old.bmp"}]), None),
        (23, 22, msg("assistant", "", "2026-10-14T01:01:03Z",
                     thinking={"thinking": "gemini thought", "signature": "Z2Vt", "signature_type": "gemini"},
                     tool_calls=[call("call_ok_1", "get_output", shell_id="abc123", timeout=180000)]), None),
        # quota ran out: no result for call_ok_1
    ]
    return {"cwd": cwd, "title": "Build the thing", "head": 23, "nodes": nodes}
