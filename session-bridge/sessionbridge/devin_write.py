"""Write a neutral session into a Devin CLI sessions.db.

Shapes copied from what Devin CLI 3000.11.3 itself writes (schema migration
17, `subagent_heads`):

  sessions       id, working_directory, backend_type ("windsurf"), model,
                 agent_mode ("normal"), created_at / last_activity_at (epoch
                 seconds), title, main_chain_id (the head node), metadata
  message_nodes  session_id, node_id, parent_node_id, chat_message (JSON),
                 created_at (epoch seconds), metadata

chat_message is OpenAI-shaped: {message_id, role, content (a string),
metadata{created_at ISO, is_user_input, extensions, telemetry}}; assistant
messages add tool_calls [{id, name, arguments (object), index, kind}] and
thinking {thinking, signature, signature_type}; tool results are their own
role "tool" message with tool_call_id; images ride on the user message as
images [{width, height, base64_data, mime_type, source_path}].

The written chain is linear: node 0 is the root, each node's parent is the
one before, main_chain_id is the last. Devin's own prefix (system prompt,
rules, skills) is not written; Devin builds it when it resumes.

Devin must not be writing the database at the same moment: we take SQLite's
write lock for one short transaction, which is safe alongside a running
Devin, but a session it has open won't see ours until it reloads.
"""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from . import devin, ir

# Devin's signature_type for each signer droid names
SIGNERS = {"anthropic": "anthropic", "google": "gemini", "gemini": "gemini"}

# how a carried-in summary starts; devin.py reads it back as a Summary
SUMMARY_MARK = "Summary of the earlier part of this conversation, written by"


def _sec(t: Optional[float]) -> int:
    return int(t if t is not None else time.time())


