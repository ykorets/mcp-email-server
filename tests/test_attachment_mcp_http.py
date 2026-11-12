"""Exercise the real MCP HTTP endpoint, schema, context and MIME draft pipeline."""

import base64
import hashlib
import json
from email.parser import BytesParser
from email.policy import default
from unittest.mock import AsyncMock

import pytest
from starlette.testclient import TestClient

from mcp_email_server import app
from mcp_email_server.emails.attachments import BinaryAttachment, TransferStore
from mcp_email_server.emails.classic import ClassicEmailHandler


def rpc(client, method, params, identity="a", request_id=1, session_id=None):
    headers = {"accept": "application/json, text/event-stream", "x-email-mcp-caller": identity * 64}
    if session_id:
        headers["mcp-session-id"] = session_id
    response = client.post(
        "/mcp", headers=headers, json={"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}
    )
    assert response.status_code == 200, response.text
    if response.headers["content-type"].startswith("text/event-stream"):
        value = json.loads(next(x[6:] for x in response.text.splitlines() if x.startswith("data: ")))
    else:
        value = response.json()
    return value, response.headers.get("mcp-session-id", session_id)


def test_remote_client_uploads_downloads_and_saves_real_mime_draft(monkeypatch, email_settings):
    transfers = TransferStore()
    monkeypatch.setattr(app, "attachment_transfers", transfers)
    saved = []

    async def append(msg, incoming_server):
        saved.append(msg.as_bytes())
        return "Drafts"

    monkeypatch.setattr("mcp_email_server.emails.classic.append_to_drafts", append)
    handler = ClassicEmailHandler(email_settings)
    raw = b"%PDF-1.7\x00\xffbinary bytes"
    handler.incoming_client.get_attachment = AsyncMock(
        return_value=BinaryAttachment("invoice.pdf", "application/pdf", raw)
    )
    monkeypatch.setattr(app, "dispatch_handler", lambda account: handler)
    http = app.mcp.streamable_http_app()
    with TestClient(http, base_url="http://localhost:8080") as client:
        _, session = rpc(
            client,
            "initialize",
            {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "attachment-test", "version": "1"},
            },
        )
        response = client.post(
            "/mcp",
            headers={"accept": "application/json, text/event-stream", "mcp-session-id": session},
            json={"jsonrpc": "2.0", "method": "notifications/initialized"},
        )
        assert response.status_code == 202

        def call(name, args, identity="a"):
            value, _ = rpc(
                client, "tools/call", {"name": name, "arguments": args}, identity=identity, session_id=session
            )
            assert "error" not in value, value
            return value["result"]

        tools, _ = rpc(client, "tools/list", {}, session_id=session)
        draft = next(t for t in tools["result"]["tools"] if t["name"] == "create_draft")
        assert "attachment_upload_ids" in draft["inputSchema"]["properties"]
        assert "ctx" not in draft["inputSchema"]["properties"]
        result = call(
            "begin_attachment_upload",
            {
                "name": "invoice.pdf",
                "mime_type": "application/pdf",
                "size": len(raw),
                "sha256": hashlib.sha256(raw).hexdigest(),
            },
        )
        assert "structuredContent" in result, result
        handle = result["structuredContent"]["upload_id"]
        foreign = call(
            "upload_attachment_chunk",
            {"upload_id": handle, "offset": 0, "content_base64": base64.b64encode(raw).decode()},
            identity="b",
        )
        assert foreign["isError"] is True
        result = call(
            "upload_attachment_chunk",
            {"upload_id": handle, "offset": 0, "content_base64": base64.b64encode(raw).decode()},
        )
        assert result["structuredContent"]["ready"]
        result = call(
            "create_draft",
            {
                "account_name": "account",
                "recipients": ["recipient@example.invalid"],
                "subject": "Remote draft",
                "body": "Please review",
                "attachment_upload_ids": [handle],
            },
        )
        assert not result.get("isError", False)
        assert len(saved) == 1 and not transfers.entries
        message = BytesParser(policy=default).parsebytes(saved[0])
        part = next(message.iter_attachments())
        assert part.get_payload(decode=True) == raw
        assert part.get_filename() == "invoice.pdf"
        result = call("get_attachment", {"account_name": "account", "email_id": "42", "attachment_id": "2"})
        resource = next(c for c in result["content"] if c["type"] == "resource")["resource"]
        assert resource["mimeType"] == "application/pdf"
        assert base64.b64decode(resource["blob"]) == raw
        assert result["structuredContent"]["sha256"] == hashlib.sha256(raw).hexdigest()
        assert not transfers.entries


@pytest.mark.asyncio
async def test_unknown_append_is_never_repeated_in_another_folder(email_server, monkeypatch):
    import asyncio

    from mcp_email_server.emails.drafts import append_to_drafts, compose_draft_message

    imap = AsyncMock()
    imap._client_task = asyncio.get_running_loop().create_future()
    imap._client_task.set_result(None)
    imap.list.return_value = ("OK", [])
    imap.select.return_value = ("OK", [])
    imap.append.side_effect = TimeoutError("ack lost")
    monkeypatch.setattr("mcp_email_server.emails.drafts.aioimaplib.IMAP4_SSL", lambda *a, **kw: imap)
    with pytest.raises(RuntimeError, match="unknown"):
        await append_to_drafts(
            compose_draft_message("from@example.invalid", ["to@example.invalid"], "s", "b"), email_server
        )
    assert imap.append.await_count == 1
    imap.logout.assert_awaited_once()
