"""RAG chat: embed the query with the same BGE model used at ingestion, do a
cosine-similarity search over ``embedding_chunk`` (pgvector), and answer
with Groq grounded on the retrieved excerpts.
"""

from __future__ import annotations

import os
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langchain_groq import ChatGroq
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

_EMBEDDER_CACHE: dict[str, Any] = {}


def _embed_model() -> Any:
    name = os.environ.get("INGESTION_EMBEDDING_MODEL_NAME", "BAAI/bge-base-en-v1.5")
    if _EMBEDDER_CACHE.get("name") != name:
        from sentence_transformers import SentenceTransformer

        _EMBEDDER_CACHE["name"] = name
        _EMBEDDER_CACHE["model"] = SentenceTransformer(name)
    return _EMBEDDER_CACHE["model"]


def _embed(texts: list[str]) -> list[list[float]]:
    vectors = _embed_model().encode(texts, normalize_embeddings=True, convert_to_numpy=True)
    return [list(map(float, vector)) for vector in vectors]


_RETRIEVAL_SQL = text(
    """
    SELECT chunk_id, text, page
    FROM embedding_chunk
    ORDER BY embedding <=> CAST(:q AS vector)
    LIMIT :k
    """
)


async def retrieve_chunks(
    session: AsyncSession, query: str, top_k: int = 6
) -> list[dict[str, Any]]:
    vector = _embed([query])[0]
    rows = await session.execute(
        _RETRIEVAL_SQL, {"q": str(vector), "k": min(max(top_k, 1), 20)}
    )
    return [
        {"chunk_id": str(row.chunk_id), "text": row.text, "page": row.page}
        for row in rows
    ]


def _chat_model() -> ChatGroq:
    return ChatGroq(
        model=os.environ.get("EAV_MODEL", "llama-3.3-70b-versatile"),
        temperature=0.2,
        api_key=os.environ.get("GROQ_API_KEY"),
    )


_SYSTEM = (
    "You are an assistant answering strictly from the provided document excerpts. "
    "Use the [n] source labels when referencing them. If the excerpts do not "
    "contain the answer, say you could not find it in the knowledge base."
)


async def answer(
    session: AsyncSession,
    message: str,
    history: list[dict[str, str]] | None = None,
    top_k: int = 6,
) -> dict[str, Any]:
    chunks = await retrieve_chunks(session, message, top_k)
    context = "\n\n".join(f"[{i + 1}] {c['text']}" for i, c in enumerate(chunks))

    messages: list[Any] = [SystemMessage(content=_SYSTEM + "\n\nContext:\n" + context)]
    for turn in history or []:
        role, content = turn.get("role", "user"), turn.get("content", "")
        messages.append(
            HumanMessage(content=content) if role == "user" else AIMessage(content=content)
        )
    messages.append(HumanMessage(content=message))

    reply = _chat_model().invoke(messages)

    return {
        "answer": str(reply.content),
        "sources": chunks,
        "matches": len(chunks),
    }