"""LangGraph Studio dev tooling (see docs/langgraph_studio_implementation.md).

This module exists purely for local dev/debugging visualization. Nothing here
is referenced by the served application — production wiring stays in
``services.chat_service.build_chat_service`` and ``agents.supervisor.graph``.

DECISION (real vs fake dependencies)
------------------------------------
**Fakes/stubs** were chosen for Studio. Rationale: the real graph requires
Postgres reachable as ``postgres:5432`` from the process that runs it, but
that name only resolves inside docker-compose (the host exposes
``localhost:5433``) and the real RAG path needs a populated pgvector
knowledge base plus a one-time BGE ``SentenceTransformer`` download. Neither
is reliably available from the host where ``langgraph dev`` runs, so per the
doc's fallback guidance we default to fakes: deterministic stub classifier,
a fake Knowledge subgraph, and ``MemorySaver``. This is plenty to visualize
topology, routing, and the interrupt/resume flows, at fast startup with zero
external calls. The single real-vs-fake choice is made here, once, and the
fake ``knowledge_graph`` result is toggled by the input message so both the
straight-through and escalation paths can be driven from Studio's UI.

The construction reuses ``build_supervisor_graph`` and the existing
supervisor graph composition directly — no graph logic is re-implemented.

Running Studio
--------------
From the ``backend/`` directory, Studio requires the package root on the
import path (langgraph loads graph files standalone, so relative imports
won't resolve on their own):

    PYTHONPATH=src/ai_customer_assistant langgraph dev
"""
from __future__ import annotations

import json
from typing import Any, Mapping

from langgraph.checkpoint.memory import MemorySaver

from agents.knowledge.types import GroundedResponse
from agents.supervisor.graph import build_supervisor_graph
from agents.ticket_agent.store import TicketStore


class _StudioLLMClient:
    """Deterministic classifier for Studio.

    Mirrors the test double shape (``StubSupervisorLLMClient`` / the
    ``FakeClient`` in the supervisor tests): returns a fixed JSON payload so
    no network/LLM call happens. Classification is chosen from the message so
    Studio can exercise different routes from its UI:

      - greeting words -> GREETING (terminates at END directly);
      - an explicitly ``ungrounded`` message -> KNOWLEDGE_QUERY (routing to
        the fake Knowledge node, whose stub below yields an ungrounded
        answer, triggering the escalation-confirmation interrupt);
      - any other knowledge-ish message -> KNOWLEDGE_QUERY (grounded).
    """

    def classify(
        self,
        system_prompt: str,
        user_message: str,
        conversation_history: list,
    ) -> str:
        lowered = (user_message or "").lower()
        tokens = lowered.split()
        is_greeting = any(
            word.lower() in tokens for word in ("hello", "hi", "hey")
        )
        return json.dumps(
            {
                "request_category": "GREETING" if is_greeting else "DOMAIN_REQUEST",
                "domain_confidence": 0.99,
                "intent": "UNKNOWN" if is_greeting else "KNOWLEDGE_QUERY",
                "intent_confidence": 0.0 if is_greeting else 0.98,
                "clarification_question": None,
            }
        )


class _StudioKnowledgeGraph:
    """Fake Knowledge subgraph for Studio.

    Matches the shape ``make_knowledge_agent_node`` expects: an object with
    an ``ainvoke(input) -> Mapping`` whose ``response`` key is a
    ``GroundedResponse``. Groundedness is flipped by the input message so the
    both the straight-through (grounded) and escalation (ungrounded) paths
    are reachable from Studio.
    """

    _GROUNDED_ANSWER = (
        "Support plans are offered as Basic, Pro, and Enterprise tiers. "
        "This answer is grounded in the knowledge base."
    )
    _UNGROUNDED_ANSWER = (
        "I'm not entirely certain about that, and this answer isn't backed "
        "by a retrieved source."
    )

    async def ainvoke(self, state: Mapping[str, Any]) -> dict:
        query = str(state.get("raw_query", "")).lower()
        ungrounded = "ungrounded" in query
        return {
            "response": GroundedResponse(
                answer_text=self._UNGROUNDED_ANSWER
                if ungrounded
                else self._GROUNDED_ANSWER,
                is_grounded=not ungrounded,
                citations=(),
            )
        }


def build_supervisor_graph_for_studio():
    """Zero-argument Studio builder: returns a compiled Supervisor graph.

    No side effects beyond constructing and returning the graph. Uses
    ``build_supervisor_graph`` with fake/durable-in-memory dependencies (see
    module docstring for the real-vs-fake decision):
      - ``llm_client``: deterministic stub classifier;
      - ``knowledge_graph``: stub subgraph (grounded/ungrounded by message);
      - ``ticket_ops``: the in-memory ``TicketStore`` (same as the app default);
      - ``checkpointer``: ``MemorySaver``, the dev/test default.
    """
    return build_supervisor_graph(
        llm_client=_StudioLLMClient(),
        knowledge_graph=_StudioKnowledgeGraph(),
        ticket_ops=TicketStore(),
        checkpointer=MemorySaver(),
    )