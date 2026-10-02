# CMN-C2-284 - Unit tests: PostProcessNode (outer backbone, domain output gate)
#
# Canon: invoked via node(state) (BaseNode.__call__ runs the trust gate, the
# input scan, execute(), then the output scan); this backbone formatter
# declares ANONYMOUS -> the state builder sets
# caller_trust_level = TrustLevel.ANONYMOUS.value. The domain output gate is
# the MODULE-LEVEL _security_gate_output() helper (the framework gate methods
# are @final and the real SDK auto-wraps _extra_ hooks), so the helper is also
# unit-tested directly as a plain function.

import pytest

from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel

from src.nodes.post_process_node import (
    ERROR_REASONS,
    _OUTPUT_BEARING_FIELDS,
    _REASON_OUTPUT_WITHHELD,
    _REASON_WORKFLOW_FAILED,
    PostProcessNode,
    _security_gate_output,
)
from src.schemas.state import to_json


def _sentinel() -> str:
    """An `error_log` line of the kind an upstream failure produces: a name and
    a credential-shaped token inside an echoed response body.

    Assembled at runtime so no credential-shaped literal is committed, and
    chosen to match NO credential detector - the point is that containment,
    not redaction, is what keeps it off the caller's channel.
    """
    token = "sk-" + "live-" + "x" * 3
    return "boom: upstream said {'customer':'A. Tanaka','token':'" + token + "'}"


_SENTINEL_FRAGMENTS = ("A. Tanaka", "boom: upstream", "sk-" + "live-")


def _every_string(value) -> list:
    """Every string reachable in a nested structure - keys AND values."""
    if isinstance(value, dict):
        return [s for k, v in value.items() for s in (*_every_string(k), *_every_string(v))]
    if isinstance(value, (list, tuple)):
        return [s for item in value for s in _every_string(item)]
    return [value if isinstance(value, str) else str(value)]


@pytest.fixture(autouse=True)
def _mute_audit(monkeypatch):
    monkeypatch.setattr("src.nodes.post_process_node.emit_trace_event", lambda *a, **k: None)


def _state(**overrides) -> dict:
    state = {
        "status": AgentStatus.SUCCESS.value,
        "record_id": "ms-204",
        "record_ref": "lineworks://messages/ms-204",
        "channel_id": "ch-101",
        "message_id": "ms-204",
        "delivery_status": "delivered",
        "intent": "check_delivery",
        "confirmation": "Checked LINE WORKS message delivery - status=delivered - ref=lineworks://messages/ms-204 - id=ms-204",
        "lineworks_payload": to_json({"message_id": "ms-204"}),
        "caller_trust_level": TrustLevel.ANONYMOUS.value,
        "correlation_id": "post-process-test",
        "node_history": [],
        "error_log": [],
        "execution_time": {},
    }
    state.update(overrides)
    return state


