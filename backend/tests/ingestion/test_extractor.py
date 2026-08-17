import pytest

import trafilatura

from ingestion.crawler import extractor


@pytest.fixture(autouse=True)
def _stub_trafilatura(monkeypatch):
    def fake_extract(html_text, **kwargs):
        return "# Our Story\n\nSome prose about the company.\n"

    monkeypatch.setattr(trafilatura, "extract", fake_extract)


TEAM_HTML = """
<html><body>
<h2 class="heading">Meet the Team</h2>
<div class="team-members">
  <figure><img src="/a.jpg" alt="Justin Flores"></figure>
  <div class="team-member-wrapper">
    <div class="member-name">Justin Flores</div>
    <div class="member-designation pb-3">Advisor, Alpinist Studios</div>
  </div>
  <div class="team-member-wrapper">
    <div class="member-name">John Chhetri</div>
    <div class="member-designation pb-3">Founder CEO, Alpinist Studios</div>
  </div>
</div>
</body></html>
"""


def test_extract_markdown_appends_team_members():
    markdown = extractor.extract_markdown(TEAM_HTML, "https://example.com/about/")
    assert "Justin Flores — Advisor, Alpinist Studios" in markdown
    assert "John Chhetri — Founder CEO, Alpinist Studios" in markdown
    assert markdown.startswith("# Our Story")
    assert "## Team Members" in markdown


def test_extract_markdown_unchanged_without_team_markup():
    html_text = "<html><body><p>Just prose, no team section.</p></body></html>"
    markdown = extractor.extract_markdown(html_text, "https://example.com/")
    assert markdown == "# Our Story\n\nSome prose about the company.\n"


def test_team_member_parser_pairs_role_with_name():
    parser = extractor._TeamMemberParser()
    parser.feed(TEAM_HTML)
    assert parser.members == [
        ("Justin Flores", "Advisor, Alpinist Studios"),
        ("John Chhetri", "Founder CEO, Alpinist Studios"),
    ]


def test_extract_markdown_raises_when_trafilatura_returns_none(monkeypatch):
    monkeypatch.setattr(trafilatura, "extract", lambda *a, **k: None)
    with pytest.raises(extractor.ExtractionError):
        extractor.extract_markdown("<html></html>", "https://example.com/")
