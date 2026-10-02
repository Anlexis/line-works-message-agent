# Proof of boundary: the ERROR envelope must CONTAIN the failure, not re-publish it.
#
# molt source review, 2026-09-04 (wave-9 batch), family finding across sixteen
# sibling repos. Two channels, both caller-visible:
#
#   1. the existing-ERROR branch of PostProcessNode.execute() rebuilt a truthy
#      `formatted_output` out of `record_id` / `record_ref` read straight back
#      from state, and returned a delta of ONLY formatted_output + status - so
#      every other output-bearing field survived in state;
#   2. `error_log` entries carried interpolated exception / upstream text rather
#      than closed-set labels, and error_log rides `formatted_output["error"]`
#      back to the caller.
#
# Why the identifiers matter here: `record_id` / `record_ref` are this agent's
# WRITE EVIDENCE - `_security_gate_output()` REFUSES a SUCCESS that lacks them.
# An envelope carrying them under an ERROR status tells a caller being told the
# operation failed that a LINE WORKS message was nonetheless sent, and which
# one. LINE WORKS is a workplace messaging system: the message id, the channel
# and the message reference identify a real message in a real room.
#
# Note the existing-ERROR branch already redacted CREDENTIAL-shaped strings.
# That is a different property: redaction removes tokens, it does not stop the
# envelope naming the record, and it does not clear anything from state.
#
# REACHABILITY (stated honestly, per the review): this branch is NOT reachable
# through the compiled graph. AgentBaseGraph.route() sends an ERROR status to
# `finalize`, bypassing `post_process`, and BaseNode.__call__ short-circuits an
# already-errored state before execute() runs. The tests below therefore call
# execute() DIRECTLY - which the node's own docstring already acknowledges as
# the branch's only caller. This is source-level defence in depth.

import json

import pytest

from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel

from src.nodes.call_lineworks_api_node import CallLineWorksApiNode
from src.nodes.post_process_node import PostProcessNode
from src.schemas.state import to_json

# The LINE WORKS record evidence that must never ride an error envelope.
# Values match the state fixture below.
_RECORD_ID = "ms-777"
_RECORD_REF = "lineworks://messages/ms-777"
_CHANNEL_ID = "ch-101"
_MESSAGE_TEXT = "Standup moved to 10am - client escalation on the Acme account"