class TestPostProcessNode:
    def setup_method(self):
        self.node = PostProcessNode()

    def test_success_formats_output(self):
        result = self.node(_state())
        assert result["status"] == AgentStatus.SUCCESS.value
        out = result["formatted_output"]
        assert out["record_id"] == "ms-204"
        assert out["record_ref"] == "lineworks://messages/ms-204"
        assert out["channel_id"] == "ch-101"
        assert out["delivery_status"] == "delivered"
        assert out["intent"] == "check_delivery"
        assert out["confirmation"].startswith("Checked LINE WORKS message delivery")
        # The JSON lineworks_payload string surfaces parsed.
        assert out["lineworks_payload"] == {"message_id": "ms-204"}

    def test_error_status_preserved(self):
        """Inner-workflow error must not be masked as success. Real-SDK
        pipeline behavior: BaseNode.__call__ short-circuits on an incoming
        errored state (execute() is skipped), so the error status + error_log
        pass through untouched and no success shape is fabricated."""
        state = _state(
            status=AgentStatus.ERROR.value,
            record_id="",
            record_ref="",
            error_log=["CallLineWorksApiNode: LINE WORKS API error 403: forbidden"],
        )
        result = self.node(state)
        assert result["status"] == AgentStatus.ERROR.value
        assert "LINE WORKS API error 403" in "\n".join(result["error_log"])
        assert "formatted_output" not in result

    def test_error_status_as_string_value_preserved(self):
        """The framework may carry status as the enum .value (string) at the boundary."""
        result = self.node(_state(status=AgentStatus.ERROR.value, error_log=["boom"]))
        assert result["status"] == AgentStatus.ERROR.value
        assert "formatted_output" not in result

    def test_gate_blocks_success_without_record_evidence(self):
        """Full node path: a SUCCESS output missing record_id/record_ref is blocked."""
        result = self.node(_state(record_id="", record_ref=""))
        assert result["status"] == AgentStatus.ERROR.value
        assert any("record_id/record_ref" in entry for entry in result["error_log"])

    def test_violation_clears_every_output_bearing_field(self):
        """Containment: returning an error is not enough - the answer must be gone.

        The base graph shapes its output as formatted_output OR state["result"],
        so a gate that only relabels the status still ships the un-gated inner
        answer inside the error envelope.
        """
        released = "Sent LINE WORKS message - id=ms-204"
        result = self.node(
            _state(
                record_id="",
                record_ref="",
                confirmation=released,
                result={"record_id": "ms-204", "confirmation": released},
            )
        )
        assert result["status"] == AgentStatus.ERROR.value
        for field in _OUTPUT_BEARING_FIELDS:
            assert not result[field], field
        assert released not in repr(result)

    def test_the_replacement_envelope_is_truthy_and_record_free(self):
        """`formatted_output` is REPLACED, not blanked.

        Clearing it to a falsy value would re-open the very
        `formatted_output or result` shaping the containment exists to close
        (it applies no status check). The refusal therefore ships a truthy
        notice carrying a closed-set reason code and nothing else.
        """
        out = self.node(_state(record_id="", record_ref=""))["formatted_output"]
        assert out, "the withheld notice must be TRUTHY"
        assert out == {"reason": _REASON_OUTPUT_WITHHELD}
        rendered = repr(out)
        assert "ms-204" not in rendered
        assert "ch-101" not in rendered
        assert "lineworks://" not in rendered

    def test_error_branch_envelope_is_truthy_and_record_free(self):
        """The same property on the pre-existing-ERROR branch - the one molt
        flagged (2026-09-04). Direct execute(): the full node path
        short-circuits an already-errored state before execute() runs."""
        out = self.node.execute(_state(status=AgentStatus.ERROR.value, error_log=["boom"]))["formatted_output"]
        assert out
        assert out == {"reason": _REASON_WORKFLOW_FAILED}

    def test_inner_error_entries_are_not_re_emitted(self):
        """The state reducer APPENDS to error_log, so re-emitting the inner
        entries here would duplicate every line in the audit trail."""
        result = self.node.execute(_state(status=AgentStatus.ERROR.value, error_log=[_sentinel()]))
        assert "error_log" not in result

    def test_gate_violations_travel_in_error_log_only(self):
        """The violation entries are the INTERNAL channel: they name the
        offending path and they stay out of the caller's envelope."""
        result = self.node(_state(record_id="", record_ref=""))
        assert any("record_id/record_ref" in entry for entry in result["error_log"])
        assert "output gate" not in repr(result["formatted_output"])

    def test_violation_envelope_carries_no_released_text_or_paths(self):
        released = "Sent LINE WORKS message - id=ms-204"
        result = self.node(
            _state(record_id="", record_ref="", confirmation=released, result={"confirmation": released})
        )
        envelope = repr(result["formatted_output"])
        assert released not in envelope
        assert "Traceback" not in envelope
        assert "/src/" not in envelope

    def test_gate_finds_a_credential_nested_in_the_payload(self):
        """A top-level-only scan reports zero findings on exactly this case."""
        bearer_like = "Bearer " + "b" * 24
        result = self.node(
            _state(lineworks_payload=to_json({"channel_id": "ch-101", "content": {"text": bearer_like}}))
        )
        assert result["status"] == AgentStatus.ERROR.value
        assert bearer_like not in repr(result)

    def test_error_text_is_contained_not_redacted(self):
        """Direct execute(): the full node path short-circuits before this branch.

        BaseNode.__call__ skips execute() when the incoming state is already
        errored, so the error branch only runs on a direct call - which is
        exactly the surface this assertion is about.

        This replaced a redaction assertion. Redaction removed credential
        SHAPES from the error text and published the rest; a room name, a
        colleague's name or a quoted message body is not credential-shaped, so
        it survived. Containment publishes none of the text at all, which is
        why the sentinel below is deliberately not credential-shaped: a
        redactor would have passed it straight through.
        """
        bearer_like = "Bearer " + "c" * 24
        result = self.node.execute(
            _state(status=AgentStatus.ERROR.value, error_log=[f"upstream said: {bearer_like}", _sentinel()])
        )
        assert result["status"] == AgentStatus.ERROR.value
        strings = _every_string(result)
        assert not any(bearer_like in s for s in strings)
        assert not any(fragment in s for fragment in _SENTINEL_FRAGMENTS for s in strings)


