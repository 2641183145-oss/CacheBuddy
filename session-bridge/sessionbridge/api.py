"""The functions a host app calls. Everything returns plain dicts (JSON-ready),
so the same calls back the CLI's `--json` output and any embedding.

    list_sessions(agent)                    -> [session, ...]
    preview(source, session_id, target)     -> plan (what carrying would do)
    carry(source, session_id, target, ...)  -> result (plan + where it was written, how to resume)
    droid_accounts()                        -> sessions per organization
    droid_release(...) / droid_restore(...) -> changes
    droid_copy(session_id, to_org)          -> change (the new session)

`source`/`target` are "devin" or "droid". Paths default to each agent's own
(override with devin_db= / droid_sessions=). Nothing here sends anything over
the network.
"""

from __future__ import annotations

import dataclasses
import os
from pathlib import Path
from typing import Optional

from . import devin, devin_write, droid, droid_build, droid_read, droid_reown, filter as filt, ir, tools

AGENTS = ("devin", "droid")


def _devin_db(p) -> Path:
    return Path(p) if p else devin.default_db()


def _droid_root(p) -> Optional[Path]:
    return Path(p) if p else None


def list_sessions(agent: str, devin_db=None, droid_sessions=None) -> list[dict]:
    if agent == "devin":
        with devin.connect(_devin_db(devin_db)) as con:
            return [{"agent": "devin", "id": s.id, "title": s.title, "cwd": s.cwd, "model": s.model,
                     "updated_at": s.updated_at, "size": s.nodes} for s in devin.list_sessions(con)]
    if agent == "droid":
        return [{"agent": "droid", "id": s.id, "title": s.title, "cwd": s.cwd, "model": s.model,
                 "updated_at": s.updated_at, "size": s.messages, "organization_id": s.organization_id}
                for s in droid_read.list_sessions(_droid_root(droid_sessions))]
    raise ValueError(f"unknown agent {agent!r}")


def read(source: str, session_id: Optional[str] = None, cwd: Optional[str] = None,
         devin_db=None, droid_sessions=None) -> ir.Session:
    """The session to carry: by id, else the latest in cwd, else the latest."""
    if source == "devin":
        with devin.connect(_devin_db(devin_db)) as con:
            return devin.read_session(con, devin.pick(con, session_id, cwd))
    if source == "droid":
        return droid_read.read_session(droid_read.pick(_droid_root(droid_sessions), session_id, cwd))
    raise ValueError(f"unknown agent {source!r}")


def _tokens(turns: list[ir.Turn]) -> int:
    chars = 0
    for t in turns:
        if isinstance(t, (ir.UserTurn, ir.AssistantTurn)):
            for b in t.blocks:
                if isinstance(b, (ir.Text, ir.Thinking)):
                    chars += len(b.text)
                elif isinstance(b, ir.ToolCall):
                    chars += len(str(b.arguments))
                elif isinstance(b, ir.Image):
                    chars += 6000  # ~1.5k tokens a picture
        elif isinstance(t, (ir.ToolResult, ir.Summary, ir.Note)):
            chars += len(getattr(t, "output", None) or getattr(t, "text", ""))
    return chars // 4


def _counts(turns: list[ir.Turn]) -> dict:
    calls = [b for t in turns if isinstance(t, ir.AssistantTurn) for b in t.blocks if isinstance(b, ir.ToolCall)]
    return {
        "user_messages": sum(isinstance(t, ir.UserTurn) for t in turns),
        "assistant_messages": sum(isinstance(t, ir.AssistantTurn) for t in turns),
        "tool_calls": len(calls),
        "thinking": sum(isinstance(b, ir.Thinking) for t in turns if isinstance(t, ir.AssistantTurn) for b in t.blocks),
        "images": sum(isinstance(b, ir.Image) for t in turns if isinstance(t, ir.UserTurn) for b in t.blocks),
        "tokens": _tokens(turns),
    }


def _map_for_devin(s: ir.Session) -> tuple[ir.Session, dict, dict]:
    """Droid tool calls renamed to Devin's, ids kept (Devin takes any)."""
    mapped, unmapped, turns = {}, {}, []
    for t in s.turns:
        if isinstance(t, ir.AssistantTurn):
            blocks = []
            for b in t.blocks:
                if isinstance(b, ir.ToolCall):
                    name, args, ok = tools.droid_to_devin(b.name, b.arguments, s.cwd)
                    if ok:
                        mapped[b.name] = name
                    else:
                        unmapped[b.name] = unmapped.get(b.name, 0) + 1
                    b = ir.ToolCall(b.id, name, args)
                blocks.append(b)
            t = dataclasses.replace(t, blocks=blocks)
        turns.append(t)
    return dataclasses.replace(s, turns=turns), mapped, unmapped


def _devin_note(s: ir.Session, mapped: dict, unmapped: dict) -> str:
    lines = [f"This conversation was carried over from {s.source_agent} (session {s.source_id}). "
             f"You are now running in Devin and continue it from here, in the same working directory: {s.cwd}."]
    if mapped:
        lines.append("Its tool calls above are shown in Devin's tools: " + ", ".join(f"{a} -> {b}" for a, b in sorted(mapped.items())) + ".")
    if unmapped:
        lines.append(f"Calls to {', '.join(sorted(unmapped))} were {s.source_agent}-only tools that Devin does not have; do not call them.")
    lines.append("Thinking shown in earlier turns was the previous model's private reasoning, not something it told the user.")
    return "\n".join(lines)


