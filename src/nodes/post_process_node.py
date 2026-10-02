"""AgentCore Platform v1.0 - outer post_process node.

Cat 2 outer backbone: finalize the response after the inner LINE WORKS workflow
graph has run. GraphNode.merge_output() maps the inner result into the outer
state; this node shapes the caller-facing `formatted_output`.

The domain output gate is the MODULE-LEVEL `_security_gate_output()` below,
called from execute(). It is deliberately NOT an instance method and NOT the
framework `_extra_security_gate_output` hook - the framework gate methods are
@final on FunctionNode and the real SDK auto-wraps `_extra_` hooks (which
breaks the .invoke() chain), so domain checks live in a module-level helper
invoked inline.

The stated output invariant (docs/02_design.md, "Output contract") is this
template's own; there is no rounding grid to enforce, because the agent reports
message and room identifiers and a confirmation sentence, not monetary
aggregates. What it does enforce, for every representation:

  1. the caller-facing output carries EXACTLY the declared keys - a LINE WORKS
     response object is projected into them, never spread wholesale, so bulk
     room or message data cannot ride out on a field nobody declared;
  2. a SUCCESS response carries record evidence (record_id / record_ref) -
     otherwise it would misrepresent the outcome of a send against a real
     messaging system;
  3. no credential-shaped string appears ANYWHERE in the output, including
     strings nested inside the assembled LINE WORKS request body (a mapping
     with the message content nested inside it). A top-level-only scan reports
     zero findings on exactly the case that matters.

Two properties of the gate are load-bearing and easy to get wrong:

- It walks the WHOLE nested output structure (dicts, lists, tuples), not just
  top-level strings.
- On EVERY non-success return - a gate violation AND a pre-existing
  inner-workflow failure - it CLEARS every output-bearing state field instead
  of merely returning an error status. The base graph's output shaping falls
  back to `state["result"]` when `formatted_output` is falsy, so a gate that
  only raised - or that returned an error without clearing - would still ship
  the un-gated inner answer inside the error envelope. Containment means the
  answer is gone, not merely relabelled.

And two more, added after molt source review (2026-09-04):

- NO error envelope carries LINE WORKS record evidence. `record_id` /
  `record_ref` are this agent's WRITE EVIDENCE - the gate above REFUSES a
  SUCCESS that lacks them - so returning them under an ERROR status would tell
  a caller being informed of failure that a message was nonetheless sent, and
  which one, in which channel.

- What the ERROR envelope may say is a CLOSED SET: a constant reason code
  chosen by this module (one of `ERROR_REASONS`) and nothing else - never
  `error_log`, never the gate's violation entries, never any other
  node-authored string. Those lines can embed upstream response text (a LINE
  WORKS error body quoting the channel or the message it refused), identifiers
  or names, and truncating or redacting them is not a closed set: credential
  redaction removes tokens, and a room name is not credential-shaped.
  `error_log` stays the INTERNAL channel - the state reducer appends to it and
  the audit trail needs it; it is simply never projected to the caller. Gate
  violations are written there naming the offending PATH (fixed keys and
  indices, never a value), and the audit event carries a count.

Every non-success return therefore goes through the one module-level
`_contain()` helper. The constant reason also keeps the mapping TRUTHY.
"""

import re
from typing import Any, Iterator

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event

from src.schemas.state import from_json

# Credential-shaped strings that must never reach the caller (defence in depth -
# the framework's own output-side credential scan in FunctionNode also runs on
# every result).
_CREDENTIAL_LIKE_RE = re.compile(r"eyJ[A-Za-z0-9._-]{10,}|sk-[A-Za-z0-9]{20,}|Bearer\s+[A-Za-z0-9._-]{16,}")

# Stand-in for a mapping key that cannot itself be written into a path label.
_UNNAMEABLE_KEY = "<withheld>"

