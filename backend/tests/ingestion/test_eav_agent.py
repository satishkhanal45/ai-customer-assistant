"""Unit tests for the JSON-mode EAV extraction agent.

Uses a fake Groq client -- no network/API calls -- to prove a single JSON
response is parsed and converted into a ChunkExtraction with entities, facts
and relations.
"""

import json
import types

from ingestion.extraction.agent import (
    ExtractionAgent,
    _coerce_value_type,
    _output_to_extraction,
    extract_chunk,
)
from ingestion.extraction.schema import ExtractionOutput


def _completion(content):
    return types.SimpleNamespace(
        choices=[types.SimpleNamespace(message=types.SimpleNamespace(content=content))]
    )


def _agent(content):
    class _FakeCompletions:
        def create(self, **kwargs):
            return _completion(content)

    class _FakeChat:
        completions = _FakeCompletions()

    class _FakeClient:
        chat = _FakeChat()

    return ExtractionAgent(client=_FakeClient(), model="fake-model")


def test_extract_chunk_parses_full_extraction():
    payload = {
        "entities": [{"entity_type": "Company", "name": "Alpinist"}],
        "attributes": [
            {"entity_type": "Company", "entity_name": "Alpinist", "attribute_name": "industry", "value": "technology", "value_type": "string"},
            {"entity_type": "Company", "entity_name": "Alpinist", "attribute_name": "founded_year", "value": 2019, "value_type": "number"},
        ],
        "relations": [
            {"source_entity_type": "Company", "source_entity_name": "Alpinist", "target_entity_type": "Employee", "target_entity_name": "Priya", "relation_type": "employs"},
        ],
    }
    result = extract_chunk(_agent(json.dumps(payload)), source_name="doc.md", chunk_index=0, chunk_text="...")
    assert result.entity == ("Company", "Alpinist")
    assert len(result.facts) == 2
    assert result.facts[0].attribute_name == "industry"
    assert result.facts[0].value == "technology"
    assert result.facts[1].attribute_name == "founded_year"
    assert result.facts[1].value == "2019"
    assert result.facts[1].value_type == "number"
    assert len(result.relations) == 1
    assert result.relations[0].relation_type == "employs"


def test_extract_chunk_without_entities_keeps_facts():
    payload = {
        "entities": [],
        "attributes": [
            {"entity_type": "Employee", "entity_name": "Priya", "attribute_name": "role", "value": "Backend Lead", "value_type": "string"},
        ],
        "relations": [],
    }
    result = extract_chunk(_agent(json.dumps(payload)), source_name="doc.md", chunk_index=0, chunk_text="...")
    assert result.entity is None
    assert len(result.facts) == 1
    assert result.facts[0].entity_name == "Priya"


def test_extract_chunk_handles_invalid_json():
    result = extract_chunk(_agent("not json at all"), source_name="doc.md", chunk_index=0, chunk_text="...")
    assert result.entity is None
    assert result.facts == ()
    assert result.relations == ()


def test_coerce_value_type_falls_back():
    assert _coerce_value_type("number", 5) == "number"
    assert _coerce_value_type("weird", "true") == "boolean"
    assert _coerce_value_type("weird", "42") == "number"
    assert _coerce_value_type("weird", "hello") == "string"


def test_output_to_extraction_canonicalizes_synonyms():
    output = ExtractionOutput.model_validate(
        {
            "entities": [{"entity_type": "organization", "name": "Alpinist"}],
            "attributes": [{"entity_type": "Company", "entity_name": "Alpinist", "attribute_name": "industry", "value": "tech", "value_type": "string"}],
            "relations": [],
        }
    )
    result = _output_to_extraction(0, output)
    assert result.entity == ("Company", "Alpinist")  # synonym canonicalized


def test_window_text_splits_long_chunks():
    from ingestion.extraction.agent import _window_text

    assert _window_text("short", 1800, 150) == ("short",)
    windows = _window_text("a" * 5000, 1800, 150)
    assert len(windows) == 3
    assert all(len(w) <= 1800 for w in windows)
    assert windows[0][-150:] == windows[1][:150]  # overlap preserved
    assert "".join(windows).count("a") >= 5000  # no content lost


def test_extract_chunk_merges_long_chunks():
    from ingestion.extraction.agent import _WINDOW_CHARS, _merge_windows
    from ingestion.pipeline_types import ChunkExtraction

    payload = {
        "entities": [{"entity_type": "Company", "name": "Alpinist"}],
        "attributes": [{"entity_type": "Company", "entity_name": "Alpinist", "attribute_name": "industry", "value": "tech", "value_type": "string"}],
        "relations": [],
    }
    long_text = "x" * (_WINDOW_CHARS * 2)
    result = extract_chunk(_agent(json.dumps(payload)), source_name="doc.md", chunk_index=0, chunk_text=long_text)
    assert result.entity == ("Company", "Alpinist")
    assert result.facts  # per-window facts merged
    assert result.chunk_index == 0