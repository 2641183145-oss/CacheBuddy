"""Map Devin CLI tool calls onto Droid's tools.

Droid's tool inputs, read from droid 0.233.0's own schemas:

  Read     file_path, offset?, limit?
  Edit     file_path, old_str, new_str, change_all?
  Create   file_path, content
  Execute  command, timeout? (seconds), summary?, riskLevel?, fireAndForget?
  Grep     pattern, path?, glob_pattern?, output_mode? ("file_paths"|"content"),
           case_insensitive?, head_limit?, ...
  Glob     patterns?, folder?, excludePatterns?
  LS       directory_path?, ignorePatterns?
  WebSearch query, ...    FetchUrl url    Skill skill

A call is mapped when a Droid tool does the same thing, so the model reading
the history sees calls in the tools it now has. Arguments with no Droid
counterpart are kept under their own names rather than dropped. A Devin tool
with no Droid equivalent (its background shells, its browser preview) keeps
its own name: renaming it would claim something Droid can't do.
"""

from __future__ import annotations

import os
from typing import Any, Callable

Args = dict[str, Any]


def _path(p: Any) -> Any:
    # Devin passes Windows extended-length paths (\\?\C:\...); same file, plainer form.
    if isinstance(p, str) and p.startswith("\\\\?\\") and not p.startswith("\\\\?\\UNC\\"):
        return p[4:]
    return p


def _same_dir(a: str, b: str) -> bool:
    if not b:
        return False

    def norm(p: str) -> str:
        return os.path.normcase(os.path.normpath(_path(p)))

    return norm(a) == norm(b)


def _rest(args: Args, used: set[str]) -> Args:
    return {k: v for k, v in args.items() if k not in used}


def _exec(a: Args, cwd: str = "") -> Args:
    cmd = a.get("command", "")
    wd = a.get("workdir")
    if wd and not _same_dir(wd, cwd):
        # Execute runs in the session folder and has no workdir of its own;
        # `cd "<dir>";` means the same in bash and PowerShell.
        cmd = f'cd "{_path(wd)}"; {cmd}'
    out: Args = {"command": cmd}
    t = a.get("timeout")
    if isinstance(t, (int, float)) and t > 0:
        out["timeout"] = max(1, round(t / 1000))  # Devin: ms, Droid: s
    out.update(_rest(a, {"command", "workdir", "timeout", "shell_flavor"}))
    return out


def _read(a: Args, cwd: str = "") -> Args:
    out: Args = {"file_path": _path(a.get("file_path"))}
    for k in ("offset", "limit"):
        if k in a:
            out[k] = a[k]
    out.update(_rest(a, {"file_path", "offset", "limit"}))
    return out


_GREP_MODE = {"files_with_matches": "file_paths", "content": "content"}


def _grep(a: Args, cwd: str = "") -> Args:
    out: Args = {"pattern": a.get("pattern", "")}
    if "path" in a:
        out["path"] = _path(a["path"])
    mode = a.get("output_mode")
    if mode in _GREP_MODE:
        out["output_mode"] = _GREP_MODE[mode]
    elif mode is not None:
        out["output_mode"] = mode
    if "case_insensitive" in a:
        out["case_insensitive"] = a["case_insensitive"]
    if "max_results" in a:
        out["head_limit"] = a["max_results"]
    if "glob" in a:
        out["glob_pattern"] = a["glob"]
    out.update(_rest(a, {"pattern", "path", "output_mode", "case_insensitive", "max_results", "glob"}))
    return out


def _find(a: Args, cwd: str = "") -> Args:
    out: Args = {}
    if "pattern" in a:
        out["patterns"] = [a["pattern"]]
    if "path" in a:
        out["folder"] = _path(a["path"])
    out.update(_rest(a, {"pattern", "path"}))
    return out


def _edit(a: Args, cwd: str = "") -> Args:
    out: Args = {
        "file_path": _path(a.get("file_path")),
        "old_str": a.get("old_string", a.get("old_str", "")),
        "new_str": a.get("new_string", a.get("new_str", "")),
    }
    if "replace_all" in a:
        out["change_all"] = a["replace_all"]
    out.update(_rest(a, {"file_path", "old_string", "new_string", "old_str", "new_str", "replace_all"}))
    return out


def _write(a: Args, cwd: str = "") -> Args:
    out: Args = {"file_path": _path(a.get("file_path")), "content": a.get("content", "")}
    out.update(_rest(a, {"file_path", "content"}))
    return out


def _rename(**names: str) -> Callable[[Args], Args]:
    def f(a: Args, cwd: str = "") -> Args:
        return {names.get(k, k): v for k, v in a.items()}

    return f


def _skill(a: Args, cwd: str = "") -> Args:
    out: Args = {"skill": a.get("skill", "")}
    out.update(_rest(a, {"skill", "command"}) if a.get("command") == "invoke" else _rest(a, {"skill"}))
    return out


