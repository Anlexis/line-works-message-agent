"""AgentCore Platform v1.0 - outer pre_process node.

Cat 2 outer backbone: serialize the caller's request (raw NL text + validated
target and message data) into a single JSON string in `validated_input`, which
the GraphNode (`main` slot) hands to the inner LINE WORKS workflow graph.
Business validation happens inside the inner graph's ValidateInputNode - this
node owns the CALLER-DATA CONTRACT: the empty-guard, the HTML/length sanitize,
the template-owned injection screen, and the field-by-field validation of
`input_context` (every accepted value is bounded; a malformed value is refused
with an error that names the field and never echoes the value).

Why input_context carries the structured data: the pipeline's other input
channel (the request text serialized into validated_input) is rewritten by the
framework's PII masking heuristics at every node boundary - a message body
naming a colleague as two Title Case words ("Taro Yamada") arrives at the LINE
WORKS send as "[MASKED]". Structured caller data therefore travels through
input_context, which is not masked, and this node screens that channel itself:
prompt-injection content (post-parse, keys included, both raw and after the
markup strip) is refused, and every value is held to a bounded, inert shape
before it can render into the assembled LINE WORKS request body or into the
caller-facing output.
"""

import json
from typing import Any

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event

from src.schemas.state import to_json
from src.services.security import (
    ACTIVITY_LIMIT_MAX,
    ACTIVITY_LIMIT_MIN,
    MAX_MESSAGE_LEN,
    MESSAGE_TEXT_RE,
    TARGET_ID_RE,
    TARGET_NUMBER_MAX,
    TARGET_NUMBER_MIN,
    finite_int_in_range,
    is_non_finite_spelling,
    sanitize_query,
    screen_text,
)

# input_context keys that may carry an explicit LINE WORKS target (caller-supplied
# only - a target is never inferred here). First present key wins.
_HINT_KEYS = ("channel_id", "target_hint", "message_id")

# The structured message contract: input_context["message"] may carry these
# fields, each bounded below. An unknown field inside `message` is refused
# (silently dropping a misspelled field would send a message missing content
# the caller supplied); the refusal names the container, never the unknown key
# itself (a field NAME is caller-controlled text too).
_MESSAGE_KEYS = ("text", "limit")


def _iter_context_strings(value: Any) -> "list[str]":
    """Every string in a parsed input_context - keys AND values, at any depth.

    The injection screen runs post-parse over this list, so a payload hidden in
    a mapping KEY, nested one level down, or JSON-escaped on the wire (parsed
    back to its real characters by then) is screened exactly like a top-level
    value.
    """
    found: "list[str]" = []
    if isinstance(value, str):
        found.append(value)
    elif isinstance(value, dict):
        for key, item in value.items():
            if isinstance(key, str):
                found.append(key)
            found.extend(_iter_context_strings(item))
    elif isinstance(value, (list, tuple)):
        for item in value:
            found.extend(_iter_context_strings(item))
    return found


