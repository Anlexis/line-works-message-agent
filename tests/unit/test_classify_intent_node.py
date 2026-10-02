# CMN-C2-284 - Unit tests: ClassifyIntentNode (inner Step 2)
# Adapted from the peer template tool-calling golden (Kaonavi -> LINE WORKS).
# Intents: send_message / check_delivery / list_activity (deterministic
# keyword heuristic - no LLM; unknown falls back to the read-only
# check_delivery - never a send).
#
# Canon: invoked via node(state) (BaseNode.__call__ runs the trust gate, the
# input scan, execute(), then the output scan); inner domain node ->
# caller_trust_level = TrustLevel.ANONYMOUS.value.

import pytest

from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel

from src.nodes.classify_intent_node import ClassifyIntentNode


@pytest.fixture(autouse=True)
def _mute_audit(monkeypatch):
    monkeypatch.setattr("src.nodes.classify_intent_node.emit_trace_event", lambda *a, **k: None)


def _state(text: str) -> dict:
    return {
        "validated_input": text,
        "caller_trust_level": TrustLevel.ANONYMOUS.value,
        "correlation_id": "classify-intent-test",
        "node_history": [],
        "error_log": [],
        "execution_time": {},
    }


class TestClassifyIntentNode:
    def setup_method(self):
        self.node = ClassifyIntentNode()

    def test_keyword_check_delivery(self):
        result = self.node(_state("Check the delivery status of message ms-204 in channel ch-101."))
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["intent"] == "check_delivery"

    def test_keyword_send_message(self):
        result = self.node(_state('Send a message to channel ch-101 saying "deploy done"'))
        assert result["intent"] == "send_message"

    def test_keyword_list_activity(self):
        result = self.node(_state("Show the recent activity in room 2044"))
        assert result["intent"] == "list_activity"

    def test_send_keyword_wins_over_check(self):
        # Priority order is the write first: a "send ... then show the status"
        # style request classifies as the send, never the read.
        result = self.node(_state("Send the update to channel ch-101 and show the delivery status"))
        assert result["intent"] == "send_message"

    def test_no_signal_defaults_to_readonly_check_delivery(self):
        result = self.node(_state("please handle this for the team"))
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["intent"] == "check_delivery"
        # Non-fatal low-confidence note travels in error_log; status stays SUCCESS.
        assert any("defaulted to check_delivery" in entry for entry in result.get("error_log", []))

    def test_empty_input_errors(self):
        result = self.node(_state(""))
        assert result["status"] == AgentStatus.ERROR.value
        assert result["error_log"]

    def test_audit_emits_intent_label_only(self, monkeypatch):
        events = []
        monkeypatch.setattr(
            "src.nodes.classify_intent_node.emit_trace_event",
            lambda *args, **kwargs: events.append(args),
        )
        self.node(_state("Check the delivery status of message ms-204 in channel ch-101."))
        payloads = {args[0]: args[1] for args in events}
        # Emit-spy asserts on the payload (args[1]) - the label, never the text.
        assert payloads["classify_intent_complete"]["intent"] == "check_delivery"
        assert payloads["classify_intent_complete"]["defaulted"] is False
