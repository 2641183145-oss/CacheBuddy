# Cache Buddy — notes for agents

- Python: use `py` (3.13). `python` on PATH is the Windows Store stub.
- Tests: `py -m unittest test_cc_buddy` from this folder.
- Build: `py -m PyInstaller CacheBuddy.spec` -> `dist\CacheBuddy.exe`; copy it to `..\CacheBuddy.exe`. `make_dist.py` writes a zip to the Desktop.
- Droid Desktop heartbeats (`droid_buddy.send_desktop_heartbeat`) go through Factory Desktop's CDP on port 9223
  (Factory must be started with `--remote-debugging-port=9223`; override with env `CACHE_BUDDY_FACTORY_CDP_PORT`).
  Devin Desktop uses 9222. Factory DOM hooks: `[data-testid="chat-composer-wrapper"]`, buttons "Send message" / "Stop agent",
  chips "Remove <name>", routing `location.hash = '#/sessions/<sid>'`.
