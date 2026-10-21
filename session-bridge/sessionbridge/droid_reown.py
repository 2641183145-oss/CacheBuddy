"""Make Droid sessions from one account load under another.

droid ties a session to an organization through session_start.organizationId,
and on load and in its session list lets a session through only when that
is the signed-in account's organization or one of its additional ones (`ja`
in droid 0.233.0). Each Factory account has its own organization, so after
switching accounts the old sessions disappear.

`release` sets the field to the signed-in account's organization. It does not
remove it: an absent organizationId passes the check, but droid then fills it
in on the first message by rewriting the file (tmp + rename), which fails
with EPERM on Windows while droid has the file open and drops that message.
The old value is kept in `sessionBridgeOrganizationId` (null when there was
none; session_start passes unknown keys through), so `restore` puts back
exactly what was there.

droid's session list comes from an index it refreshes when a file's size or
mtime changes; the rewrite changes the size, and the mtime is moved forward
by one second so the list order is kept.

Close droid first: it holds the session it has open in memory and writes it
back, which would undo the change for that one.

`copy` is the other way to take a session along: a new session for the other
account, the original left as it is (so droid can stay open).
"""

from __future__ import annotations

import json
import os
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from . import droid

KEPT = droid.BRIDGE_KEPT_ORG


@dataclass
class Change:
    session_id: str
    path: Path
    before: Optional[str]
    after: Optional[str]


def droid_first(path: Path) -> Optional[dict]:
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            if line.strip():
                o = json.loads(line)
                return o if o.get("type") == "session_start" else None
    return None


def _rewrite_start(path: Path, change: Callable[[dict], None]) -> tuple[Optional[str], Optional[str]]:
    """Apply change to the session_start line; the rest of the file is kept byte for byte."""
    with open(path, encoding="utf-8", newline="") as fh:
        lines = fh.readlines()
    i = next(k for k, line in enumerate(lines) if line.strip())
    start = json.loads(lines[i])
    before = start.get("organizationId")
    change(start)
    after = start.get("organizationId")
    lines[i] = json.dumps(start, ensure_ascii=False, separators=(",", ":")) + "\n"
    st = path.stat()
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".bridge-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
            f.writelines(lines)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
    os.utime(path, (st.st_atime, st.st_mtime + 1))
    return before, after


def _sessions(root: Optional[Path], session_id: Optional[str]):
    for f in sorted((root or droid.sessions_dir()).glob("*/*.jsonl")):
        if session_id and f.stem != session_id:
            continue
        try:
            first = droid_first(f)
        except (OSError, ValueError):
            continue
        if first:
            yield f, first


def release(root: Optional[Path], to_organization_id: str, organization_id: Optional[str] = None,
            session_id: Optional[str] = None, dry_run: bool = False) -> list[Change]:
    """Move sessions onto to_organization_id: all of them, or those of
    organization_id, or one session. Sessions already on it are left alone."""
    if not to_organization_id:
        raise ValueError("to_organization_id is required")
    out = []
    for f, first in _sessions(root, session_id):
        org = first.get("organizationId")
        if org == to_organization_id or (organization_id and org != organization_id):
            continue

        def change(start: dict) -> None:
            if KEPT not in start:  # keep the original through repeated releases
                start[KEPT] = start.get("organizationId")
            start["organizationId"] = to_organization_id

        if dry_run:
            out.append(Change(f.stem, f, org, to_organization_id))
        else:
            out.append(Change(f.stem, f, *_rewrite_start(f, change)))
    return out


def restore(root: Optional[Path] = None, session_id: Optional[str] = None,
            dry_run: bool = False) -> list[Change]:
    """Put back the organization `release` replaced (or its absence)."""
    out = []
    for f, first in _sessions(root, session_id):
        if KEPT not in first:
            continue

        def change(start: dict) -> None:
            old = start.pop(KEPT)
            if old is None:
                start.pop("organizationId", None)
            else:
                start["organizationId"] = old

        if dry_run:
            out.append(Change(f.stem, f, first.get("organizationId"), first[KEPT]))
        else:
            out.append(Change(f.stem, f, *_rewrite_start(f, change)))
    return out


_USAGE = ("tokenUsage", "inclusiveTokenUsage")


def copy(root: Optional[Path], session_id: str, to_organization_id: str) -> Change:
    """Copy one session to another account: a new session id under
    to_organization_id, the original untouched (droid may have it open).
    The transcript is kept line for line, a half-written last line left
    out. The copy's running token totals start at zero, so a usage reader
    that books their growth doesn't count the original's history twice;
    the last call's usage stays (it is the context size). Marked with
    BRIDGE_SOURCE = "droid:<original id>", so identity() doesn't take its
    organization for the signed-in account's."""
    if not to_organization_id:
        raise ValueError("to_organization_id is required")
    src = next((f for f, _ in _sessions(root, session_id)), None)
    if src is None:
        raise FileNotFoundError(f"no Droid session {session_id}")
    with open(src, encoding="utf-8", newline="") as fh:
        lines = fh.readlines()
    if lines and not lines[-1].endswith("\n"):
        lines.pop()  # droid is still writing it
    i = next(k for k, line in enumerate(lines) if line.strip())
    start = json.loads(lines[i])
    before = start.get("organizationId")
    new_id = str(uuid.uuid4())
    start.pop(KEPT, None)
    start.update(id=new_id, organizationId=to_organization_id)
    start[droid.BRIDGE_SOURCE] = f"droid:{session_id}"
    lines[i] = json.dumps(start, ensure_ascii=False, separators=(",", ":")) + "\n"
    try:
        with open(src.with_name(f"{session_id}.settings.json"), encoding="utf-8") as fh:
            settings = json.load(fh)
    except (OSError, ValueError):
        settings = {}
    for k in _USAGE:
        if isinstance(settings.get(k), dict):
            settings[k] = {n: 0 for n in settings[k]}
    settings.pop("childInclusiveTokenUsageBySessionId", None)
    path = src.with_name(f"{new_id}.jsonl")
    droid._write_new(path, "".join(lines))
    droid._write_new(src.with_name(f"{new_id}.settings.json"), json.dumps(settings, indent=2) + "\n")
    return Change(new_id, path, before, to_organization_id)


def organizations(root: Optional[Path] = None) -> dict[Optional[str], int]:
    """How many sessions each organization has (None: no organization set)."""
    counts: dict[Optional[str], int] = {}
    for _, first in _sessions(root, None):
        org = first.get("organizationId")
        counts[org] = counts.get(org, 0) + 1
    return counts
