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
