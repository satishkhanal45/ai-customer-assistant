from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class PGQueueSettings(BaseSettings):
    """
    The queue *is* knowledge_injection_job -- no separate queue table.
    See ingestion_flow.md step 2 (one-job-per-document guard, application
    level) and step 3 (worker claims a job).
    """

    model_config = SettingsConfigDict(env_prefix="PGQUEUE_")

    poll_interval_seconds: float = 2.0
    visibility_lock_id_namespace: int = 726346  # arbitrary constant for pg_advisory_lock
    max_jobs_per_poll: int = 1

    # How many documents this worker processes at once (`PGQUEUE_CONCURRENCY`).
    #
    # Lanes inside one process, sharing one copy of the embedding model --
    # a second *process* would load its own 400 MB copy, which is what made
    # concurrency look unaffordable before the model was hoisted to
    # process scope.
    #
    # Two rather than more, because the stage that dominates a document's wall
    # clock is extraction, and that is bound by a provider token budget rather
    # than by this worker. More lanes would mostly mean more 429s -- which the
    # retry policy absorbs, but spending quota on backoff is not throughput.
    # Set to 1 to restore strictly serial processing.
    concurrency: int = 2
