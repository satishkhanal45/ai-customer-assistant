"""Extraction batches several windows into one model call.

The system prompt carries the whole canonical vocabulary: 896 tokens of the
~1,346 sent for a single window, so two thirds of every call was the same
text again. On a per-minute token budget that is the difference between a
document finishing and a document timing out -- three documents in the
development corpus could not be ingested at all for exactly this reason.

What these pin is the contract, not the batch size: every window still gets
its own extraction, chunks that span several windows still merge, and a
window the model forgets costs that window rather than the document.
"""
from __future__ import annotations

import json
from dataclasses import dataclass

import pytest

from ingestion.extraction import agent as extraction_agent
from ingestion.extraction.agent import ExtractionAgent, extract_document


@dataclass
class _Chunk:
    chunk_index: int
    text: str


@dataclass
class _Embedded:
    chunk: _Chunk


def _doc(*texts: str) -> tuple:
    return tuple(_Embedded(_Chunk(i, t)) for i, t in enumerate(texts))


class _Recorder:
    """A Groq stand-in that records every call and answers from a script."""

    def __init__(self, responder):
        self.calls: list[dict] = []
        self._responder = responder
        self.chat = self
        self.completions = self

    def create(self, **kwargs):
        self.calls.append(kwargs)
        content = self._responder(kwargs)

        class _Msg:
            def __init__(self, c): self.content = c

        class _Choice:
            def __init__(self, c): self.message = _Msg(c)

        class _Resp:
            def __init__(self, c): self.choices = [_Choice(c)]

        return _Resp(content)


def _windows_in(kwargs) -> int:
    """How many windows the user message carried."""
    return kwargs["messages"][1]["content"].count("--- WINDOW ")


def _answer_every_window(kwargs) -> str:
    """A well-behaved model: one object per window, ids in order."""
    n = _windows_in(kwargs)
    return json.dumps({
        "windows": [
            {
                "window_id": i,
                "entities": [{"entity_type": "Company", "name": f"Co{i}"}],
                "attributes": [],
                "relations": [],
            }
            for i in range(n)
        ]
    })


def _agent(responder=_answer_every_window) -> tuple[ExtractionAgent, _Recorder]:
    client = _Recorder(responder)
    return ExtractionAgent(client=client, model="test-model"), client


@pytest.fixture(autouse=True)
def batch_of_three(monkeypatch):
    monkeypatch.setattr(extraction_agent, "_BATCH_WINDOWS", 3)


class TestCallCount:
    def test_three_short_chunks_take_one_call(self):
        """The whole point: three windows, one round trip."""
        agent, client = _agent()
        extract_document(agent, source_name="doc", chunks=_doc("a", "b", "c"))
        assert len(client.calls) == 1

    def test_batches_span_chunk_boundaries(self):
        """Batching within a chunk would leave short documents sending
        batches of one and throw most of the saving away."""
        agent, client = _agent()
        extract_document(agent, source_name="doc", chunks=_doc("a", "b", "c", "d", "e", "f"))
        assert len(client.calls) == 2

    def test_a_long_chunk_still_becomes_several_windows(self):
        """Windowing is why this model extracts well; batching must not
        quietly hand it one long passage instead."""
        agent, client = _agent()
        long_text = "x" * 5000                      # 3 windows at 1800/150
        extract_document(agent, source_name="doc", chunks=_doc(long_text))
        assert _windows_in(client.calls[0]) == 3

    def test_batch_size_one_restores_the_old_behaviour(self, monkeypatch):
        """The escape hatch, for a future model that batches worse."""
        monkeypatch.setattr(extraction_agent, "_BATCH_WINDOWS", 1)
        agent, client = _agent()
        extract_document(agent, source_name="doc", chunks=_doc("a", "b", "c"))
        assert len(client.calls) == 3


class TestTokenSaving:
    def test_the_vocabulary_is_sent_once_per_batch_not_once_per_window(self):
        """The measurable claim. The system prompt is the expensive part;
        batching is only worth doing if it is sent fewer times."""
        agent, client = _agent()
        extract_document(agent, source_name="doc", chunks=_doc("a", "b", "c"))

        system_prompts = [c["messages"][0]["content"] for c in client.calls]
        assert len(system_prompts) == 1
        assert "CANONICAL VOCABULARY" in system_prompts[0]

    def test_output_budget_grows_with_the_batch(self):
        """A fixed max_tokens truncates the response mid-JSON, which the
        model then reports as a validation failure -- a budget problem
        wearing a content problem's clothes."""
        agent, client = _agent()
        extract_document(agent, source_name="doc", chunks=_doc("a", "b", "c"))
        assert client.calls[0]["max_tokens"] >= 3 * 1500


