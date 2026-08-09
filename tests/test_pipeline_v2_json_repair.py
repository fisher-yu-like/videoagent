import json

import pytest

from pipeline_v2.json_repair import JSONRepairError, parse_json_response


def test_json_repair_unwraps_markdown_and_records_action() -> None:
    value, meta = parse_json_response("```json\n{\"ok\": true}\n```", label="test")
    assert value == {"ok": True}
    assert meta["repaired"] is True
    assert "markdown_fence_removed" in meta["actions"]


def test_json_repair_accepts_one_stray_trailing_delimiter() -> None:
    value, meta = parse_json_response('{"ok": true}}', label="test")
    assert value == {"ok": True}
    assert meta["repaired"] is True
    assert any(action in meta["actions"] for action in {"trailing_delimiter_removed", "json_repair_library"})


def test_json_repair_does_not_hide_a_second_json_document() -> None:
    with pytest.raises(JSONRepairError):
        parse_json_response('{"a": 1}{"b": 2}', label="test")