# The declared caller-facing response keys. The gate refuses anything else, so
# a future change that spreads a LINE WORKS response object into the output
# fails here instead of shipping a whole room transcript.
_DECLARED_OUTPUT_KEYS = frozenset(
    {
        "record_id",
        "record_ref",
        "channel_id",
        "message_id",
        "delivery_status",
        "intent",
        "confirmation",
        "lineworks_payload",
    }
)

# Every state field that can carry released answer text. On EVERY error return
# all of them are cleared, so nothing downstream can fall back to one of them -
# and nothing survives in state either, where a checkpoint or a later reader
# picks it straight back up. Omitting a field from one envelope is not clearing
# it. `intent` is in the set because it is part of the declared success output
# and is merged back by the graph node.
_OUTPUT_BEARING_FIELDS = (
    "result",
    "confirmation",
    "record_id",
    "record_ref",
    "channel_id",
    "message_id",
    "delivery_status",
    "intent",
    "lineworks_payload",
)

# Fields cleared to None rather than "" - they are Optional[str]/object-shaped
# in the state contract, so an empty string would be a type lie.
_NULLABLE_OUTPUT_FIELDS = frozenset({"result", "lineworks_payload"})

# Reason codes - the ONLY values the caller-visible ERROR envelope may carry.
# Chosen here, never derived from state, so the envelope is a closed set: it
# says WHAT happened, never to which message or room and never in whose words.
_REASON_WORKFLOW_FAILED = "lineworks_workflow_failed"  # the inner workflow reported an error
_REASON_OUTPUT_WITHHELD = "output_withheld_by_gate"  # the output gate refused the response
ERROR_REASONS = frozenset({_REASON_WORKFLOW_FAILED, _REASON_OUTPUT_WITHHELD})


def _cleared_output_state() -> "dict[str, Any]":
    """Blank every output-bearing field, honouring the state contract's types."""
    return {field: (None if field in _NULLABLE_OUTPUT_FIELDS else "") for field in _OUTPUT_BEARING_FIELDS}


def _contain(reason: str, new_errors: "list[str] | None" = None) -> "dict[str, Any]":
    """The node result for ANY non-success outcome - the single error shape.

    Error status, every output-bearing field cleared, and an envelope made of
    closed-set labels only: `reason` is one of ERROR_REASONS. `new_errors`
    (gate violations - path labels only) are appended to `error_log`, the
    internal channel the state reducer accumulates, and never enter the
    envelope. Nothing is read out of state: not the record, not `error_log`,
    and no count of it either - the count is an audit signal, not a caller
    field.

    The constant `reason` key keeps the mapping TRUTHY, so the framework's
    `formatted_output or result` projection (AgentBaseGraph.get_output()
    applies no status check) serves this envelope and never whatever survived
    in `result`.
    """
    contained: "dict[str, Any]" = _cleared_output_state()
    contained["formatted_output"] = {"reason": reason}
    contained["status"] = AgentStatus.ERROR.value
    if new_errors:
        contained["error_log"] = list(new_errors)
    return contained


def _iter_strings(value: Any, path: str) -> "Iterator[tuple[str, str]]":
    """Yield (path, string) for EVERY string leaf in a nested structure.

    Walks dicts, lists and tuples so a value nested inside the LINE WORKS
    request body (e.g. formatted_output["lineworks_payload"]["content"]["text"])
    is scanned exactly like a top-level field.

    A credential-shaped mapping KEY is withheld from the path label rather than
    quoted into it. The label travels in `error_log`, where the framework's own
    credential scan walks every string leaf of the node result and RAISES - and
    a raise here would discard the cleared fields this containment just built,
    re-opening the `formatted_output or result` fallback on whatever survived.
    """
    if isinstance(value, str):
        yield path, value
    elif isinstance(value, dict):
        for key, item in value.items():
            label = key
            if isinstance(key, str) and _CREDENTIAL_LIKE_RE.search(key):
                label = _UNNAMEABLE_KEY
            yield from _iter_strings(item, f"{path}.{label}" if path else str(label))
    elif isinstance(value, (list, tuple)):
        for idx, item in enumerate(value):
            yield from _iter_strings(item, f"{path}[{idx}]")


