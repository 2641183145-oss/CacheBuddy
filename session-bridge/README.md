# session-bridge

Carry a coding-agent session into another agent as a native session there, so the
other agent resumes the same conversation (every message, tool call, result and
piece of reasoning) instead of a summary of it. Devin CLI ↔ Droid, plus moving Droid
sessions between Droid accounts.

Python 3.11+, standard library only. Reads and writes local files only; sends nothing
over the network.

## Embedding

The calls a host app makes are in `sessionbridge/api.py`. Every one of them returns a
JSON-ready dict.

```python
from sessionbridge import api

api.list_sessions("devin")                    # or "droid"
plan = api.preview("devin", "forest-vermicelli")    # what carrying would do; writes nothing
res  = api.carry("devin", "forest-vermicelli")      # -> new Droid session
res["written"]["resume"]   # {"cwd": "...", "argv": ["droid", "--resume", "<id>"]}

api.carry("droid", "<droid session id>")            # -> new Devin session
api.droid_accounts()                                # Droid sessions per account org
api.droid_release()                                 # move them onto the signed-in account
api.droid_restore()                                 # undo
api.droid_copy("<droid session id>", "<other org>") # a copy for another account, original kept
```

From another language, call the CLI with `--json`. It prints the same dicts.

```sh
python -m sessionbridge --json list --agent devin
python -m sessionbridge --json carry --from devin --session forest-vermicelli --dry-run
python -m sessionbridge --json carry --from droid --session <id>
python -m sessionbridge --json droid-release --dry-run
```

`carry` picks the latest session in `--cwd` (default: the current folder) when no
`--session` is given. `--any-dir` picks the latest anywhere. On failure the exit
code is 1 and the message goes to stderr.

### `carry` / `preview` result

```jsonc
{
  "source": {"agent": "devin", "id": "...", "title": "...", "cwd": "...", "model": "..."},
  "target": "droid",
  "before": {"user_messages": 2, "assistant_messages": 22, "tool_calls": 25, "thinking": 11, "images": 0, "tokens": 53222},
  "after":  { ... same keys, after trimming ... },
  "trimmed_calls": 2,                 // unused read-only calls left out
  "tools": {"mapped": {"exec": "Execute", ...}, "unmapped": {"get_output": 1}},
  "interrupted_calls": 0,             // calls that never returned (Droid target)
  "droid_organization_id": "...",     // the account it was written for (Droid target)
  "warnings": [{"kind": "large" | "cwd_missing", "message": "..."}],
  "dry_run": false,
  "written": {"session_id": "...", "path": "...", "resume": {"cwd": "...", "argv": [...]}}  // absent on dry run
}
```

The `tokens` figures are estimates (characters / 4). Use them for the size reminder.

### Options

| option | default | meaning |
|---|---|---|
| `trim` / `--no-trim` | on | leave out read-only calls (read, grep, glob, search, fetch) older than the last 20 turns whose results nothing later used; a note marks where |
| `thinking` | `downgrade` | `downgrade` keeps the reasoning text without its signature; `keep` keeps signatures (only when the target runs on the backend that signed them); `drop` leaves it out |
| `note` / `--no-note` | on | adds a model-only message saying where the session came from, how tool names map, and which tools the target lacks |
| `model`, `reasoning_effort` | target's default | model for the new session |
| `organization_id` / `--droid-org` | the signed-in account's | Droid organization to write into the new session |
| `devin_db`, `droid_sessions` | the agents' own | where to read and write |

## What happens to a session

Kept whole: every user message (text and images), everything the assistant said,
every tool call with its arguments and result, error flags, reasoning text, and the
source agent's own compaction summaries.

