"""Thin CLI client (PRD §7.2).

Generates a ``session_id``, loops on stdin, POSTs to ``/chat``, and prints the
reply. **No logic lives here** — the Pi (or any future client) replaces this by
talking to the same endpoint. The service URL comes from the environment so the
client never needs the agent's config or credentials.
"""

from __future__ import annotations

import os
import sys
import uuid

import httpx


def _base_url() -> str:
    if url := os.environ.get("DEXTER_URL"):
        return url.rstrip("/")
    host = os.environ.get("DEXTER_HOST", "127.0.0.1")
    port = os.environ.get("DEXTER_PORT", "8000")
    return f"http://{host}:{port}"


def main() -> None:
    # Replies contain em-dashes; force UTF-8 so they render in any console.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass

    base_url = _base_url()
    session_id = str(uuid.uuid4())
    print("Dexter — ask about the next MBTA bus or train. Type 'quit' or Ctrl-C to exit.")

    with httpx.Client(base_url=base_url, timeout=30.0) as client:
        while True:
            try:
                message = input("you> ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                break
            if not message:
                continue
            if message.lower() in {"quit", "exit"}:
                break
            try:
                response = client.post("/chat", json={"session_id": session_id, "message": message})
                response.raise_for_status()
                print("dexter>", response.json()["reply"])
            except httpx.HTTPError as exc:
                print(f"dexter> (couldn't reach the service at {base_url}: {exc})")


if __name__ == "__main__":
    main()
