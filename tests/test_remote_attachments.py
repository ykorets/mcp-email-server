"""Remote-file behaviour, adversarial inputs and actual MIME round trips."""

import asyncio
import base64
import hashlib
from email.message import EmailMessage
from email.parser import BytesParser
from email.policy import default
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from mcp_email_server import app
from mcp_email_server.emails import attachments as files
from mcp_email_server.emails.attachments import (
    BinaryAttachment,
    TransferStore,
    _sexpr,
    structure_attachments,
    structures_by_uid,
)
from mcp_email_server.emails.classic import ClassicEmailHandler, EmailClient
from mcp_email_server.emails.drafts import compose_draft_message


def caller(value="a"):
    return SimpleNamespace(
        request_context=SimpleNamespace(request=SimpleNamespace(headers={"x-email-mcp-caller": value * 64}))
    )


def upload(store, data, identity="a" * 64, name="рахунок.pdf", mime="application/pdf"):
    handle = store.begin(identity, name, mime, len(data), hashlib.sha256(data).hexdigest())
    for offset in range(0, len(data), files.CHUNK_BYTES):
        store.append(identity, handle, offset, base64.b64encode(data[offset : offset + files.CHUNK_BYTES]).decode())
    return handle


@pytest.fixture
def store(monkeypatch):
    result = TransferStore()
    monkeypatch.setattr(app, "attachment_transfers", result)
    yield result
    for handle in list(result.entries):
        result.discard(handle)


