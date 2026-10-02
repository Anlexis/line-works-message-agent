# Output-containment boundary test.
#
# The base graph shapes its response as `formatted_output` OR `state["result"]`
# - the fallback applies on an ERROR status too. So an output gate that merely
# raises, or returns an error without clearing state, still ships the un-gated
# inner answer inside the error envelope. Containment means the answer is gone.
#
# The gate is driven through the REAL compiled graph, with the violation
# introduced where a violation would actually come from: the inner workflow
# result. Both directions are asserted - the clean path must still ship, or a
# gate that refuses everything would pass this file.
#
# What the refusal publishes is a CLOSED SET (molt, 2026-09-04): the reason
# code only. The gate's own finding names a field path rather than a value, but
# it is still node-authored text, so it travels on error_log and not to the
# caller. See tests/proof_of_boundary/test_error_envelope_closed_set.py for
# that property on every non-success path through the HTTP adapter.

import json
import re

import pytest

from framework.schemas.agent_status import AgentStatus
from framework.schemas.invocation_context import InvocationContext
from framework.schemas.trust_level import TrustLevel

from src.graph.graph import LineWorksMessageAgent
from src.nodes.post_process_node import _CREDENTIAL_LIKE_RE
from src.schemas.state import to_json

_REQUEST = "Send an update to the project room"
_CREDENTIAL = "Bearer " + "e" * 24
_RELEASED = "Sent LINE WORKS message - ref=lineworks://messages/ms-777 - id=ms-777"


@pytest.fixture
def agent():
    graph = LineWorksMessageAgent()
    graph.compile()
    return graph


def _ctx(name):
    return InvocationContext(caller_id=name, caller_trust_level=TrustLevel.VERIFIED_EXTERNAL)


def _leaky_inner_result():
    """What the inner workflow hands back when a credential rode along."""
    return {
        "output": {"record_id": "ms-777", "record_ref": "lineworks://messages/ms-777", "confirmation": _RELEASED},
        "status": AgentStatus.SUCCESS.value,
        "intent": "send_message",
        "channel_id": "ch-101",
        "message_id": "ms-777",
        "delivery_status": "",
        "record_id": "ms-777",
        "record_ref": "lineworks://messages/ms-777",
        "confirmation": _RELEASED,
        # The credential is NESTED, one level inside the assembled request body -
        # a top-level-only scan reports zero findings on exactly this shape.
        "lineworks_payload": to_json({"channel_id": "ch-101", "content": {"type": "text", "text": _CREDENTIAL}}),
        "redaction_flags": "",
        "error_log": [],
    }


class TestOutputContainment:
    def test_the_probe_itself_recognises_a_credential(self):
        """Control: a scan that cannot see the planted value proves nothing."""
        assert _CREDENTIAL_LIKE_RE.search(_CREDENTIAL)
        assert _CREDENTIAL_LIKE_RE.search(json.dumps(_leaky_inner_result()))

    def test_clean_pipeline_still_ships(self, agent):
        result = agent.invoke(
            _REQUEST,
            ctx=_ctx("pb-contain-clean"),
            input_context={"channel_id": "ch-101", "message": {"text": "Standup moved to 10am"}},
        )
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["output"]["confirmation"]

    def test_violating_output_is_contained_end_to_end(self, agent, monkeypatch):
        """A credential nested in the inner result must reach nobody."""
        monkeypatch.setattr(
            "src.graph.domain_workflow_graph.LineWorksWorkflowGraph.get_output",
            lambda self, state: _leaky_inner_result(),
        )
        result = agent.invoke(
            _REQUEST,
            ctx=_ctx("pb-contain-leak"),
            input_context={"channel_id": "ch-101", "message": {"text": "Standup moved to 10am"}},
        )
        body = json.dumps(result, ensure_ascii=False, default=str)

        assert result["status"] == AgentStatus.ERROR.value
        # The credential is gone...
        assert not _CREDENTIAL_LIKE_RE.search(body)
        # ...and so is the answer it rode in on - status alone is not containment.
        assert _RELEASED not in body
        assert "ms-777" not in body
        # No traceback, no source paths.
        assert "Traceback" not in body
        assert not re.search(r"/src/|\.py\b", body)
        # The envelope carries the closed-set reason code and NOTHING else.
        # The gate's own finding is node-authored text naming a field path, so
        # it stays on the internal channel (error_log) where the audit trail
        # reads it; it used to ride the envelope, which is the leak molt
        # flagged on 2026-09-04.
        assert result["output"] == {"reason": "output_withheld_by_gate"}
        assert "credential-like value" not in body
        assert "lineworks_payload" not in body
