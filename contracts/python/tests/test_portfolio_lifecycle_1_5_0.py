"""Lifecycle receipts may archive public citations without accepting storage paths."""
import pytest

from finplan_contracts.validate import validate


def activity(payload):
    return {"event_kind": "agent_turn", "correlation_id": "turn-1",
            "idempotency_key": "turn-1", "payload": payload}


def test_public_citation_is_archived_evidence():
    assert validate(activity({"sources": [{"url": "https://arxiv.org/abs/2501.00001"}]}),
                    "tools/record-agent-activity-request").valid


@pytest.mark.parametrize("location", ["s3:" + "//private-bucket/evidence", "arn:aws:" + "s3:::private-bucket",
                                     "https://private-bucket." + "s3.us-east-2.amazonaws.com/evidence",
                                     "/tmp/evidence", "../evidence"])
def test_activity_rejects_private_storage_and_paths(location):
    assert not validate(activity({"source": location}), "tools/record-agent-activity-request").valid


def test_citation_exception_does_not_change_other_request_fields():
    request = activity({"sources": []})
    request["event_kind"] = "https://example.com/event"
    assert not validate(request, "tools/record-agent-activity-request").valid
    assert not validate({"portfolio_id": "https://example.com/book"},
                        "tools/get-portfolio-history-request").valid
