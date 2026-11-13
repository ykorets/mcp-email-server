"""Exercise actual SMTP-policy serialization of international draft headers."""

from email import policy
from email.parser import BytesParser
from email.utils import parseaddr

import pytest

from mcp_email_server.emails.attachments import BinaryAttachment
from mcp_email_server.emails.drafts import compose_draft_message


@pytest.mark.parametrize("subject", ["ASCII audit", "Audit — attachments", "Проверка вложения"])
@pytest.mark.parametrize("with_files", [False, True])
def test_unicode_draft_headers_survive_smtp_wire(subject, with_files):
    sender_name, sender_address = "Ярослав Korets", "owner@example.invalid"
    payload = bytes(range(256)) * 1024
    files = [BinaryAttachment("проверка.bin", "application/octet-stream", payload)] if with_files else []
    message = compose_draft_message(
        f"{sender_name} <{sender_address}>",
        ["recipient@example.invalid"],
        subject,
        "Synthetic draft. Never send.",
        in_reply_to="<prior@example.invalid>",
        references="<prior@example.invalid>",
        binary_attachments=files,
    )
    raw = message.as_bytes(policy=policy.SMTP)
    decoded = BytesParser(policy=policy.default).parsebytes(raw)
    assert str(decoded["Subject"]) == subject
    assert parseaddr(str(decoded["From"])) == (sender_name, sender_address)
    assert str(decoded["To"]) == "recipient@example.invalid"
    assert str(decoded["In-Reply-To"]) == "<prior@example.invalid>"
    assert str(decoded["References"]) == "<prior@example.invalid>"
    assert decoded.get_body(preferencelist=("plain",)).get_content().strip() == "Synthetic draft. Never send."
    attachments = list(decoded.iter_attachments())
    assert len(attachments) == int(with_files)
    if with_files:
        assert attachments[0].get_filename() == "проверка.bin"
        assert attachments[0].get_payload(decode=True) == payload
    assert b"\r\n" in raw
