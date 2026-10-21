"""Check Droid session events against what droid and the model APIs accept.

Block shapes mirror droid 0.233.0's strict zod schemas (unknown keys are
refused). The pairing rules are the model APIs': every tool_use is answered
by a tool_result in the message right after it, tool_result blocks come
before anything else in that message, and tool ids are [A-Za-z0-9_-] and
unique. A file that fails here would load and then break on the next
request, far from its cause, so the writer refuses to write one.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

ID = re.compile(r"^[A-Za-z0-9_-]+$")
MEDIA = {"image/jpeg", "image/png", "image/gif", "image/webp"}
PROVIDERS = {"anthropic", "openai", "generic-chat-completion-api", "factory", "google", "xai", "bedrock-converse", "unknown"}
VISIBILITY = {"both", "user_only", "llm_only"}

_COMMON = {"id", "cache_control"}
# type -> (required keys, optional keys)
_KEYS = {
    "text": ({"type", "text"}, set()),
    "image": ({"type", "source"}, set()),
    "thinking": ({"type", "signature", "thinking"}, {"signatureProvider", "durationMs"}),
    "redacted_thinking": ({"type", "data"}, set()),
    "tool_use": ({"type", "id", "input", "name"}, {"thought_signature", "namespace", "script_execution"}),
    "tool_result": ({"type", "tool_use_id"}, {"content", "is_error"}),
    "document": ({"type", "source"}, set()),
}


def check_block(b: Any, where: str) -> list[str]:
    if not isinstance(b, dict):
        return [f"{where}: block is not an object"]
    t = b.get("type")
    if t not in _KEYS:
        return [f"{where}: unknown block type {t!r}"]
    req, opt = _KEYS[t]
    errs = [f"{where}: {t} block missing {k!r}" for k in sorted(req - b.keys())]
    errs += [f"{where}: {t} block has unknown key {k!r}" for k in sorted(b.keys() - req - opt - _COMMON)]
    if t == "text" and not isinstance(b.get("text"), str):
        errs.append(f"{where}: text is not a string")
    elif t == "image":
        src = b.get("source")
        if not isinstance(src, dict) or set(src) != {"type", "data", "media_type"} or src.get("type") != "base64":
            errs.append(f"{where}: image source must be exactly {{type: base64, data, media_type}}")
        elif src["media_type"] not in MEDIA:
            errs.append(f"{where}: image media_type {src['media_type']!r} not accepted")
    elif t == "thinking":
        if not isinstance(b.get("thinking"), str) or not isinstance(b.get("signature"), str):
            errs.append(f"{where}: thinking and signature must be strings")
        if "signatureProvider" in b and b["signatureProvider"] not in PROVIDERS:
            errs.append(f"{where}: signatureProvider {b['signatureProvider']!r} not accepted")
    elif t == "tool_use":
        if not isinstance(b.get("id"), str) or not ID.match(b["id"]):
            errs.append(f"{where}: tool_use id {b.get('id')!r} is not [A-Za-z0-9_-]+")
        if not isinstance(b.get("name"), str) or not b["name"]:
            errs.append(f"{where}: tool_use name missing")
        if not isinstance(b.get("input"), dict):
            errs.append(f"{where}: tool_use input is not an object")
    elif t == "tool_result":
        c = b.get("content")
        if c is not None and not isinstance(c, str):
            if not isinstance(c, list) or any(not isinstance(x, dict) or x.get("type") not in ("text", "image") for x in c):
                errs.append(f"{where}: tool_result content must be a string or text/image blocks")
        if "is_error" in b and not isinstance(b["is_error"], bool):
            errs.append(f"{where}: is_error is not a boolean")
    return errs


def _timestamp_ok(ts: Any) -> bool:
    if isinstance(ts, (int, float)):
        return True
    try:
        datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        return True
    except ValueError:
        return False


def check(events: list[dict]) -> list[str]:
    errs: list[str] = []
    if not events or events[0].get("type") != "session_start":
        return ["first event is not session_start"]
    start = events[0]
    # organizationId is optional to droid's schema, but without it droid
    # rewrites the file on the first message, which fails on Windows and
    # drops that message
    for k in ("id", "owner", "organizationId"):
        if not isinstance(start.get(k), str) or not start[k]:
            errs.append(f"session_start: {k} must be a non-empty string")
    seen: set[str] = set()
    users: set[str] = set()
    tool_ids: set[str] = set()
    prev = None
    open_calls: list[str] = []
    for i, e in enumerate(events[1:], 1):
        t = e.get("type")
        where = f"event {i}"
        if t == "agent_turn_outcome":
            if e.get("turnId") not in users:
                errs.append(f"{where}: turn outcome for unknown user message {e.get('turnId')!r}")
            continue
        if t != "message":
            continue
        mid = e.get("id")
        if not isinstance(mid, str) or mid in seen:
            errs.append(f"{where}: message id missing or repeated")
        seen.add(mid)
        if prev is not None and e.get("parentId") != prev:
            errs.append(f"{where}: parentId does not point at the message before it")
        if not _timestamp_ok(e.get("timestamp")):
            errs.append(f"{where}: bad timestamp {e.get('timestamp')!r}")
        m = e.get("message") or {}
        role, content = m.get("role"), m.get("content")
        if role not in ("user", "assistant"):
            errs.append(f"{where}: role {role!r}")
        if m.get("visibility") is not None and m["visibility"] not in VISIBILITY:
            errs.append(f"{where}: visibility {m['visibility']!r}")
        if not isinstance(content, list) or not content:
            errs.append(f"{where}: content must be a non-empty list")
            content = []
        for j, b in enumerate(content):
            errs += check_block(b, f"{where} block {j}")
        blocks = [b for b in content if isinstance(b, dict)]
        if role == "assistant":
            if open_calls:
                errs.append(f"{where}: tool_use {open_calls} never answered")
            open_calls = []
            for b in blocks:
                if b.get("type") == "tool_use":
                    if b.get("id") in tool_ids:
                        errs.append(f"{where}: tool_use id {b.get('id')!r} repeated")
                    tool_ids.add(b.get("id"))
                    open_calls.append(b.get("id"))
        elif role == "user":
            kinds = [b.get("type") == "tool_result" for b in blocks]
            answered = [b.get("tool_use_id") for b in blocks if b.get("type") == "tool_result"]
            if answered:
                if sorted(answered) != sorted(open_calls):
                    errs.append(f"{where}: tool_results {answered} do not answer {open_calls}")
                if kinds != sorted(kinds, reverse=True):
                    errs.append(f"{where}: tool_result blocks must come first")
            elif open_calls:
                errs.append(f"{where}: tool_use {open_calls} never answered")
            open_calls = []
            users.add(mid)
        prev = mid
    if open_calls:
        errs.append(f"last assistant message leaves tool_use {open_calls} unanswered")
    return errs
