"""Bounded in-memory file transfers and MIME attachment metadata.

No mailbox credentials, filesystem paths or payloads are logged/persisted here.
HTTP callers are identified by the trusted proxy, never by an MCP tool argument.
"""

import asyncio
import base64
import binascii
import hashlib
import re
import secrets
import time
from dataclasses import dataclass, field
from email import encoders
from email.header import decode_header, make_header
from email.message import EmailMessage
from email.mime.base import MIMEBase
from email.mime.message import MIMEMessage
from email.parser import BytesParser
from email.policy import SMTP
from typing import Literal

from pydantic import BaseModel, Field

MAX_FILE_BYTES = 16 * 1024 * 1024
MAX_MESSAGE_BYTES = 25_000_000
CHUNK_BYTES = 256 * 1024
TRANSFER_TTL_SECONDS = 600
MAX_TRANSFER_BYTES = 64 * 1024 * 1024
MAX_CALLER_BYTES = 32 * 1024 * 1024
MAX_TRANSFERS = 32
MAX_ATTACHMENTS = 20


class AttachmentInfo(BaseModel):
    attachment_id: str
    name: str
    mime_type: str
    size: int
    size_kind: Literal["decoded", "transfer_encoded"] = "decoded"
    disposition: str = "attachment"


class MailAttachmentSource(BaseModel):
    """An explicit mailbox source for a reply/forward, never inferred automatically."""

    account_name: str
    email_id: str = Field(pattern=r"^[1-9][0-9]*$")
    attachment_id: str = Field(pattern=r"^[1-9][0-9]*(\.[1-9][0-9]*)*$")
    mailbox: str = "INBOX"


@dataclass(frozen=True)
class BinaryAttachment:
    name: str
    mime_type: str
    data: bytes


def validate_filename(name: str) -> str:
    if not name or len(name) > 180 or name in {".", ".."} or any(c in name for c in "/\\\r\n\x00"):
        raise ValueError("Use a filename without a directory, control characters, or more than 180 characters")
    if any(ord(c) < 32 or ord(c) == 127 for c in name):
        raise ValueError("Filename contains control characters")
    return name


