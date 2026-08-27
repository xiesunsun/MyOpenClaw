import pytest

from pickel.providers.response_json import provider_response_json


def test_provider_response_json_rejects_sensitive_fields() -> None:
    with pytest.raises(TypeError, match="敏感字段"):
        provider_response_json({"id": "response-1", "api_key": "secret"})


def test_provider_response_json_does_not_walk_arbitrary_object_dict() -> None:
    class Response:
        def __init__(self) -> None:
            self.id = "response-1"

    with pytest.raises(TypeError, match="不可 JSON 化"):
        provider_response_json(Response())
