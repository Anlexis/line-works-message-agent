# CMN-C2-284 - Unit tests: InferLineWorksFieldsNode (inner Step 3)
#
# Canon: invoked via node(state) (BaseNode.__call__ runs the trust gate, the
# input scan, execute(), then the output scan); inner domain node ->
# caller_trust_level = TrustLevel.ANONYMOUS.value. Positive payloads are
# PII-free: the framework's input mask rewrites Title-Case bigrams in
# validated_input, so prose message bodies stay lower-case and ids use the LINE
# WORKS short-id shapes (ch-101 / ms-204 / 2044).

import pytest

from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel

from src.nodes.infer_lineworks_fields_node import InferLineWorksFieldsNode
from src.schemas.state import from_json, to_json
from src.services.security import DEFAULT_ACTIVITY_LIMIT


@pytest.fixture(autouse=True)
def _mute_audit(monkeypatch):
    monkeypatch.setattr("src.nodes.infer_lineworks_fields_node.emit_trace_event", lambda *a, **k: None)


def _state(text: str, intent: str = "check_delivery", target_hint: str = "", **overrides) -> dict:
    state = {
        "validated_input": text,
        "intent": intent,
        "target_hint": target_hint,
        "caller_trust_level": TrustLevel.ANONYMOUS.value,
        "correlation_id": "infer-fields-test",
        "node_history": [],
        "error_log": [],
        "execution_time": {},
    }
    state.update(overrides)
    return state


class TestInferLineWorksFieldsNode:
    def setup_method(self):
        self.node = InferLineWorksFieldsNode()

    def test_check_delivery_extracts_message_id_from_text(self):
        result = self.node(_state("Check the delivery status of message ms-204 in channel ch-101."))
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["message_id"] == "ms-204"
        assert result["channel_id"] == "ch-101"
        # lineworks_payload is stored as a JSON string, not a native dict.
        assert isinstance(result["lineworks_payload"], str)
        assert from_json(result["lineworks_payload"], {}) == {"message_id": "ms-204"}

    def test_send_builds_content_payload(self):
        result = self.node(_state('Send a message to channel ch-101 saying "hello team"', intent="send_message"))
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["channel_id"] == "ch-101"
        payload = from_json(result["lineworks_payload"], {})
        assert payload == {"channel_id": "ch-101", "content": {"type": "text", "text": "hello team"}}

    def test_send_any_quoted_span_fallback(self):
        # No saying/message/text keyword before the quote: the any-quoted
        # fallback still recovers the message body.
        result = self.node(_state('Post to channel ch-101: "deploy done"', intent="send_message"))
        payload = from_json(result["lineworks_payload"], {})
        assert payload["content"]["text"] == "deploy done"

    def test_send_without_quoted_text_leaves_text_empty(self):
        # No quoted span: the body is left empty, never invented (the executor
        # surfaces the miss as status=error before any send).
        result = self.node(_state("Send an update to channel ch-101", intent="send_message"))
        assert result["status"] == AgentStatus.SUCCESS.value
        payload = from_json(result["lineworks_payload"], {})
        assert payload["content"]["text"] == ""

    def test_list_activity_builds_channel_payload(self):
        result = self.node(_state("Show the recent activity in room 2044", intent="list_activity"))
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["channel_id"] == "2044"
        assert from_json(result["lineworks_payload"], {}) == {"channel_id": "2044", "limit": 10}

    def test_id_shaped_hint_used_when_text_has_no_id(self):
        result = self.node(_state("Show the delivery state for the flagged item", target_hint="ms-204"))
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["message_id"] == "ms-204"
        assert from_json(result["lineworks_payload"], {}) == {"message_id": "ms-204"}

    def test_text_mention_wins_over_hint(self):
        result = self.node(_state("Check the delivery status of message ms-204", target_hint="ms-999"))
        assert result["message_id"] == "ms-204"

    def test_ms_prefixed_hint_never_used_as_channel(self):
        result = self.node(_state('Send a greeting saying "hi there"', intent="send_message", target_hint="ms-204"))
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["channel_id"] == ""  # unresolved - left empty, never invented

    def test_non_id_shaped_hint_left_unresolved(self):
        result = self.node(_state("Show the delivery state for the flagged item", target_hint="not a valid id!"))
        assert result["message_id"] == ""

    def test_missing_input_errors(self):
        result = self.node(_state(""))
        assert result["status"] == AgentStatus.ERROR.value
        assert result["error_log"]


class TestCallerSuppliedMessageData:
    """The caller's validated structured data must beat the request prose.

    The prose channel is masked at every node boundary, so it is the only one
    that can arrive corrupted; the structured channel is the one the send has
    to trust.
    """

    def setup_method(self):
        self.node = InferLineWorksFieldsNode()

    def test_caller_body_wins_over_a_quoted_span(self):
        state = _state(
            'Send to channel ch-101 saying "from the prose"',
            intent="send_message",
            caller_message=to_json({"text": "from the caller contract"}),
        )
        result = self.node(state)
        payload = from_json(result["lineworks_payload"], {})
        assert payload["content"]["text"] == "from the caller contract"

    def test_quoted_span_is_still_the_fallback(self):
        state = _state('Send to channel ch-101 saying "from the prose"', intent="send_message")
        payload = from_json(self.node(state)["lineworks_payload"], {})
        assert payload["content"]["text"] == "from the prose"

    def test_caller_page_size_reaches_the_request_body(self):
        state = _state(
            "list the recent activity in channel ch-101",
            intent="list_activity",
            caller_message=to_json({"limit": 42}),
        )
        payload = from_json(self.node(state)["lineworks_payload"], {})
        assert payload["limit"] == 42

    def test_page_size_defaults_when_the_caller_supplies_none(self):
        state = _state("list the recent activity in channel ch-101", intent="list_activity")
        payload = from_json(self.node(state)["lineworks_payload"], {})
        assert payload["limit"] == DEFAULT_ACTIVITY_LIMIT

    def test_caller_body_is_ignored_for_a_read_only_intent(self):
        state = _state(
            "check the delivery status of message ms-204",
            intent="check_delivery",
            caller_message=to_json({"text": "should not be sent"}),
        )
        payload = from_json(self.node(state)["lineworks_payload"], {})
        assert payload == {"message_id": "ms-204"}