@pytest.mark.asyncio
async def test_upload_download_chunks_and_immediate_cleanup(store):
    data = bytes(range(256)) * (files.CHUNK_BYTES // 128)
    handle = upload(store, data)
    with pytest.raises(ValueError, match="unavailable"):
        store.consume("b" * 64, [handle])
    item = store.consume("a" * 64, [handle])[0]
    assert item.data == data
    assert not store.entries
    with pytest.raises(ValueError):
        store.consume("a" * 64, [handle])
    response = store.download("a" * 64, item)
    received = base64.b64decode(response["content_base64"])
    while not response["complete"]:
        with pytest.raises(ValueError):
            store.read("b" * 64, response["download_id"], response["next_offset"])
        response = store.read("a" * 64, response["download_id"], response["next_offset"])
        received += base64.b64decode(response["content_base64"])
    assert received == data
    assert response["sha256"] == hashlib.sha256(data).hexdigest()
    assert not store.entries


@pytest.mark.asyncio
async def test_retried_upload_chunk_and_digest_failure(store):
    data = b"abcdef"
    handle = store.begin("a", "file.pdf", "application/pdf", 6, hashlib.sha256(data).hexdigest())
    chunk = base64.b64encode(data[:3]).decode()
    store.append("a", handle, 0, chunk)
    assert store.append("a", handle, 0, chunk)["received_bytes"] == 3
    with pytest.raises(ValueError, match="Offset"):
        store.append("a", handle, 0, base64.b64encode(b"BAD").decode())
    with pytest.raises(ValueError, match="SHA-256"):
        store.append("a", handle, 3, base64.b64encode(b"BAD").decode())
    assert not store.entries


@pytest.mark.asyncio
async def test_incomplete_expired_and_duplicate_uploads(store):
    handle = store.begin("a", "empty.bin", "application/octet-stream", 2, "0" * 64)
    with pytest.raises(ValueError, match="incomplete"):
        store.consume("a", [handle])
    with pytest.raises(ValueError, match="Duplicate"):
        store.consume("a", [handle, handle])
    store.entries[handle].expires = 0
    with pytest.raises(ValueError, match="expired"):
        store.consume("a", [handle])
    assert not store.entries


@pytest.mark.asyncio
async def test_abandoned_files_expire_without_another_request(store, monkeypatch):
    monkeypatch.setattr(files, "TRANSFER_TTL_SECONDS", 0.01)
    upload(store, b"abandoned")
    await asyncio.sleep(0.03)
    assert not store.entries


@pytest.mark.asyncio
async def test_memory_reservations_and_empty_file(store, monkeypatch):
    monkeypatch.setattr(files, "MAX_CALLER_BYTES", 3)
    upload(store, b"abc", identity="a")
    with pytest.raises(ValueError, match="capacity"):
        upload(store, b"x", identity="a")
    monkeypatch.setattr(files, "MAX_TRANSFER_BYTES", 3)
    with pytest.raises(ValueError, match="capacity"):
        upload(store, b"x", identity="b")
    handle = upload(store, b"", identity="b")
    result = store.download("b", store.consume("b", [handle])[0])
    assert result["complete"] and result["size"] == 0 and result["content_base64"] == ""


@pytest.mark.parametrize("name", ["../secret", "a\\b", "bad\r\nBcc:x", "\x00name", "..", "x" * 181])
@pytest.mark.asyncio
async def test_rejects_server_paths_and_header_injection(store, name):
    with pytest.raises(ValueError):
        upload(store, b"x", name=name)


@pytest.mark.asyncio
async def test_malformed_oversize_chunks_and_wrong_owner_discard(store):
    handle = upload(store, b"abc")
    with pytest.raises(ValueError, match="base64"):
        store.append("a" * 64, handle, 0, "$$$")
    with pytest.raises(ValueError, match="256 KiB"):
        store.append("a" * 64, handle, 0, "x" * (files.CHUNK_BYTES * 2))
    with pytest.raises(ValueError):
        store.discard(handle, "b" * 64)
    assert handle in store.entries
    with pytest.raises(ValueError, match="size"):
        store.begin("a", "file", "application/pdf", files.MAX_FILE_BYTES + 1, "0" * 64)


def test_remote_context_cannot_be_claimed_through_tool_arguments():
    with pytest.raises(ValueError, match="Trusted"):
        app._attachment_caller(SimpleNamespace(request_context=SimpleNamespace(request=SimpleNamespace(headers={}))))
    assert app._attachment_caller(caller()) == "a" * 64


PDF = '("APPLICATION" "PDF" ("NAME" "same.pdf") NIL NIL "BASE64" 8 NIL ("ATTACHMENT" ("FILENAME" "same.pdf")))'
TEXT = '("TEXT" "PLAIN" ("CHARSET" "UTF-8") NIL NIL "7BIT" 5 1 NIL NIL)'


def test_bodystructure_duplicates_inline_rfc2231_and_encoded_size():
    image = '("IMAGE" "PNG" NIL "<logo>" NIL "BASE64" 16 NIL ("INLINE" ("FILENAME*" "utf-8\'\'%D1%84.png")))'
    tree = _sexpr(f'({TEXT}({PDF}{image}"RELATED"){PDF}"MIXED")'.encode())
    infos = structure_attachments(tree)
    assert [i.attachment_id for i in infos] == ["2.1", "2.2", "3"]
    assert [i.name for i in infos] == ["same.pdf", "ф.png", "same.pdf"]
    assert infos[0].size == 8 and infos[0].size_kind == "transfer_encoded"
    assert infos[1].mime_type == "image/png" and infos[1].disposition == "inline"


def test_uid_parser_does_not_trust_uid_text_in_a_filename():
    body = PDF.replace("same.pdf", "UID 999 BODYSTRUCTURE fake")
    raw = f"1 FETCH (BODYSTRUCTURE {body} UID 42)".encode()
    assert set(structures_by_uid([raw])) == {"42"}


def test_bodystructure_literals_and_uid_after_structure():
    raw = b'1 FETCH (BODYSTRUCTURE ("APPLICATION" "PDF" ("NAME" {5}\r\n'
    values = structures_by_uid([raw, bytearray(b"a.pdf"), b') NIL NIL "BASE64" 8) UID 7)'])
    assert values["7"][0].name == "a.pdf"


@pytest.mark.parametrize("raw", [b"(", b'("unterminated)', b"({999}\r\nx)", b"(" * 40 + b")" * 40])
def test_malformed_or_deep_structure_is_bounded(raw):
    with pytest.raises(ValueError):
        _sexpr(raw)


def test_raw_mime_metadata_empty_file_inline_and_nested():
    msg = EmailMessage()
    msg.set_content("body")
    msg.add_attachment(b"", maintype="application", subtype="octet-stream", filename="empty.bin")
    msg.add_attachment(b"image", maintype="image", subtype="png", filename="logo.png", disposition="inline")
    parsed = EmailClient(MagicMock())._parse_email_data(msg.as_bytes())
    assert parsed["attachments"] == ["empty.bin", "logo.png"]
    assert [i.size for i in parsed["attachment_info"]] == [0, 5]


@pytest.mark.asyncio
async def test_draft_round_trip_keeps_bytes_types_bcc_and_threading(email_settings, store):
    data = b"%PDF-1.7\x00\xff\x01"
    handle = upload(store, data)
    handler = ClassicEmailHandler(email_settings)
    append = AsyncMock(return_value="Drafts")
    with (
        patch.object(app, "dispatch_handler", return_value=handler),
        patch("mcp_email_server.emails.classic.append_to_drafts", append),
    ):
        await app.create_draft(
            "test_account",
            ["to@example.invalid"],
            "subject",
            "body",
            bcc=["hidden@example.invalid"],
            in_reply_to="<original@example.invalid>",
            attachment_upload_ids=[handle],
            ctx=caller(),
        )
    assert not store.entries
    msg = BytesParser(policy=default).parsebytes(append.call_args.kwargs["msg"].as_bytes())
    part = next(msg.iter_attachments())
    assert part.get_payload(decode=True) == data
    assert part.get_content_type() == "application/pdf"
    assert part.get_filename() == "рахунок.pdf"
    assert msg["Bcc"] == "hidden@example.invalid" and msg["In-Reply-To"] == "<original@example.invalid>"


@pytest.mark.asyncio
async def test_send_round_trip_bcc_envelope_and_mime(email_settings, store):
    handle = upload(store, b"png bytes", name="photo.png", mime="image/png")
    handler = ClassicEmailHandler(email_settings)
    smtp = AsyncMock()
    smtp.__aenter__.return_value = smtp
    with (
        patch.object(app, "dispatch_handler", return_value=handler),
        patch("mcp_email_server.emails.classic.aiosmtplib.SMTP", return_value=smtp),
    ):
        await app.send_email(
            "test_account",
            ["to@example.invalid"],
            "subject",
            "body",
            bcc=["hidden@example.invalid"],
            attachment_upload_ids=[handle],
            ctx=caller(),
        )
    msg = smtp.send_message.call_args.args[0]
    parsed = BytesParser(policy=default).parsebytes(msg.as_bytes())
    part = next(parsed.iter_attachments())
    assert part.get_payload(decode=True) == b"png bytes" and part.get_content_type() == "image/png"
    assert parsed["Bcc"] is None
    assert smtp.send_message.call_args.kwargs["recipients"] == ["to@example.invalid", "hidden@example.invalid"]
    assert not store.entries


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["send_email", "create_draft"])
async def test_provider_failure_discards_upload_without_resend(store, operation):
    handle = upload(store, b"x")
    handler = SimpleNamespace(**{operation: AsyncMock(side_effect=TimeoutError("unknown provider outcome"))})
    with patch.object(app, "dispatch_handler", return_value=handler):
        with pytest.raises(TimeoutError):
            await getattr(app, operation)(
                "account", ["to@example.invalid"], "subject", "body", attachment_upload_ids=[handle], ctx=caller()
            )
    assert not store.entries
    assert getattr(handler, operation).await_count == 1


