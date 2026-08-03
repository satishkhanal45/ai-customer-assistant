"""
Chunking stage: ExtractedDocument -> tuple[Chunk, ...].

Two independent strategies, selected by source_type:

    long_form   (PDF / DOCX / Markdown): structure-aware split first
                (at each HeadingMarker.position — heading text is already
                part of the sliced section, since HeadingMarker.position
                points at the heading's own start within document.text),
                then recursive token-window splitting for any section
                still over chunk_size_tokens.

    structured  (CSV / FAQ / DB record): "one row = one chunk, no
                splitting applied" (per confirmed pipeline diagram) —
                document.text becomes exactly one Chunk, unconditionally.

The exact source_type string values for each category are NOT owned by
this module (still unconfirmed upstream) — callers must supply
long_form_source_types / structured_source_types explicitly. A
source_type matching neither (or, defensively, both) set is a hard
error: UnknownSourceTypeError. Guessing which path to take for an
unrecognized type risks silently mis-chunking a document, which is
worse than failing loudly.

Known, disclosed limitations (not silent assumptions):

    - Page-range metadata (page_start/page_end) is resolved via an
      OPTIONAL injected `resolve_page_range` callable, since the shape
      of page-boundary data is owned by extraction, not this module.
      When a long-form section is further split into multiple token
      windows, every resulting chunk is resolved against its *parent
      section's* full character span, not each window's own exact
      sub-span — computing an exact per-window character offset would
      require token->character offset mapping from the tokenizer, which
      is a separate, unconfirmed capability. If exact per-window page
      ranges are needed later, resolve_page_range's inputs can be
      refined without changing this module's public signature.
    - An empty document (document.text == "") or an empty long-form
      section produces zero chunks, not one empty chunk — embedding an
      empty string is not meaningful downstream.
    - Heading metadata is SINGLE-LEVEL: each chunk records only its
      immediately owning heading (heading text + level), not the full
      ancestor breadcrumb (e.g. "Section 2 > Coverage > Liability").
      This is a real tradeoff, not just a minimalism default: in a
      deeply nested document, a chunk under a level-3 heading loses the
      level-1/level-2 headings above it in its own metadata. Chosen
      because the spec only asked for "heading/section path" as an
      example without specifying hierarchy tracking, and building a
      correct ancestor stack (popping headings whose level >= the
      current one, per standard heading-nesting rules) is nontrivial
      additional logic that hasn't been requested. If full breadcrumb
      context turns out to matter for retrieval quality, this is the
      function to revisit: _build_chunk's positional_metadata.
"""

from __future__ import annotations

from typing import Callable, Mapping

from transformers.tokenization_utils_base import PreTrainedTokenizerBase

from ingestion.chunk_embed.tokenizer import count_tokens, decode, encode, get_tokenizer
from ingestion.chunk_embed.types import Chunk, ExtractedDocument, HeadingMarker

# A section is (text, start_offset, end_offset, owning_marker_or_None).
_Section = tuple[str, int, int, HeadingMarker | None]

# Injected resolver: (start_char_offset, end_char_offset) -> extra metadata
# (e.g. {"page_start": 4, "page_end": 5}), or None to omit page metadata.
PageRangeResolver = Callable[[int, int], Mapping[str, object]]


class ChunkingError(Exception):
    """Base exception for this module."""


class UnknownSourceTypeError(ChunkingError):
    """
    Raised when document.source_type is not found in exactly one of the
    supplied long_form_source_types / structured_source_types sets.
    """


def chunk_document(
    document: ExtractedDocument,
    *,
    tokenizer: PreTrainedTokenizerBase,
    chunk_size_tokens: int,
    chunk_overlap_tokens: int,
    long_form_source_types: frozenset[str],
    structured_source_types: frozenset[str],
    resolve_page_range: PageRangeResolver | None = None,
) -> tuple[Chunk, ...]:
    """
    Split ``document`` into chunks according to its source_type category.

    Args:
        document: The document to chunk.
        tokenizer: A loaded tokenizer instance (see tokenizer.get_tokenizer),
                   passed in explicitly by the caller (pipeline.py) rather
                   than loaded here.
        chunk_size_tokens: Target chunk size, in tokens (see
                            IngestionSettings.chunk_size_tokens).
        chunk_overlap_tokens: Overlap between consecutive token windows,
                               in tokens (see
                               IngestionSettings.chunk_overlap_tokens).
        long_form_source_types: source_type values treated as long-form
                                 (structure-aware + recursive splitting).
        structured_source_types: source_type values treated as structured
                                  (no splitting; one chunk per document).
        resolve_page_range: Optional callable resolving a character span
                             to page-boundary metadata. Omitted from chunk
                             metadata entirely when not provided.

    Returns:
        An immutable, document-ordered tuple of Chunk instances.

    Raises:
        UnknownSourceTypeError: if document.source_type is not found in
            exactly one of long_form_source_types / structured_source_types.
    """
    category = _resolve_category(
        document.source_type, long_form_source_types, structured_source_types
    )
    handler = _CATEGORY_HANDLERS[category]
    return handler(
        document, tokenizer, chunk_size_tokens, chunk_overlap_tokens, resolve_page_range
    )


def _resolve_category(
    source_type: str,
    long_form_source_types: frozenset[str],
    structured_source_types: frozenset[str],
) -> str:
    """
    Determine which chunking category ``source_type`` belongs to.

    Declarative membership check (no if/elif chain): builds the set of
    matching category names, and requires exactly one match.
    """
    matches = tuple(
        name
        for name, is_member in (
            ("long_form", source_type in long_form_source_types),
            ("structured", source_type in structured_source_types),
        )
        if is_member
    )
    if len(matches) != 1:
        raise UnknownSourceTypeError(
            f"source_type={source_type!r} must appear in exactly one of "
            f"long_form_source_types or structured_source_types "
            f"(matched categories: {matches})"
        )
    return matches[0]


