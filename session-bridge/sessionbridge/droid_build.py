"""Turn a neutral session into the events of a Droid session file.

A Droid session is JSON lines: a session_start, then one `message` event per
message (Anthropic-shaped role and content blocks, chained by parentId and
numbered by seq), with an `agent_turn_outcome` after each finished turn.
droid loads the messages in file order and sends them as the history.

What has to change on the way:
- Tool calls are renamed to Droid's tools where one does the same job
  (tools.py), and their ids rewritten to the [A-Za-z0-9_-] ids the model APIs
  require (Devin's look like `functions.exec:5#7b47...`).
- Each tool's results go together in one user message right after the call,
  as the APIs require; a call whose result never came (Devin stopped mid-
  call) gets an error result saying so, rather than leaving the history
  unsendable.
- Thinking keeps its text. Its signature only verifies against the account
  and backend that made it, so by default it is marked unknown, and droid
  sends it as text rather than as a signed block the API would reject.
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Optional

from . import droid_check, ir, tools

INTERRUPTED = "Tool execution was interrupted"  # droid's own words for it

# Devin's signature_type -> droid's signatureProvider
_SIGNERS = {"anthropic": "anthropic", "gemini": "google"}

Mapper = Callable[[str, dict, str], tuple[str, dict, bool]]
MAPPERS: dict[str, Mapper] = {"devin": tools.devin_to_droid}


@dataclass
class Options:
    owner: str
    # The signed-in account's organization. droid fills a missing one in on
    # the first message by rewriting the file (tmp + rename), which fails with
    # EPERM on Windows while droid has it open and rolls that message back.
    organization_id: str
    host_id: Optional[str] = None
    model: Optional[str] = None
    reasoning_effort: Optional[str] = None
    # "downgrade": keep the text, let droid send it as text (default)
    # "keep": keep signatures as signed, for a target on the same backend
    # "drop": leave thinking out
    thinking: str = "downgrade"
    note: bool = True  # tell the model it moved, and what its tools are now
    session_id: Optional[str] = None


@dataclass
class Report:
    session_id: str = ""
    messages: int = 0
    user_messages: int = 0
    assistant_messages: int = 0
    tool_calls: int = 0
    mapped: dict[str, str] = field(default_factory=dict)  # source name -> droid name
    unmapped: dict[str, int] = field(default_factory=dict)
    interrupted: int = 0  # calls given an "interrupted" result
    orphan_results: int = 0  # results whose call was not on the chain
    images: int = 0
    images_as_text: int = 0
    thinking: int = 0
    summaries: int = 0
    notes: int = 0
    chars: int = 0


def _iso(t: Optional[float]) -> str:
    dt = datetime.fromtimestamp(t, timezone.utc) if t is not None else datetime.now(timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def _tool_id(source: str, used: set[str]) -> str:
    if droid_check.ID.match(source) and source not in used:
        return source
    digest = hashlib.sha256(source.encode()).hexdigest()[:24]
    out = f"toolu_br_{digest}"
    n = 1
    while out in used:
        out = f"toolu_br_{digest}_{n}"
        n += 1
    return out


class _Builder:
    def __init__(self, s: ir.Session, opts: Options):
        self.s, self.opts = s, opts
        self.map = MAPPERS.get(s.source_agent, lambda n, a, cwd: (n, dict(a), False))
        self.events: list[dict] = []
        self.prev: Optional[str] = None
        self.seq = 0
        self.last_t: Optional[float] = None  # keeps times from going backwards
        self.used: set[str] = set()
        self.ids: dict[str, str] = {}  # source call id -> droid id, latest use
        self.pending: list[str] = []  # droid ids awaiting results, in call order
        self.results: dict[str, dict] = {}
        self.orphans: list[dict] = []
        self.turn: Optional[str] = None  # the user message that started the turn
        self.clean = False  # the turn's last message is an answer, not a call
        self.r = Report()

    def message(self, role: str, content: list[dict], at: Optional[float], **extra) -> str:
        t = at if at is not None else (self.last_t if self.last_t is not None else self.s.created_at)
        if t is not None and self.last_t is not None and t < self.last_t:
            t = self.last_t
        self.last_t = t
        mid = str(uuid.uuid4())
        e: dict = {"type": "message", "id": mid}
        if self.prev:
            e["parentId"] = self.prev
        e["timestamp"] = _iso(t)
        e["message"] = {"role": role, "content": content, **extra}
        e["seq"] = self.seq
        self.events.append(e)
        self.prev, self.seq = mid, self.seq + 1
        self.r.messages += 1
        if role == "user":
            self.r.user_messages += 1
        else:
            self.r.assistant_messages += 1
        for b in content:
            self.r.chars += len(b.get("text") or b.get("thinking") or "") + len(str(b.get("input") or "")) + len(str(b.get("content") or ""))
        return mid

    def flush(self, at: Optional[float]) -> None:
        """Answer the last assistant message's calls, in call order."""
        if not self.pending:
            return
        blocks = []
        for tid in self.pending:
            res = self.results.get(tid)
            if res is None:
                res = {"type": "tool_result", "tool_use_id": tid, "is_error": True, "content": INTERRUPTED}
                self.r.interrupted += 1
            blocks.append(res)
        blocks += self.orphans
        self.pending, self.results, self.orphans = [], {}, []
        self.message("user", blocks, at)
        self.clean = False

    def close_turn(self) -> None:
        if self.turn and self.clean:
            self.events.append({"type": "agent_turn_outcome", "turnId": self.turn, "reason": "completed", "resultKind": "text"})
        self.turn, self.clean = None, False

    def user(self, t: ir.UserTurn) -> None:
        self.flush(t.created_at)
        self.close_turn()
        content = []
        for b in t.blocks:
            if isinstance(b, ir.Text):
                content.append({"type": "text", "text": b.text})
            elif isinstance(b, ir.Image):
                if b.media_type in droid_check.MEDIA:
                    content.append({"type": "image", "source": {"type": "base64", "data": b.data, "media_type": b.media_type}})
                    self.r.images += 1
                else:
                    content.append({"type": "text", "text": f"[Image {b.source_path or ''} ({b.media_type}) not carried: droid takes jpeg, png, gif, webp]"})
                    self.r.images_as_text += 1
        if content:
            self.turn = self.message("user", content, t.created_at)
            self.clean = False

    def thinking(self, b: ir.Thinking) -> Optional[dict]:
        mode = self.opts.thinking
        if mode == "drop":
            return None
        self.r.thinking += 1
        if mode == "keep" and b.signature and b.signature_type in _SIGNERS:
            return {"type": "thinking", "thinking": b.text, "signature": b.signature, "signatureProvider": _SIGNERS[b.signature_type]}
        # Unsigned. droid's history prep (`rg` in 0.233.0) sends a thinking block
        # whose signature doesn't verify as text wrapped in <thinking>...</thinking>,
        # so the model still reads it as reasoning; a provider that accepts
        # unsigned thinking gets it as thinking. On Google routes droid drops it.
        return {"type": "thinking", "thinking": b.text, "signature": "", "signatureProvider": "unknown"}

    def assistant(self, t: ir.AssistantTurn) -> None:
        self.flush(t.created_at)
        content = []
        for b in t.blocks:
            if isinstance(b, ir.Thinking):
                blk = self.thinking(b)
                if blk:
                    content.append(blk)
            elif isinstance(b, ir.Text):
                content.append({"type": "text", "text": b.text})
            elif isinstance(b, ir.ToolCall):
                name, args, ok = self.map(b.name, b.arguments, self.s.cwd)
                if ok:
                    self.r.mapped[b.name] = name
                else:
                    self.r.unmapped[b.name] = self.r.unmapped.get(b.name, 0) + 1
                tid = _tool_id(b.id, self.used)
                self.used.add(tid)
                self.ids[b.id] = tid
                self.pending.append(tid)
                self.r.tool_calls += 1
                content.append({"type": "tool_use", "id": tid, "name": name, "input": args})
        if content:
            self.message("assistant", content, t.created_at)
            self.clean = not self.pending
    def tool_result(self, t: ir.ToolResult) -> None:
        tid = self.ids.get(t.call_id)
        if tid is None or tid not in self.pending:
            # its call isn't one being answered: keep what it said, as text
            # after the results (the APIs want tool_result blocks first)
            self.r.orphan_results += 1
            block = {"type": "text", "text": f"[Result of an earlier tool call {t.call_id}]\n{t.output}"}
            if self.pending:
                self.orphans.append(block)
            else:
                self.message("user", [block], t.created_at)
                self.clean = False
            return
        self.results[tid] = {"type": "tool_result", "tool_use_id": tid, "is_error": t.is_error, "content": t.output or "(no output)"}

    def summary(self, t: ir.Summary) -> None:
        self.flush(t.created_at)
        self.r.summaries += 1
        text = f"<system-reminder>\nSummary of the earlier part of this conversation, written by {self.s.source_agent} when it compacted it:\n\n{t.text}\n</system-reminder>"
        self.message("user", [{"type": "text", "text": text}], t.created_at, visibility="llm_only")

    def bridge_note(self, t: ir.Note) -> None:
        self.flush(t.created_at)
        self.r.notes += 1
        text = f"<system-reminder>\n{t.text}\n</system-reminder>"
        self.message("user", [{"type": "text", "text": text}], t.created_at, visibility="llm_only")
        self.r.messages -= 1
        self.r.user_messages -= 1

    def note(self) -> str:
        mapped = ", ".join(f"{a} -> {b}" for a, b in sorted(self.r.mapped.items()))
        lines = [
            "<system-reminder>",
            f"This conversation was carried over from {self.s.source_agent} (session {self.s.source_id})"
            + (f", where it ran on {self.s.model}" if self.s.model else "")
            + ". You are now running in Droid and continue it from here, in the same working directory: "
            + self.s.cwd + ".",
        ]
        if mapped:
            lines.append(f"Its tool calls above are shown in Droid's tools: {mapped}.")
        if self.r.unmapped:
            names = ", ".join(sorted(self.r.unmapped))
            lines.append(f"Calls to {names} were {self.s.source_agent}-only tools that Droid does not have; do not call them. Any background process they referred to is not running here; start it again with Execute if it is still needed.")
        if self.r.interrupted:
            lines.append(f"{self.r.interrupted} tool call(s) stopped before returning; their results say so. Check the state they would have changed before relying on it.")
        lines.append("</system-reminder>")
        return "\n".join(lines)

    def finish(self) -> None:
        self.flush(self.s.updated_at)
        self.close_turn()
        if self.opts.note and self.r.messages:
            self.message("user", [{"type": "text", "text": self.note()}], self.s.updated_at, visibility="llm_only")
            self.r.messages -= 1  # the note isn't part of the carried conversation
            self.r.user_messages -= 1


