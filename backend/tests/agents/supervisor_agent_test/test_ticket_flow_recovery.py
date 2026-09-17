"""The ticket flow must survive an answer that is not an email address.

A customer who is asked for an email and replies "wait i don't want to book
it" is behaving normally, not exceptionally. That input used to reach
``create_ticket`` unfiltered and raise ``InvalidEmailError`` out of the node,
which did far more damage than failing one turn:

* the buffered endpoint returned **HTTP 500** with an empty body;
* and the thread was **wedged permanently**. A crashed task stays in the
  checkpoint holding *both* its interrupt and its error, so every later
  message was delivered as a resume to the dead task, replayed the original
  stored text, and failed identically. Three turns later "hello" still
  raised ``InvalidEmailError: 'wait i donot want to book it'``.

Two fixes, tested separately because either alone leaves a hole: the node
no longer raises on ordinary input, and the serving layer no longer resumes
a task that carries an error.
"""

from __future__ import annotations

import pytest

from agents.supervisor.agents_wiring import (
    _MAX_EMAIL_ATTEMPTS,
    _TICKET_ABANDONED,
    _TICKET_CANCELLED,
    _TICKET_EMAIL_RETRY,
    _looks_like_cancellation,
)


# ---------------------------------------------------------------------------
# The cancellation predicate (pure)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "wait i donot want to book it",
        "nope changed my mind",
        "cancel",
        "Cancel please",
        "never mind",
        "nevermind",
        "forget it",
        "I don't want it",
        "no thanks",
        "stop",
    ],
)
def test_backing_out_is_recognised(text):
    assert _looks_like_cancellation(text) is True


@pytest.mark.parametrize(
    "text",
    [
        "satish@gmail.com",
        "to book frontend engineer",
        "i need help with billing",
        "hello",
        "",
        "   ",
    ],
)
def test_ordinary_answers_are_not_cancellations(text):
    assert _looks_like_cancellation(text) is False


def test_an_address_is_never_a_cancellation():
    """The markers are substrings, so an address that happens to contain one
    must not abandon the ticket it was supplied for."""
    assert _looks_like_cancellation("dont.want.spam@example.com") is False
    assert _looks_like_cancellation("cancel@example.com") is False


# ---------------------------------------------------------------------------
# The node — driven through a real compiled graph with a real checkpointer
# ---------------------------------------------------------------------------


@pytest.fixture
def ticket_graph():
    """A compiled Supervisor graph routed straight at the ticket node."""
    from langgraph.checkpoint.memory import MemorySaver

    from agents.supervisor.graph import build_supervisor_graph
    from agents.supervisor.llm_client import StubSupervisorLLMClient

    class _Client(StubSupervisorLLMClient):
        def classify(self, system_prompt, user_message, conversation_history):
            import json

            return json.dumps(
                {
                    "request_category": "DOMAIN_REQUEST",
                    "domain_confidence": 0.99,
                    "intent": "CREATE_TICKET",
                    "intent_confidence": 0.99,
                    "clarification_question": None,
                }
            )

    class _Ticket:
        ticket_id = "TCK-1"
        email = "someone@example.com"
        reason = "a reason"

    class _Ops:
        def __init__(self):
            self.created = []

        def call(self, query, reason):
            return {"query": query, "reason": reason}

        async def create_ticket(self, pending, email, idempotency_key=None):
            from agents.ticket_agent.validation import validate_email

            normalized = validate_email(email)  # raises InvalidEmailError
            self.created.append(normalized)
            t = _Ticket()
            t.email = normalized
            return t

    ops = _Ops()
    graph = build_supervisor_graph(
        llm_client=_Client(),
        ticket_ops=ops,
        checkpointer=MemorySaver(),
    )
    return graph, ops


async def _drive(graph, thread, messages):
    """Send messages the way ChatService does, returning each reply."""
    from langgraph.types import Command

    config = {"configurable": {"thread_id": thread}}
    replies = []
    for i, message in enumerate(messages):
        snapshot = await graph.aget_state(config)
        resumable = snapshot.next or any(
            getattr(t, "interrupts", ()) for t in snapshot.tasks
        )
        if resumable and i:
            result = await graph.ainvoke(Command(resume=message), config=config)
        else:
            result = await graph.ainvoke(
                {"user_message": message, "conversation_history": []}, config=config
            )
        if "__interrupt__" in result:
            payload = result["__interrupt__"][0].value
            replies.append(payload.get("question") or payload.get("query") or "")
        else:
            replies.append(result.get("final_response") or "")
    return replies


