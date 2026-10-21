"""Live check (spends a little Droid quota): carry a synthetic session into
Droid, send one message with `droid exec -s`, and confirm droid appended it
to the session file (the first message used to be rolled back when
organizationId was missing). Removes the session afterwards unless --keep.

Usage: droid_live_check.py [--org ORG] [--keep]
"""

import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sessionbridge import api, droid  # noqa: E402
from tests.fixtures import make_db, rich_session  # noqa: E402

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
ap = argparse.ArgumentParser()
ap.add_argument("--org", help="organization id to write (default: what carry picks)")
ap.add_argument("--keep", action="store_true")
a = ap.parse_args()


def roles(path: Path) -> list[str]:
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            e = json.loads(line)
            if e.get("type") == "message":
                out.append(e["message"]["role"] + ("/" + e["message"]["visibility"] if e["message"].get("visibility") else ""))
    return out


with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as work:
    db = make_db(Path(tmp) / "s.db", {"live": rich_session(work)})
    r = api.carry("devin", "live", devin_db=db, organization_id=a.org)
    sid, path = r["written"]["session_id"], Path(r["written"]["path"])
    settings = path.with_name(f"{sid}.settings.json")
    print("carried", sid, "org", r["droid_organization_id"])
    before = roles(path)
    try:
        p = subprocess.run(["droid", "exec", "-s", sid, "--cwd", work, "Reply with exactly: ok"],
                           capture_output=True, text=True, encoding="utf-8", timeout=180)
        print("droid exec exit", p.returncode)
        print("  stdout:", p.stdout.strip()[-300:])
        if p.stderr.strip():
            print("  stderr:", p.stderr.strip()[-600:])
        after = roles(path)
        added = after[len(before):]
        print("messages before", len(before), "after", len(after), "added", added)
        first = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
        print("session_start.organizationId now", first.get("organizationId"))
        ok = after[:len(before)] == before and "user" in added
        print("PASS: the message was kept" if ok else "FAIL: droid did not append the message")
    finally:
        if not a.keep:
            for f in (path, settings, settings.with_name(settings.name + ".bak")):
                f.unlink(missing_ok=True)
            try:
                path.parent.rmdir()
            except OSError:
                pass
sys.exit(0 if ok else 1)
