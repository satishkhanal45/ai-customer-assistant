def next_frontier(
    current_docs_links: tuple[tuple[str, ...], ...],
    visited: frozenset[str],
    max_pages: int,
) -> frozenset[str]:
    candidates = frozenset(link for links in current_docs_links for link in links)
    budget = max_pages - len(visited)
    unseen = candidates - visited
    return frozenset(tuple(unseen)[: max(budget, 0)])