def validate_mime_type(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9!#$&^_.+-]+/[A-Za-z0-9!#$&^_.+-]+", value):
        raise ValueError("Invalid MIME type")
    return value.lower()


def mime_part(item: BinaryAttachment) -> MIMEBase:
    validate_filename(item.name)
    major, minor = validate_mime_type(item.mime_type).split("/", 1)
    if len(item.data) > MAX_FILE_BYTES:
        raise ValueError("Attachment exceeds 16 MiB")
    if (major, minor) == ("message", "rfc822"):
        # RFC 2046 forbids base64 transfer encoding on message/rfc822.
        part = MIMEMessage(BytesParser(policy=SMTP).parsebytes(item.data))
    else:
        part = MIMEBase(major, minor)
        part.set_payload(item.data)
        encoders.encode_base64(part)
    part.add_header("Content-Disposition", "attachment", filename=item.name)
    return part


@dataclass
class Transfer:
    caller: str
    name: str
    mime_type: str
    size: int
    expected_sha256: str
    expires: float
    direction: str = "upload"
    data: bytearray = field(default_factory=bytearray, repr=False)
    timer: asyncio.TimerHandle | None = field(default=None, repr=False)


class TransferStore:
    """Single-process, caller-bound, expiring, one-operation transfers."""

    def __init__(self):
        self.entries: dict[str, Transfer] = {}

    def discard(self, handle: str, caller: str | None = None) -> None:
        entry = self.entries.get(handle)
        if entry is None:
            return
        if caller is not None and entry.caller != caller:
            raise ValueError("Transfer unavailable or expired")
        self.entries.pop(handle)
        if entry.timer:
            entry.timer.cancel()
        entry.data.clear()

    def _get(self, handle: str, caller: str, direction: str) -> Transfer:
        entry = self.entries.get(handle)
        if entry is None or entry.caller != caller or entry.direction != direction:
            raise ValueError("Transfer unavailable or expired")
        if entry.expires <= time.monotonic():
            self.discard(handle)
            raise ValueError("Transfer unavailable or expired")
        return entry

    def begin(self, caller: str, name: str, mime_type: str, size: int, sha256: str, direction: str = "upload") -> str:
        validate_filename(name)
        validate_mime_type(mime_type)
        if not 0 <= size <= MAX_FILE_BYTES or not re.fullmatch(r"[0-9a-fA-F]{64}", sha256):
            raise ValueError("Supply a size up to 16 MiB and a SHA-256 digest")
        for handle, entry in list(self.entries.items()):
            if entry.expires <= time.monotonic():
                self.discard(handle)
        reserved = sum(e.size for e in self.entries.values())
        owned = sum(e.size for e in self.entries.values() if e.caller == caller)
        if (
            len(self.entries) >= MAX_TRANSFERS
            or reserved + size > MAX_TRANSFER_BYTES
            or owned + size > MAX_CALLER_BYTES
        ):
            raise ValueError("Temporary transfer capacity reached; finish/discard existing transfers")
        handle = secrets.token_urlsafe(32)
        entry = Transfer(
            caller, name, mime_type, size, sha256.lower(), time.monotonic() + TRANSFER_TTL_SECONDS, direction
        )
        self.entries[handle] = entry
        entry.timer = asyncio.get_running_loop().call_later(TRANSFER_TTL_SECONDS, self.discard, handle)
        return handle

    def append(self, caller: str, handle: str, offset: int, content_base64: str) -> dict:
        entry = self._get(handle, caller, "upload")
        if len(content_base64) > 4 * ((CHUNK_BYTES + 2) // 3):
            raise ValueError("Chunk exceeds 256 KiB")
        try:
            chunk = base64.b64decode(content_base64, validate=True)
        except (ValueError, binascii.Error) as exc:
            raise ValueError("Invalid base64 chunk") from exc
        if len(chunk) > CHUNK_BYTES or offset < 0 or offset + len(chunk) > entry.size:
            raise ValueError("Chunk exceeds declared size or chunk limit")
        if offset < len(entry.data) and bytes(entry.data[offset : offset + len(chunk)]) == chunk:
            pass  # A lost upload acknowledgement can be retried without duplicating bytes.
        elif offset == len(entry.data):
            entry.data.extend(chunk)
        else:
            raise ValueError("Offset does not match the uploaded bytes")
        ready = len(entry.data) == entry.size
        if ready and hashlib.sha256(entry.data).hexdigest() != entry.expected_sha256:
            self.discard(handle)
            raise ValueError("SHA-256 mismatch; transfer discarded")
        return {"upload_id": handle, "received_bytes": len(entry.data), "ready": ready}

    def consume(self, caller: str, handles: list[str]) -> list[BinaryAttachment]:
        if len(handles) != len(set(handles)) or len(handles) > MAX_ATTACHMENTS:
            raise ValueError("Duplicate uploads or too many attachments")
        entries = [self._get(h, caller, "upload") for h in handles]
        if any(len(e.data) != e.size or hashlib.sha256(e.data).hexdigest() != e.expected_sha256 for e in entries):
            raise ValueError("Upload incomplete")
        result = [BinaryAttachment(e.name, e.mime_type, bytes(e.data)) for e in entries]
        for h in handles:
            self.discard(h, caller)
        return result

    def download(self, caller: str, item: BinaryAttachment) -> dict:
        digest = hashlib.sha256(item.data).hexdigest()
        handle = self.begin(caller, item.name, item.mime_type, len(item.data), digest, "download")
        self.entries[handle].data.extend(item.data)
        return self.read(caller, handle, 0)

    def read(self, caller: str, handle: str, offset: int) -> dict:
        entry = self._get(handle, caller, "download")
        if offset < 0 or offset > entry.size or offset % CHUNK_BYTES:
            raise ValueError("Use the next_offset returned by the previous chunk")
        chunk = bytes(entry.data[offset : offset + CHUNK_BYTES])
        end = offset + len(chunk)
        complete = end == entry.size
        result = {
            "download_id": None if complete else handle,
            "name": entry.name,
            "mime_type": entry.mime_type,
            "size": entry.size,
            "sha256": entry.expected_sha256,
            "offset": offset,
            "content_base64": base64.b64encode(chunk).decode("ascii"),
            "next_offset": None if complete else end,
            "complete": complete,
        }
        if complete:
            self.discard(handle, caller)
        return result


def _sexpr(raw: bytes) -> list:  # noqa: C901 - bounded IMAP grammar parser
    """Parse IMAP BODYSTRUCTURE strings/lists/literals, without evaluating input."""
    if len(raw) > 2 * 1024 * 1024:
        raise ValueError("BODYSTRUCTURE too large")
    pos = 0

    def value(depth=0):  # noqa: C901 - explicit grammar alternatives
        nonlocal pos
        if depth > 32:
            raise ValueError("BODYSTRUCTURE too deeply nested")
        while pos < len(raw) and raw[pos : pos + 1].isspace():
            pos += 1
        if pos >= len(raw):
            raise ValueError("Incomplete BODYSTRUCTURE")
        if raw[pos] == 40:
            pos += 1
            result = []
            while True:
                while pos < len(raw) and raw[pos : pos + 1].isspace():
                    pos += 1
                if pos >= len(raw):
                    raise ValueError("Incomplete BODYSTRUCTURE")
                if raw[pos] == 41:
                    pos += 1
                    return result
                result.append(value(depth + 1))
        if raw[pos] == 34:
            pos += 1
            out = bytearray()
            while pos < len(raw):
                c = raw[pos]
                pos += 1
                if c == 34:
                    return out.decode("utf-8", "replace")
                if c == 92 and pos < len(raw):
                    c = raw[pos]
                    pos += 1
                out.append(c)
            raise ValueError("Unterminated quoted string")
        if raw[pos] == 123:
            match = re.match(rb"\{(\d+)\}\r\n", raw[pos:])
            if not match:
                raise ValueError("Invalid literal")
            pos += match.end()
            size = int(match[1])
            if pos + size > len(raw):
                raise ValueError("Incomplete literal")
            out = raw[pos : pos + size].decode("utf-8", "replace")
            pos += size
            return out
        match = re.match(rb"[^\s()]+", raw[pos:])
        if not match:
            raise ValueError("Invalid BODYSTRUCTURE token")
        token = match[0].decode("ascii")
        pos += match.end()
        return None if token.upper() == "NIL" else int(token) if token.isdigit() else token

    result = value()
    if not isinstance(result, list):
        raise TypeError("Invalid BODYSTRUCTURE")
    return result


def _filename(parameters, disposition) -> str | None:
    msg = EmailMessage()

    def params(values):
        if not isinstance(values, list) or len(values) % 2:
            return ""
        return "".join(
            "; " + str(k) + '="' + str(v).replace("\\", "\\\\").replace('"', '\\"') + '"'
            for k, v in zip(values[::2], values[1::2], strict=True)
            if k is not None and v is not None
        )

    msg["Content-Type"] = "application/octet-stream" + params(parameters)
    if isinstance(disposition, list):
        msg["Content-Disposition"] = str(disposition[0] or "attachment") + params(
            disposition[1] if len(disposition) > 1 else None
        )
    name = msg.get_filename()
    return str(make_header(decode_header(name))) if name else None


def structure_attachments(tree: list, prefix: str = "") -> list[AttachmentInfo]:
    if not tree:
        return []
    if isinstance(tree[0], list):
        result = []
        for i, child in enumerate(tree):
            if not isinstance(child, list):
                break
            result.extend(structure_attachments(child, f"{prefix}.{i + 1}" if prefix else str(i + 1)))
        return result
    if len(tree) < 7:
        raise ValueError("Incomplete single-part BODYSTRUCTURE")
    major, minor = str(tree[0]).lower(), str(tree[1]).lower()
    index = 9 if major == "text" else 11 if (major, minor) == ("message", "rfc822") else 8
    disposition = tree[index] if len(tree) > index else None
    name = _filename(tree[2], disposition)
    kind = str(disposition[0]).lower() if isinstance(disposition, list) and disposition else "attachment"
    part_id = prefix or "1"
    if name or (isinstance(disposition, list) and kind == "attachment"):
        if not isinstance(tree[6], int) or tree[6] < 0:
            raise ValueError("Invalid attachment size")
        return [
            AttachmentInfo(
                attachment_id=part_id,
                name=name or f"attachment-{part_id}",
                mime_type=f"{major}/{minor}",
                size=tree[6],
                size_kind="transfer_encoded",
                disposition=kind,
            )
        ]
    return []


def structures_by_uid(data: list) -> dict[str, list[AttachmentInfo]]:
    result = {}
    groups = []
    current = []
    for item in data:
        if not isinstance(item, (bytes, bytearray)):
            continue
        if re.match(rb"(?:\* )?\d+ FETCH \(", item, re.I) and current:
            groups.append(current)
            current = []
        current.append(bytes(item))
    if current:
        groups.append(current)
    for parts in groups:
        raw = b"".join(parts)
        start = re.search(rb"(?:\* )?\d+ FETCH (\()", raw, re.I)
        if not start:
            continue
        attrs = _sexpr(raw[start.start(1) :])
        if len(attrs) % 2:
            raise ValueError("Invalid FETCH attributes")
        fields = {str(k).upper(): v for k, v in zip(attrs[::2], attrs[1::2], strict=True)}
        uid, body = fields.get("UID"), fields.get("BODYSTRUCTURE")
        if isinstance(uid, int) and uid > 0 and isinstance(body, list):
            result[str(uid)] = structure_attachments(body)
    return result


def raw_attachment_parts(message, prefix=""):
    """Yield MIME parts with stable IMAP section identifiers; don't descend into attached emails."""
    if message.get_filename() or message.get_content_disposition() == "attachment":
        yield prefix or "1", message
    elif message.is_multipart():
        for i, part in enumerate(message.get_payload(), 1):
            yield from raw_attachment_parts(part, f"{prefix}.{i}" if prefix else str(i))


def raw_attachment_info(message) -> list[AttachmentInfo]:
    result = []
    for part_id, part in raw_attachment_parts(message):
        payload = part.get_payload(decode=True)
        if payload is None:
            nested = part.get_payload()
            payload = nested[0].as_bytes() if isinstance(nested, list) and nested else part.as_bytes()
        result.append(
            AttachmentInfo(
                attachment_id=part_id,
                name=part.get_filename() or f"attachment-{part_id}",
                mime_type=part.get_content_type(),
                size=len(payload),
                disposition=part.get_content_disposition() or "attachment",
            )
        )
    return result
