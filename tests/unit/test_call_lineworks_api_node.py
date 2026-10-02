# CMN-C2-284 - Unit tests: CallLineWorksApiNode (inner Step 4, tool side-effect)
#
# Canon: invoked via node(state) (BaseNode.__call__ runs the trust gate, the
# input scan, execute(), then the output scan); inner domain node ->
# caller_trust_level = TrustLevel.ANONYMOUS.value. The ONE documented
# exception: the config-override call passes a 2nd (config) argument, which
# __call__ cannot forward - that single test stays a DIRECT
# execute(state, config=...) call (an ANONYMOUS node, so the trust gate is not
# the subject).
#
# The node builds its client locally (nodes are no-arg), so error-path
# transports are exercised by monkeypatching the module's LineWorksClient symbol
# (our own module attribute - never a sys.modules stub of shared.*).

import time

import pytest

from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from framework.secrets.context import bound_secrets
from shared.secrets.inmemory_provider import InMemoryProvider

from src.nodes.call_lineworks_api_node import CallLineWorksApiNode
from src.services.lineworks_client import LineWorksApiError
from src.schemas.state import to_json


@pytest.fixture(autouse=True)
def _mute_audit(monkeypatch):
    monkeypatch.setattr("src.nodes.call_lineworks_api_node.emit_trace_event", lambda *a, **k: None)


def _state(**overrides) -> dict:
    state = {
        "lineworks_payload": to_json({"message_id": "ms-204"}),
        "intent": "check_delivery",
        "message_id": "ms-204",
        "caller_trust_level": TrustLevel.ANONYMOUS.value,
        "correlation_id": "call-lineworks-test",
        "session_id": "s1",
        "thread_id": "th1",
        "trace_id": "t1",
        "node_history": [],
        "error_log": [],
        "execution_time": {},
    }
    state.update(overrides)
    return state


class _FakeErrorClient:
    """Stands in for LineWorksClient: the status lookup raises the documented API error."""

    def __init__(self, *args, **kwargs):
        pass

    uses_stub_transport = True

    def get_message_status(self, message_id, api_token):
        raise LineWorksApiError(403, "forbidden by bot scope")


class _FakeLiveClient:
    """Stands in for LineWorksClient with a LIVE (non-stub) transport."""

    captured: dict = {}

    def __init__(self, *args, **kwargs):
        pass

    uses_stub_transport = False

    def get_message_status(self, message_id, api_token):
        _FakeLiveClient.captured = {"message_id": message_id, "api_token": api_token}
        return {"message_id": message_id, "status": "delivered"}