def _plan(source: str, target: str, s: ir.Session, trim: bool) -> tuple[ir.Session, dict]:
    if source not in AGENTS or target not in AGENTS:
        raise ValueError("source and target are 'devin' or 'droid'")
    if source == target:
        raise ValueError("source and target are the same agent; for a Droid account switch use droid_release")
    before = _counts(s.turns)
    turns, dropped = filt.apply(s.turns) if trim else (list(s.turns), 0)
    trimmed = dataclasses.replace(s, turns=turns)
    plan = {
        "source": {"agent": source, "id": s.source_id, "title": s.title, "cwd": s.cwd, "model": s.model},
        "target": target,
        "before": before,
        "after": _counts(turns),
        "trimmed_calls": dropped,
        "warnings": [],
    }
    if plan["after"]["tokens"] > 100_000:
        plan["warnings"].append({"kind": "large", "message": f"about {plan['after']['tokens']:,} tokens of history; "
                                 f"{target} will likely compact it on the first turn"})
    if not os.path.isdir(s.cwd):
        plan["warnings"].append({"kind": "cwd_missing", "message": f"the working directory {s.cwd} does not exist here"})
    return trimmed, plan


def preview(source: str, session_id: Optional[str] = None, target: Optional[str] = None, *,
            cwd: Optional[str] = None, trim: bool = True, thinking: str = "downgrade",
            devin_db=None, droid_sessions=None) -> dict:
    return carry(source, session_id, target, cwd=cwd, trim=trim, thinking=thinking,
                 devin_db=devin_db, droid_sessions=droid_sessions, dry_run=True)


class NoDroidAccount(LookupError):
    """No Droid session of droid's own to learn the account's organization from."""


def _droid_org(given: Optional[str], me: "droid.Identity") -> str:
    if given:
        return given
    if me.organization_id:
        return me.organization_id
    raise NoDroidAccount(
        "Can't tell which Droid account is signed in: there is no Droid session of its own on this "
        "machine to learn the organization from. Open Droid, send one message in a new session, then retry "
        "(or pass organization_id).")


def carry(source: str, session_id: Optional[str] = None, target: Optional[str] = None, *,
          cwd: Optional[str] = None, trim: bool = True, thinking: str = "downgrade",
          model: Optional[str] = None, reasoning_effort: Optional[str] = None, note: bool = True,
          organization_id: Optional[str] = None,
          devin_db=None, droid_sessions=None, dry_run: bool = False) -> dict:
    """Carry a session into the other agent as a new session there. The
    source session is only read."""
    target = target or ("droid" if source == "devin" else "devin")
    s = read(source, session_id, cwd, devin_db, droid_sessions)
    s, plan = _plan(source, target, s, trim)
    plan["dry_run"] = dry_run
    if target == "droid":
        root = _droid_root(droid_sessions)
        me = droid.identity(root)
        org = _droid_org(organization_id, me)
        events, settings, r = droid_build.build(s, droid_build.Options(
            owner=me.owner, organization_id=org, host_id=me.host_id, model=model,
            reasoning_effort=reasoning_effort, thinking=thinking, note=note))
        plan["droid_organization_id"] = org
        plan["tools"] = {"mapped": r.mapped, "unmapped": r.unmapped}
        plan["interrupted_calls"] = r.interrupted
        if not dry_run:
            path = droid.write(events, settings, root)
            plan["written"] = {"session_id": r.session_id, "path": str(path),
                               "resume": {"cwd": s.cwd, "argv": ["droid", "--resume", r.session_id]}}
    else:
        s, mapped, unmapped = _map_for_devin(s)
        plan["tools"] = {"mapped": mapped, "unmapped": unmapped}
        if not dry_run:
            db = _devin_db(devin_db)
            sid = devin_write.write(s, db, model=model, thinking=thinking,
                                    note=_devin_note(s, mapped, unmapped) if note else None)
            plan["written"] = {"session_id": sid, "path": str(db),
                               "resume": {"cwd": s.cwd, "argv": ["devin", "--resume", sid]}}
    return plan


def droid_accounts(droid_sessions=None) -> dict:
    return {"sessions_by_organization": {str(k): v for k, v in droid_reown.organizations(_droid_root(droid_sessions)).items()}}


def _changes(cs) -> list[dict]:
    return [{"session_id": c.session_id, "path": str(c.path), "before": c.before, "after": c.after} for c in cs]


def droid_release(organization_id: Optional[str] = None, session_id: Optional[str] = None,
                  to_organization_id: Optional[str] = None,
                  droid_sessions=None, dry_run: bool = False) -> list[dict]:
    """Move sessions onto the signed-in Droid account's organization (close
    droid first): all of them, or those of organization_id, or one session.
    to_organization_id defaults to the signed-in account's, as droid.identity finds it."""
    root = _droid_root(droid_sessions)
    target = _droid_org(to_organization_id, droid.identity(root))
    return _changes(droid_reown.release(root, target, organization_id, session_id, dry_run))


def droid_copy(session_id: str, to_organization_id: str, droid_sessions=None) -> dict:
    """Copy a Droid session to another account's organization as a new
    session; it shows up when that account is signed in."""
    return _changes([droid_reown.copy(_droid_root(droid_sessions), session_id, to_organization_id)])[0]


def droid_restore(session_id: Optional[str] = None, droid_sessions=None, dry_run: bool = False) -> list[dict]:
    """Undo droid_release."""
    return _changes(droid_reown.restore(_droid_root(droid_sessions), session_id, dry_run))