@pytest.mark.asyncio
async def test_explicit_forward_sources_without_a_disk_file(store):
    read = AsyncMock(return_value=BinaryAttachment("source.pdf", "application/pdf", b"original"))
    handler = SimpleNamespace(
        incoming_client=SimpleNamespace(get_attachment=read), create_draft=AsyncMock(return_value="draft")
    )
    source = files.MailAttachmentSource(account_name="account", email_id="42", attachment_id="2")
    with patch.object(app, "dispatch_handler", return_value=handler):
        await app.create_draft(
            "account", ["to@example.invalid"], "forward", "body", attachment_sources=[source], ctx=caller()
        )
    read.assert_awaited_once_with("42", "2", "INBOX")
    assert handler.create_draft.call_args.kwargs["binary_attachments"][0].data == b"original"


def test_wire_size_limit_checks_mime_expansion_not_just_file_size(monkeypatch):
    monkeypatch.setattr("mcp_email_server.emails.drafts.MAX_MESSAGE_BYTES", 1200)
    with pytest.raises(ValueError, match="Encoded"):
        compose_draft_message(
            "from@example.invalid",
            ["to@example.invalid"],
            "s",
            "b",
            binary_attachments=[BinaryAttachment("x.pdf", "application/pdf", b"x" * 1000)],
        )


