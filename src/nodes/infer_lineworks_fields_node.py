"""AgentCore Platform v1.0 - inner workflow Step 3: InferLineWorksFields.

Assembles a validated LINE WORKS REST API request body for the classified
intent from the caller's structured data first and the request text second.

The message body and the room-activity page size come from the caller's
validated `caller_message` contract when supplied; a body written into the
request prose is only a FALLBACK, because the framework masks personal data in
the text channel and a name inside the prose reaches this node already
rewritten. Channel and message ids are taken only from an explicit id in the
text or the caller-supplied target_hint - an unresolved id is left empty rather
than invented (risk mitigation: never message the wrong room or report on the
wrong message; the executor surfaces the miss as status=error). Deterministic -
no LLM (docs/02_design.md "Implementation Note").
"""

import re
from typing import Any

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event

from src.schemas.state import from_json, to_json
from src.services.security import DEFAULT_ACTIVITY_LIMIT

# A LINE WORKS id token: a dash-prefixed short id (ch-101, us-42, ms-204) or a
# digit-leading identifier. Deliberately narrower than "any word" so prose
# following the keyword ("channel activity") is never mistaken for an id.
_ID_TOKEN = r"([A-Za-z]{0,4}-[A-Za-z0-9_-]{1,15}|[0-9][A-Za-z0-9_-]{0,19})"
_ID_SHAPE_RE = re.compile(rf"^{_ID_TOKEN}$")
# Explicit channel/room/user target mention in the request text, EN or JA
# ("channel ch-101" / "room: 2044" / "user us-42" / "トークルーム ch-101").
_CHANNEL_IN_TEXT_RE = re.compile(
    rf"(?:channel|room|user)\s*[:#]?\s*{_ID_TOKEN}" rf"|(?:チャンネル|トークルーム|ルーム)\s*[:：#]?\s*{_ID_TOKEN}",
    re.IGNORECASE,
)
# Explicit message-id mention ("message ms-204" / "message id: 7044" / "メッセージID 7044").
_MESSAGE_IN_TEXT_RE = re.compile(
    rf"(?:message|msg)\s+(?:id\s*[:#]?\s*)?{_ID_TOKEN}" rf"|メッセージ\s*(?:ID)?\s*[:：#]?\s*{_ID_TOKEN}",
    re.IGNORECASE,
)
# Quoted message body: saying "Foo" / message: "Foo". Curly quotes as literals
# are avoided - the class uses \u escapes so the source stays pure ASCII (push-safe).
_TEXT_QUOTED_RE = re.compile(
    r'(?:saying|says|reading|message|text)\s*[:：]?\s*["“]([^"”\n]+)["”]',
    re.IGNORECASE,
)
_ANY_QUOTED_RE = re.compile(r'["“]([^"”\n]+)["”]')

_MAX_TEXT_LEN = 2000


class InferLineWorksFieldsNode(FunctionNode):
    """Extract entities and assemble the LINE WORKS REST API request body."""

    # Inner domain node - derives fields from already-validated text; the
    # external gate lives on the outer backbone pre_process.
    required_trust_level = TrustLevel.ANONYMOUS

    def execute(self, state: "dict[str, Any]") -> "dict[str, Any]":
        text = state.get("validated_input", "") or ""
        intent = state.get("intent", "check_delivery") or "check_delivery"
        target_hint = state.get("target_hint", "") or ""
        # Already validated field-by-field by PreProcessNode (bounded, inert);
        # {} when the caller supplied no structured data.
        caller_message = from_json(state.get("caller_message"), {}) or {}

        if not text.strip():
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": ["InferLineWorksFieldsNode: missing validated_input"],
            }

        channel_id = self._resolve_channel(text, target_hint, intent)
        message_id = self._resolve_message(text, target_hint, intent)
        message_text = self._resolve_text(text, intent, caller_message)
        activity_limit = self._resolve_limit(caller_message)

        payload: "dict[str, Any]"
        if intent == "send_message":
            payload = {
                "channel_id": channel_id,
                "content": {"type": "text", "text": message_text},
            }
        elif intent == "list_activity":
            payload = {"channel_id": channel_id, "limit": activity_limit}
        else:  # check_delivery (read-only default)
            payload = {"message_id": message_id}

        # Audit the assembled payload shape - field signals only, not content.
        emit_trace_event(
            "infer_lineworks_fields_complete",
            {
                "intent": intent,
                "has_channel_id": bool(channel_id),
                "has_message_id": bool(message_id),
                "has_text": bool(message_text),
                "text_from_caller_context": bool(str(caller_message.get("text") or "").strip()),
            },
            state,
        )

        return {
            "channel_id": channel_id,
            "message_id": message_id,
            "lineworks_payload": to_json(payload),
            "status": AgentStatus.SUCCESS.value,
        }

    # -- extraction -----------------------------------------------------------

    def _resolve_channel(self, text: str, target_hint: str, intent: str) -> str:
        """Explicit id only: text mention > id-shaped hint (send/list intents). Never invented."""
        m = _CHANNEL_IN_TEXT_RE.search(text)
        if m:
            return m.group(1) or m.group(2) or ""
        hint = target_hint.strip()
        if (
            intent in ("send_message", "list_activity")
            and hint
            and _ID_SHAPE_RE.match(hint)
            and not hint.lower().startswith("ms-")
        ):
            return hint
        return ""  # unresolved - left empty, never invented

    def _resolve_message(self, text: str, target_hint: str, intent: str) -> str:
        """Explicit id only: text mention > id-shaped hint (check_delivery intent). Never invented."""
        m = _MESSAGE_IN_TEXT_RE.search(text)
        if m:
            return m.group(1) or m.group(2) or ""
        hint = target_hint.strip()
        if intent == "check_delivery" and hint and _ID_SHAPE_RE.match(hint):
            return hint
        return ""  # unresolved - left empty, never invented

    def _resolve_text(self, text: str, intent: str, caller_message: "dict[str, Any]") -> str:
        """Message body for a send.

        The caller's validated body wins: it arrives over the unmasked
        structured channel, so it is the only representation guaranteed to
        reach the send intact. A quoted span in the request prose is the
        fallback for callers that send text only (keyword-quoted first, then
        any quoted span).
        """
        if intent != "send_message":
            return ""
        supplied = str(caller_message.get("text") or "").strip()
        if supplied:
            return supplied[:_MAX_TEXT_LEN]
        m = _TEXT_QUOTED_RE.search(text)
        if not m:
            m = _ANY_QUOTED_RE.search(text)
        if m:
            return m.group(1).strip()[:_MAX_TEXT_LEN]
        return ""

    def _resolve_limit(self, caller_message: "dict[str, Any]") -> int:
        """Room-activity page size: the caller's validated value, else the default.

        PreProcessNode already put a supplied value through the finite+bounded
        parser, so anything present here is an in-range whole number; a value
        that failed never reached this node.
        """
        limit = caller_message.get("limit")
        if isinstance(limit, int) and not isinstance(limit, bool):
            return limit
        return DEFAULT_ACTIVITY_LIMIT
