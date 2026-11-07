"""Mid-turn delivery hook for the CLIs without live input: Codex runs it after each tool call (PostToolUse), agy
before each model call (PreInvocation). Neither is continued at its stop, as its final reply keeps only its last
message: what arrives after the last tool call starts the next turn.

It runs only while the session's mailbox flag exists ($LOOP_MAILBOX, which the platform creates when something
waits for the session), takes what waits through the `loop` API and hands it to the CLI in that CLI's hook format.
It never breaks the agent: on any failure it hands nothing over, and what waits reaches the session later.
"""
from __future__ import annotations

import json
import sys

LEAD = "New message, sent while you were working:\n\n"


def respond(event: dict, text: str) -> dict | None:
    """The hook output that gives `text` to the CLI for this event, or None when the event cannot carry it."""
    if "invocationNum" in event:  # agy PreInvocation: a user message before the next model call
        return {"injectSteps": [{"userMessage": text}]}
    if event.get("hook_event_name") == "PostToolUse":  # Codex
        return {"hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": LEAD + text}}
    return None


def main() -> None:
    try:
        event = json.loads(sys.stdin.read() or "{}")
        if respond(event, "") is None:  # an event that cannot carry a message takes nothing
            return
        from .cli import call
        text = call("mail", {})
        if text:
            print(json.dumps(respond(event, text), ensure_ascii=False))
    except BaseException:  # SystemExit from `call` included: the agent goes on as if nothing waited
        return


if __name__ == "__main__":
    main()
