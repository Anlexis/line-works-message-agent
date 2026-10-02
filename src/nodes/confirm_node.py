"""AgentCore Platform v1.0 - inner workflow Step 5: Confirm.

Formats the sent / status-checked / activity-listed LINE WORKS record (id +
reference + delivery status) into a human-readable confirmation message,
surfacing the affected message/room for human review (risk mitigation).
"""

from typing import Any

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event

_VERBS = {
    "send_message": "Sent LINE WORKS message",
    "check_delivery": "Checked LINE WORKS message delivery",
    "list_activity": "Listed recent LINE WORKS room activity",
}


class ConfirmNode(FunctionNode):
    """Build the human-readable confirmation."""

    # Inner domain node, read-only formatting of already-fetched data -
    # the external gate lives on the outer backbone pre_process.
    required_trust_level = TrustLevel.ANONYMOUS

    def execute(self, state: "dict[str, Any]") -> "dict[str, Any]":
        record_id = state.get("record_id", "")
        record_ref = state.get("record_ref", "")
        delivery_status = state.get("delivery_status", "")
        intent = state.get("intent", "check_delivery")

        if not record_id and not record_ref:
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": ["ConfirmNode: no record_id/record_ref to confirm"],
            }

        verb = _VERBS.get(intent, "Processed LINE WORKS request")
        parts = [verb]
        if delivery_status:
            parts.append(f"status={delivery_status}")
        if record_ref:
            parts.append(f"ref={record_ref}")
        if record_id:
            parts.append(f"id={record_id}")
        confirmation = " - ".join(parts)

        # Audit the confirmed action - intent + reference presence (no content).
        emit_trace_event(
            "confirm_complete",
            {"intent": intent, "has_record_ref": bool(record_ref)},
            state,
        )

        return {
            "confirmation": confirmation,
            "result": {
                "record_id": record_id,
                "record_ref": record_ref,
                "confirmation": confirmation,
            },
            "status": AgentStatus.SUCCESS.value,
        }
