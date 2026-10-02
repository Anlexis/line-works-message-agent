"""AgentCore Platform v1.0 - caller-context bridge across the graph boundary."""

# Why this exists: GraphNode.execute() invokes the inner graph as
# `subgraph.invoke(user_input, session_id=..., ctx=...)` and does NOT forward
# outer state fields or the caller's input_context, so the validated request
# data collected by PreProcessNode (the target id, the message body and the
# activity page size) would never reach the inner workflow on its own. The
# sanctioned subclass hooks bridge it:
#
#   LineWorksWorkflowGraphNode.extract_input(state)  [runs BEFORE subgraph.invoke]
#       -> set_caller_request_context({"target_hint": ..., "message": ...})
#   LineWorksWorkflowGraph._extra_initial_state()    [runs INSIDE subgraph.invoke]
#       -> seeds {"target_hint": ..., "caller_message": <JSON>}
#
# What crosses the bridge is the VALIDATED caller contract produced by
# PreProcessNode - every value already passed its bounded, inert shape check -
# never the raw request body.
#
# The alternative, smuggling the data inside the validated_input JSON, is not
# reliable here: the framework masks that field at node boundaries, and a real
# message body trips the masking heuristics - a person's name written as two
# Title Case words ("Taro Yamada") is rewritten to "[MASKED]" and a long digit
# run is rewritten as a digit group, so the LINE WORKS send would carry
# corrupted caller data. This channel is not masked.
#
# A ContextVar keeps the hand-off correct per thread/task, so concurrent
# invocations in one process cannot see each other's request data.

from contextvars import ContextVar
from typing import Any

_CALLER_REQUEST_CONTEXT: ContextVar["dict[str, Any] | None"] = ContextVar(
    "cmn_c2_284_caller_request_context", default=None
)


def set_caller_request_context(request_context: "dict[str, Any] | None") -> None:
    """Stash the validated caller request data for the imminent inner-graph invoke."""
    _CALLER_REQUEST_CONTEXT.set(dict(request_context) if request_context else {})


def get_caller_request_context() -> "dict[str, Any]":
    """Read (without consuming) the stashed request data; {} when none was set."""
    return _CALLER_REQUEST_CONTEXT.get() or {}