# Devin tool name -> (Droid tool name, argument mapping)
DEVIN_TO_DROID: dict[str, tuple[str, Callable[..., Args]]] = {
    "exec": ("Execute", _exec),
    "read": ("Read", _read),
    "grep": ("Grep", _grep),
    "find_file_by_name": ("Glob", _find),
    "edit": ("Edit", _edit),
    "write": ("Create", _write),
    "web_search": ("WebSearch", _rename()),
    "webfetch": ("FetchUrl", _rename()),
    "skill": ("Skill", _skill),
}



# ── Droid → Devin ──────────────────────────────────────────────────────────

_GREP_MODE_REV = {"file_paths": "files_with_matches", "content": "content"}


def _droid_exec(a: Args, cwd: str = "") -> Args:
    cmd = a.get("command", "")
    out: Args = {"command": cmd, "shell_flavor": "bash"}
    t = a.get("timeout")
    if isinstance(t, (int, float)) and t > 0:
        out["timeout"] = round(t * 1000)  # Droid: s, Devin: ms
    out.update(_rest(a, {"command", "timeout"}))
    return out


def _droid_read(a: Args, cwd: str = "") -> Args:
    out: Args = {"file_path": a.get("file_path", "")}
    for k in ("offset", "limit"):
        if k in a:
            out[k] = a[k]
    out.update(_rest(a, {"file_path", "offset", "limit"}))
    return out


def _droid_grep(a: Args, cwd: str = "") -> Args:
    out: Args = {"pattern": a.get("pattern", "")}
    if "path" in a:
        out["path"] = a["path"]
    if "glob_pattern" in a:
        out["glob"] = a["glob_pattern"]
    mode = a.get("output_mode")
    if mode in _GREP_MODE_REV:
        out["output_mode"] = _GREP_MODE_REV[mode]
    elif mode is not None:
        out["output_mode"] = mode
    if "case_insensitive" in a:
        out["case_insensitive"] = a["case_insensitive"]
    if "head_limit" in a:
        out["max_results"] = a["head_limit"]
    out.update(_rest(a, {"pattern", "path", "glob_pattern", "output_mode", "case_insensitive", "head_limit"}))
    return out


def _droid_glob(a: Args, cwd: str = "") -> Args:
    out: Args = {}
    patterns = a.get("patterns")
    if patterns and isinstance(patterns, list):
        out["pattern"] = patterns[0] if len(patterns) == 1 else "|".join(patterns)
    if "folder" in a:
        out["path"] = a["folder"]
    out.update(_rest(a, {"patterns", "folder", "excludePatterns"}))
    return out


def _droid_edit(a: Args, cwd: str = "") -> Args:
    out: Args = {
        "file_path": a.get("file_path", ""),
        "old_string": a.get("old_str", a.get("old_string", "")),
        "new_string": a.get("new_str", a.get("new_string", "")),
    }
    if "change_all" in a:
        out["replace_all"] = a["change_all"]
    out.update(_rest(a, {"file_path", "old_str", "new_str", "old_string", "new_string", "change_all"}))
    return out


def _droid_create(a: Args, cwd: str = "") -> Args:
    out: Args = {"file_path": a.get("file_path", ""), "content": a.get("content", "")}
    out.update(_rest(a, {"file_path", "content"}))
    return out


def _droid_websearch(a: Args, cwd: str = "") -> Args:
    out: Args = {"query": a.get("query", "")}
    out.update(_rest(a, {"query"}))
    return out


def _droid_fetchurl(a: Args, cwd: str = "") -> Args:
    return dict(a)


def _droid_skill(a: Args, cwd: str = "") -> Args:
    return {"command": "invoke", "skill": a.get("skill", "")}


DROID_TO_DEVIN: dict[str, tuple[str, Callable[..., Args]]] = {
    "Execute": ("exec", _droid_exec),
    "Read": ("read", _droid_read),
    "Grep": ("grep", _droid_grep),
    "Glob": ("find_file_by_name", _droid_glob),
    "LS": ("find_file_by_name", _droid_glob),
    "Edit": ("edit", _droid_edit),
    "Create": ("write", _droid_create),
    "WebSearch": ("web_search", _droid_websearch),
    "FetchUrl": ("webfetch", _droid_fetchurl),
    "Skill": ("skill", _droid_skill),
}


def droid_to_devin(name: str, args: Args, cwd: str = "") -> tuple[str, Args, bool]:
    """(Devin name, Devin arguments, whether it mapped). cwd is the session folder."""
    m = DROID_TO_DEVIN.get(name)
    if m is None:
        return name, dict(args), False
    target, fn = m
    return target, fn(dict(args), cwd), True


def devin_to_droid(name: str, args: Args, cwd: str = "") -> tuple[str, Args, bool]:
    """(Droid name, Droid arguments, whether it mapped). cwd is the session's
    folder, where Execute runs."""
    m = DEVIN_TO_DROID.get(name)
    if m is None:
        return name, dict(args), False
    target, fn = m
    return target, fn(dict(args), cwd), True
