"""Where Droid keeps sessions, and writing one there.

Droid's folder is $FACTORY_HOME_OVERRIDE/.factory, else ~/.factory. A
session is `sessions/<project key>/<id>.jsonl` with `<id>.settings.json`
beside it; the project key is droid's own encoding of the working directory
(`qi` in droid 0.233.0): the real path, trailing separators off, the drive's
colon dropped, each run of separators a dash, and a dash in front, so
C:\\Users\\me\\proj is `-C-Users-me-proj`.

droid computes that key from session_start.cwd as written, and its realpath
leaves 8.3 short names (C:\\Users\\JOHNDO~1) as they are. When our cwd string
and droid's give different keys, droid looks for the transcript in a folder
it isn't in, fails (ENOENT), and keeps the turn in a copy of its own. So
the cwd written is the long, true-case path (canonical_cwd), and the folder
is made from that same string.
"""

from __future__ import annotations

import getpass
import json
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


def factory_dir() -> Path:
    home = (os.environ.get("FACTORY_HOME_OVERRIDE") or "").strip()
    return Path(os.path.expanduser(home) if home else os.path.expanduser("~")) / ".factory"


def sessions_dir() -> Path:
    return factory_dir() / "sessions"


def canonical_cwd(cwd: str) -> str:
    """The folder as written into session_start.cwd: real path, long names,
    true case. droid keys the session's folder off this string (and its own
    realpath keeps 8.3 short names like JOHNDO~1 as given), so writing the
    canonical form is what makes our folder and droid's agree."""
    p = os.path.expanduser(cwd) if cwd == "~" or cwd.startswith("~/") else cwd
    p = os.path.realpath(p) if os.path.exists(p) else os.path.abspath(p)
    if p.startswith("\\\\?\\") and not p.startswith("\\\\?\\UNC\\"):
        p = p[4:]
    return p


def project_key(cwd: str) -> str:
    p = canonical_cwd(cwd)
    p = re.sub(r"[\\/]+$", "", p)
    p = re.sub(r"^([A-Za-z]):", r"\1", p)
    return "-" + re.sub(r"[\\/]+", "-", p)


BRIDGE_SOURCE = "sessionBridgeSource"  # session_start key marking a session we wrote
BRIDGE_KEPT_ORG = "sessionBridgeOrganizationId"  # where droid-release keeps the old org


def is_bridged(start: dict) -> bool:
    """Written or rewritten by session-bridge, so not evidence of what droid
    itself writes. Sessions from before the marker carry a "[agent] " title."""
    title = start.get("title") or ""
    return (BRIDGE_SOURCE in start or BRIDGE_KEPT_ORG in start
            or title.startswith("[devin] ") or title.startswith("[droid] "))


@dataclass
class Identity:
    owner: str
    host_id: Optional[str]
    organization_id: Optional[str]


def signed_in_org(home: Optional[Path] = None) -> Optional[str]:
    """The signed-in account's organization, from the org-policy cache droid
    rewrites each time it loads the account's policy
    (`org-managed-settings.cache.json`: orgId, userId, policy; no credentials).
    Only `orgId` is read. None when droid hasn't written it."""
    try:
        with open((home or factory_dir()) / "org-managed-settings.cache.json", encoding="utf-8") as f:
            org = json.load(f).get("orgId")
    except (OSError, ValueError, AttributeError):
        return None
    return org if isinstance(org, str) and org else None


def identity(root: Optional[Path] = None, home: Optional[Path] = None) -> Identity:
    """owner, hostId and organizationId as this machine's droid writes them.

    organizationId: droid's org-policy cache when present (it follows account
    switches; a session's org doesn't until the new account writes one), else
    the latest session droid wrote itself (ones session-bridge wrote or
    rewrote are skipped), else None. owner and hostId come from that latest
    own session. home is droid's folder (default ~/.factory); root its
    sessions folder."""
    if root is None:
        root = sessions_dir()
        home = home or factory_dir()
    cached = signed_in_org(home) if home else None
    me = _from_sessions(root)
    return Identity(me.owner, me.host_id, cached or me.organization_id)


def _from_sessions(root: Path) -> Identity:
    files = list(root.glob("*/*.jsonl")) + list(root.glob("*.jsonl")) if root.is_dir() else []
    files.sort(key=lambda f: f.stat().st_mtime, reverse=True)
    fallback = None  # latest own session, when none has an organization
    for f in files:
        try:
            with open(f, encoding="utf-8") as fh:
                first = json.loads(fh.readline())
        except (OSError, ValueError):
            continue
        if first.get("type") != "session_start" or is_bridged(first):
            continue
        if first.get("organizationId"):
            return Identity(first.get("owner") or getpass.getuser(), first.get("hostId"), first["organizationId"])
        fallback = fallback or first
    if fallback:
        return Identity(fallback.get("owner") or getpass.getuser(), fallback.get("hostId"), None)
    return Identity(getpass.getuser(), None, None)


def _write_new(path: Path, text: str) -> None:
    if path.exists():
        raise FileExistsError(path)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".bridge-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def write(events: list[dict], settings: dict, root: Optional[Path] = None) -> Path:
    """Write a new session; refuses to replace one that exists."""
    start = events[0]
    folder = (root or sessions_dir()) / project_key(start["cwd"])
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{start['id']}.jsonl"
    lines = "".join(json.dumps(e, ensure_ascii=False, separators=(",", ":")) + "\n" for e in events)
    _write_new(path, lines)
    _write_new(folder / f"{start['id']}.settings.json", json.dumps(settings, indent=2) + "\n")
    return path


def load(path: Path) -> list[dict]:
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]
