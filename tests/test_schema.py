import json

import pytest
from pydantic import ValidationError

from agent_scheduler.domain.models import (
    ExecutionPlan,
    PrepareManifest,
    ProposalFacts,
    ProtocolEnvelope,
    Review,
    Task,
)


@pytest.mark.parametrize(
    "model", [ProposalFacts, Review, Task, PrepareManifest, ExecutionPlan, ProtocolEnvelope]
)
def test_persisted_and_protocol_schemas_forbid_unknown_fields(model):
    schema = model.model_json_schema()
    assert schema["additionalProperties"] is False


def test_unknown_protocol_field_is_rejected():
    value = {
        "schema_version": "v1",
        "message_id": "msg_01900000000070008000000000000000",
        "sequence": 1,
        "message_type": "PING",
        "payload": {},
        "unknown": True,
    }
    with pytest.raises(ValidationError):
        ProtocolEnvelope.model_validate_json(json.dumps(value))


def test_unknown_schema_version_is_rejected():
    value = {
        "schema_version": "v2",
        "message_id": "msg_01900000000070008000000000000000",
        "sequence": 1,
        "message_type": "PING",
        "payload": {},
    }
    with pytest.raises(ValidationError):
        ProtocolEnvelope.model_validate_json(json.dumps(value))
