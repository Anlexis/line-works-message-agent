"""AgentCore Platform v1.0 - inner workflow Step 2: ClassifyIntent.

Classifies the (redacted) request into one of send_message / check_delivery /
list_activity using a deterministic keyword heuristic, so the template is
testable and runnable without a live LLM (docs/02_design.md,
"Implementation Note - LLM synthesis"). Low-confidence / unknown falls back to the
read-only "check_delivery" default with a note - never a send (write).
"""

from typing import Any

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event

_VALID_INTENTS = ("send_message", "check_delivery", "list_activity")

# Deterministic keyword signals (checked in priority order, the write first so a
# "send the update then show the status" style request classifies as the send).
_KEYWORDS = (
    (
        "send_message",
        (
            "send",
            "post a message",
            "post to",
            "notify",
            "announce",
            "broadcast",
            "reply",
            "message the",
            "tell the",
            "送信",
            "送って",
            "投稿",
            "通知",
            "知らせ",
        ),
    ),
    (
        "check_delivery",
        (
            "delivery",
            "delivered",
            "status of",
            "read receipt",
            "was read",
            "been read",
            "seen by",
            "reach",
            "配信",
            "既読",
            "届いた",
            "ステータス",
        ),
    ),
    (
        "list_activity",
        (
            "activity",
            "recent",
            "history",
            "latest",
            "what happened",
            "list the",
            "show the messages",
            "timeline",
            "履歴",
            "最近",
            "一覧",
            "アクティビティ",
        ),
    ),
)


class ClassifyIntentNode(FunctionNode):
    """Classify the request into a LINE WORKS messaging operation intent."""

    # Inner domain node, read-only classification of already-redacted text -
    # the external gate lives on the outer backbone pre_process.
    required_trust_level = TrustLevel.ANONYMOUS

    def execute(self, state: "dict[str, Any]") -> "dict[str, Any]":
        text = state.get("validated_input", "") or ""
        if not text:
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": ["ClassifyIntentNode: missing validated_input"],
            }

        intent = self._classify_via_keywords(text)

        note: list[str] = []
        if intent not in _VALID_INTENTS:
            note = ["ClassifyIntentNode: low-confidence classification, " "defaulted to check_delivery (read-only)"]
            intent = "check_delivery"

        # Audit the classification decision - intent label only, never the text.
        emit_trace_event(
            "classify_intent_complete",
            {"intent": intent, "defaulted": bool(note)},
            state,
        )

        result: "dict[str, Any]" = {"intent": intent, "status": AgentStatus.SUCCESS.value}
        if note:
            result["error_log"] = note  # non-fatal note; status stays SUCCESS
        return result

    # -- classification -------------------------------------------------------

    def _classify_via_keywords(self, text: str) -> str:
        low = text.lower()
        for intent, words in _KEYWORDS:
            if any(w in low for w in words):
                return intent
        # No signal at all: fall through to the read-only default via the
        # _VALID_INTENTS guard in execute() (returns a sentinel outside the set).
        return "unknown"
