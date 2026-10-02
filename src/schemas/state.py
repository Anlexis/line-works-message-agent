"""AgentCore Platform v1.0 - CMN-C2-284 LINE WORKS Message Agent state."""

# State must be a flat TypedDict - never a Pydantic BaseModel.
# LangGraph checkpoints use msgpack serialization; Pydantic objects (and
# nested dict/list containers) are not msgpack-safe. Extend AgentState with
# agent-specific fields only, and declare every domain field NotRequired[...]
# (fields are absent until their producer node writes them). lineworks_payload /
# lineworks_config / redaction_flags are dicts/lists at the point of use but are
# stored in State as JSON strings via to_json/from_json below. Do NOT add
# credentials, secrets, or Pydantic models. The LINE WORKS
# integration token is NEVER stored here - it is read via ctx.secrets in
# CallLineWorksApiNode.

from __future__ import annotations

import json
from typing import Any, NotRequired, Optional

from framework.schemas.agent_state import AgentState


def to_json(value: Any) -> Optional[str]:
    """Serialize a list/dict State value to a compact JSON string (msgpack-safe).

    Returns None for None so the field stays a true Optional[str].
    """
    if value is None:
        return None
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def from_json(value: Any, default: Any) -> Any:
    """Deserialize a JSON-string State value back to its list/dict form.

    Tolerant by design: None/empty -> default; an already-native list/dict (e.g. a value
    supplied directly in a unit test) passes through unchanged; a malformed string -> default.
    """
    if value is None or value == "":
        return default
    if isinstance(value, (list, dict)):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


class State(AgentState):
    """LINE WORKS Message agent state.

    Shared fields (user_input, validated_input, intent, result, status,
    formatted_output, session_id, node_history, error_log, correlation_id,
    trace_id, hitl_*, etc.) are inherited from AgentState and NOT re-declared.
    Only LINE WORKS-workflow fields are added below, all NotRequired (the
    state contract). All values are JSON/msgpack-serializable primitives -
    the LINE WORKS integration token is NEVER stored here (accessed via
    ctx.secrets).
    """

    # Caller-supplied target hint (channel/room, user, or message id from
    # input_context). Never inferred; resolution to a LINE WORKS channel or
    # message id is explicit-only (pass-through when the hint or the request
    # text already carries an id).
    target_hint: NotRequired[str]

    # Validated caller-supplied message data from input_context (body text and
    # activity page size), carried across the graph boundary by the context
    # bridge. JSON string (state values stay msgpack-safe); every field passed
    # its bounded, inert shape check in PreProcessNode before it got here.
    caller_message: NotRequired[Optional[str]]
    channel_id: NotRequired[str]  # resolved LINE WORKS channel (talk room) / user target id
    message_id: NotRequired[str]  # resolved LINE WORKS message id (delivery lookup / send receipt)

    # ValidateInput (deterministic content scan)
    # JSON list[str] of patterns redacted from the text before logging
    # (stored as a JSON string; (de)serialize via to_json/from_json).
    redaction_flags: NotRequired[Optional[str]]

    # InferLineWorksFields
    # JSON - assembled LINE WORKS REST API request body (stored as a
    # JSON string, not a native dict; (de)serialize via to_json/from_json).
    lineworks_payload: NotRequired[Optional[str]]

    # Runtime `lineworks:` section forwarded by _parent_config() and injected
    # by the inner graph's _extra_initial_state() (JSON string).
    lineworks_config: NotRequired[Optional[str]]

    # CallLineWorksApi
    record_id: NotRequired[str]  # message id / channel id returned by LINE WORKS
    record_ref: NotRequired[str]  # human-readable reference (lineworks://messages/<id>)
    delivery_status: NotRequired[str]  # delivery status label from a check_delivery lookup

    # Confirm
    confirmation: NotRequired[str]  # human-readable confirmation message
