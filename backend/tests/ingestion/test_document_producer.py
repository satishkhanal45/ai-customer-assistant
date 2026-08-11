"""
Regression test for a real bug: register_document_version's dispatch on the
dedup classification originally used `case type() if ...` (which only
matches if the subject IS the `type` class itself -- never true for a
dataclass instance), so every call silently fell through to the NewVersion
branch regardless of the actual classification, crashing with
AttributeError on NewDocument's missing .source_id.

The dispatch is now `_plan_version`, a pure function with no I/O -- these
tests exercise it directly, so this class of bug fails here instantly
instead of at ingestion time against a real Postgres.
"""

from __future__ import annotations

from uuid import uuid4

from db.models import KnowledgeSource
from ingestion.dedup import NewDocument, NewVersion
from ingestion.queue.document_producer import _plan_version


def test_new_document_plans_a_fresh_source_and_version_1():
    plan = _plan_version(
        NewDocument(), url="https://example.com/a.pdf", category_id=None, uploaded_by=uuid4()
    )

    assert isinstance(plan.new_source, KnowledgeSource)
    assert plan.source_id == plan.new_source.source_id
    assert plan.version_number == 1


def test_new_version_plans_no_new_source_and_increments():
    existing_source_id = uuid4()
    classification = NewVersion(source_id=existing_source_id, next_version_number=5)

    plan = _plan_version(
        classification, url="https://example.com/a.pdf", category_id=None, uploaded_by=uuid4()
    )

    assert plan.new_source is None  # this is exactly what the old bug got wrong
    assert plan.source_id == existing_source_id
    assert plan.version_number == 5