def _canonical_cwd(cwd: str) -> str:
    from .droid import canonical_cwd  # droid.py imports nothing from here; keep it that way at import time

    return canonical_cwd(cwd)


def _title(s: ir.Session) -> str:
    t = " ".join((s.title or "").split()) or "Untitled"
    if len(t) > 80:
        t = t[:79] + "…"
    return f"[{s.source_agent}] {t}"


def build(s: ir.Session, opts: Options) -> tuple[list[dict], dict, Report]:
    """(events, settings, report). Raises ValueError when the result would not
    be a session droid and the model APIs accept."""
    b = _Builder(s, opts)
    for t in s.turns:
        if isinstance(t, ir.UserTurn):
            b.user(t)
        elif isinstance(t, ir.AssistantTurn):
            b.assistant(t)
        elif isinstance(t, ir.ToolResult):
            b.tool_result(t)
        elif isinstance(t, ir.Summary):
            b.summary(t)
        elif isinstance(t, ir.Note):
            b.bridge_note(t)
    if not opts.organization_id:
        raise ValueError("no Droid organization id: a session without one loses its first message on Windows")
    b.finish()
    sid = opts.session_id or str(uuid.uuid4())
    start: dict = {
        "type": "session_start",
        "id": sid,
        "title": _title(s),
        "owner": opts.owner,
        "organizationId": opts.organization_id,
        "version": 2,
        # the form droid's folder key and ours agree on (see droid.canonical_cwd)
        "cwd": _canonical_cwd(s.cwd),
    }
    if opts.host_id:
        start["hostId"] = opts.host_id
    start["isSessionTitleManuallySet"] = True
    # session_start passes unknown keys through; marks the session as ours so
    # droid.identity() never takes it as evidence of the signed-in account
    start["sessionBridgeSource"] = f"{s.source_agent}:{s.source_id}"
    events = [start] + b.events
    errs = droid_check.check(events)
    if errs:
        raise ValueError("converted session would not load cleanly:\n  " + "\n  ".join(errs[:20]))
    settings: dict = {}
    if opts.model:
        settings["model"] = opts.model
    if opts.reasoning_effort:
        settings["reasoningEffort"] = opts.reasoning_effort
    b.r.session_id = sid
    return events, settings, b.r
