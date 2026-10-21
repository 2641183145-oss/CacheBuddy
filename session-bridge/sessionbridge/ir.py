"""The neutral session form readers produce and writers consume.

It keeps what a conversation is (who said what, which tools ran with which
arguments, what they returned, what the model was thinking) and drops what
only one agent cares about (its own system prompt, UI state, telemetry).
Tool names and arguments stay as the source agent spelled them; mapping them
is the writer's job, since only the writer knows the target's tools.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional, Union


@dataclass
class Text:
    text: str


@dataclass
class Image:
    media_type: str
    data: str  # base64
    source_path: Optional[str] = None


@dataclass
class Thinking:
    text: str
    signature: str = ""
    # who signed it, as the source said: "anthropic", "gemini", "sealed", or None
    signature_type: Optional[str] = None


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


Block = Union[Text, Image, Thinking, ToolCall]


@dataclass
class UserTurn:
    blocks: list[Block]  # Text and Image only
    created_at: Optional[float] = None  # epoch seconds


@dataclass
class AssistantTurn:
    blocks: list[Block]  # Thinking, Text, ToolCall in order
    model: Optional[str] = None
    created_at: Optional[float] = None


@dataclass
class ToolResult:
    call_id: str
    output: str
    is_error: bool = False
    created_at: Optional[float] = None


@dataclass
class Summary:
    """Text the source agent wrote in place of earlier turns it compacted."""

    text: str
    created_at: Optional[float] = None


@dataclass
class Note:
    """Context for the model from the bridge itself (what was carried, what was
    left out). Never shown as something the user said."""

    text: str
    created_at: Optional[float] = None


Turn = Union[UserTurn, AssistantTurn, ToolResult, Summary, Note]


@dataclass
class Session:
    source_agent: str
    source_id: str
    cwd: str
    title: Optional[str] = None
    model: Optional[str] = None
    created_at: Optional[float] = None
    updated_at: Optional[float] = None
    turns: list[Turn] = field(default_factory=list)