def _iso(t: Optional[float]) -> str:
    dt = datetime.fromtimestamp(t if t is not None else time.time(), timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"


def _meta(at: Optional[float], source: str, operation: str = "unknown", **extra) -> dict:
    m = {"num_tokens": None, "is_user_input": None, "request_id": None, "metrics": None,
         "finish_reason": None, "created_at": _iso(at), "telemetry": {"source": source, "operation": operation}}
    m.update(extra)
    return m


class _Writer:
    def __init__(self, s: ir.Session, thinking: str):
        self.s = s
        self.thinking_mode = thinking
        self.nodes: list[tuple[dict, Optional[float]]] = []
        self.last_t: Optional[float] = None

    def _t(self, at: Optional[float]) -> Optional[float]:
        t = at if at is not None else (self.last_t if self.last_t is not None else self.s.created_at)
        if t is not None and self.last_t is not None and t < self.last_t:
            t = self.last_t
        self.last_t = t
        return t

    def add(self, msg: dict, at: Optional[float]) -> None:
        msg["message_id"] = str(uuid.uuid4())
        self.nodes.append((msg, at))

    def user(self, t: ir.UserTurn) -> None:
        at = self._t(t.created_at)
        text = "\n\n".join(b.text for b in t.blocks if isinstance(b, ir.Text))
        m: dict = {"role": "user", "content": text,
                   "metadata": _meta(at, "user", is_user_input=True,
                                     extensions={"chisel/client-message-id": str(uuid.uuid4())})}
        images = [{"width": 0, "height": 0, "base64_data": b.data, "mime_type": b.media_type,
                   **({"source_path": b.source_path} if b.source_path else {})}
                  for b in t.blocks if isinstance(b, ir.Image)]
        if images:
            m["images"] = images
        self.add(m, at)

    def _thinking(self, blocks: list[ir.Thinking]) -> Optional[dict]:
        if self.thinking_mode == "drop" or not blocks:
            return None
        # Devin keeps one thinking per message; several are joined in order.
        text = "\n\n".join(b.text for b in blocks)
        first = blocks[0]
        signer = SIGNERS.get(first.signature_type or "")
        if self.thinking_mode == "keep" and len(blocks) == 1 and first.signature and signer:
            return {"thinking": text, "signature": first.signature, "signature_type": signer}
        # Unsigned, as Devin itself stores thinking from models that don't sign.
        return {"thinking": text, "signature": ""}

    def assistant(self, t: ir.AssistantTurn) -> None:
        at = self._t(t.created_at)
        text = "\n\n".join(b.text for b in t.blocks if isinstance(b, ir.Text))
        calls = [b for b in t.blocks if isinstance(b, ir.ToolCall)]
        m: dict = {"role": "assistant", "content": text,
                   "tool_calls": [{"id": c.id, "name": c.name, "arguments": c.arguments, "index": i, "kind": "function"}
                                  for i, c in enumerate(calls)]}
        th = self._thinking([b for b in t.blocks if isinstance(b, ir.Thinking)])
        if th:
            m["thinking"] = th
        m["metadata"] = _meta(at, "assistant", "inference", finish_reason="tool_calls" if calls else "stop",
                              **({"generation_model": t.model} if t.model else {}))
        self.add(m, at)

    def tool_result(self, t: ir.ToolResult) -> None:
        at = self._t(t.created_at)
        meta = {"success": True, "kind": "other"} if not t.is_error else {"success": False, "failure_reason": "ToolError", "kind": "other"}
        m = {"role": "tool", "content": t.output, "tool_call_id": t.call_id,
             "metadata": _meta(at, "tool_result", extensions={"chisel/tool_result_meta": meta})}
        self.add(m, at)

    # Summaries and notes are for the model, not the user: Devin keeps such
    # context as role "system" messages in the chain (its <system_info>
    # refreshes), which the model reads and the UI doesn't show.
    def summary(self, t: ir.Summary) -> None:
        at = self._t(t.created_at)
        text = f"{SUMMARY_MARK} {self.s.source_agent} when it compacted it:\n\n{t.text}"
        self.add({"role": "system", "content": text, "metadata": _meta(at, "system")}, at)

    def bridge_note(self, text: str, at: Optional[float]) -> None:
        at = self._t(at)
        self.add({"role": "system", "content": text, "metadata": _meta(at, "system")}, at)


def _sid(con: sqlite3.Connection) -> str:
    while True:
        sid = "bridge-" + uuid.uuid4().hex[:12]
        if con.execute("select 1 from sessions where id = ?", (sid,)).fetchone() is None:
            return sid


def write(s: ir.Session, db: Path, model: Optional[str] = None, thinking: str = "downgrade",
          note: Optional[str] = None) -> str:
    """Write s into db as a new Devin session; returns its id. db must be an
    existing Devin sessions.db (we never create Devin's schema ourselves)."""
    if not db.exists():
        raise FileNotFoundError(f"Devin sessions database not found: {db}")
    w = _Writer(s, thinking)
    for turn in s.turns:
        if isinstance(turn, ir.UserTurn):
            w.user(turn)
        elif isinstance(turn, ir.AssistantTurn):
            w.assistant(turn)
        elif isinstance(turn, ir.ToolResult):
            w.tool_result(turn)
        elif isinstance(turn, ir.Summary):
            w.summary(turn)
        elif isinstance(turn, ir.Note):
            w.bridge_note(turn.text, turn.created_at)
    if note:
        w.bridge_note(note, s.updated_at)
    if not w.nodes:
        raise ValueError("nothing to write: the session has no messages")
    con = sqlite3.connect(str(db), timeout=10)
    try:
        cols = {r[1] for r in con.execute("pragma table_info(sessions)")}
        if not {"id", "working_directory", "main_chain_id"} <= cols:
            raise ValueError(f"{db} is not a Devin CLI sessions database")
        with con:  # one transaction
            sid = _sid(con)
            title = s.title or "Untitled"
            con.execute(
                "insert into sessions (id, working_directory, backend_type, model, agent_mode, created_at,"
                " last_activity_at, title, main_chain_id, metadata) values (?,?,?,?,?,?,?,?,?,?)",
                (sid, s.cwd, "windsurf", model or "", "normal", _sec(s.created_at), _sec(w.last_t),
                 f"[{s.source_agent}] {title}"[:200], len(w.nodes) - 1,
                 json.dumps({"total_credit_cost": 0, "total_acu_cost": 0.0,
                             "client_meta": {"session-bridge/source": f"{s.source_agent}:{s.source_id}"}})),
            )
            for nid, (msg, at) in enumerate(w.nodes):
                con.execute(
                    "insert into message_nodes (session_id, node_id, parent_node_id, chat_message, created_at, metadata)"
                    " values (?,?,?,?,?,?)",
                    (sid, nid, nid - 1 if nid else None, json.dumps(msg, ensure_ascii=False), _sec(at), None),
                )
    finally:
        con.close()
    return sid


def default_db() -> Path:
    return devin.default_db()
