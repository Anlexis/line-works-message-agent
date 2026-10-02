"""AgentCore Platform v1.0 - LINE WORKS REST API client.

Service layer: a thin wrapper around the LINE WORKS (business messaging SaaS)
Bot REST API message endpoints. Contains NO business logic, NO routing, and NO
credentials - the integration token is passed in per call by the node (which
reads it via ctx.secrets). This module imports no framework/SDK internals -
pure stdlib (import-isolation, PB-4).

LIMITATION (deliberate, documented):
    The DEFAULT transport is a deterministic, NETWORK-FREE stub. It returns the
    documented LINE WORKS response shapes (a ``message_id`` receipt for sends;
    a delivery-status record for status lookups; an ``activity`` list for
    recent room activity), with synthetic ids derived from the request, so the
    pipeline is runnable and testable without a live LINE WORKS tenant or the
    ``requests`` package - it does NOT perform a live LINE WORKS call. The rule
    it follows: never fake a live call; document the limitation.

    To perform real LINE WORKS calls, inject live transports (requests-based
    ``post`` / ``get``) at construction time; the method contracts and payload
    shapes mirror the LINE WORKS Bot API, so no business-logic change is needed
    to go live. A live transport should set its own socket timeout - the caller
    node measures a deadline around the call, but only the socket can stop a
    hung connection. A live transport also requires a real integration token (see
    CallLineWorksApiNode - the stub runs without one because no request ever
    leaves the process).
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Callable

# A transport callable: (url, headers, json_body) -> (status_code, response_dict)
Transport = Callable[[str, "dict[str, Any]", "dict[str, Any]"], "tuple[int, dict[str, Any]]"]

_BASE_URL = "https://www.worksapis.com/v1.0"
_DEFAULT_BOT_ID = "stub-bot"


class LineWorksApiError(Exception):
    """Raised when the LINE WORKS REST API returns a non-2xx status."""

    def __init__(self, status_code: int, message: str) -> None:
        self.status_code = status_code
        super().__init__(f"LINE WORKS API error {status_code}: {message}")


class LineWorksClient:
    """LINE WORKS Bot REST API message client.

    Args:
        base_url: LINE WORKS API base URL (default https://www.worksapis.com/v1.0).
        bot_id: bot identifier used in the Bot API paths (manifest-provided;
            the non-routable default is only ever seen by the stub transport).
        post/get: optional injected transports (tests or a live client).
            When none is injected, a deterministic NETWORK-FREE stub is used
            (see the module docstring - it returns the documented shape without
            a live LINE WORKS call).
    """

    def __init__(
        self,
        base_url: str = _BASE_URL,
        bot_id: str = _DEFAULT_BOT_ID,
        *,
        post: Transport | None = None,
        get: Transport | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._bot_id = bot_id or _DEFAULT_BOT_ID
        self._post = post
        self._get = get

    # -- transport mode --------------------------------------------------------

    @property
    def uses_stub_transport(self) -> bool:
        """True when NO live transport is injected (the network-free default)."""
        return self._post is None and self._get is None

    # -- auth ----------------------------------------------------------------

    def _headers(self, api_token: str) -> "dict[str, str]":
        """Build the LINE WORKS REST API auth headers.

        api_token is supplied per-call by the node (from ctx.secrets); it is
        never persisted on the instance or logged.
        """
        return {
            "Content-Type": "application/json",
            "Authorization": "Bearer " + api_token,
        }

    # -- deterministic stub transport (default; NO network) -------------------

    def _stub_transport(
        self, url: str, headers: "dict[str, Any]", json_body: "dict[str, Any]"
    ) -> "tuple[int, dict[str, Any]]":
        """Deterministic, network-free stub - returns the documented LINE WORKS shape.

        NOT a live call. Synthetic ids are derived from the request so the
        response is stable and inspectable. See the module docstring for the
        limitation and how to inject live transports.
        """
        seed = url + "|" + json.dumps(json_body, sort_keys=True, ensure_ascii=False, default=str)
        digest = hashlib.sha256(seed.encode("utf-8")).hexdigest()
        op = json_body.get("_lineworks_op")
        if op == "status":
            # Documented delivery-status record shape for a message lookup.
            mid = str(json_body.get("message_id", "")) or f"ms-{digest[:8]}"
            return 200, {
                "message_id": mid,
                "status": "delivered",
                "delivered_at": "1970-01-01 00:00:00",
                "_stub": True,  # marks the network-free stub response
            }
        if op == "activity":
            # Recent room activity: the channel's latest message events.
            cid = str(json_body.get("channel_id", "")) or f"ch-{digest[:8]}"
            return 200, {
                "channel_id": cid,
                "activity": [
                    {
                        "type": "message",
                        "message_id": f"ms-{digest[:8]}",
                        "created_at": "1970-01-01 00:00:00",
                    }
                ],
                "_stub": True,  # marks the network-free stub response
            }
        # POST .../channels/{channelId}/messages (send) - receipt shape with a
        # synthetic message_id echo so the caller can reference the sent
        # message without a follow-up lookup.
        return 200, {
            "message_id": f"ms-{digest[:8]}",
            "_stub": True,  # marks the network-free stub response
        }

    def _resolve(self, injected: Transport | None) -> Transport:
        return injected or self._stub_transport

    # -- public API ---------------------------------------------------------

    def send_message(self, channel_id: str, payload: "dict[str, Any]", api_token: str) -> "dict[str, Any]":
        """POST /bots/{botId}/channels/{channelId}/messages - send a message.

        ``payload`` is the documented request body (``{"content": {"type":
        "text", "text": ...}}``). Returns the parsed response dict (send
        receipt with ``message_id``). Raises LineWorksApiError on a non-2xx
        status.
        """
        url = f"{self._base_url}/bots/{self._bot_id}/channels/{channel_id}/messages"
        transport = self._resolve(self._post)
        status, body = transport(url, self._headers(api_token), payload)
        if not (200 <= status < 300):
            raise LineWorksApiError(status, _err_message(body))
        return body

    def get_message_status(self, message_id: str, api_token: str) -> "dict[str, Any]":
        """GET /bots/{botId}/messages/{messageId}/status - look up delivery status.

        Returns the parsed response dict (containing ``message_id`` and
        ``status``). Raises LineWorksApiError on non-2xx.
        """
        url = f"{self._base_url}/bots/{self._bot_id}/messages/{message_id}/status"
        transport = self._resolve(self._get)
        status, body = transport(url, self._headers(api_token), {"_lineworks_op": "status", "message_id": message_id})
        if not (200 <= status < 300):
            raise LineWorksApiError(status, _err_message(body))
        return body

    def list_room_activity(self, channel_id: str, api_token: str, limit: int = 10) -> "dict[str, Any]":
        """GET /bots/{botId}/channels/{channelId}/messages - list recent room activity.

        Returns the parsed response dict (containing the ``activity`` list of
        recent message events). Raises LineWorksApiError on non-2xx.
        """
        url = f"{self._base_url}/bots/{self._bot_id}/channels/{channel_id}/messages"
        transport = self._resolve(self._get)
        status, body = transport(
            url,
            self._headers(api_token),
            {"_lineworks_op": "activity", "channel_id": channel_id, "limit": limit},
        )
        if not (200 <= status < 300):
            raise LineWorksApiError(status, _err_message(body))
        return body


def _err_message(body: Any) -> str:
    """Extract a human-readable error message from a LINE WORKS error body."""
    if isinstance(body, dict):
        desc = body.get("description")
        if desc:
            return str(desc)
        msg = body.get("message")
        if msg:
            return str(msg)
        code = body.get("code")
        if code:
            return str(code)
    return str(body)