def test_attached_email_is_a_message_not_an_invalid_base64_message_part():
    data = b"From: original@example.invalid\r\nSubject: Original\r\n\r\nOriginal body\r\n"
    draft = compose_draft_message(
        "owner@example.invalid",
        ["to@example.invalid"],
        "Forward",
        "body",
        binary_attachments=[BinaryAttachment("original.eml", "message/rfc822", data)],
    )
    attachment = next(BytesParser(policy=default).parsebytes(draft.as_bytes()).iter_attachments())
    assert attachment.get_content_type() == "message/rfc822"
    assert attachment["Content-Transfer-Encoding"] is None
    assert attachment.get_payload()[0]["Subject"] == "Original"


@pytest.mark.asyncio
async def test_fetch_uses_readonly_peek_and_returns_exact_bytes(email_server):
    client = EmailClient(email_server)
    imap = AsyncMock()
    imap._client_task = asyncio.get_running_loop().create_future()
    imap._client_task.set_result(None)
    imap.login.return_value = ("OK", [])
    imap.select.return_value = ("OK", [])
    imap.uid.side_effect = [
        ("OK", [f"1 FETCH (UID 42 BODYSTRUCTURE {PDF})".encode()]),
        ("OK", [b"1 FETCH (UID 42 BODY[1] {8}", bytearray(b"aGVsbG8="), b")"]),
        ("OK", [bytearray(b"Content-Type: application/pdf\r\nContent-Transfer-Encoding: base64\r\n")]),
    ]
    with patch.object(client, "_imap_connect", return_value=imap):
        item = await client.get_attachment("42", "1")
    assert item.data == b"hello" and item.mime_type == "application/pdf"
    assert imap.select.await_count == 1 and imap.examine.await_count == 0
    assert [c.args[2] for c in imap.uid.call_args_list] == ["(BODYSTRUCTURE)", "(BODY.PEEK[1])", "(BODY.PEEK[1.MIME])"]
    imap.logout.assert_awaited_once()


