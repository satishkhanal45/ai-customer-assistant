import trafilatura
from html.parser import HTMLParser
from trafilatura.metadata import extract_metadata

from .models import PageMeta
from .exception import ExtractionError

# Team-member markup class tokens that trafilatura routinely prunes as
# "non-main content". On the Alpinist theme these hold each person's name and
# role/post text; the roles otherwise vanish from the chunk corpus (the names
# survive only as image alt-texts). Matched as whole tokens so combined
# classes like `member-designation pb-3` still match.
_NAME_CLASS_TOKENS = frozenset({
    "member-name", "member_name", "team-member-name", "team_member_name",
})
_ROLE_CLASS_TOKENS = frozenset({
    "member-designation", "member_title", "member-role", "member_role",
    "team-member-role", "team_member_role", "designation", "position", "role",
})

_TEAM_SECTION_HEADING = "## Team Members"


def _clean_text(text: str) -> str:
    return " ".join(text.split())


class _TeamMemberParser(HTMLParser):
    """Extract ``(name, role)`` pairs from elements whose class tokens are in
    _NAME_CLASS_TOKENS / _ROLE_CLASS_TOKENS. A role is paired with the most
    recent unmatched name, so markup like
    ``<div class="member-name">X</div>
     <div class="member-designation">R</div>``
    yields ("X", "R")."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._open_tokens: list[frozenset[str]] = []
        self._buffers: list[str] = []
        self._pending_name: str | None = None
        self.members: list[tuple[str, str]] = []

    @staticmethod
    def _class_tokens(attrs) -> frozenset[str]:
        for key, value in attrs:
            if key == "class" and value:
                return frozenset(value.split())
        return frozenset()

    def handle_starttag(self, tag: str, attrs) -> None:
        self._open_tokens.append(self._class_tokens(attrs))
        self._buffers.append("")

    def handle_startendtag(self, tag: str, attrs) -> None:
        pass

    def handle_data(self, data: str) -> None:
        if self._buffers:
            self._buffers[-1] += data

    def handle_endtag(self, tag: str) -> None:
        if not self._open_tokens:
            return
        tokens = self._open_tokens.pop()
        text = _clean_text(self._buffers.pop() if self._buffers else "")
        if not text:
            return
        if tokens & _NAME_CLASS_TOKENS:
            self._pending_name = text
        elif tokens & _ROLE_CLASS_TOKENS and self._pending_name is not None:
            self.members.append((self._pending_name, text))
            self._pending_name = None


def _append_team_members(markdown: str, html_text: str) -> str:
    """Append name/role pairs trafilatura pruned from the page's main content.

    Team-member markup is routinely treated as noise by trafilatura, so the
    role/post text next to each person is otherwise lost entirely. Appending
    a clean ``## Team Members`` section keeps it in the chunks without
    pulling in navigation/footer noise. Unchanged input when no members are
    found."""
    parser = _TeamMemberParser()
    parser.feed(html_text)
    if not parser.members:
        return markdown
    lines = [_TEAM_SECTION_HEADING, ""]
    for name, role in parser.members:
        lines.append(f"- {name} — {role}")
    return f"{markdown}\n\n" + "\n".join(lines) + "\n"


def extract_markdown(html_text: str, url: str) -> str:
    markdown = trafilatura.extract(
        html_text,
        url=url,
        output_format="markdown",
        include_tables=True,
        include_links=True,
        include_images=False,
        favor_precision=True,
    )
    if markdown is None:
        raise ExtractionError(f"Trafilatura could not extract content from {url}")
    return _append_team_members(markdown, html_text)


def extract_meta(html_text: str, url: str) -> PageMeta:
    meta = extract_metadata(html_text, default_url=url)
    return PageMeta(
        title=(meta.title if meta and meta.title else url),
        canonical_url=(meta.url if meta and meta.url else url),
        description=(meta.description if meta and meta.description else ""),
    )
