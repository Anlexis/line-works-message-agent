"""AgentCore Platform v1.0 - inner LINE WORKS workflow graph (Cat 2 domain workflow).

Instantiated by LineWorksWorkflowGraphNode.get_subgraph() in graph.py. Inherits
BaseGraph directly for a fully custom linear topology:

    START -> validate_input -> classify_intent -> infer_lineworks_fields
          -> call_lineworks_api -> confirm -> END

Config (forwarded from the outer graph via _parent_config(), under
config["configurable"]):
    lineworks - config/config.yaml integration section (base_url, bot_id);
                merged with the call deadline and injected into State as the
                JSON `lineworks_config` field via _extra_initial_state() so the
                no-arg nodes can read it
    llm       - reserved for the documented LLM-synthesis follow-up (docs/02
                "Implementation Note"); unused in the deterministic pipeline
    timeout_s - the LINE WORKS call deadline, merged into `lineworks_config`
                and enforced by CallLineWorksApiNode

The validated caller request data (target id, message body, activity page size)
arrives over the ContextVar bridge rather than the config: GraphNode does not
forward the caller's input_context into the subgraph - see context_bridge.py.

Nodes are registered WITHOUT constructor arguments (nodes are no-arg; ctor args
raise TypeError at graph build).
"""

from typing import Any

from langgraph.graph import END, START

from framework.graph.base_graph import BaseGraph
from framework.schemas.agent_status import AgentStatus
from src.graph.context_bridge import get_caller_request_context
from src.nodes.validate_input_node import ValidateInputNode
from src.nodes.classify_intent_node import ClassifyIntentNode
from src.nodes.infer_lineworks_fields_node import InferLineWorksFieldsNode
from src.nodes.call_lineworks_api_node import CallLineWorksApiNode
from src.nodes.confirm_node import ConfirmNode
from src.schemas.state import State, to_json


class LineWorksWorkflowGraph(BaseGraph):
    """Inner graph: NL -> validate -> classify -> infer -> call -> confirm."""

    @property
    def name(self) -> str:
        return "lineworks_message_workflow"

    @property
    def state_schema(self) -> type:
        return State

    def _validate_config(self) -> None:
        # No mandatory config: the lineworks section is optional (the client
        # falls back to the documented default base_url and the network-free
        # stub transport), and a missing/unusable setting is handled at
        # CallLineWorksApiNode.execute() as a graceful status=error rather than
        # a compile-time crash.
        pass

    def register_nodes(self) -> None:
        # No super() - BaseGraph.register_nodes() is abstract. Do NOT register
        # initialize / finalize (outer backbone concern). All nodes are no-arg.
        self._nodes["validate_input"] = ValidateInputNode()
        self._nodes["classify_intent"] = ClassifyIntentNode()
        self._nodes["infer_lineworks_fields"] = InferLineWorksFieldsNode()
        self._nodes["call_lineworks_api"] = CallLineWorksApiNode()
        self._nodes["confirm"] = ConfirmNode()

    def add_edges(self) -> None:
        self._sg.add_edge(START, "validate_input")
        self._sg.add_edge("validate_input", "classify_intent")
        self._sg.add_edge("classify_intent", "infer_lineworks_fields")
        self._sg.add_edge("infer_lineworks_fields", "call_lineworks_api")
        self._sg.add_edge("call_lineworks_api", "confirm")
        self._sg.add_edge("confirm", END)

    def route(self, state: State) -> str:
        """Required by the base class. Linear topology -> never wired as a path.

        The annotation is this graph's OWN State on purpose. A routing callable
        is read as its input schema, and fields outside that schema are
        projected away before the callable sees them - so annotating a base
        state type here would make every domain field invisible to any future
        conditional edge that used this method, and the branch it guards would
        silently never be taken.
        """
        return END if state.get("status") == AgentStatus.ERROR.value else "confirm"

    def _extra_initial_state(self) -> "dict[str, Any]":
        """Seed the inner State with the forwarded config and the caller's request data.

        `lineworks_config` carries the integration settings plus the call
        deadline (JSON string - State values stay msgpack-safe) so the no-arg
        CallLineWorksApiNode can read them via state.get("lineworks_config").
        `target_hint` / `caller_message` carry the VALIDATED caller contract
        across the graph boundary (context_bridge.py).
        """
        configurable = self.config.get("configurable") or {}
        settings = dict(configurable.get("lineworks") or {})
        if "timeout_s" in configurable:
            settings["timeout_s"] = configurable["timeout_s"]

        extra: "dict[str, Any]" = {}
        if settings:
            extra["lineworks_config"] = to_json(settings)

        caller = get_caller_request_context()
        target_hint = str(caller.get("target_hint") or "")
        if target_hint:
            extra["target_hint"] = target_hint
        message = caller.get("message") or {}
        if message:
            extra["caller_message"] = to_json(message)
        return extra

    def get_output(self, state: State) -> "dict[str, Any]":
        return {
            "output": state.get("result") or state.get("confirmation"),
            "status": state.get("status"),
            "intent": state.get("intent", ""),
            "channel_id": state.get("channel_id", ""),
            "message_id": state.get("message_id", ""),
            "delivery_status": state.get("delivery_status", ""),
            "record_id": state.get("record_id", ""),
            "record_ref": state.get("record_ref", ""),
            "confirmation": state.get("confirmation", ""),
            "lineworks_payload": state.get("lineworks_payload", ""),
            "redaction_flags": state.get("redaction_flags", ""),
            "error_log": state.get("error_log", []),
            "trace_id": state.get("trace_id", ""),
            "correlation_id": state.get("correlation_id", ""),
            "node_history": state.get("node_history", []),
        }
