# CMN-C2-284 - Unit tests: inner LineWorksWorkflowGraph (BaseGraph) contract.
# The compiled outer path is exercised end-to-end by
# tests/proof_of_boundary/test_pb_invoke_order.py; this module unit-checks the
# inner graph's identity, config forwarding, the caller-context bridge,
# routing, the output contract, and a direct inner invoke on the network-free
# stub transport.

import inspect

import pytest

from langgraph.graph import END

from framework.schemas.agent_status import AgentStatus

from src.graph.context_bridge import set_caller_request_context
from src.graph.domain_workflow_graph import LineWorksWorkflowGraph
from src.schemas.state import State, from_json


@pytest.fixture(autouse=True)
def _clear_bridge():
    """The bridge is a ContextVar - clear it so tests cannot leak into each other."""
    set_caller_request_context({})
    yield
    set_caller_request_context({})


def _graph(config=None):
    return LineWorksWorkflowGraph(config=config or {})


def test_inner_graph_identity():
    g = _graph()
    assert g.name == "lineworks_message_workflow"
    assert g.state_schema is State


def test_extra_initial_state_injects_lineworks_config_as_json():
    g = _graph({"configurable": {"lineworks": {"base_url": "https://lineworks.example.test/v1.0"}}})
    extra = g._extra_initial_state()
    # Forwarded as a JSON string, not a native dict (state stays msgpack-safe).
    assert isinstance(extra["lineworks_config"], str)
    assert from_json(extra["lineworks_config"], {}) == {"base_url": "https://lineworks.example.test/v1.0"}


def test_extra_initial_state_empty_without_lineworks_section():
    assert _graph()._extra_initial_state() == {}


def test_extra_initial_state_carries_the_call_deadline():
    """timeout_s is declared once in config/config.yaml and must reach the node."""
    g = _graph({"configurable": {"lineworks": {"base_url": "https://x.test"}, "timeout_s": 45}})
    settings = from_json(g._extra_initial_state()["lineworks_config"], {})
    assert settings["timeout_s"] == 45


def test_extra_initial_state_seeds_the_validated_caller_contract():
    """GraphNode does not forward input_context, so the bridge has to."""
    set_caller_request_context({"target_hint": "ch-101", "message": {"text": "hello", "limit": 7}})
    extra = _graph()._extra_initial_state()
    assert extra["target_hint"] == "ch-101"
    assert from_json(extra["caller_message"], {}) == {"text": "hello", "limit": 7}


def test_route_error_ends_graph():
    g = _graph()
    assert g.route({"status": AgentStatus.ERROR.value}) == END
    assert g.route({"status": AgentStatus.SUCCESS.value}) == "confirm"


def test_route_is_annotated_with_this_graphs_own_state():
    """A path callable's annotation IS its input schema.

    Anything outside it is projected away before the callable runs, so a
    base-state annotation would hide every domain field from a conditional
    branch while the unit suite - which passes a full dict - stayed green.
    """
    assert inspect.signature(LineWorksWorkflowGraph.route).parameters["state"].annotation is State


def test_get_output_surfaces_record_fields():
    g = _graph()
    out = g.get_output(
        {
            "result": {"record_id": "ms-204", "record_ref": "lineworks://messages/ms-204", "confirmation": "ok"},
            "status": AgentStatus.SUCCESS.value,
            "intent": "check_delivery",
            "channel_id": "ch-101",
            "message_id": "ms-204",
            "delivery_status": "delivered",
            "record_id": "ms-204",
            "record_ref": "lineworks://messages/ms-204",
            "confirmation": "ok",
            "lineworks_payload": "{}",
            "redaction_flags": "[]",
            "error_log": [],
            "trace_id": "tr",
            "correlation_id": "co",
            "node_history": ["ValidateInputNode", "ConfirmNode"],
        }
    )
    assert out["status"] == AgentStatus.SUCCESS.value
    assert out["intent"] == "check_delivery"
    assert out["record_ref"] == "lineworks://messages/ms-204"
    assert out["delivery_status"] == "delivered"
    assert out["confirmation"] == "ok"
    assert out["output"] == {"record_id": "ms-204", "record_ref": "lineworks://messages/ms-204", "confirmation": "ok"}


def test_get_output_carries_error_log():
    g = _graph()
    out = g.get_output({"status": AgentStatus.ERROR.value, "error_log": ["boom"], "confirmation": ""})
    assert out["status"] == AgentStatus.ERROR.value
    assert out["error_log"] == ["boom"]


def test_inner_graph_compiles():
    g = _graph()
    g.compile()
    assert g._compiled is not None


def test_inner_invoke_check_delivery_on_v1_stub():
    """Direct inner invoke (default ANONYMOUS ctx - every inner node declares
    ANONYMOUS): validate -> classify -> infer -> call(stub) -> confirm."""
    g = _graph({"configurable": {"lineworks": {"base_url": "https://www.worksapis.com/v1.0"}}})
    g.compile()
    result = g.invoke(user_input="Check the delivery status of message ms-204 in channel ch-101.")
    assert result["status"] == AgentStatus.SUCCESS.value
    assert result["record_id"] == "ms-204"
    assert result["record_ref"] == "lineworks://messages/ms-204"
    assert result["intent"] == "check_delivery"
    assert result["confirmation"]
    history = result.get("node_history", [])
    assert history == [
        "ValidateInputNode",
        "ClassifyIntentNode",
        "InferLineWorksFieldsNode",
        "CallLineWorksApiNode",
        "ConfirmNode",
    ]
