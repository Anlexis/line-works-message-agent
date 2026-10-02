# Proof of boundary: what the CALLER actually receives on every non-success
# path, read off the real ASGI /invoke response body.
#
# molt, 2026-09-04, family-level NEEDS-FIX on the Wave 9 CMN family:
#
#   "That log can contain upstream exception/response text; truncation, path
#    stripping, or credential-only redaction is not a closed-set error
#    contract. Identifiers, names, emails, and arbitrary third-party response
#    bodies remain possible. The caller-visible error must instead be
#    closed-set labels only."
#
# `error_log` is node-authored text - an upstream failure puts a response body,
# identifiers and names in it - and it is the INTERNAL channel: the state
# reducer appends to it, the audit trail needs it, and the invoke body must
# carry none of it under any key.
#
# The sibling file tests/unit/test_post_process_node.py holds the same
# properties at the node boundary, parameterised over every path. This file
# holds them where it counts: through the HTTP adapter, the compiled graph and
# AgentBaseGraph.get_output(), which projects `formatted_output or result` with
# no status check.
#
# The sentinel is seeded by patching one inner node's execute() on its CLASS:
# GraphNode builds the inner graph afresh per invoke, so the patched node is
# what runs, and the line rides the state reducer into the outer state exactly
# as a real node's would.

import json
import os
import warnings

import pytest

from framework.schemas.agent_status import AgentStatus

_TOKEN = "pb-envelope-test-token"

_STATUS_REQUEST = "Check the delivery status of message ms-204 in channel ch-101."

# The room and message this agent would be talking about. None of it is
# credential-shaped, which is the point: a redactor passes it straight through.
_CHANNEL_ID = "ch-101"
_MESSAGE_TEXT = "Standup moved to 10am - client escalation on the Acme account"


@pytest.fixture(scope="module")
def client():
    os.environ["INVOKE_AUTH_TOKEN"] = _TOKEN
    with warnings.catch_warnings():
        # Import-time noise from the sync test client shim, not app behaviour.
        warnings.simplefilter("ignore")
        from fastapi.testclient import TestClient

        import src.api.server as server

        with TestClient(server.app) as test_client:
            yield test_client


def _invoke(client, payload):
    return client.post("/invoke", json=payload, headers={"Authorization": f"Bearer {_TOKEN}"})


class TestInvokeBodyCarriesClosedSetLabelsOnly:
    """The caller's channel, end to end."""

    _FRAGMENTS = ("A. Tanaka", "boom: upstream", "sk-" + "live-")

    @staticmethod
    def _sentinel() -> str:
        # Assembled at runtime (no credential-shaped literal is committed) and
        # chosen to match no credential detector: text no redactor would catch.
        token = "sk-" + "live-" + "x" * 3
        return "boom: upstream said {'customer':'A. Tanaka','token':'" + token + "'}"

    @classmethod
    def _every_string(cls, value) -> list:
        """Every string reachable in the body - keys AND values, at any depth."""
        if isinstance(value, dict):
            return [s for k, v in value.items() for s in (*cls._every_string(k), *cls._every_string(v))]
        if isinstance(value, (list, tuple)):
            return [s for item in value for s in cls._every_string(item)]
        return [value if isinstance(value, str) else str(value)]

    @classmethod
    def _leaked(cls, body) -> list:
        strings = cls._every_string(body)
        return [fragment for fragment in cls._FRAGMENTS if any(fragment in s for s in strings)]

    def test_inner_workflow_error_body_carries_none_of_error_log(self, client, monkeypatch):
        """The LINE WORKS call fails and reports an upstream-shaped line. The
        inner ERROR is routed straight to finalize, so `output` is withheld
        outright, and the line reaches no key and no value of the body."""
        from src.nodes.call_lineworks_api_node import CallLineWorksApiNode

        sentinel = self._sentinel()

        def failing_call(self, state, config=None):
            return {"status": AgentStatus.ERROR.value, "error_log": [sentinel]}

        monkeypatch.setattr(CallLineWorksApiNode, "execute", failing_call)
        response = _invoke(client, {"input": _STATUS_REQUEST, "session_id": "pb-http-inner-error"})

        assert response.status_code == 200
        body = response.json()
        assert body["status"] == AgentStatus.ERROR.value
        assert not body["output"]
        assert "error_log" not in body
        assert self._leaked(body) == [], self._leaked(body)
        rendered = json.dumps(body, default=str)
        for fragment in ("Subgraph", "Traceback", "/src/"):
            assert fragment not in rendered, fragment

    def test_gate_refusal_body_carries_the_reason_code_only(self, client, monkeypatch):
        """The LIVE post_process path: the inner workflow succeeds with an
        upstream-shaped line in error_log and without record evidence, so the
        outer gate refuses. The body carries the reason code - not the line,
        not the gate's finding, not the answer that was merged into `result`."""
        from src.nodes.confirm_node import ConfirmNode

        sentinel = self._sentinel()
        released = f"Sent LINE WORKS message to {_CHANNEL_ID} - '{_MESSAGE_TEXT}'"

        def confirming_without_evidence(self, state):
            return {
                "status": AgentStatus.SUCCESS.value,
                "record_id": "",
                "record_ref": "",
                "confirmation": released,
                "error_log": [sentinel],
            }

        monkeypatch.setattr(ConfirmNode, "execute", confirming_without_evidence)
        response = _invoke(client, {"input": _STATUS_REQUEST, "session_id": "pb-http-gate-refusal"})

        assert response.status_code == 200
        body = response.json()
        assert body["status"] == AgentStatus.ERROR.value
        assert body["output"] == {"reason": "output_withheld_by_gate"}
        assert "error_log" not in body
        assert self._leaked(body) == [], self._leaked(body)
        rendered = json.dumps(body, default=str)
        assert released not in rendered
        assert _MESSAGE_TEXT not in rendered
        assert "output gate" not in rendered

    def test_lineworks_error_body_reaches_nothing_the_caller_sees(self, client, monkeypatch):
        """The source half, over the wire: LINE WORKS answers 403 with a body
        that names the channel and quotes the message. The node reduces it to
        the HTTP status for error_log, and the caller's body carries none of it
        either way."""
        from src.services.lineworks_client import LineWorksClient

        sentinel = self._sentinel()
        upstream = f"denied for channel {_CHANNEL_ID}: '{_MESSAGE_TEXT}' (bot not a member) {sentinel}"

        def forbidden(self, url, headers, json_body):
            return 403, {"description": upstream}

        monkeypatch.setattr(LineWorksClient, "_stub_transport", forbidden)
        response = _invoke(client, {"input": _STATUS_REQUEST, "session_id": "pb-http-lineworks-403"})

        assert response.status_code == 200
        body = response.json()
        assert body["status"] == AgentStatus.ERROR.value
        assert not body["output"]
        assert "error_log" not in body
        assert self._leaked(body) == [], self._leaked(body)
        rendered = json.dumps(body, default=str)
        assert _MESSAGE_TEXT not in rendered
        assert "bot not a member" not in rendered

    def test_control_an_unpatched_request_still_ships_the_answer(self, client):
        """Without this, every assertion above holds for an agent that refuses
        everything and publishes nothing."""
        body = _invoke(client, {"input": _STATUS_REQUEST, "session_id": "pb-http-envelope-control"}).json()

        assert body["status"] == AgentStatus.SUCCESS.value
        assert body["output"]["message_id"] == "ms-204"
        assert body["output"]["record_ref"] == "lineworks://messages/ms-204"
        assert body["output"]["confirmation"]
        assert "reason" not in body["output"]