# Every State field that can carry text produced by the inner workflow. The
# error delta must blank all of them - omitting a field from ONE envelope is
# not clearing it from state, where a checkpoint or a downstream reader picks
# it straight back up.
_OUTPUT_BEARING = (
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


@pytest.fixture(autouse=True)
def _mute_audit(monkeypatch):
    monkeypatch.setattr("src.nodes.post_process_node.emit_trace_event", lambda *a, **k: None)
    monkeypatch.setattr("src.nodes.call_lineworks_api_node.emit_trace_event", lambda *a, **k: None)


def _state(**overrides) -> dict:
    state = {
        "status": AgentStatus.SUCCESS.value,
        "record_id": _RECORD_ID,
        "record_ref": _RECORD_REF,
        "channel_id": _CHANNEL_ID,
        "message_id": _RECORD_ID,
        "delivery_status": "delivered",
        "intent": "send_message",
        "confirmation": f"Sent LINE WORKS message - ref={_RECORD_REF} - id={_RECORD_ID}",
        "lineworks_payload": to_json({"channel_id": _CHANNEL_ID, "content": {"type": "text", "text": _MESSAGE_TEXT}}),
        "caller_trust_level": TrustLevel.ANONYMOUS.value,
        "correlation_id": "error-envelope-pob",
        "session_id": "s1",
        "thread_id": "th1",
        "trace_id": "t1",
        "node_history": [],
        "error_log": [],
        "execution_time": {},
    }
    state.update(overrides)
    return state


def _errored_state() -> dict:
    """A failure raised AFTER the LINE WORKS call sent the message.

    This is the only shape in which record evidence is present on an error at
    all: the send landed, a later step failed. A fixture without the evidence
    would make the test vacuous.
    """
    state = _state(status=AgentStatus.ERROR.value)
    state["result"] = {
        "record_id": _RECORD_ID,
        "record_ref": _RECORD_REF,
        "confirmation": f"Sent LINE WORKS message - ref={_RECORD_REF}",
    }
    state["error_log"] = ["ConfirmNode: downstream failure after the LINE WORKS call"]
    return state


class TestErrorEnvelopeCarriesNoRecordEvidence:
    """Channel 1: the caller-facing envelope and the state it leaves behind."""

    def test_envelope_is_present_and_truthy(self):
        """Containment must NOT be achieved by emptying the envelope.

        AgentBaseGraph.get_output() projects `formatted_output or result` with
        no status check, so a falsy formatted_output hands the caller whatever
        `result` holds - the exact fallback this containment exists to close.
        """
        result = PostProcessNode().execute(_errored_state())
        assert "formatted_output" in result, "the error path must still ship an envelope"
        assert result["formatted_output"], (
            "error envelope must be TRUTHY - a falsy one re-opens the "
            "`formatted_output or result` fallback in AgentBaseGraph.get_output()"
        )

    def test_envelope_names_no_record(self):
        """The leak itself: no identifier may ride the failure envelope back to
        the caller. Credential redaction on this branch does not cover it - a
        record id is not credential-shaped."""
        shipped = json.dumps(
            PostProcessNode().execute(_errored_state())["formatted_output"],
            default=str,
            ensure_ascii=False,
        )
        leaked = [
            field
            for field, value in (
                ("record_id", _RECORD_ID),
                ("record_ref", _RECORD_REF),
                ("channel_id", _CHANNEL_ID),
            )
            if value in shipped
        ]
        assert not leaked, f"error envelope leaked LINE WORKS record evidence: {leaked} in {shipped}"

    def test_error_delta_clears_output_bearing_state(self):
        """ "Retains output-bearing state": the delta must blank every field."""
        result = PostProcessNode().execute(_errored_state())
        retained = [field for field in _OUTPUT_BEARING if field not in result or result[field]]
        assert not retained, (
            f"output-bearing state not cleared on the error path: {retained}; " f"delta keys = {sorted(result)}"
        )

    def test_error_status_is_still_reported(self):
        """Containment must not mask the failure."""
        assert PostProcessNode().execute(_errored_state())["status"] == AgentStatus.ERROR.value

    def test_clean_path_control_still_returns_the_evidence(self):
        """CONTROL. Without it every assertion above passes vacuously - a node
        that returned an empty envelope and cleared everything would satisfy
        them all. The success path must still carry the record evidence."""
        result = PostProcessNode().execute(_state())
        assert result["status"] == AgentStatus.SUCCESS.value
        out = result["formatted_output"]
        assert out["record_id"] == _RECORD_ID
        assert out["record_ref"] == _RECORD_REF
        assert out["channel_id"] == _CHANNEL_ID
        assert out["message_id"] == _RECORD_ID
        assert out["delivery_status"] == "delivered"
        assert out["confirmation"].startswith("Sent LINE WORKS message")
        assert out["lineworks_payload"]["content"]["text"] == _MESSAGE_TEXT


class TestErrorReasonsAreClosedSetLabels:
    """Channel 2: `error_log` rides `formatted_output["error"]` to the caller.

    An upstream error body is unbounded third-party text that can quote the
    very channel or message it refused, and a transport error string carries
    the request URL, which embeds the message id. Only closed-set labels - the
    HTTP status and the exception TYPE - may travel.
    """

    _CONFIG = {"configurable": {"lineworks": {"base_url": "https://api.lineworks.test/v1.0"}}}

    def _call_state(self, **overrides) -> dict:
        state = _state(
            status=AgentStatus.PENDING.value,
            record_id="",
            record_ref="",
            delivery_status="",
            confirmation="",
        )
        state.update(overrides)
        return state

    def test_api_error_reason_does_not_carry_the_upstream_body(self):
        """A live tenant's error body can quote the channel or message text."""
        from src.services.lineworks_client import LineWorksApiError

        body = f"denied for channel {_CHANNEL_ID}: '{_MESSAGE_TEXT}' (bot not a member)"

        class _Client:
            uses_stub_transport = True

            def send_message(self, channel_id, payload, api_token):
                raise LineWorksApiError(403, body)

        result = self._run(CallLineWorksApiNode(), self._call_state(intent="send_message"), _Client())
        reasons = "\n".join(result["error_log"])
        assert "403" in reasons, "the closed-set signal (HTTP status) must still travel"
        assert body not in reasons, f"reason carried the upstream body verbatim: {reasons!r}"
        assert _CHANNEL_ID not in reasons, f"reason named the channel: {reasons!r}"
        assert _MESSAGE_TEXT not in reasons, f"reason quoted the message text: {reasons!r}"

    def test_transport_failure_reason_carries_the_type_not_the_string(self):
        detail = f"HTTPSConnectionPool(host='api.lineworks.test'): /messages/{_RECORD_ID}"

        class _Client:
            uses_stub_transport = True

            def get_message_status(self, message_id, api_token):
                raise ConnectionError(detail)

        result = self._run(
            CallLineWorksApiNode(),
            self._call_state(intent="check_delivery", message_id=_RECORD_ID),
            _Client(),
        )
        reasons = "\n".join(result["error_log"])
        assert "ConnectionError" in reasons, "the closed-set signal (exception type) must travel"
        assert _RECORD_ID not in reasons, f"transport reason named the message: {reasons!r}"

    def test_control_successful_call_still_returns_the_evidence(self):
        """CONTROL for this class: a reason-sanitising change must not turn the
        clean call into an error."""

        class _Client:
            uses_stub_transport = True

            def send_message(self, channel_id, payload, api_token):
                return {"message_id": _RECORD_ID}

        result = self._run(CallLineWorksApiNode(), self._call_state(intent="send_message"), _Client())
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["record_id"] == _RECORD_ID

    @staticmethod
    def _run(node, state, client) -> dict:
        import src.nodes.call_lineworks_api_node as mod

        original = mod.LineWorksClient
        mod.LineWorksClient = lambda *a, **k: client
        try:
            return node.execute(state, config=TestErrorReasonsAreClosedSetLabels._CONFIG)
        finally:
            mod.LineWorksClient = original