@pytest.mark.asyncio
async def test_backing_out_at_the_email_step_cancels_cleanly(ticket_graph):
    """The exact transcript that used to brick the thread."""
    graph, ops = ticket_graph

    replies = await _drive(
        graph,
        "t1",
        [
            "can you create me a ticket to book the frontend engineer?",
            "to book frontend engineer",
            "wait i donot want to book it",
        ],
    )

    assert replies[-1] == _TICKET_CANCELLED
    assert ops.created == []


@pytest.mark.asyncio
async def test_the_thread_is_usable_afterwards(ticket_graph):
    """The real damage was the *next* message, not the bad one."""
    graph, _ = ticket_graph

    replies = await _drive(
        graph,
        "t2",
        [
            "please open a support ticket",
            "i need help with billing",
            "nope changed my mind",
            "hello",
        ],
    )

    # "hello" starts a fresh turn instead of replaying the dead task.
    assert "not a valid email" not in replies[-1]
    assert replies[-1] != replies[-2]


@pytest.mark.asyncio
async def test_a_typo_is_re_prompted_then_accepted(ticket_graph):
    graph, ops = ticket_graph

    replies = await _drive(
        graph,
        "t3",
        [
            "open a ticket please",
            "my login is broken",
            "satish.gmail.com",          # missing the @
            "satish@gmail.com",
        ],
    )

    assert replies[2] == _TICKET_EMAIL_RETRY
    assert ops.created == ["satish@gmail.com"]
    assert "TCK-1" in replies[-1]


@pytest.mark.asyncio
async def test_it_gives_up_rather_than_pausing_forever(ticket_graph):
    """Unbounded retries would leave the thread paused with no way out."""
    graph, ops = ticket_graph

    replies = await _drive(
        graph,
        "t4",
        ["open a ticket", "a reason", *(["nonsense"] * _MAX_EMAIL_ATTEMPTS)],
    )

    assert replies[-1] == _TICKET_ABANDONED
    assert ops.created == []


@pytest.mark.asyncio
async def test_a_valid_email_still_books_on_the_first_try(ticket_graph):
    """The happy path is unchanged."""
    graph, ops = ticket_graph

    replies = await _drive(
        graph,
        "t5",
        ["I want to talk to a human", "my account is locked", "sam@example.com"],
    )

    assert ops.created == ["sam@example.com"]
    assert "TCK-1" in replies[-1]


# ---------------------------------------------------------------------------
# The serving-layer backstop, exercised with a node that still raises
# ---------------------------------------------------------------------------


class _Task:
    def __init__(self, name, interrupts=(), error=None):
        self.name = name
        self.id = "task-1"
        self.interrupts = interrupts
        self.error = error


class _Snapshot:
    def __init__(self, tasks, nxt=()):
        self.tasks = tasks
        self.next = nxt
        self.values = {"conversation_history": []}


class _Graph:
    def __init__(self, snapshot):
        self._snapshot = snapshot

    async def aget_state(self, config):
        return self._snapshot


def _service(snapshot):
    from services.chat_service import ChatService

    return ChatService(
        graph=_Graph(snapshot),
        llm_client=None,
        embedding_model=None,
        domain_scope=None,
        session_factory=None,
    )


@pytest.mark.asyncio
async def test_a_paused_task_is_resumed():
    """The healthy case: an interrupt and no error means the customer is
    being waited on, so their message is the resume value."""
    from langgraph.types import Command

    svc = _service(_Snapshot([_Task("ticket_agent", interrupts=("ask",))]))

    _, _, graph_input = await svc._prepare_turn("t", "sam@example.com")

    assert isinstance(graph_input, Command)
    assert graph_input.resume == "sam@example.com"


@pytest.mark.asyncio
async def test_a_crashed_task_starts_a_fresh_turn_instead():
    """The regression: a failed task keeps its interrupt, so resuming on
    "has an interrupt" alone replays the input that already failed and
    wedges the thread for good."""
    svc = _service(
        _Snapshot(
            [
                _Task(
                    "ticket_agent",
                    interrupts=("ask",),
                    error=RuntimeError("InvalidEmailError: 'nonsense'"),
                )
            ]
        )
    )

    _, _, graph_input = await svc._prepare_turn("t", "what is alpinist studios?")

    assert graph_input == {
        "user_message": "what is alpinist studios?",
        "conversation_history": [],
    }