Changed on the way:
- Tool calls are renamed to the target's tool that does the same job, with their
  arguments mapped (see `sessionbridge/tools.py`; timeouts ms ↔ s, Windows `\\?\`
  prefixes dropped, `workdir` folded into the command). Arguments with no
  counterpart are kept under their own names. Tools the target has no equivalent
  for keep their name, and the note tells the model not to call them.
- Reasoning keeps its text, but signatures only verify on the account and backend
  that made them, so by default it is stored unsigned. The model still reads it as
  reasoning, not as something said to the user. On a Droid target, droid sends
  unsigned reasoning wrapped in `<thinking>…</thinking>` (on Google routes it
  drops it). On a Devin target it stays in Devin's own `thinking` field.
- Tool ids are rewritten to the `[A-Za-z0-9_-]` form the model APIs need. A call
  that never got a result (the source ran out of quota mid-call) gets an
  "interrupted" error result, so the history stays sendable.
- Each agent's own prompt, rules, environment blocks and UI-only notices are left
  out. The target builds its own on resume.

Not carried: running processes (background shells, previews), per-agent UI state,
the source's token accounting, and fork lineage.

Written into a Droid session's `session_start`: the signed-in account's
`organizationId` (see below), `owner`/`hostId` from droid's own sessions, the
working directory in its long, true-case form (droid names the session folder after
it, and an 8.3 short name such as `JOHNDO~1` would put droid's copy in a different
folder from ours), and `sessionBridgeSource` (`<agent>:<id>`) marking it as carried.

The source session is only read. A carry always creates a new session, and nothing
existing is overwritten.

## Droid account switch

droid ties a session to an account's organization through
`session_start.organizationId`, and hides every session whose organization
isn't the signed-in account's. `droid-release` moves sessions onto the
signed-in account's organization. The old value (or `null` when there was none) is
kept in `sessionBridgeOrganizationId`, and `droid-restore` puts back exactly what
was there. Only the first line of each file changes. Close droid before running it:
an open session is held in memory and written back.

It writes the organization instead of removing it. With no `organizationId`, droid
fills it in on the first message by rewriting the file (tmp + rename), which fails
with `EPERM` on Windows while droid has the file open, and that message is lost.

## Which account is signed in

`carry` into Droid and `droid-release` need the signed-in account's organization.
They take it from, in order:

1. `organization_id` / `--droid-org` (carry), `to_organization_id` / `--to-org` (release);
2. `~/.factory/org-managed-settings.cache.json` → `orgId`, which droid rewrites
   whenever it loads the account's policy, so it follows account switches. Only
   that one field is read; nothing reads droid's login token;
3. the latest session droid wrote itself (sessions session-bridge wrote or
   rewrote are skipped, so they never vouch for an account).

When none of these exists, carry fails with `NoDroidAccount` (exit 1). Open Droid,
send a message in a new session, and retry.

## Where each agent keeps sessions

- Devin CLI: `%APPDATA%\devin\cli\sessions.db` on Windows, `~/.config/devin/cli/sessions.db` elsewhere.
  It is SQLite. `sessions.main_chain_id` is the head of the conversation, and you
  walk `message_nodes.parent_node_id` back to the root. Devin re-roots its prompt
  prefix and copies older nodes under it, so only the chain from the head is the
  live conversation.
- Droid: `~/.factory/sessions/<project key>/<id>.jsonl` (+ `<id>.settings.json`),
  or under `$FACTORY_HOME_OVERRIDE/.factory`. The project key is the real cwd with
  the drive colon dropped and separators turned into dashes, with a leading dash:
  `C:\Users\me\proj` → `-C-Users-me-proj`.

The format details are written at the top of each module (`devin.py`, `devin_write.py`,
`droid.py`, `droid_read.py`, `droid_build.py`, `droid_check.py`, `droid_reown.py`).
They were read from Devin CLI 3000.11.3 and droid 0.233.0.

## Verifying after an agent update

```sh
python -m unittest discover -s tests -t .                      # 51 tests, no agent needed
python scripts/droid_load_check.py                             # droid loads a converted (synthetic) session
python scripts/devin_load_check.py <devin session id> <cwd>    # Devin replays a session over ACP
python scripts/droid_live_check.py                             # carry -> droid exec one message -> it was appended
```

The two load checks run the agents' own loaders and make no model calls.
`droid_live_check.py` spends a little Droid quota. It carries a synthetic session,
sends "Reply with exactly: ok" with `droid exec -s <id>`, checks that the jsonl
gained the user message and the reply, and deletes the session again (`--keep` to
look at it). The synthetic session is from `tests/fixtures.py`, so none of your
conversations are sent.

## Not yet verified

- A live turn after carrying into Devin. Devin's loader replays carried sessions
  intact, but no model call has been made there.
- A real conversation carried into Droid in the desktop app. Live checks so far use
  `droid exec` and a synthetic session.
- macOS/Linux paths are written to the agents' conventions but were only exercised
  on Windows.
