"""AgentCore Platform v1.0 - inner workflow Step 4: CallLineWorksApi (tool side-effect).

Performs the send/status/activity call against the LINE WORKS Bot REST API
message endpoints via src/services/lineworks_client.py.

Security posture:
  Trust: required_trust_level = ANONYMOUS. The single external trust gate lives
       on the OUTER backbone pre_process (VERIFIED_EXTERNAL), not on this inner
       node. GraphNode.execute() passes the caller's InvocationContext into the
       inner subgraph UNCHANGED (no trust elevation), so a real external caller
       runs this call under its own VERIFIED_EXTERNAL context; declaring INTERNAL
       here would deny that already-gated external caller before the call ever
       runs. The node therefore stays ANONYMOUS.
  Secrets: the integration token is read via ctx.secrets.get("LINEWORKS_TOKEN")
       (InvocationContext.from_state(state)) - never os.environ, never stored in
       state. While the deterministic NETWORK-FREE stub transport is active a
       missing token is tolerated (a sentinel placeholder is used - it is never
       sent anywhere because no request leaves the process); with a LIVE
       transport injected, a missing token is a hard status=error - a real API is
       never called unauthenticated.
  Audit: emit_trace_event() is called on the success path - a side-effect against
       an external messaging system; HTTP 4xx/5xx surfaces as status=error +
       error_log (no silent pass). The log line carries the HTTP status only -
       never the API's own message, which is remote content that can quote the
       channel or the message it refused - and a transport failure is reported
       by exception class, never by its text (which carries the request URL,
       and with it the message id).

Configuration: this node takes NO constructor arguments (nodes are no-arg).
LINE WORKS settings (base_url, bot_id) and the call deadline (timeout_s) arrive
as the JSON `lineworks_config` state field - injected by the inner graph's
_extra_initial_state() from the config/config.yaml section forwarded by
LineWorksWorkflowGraphNode._parent_config() - or via the optional
`config["configurable"]["lineworks"]` argument for direct invocation.
The client is constructed locally per call (no module-global mutation).
"""

import time
from typing import Any

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.invocation_context import InvocationContext
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event

from src.schemas.state import from_json
from src.services.lineworks_client import LineWorksApiError, LineWorksClient
from src.services.security import TIMEOUT_MAX, TIMEOUT_MIN, finite_int_in_range

_SECRET_KEY = "LINEWORKS_TOKEN"
# Placeholder handed to the network-free stub transport when no secret is
# provisioned. Never sent over any network (the stub performs no I/O) and never
# written to state or logs.
_STUB_PLACEHOLDER = "stub-transport-no-credential"

# Fallback deadline (seconds) when config/config.yaml declares none, or declares
# one outside the supported bounds. The declared value is measured against the
# wall clock around the call: a result that arrives after the deadline is
# discarded and reported as an error rather than used, because a late answer
# about an external messaging system is too late to trust. A live transport
# should also set its own socket timeout so a hung connection cannot outlive
# this check.
_DEFAULT_TIMEOUT_S = 30


