"""Run aioimaplib against an isolated loopback IMAP fixture, never real mail."""

import asyncio
import base64
import re
from email.parser import BytesParser
from email.policy import default

import pytest

from mcp_email_server.config import EmailServer, EmailSettings
from mcp_email_server.emails.attachments import BinaryAttachment
from mcp_email_server.emails.classic import ClassicEmailHandler


@pytest.mark.asyncio
async def test_native_imap_metadata_peek_and_draft_bytes():  # noqa: C901 - wire protocol fixture
    data = b"%PDF-1.7\x00\xffreal wire fixture"
    payload = base64.b64encode(data)
    mime = b'Content-Type: application/pdf\r\nContent-Transfer-Encoding: base64\r\nContent-Disposition: attachment; filename="invoice.pdf"\r\n\r\n'
    headers = b"From: sender@example.invalid\r\nTo: owner@example.invalid\r\nSubject: Fixture\r\nDate: Wed, 11 Nov 2026 12:00:00 +0000\r\nMessage-ID: <fixture@example.invalid>\r\n\r\n"
    tree = (
        b'(("TEXT" "PLAIN" NIL NIL NIL "7BIT" 5 1 NIL NIL)'
        b'("APPLICATION" "PDF" ("NAME" "invoice.pdf") NIL NIL "BASE64" '
        + str(len(payload)).encode()
        + b' NIL ("ATTACHMENT" ("FILENAME" "invoice.pdf"))) "MIXED")'
    )
    commands = []
    saved = []
    errors = []

    async def serve(reader, writer):  # noqa: C901 - explicit IMAP command dispatcher
        try:
            writer.write(b"* OK IMAP fixture ready\r\n")
            await writer.drain()
            while line := await reader.readline():
                tag, command = line.rstrip(b"\r\n").split(b" ", 1)
                commands.append(command)
                upper = command.upper()
                if upper.startswith(b"CAPABILITY"):
                    writer.write(b"* CAPABILITY IMAP4rev1 ID UIDPLUS\r\n")
                elif upper.startswith(b"LOGOUT"):
                    writer.write(b"* BYE closing\r\n" + tag + b" OK logout\r\n")
                    await writer.drain()
                    break
                elif upper.startswith(b"LIST"):
                    writer.write(b'* LIST (\\Drafts) "/" "Drafts"\r\n')
                elif upper.startswith((b"SELECT", b"EXAMINE")):
                    writer.write(
                        b"* FLAGS (\\Seen \\Draft)\r\n* 1 EXISTS\r\n* 0 RECENT\r\n* OK [UIDVALIDITY 123] stable\r\n"
                    )
                elif upper.startswith(b"UID SEARCH"):
                    writer.write(b"* SEARCH 42\r\n")
                elif upper.startswith(b"UID FETCH"):
                    if b"BODYSTRUCTURE" in upper:
                        writer.write(b"* 1 FETCH (UID 42 BODYSTRUCTURE " + tree + b")\r\n")
                    elif b"INTERNALDATE" in upper:
                        writer.write(b'* 1 FETCH (UID 42 INTERNALDATE "11-Nov-2026 12:00:00 +0000")\r\n')
                    else:
                        label, content = (
                            (b"HEADER", headers)
                            if b"HEADER" in upper
                            else ((b"2.MIME", mime) if b"2.MIME" in upper else (b"2", payload))
                        )
                        writer.write(
                            b"* 1 FETCH (UID 42 BODY["
                            + label
                            + b"] {"
                            + str(len(content)).encode()
                            + b"}\r\n"
                            + content
                            + b")\r\n"
                        )
                elif upper.startswith(b"APPEND"):
                    size = int(re.search(rb"\{(\d+)\}", command)[1])
                    writer.write(b"+ continue\r\n")
                    await writer.drain()
                    saved.append(await reader.readexactly(size))
                    await reader.readline()
                elif not upper.startswith((b"LOGIN", b"ID ")):
                    errors.append(command)
                    writer.write(tag + b" BAD unsupported\r\n")
                    await writer.drain()
                    continue
                writer.write(tag + b" OK done\r\n")
                await writer.drain()
        except Exception as exc:
            errors.append(repr(exc))
        finally:
            writer.close()
            await writer.wait_closed()

    server = await asyncio.start_server(serve, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    settings = EmailSettings(
        account_name="fixture",
        full_name="Fixture",
        email_address="owner@example.invalid",
        incoming=EmailServer(host="127.0.0.1", port=port, user_name="fixture", password="fixture", use_ssl=False),
        outgoing=EmailServer(host="127.0.0.1", port=port, user_name="fixture", password="fixture", use_ssl=False),
    )
    handler = ClassicEmailHandler(settings)

    async def exercise():
        page = await handler.get_emails_metadata(page_size=1)
        assert page.total == 1 and page.emails[0].attachment_info_available
        assert page.emails[0].attachments == ["invoice.pdf"]
        assert page.emails[0].attachment_info[0].attachment_id == "2"
        item = await handler.incoming_client.get_attachment("42", "2")
        assert item.data == data
        await handler.create_draft(
            ["to@example.invalid"],
            "Review",
            "body",
            binary_attachments=[item, BinaryAttachment("empty.txt", "text/plain", b"")],
        )
        assert len(saved) == 1
        draft = BytesParser(policy=default).parsebytes(saved[0])
        attachments = list(draft.iter_attachments())
        assert [p.get_filename() for p in attachments] == ["invoice.pdf", "empty.txt"]
        assert [p.get_payload(decode=True) for p in attachments] == [data, b""]
        assert not any(b"STORE" in c or b"EXPUNGE" in c for c in commands)
        assert not errors, errors

    try:
        await asyncio.wait_for(exercise(), 10)
    finally:
        server.close()
        await server.wait_closed()