class PreProcessNode(FunctionNode):
    """Validate + serialize caller input for the inner workflow graph."""

    # The outer backbone's SINGLE external trust gate. A real caller enters at
    # VERIFIED_EXTERNAL and the inner LINE WORKS call runs under this same
    # (unelevated) context, so the external gate lives HERE, not on the inner
    # API node. An under-trusted (ANONYMOUS) caller is denied at this gate
    # before any call.
    required_trust_level = TrustLevel.VERIFIED_EXTERNAL

    def execute(self, state: "dict[str, Any]") -> "dict[str, Any]":
        user_input = state.get("user_input", "")
        input_context = state.get("input_context", {})  # read-only

        if not user_input or not user_input.strip():
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": ["PreProcessNode: user_input is empty or missing"],
            }

        # Caller context must be a mapping; anything else is refused without
        # being echoed (fail closed - never guess at a malformed contract).
        if input_context is None:
            input_context = {}
        if not isinstance(input_context, dict):
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": ["PreProcessNode: input_context must be an object"],
            }

        # Template-owned injection screen. Both channels are screened RAW and
        # again after the markup strip, and input_context is screened
        # post-parse over every key and string value at any depth. Refusals
        # name the pattern type only - hostile content is never echoed.
        findings = screen_text(user_input)
        if findings:
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": [f"PreProcessNode: user_input contains disallowed content ({findings[0]})"],
            }
        for text in _iter_context_strings(input_context):
            findings = screen_text(text)
            if findings:
                return {
                    "status": AgentStatus.ERROR.value,
                    "error_log": [f"PreProcessNode: input_context contains disallowed content ({findings[0]})"],
                }

        # Strip HTML markup + cap length before JSON serialization.
        sanitized_input = sanitize_query(user_input.strip())

        # Target channel/message is caller-supplied and never inferred here: an
        # explicit id from input_context, validated against the bounded inert
        # shape. A present-but-malformed value (wrong type, wrong charset,
        # over-length) is a hard error naming the FIELD, never the value -
        # silently dropping it could post into a different room than the caller
        # intended.
        target_hint, problem = self._validate_hint(input_context)
        if problem:
            return {"status": AgentStatus.ERROR.value, "error_log": [problem]}

        # Structured message data: validated field-by-field against explicit
        # bounds; the body must match the inert render charset and the activity
        # page size goes through the finite+bounded number parser. Absent data
        # degrades to the text-inference baseline in the inner graph.
        caller_message, problem = self._validate_message(input_context.get("message"))
        if problem:
            return {"status": AgentStatus.ERROR.value, "error_log": [problem]}

        validated_input = json.dumps({"text": sanitized_input, "target_hint": target_hint})

        # Audit the shaped request - presence signals only, not the raw text.
        emit_trace_event(
            "pre_process_complete",
            {"has_target_hint": bool(target_hint), "has_caller_message": bool(caller_message)},
            state,
        )

        return {
            "validated_input": validated_input,
            "target_hint": target_hint,
            "caller_message": to_json(caller_message) if caller_message else None,
            "status": AgentStatus.SUCCESS.value,
        }

    # -- caller contract validation -------------------------------------------

    def _validate_hint(self, input_context: "dict[str, Any]") -> "tuple[str, str]":
        """Resolve the caller-supplied target id -> (id, "" | problem).

        An id delivered as a JSON NUMBER goes through the finite+bounded parser
        before it is rendered as a string: NaN and Infinity parse cleanly
        through float() and arrive intact in a raw JSON body, so an unchecked
        numeric id would reach the LINE WORKS request as "nan".
        """
        for key in _HINT_KEYS:
            value = input_context.get(key)
            if value is None:
                continue
            if isinstance(value, bool) or isinstance(value, (int, float)):
                bounded = finite_int_in_range(value, TARGET_NUMBER_MIN, TARGET_NUMBER_MAX)
                if bounded is None:
                    return "", (
                        f"PreProcessNode: input_context.{key} must be a finite whole number "
                        f"between {TARGET_NUMBER_MIN} and {TARGET_NUMBER_MAX}"
                    )
                value = str(bounded)
            if not isinstance(value, str):
                return "", f"PreProcessNode: input_context.{key} must be a string"
            value = value.strip()
            if not value:
                continue  # blank = absent
            if is_non_finite_spelling(value):
                # "NaN" / "inf" satisfy the identifier shape, so the shape check
                # alone lets one through as a STRING id. It then renders into
                # the request body and the record reference, where a downstream
                # reader we do not control will parse it - and every comparison
                # against a NaN it produces is False. Refuse the whole spelling;
                # an ordinary id that merely contains those letters is
                # unaffected because the match is on the entire value.
                return "", f"PreProcessNode: input_context.{key} must not be a non-finite number"
            if not TARGET_ID_RE.match(value):
                return "", f"PreProcessNode: input_context.{key} is not a valid LINE WORKS target id"
            return value, ""
        return "", ""

    def _validate_message(self, message: Any) -> "tuple[dict[str, Any], str]":
        """Validate input_context.message -> (accepted fields, "" | problem).

        Fail closed: wrong container type, an unsupported field, a wrong-typed
        or over-bound value, a body outside the inert render charset, or a
        non-finite / out-of-range page size each refuse with a field-naming
        error. Values are never echoed; the unsupported-field refusal does not
        echo the key either (a field NAME is caller-controlled text too).
        """
        if message is None:
            return {}, ""
        if not isinstance(message, dict):
            return {}, "PreProcessNode: input_context.message must be an object"
        if [key for key in message if key not in _MESSAGE_KEYS]:
            return {}, "PreProcessNode: input_context.message contains an unsupported field"

        accepted: "dict[str, Any]" = {}

        text = message.get("text")
        if text is not None:
            if not isinstance(text, str):
                return {}, "PreProcessNode: input_context.message.text must be a string"
            text = text.strip()
            if text:
                if is_non_finite_spelling(text):
                    # Ordinary prose is fine; a body that IS a non-finite number
                    # spelling is not - see is_non_finite_spelling() for why.
                    return {}, "PreProcessNode: input_context.message.text must not be a non-finite number"
                if not MESSAGE_TEXT_RE.match(text):
                    return {}, (
                        f"PreProcessNode: input_context.message.text must be at most {MAX_MESSAGE_LEN} "
                        "letters, digits, spaces, line breaks or ordinary sentence punctuation"
                    )
                accepted["text"] = text

        limit = message.get("limit")
        if limit is not None:
            bounded = finite_int_in_range(limit, ACTIVITY_LIMIT_MIN, ACTIVITY_LIMIT_MAX)
            if bounded is None:
                return {}, (
                    "PreProcessNode: input_context.message.limit must be a finite whole number "
                    f"between {ACTIVITY_LIMIT_MIN} and {ACTIVITY_LIMIT_MAX}"
                )
            accepted["limit"] = bounded

        return accepted, ""
