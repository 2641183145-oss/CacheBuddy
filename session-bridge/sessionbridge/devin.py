"""Read Devin CLI sessions into the neutral form.

Devin CLI keeps every session in one SQLite file, `<devin dir>/cli/sessions.db`
(`%APPDATA%\\devin` on Windows, `~/.config/devin` elsewhere, as magpie's
internal/agent/devin.go finds it). The `sessions` row has the working
directory, model, title and `main_chain_id`: the node the conversation is at
now. `message_nodes` is a forest per session: each node one chat message
(OpenAI-style JSON: role, content, tool_calls, tool_call_id, thinking) with
its parent. Devin rebuilds its prefix (system prompt, rules, skills) as new
roots and copies older nodes beneath them, so the conversation the model saw
last is the path from `main_chain_id` back to its root, and only that path.

The database is opened read-only; Devin may be running and writing to it.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator, Optional

from . import ir


def devin_dir() -> Path:
    if sys.platform == "win32" and os.environ.get("APPDATA"):
        return Path(os.environ["APPDATA"]) / "devin"
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config")
    return Path(base) / "devin"


def default_db() -> Path:
    return devin_dir() / "cli" / "sessions.db"


@contextmanager
def connect(db: Path) -> Iterator[sqlite3.Connection]:
    """The database, read-only, closed on exit (sqlite3's own `with` only
    commits, which on Windows leaves the file locked)."""
    if not db.exists():
        raise FileNotFoundError(f"Devin sessions database not found: {db}")
    # mode=ro reads alongside a running Devin; WAL readers see the last commit.
    uri = "file:" + db.resolve().as_posix() + "?mode=ro"
    con = sqlite3.connect(uri, uri=True)
    try:
        yield con
    finally:
        con.close()


@dataclass
class Listing:
    id: str
    cwd: str
    title: Optional[str]
    model: str
    created_at: float
    updated_at: float
    nodes: int


def _epoch(value: Any) -> Optional[float]:
    """Devin's times: ISO strings in messages, epoch seconds in rows."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def list_sessions(con: sqlite3.Connection) -> list[Listing]:
    rows = con.execute(
        """select s.id, s.working_directory, s.title, s.model, s.created_at,
                  s.last_activity_at,
                  (select count(*) from message_nodes n where n.session_id = s.id)
           from sessions s where coalesce(s.hidden, 0) = 0
           order by s.last_activity_at desc"""
    ).fetchall()
    return [Listing(r[0], r[1], r[2], r[3], float(r[4]), float(r[5]), r[6]) for r in rows]


def main_chain(con: sqlite3.Connection, session_id: str) -> list[tuple[dict, Optional[dict]]]:
    """The messages on the path from the root to main_chain_id, in order,
    each with its node metadata."""
    row = con.execute("select main_chain_id from sessions where id = ?", (session_id,)).fetchone()
    if row is None:
        raise KeyError(f"no Devin session {session_id!r}")
    head = row[0]
    nodes = {
        nid: (pid, msg, meta)
        for nid, pid, msg, meta in con.execute(
            "select node_id, parent_node_id, chat_message, metadata from message_nodes where session_id = ?",
            (session_id,),
        )
    }
    path = []
    seen = set()
    cur = head
    while cur is not None:
        if cur in seen or cur not in nodes:
            raise ValueError(f"Devin session {session_id}: broken chain at node {cur}")
        seen.add(cur)
        pid, msg, meta = nodes[cur]
        path.append((json.loads(msg), json.loads(meta) if meta else None))
        cur = pid
    path.reverse()
    return path


def _extensions(msg: dict) -> dict:
    return ((msg.get("metadata") or {}).get("extensions")) or {}


def _is_summary(meta: Optional[dict]) -> bool:
    return bool(meta) and meta.get("summarized_from") is not None


def _user_blocks(msg: dict) -> list[ir.Block]:
    blocks: list[ir.Block] = []
    text = msg.get("content") or ""
    if text.strip():
        blocks.append(ir.Text(text))
    for im in msg.get("images") or []:
        data = im.get("base64_data")
        if data:
            blocks.append(ir.Image(im.get("mime_type") or "image/png", data, im.get("source_path")))
    return blocks


def _assistant_blocks(msg: dict) -> list[ir.Block]:
    blocks: list[ir.Block] = []
    th = msg.get("thinking")
    if th and (th.get("thinking") or "").strip():
        blocks.append(ir.Thinking(th["thinking"], th.get("signature") or "", th.get("signature_type")))
    text = msg.get("content") or ""
    if text.strip():
        blocks.append(ir.Text(text))
    for tc in msg.get("tool_calls") or []:
        args = tc.get("arguments")
        if isinstance(args, str):  # OpenAI wire form, in case a backend stores it so
            try:
                args = json.loads(args)
            except ValueError:
                args = {"_raw": args}
        blocks.append(ir.ToolCall(tc["id"], tc["name"], args or {}))
    return blocks


def _tool_failed(msg: dict) -> bool:
    ext = _extensions(msg)
    meta = ext.get("chisel/tool_result_meta") or {}
    return meta.get("success") is False or "chisel/tool_failure" in ext


def to_turns(chain: list[tuple[dict, Optional[dict]]]) -> Iterator[ir.Turn]:
    for msg, meta in chain:
        role = msg.get("role")
        at = _epoch((msg.get("metadata") or {}).get("created_at"))
        if _is_summary(meta):
            # A compaction summary stands for turns no longer on the chain;
            # it is conversation, not Devin's own prompt, so it is kept.
            text = msg.get("content") or ""
            if text.strip():
                yield ir.Summary(text, at)
            continue
        if role == "system":
            # Devin's prompt, rules, skills, environment: the target has its own.
            # A summary session-bridge carried in is conversation, so it is kept;
            # a note it added is not.
            text = msg.get("content") or ""
            if text.startswith("Summary of the earlier part of this conversation, written by"):
                yield ir.Summary(text.split("\n\n", 1)[-1], at)
            continue
        if role == "user":
            blocks = _user_blocks(msg)
            if blocks:
                yield ir.UserTurn(blocks, at)
        elif role == "assistant":
            blocks = _assistant_blocks(msg)
            if blocks:
                yield ir.AssistantTurn(blocks, (msg.get("metadata") or {}).get("generation_model"), at)
        elif role == "tool":
            yield ir.ToolResult(msg.get("tool_call_id") or "", msg.get("content") or "", _tool_failed(msg), at)


def read_session(con: sqlite3.Connection, session_id: str) -> ir.Session:
    row = con.execute(
        "select id, working_directory, title, model, created_at, last_activity_at from sessions where id = ?",
        (session_id,),
    ).fetchone()
    if row is None:
        raise KeyError(f"no Devin session {session_id!r}")
    return ir.Session(
        source_agent="devin",
        source_id=row[0],
        cwd=row[1],
        title=row[2],
        model=row[3],
        created_at=_epoch(row[4]),
        updated_at=_epoch(row[5]),
        turns=list(to_turns(main_chain(con, session_id))),
    )


def pick(con: sqlite3.Connection, session_id: Optional[str] = None, cwd: Optional[str] = None) -> str:
    """The session to carry: the one named, else the latest in cwd, else the latest."""
    if session_id:
        return session_id
    sessions = list_sessions(con)
    if cwd:
        want = os.path.normcase(os.path.abspath(cwd))
        sessions = [s for s in sessions if os.path.normcase(os.path.abspath(s.cwd)) == want]
    if not sessions:
        raise LookupError("no Devin session found" + (f" in {cwd}" if cwd else ""))
    return sessions[0].id
