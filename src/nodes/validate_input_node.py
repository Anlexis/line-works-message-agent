"""AgentCore Platform v1.0 - inner workflow Step 1: ValidateInput.

Rejects empty / non-request input and runs a deterministic (regex, NOT LLM)
scan of the inbound text for email addresses / access-token-like strings, which
are flag-and-redacted before anything is logged. A messaging request
legitimately names channels and colleagues (the framework's own input gate
additionally masks emails/phones/names in user_input / validated_input), so
this is flag-and-redact for safe logging, not a hard reject. Prompt-injection
content is refused earlier, in the node that owns the caller contract
(PreProcessNode); the only deterministic auto-reject here is the empty /
non-request guard.
"""

import json
import re
from typing import Any

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event

from src.schemas.state import to_json

# Deterministic patterns: email addresses and bearer/JWT/API-token-like strings
# that might appear in a pasted request. Flagged + redacted before logging.
_EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}")
_TOKEN_RE = re.compile(r"\b(?:eyJ[A-Za-z0-9_-]{6,}|secret_[A-Za-z0-9]{6,}|sk-[A-Za-z0-9]{6,})\b")
_REDACTION = "[REDACTED]"

# Minimum signal that the text is a real request rather than noise.
_MIN_LEN = 3


class ValidateInputNode(FunctionNode):
    """Validate + flag-and-redact the inbound LINE WORKS messaging request."""

    # Inner domain node - the external gate lives on the outer backbone
    # pre_process (VERIFIED_EXTERNAL); the caller context is forwarded unchanged.
    required_trust_level = TrustLevel.ANONYMOUS

    def execute(self, state: "dict[str, Any]") -> "dict[str, Any]":
        raw = state.get("validated_input") or state.get("user_input") or ""

        # The outer graph serialized the request into a JSON string; accept both
        # the serialized shape and a bare string for direct unit testing.
        text = raw
        # A target id seeded from the caller's structured channel WINS over the
        # copy inside the serialized text: the text channel is masked at node
        # boundaries, so an id that looks like a digit group arrives there
        # already rewritten, while the structured channel is untouched.
        bridged_hint = str(state.get("target_hint") or "")
        target_hint = bridged_hint
        if isinstance(raw, str) and raw.strip().startswith("{"):
            try:
                obj = json.loads(raw)
                text = obj.get("text", "")
                target_hint = bridged_hint or obj.get("target_hint", "")
            except (ValueError, TypeError):
                text = raw

        if not isinstance(text, str) or len(text.strip()) < _MIN_LEN:
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": ["ValidateInputNode: empty or non-request input"],
            }

        # Deterministic flag-and-redact (before any logging).
        # Local list per invocation - never a module-global (no cross-invoke leak).
        flags: list[str] = []
        redacted = text
        if _EMAIL_RE.search(redacted):
            flags.append("email")
            redacted = _EMAIL_RE.sub(_REDACTION, redacted)
        if _TOKEN_RE.search(redacted):
            flags.append("token")
            redacted = _TOKEN_RE.sub(_REDACTION, redacted)

        # Audit the scan outcome - redaction flags only, never the inbound text.
        emit_trace_event(
            "validate_input_complete",
            {"has_target_hint": bool(target_hint), "redaction_flags": flags},
            state,
        )

        return {
            "validated_input": redacted.strip(),
            "target_hint": target_hint,
            "redaction_flags": to_json(flags),
            "status": AgentStatus.SUCCESS.value,
        }
