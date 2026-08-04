"""FastAPI integration for the manual-upload path.

This module is intentionally thin: it adapts an HTTP request into an
``UploadCandidate``-producing call to ``uploader.handle_manual_upload`` and
maps the returned ``UploadOutcome`` to an HTTP response. All actual
decision-making (validation, dedup, storage, persistence) lives in
``storage/`` proper.
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, UploadFile
from sqlalchemy.ext.asyncio import AsyncSession

from . import uploader
from .client import StorageClient
from .config import StorageConfig
from .exceptions import UnsupportedMediaTypeError, UploadTooLargeError

router = APIRouter(prefix="/admin/knowledge-sources", tags=["knowledge-sources"])


# Maps each UploadOutcome variant to the HTTP response it produces. A
# dispatch table keyed by type, looked up via `type(outcome)`, instead of
# an isinstance-chained if/elif in the route handler.
def _created_response(outcome: uploader.Created) -> dict:
    return {
        "status": "created",
        "source_id": str(outcome.source_id),
        "version_id": str(outcome.version_id),
        "job_id": str(outcome.job_id),
        "storage_uri": outcome.storage_uri,
    }


def _duplicate_response(outcome: uploader.DuplicateSkipped) -> dict:
    return {
        "status": "duplicate_skipped",
        "source_id": str(outcome.source_id),
        "version_id": str(outcome.version_id),
    }


_SUCCESS_RESPONSES: dict[type, callable] = {
    uploader.Created: _created_response,
    uploader.DuplicateSkipped: _duplicate_response,
}

# HTTP status for each failure variant's underlying exception type.
_ERROR_STATUS: dict[type, int] = {
    UnsupportedMediaTypeError: 415,
    UploadTooLargeError: 413,
}


def _error_status(error: Exception) -> int:
    return _ERROR_STATUS.get(type(error), 502)


async def get_storage_config() -> StorageConfig:
    # In the real app this is a startup-time singleton injected via
    # FastAPI's dependency-override mechanism, not reconstructed per
    # request. Left as a placeholder dependency here.
    raise NotImplementedError("wire to the app's singleton StorageConfig")


async def get_storage_client(
    config: StorageConfig = Depends(get_storage_config),
) -> StorageClient:
    return StorageClient(config)


async def get_db_session() -> AsyncSession:
    # Placeholder — wire to the project's existing session-per-request
    # dependency.
    raise NotImplementedError("wire to the app's AsyncSession dependency")


async def get_current_admin_user_id() -> UUID:
    # Placeholder — wire to the project's existing auth dependency.
    raise NotImplementedError("wire to the app's auth dependency")


@router.post("")
async def upload_knowledge_source(
    file: UploadFile,
    target_source_id: UUID | None = None,
    session: AsyncSession = Depends(get_db_session),
    client: StorageClient = Depends(get_storage_client),
    config: StorageConfig = Depends(get_storage_config),
    admin_user_id: UUID = Depends(get_current_admin_user_id),
) -> dict:
    data = await file.read()

    outcome = await uploader.handle_manual_upload(
        session=session,
        client=client,
        config=config,
        data=data,
        filename=file.filename or "upload",
        mime_type=file.content_type or "application/octet-stream",
        uploaded_by=admin_user_id,
        target_source_id=target_source_id,
    )

    match outcome:
        case uploader.Rejected(error=error):
            raise HTTPException(status_code=_error_status(error), detail=str(error))
        case uploader.Failed(error=error):
            raise HTTPException(status_code=_error_status(error), detail=str(error))
        case uploader.Created() | uploader.DuplicateSkipped():
            return _SUCCESS_RESPONSES[type(outcome)](outcome)
