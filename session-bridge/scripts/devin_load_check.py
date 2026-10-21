"""Re-run after a Devin CLI update: have Devin itself load a session over
ACP (initialize, then session/load; no model call) and print what it
replays. Usage: devin_load_check.py <session id> <cwd>"""

import json
import os
import shutil
import subprocess
import sys
import threading
import time

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
DEVIN = shutil.which("devin") or os.path.expandvars(r"%LOCALAPPDATA%\devin\cli\bin\devin.exe")
sid, cwd = sys.argv[1], sys.argv[2]

p = subprocess.Popen([DEVIN, "acp"], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                     text=True, encoding="utf-8", cwd=cwd)
msgs: list[dict] = []


def pump():
    for line in p.stdout:
        if line.startswith("{"):
            try:
                msgs.append(json.loads(line))
            except ValueError:
                pass


threading.Thread(target=pump, daemon=True).start()


def send(i, method, params):
    p.stdin.write(json.dumps({"jsonrpc": "2.0", "id": i, "method": method, "params": params}) + "\n")
    p.stdin.flush()


def reply(i, timeout=60):
    end = time.time() + timeout
    while time.time() < end:
        for m in msgs:
            if m.get("id") == i:
                return m
        time.sleep(0.2)
    return None


try:
    send(1, "initialize", {"protocolVersion": 1, "clientCapabilities": {}})
    if not reply(1):
        sys.exit("devin acp did not answer initialize")
    start = len(msgs)
    send(2, "session/load", {"sessionId": sid, "cwd": cwd, "mcpServers": []})
    res = reply(2)
    if res is None:
        sys.exit("no reply to session/load")
    if "error" in res:
        sys.exit("ERROR " + json.dumps(res["error"], ensure_ascii=False))
    print("loaded")
    for m in msgs[start:]:
        up = (m.get("params") or {}).get("update") or {}
        k = up.get("sessionUpdate")
        if k in ("user_message_chunk", "agent_message_chunk", "agent_thought_chunk"):
            print(f"  {k:20} {(up.get('content') or {}).get('text', '')[:80]!r}")
        elif k in ("tool_call", "tool_call_update"):
            print(f"  {k:20} {up.get('title') or ''} {up.get('status')}")
finally:
    p.kill()