class TestResultMapping:
    def test_every_chunk_gets_its_extraction(self):
        agent, _ = _agent()
        result = extract_document(agent, source_name="doc", chunks=_doc("a", "b", "c"))

        assert [e.chunk_index for e in result] == [0, 1, 2]
        assert all(e.entity is not None for e in result)

    def test_windows_of_one_chunk_are_merged(self):
        """A chunk spanning three windows is still one extraction."""
        agent, _ = _agent()
        result = extract_document(agent, source_name="doc", chunks=_doc("x" * 5000))

        assert len(result) == 1
        assert result[0].chunk_index == 0

    def test_results_are_mapped_by_window_id_not_by_luck(self):
        """Ids returned out of order must still land on the right window."""
        def reversed_ids(kwargs):
            n = _windows_in(kwargs)
            return json.dumps({
                "windows": [
                    {
                        "window_id": i,
                        "entities": [{"entity_type": "Company", "name": f"Co{i}"}],
                        "attributes": [], "relations": [],
                    }
                    for i in reversed(range(n))
                ]
            })

        agent, _ = _agent(reversed_ids)
        result = extract_document(agent, source_name="doc", chunks=_doc("a", "b", "c"))

        assert [e.entity[1] for e in result] == ["Co0", "Co1", "Co2"]

    def test_a_response_without_ids_falls_back_to_position(self):
        """Some responses come back correctly ordered but unlabelled.
        Discarding real extractions over a missing integer would be worse
        than trusting the order the model was asked for."""
        def no_ids(kwargs):
            n = _windows_in(kwargs)
            return json.dumps({
                "windows": [
                    {"entities": [{"entity_type": "Company", "name": f"Co{i}"}],
                     "attributes": [], "relations": []}
                    for i in range(n)
                ]
            })

        agent, _ = _agent(no_ids)
        result = extract_document(agent, source_name="doc", chunks=_doc("a", "b", "c"))
        assert [e.entity[1] for e in result] == ["Co0", "Co1", "Co2"]


class TestPartialFailure:
    def test_a_forgotten_window_costs_that_window_only(self):
        """Losing one window's facts is a smaller harm than failing the
        document -- and the document is what the queue retries."""
        def drops_the_middle(kwargs):
            return json.dumps({
                "windows": [
                    {"window_id": 0, "entities": [{"entity_type": "Company", "name": "Co0"}],
                     "attributes": [], "relations": []},
                    {"window_id": 2, "entities": [{"entity_type": "Company", "name": "Co2"}],
                     "attributes": [], "relations": []},
                ]
            })

        agent, _ = _agent(drops_the_middle)
        result = extract_document(agent, source_name="doc", chunks=_doc("a", "b", "c"))

        assert len(result) == 3
        assert result[0].entity[1] == "Co0"
        assert result[1].entity is None          # the dropped one
        assert result[2].entity[1] == "Co2"

    def test_unparseable_json_does_not_raise(self):
        """The 400-validation failures in production came back as junk; the
        document should end up empty, not exploded."""
        agent, _ = _agent(lambda kwargs: "not json at all")
        result = extract_document(agent, source_name="doc", chunks=_doc("a", "b"))

        assert len(result) == 2
        assert all(e.entity is None for e in result)

    def test_an_empty_document_makes_no_calls(self):
        agent, client = _agent()
        assert extract_document(agent, source_name="doc", chunks=()) == ()
        assert client.calls == []


class TestCooldownBound:
    def test_one_cooldown_cannot_eat_the_stage_budget(self):
        """It was 420s inside a 600s stage budget, so a single rate-limit
        cooldown consumed 70% of the time available for the whole document
        and a second exceeded it outright."""
        from timeouts import INGEST_EXTRACTION_STAGE_BUDGET_S

        assert extraction_agent._MAX_COOLDOWN_WAIT < INGEST_EXTRACTION_STAGE_BUDGET_S / 4


# ---------------------------------------------------------------------------
# A rejected batch costs a window, not a document.
#
# Two documents in the development corpus (`sdlc.pdf`, `tech_stck.pdf`) sat at
# zero chunks because one call in the batch came back 400 `json_validate_failed`
# with an *empty* `failed_generation` -- a response outgrowing what the model
# will emit for three windows at once, not unextractable content. The batch
# raised, and the whole document failed over it.
# ---------------------------------------------------------------------------

