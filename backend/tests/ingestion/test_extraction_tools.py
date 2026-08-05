from ingestion.extraction.tools import tool_calls_to_extraction


def test_no_fact_found_yields_empty_extraction():
    result = tool_calls_to_extraction(
        0, [{"name": "NoFactFound", "args": {"reason": "nothing here"}}]
    )
    assert result.entity is None
    assert result.facts == ()
    assert result.relations == ()


def test_resolve_entity_then_attribute_and_relation():
    tool_calls = [
        {
            "name": "ResolveEntityArgs",
            "args": {"entity_type": "customer", "name": "Acme Co", "label": "Acme Co"},
        },
        {
            "name": "RecordAttributeValueArgs",
            "args": {
                "entity_type": "customer",
                "entity_name": "Acme Co",
                "namespace": "customer",
                "attribute_name": "plan_tier",
                "value": "enterprise",
            },
        },
        {
            "name": "RecordRelationArgs",
            "args": {
                "source_entity_type": "customer",
                "source_entity_name": "Acme Co",
                "target_entity_type": "product",
                "target_entity_name": "Alpinist Suite",
                "relation_type": "uses",
            },
        },
    ]

    result = tool_calls_to_extraction(2, tool_calls)

    assert result.chunk_index == 2
    assert result.entity == ("customer", "Acme Co")
    assert len(result.facts) == 1
    assert result.facts[0].attribute_name == "plan_tier"
    assert len(result.relations) == 1
    assert result.relations[0].relation_type == "uses"


def test_unknown_tool_name_is_ignored_not_raised():
    result = tool_calls_to_extraction(0, [{"name": "SomethingElse", "args": {}}])
    assert result.entity is None