class TestCallLineWorksApiNode:
    def setup_method(self):
        self.node = CallLineWorksApiNode()

    def test_check_delivery_success_via_default_v1_stub(self):
        # Default transport = deterministic, network-free stub; no secret
        # provider bound -> the node runs on the documented stub placeholder.
        result = self.node(_state())
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["record_id"] == "ms-204"
        assert result["record_ref"] == "lineworks://messages/ms-204"
        assert result["message_id"] == "ms-204"
        assert result["delivery_status"] == "delivered"

    def test_send_success_via_default_v1_stub(self):
        state = _state(
            intent="send_message",
            channel_id="ch-101",
            message_id="",
            lineworks_payload=to_json({"channel_id": "ch-101", "content": {"type": "text", "text": "hello team"}}),
        )
        result = self.node(state)
        assert result["status"] == AgentStatus.SUCCESS.value
        # The stub send receipt carries a synthetic ms-* message id.
        assert result["record_id"].startswith("ms-")
        assert result["record_ref"] == f"lineworks://messages/{result['record_id']}"
        assert result["message_id"] == result["record_id"]
        assert result["channel_id"] == "ch-101"

    def test_list_activity_success_via_default_v1_stub(self):
        state = _state(
            intent="list_activity",
            channel_id="ch-101",
            message_id="",
            lineworks_payload=to_json({"channel_id": "ch-101", "limit": 10}),
        )
        result = self.node(state)
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["record_id"] == "ch-101"
        assert result["record_ref"] == "lineworks://channels/ch-101/activity"

    def test_lineworks_config_state_field_sets_base_url(self):
        # The inner graph injects the manifest `lineworks:` section as the JSON
        # lineworks_config state field; the stub transport still serves the call.
        state = _state(lineworks_config=to_json({"base_url": "https://lineworks.example.test/v1.0"}))
        result = self.node(state)
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["record_ref"] == "lineworks://messages/ms-204"

    def test_config_override_direct_execute_call(self):
        # Documented canon exception: execute(state, config=...) takes a 2nd
        # argument that __call__ cannot forward, so this ONE test calls execute
        # directly (an ANONYMOUS node - the trust gate is not the subject here).
        config = {"configurable": {"lineworks": {"base_url": "https://lineworks.example.test/v1.0"}}}
        result = self.node.execute(_state(), config=config)
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["record_id"] == "ms-204"

    def test_missing_payload_errors(self):
        result = self.node(_state(lineworks_payload=None))
        assert result["status"] == AgentStatus.ERROR.value
        assert result["error_log"]

    def test_check_delivery_with_unresolved_message_id_errors(self):
        state = _state(message_id="", lineworks_payload=to_json({"message_id": ""}))
        result = self.node(state)
        assert result["status"] == AgentStatus.ERROR.value
        assert any("unresolved message id" in entry for entry in result["error_log"])

    def test_send_with_unresolved_channel_errors(self):
        state = _state(
            intent="send_message",
            channel_id="",
            message_id="",
            lineworks_payload=to_json({"channel_id": "", "content": {"type": "text", "text": "hi"}}),
        )
        result = self.node(state)
        assert result["status"] == AgentStatus.ERROR.value
        assert any("unresolved channel" in entry for entry in result["error_log"])

    def test_send_with_empty_text_errors(self):
        state = _state(
            intent="send_message",
            channel_id="ch-101",
            message_id="",
            lineworks_payload=to_json({"channel_id": "ch-101", "content": {"type": "text", "text": ""}}),
        )
        result = self.node(state)
        assert result["status"] == AgentStatus.ERROR.value
        assert any("empty message text" in entry for entry in result["error_log"])

    def test_list_activity_with_unresolved_channel_errors(self):
        state = _state(
            intent="list_activity",
            channel_id="",
            message_id="",
            lineworks_payload=to_json({"channel_id": "", "limit": 10}),
        )
        result = self.node(state)
        assert result["status"] == AgentStatus.ERROR.value
        assert any("cannot list room activity" in entry for entry in result["error_log"])

    def test_unknown_intent_errors(self):
        result = self.node(_state(intent="delete_message"))
        assert result["status"] == AgentStatus.ERROR.value
        assert any("unknown intent" in entry for entry in result["error_log"])

    def test_api_error_surfaces_status_error(self, monkeypatch):
        monkeypatch.setattr("src.nodes.call_lineworks_api_node.LineWorksClient", _FakeErrorClient)
        result = self.node(_state())
        assert result["status"] == AgentStatus.ERROR.value
        assert any("403" in entry for entry in result["error_log"])

    def test_live_transport_without_secret_refuses_call(self, monkeypatch):
        # With a LIVE transport a missing LINEWORKS_TOKEN is a hard error -
        # a real API is never called unauthenticated.
        monkeypatch.setattr("src.nodes.call_lineworks_api_node.LineWorksClient", _FakeLiveClient)
        result = self.node(_state())
        assert result["status"] == AgentStatus.ERROR.value
        assert any("unauthenticated" in entry for entry in result["error_log"])

    def test_live_transport_reads_token_from_ctx_secrets(self, monkeypatch):
        monkeypatch.setattr("src.nodes.call_lineworks_api_node.LineWorksClient", _FakeLiveClient)
        _FakeLiveClient.captured = {}
        with bound_secrets(InMemoryProvider({"LINEWORKS_TOKEN": "mock-token-for-testing"})):
            result = self.node(_state())
        assert result["status"] == AgentStatus.SUCCESS.value
        assert _FakeLiveClient.captured["api_token"] == "mock-token-for-testing"
        assert _FakeLiveClient.captured["message_id"] == "ms-204"

    def test_audit_emits_side_effect_signals_only(self, monkeypatch):
        events = []
        monkeypatch.setattr(
            "src.nodes.call_lineworks_api_node.emit_trace_event",
            lambda *args, **kwargs: events.append(args),
        )
        self.node(_state())
        payloads = {args[0]: args[1] for args in events}
        # Emit-spy asserts on the payload (args[1]) - presence signals only.
        payload = payloads["call_lineworks_api_complete"]
        assert payload["intent"] == "check_delivery"
        assert payload["has_record_id"] is True
        assert payload["stub_transport"] is True


class TestCallDeadline:
    """The declared call deadline must have a live reader, and fail closed."""

    def setup_method(self):
        self.node = CallLineWorksApiNode()

    def test_declared_deadline_is_enforced_on_a_slow_transport(self, monkeypatch):
        class _SlowClient:
            uses_stub_transport = True

            def __init__(self, *args, **kwargs):
                pass

            def get_message_status(self, message_id, api_token):
                time.sleep(0.02)
                return {"message_id": message_id, "status": "delivered"}

        monkeypatch.setattr("src.nodes.call_lineworks_api_node.LineWorksClient", _SlowClient)
        state = _state(lineworks_config=to_json({"base_url": "https://x.test", "timeout_s": 0}))
        # timeout_s=0 is below the supported floor, so it degrades to the
        # documented default rather than being honoured as "no time at all".
        assert self.node(state)["status"] == AgentStatus.SUCCESS.value

        # A deadline the call cannot meet discards the result instead of using it.
        monkeypatch.setattr("src.nodes.call_lineworks_api_node._DEFAULT_TIMEOUT_S", 0)
        state = _state(lineworks_config=to_json({"base_url": "https://x.test", "timeout_s": "not-a-number"}))
        result = self.node(state)
        assert result["status"] == AgentStatus.ERROR.value
        assert "deadline" in result["error_log"][0]
        assert "record_id" not in result

    @pytest.mark.parametrize("bad", ["NaN", "Infinity", float("nan"), float("inf"), True, 0, 601, "x"])
    def test_a_non_finite_or_out_of_range_deadline_degrades_to_the_default(self, bad):
        """NaN is the sharp edge: every comparison against it is False, so an
        unchecked value would silently disable the deadline it declares."""
        state = _state(lineworks_config=to_json({"base_url": "https://x.test", "timeout_s": bad}))
        result = self.node(state)
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["record_id"] == "ms-204"

    def test_a_declared_in_range_deadline_is_used(self):
        state = _state(lineworks_config=to_json({"base_url": "https://x.test", "timeout_s": 45}))
        assert self.node(state)["status"] == AgentStatus.SUCCESS.value