class CallLineWorksApiNode(FunctionNode):
    """Send a message / check delivery / list room activity via the LINE WORKS API."""

    # The external trust gate is enforced UPSTREAM on the outer backbone
    # pre_process (VERIFIED_EXTERNAL). This inner node runs under the caller's
    # UNELEVATED context (GraphNode does not elevate trust for the subgraph), so
    # it must stay ANONYMOUS - declaring INTERNAL would deny a real external
    # caller before the call runs.
    required_trust_level = TrustLevel.ANONYMOUS

    def execute(self, state: "dict[str, Any]", config: "dict[str, Any] | None" = None) -> "dict[str, Any]":
        payload = from_json(state.get("lineworks_payload"), None)
        if not payload:
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": ["CallLineWorksApiNode: missing lineworks_payload"],
            }

        intent = state.get("intent", "check_delivery") or "check_delivery"

        # Settings: manifest section from state (graph-injected), overridable via
        # an explicit config["configurable"]["lineworks"] for direct invocation.
        # Merged into a LOCAL dict - module globals are never mutated.
        settings = dict(from_json(state.get("lineworks_config"), {}) or {})
        override = ((config or {}).get("configurable") or {}).get("lineworks") or {}
        settings.update(override)

        # The declared deadline is caller-independent config, but it is still a
        # number from a file: it goes through the same finite+bounded parser as
        # caller input, so a non-finite or absurd value degrades to the
        # documented default instead of disabling the deadline silently (every
        # comparison against NaN is False).
        timeout_s = finite_int_in_range(settings.get("timeout_s"), TIMEOUT_MIN, TIMEOUT_MAX)
        if timeout_s is None:
            timeout_s = _DEFAULT_TIMEOUT_S

        # Client built locally per call; with no injected transport it uses the
        # deterministic NETWORK-FREE stub (documented limitation, docs/02).
        base_url = str(settings.get("base_url", "") or "").strip()
        bot_id = str(settings.get("bot_id", "") or "").strip()
        if base_url:
            client = LineWorksClient(base_url=base_url, bot_id=bot_id)
        else:
            client = LineWorksClient(bot_id=bot_id)

        # Token from the bound secret provider - never os.environ / state.
        ctx = InvocationContext.from_state(state)
        api_token = ctx.secrets.get(_SECRET_KEY)
        if api_token is None:
            if client.uses_stub_transport:
                # Stub-transport limitation: no request leaves the process, so run
                # with a non-credential placeholder (see module docstring).
                api_token = _STUB_PLACEHOLDER
            else:
                return {
                    "status": AgentStatus.ERROR.value,
                    "error_log": [
                        f"CallLineWorksApiNode: secret {_SECRET_KEY} unavailable - "
                        "refusing to call a live transport unauthenticated"
                    ],
                }

        channel_id = state.get("channel_id", "") or str(payload.get("channel_id", "") or "")
        message_id = state.get("message_id", "") or str(payload.get("message_id", "") or "")
        delivery_status = ""

        started = time.monotonic()
        try:
            if intent == "check_delivery":
                if not message_id:
                    return {
                        "status": AgentStatus.ERROR.value,
                        "error_log": ["CallLineWorksApiNode: unresolved message id - " "cannot check delivery status"],
                    }
                resp = client.get_message_status(message_id, api_token) or {}
                record_id = str(resp.get("message_id", "")) or message_id
                delivery_status = str(resp.get("status", ""))
                record_ref = f"lineworks://messages/{record_id}" if record_id else ""
                message_id = record_id
            elif intent == "send_message":
                if not channel_id:
                    return {
                        "status": AgentStatus.ERROR.value,
                        "error_log": ["CallLineWorksApiNode: unresolved channel - cannot send message"],
                    }
                content = payload.get("content") or {}
                if not str(content.get("text", "") or "").strip():
                    return {
                        "status": AgentStatus.ERROR.value,
                        "error_log": ["CallLineWorksApiNode: empty message text - nothing to send"],
                    }
                resp = client.send_message(channel_id, payload, api_token) or {}
                record_id = str(resp.get("message_id", ""))
                record_ref = f"lineworks://messages/{record_id}" if record_id else ""
                message_id = record_id
            elif intent == "list_activity":
                if not channel_id:
                    return {
                        "status": AgentStatus.ERROR.value,
                        "error_log": ["CallLineWorksApiNode: unresolved channel - " "cannot list room activity"],
                    }
                resp = client.list_room_activity(channel_id, api_token) or {}
                record_id = str(resp.get("channel_id", "")) or channel_id
                record_ref = f"lineworks://channels/{record_id}/activity" if record_id else ""
                channel_id = record_id
            else:
                return {
                    "status": AgentStatus.ERROR.value,
                    "error_log": [f"CallLineWorksApiNode: unknown intent '{intent}'"],
                }
        except LineWorksApiError as exc:
            # HTTP status only. LineWorksApiError stringifies the upstream error
            # BODY, which on a live tenant is unbounded third-party text that
            # can quote the channel or the message it refused. The closed-set
            # signal travels; the body does not. The status is hoisted to a
            # local so the caught exception itself never appears in an f-string
            # - the shape that put the body in the log line to begin with.
            status_code = exc.status_code
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": [f"CallLineWorksApiNode: LINE WORKS API error {status_code}"],
            }
        except Exception as exc:  # transport failure - no silent pass
            # Exception CLASS only, for the same reason: a transport error
            # string carries the request URL, which embeds the message id.
            # Hoisted to a local for the same reason as above.
            exception_class = type(exc).__name__
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": [f"CallLineWorksApiNode: LINE WORKS call failed ({exception_class})"],
            }

        elapsed = time.monotonic() - started
        if elapsed > timeout_s:
            emit_trace_event(
                "call_lineworks_api_deadline_exceeded",
                {"intent": intent, "timeout_s": timeout_s},
                state,
            )
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": [
                    f"CallLineWorksApiNode: LINE WORKS call exceeded the {timeout_s}s " "deadline - result discarded"
                ],
            }

        # Audit the tool side-effect - intent + presence signals only, never
        # message content or credentials.
        emit_trace_event(
            "call_lineworks_api_complete",
            {
                "intent": intent,
                "has_record_id": bool(record_id),
                "stub_transport": client.uses_stub_transport,
            },
            state,
        )

        return {
            "record_id": record_id,
            "record_ref": record_ref,
            "channel_id": channel_id,
            "message_id": message_id,
            "delivery_status": delivery_status,
            "status": AgentStatus.SUCCESS.value,
        }