def _security_gate_output(formatted_output: "dict[str, Any]", is_success: bool) -> "list[str]":
    """Domain output gate (module-level; called from PostProcessNode.execute()).

    Returns the list of violations ([] = the output may ship). Violations name
    the field PATH, never the value - and they are `error_log` entries
    (internal); they never reach the caller.
    """
    problems: "list[str]" = []
    if set(formatted_output) - _DECLARED_OUTPUT_KEYS:
        problems.append("PostProcess output gate: response carries a field outside the declared output contract")
    if is_success and not (formatted_output.get("record_id") or formatted_output.get("record_ref")):
        problems.append("PostProcess output gate: SUCCESS output missing record_id/record_ref evidence")
    for path, value in _iter_strings(formatted_output, ""):
        if _CREDENTIAL_LIKE_RE.search(value):
            problems.append(f"PostProcess output gate: credential-like value in formatted_output['{path}']")
    return problems


class PostProcessNode(FunctionNode):
    """Format the final agent output."""

    # Read-only formatting of the already-produced result - default permissive;
    # the external trust gate lives on the outer backbone pre_process.
    required_trust_level = TrustLevel.ANONYMOUS

    def execute(self, state: "dict[str, Any]") -> "dict[str, Any]":
        # If the inner workflow errored, preserve the error status (do not mask
        # it) and publish NOTHING of it: `error_log` already carries the inner
        # entries (the state reducer appends, so re-emitting them here would
        # duplicate every line) and the caller receives the reason code only.
        # The delta clears every output-bearing field, so the identifiers and
        # the message body cannot be recovered from the checkpoint or by a
        # downstream reader either.
        #
        # Under the real pipeline BaseNode.__call__ short-circuits on an
        # errored state before execute() runs, and the backbone routes an ERROR
        # straight to finalize, so this branch is defence in depth reachable
        # only by a direct call.
        if state.get("status") == AgentStatus.ERROR.value:
            # Outcome signals only - a closed-set reason code and a count. The
            # audit log is not a store for message content or error text.
            emit_trace_event(
                "post_process_error_contained",
                {"reason": _REASON_WORKFLOW_FAILED, "error_count": len(state.get("error_log", []) or [])},
                state,
            )
            return _contain(_REASON_WORKFLOW_FAILED)

        formatted_output = {
            "record_id": state.get("record_id", ""),
            "record_ref": state.get("record_ref", ""),
            "channel_id": state.get("channel_id", ""),
            "message_id": state.get("message_id", ""),
            "delivery_status": state.get("delivery_status", ""),
            "intent": state.get("intent", ""),
            "confirmation": state.get("confirmation", ""),
            "lineworks_payload": from_json(state.get("lineworks_payload"), {}),
        }

        # Domain output gate (module-level helper - see module docstring). A
        # refusal is contained exactly like an inner error: the violations go
        # to error_log only, the caller receives the reason code only.
        violations = _security_gate_output(formatted_output, is_success=True)
        if violations:
            # A refusal is a decision, so it gets its own audit event - a block
            # that leaves no trace is indistinguishable from a request that was
            # never made. Counts and the violated rule only, never the values.
            emit_trace_event(
                "post_process_gate_blocked",
                {
                    "reason": _REASON_OUTPUT_WITHHELD,
                    "intent": state.get("intent", ""),
                    "violation_count": len(violations),
                    "missing_record_evidence": not (
                        formatted_output.get("record_id") or formatted_output.get("record_ref")
                    ),
                },
                state,
            )
            return _contain(_REASON_OUTPUT_WITHHELD, violations)

        # Audit the final response shaping - outcome signals only, no payload content.
        emit_trace_event(
            "post_process_complete",
            {
                "intent": state.get("intent", ""),
                "has_record_id": bool(state.get("record_id")),
            },
            state,
        )

        return {
            "formatted_output": formatted_output,
            "status": AgentStatus.SUCCESS.value,
        }
