"""Carry an agent session into another agent by converting its native format.

Readers turn a source agent's stored session into the neutral form in `ir`;
writers turn that form into a target agent's own session files, so the target
resumes the conversation as if it had held it all along.
"""

__version__ = "0.1.1"