def _run(state: dict) -> dict:
    """Drive post_process the way the pipeline would.

    A gate refusal goes through the full node path (trust gate, input scan,
    execute, the framework's own credential scan); an already-errored state
    goes through execute(), because BaseNode.__call__ short-circuits on it.
    """
    node = PostProcessNode()
    if state.get("status") == AgentStatus.ERROR.value:
        return node.execute(state)
    return node(state)


def _bearer(fill: str = "f") -> str:
    # Built at runtime so no credential-shaped literal is committed.
    return "Bearer " + fill * 24


_NON_SUCCESS_STATES = [
    pytest.param(
        {"status": AgentStatus.ERROR.value, "error_log": [_sentinel()]},
        id="inner-workflow-error",
    ),
    pytest.param(
        {
            "status": AgentStatus.ERROR.value,
            "error_log": [_sentinel()],
            "result": {"record_id": "ms-204", "confirmation": "Sent LINE WORKS message - id=ms-204"},
        },
        id="inner-error-with-the-answer-still-in-result",
    ),
    pytest.param(
        {"status": AgentStatus.ERROR.value, "error_log": [_sentinel(), "upstream said: " + _bearer()]},
        id="inner-error-with-a-credential-in-error-log",
    ),
    pytest.param(
        {"record_id": "", "record_ref": "", "error_log": [_sentinel()]},
        id="gate-refusal-missing-record-evidence",
    ),
    pytest.param(
        {
            "lineworks_payload": to_json({"channel_id": "ch-101", "content": {"text": _bearer()}}),
            "error_log": [_sentinel()],
        },
        id="gate-refusal-credential-nested-in-payload",
    ),
    pytest.param(
        {
            "lineworks_payload": to_json({"content": {_bearer("g"): _bearer("h")}}),
            "error_log": [_sentinel()],
        },
        id="gate-refusal-credential-shaped-mapping-key",
    ),
]


