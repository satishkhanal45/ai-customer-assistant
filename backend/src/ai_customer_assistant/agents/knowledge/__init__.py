"""Knowledge agent: hybrid RAG retrieval + citation-grounded answer generation.

Public API re-exported here; internal modules (rewriting, extraction,
retrieval, ranking, generation) are wired together by
``graph.build_knowledge_agent_graph`` and are not part of the public
surface.
"""
from .config import KnowledgeAgentConfig
from .graph import build_knowledge_agent_graph
from .state import KnowledgeAgentState
from .types import GroundedResponse
from .vector_search import EmbeddingFunction

__all__ = [
    "KnowledgeAgentConfig",
    "build_knowledge_agent_graph",
    "KnowledgeAgentState",
    "GroundedResponse",
    "EmbeddingFunction",
]