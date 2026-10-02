# CMN-C2-284 - Unit tests: ConfirmNode (inner Step 5)
# Adapted from the peer template tool-calling golden (Kaonavi -> LINE WORKS).
#
# Canon: invoked via node(state) (BaseNode.__call__ runs the trust gate, the
# input scan, execute(), then the output scan); inner domain node ->
# caller_trust_level = TrustLevel.ANONYMOUS.value.

import pytest

from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel

from src.nodes.confirm_node import ConfirmNode


@pytest.fixture(autouse=True)
def _mute_audit(monkeypatch):
    monkeypatch.setattr("src.nodes.confirm_node.emit_trace_event", lambda *a, **k: None)


def _state(**overrides) -> dict:
    state = {
        "record_id": "ms-204",
        "record_ref": "lineworks://messages/ms-204",
        "delivery_status": "delivered",
        "intent": "check_delivery",
        "caller_trust_level": TrustLevel.ANONYMOUS.value,
        "correlation_id": "confirm-test",
        "node_history": [],
        "error_log": [],
        "execution_time": {},
    }
    state.update(overrides)
    return state


class TestConfirmNode:
    def setup_method(self):
        self.node = ConfirmNode()

    def test_check_delivery_confirmation(self):
        result = self.node(_state())
        assert result["status"] == AgentStatus.SUCCESS.value
        assert "Checked LINE WORKS message delivery" in result["confirmation"]
        assert "status=delivered" in result["confirmation"]
        assert "ref=lineworks://messages/ms-204" in result["confirmation"]
        assert "id=ms-204" in result["confirmation"]
        assert result["result"]["record_id"] == "ms-204"
        assert result["result"]["record_ref"] == "lineworks://messages/ms-204"

    def test_send_verb(self):
        result = self.node(
            _state(
                intent="send_message",
                record_id="ms-9001",
                record_ref="lineworks://messages/ms-9001",
                delivery_status="",
            )
        )
        assert "Sent LINE WORKS message" in result["confirmation"]

    def test_list_activity_verb(self):
        result = self.node(
            _state(
                intent="list_activity",
                record_id="ch-101",
                record_ref="lineworks://channels/ch-101/activity",
                delivery_status="",
            )
        )
        assert "Listed recent LINE WORKS room activity" in result["confirmation"]

    def test_unknown_intent_uses_generic_verb(self):
        result = self.node(_state(intent="mystery"))
        assert "Processed LINE WORKS request" in result["confirmation"]

    def test_id_only_no_ref(self):
        result = self.node(_state(record_ref=""))
        assert result["status"] == AgentStatus.SUCCESS.value
        assert "id=ms-204" in result["confirmation"]
        assert "ref=" not in result["confirmation"]

    def test_empty_delivery_status_omits_status_part(self):
        result = self.node(_state(delivery_status=""))
        assert result["status"] == AgentStatus.SUCCESS.value
        assert "status=" not in result["confirmation"]

    def test_missing_record_evidence_errors(self):
        result = self.node(_state(record_id="", record_ref=""))
        assert result["status"] == AgentStatus.ERROR.value
        assert result["error_log"]