class TestErrorEnvelopeIsClosedSet:
    """On EVERY non-success path the caller-visible envelope carries closed-set
    labels only - parameterised over every path, not one of them.

    molt, 2026-09-04 (family finding): clearing the answer is a different
    property from bounding the error channel. `error_log` is node-authored text
    and, wherever a node interpolates a caught exception, upstream response
    bodies; truncation, path stripping and credential-only redaction are not
    closed-set contracts.
    """

    @pytest.mark.parametrize("overrides", _NON_SUCCESS_STATES)
    def test_every_envelope_value_is_a_declared_constant(self, overrides):
        envelope = _run(_state(**overrides))["formatted_output"]
        assert set(envelope) == {"reason"}, envelope
        assert set(envelope.values()) <= ERROR_REASONS, envelope

    @pytest.mark.parametrize("overrides", _NON_SUCCESS_STATES)
    def test_envelope_stays_truthy(self, overrides):
        """A falsy `formatted_output` re-opens `formatted_output or result` in
        AgentBaseGraph.get_output(), which applies no status check."""
        assert _run(_state(**overrides))["formatted_output"]

    @pytest.mark.parametrize("overrides", _NON_SUCCESS_STATES)
    def test_output_bearing_state_is_cleared(self, overrides):
        result = _run(_state(**overrides))
        retained = [field for field in _OUTPUT_BEARING_FIELDS if field not in result or result[field]]
        assert not retained, f"not cleared: {retained}; delta keys = {sorted(result)}"

    @pytest.mark.parametrize("overrides", _NON_SUCCESS_STATES)
    def test_the_sentinel_appears_nowhere_in_the_returned_mapping(self, overrides):
        """The seeded `error_log` line must reach no key and no value of the
        result, at any depth."""
        strings = _every_string(_run(_state(**overrides)))
        leaked = [fragment for fragment in _SENTINEL_FRAGMENTS if any(fragment in s for s in strings)]
        assert leaked == [], leaked

    def test_a_credential_shaped_key_is_withheld_from_the_violation_label(self):
        """The label rides `error_log`, and the framework's own S-3 scan walks
        every string leaf of the node result and RAISES on a credential. A
        raise there would discard the cleared fields this containment just
        built and re-open the `formatted_output or result` fallback - so the
        key is withheld from the label rather than quoted into it.
        """
        key = _bearer("g")
        result = _run(_state(lineworks_payload=to_json({"content": {key: _bearer("h")}})))
        reported = " ".join(result["error_log"])
        assert "credential-like value" in reported, reported
        assert key not in reported, reported
        assert "<withheld>" in reported, reported
        # ...and the clearing survived, which is what the withholding protects.
        assert result["formatted_output"] == {"reason": _REASON_OUTPUT_WITHHELD}
        assert result["result"] is None

    def test_control_the_clean_path_still_ships_the_answer(self):
        """Without this every assertion above holds for an agent that refuses
        everything and publishes nothing."""
        result = PostProcessNode()(_state())
        assert result["status"] == AgentStatus.SUCCESS.value
        out = result["formatted_output"]
        assert out["record_id"] == "ms-204"
        assert out["confirmation"].startswith("Checked LINE WORKS message delivery")
        assert "reason" not in out


class TestSecurityGateOutputHelper:
    """The module-level domain output gate as a plain function (not a node call)."""

    def test_passes_success_with_record_evidence(self):
        violations = _security_gate_output(
            {"record_id": "ms-204", "record_ref": "lineworks://messages/ms-204", "confirmation": "ok"},
            is_success=True,
        )
        assert violations == []

    def test_blocks_success_without_record_evidence(self):
        violations = _security_gate_output(
            {"record_id": "", "record_ref": "", "confirmation": "looks done"},
            is_success=True,
        )
        assert len(violations) == 1
        assert "record_id/record_ref" in violations[0]

    def test_blocks_credential_shaped_value(self):
        # Built at runtime so no credential-shaped literal is committed.
        bearer_like = "Bearer " + "a" * 24
        violations = _security_gate_output(
            {"record_id": "ms-204", "note": bearer_like},
            is_success=True,
        )
        assert any("note" in v for v in violations)

    def test_error_output_not_required_to_carry_evidence(self):
        violations = _security_gate_output({"record_id": "", "record_ref": ""}, is_success=False)
        assert violations == []

    def test_blocks_an_undeclared_output_field(self):
        """A response key nobody declared cannot ship, whatever it holds."""
        violations = _security_gate_output(
            {"record_id": "ms-204", "raw_room_transcript": "..."},
            is_success=True,
        )
        assert any("declared output contract" in v for v in violations)

    def test_walks_nested_structures_and_the_top_level_control(self):
        """Both directions: the nested probe alone cannot tell a blind gate from a bad probe."""
        bearer_like = "Bearer " + "d" * 24
        nested = _security_gate_output(
            {"record_id": "ms-204", "lineworks_payload": {"content": {"text": bearer_like}}},
            is_success=True,
        )
        assert any("lineworks_payload.content.text" in v for v in nested)
        top = _security_gate_output({"record_id": "ms-204", "confirmation": bearer_like}, is_success=True)
        assert any("confirmation" in v for v in top)

    def test_ordinary_domain_output_is_untouched(self):
        """Structural tokens in a real confirmation must not read as credentials."""
        violations = _security_gate_output(
            {
                "record_id": "ms-204",
                "record_ref": "lineworks://messages/ms-204",
                "channel_id": "ch-101",
                "delivery_status": "delivered",
                "intent": "check_delivery",
                "confirmation": "Checked LINE WORKS message delivery - status=delivered - id=ms-204",
                "lineworks_payload": {"message_id": "ms-204", "limit": 10},
            },
            is_success=True,
        )
        assert violations == []
