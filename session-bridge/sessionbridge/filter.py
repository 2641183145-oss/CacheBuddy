"""The default trim: drop exploratory tool calls whose results nothing used.

Kept, always:
- every user message, every word the assistant said, all its thinking;
- every call that changed files (Edit/Create, edit/write) and its result;
- everything in the last TAIL turns (the state the next agent picks up from).

Dropped: a read-only call (read, grep, glob, ls, search, fetch) outside the
tail whose result shares no identifier, path or number with anything said or
called after it. Only the call and its result go; the assistant message that
made it keeps its text and thinking. Where calls were dropped, a Note tells
the model how many, so it doesn't mistake the gap for nothing having run.

Shell commands (exec/Execute) are kept: a command can change state even when
its output looks like a read.
"""

from __future__ import annotations

import re
from dataclasses import replace

from . import ir

TAIL = 20

READ_ONLY = {
    "Read", "Grep", "Glob", "LS", "WebSearch", "FetchUrl",
    "read", "grep", "find_file_by_name", "web_search", "webfetch",
}

_TOKEN = re.compile(r"[\w./\\:-]{4,}")
_COMMON = {"none", "true", "false", "null", "return", "self", "this", "from", "import", "with",
           "that", "have", "file", "line", "lines", "error", "output", "result", "true,", "def"}


def _tokens(text: str) -> set[str]:
    return {t.lower().strip(".:,") for t in _TOKEN.findall(text[:20000])} - _COMMON


def _said(t: ir.Turn) -> str:
    """What a turn says or asks for, as later evidence a result was used."""
    if isinstance(t, (ir.UserTurn, ir.AssistantTurn)):
        parts = []
        for b in t.blocks:
            if isinstance(b, (ir.Text, ir.Thinking)):
                parts.append(b.text)
            elif isinstance(b, ir.ToolCall):
                parts.append(" ".join(str(v) for v in b.arguments.values()))
        return " ".join(parts)
    if isinstance(t, ir.Summary):
        return t.text
    return ""


def apply(turns: list[ir.Turn], tail: int = TAIL) -> tuple[list[ir.Turn], int]:
    """(trimmed turns, number of calls dropped)."""
    n = len(turns)
    cut = n - tail
    if cut <= 0:
        return list(turns), 0

    # last index at which each token is said: a result is used when one of its
    # tokens is said after it
    last_said: dict[str, int] = {}
    for i, t in enumerate(turns):
        for tok in _tokens(_said(t)):
            last_said[tok] = i

    results = {t.call_id: (i, t) for i, t in enumerate(turns) if isinstance(t, ir.ToolResult)}
    drop: set[str] = set()
    for i, t in enumerate(turns[:cut]):
        if not isinstance(t, ir.AssistantTurn):
            continue
        for b in t.blocks:
            if not isinstance(b, ir.ToolCall) or b.name not in READ_ONLY:
                continue
            j, res = results.get(b.id, (None, None))
            if res is None or j >= cut:
                continue  # unanswered, or answered inside the tail: keep
            if not any(last_said.get(tok, -1) > j for tok in _tokens(res.output)):
                drop.add(b.id)

    out: list[ir.Turn] = []
    pending = 0  # dropped calls not yet noted
    for t in turns:
        if isinstance(t, ir.ToolResult):
            if t.call_id not in drop:
                out.append(t)
            continue
        # the note goes before the next non-result turn, so it never sits
        # between a call and its result
        if pending:
            out.append(_note(pending, t.created_at))
            pending = 0
        if isinstance(t, ir.AssistantTurn):
            gone = sum(1 for b in t.blocks if isinstance(b, ir.ToolCall) and b.id in drop)
            if gone:
                pending += gone
                kept = [b for b in t.blocks if not (isinstance(b, ir.ToolCall) and b.id in drop)]
                if not kept:
                    continue
                t = replace(t, blocks=kept)
        out.append(t)
    if pending:
        out.append(_note(pending, None))
    return out, len(drop)


def _note(n: int, at) -> ir.Note:
    return ir.Note(f"{n} read-only tool call(s) were left out here when this session was carried "
                   "over; nothing later used their results.", at)