# ---------------------------------------------------------------------------
# Long-form: structure-aware split, then recursive token-window splitting.
# ---------------------------------------------------------------------------


def _handle_long_form(
    document: ExtractedDocument,
    tokenizer: PreTrainedTokenizerBase,
    chunk_size_tokens: int,
    chunk_overlap_tokens: int,
    resolve_page_range: PageRangeResolver | None,
) -> tuple[Chunk, ...]:
    """Structure-aware split into sections, then recursive windowing."""
    sections = _long_form_sections(document)

    # Flatten (window_text, owning_section) pairs across all sections,
    # preserving document order, before assigning any chunk_index.
    windowed_texts = tuple(
        (window_text, section)
        for section in sections
        for window_text in _windows_for_section(
            section[0], tokenizer, chunk_size_tokens, chunk_overlap_tokens
        )
    )

    return tuple(
        _build_chunk(document, chunk_index, window_text, section, tokenizer, resolve_page_range)
        for chunk_index, (window_text, section) in enumerate(windowed_texts)
    )


def _long_form_sections(document: ExtractedDocument) -> tuple[_Section, ...]:
    """
    Split document.text into sections at each HeadingMarker.position.

    If document.structure is None (extractor could not determine
    structure), the whole text is a single section with no owning marker,
    falling straight through to recursive splitting. Empty sections are
    dropped.
    """
    text = document.text
    structure = document.structure

    if not structure:
        return ((text, 0, len(text), None),) if text else ()

    positions = tuple(marker.position for marker in structure)
    has_preamble = positions[0] != 0

    boundaries = (0,) + positions if has_preamble else positions
    owning_markers = (None,) + structure if has_preamble else structure
    ends = boundaries[1:] + (len(text),)

    return tuple(
        (text[start:end], start, end, marker)
        for start, end, marker in zip(boundaries, ends, owning_markers)
        if text[start:end]
    )


def _windows_for_section(
    section_text: str,
    tokenizer: PreTrainedTokenizerBase,
    chunk_size_tokens: int,
    chunk_overlap_tokens: int,
) -> tuple[str, ...]:
    """
    Recursively split ``section_text`` into overlapping token windows.

    A section already at or under chunk_size_tokens is returned as a
    single-element tuple containing the original text unchanged (not a
    decode(encode(text)) round trip, which could introduce whitespace
    artifacts the tokenizer doesn't preserve exactly).
    """
    if not section_text:
        return ()

    token_ids = encode(section_text, tokenizer)
    if not token_ids:
        return ()
    if len(token_ids) <= chunk_size_tokens:
        return (section_text,)

    step = chunk_size_tokens - chunk_overlap_tokens  # > 0, enforced by config validation
    window_starts = tuple(range(0, len(token_ids), step))

    return tuple(
        decode(token_ids[start : start + chunk_size_tokens], tokenizer)
        for start in window_starts
    )


def _build_chunk(
    document: ExtractedDocument,
    chunk_index: int,
    text: str,
    section: _Section,
    tokenizer: PreTrainedTokenizerBase,
    resolve_page_range: PageRangeResolver | None,
) -> Chunk:
    """Assemble a single Chunk, merging inherited, positional, and page metadata."""
    _, start, end, marker = section

    inherited_metadata = {
        "source_type": document.source_type,
        **({"title": document.metadata["title"]} if "title" in document.metadata else {}),
    }
    # Single-level heading only (immediate owner, not full ancestor
    # breadcrumb) — see module docstring "Known, disclosed limitations"
    # for why this is a deliberate tradeoff, not an oversight.
    positional_metadata = {
        "heading": marker.heading_text if marker is not None else None,
        "heading_level": marker.level if marker is not None else None,
    }
    page_metadata = resolve_page_range(start, end) if resolve_page_range is not None else {}

    return Chunk(
        source_id=document.source_id,
        chunk_index=chunk_index,
        text=text,
        token_count=count_tokens(text, tokenizer),
        metadata={**inherited_metadata, **positional_metadata, **page_metadata},
    )


# ---------------------------------------------------------------------------
# Structured: one chunk per document, no splitting.
# ---------------------------------------------------------------------------


def _handle_structured(
    document: ExtractedDocument,
    tokenizer: PreTrainedTokenizerBase,
    chunk_size_tokens: int,  # unused: structured sources are never split
    chunk_overlap_tokens: int,  # unused: structured sources are never split
    resolve_page_range: PageRangeResolver | None,
) -> tuple[Chunk, ...]:
    """
    Wrap document.text as exactly one Chunk, unconditionally.

    Per the confirmed pipeline diagram: "One row = one chunk, no
    splitting applied." Whatever text arrives is the chunk's text,
    regardless of its token count.
    """
    text = document.text
    if not text:
        return ()

    inherited_metadata = {
        "source_type": document.source_type,
        **({"title": document.metadata["title"]} if "title" in document.metadata else {}),
    }
    page_metadata = (
        resolve_page_range(0, len(text)) if resolve_page_range is not None else {}
    )

    chunk = Chunk(
        source_id=document.source_id,
        chunk_index=0,
        text=text,
        token_count=count_tokens(text, tokenizer),
        metadata={**inherited_metadata, **page_metadata},
    )
    return (chunk,)


_CATEGORY_HANDLERS: Mapping[
    str,
    Callable[
        [ExtractedDocument, PreTrainedTokenizerBase, int, int, PageRangeResolver | None],
        tuple[Chunk, ...],
    ],
] = {
    "long_form": _handle_long_form,
    "structured": _handle_structured,
}