class _Rejected(Exception):
    """Groq's 400 for a response that did not match the requested schema."""

    def __init__(self, message: str | None = None) -> None:
        super().__init__(
            message
            or "Error code: 400 - {'error': {'message': \"Failed to validate "
               "JSON. Please adjust your prompt.\", 'code': 'json_validate_failed', "
               "'failed_generation': ''}}"
        )


def _reject_batches_larger_than(limit: int):
    """A model that cannot answer a wide request but handles a narrow one --
    the shape of the real failure."""

    def responder(kwargs):
        if _windows_in(kwargs) > limit:
            raise _Rejected()
        return _answer_every_window(kwargs)

    return responder


class TestRejectedBatchSplits:
    def test_a_rejected_batch_is_split_rather_than_lost(self):
        agent, client = _agent(_reject_batches_larger_than(1))

        result = extract_document(agent, source_name="doc", chunks=_doc("a", "b", "c"))

        # Every chunk still extracted, from the single-window retries.
        assert len(result) == 3
        assert all(e.entity is not None for e in result)
        # 1 rejected batch of three, then 2 and 1 (also rejected), then singles.
        assert [_windows_in(c) for c in client.calls] == [3, 1, 2, 1, 1]

    def test_only_the_window_that_cannot_be_extracted_is_lost(self):
        """The other case: one genuinely bad window. The split isolates it."""

        def responder(kwargs):
            if "POISON" in kwargs["messages"][1]["content"]:
                raise _Rejected()
            return _answer_every_window(kwargs)

        agent, _ = _agent(responder)

        result = extract_document(
            agent, source_name="doc", chunks=_doc("a", "POISON", "c")
        )

        by_index = {e.chunk_index: e for e in result}
        assert by_index[0].entity is not None
        assert by_index[2].entity is not None
        assert by_index[1].entity is None      # lost, but only this one

    def test_a_document_whose_every_window_is_rejected_still_fails(self):
        """Degrading to "extracted nothing" would mark the job SUCCEEDED with
        an empty graph -- indistinguishable from a document that genuinely had
        no facts in it, which is worse than failing."""
        from ingestion.extraction.agent import ExtractionFailed

        agent, _ = _agent(lambda kwargs: (_ for _ in ()).throw(_Rejected()))

        with pytest.raises(ExtractionFailed):
            extract_document(agent, source_name="doc", chunks=_doc("a", "b", "c"))

    def test_a_rate_limit_is_not_split(self, monkeypatch):
        """Splitting a throttled batch makes two throttled calls against a
        budget that is already gone. Only deterministic rejections split."""
        monkeypatch.setattr(extraction_agent.time, "sleep", lambda _: None)
        calls: list[int] = []

        def responder(kwargs):
            calls.append(_windows_in(kwargs))
            raise Exception("Error code: 429 - rate limit reached")

        agent, _ = _agent(responder)

        with pytest.raises(Exception, match="429"):
            extract_document(agent, source_name="doc", chunks=_doc("a", "b", "c"))

        # `_invoke_with_retry` retries the same width; nothing narrower is tried.
        assert set(calls) == {3}


class TestRetryPredicate:
    def test_a_schema_rejection_is_not_retried(self):
        """It was: the message contains the word "JSON", which satisfied the
        old `"json" in lower` clause. With temperature=0 and an identical
        prompt every attempt fails identically -- five times the tokens for a
        guaranteed failure, against the daily budget that was the constraint."""
        assert extraction_agent._is_retryable(_Rejected()) is False

    def test_a_rate_limit_is_still_retried(self):
        assert extraction_agent._is_retryable(
            Exception("Error code: 429 - rate limit reached for model")
        ) is True

    def test_a_schema_rejection_costs_one_call_per_width(self):
        """The token saving, measured: each width is tried once, not five
        times."""
        agent, client = _agent(lambda kwargs: (_ for _ in ()).throw(_Rejected()))

        with pytest.raises(Exception):
            extract_document(agent, source_name="doc", chunks=_doc("a", "b", "c"))

        # 3 -> (1, 2) -> the 2 splits into (1, 1): five calls, each a
        # different request. Under the old predicate the first would alone
        # have been five identical ones.
        assert len(client.calls) == 5
        assert [_windows_in(c) for c in client.calls] == [3, 1, 2, 1, 1]
