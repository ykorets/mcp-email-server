import asyncio
from email.mime.text import MIMEText
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from mcp_email_server.app import mcp
from mcp_email_server.emails.classic import ClassicEmailHandler, EmailClient
from mcp_email_server.emails.folders import listed_folders


def connected():
    imap = AsyncMock()
    imap._client_task = asyncio.get_running_loop().create_future()
    imap._client_task.set_result(None)
    imap.id.return_value = SimpleNamespace(result="OK")
    imap.protocol.capabilities = {"IMAP4REV1", "MOVE"}
    imap.list.return_value = ("OK", [b'(\\Trash) "/" "[Gmail]/&BBoEPgRABDcEOAQ9BDA-"'])
    imap.select.return_value = ("OK", [])
    imap.uid.return_value = ("OK", [])
    return imap


@pytest.mark.asyncio
async def test_trash_uses_only_atomic_move_and_never_expunge(email_server):
    client = EmailClient(email_server)
    imap = connected()
    with patch.object(client, "_imap_connect", return_value=imap):
        assert await client.delete_emails(["42", "42", "43"]) == (["42", "43"], [])
    assert [c.args[0] for c in imap.uid.call_args_list] == ["move", "move"]
    assert all(c.args[2] == '"[Gmail]/&BBoEPgRABDcEOAQ9BDA-"' for c in imap.uid.call_args_list)
    imap.expunge.assert_not_called()
    imap.logout.assert_awaited_once()


@pytest.mark.asyncio
async def test_no_move_capability_or_folder_is_fail_closed(email_server):
    for folder, caps in [([], {"MOVE"}), ([b'(\\Trash) "/" Trash'], set())]:
        imap = connected()
        imap.list.return_value = ("OK", folder)
        imap.protocol.capabilities = caps
        with patch.object(EmailClient, "_imap_connect", return_value=imap), pytest.raises(ValueError):
            await EmailClient(email_server).move_to_trash(["42"])
        imap.uid.assert_not_called()
        imap.expunge.assert_not_called()


@pytest.mark.asyncio
async def test_trash_itself_never_emptied_and_unknown_move_not_replayed(email_server):
    imap = connected()
    imap.list.return_value = ("OK", [b'(\\Trash) "/" Trash'])
    with patch.object(EmailClient, "_imap_connect", return_value=imap):
        assert await EmailClient(email_server).move_to_trash(["42"], "Trash") == (["42"], [])
    imap.uid.assert_not_called()
    imap.uid.side_effect = TimeoutError()
    with patch.object(EmailClient, "_imap_connect", return_value=imap):
        assert await EmailClient(email_server).move_to_trash(["42", "43"]) == ([], ["42", "43"])
    assert imap.uid.await_count == 1


@pytest.mark.asyncio
async def test_gmail_never_appends_an_extra_sent_copy(email_settings):
    email_settings.save_to_sent = True
    email_settings.outgoing.host = "smtp.gmail.com"
    handler = ClassicEmailHandler(email_settings)
    with (
        patch.object(handler.outgoing_client, "send_email", AsyncMock(return_value=MIMEText("body"))) as send,
        patch.object(handler.outgoing_client, "append_to_sent", AsyncMock()) as append,
    ):
        result = await handler.send_email(["owner@example.invalid"], "s", "b")
    assert result["sent_copy"] == "provider_managed"
    send.assert_awaited_once()
    append.assert_not_called()


@pytest.mark.asyncio
async def test_sent_unknown_append_stops_and_existing_message_is_not_duplicated(email_server):
    for exists, expected_calls in [(False, 1), (True, 0)]:
        imap = connected()
        imap.list.return_value = ("OK", [b'(\\Sent) "/" Sent'])
        imap.uid_search.return_value = ("OK", [b"42" if exists else b""])
        imap.append.side_effect = TimeoutError()
        msg = MIMEText("body")
        msg["Message-ID"] = "<synthetic@example.invalid>"
        with patch("mcp_email_server.emails.classic.aioimaplib.IMAP4_SSL", return_value=imap):
            result = await EmailClient(email_server).append_to_sent(msg, email_server)
        assert result is exists
        assert imap.append.await_count == expected_calls


@pytest.mark.asyncio
async def test_catalog_has_no_permanent_delete_tool():
    names = {t.name for t in await mcp.list_tools()}
    assert "move_to_trash" in names and "delete_emails" not in names


def test_list_handles_literal_quoted_and_bare_special_names():
    assert listed_folders([b'(\\Sent) "/" Sent'])[0][1] == "Sent"
    assert listed_folders([b'(\\Trash) "/" {5}', bytearray(b"Trash")])[0][1] == "Trash"
    assert not listed_folders([b'(\\Noselect \\Trash) "/" "Trash"'])
