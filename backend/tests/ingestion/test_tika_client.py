import pytest

from ingestion.tika.client import (
    TikaExtractionError,
    build_headers,
    parse_extraction_response,
)


def test_build_headers_pins_content_type_and_json_accept():
    headers = build_headers("application/pdf")
    assert headers == {"Content-Type": "application/pdf", "Accept": "application/json"}


def test_parse_extraction_response_takes_top_level_entry_only():
    payload = [
        {"X-TIKA:content": "  hello world  ", "Content-Type": "application/pdf"},
        {"X-TIKA:content": "embedded image caption"},
    ]
    doc = parse_extraction_response(payload)
    assert doc.text == "hello world"
    assert doc.metadata["Content-Type"] == "application/pdf"


def test_parse_extraction_response_defaults_missing_content_to_empty_string():
    doc = parse_extraction_response([{"Content-Type": "text/plain"}])
    assert doc.text == ""


def test_parse_extraction_response_rejects_empty_list():
    with pytest.raises(TikaExtractionError):
        parse_extraction_response([])
