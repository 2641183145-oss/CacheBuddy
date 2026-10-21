"""Read Droid sessions into the neutral form.

A Droid session is `<sessions dir>/<project key>/<id>.jsonl` (see droid.py):
a session_start line, then `message` events (Anthropic-shaped role and
content blocks) with `agent_turn_outcome` and `compaction_state` events
between them. droid loads the messages in file order (N5 in droid 0.233.0),
so this reads them in file order too, every one of them: when droid has
compacted, the messages before the anchor are still in the file, and carrying
them is better than carrying droid's summary of them.

Left out, because they are droid's own and the target has its own:
- `llm_only` messages droid writes for itself (the `context-*` message with
  its tool catalog and environment, system reminders);
- `user_only` messages (UI notices such as "No active subscription found");
- a note session-bridge itself added when it carried a session in.
Kept as Summary: an `llm_only` summary session-bridge carried in.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from . import droid, ir
from .devin import _epoch

BRIDGE_NOTE = "This conversation was carried over from"
BRIDGE_SUMMARY = "Summary of the earlier part of this conversation, written by"
BRIDGE_OMITTED = "were left out here when this session was carried"


@dataclass
class Listing:
    id: str
    path: Path
    cwd: str
    title: Optional[str]
    model: Optional[str]
    organization_id: Optional[str]
    updated_at: float
    messages: int


def _start(path: Path) -> Optional[dict]:
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                if line.strip():
                    o = json.loads(line)
                    return o if o.get("type") == "session_start" else None
    except (OSError, ValueError):
        return None
    return None


def settings_path(path: Path) -> Path:
    return path.with_name(path.stem + ".settings.json")


def _settings(path: Path) -> dict:
    try:
        return json.loads(settings_path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _count(path: Path) -> int:
    n = 0
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            if '"type":"message"' in line or '"type": "message"' in line:
                n += 1
    return n


def list_sessions(root: Optional[Path] = None) -> list[Listing]:
    root = root or droid.sessions_dir()
    out = []
    for f in root.glob("*/*.jsonl"):
        start = _start(f)
        if not start:
            continue
        out.append(Listing(
            id=start.get("id") or f.stem, path=f, cwd=start.get("cwd") or "",
            title=start.get("title"), model=_settings(f).get("model"),
            organization_id=start.get("organizationId"),
            updated_at=f.stat().st_mtime, messages=_count(f),
        ))
    out.sort(key=lambda s: s.updated_at, reverse=True)
    return out


def _text_of(blocks: list) -> str:
    return "\n".join(b.get("text", "") for b in blocks if isinstance(b, dict) and b.get("type") == "text")


def _result_text(c) -> str:
    if c is None:
        return ""
    if isinstance(c, str):
        return c
    parts = []
    for b in c:
        if b.get("type") == "text":
            parts.append(b.get("text", ""))
        elif b.get("type") == "image":
            parts.append("[image]")
    return "\n".join(parts)


def _strip_reminder(text: str) -> str:
    t = text.strip()
    if t.startswith("<system-reminder>") and t.endswith("</system-reminder>"):
        t = t[len("<system-reminder>"):-len("</system-reminder>")].strip()
    return t


def to_turns(events: list[dict]) -> list[ir.Turn]:
    turns: list[ir.Turn] = []
    for e in events:
        if e.get("type") != "message":
            continue
        m = e.get("message") or {}
        role, vis = m.get("role"), m.get("visibility")
        at = _epoch(e.get("timestamp"))
        content = m.get("content")
        if isinstance(content, str):
            content = [{"type": "text", "text": content}]
        content = [b for b in (content or []) if isinstance(b, dict)]
        if vis == "user_only":
            continue
        if role == "user":
            results = [b for b in content if b.get("type") == "tool_result"]
            for b in results:
                turns.append(ir.ToolResult(b.get("tool_use_id") or "", _result_text(b.get("content")),
                                           bool(b.get("is_error")), at))
            rest = [b for b in content if b.get("type") != "tool_result"]
            if vis == "llm_only":
                text = _strip_reminder(_text_of(rest))
                if text.startswith(BRIDGE_SUMMARY):
                    turns.append(ir.Summary(text.split("\n\n", 1)[-1], at))
                continue  # droid's own context, or a note we added: not carried
            blocks: list[ir.Block] = []
            for b in rest:
                if b.get("type") == "text":
                    if b.get("text", "").strip() and not (results and b["text"].startswith("[Result of an earlier tool call")):
                        blocks.append(ir.Text(b["text"]))
                elif b.get("type") == "image":
                    src = b.get("source") or {}
                    if src.get("data"):
                        blocks.append(ir.Image(src.get("media_type") or "image/png", src["data"]))
                elif b.get("type") == "document":
                    src = b.get("source") or {}
                    name = src.get("name") or "document"
                    data = src.get("parsed_data") or (src.get("data") if src.get("media_type") == "text/plain" else "")
                    blocks.append(ir.Text(f"[Attached document {name}]\n{data}" if data else f"[Attached document {name}]"))
            if blocks:
                turns.append(ir.UserTurn(blocks, at))
        elif role == "assistant":
            blocks = []
            for b in content:
                t = b.get("type")
                if t == "thinking" and (b.get("thinking") or "").strip():
                    blocks.append(ir.Thinking(b["thinking"], b.get("signature") or "", b.get("signatureProvider")))
                elif t == "redacted_thinking":
                    continue  # encrypted; only the backend that wrote it can read it
                elif t == "text" and (b.get("text") or "").strip():
                    blocks.append(ir.Text(b["text"]))
                elif t == "tool_use":
                    blocks.append(ir.ToolCall(b.get("id") or "", b.get("name") or "", b.get("input") or {}))
            if blocks:
                turns.append(ir.AssistantTurn(blocks, m.get("modelId"), at))
    return turns


def read_session(path: Path) -> ir.Session:
    events = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            if line.strip():
                try:
                    events.append(json.loads(line))
                except ValueError:
                    continue  # droid skips unreadable lines too
    if not events or events[0].get("type") != "session_start":
        raise ValueError(f"not a Droid session file: {path}")
    start = events[0]
    times = [t for t in (_epoch(e.get("timestamp")) for e in events) if t is not None]
    return ir.Session(
        source_agent="droid", source_id=start.get("id") or path.stem,
        cwd=start.get("cwd") or "", title=start.get("title"), model=_settings(path).get("model"),
        created_at=min(times) if times else None, updated_at=max(times) if times else None,
        turns=to_turns(events[1:]),
    )


def pick(root: Optional[Path] = None, session_id: Optional[str] = None, cwd: Optional[str] = None) -> Path:
    """The session file to carry: the one named, else the latest in cwd, else the latest."""
    sessions = list_sessions(root)
    if session_id:
        for s in sessions:
            if s.id == session_id or s.path.stem == session_id:
                return s.path
        raise LookupError(f"no Droid session {session_id!r}")
    if cwd:
        want = os.path.normcase(os.path.abspath(cwd))
        sessions = [s for s in sessions if os.path.normcase(os.path.abspath(s.cwd)) == want]
    if not sessions:
        raise LookupError("no Droid session found" + (f" in {cwd}" if cwd else ""))
    return sessions[0].path