@pytest.mark.asyncio
async def test_fetch_rejects_oversize_before_payload_and_injected_uid(email_server):
    client = EmailClient(email_server)
    with pytest.raises(ValueError, match="Invalid"):
        await client.get_attachment("42) STORE 1:* +FLAGS \\Seen", "1")
    with pytest.raises(ValueError, match="Invalid mailbox"):
        await client.get_attachment("42", "1", "INBOX\r\nA STORE 1:* +FLAGS \\Seen")
    imap = AsyncMock()
    imap._client_task = asyncio.get_running_loop().create_future()
    imap._client_task.set_result(None)
    imap.login.return_value = imap.select.return_value = ("OK", [])
    imap.uid.return_value = (
        "OK",
        [
            f"1 FETCH (UID 42 BODYSTRUCTURE {PDF.replace(' 8 NIL', str(' ' + str(files.MAX_MESSAGE_BYTES + 1) + ' NIL'))})".encode()
        ],
    )
    with patch.object(client, "_imap_connect", return_value=imap), pytest.raises(ValueError, match="exceeds"):
        await client.get_attachment("42", "1")
    assert imap.uid.await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload,headers,reason",
    [
        (b"aGVs", b"Content-Type: application/pdf\r\nContent-Transfer-Encoding: base64\r\n", "size differs"),
        (b"!!!!!!!!", b"Content-Type: application/pdf\r\nContent-Transfer-Encoding: base64\r\n", "Invalid base64"),
        (b"aGVsbG8=", b"Content-Type: image/png\r\nContent-Transfer-Encoding: base64\r\n", "MIME type differs"),
    ],
)
async def test_corrupt_or_mismatched_inbound_files_fail_closed(email_server, payload, headers, reason):
    client = EmailClient(email_server)
    imap = AsyncMock()
    imap._client_task = asyncio.get_running_loop().create_future()
    imap._client_task.set_result(None)
    imap.login.return_value = imap.select.return_value = ("OK", [])
    imap.id.return_value = SimpleNamespace(result="OK")
    imap.uid.side_effect = [
        ("OK", [f"1 FETCH (UID 42 BODYSTRUCTURE {PDF})".encode()]),
        ("OK", [bytearray(payload)]),
        ("OK", [bytearray(headers)]),
    ]
    with patch.object(client, "_imap_connect", return_value=imap), pytest.raises(ValueError, match=reason):
        await client.get_attachment("42", "1")
    imap.logout.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload,encoding,declared_size,accepted",
    [
        (b"aGVsbG8=\r\n", "base64", 9, True),
        (b"aGVs\r\n", "base64", 9, False),
        (b"aGVs\rbG8=\r\n", "base64", 10, False),
        (b"aGVs\nbG8=\r\n", "base64", 10, False),
        (b"hello\r\n", "7bit", 6, False),
    ],
)
async def test_gmail_lf_size_exception_is_only_complete_base64_crlf(
    email_server, payload, encoding, declared_size, accepted
):
    client = EmailClient(email_server)
    imap = AsyncMock()
    imap._client_task = asyncio.get_running_loop().create_future()
    imap._client_task.set_result(None)
    imap.login.return_value = imap.select.return_value = ("OK", [])
    imap.id.return_value = SimpleNamespace(result="OK")
    tree = PDF.replace(" 8 NIL", f" {declared_size} NIL")
    imap.uid.side_effect = [
        ("OK", [f"1 FETCH (UID 42 BODYSTRUCTURE {tree})".encode()]),
        ("OK", [bytearray(payload)]),
        ("OK", [bytearray(f"Content-Type: application/pdf\r\nContent-Transfer-Encoding: {encoding}\r\n".encode())]),
    ]
    with patch.object(client, "_imap_connect", return_value=imap):
        if accepted:
            assert (await client.get_attachment("42", "1")).data == b"hello"
        else:
            with pytest.raises(ValueError, match="size differs"):
                await client.get_attachment("42", "1")


def test_draft_limit_counts_actual_crlf_wire_size(monkeypatch):
    from email.policy import SMTP

    args = ("from@example.invalid", ["to@example.invalid"], "subject", "body")
    msg = compose_draft_message(*args)
    # This fits under the old LF-based check but exceeds the actual SMTP wire size.
    monkeypatch.setattr("mcp_email_server.emails.drafts.MAX_MESSAGE_BYTES", len(msg.as_bytes()))
    assert len(msg.as_bytes(policy=SMTP)) > len(msg.as_bytes())
    with pytest.raises(ValueError, match="Encoded"):
        compose_draft_message(*args)
