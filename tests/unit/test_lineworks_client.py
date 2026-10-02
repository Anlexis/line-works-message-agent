# CMN-C2-284 - Unit tests: LineWorksClient service (LINE WORKS Bot REST API shape)
# Adapted from the peer template tool-calling golden (kaonavi_client -> lineworks_client).
# Pure service layer (stdlib-only, no framework imports) - plain function tests.

import pytest

from src.services.lineworks_client import LineWorksApiError, LineWorksClient


def test_send_message_success_with_injected_post():
    captured = {}

    def post(url, headers, body):
        captured["url"] = url
        captured["headers"] = headers
        captured["body"] = body
        return 200, {"message_id": "ms-9001"}

    client = LineWorksClient("https://lineworks.example.test/v1.0/", bot_id="bot-9", post=post)
    payload = {"content": {"type": "text", "text": "hello team"}}
    resp = client.send_message("ch-101", payload, "tok123")
    assert resp["message_id"] == "ms-9001"
    assert captured["url"] == "https://lineworks.example.test/v1.0/bots/bot-9/channels/ch-101/messages"
    # LINE WORKS Bot REST API auth: the per-call token travels as a Bearer header.
    assert captured["headers"]["Authorization"] == "Bearer tok123"
    assert captured["headers"]["Content-Type"] == "application/json"
    assert captured["body"] == payload


def test_get_message_status_success_with_injected_get():
    captured = {}

    def get(url, headers, body):
        captured["url"] = url
        captured["body"] = body
        return 200, {"message_id": "ms-204", "status": "delivered"}

    client = LineWorksClient("https://lineworks.example.test/v1.0", bot_id="bot-9", get=get)
    resp = client.get_message_status("ms-204", "tok")
    assert resp["status"] == "delivered"
    assert captured["url"] == "https://lineworks.example.test/v1.0/bots/bot-9/messages/ms-204/status"
    assert captured["body"]["message_id"] == "ms-204"


def test_list_room_activity_success_with_injected_get():
    captured = {}

    def get(url, headers, body):
        captured["url"] = url
        captured["body"] = body
        return 200, {"channel_id": "ch-101", "activity": []}

    client = LineWorksClient("https://lineworks.example.test/v1.0", bot_id="bot-9", get=get)
    resp = client.list_room_activity("ch-101", "tok", limit=5)
    assert resp["channel_id"] == "ch-101"
    assert captured["url"] == "https://lineworks.example.test/v1.0/bots/bot-9/channels/ch-101/messages"
    assert captured["body"]["limit"] == 5


def test_non_2xx_raises_lineworks_api_error():
    def post(url, headers, body):
        return 400, {"description": "content.text is malformed"}

    client = LineWorksClient("https://lineworks.example.test/v1.0", post=post)
    with pytest.raises(LineWorksApiError) as exc:
        client.send_message("ch-101", {"content": {}}, "tok")
    assert exc.value.status_code == 400
    assert "content.text is malformed" in str(exc.value)


def test_default_stub_transport_status_shape():
    # No transport injected -> deterministic, network-free stub.
    client = LineWorksClient()
    assert client.uses_stub_transport is True
    resp = client.get_message_status("ms-204", "tok")
    assert resp.get("_stub") is True
    assert resp["message_id"] == "ms-204"
    assert resp["status"] == "delivered"


def test_default_stub_transport_send_returns_receipt():
    client = LineWorksClient()
    resp = client.send_message("ch-101", {"content": {"type": "text", "text": "hello team"}}, "tok")
    assert resp.get("_stub") is True
    assert resp["message_id"].startswith("ms-")


def test_default_stub_transport_activity_echoes_channel():
    client = LineWorksClient()
    resp = client.list_room_activity("ch-101", "tok")
    assert resp.get("_stub") is True
    assert resp["channel_id"] == "ch-101"
    assert isinstance(resp["activity"], list) and resp["activity"]


def test_injected_transport_disables_stub_flag():
    client = LineWorksClient(get=lambda url, headers, body: (200, {"activity": []}))
    assert client.uses_stub_transport is False
