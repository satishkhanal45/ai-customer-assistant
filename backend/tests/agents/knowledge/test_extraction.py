from agents.knowledge import extraction
from agents.knowledge.types import QueryFilter


def test_parse_filters_accepts_a_single_dict():
    parsed = extraction._parse_filters({"field": "organization", "value": "Alpinist Studios"})
    assert parsed == (QueryFilter(field="organization", value="Alpinist Studios"),)


def test_parse_filters_drops_malformed_entries():
    parsed = extraction._parse_filters(
        [
            {"field": "organization", "value": "Alpinist Studios"},
            {"field": "team"},
            "garbage",
            42,
        ]
    )
    assert parsed == (QueryFilter(field="organization", value="Alpinist Studios"),)


def test_parse_filters_none_and_non_list_are_empty():
    assert extraction._parse_filters(None) == ()
    assert extraction._parse_filters("not a list") == ()


def test_parse_filters_well_formed_list_passes_through():
    raw = [{"field": "a", "value": "1"}, {"field": "b", "value": "2"}]
    assert extraction._parse_filters(raw) == (
        QueryFilter(field="a", value="1"),
        QueryFilter(field="b", value="2"),
    )
