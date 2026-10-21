"""session-bridge: carry an agent session into another agent.

  session-bridge list --agent devin|droid
  session-bridge carry --from devin|droid [--session ID] [--dry-run]
  session-bridge droid-accounts
  session-bridge droid-release [--org ID | --session ID] [--to-org ID] [--dry-run]
  session-bridge droid-restore [--session ID] [--dry-run]
  session-bridge check FILE.jsonl

Every command takes --json, which prints what sessionbridge.api returns.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

from . import api, droid, droid_check


def _when(t) -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(t)) if t else "?"


def _out(a, data, human) -> int:
    if a.json:
        print(json.dumps(data, ensure_ascii=False, indent=2))
    else:
        human(data)
    return 0


def cmd_list(a) -> int:
    def show(rows):
        for s in rows:
            title = " ".join((s["title"] or "").split())[:70]
            org = f"  org {s['organization_id']}" if s.get("organization_id") else ""
            print(f"{s['id']}  {_when(s['updated_at'])}  {s['size']:>5}  {s['cwd']}{org}\n    {title}")
    return _out(a, api.list_sessions(a.agent, a.devin_db, a.droid_sessions), show)


def cmd_carry(a) -> int:
    plan = api.carry(a.from_, a.session, a.to, cwd=None if (a.session or a.any_dir) else (a.cwd or os.getcwd()),
                     trim=not a.no_trim, thinking=a.thinking, model=a.model, reasoning_effort=a.reasoning_effort,
                     note=not a.no_note, organization_id=a.droid_org,
                     devin_db=a.devin_db, droid_sessions=a.droid_sessions, dry_run=a.dry_run)

    def show(p):
        src, b, f = p["source"], p["before"], p["after"]
        print(f"{src['agent']} session {src['id']}  ({src['cwd']})  ->  {p['target']}")
        print(f"  {f['user_messages']} user / {f['assistant_messages']} assistant messages, "
              f"{f['tool_calls']} tool calls, {f['thinking']} thinking, {f['images']} images")
        print(f"  ~{f['tokens']:,} tokens" + (f" (was ~{b['tokens']:,}; {p['trimmed_calls']} unused read-only calls left out)"
                                            if p["trimmed_calls"] else ""))
        if p["tools"]["mapped"]:
            print("  tools: " + ", ".join(f"{k}->{v}" for k, v in sorted(p["tools"]["mapped"].items())))
        if p["tools"]["unmapped"]:
            print("  no equivalent, kept by name: " + ", ".join(f"{k} x{n}" for k, n in sorted(p["tools"]["unmapped"].items())))
        if p.get("interrupted_calls"):
            print(f"  {p['interrupted_calls']} call(s) never returned; marked interrupted")
        for w in p["warnings"]:
            print(f"  ! {w['message']}")
        if p.get("written"):
            w = p["written"]
            print(f"\nwrote {w['path']}\nresume:  cd \"{w['resume']['cwd']}\"; {' '.join(w['resume']['argv'])}")
        else:
            print("dry run: nothing written")
    try:
        return _out(a, plan, show)
    except (LookupError, ValueError, FileNotFoundError) as e:
        print(e, file=sys.stderr)
        return 1


def cmd_accounts(a) -> int:
    def show(d):
        for org, n in d["sessions_by_organization"].items():
            print(f"{n:5}  {'(none set)' if org == 'None' else org}")
    return _out(a, api.droid_accounts(a.droid_sessions), show)


def _show_changes(cs, verb):
    for c in cs:
        print(f"{c['session_id']}  {c['before']} -> {c['after']}")
    print(f"{len(cs)} session(s) {verb}.")


def cmd_release(a) -> int:
    cs = api.droid_release(a.org, a.session, a.to_org, a.droid_sessions, a.dry_run)
    return _out(a, cs, lambda c: _show_changes(c, "would be released" if a.dry_run else "released; close and reopen droid to see them"))


def cmd_restore(a) -> int:
    cs = api.droid_restore(a.session, a.droid_sessions, a.dry_run)
    return _out(a, cs, lambda c: _show_changes(c, "would be restored" if a.dry_run else "restored"))


def cmd_check(a) -> int:
    errs = droid_check.check(droid.load(Path(a.file)))
    _out(a, {"ok": not errs, "errors": errs}, lambda d: print("\n".join(errs) or "ok"))
    return 1 if errs else 0


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    p = argparse.ArgumentParser(prog="session-bridge", description="Carry an agent session into another agent.")
    p.add_argument("--devin-db", help="Devin CLI sessions.db (default: the CLI's own)")
    p.add_argument("--droid-sessions", help="Droid sessions folder (default: ~/.factory/sessions)")
    p.add_argument("--json", action="store_true", help="print machine-readable output")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("list", help="list sessions")
    s.add_argument("--agent", choices=api.AGENTS, default="devin")
    s.set_defaults(fn=cmd_list)

    c = sub.add_parser("carry", help="carry a session into the other agent")
    c.add_argument("--from", dest="from_", choices=api.AGENTS, default="devin")
    c.add_argument("--to", choices=api.AGENTS, help="default: the other agent")
    c.add_argument("--session", help="session id (default: the latest in --cwd)")
    c.add_argument("--cwd", help="pick the latest session in this folder (default: the current one)")
    c.add_argument("--any-dir", action="store_true", help="pick the latest session anywhere")
    c.add_argument("--model", help="model for the new session")
    c.add_argument("--reasoning-effort", help="reasoning effort (Droid)")
    c.add_argument("--thinking", choices=["downgrade", "keep", "drop"], default="downgrade",
                   help="downgrade: keep the reasoning, unsigned (default); keep: keep signatures; drop: leave it out")
    c.add_argument("--no-trim", action="store_true", help="keep every tool call (default: leave out unused read-only ones)")
    c.add_argument("--no-note", action="store_true", help="don't tell the model it was carried over")
    c.add_argument("--droid-org", help="Droid organization id for the new session (default: the signed-in account's)")
    c.add_argument("--dry-run", action="store_true", help="report what would happen, write nothing")
    c.set_defaults(fn=cmd_carry)

    sub.add_parser("droid-accounts", help="Droid sessions per account organization").set_defaults(fn=cmd_accounts)

    r = sub.add_parser("droid-release", help="let Droid sessions load under any account (close droid first)")
    g = r.add_mutually_exclusive_group()
    g.add_argument("--org", help="only sessions of this organization")
    g.add_argument("--session", help="only this session")
    r.add_argument("--to-org", help="organization to move them onto (default: the signed-in account's)")
    r.add_argument("--dry-run", action="store_true")
    r.set_defaults(fn=cmd_release)

    u = sub.add_parser("droid-restore", help="undo droid-release")
    u.add_argument("--session")
    u.add_argument("--dry-run", action="store_true")
    u.set_defaults(fn=cmd_restore)

    k = sub.add_parser("check", help="check a Droid session file")
    k.add_argument("file")
    k.set_defaults(fn=cmd_check)

    a = p.parse_args(argv)
    try:
        return a.fn(a)
    except (LookupError, ValueError, FileNotFoundError) as e:
        print(e, file=sys.stderr)
        return 1
