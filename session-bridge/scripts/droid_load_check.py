"""Re-run after a droid update: have droid itself load a converted session (no model call) and report
what it sees. Uses a synthetic session so no real conversation leaves the
machine. Writes into ~/.factory/sessions under this folder's key and
removes the files afterwards."""

import json
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sessionbridge import devin, droid, droid_build  # noqa: E402
from tests.fixtures import make_db, rich_session  # noqa: E402

cwd = str(Path(__file__).resolve().parent)
with tempfile.TemporaryDirectory() as tmp:
    db = make_db(Path(tmp) / "s.db", {"probe": rich_session(cwd)})
    with devin.connect(db) as con:
        s = devin.read_session(con, "probe")
me = droid.identity()
if not me.organization_id:
    sys.exit("no Droid session of droid's own here to take the account's organization from")
events, settings, r = droid_build.build(s, droid_build.Options(owner=me.owner, organization_id=me.organization_id,
                                                                host_id=me.host_id))
path = droid.write(events, settings)
settings_path = path.with_name(f"{r.session_id}.settings.json")
print("wrote", path)

req = {"jsonrpc": "2.0", "factoryApiVersion": "1.0.0", "type": "request", "id": "1",
       "method": "droid.load_session", "params": {"sessionId": r.session_id, "loadAllMessages": True}}
try:
    p = subprocess.run(
        ["droid", "exec", "--input-format", "stream-jsonrpc", "--output-format", "stream-jsonrpc", "--cwd", cwd],
        input=json.dumps(req) + "\n", capture_output=True, text=True, encoding="utf-8", timeout=90,
    )
    out = p.stdout
    print("exit", p.returncode, "stderr:", p.stderr[-800:])
except subprocess.TimeoutExpired as e:
    out = (e.stdout or b"").decode("utf-8", "replace") if isinstance(e.stdout, bytes) else (e.stdout or "")
    print("timed out (expected if it waits for more input)")
finally:
    for f in (path, settings_path):
        f.unlink(missing_ok=True)
    try:
        path.parent.rmdir()
    except OSError:
        pass

for line in out.splitlines():
    try:
        o = json.loads(line)
    except ValueError:
        print("non-json:", line[:200])
        continue
    if o.get("type") == "response":
        res = o.get("result") or {}
        if o.get("error"):
            print("ERROR", json.dumps(o["error"])[:1500])
        sess = res.get("session") or {}
        msgs = sess.get("messages") or []
        print("loaded messages:", len(msgs), "cwd:", res.get("cwd"), "settings.modelId:", (res.get("settings") or {}).get("modelId"))
        for m in msgs:
            kinds = [b.get("type") for b in m.get("content") or []]
            names = [b.get("name") for b in m.get("content") or [] if b.get("type") == "tool_use"]
            print("  ", m.get("role"), m.get("visibility", ""), kinds, names)
    else:
        print(o.get("type"), o.get("method"), json.dumps(o.get("params"))[:160])
