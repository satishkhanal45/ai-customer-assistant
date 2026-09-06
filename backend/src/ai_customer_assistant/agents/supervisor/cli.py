"""Manual test entry point: run a single query through the Supervisor graph.

Usage:
    python -m ai_customer_assistant.agents.supervisor.cli "Hi there"
    python -m ai_customer_assistant.agents.supervisor.cli "Create a ticket" --provider groq
    python -m ai_customer_assistant.agents.supervisor.cli "ticket" --provider groq --attempts 2

Requires GROQ_API_KEY when --provider groq is used. Loaded from backend/.env
by `config.load_env` when this runs as a command, or read from the process
environment directly (e.g. `export GROQ_API_KEY=...`).
--provider stub (the default) needs no key and no network — it always
returns the same deterministic classification, so it only exercises graph
wiring, not real intent detection.
"""
from __future__ import annotations

import argparse
import json
import os
import uuid

from langgraph.checkpoint.memory import MemorySaver

from config import load_env

from .graph import build_supervisor_graph
from .llm_client import build_llm_client


def _parse_args() -> argparse.Namespace:
    default_provider = "groq" if os.environ.get("GROQ_API_KEY") else "stub"
    parser = argparse.ArgumentParser(description="Run one query through the Supervisor agent.")
    parser.add_argument("query", help="The customer message to classify/route.")
    parser.add_argument(
        "--provider",
        default=default_provider,
        choices=["stub", "gemini", "groq"],
        help="Which SupervisorLLMClient implementation to use (default: %(default)s).",
    )
    parser.add_argument(
        "--attempts",
        type=int,
        default=0,
        help="Simulate prior clarification_attempts (default: 0).",
    )
    parser.add_argument(
        "--thread-id",
        default=None,
        help="Conversation/session id. When set, compile the graph with a "
        "MemorySaver checkpointer keyed by this id so state can be resumed.",
    )
    parser.add_argument(
        "--trace-id",
        default=None,
        help="Correlation id threaded into SupervisorState. Defaults to a "
        "fresh UUID4 hex per run.",
    )
    return parser.parse_args()


def main() -> None:
    # Loading .env happens here, not at import: this module is only an entry
    # point when run as a command, and importing it must not mutate the
    # process environment (see config.load_env).
    load_env()

    args = _parse_args()
    client = build_llm_client(args.provider)
    checkpointer = MemorySaver() if args.thread_id else None
    graph = build_supervisor_graph(llm_client=client, checkpointer=checkpointer)
    trace_id = args.trace_id or uuid.uuid4().hex

    input_state = {
        "user_message": args.query,
        "conversation_history": [],
        "clarification_attempts": args.attempts,
        "trace_id": trace_id,
    }
    config = (
        {"configurable": {"thread_id": args.thread_id, "trace_id": trace_id}}
        if checkpointer
        else None
    )

    result = graph.invoke(input_state, config=config) if config else graph.invoke(input_state)

    printable = {key: str(value) for key, value in result.items()}
    print(json.dumps(printable, indent=2))


if __name__ == "__main__":
    main()