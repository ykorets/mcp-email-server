"""Draft email creation via IMAP APPEND to Drafts folder.

This module provides functionality to create email drafts by composing
a MIME message and appending it to the IMAP Drafts folder. No SMTP
connection is required — this is a pure IMAP operation.

The implementation follows the same pattern as append_to_sent in classic.py.
"""

import email.utils
import ssl

import aioimaplib

from email.header import Header
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from mcp_email_server.config import EmailServer
from mcp_email_server.log import logger


def _quote_mailbox(mailbox: str) -> str:
    """Quote mailbox name for IMAP compatibility."""
    escaped = mailbox.replace("\\\\", "\\\\\\\\").replace('"', r'\"')
    return f'"{ escaped}"'


def _create_ssl_context(verify_ssl: bool) -> ssl.SSLContext | None:
    """Create SSL context for IMAP connections."""
    if verify_ssl:
        return None
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def compose_draft_message(
    sender: str,
    recipients: list[str],
    subject: str,
    body: str,
    cc: list[str] | None = None,
    bcc: list[str] | None = None,
    html: bool = False,
    in_reply_to: str | None = None,
    references: str | None = None,
) -> MIMEText | MIMEMultipart:
    """Compose a MIME message suitable for saving as a draft.

    Args:
        sender: The sender email address (or "Name <email>").
        recipients: List of recipient email addresses.
        subject: Email subject.
        body: Email body content.
        cc: List of CC email addresses.
        bcc: List of BCC email addresses.
        html: Whether the body is HTML.
        in_reply_to: Message-ID for reply threading.
        references: Space-separated Message-IDs for thread chain.

    Returns:
        A MIMEText or MIMEMultipart message object.
    """
    content_type = "html" if html else "plain"
    msg = MIMEText(body, content_type, "utf-8")

    # Handle subject with special characters
    if any(ord(c) > 127 for c in subject):
        msg["Subject"] = Header(subject, "utf-8")
    else:
        msg["Subject"] = subject

    # Handle sender name with special characters
    if any(ord(c) > 127 for c in sender):
        msg["From"] = Header(sender, "utf-8")
    else:
        msg["From"] = sender

    msg["To"] = ", ".join(recipients)

    if cc:
        msg["Cc"] = ", ".join(cc)

    # Set threading headers for replies
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
    if references:
        msg["References"] = references

    # Set Date and Message-Id
    msg["Date"] = email.utils.formatdate(localtime=True)
    sender_domain = sender.rsplit("@", 1)[-1].rstrip(">")
    msg["Message-Id"] = email.utils.make_msgid(domain=sender_domain)

    return msg


async def _find_drafts_folder_by_flag(imap: aioimaplib.IMAP4 | aioimaplib.IMAP4_SSL) -> str | None:
    """Find the Drafts folder by searching for the \\Drafts IMAP flag.

    Args:
        imap: Connected IMAP client.

    Returns:
        The folder name with the \\Drafts flag, or None if not found.
    """
    try:
        _, folders = await imap.list('""', "*")

        for folder in folders:
            folder_str = folder.decode("utf-8") if isinstance(folder, bytes) else str(folder)
            if r"\Drafts" in folder_str or "\\Drafts" in folder_str:
                parts = folder_str.split('"')
                if len(parts) >= 3:
                    folder_name = parts[-2]
                    logger.info(f"Found Drafts folder by \\Drafts flag: '{folder_name}'")
                    return folder_name
    except Exception as e:
        logger.debug(f"Error finding Drafts folder by flag: {e}")

    return None


async def append_to_drafts(
    msg: MIMEText | MIMEMultipart,
    incoming_server: EmailServer,
    drafts_folder_name: str | None = None,
) -> str:
    """Append a composed message to the IMAP Drafts folder.

    Args:
        msg: The email message to save as draft.
        incoming_server: IMAP server configuration.
        drafts_folder_name: Override folder name, or None for auto-detection.

    Returns:
        The name of the folder where the draft was saved.

    Raises:
        RuntimeError: If no valid Drafts folder could be found.
    """
    if incoming_server.use_ssl:
        imap_ssl_context = _create_ssl_context(incoming_server.verify_ssl)
        imap = aioimaplib.IMAP4_SSL(incoming_server.host, incoming_server.port, ssl_context=imap_ssl_context)
    else:
        imap = aioimaplib.IMAP4(incoming_server.host, incoming_server.port)

    # Common Drafts folder names across different providers
    drafts_folder_candidates = [
        drafts_folder_name,  # User-specified override (if provided)
        "Drafts",
        "INBOX.Drafts",
        "Draft",
        "INBOX.Draft",
        "[Gmail]/Drafts",
        "Sent Messages",  # Some providers use this
    ]
    # Filter out None values
    drafts_folder_candidates = [f for f in drafts_folder_candidates if f]

    try:
        await imap._client_task
        await imap.wait_hello_from_server()
        await imap.login(incoming_server.user_name, incoming_server.password.get_secret_value())

        # Try to find Drafts folder by IMAP \Drafts flag first
        flag_folder = await _find_drafts_folder_by_flag(imap)
        if flag_folder and flag_folder not in drafts_folder_candidates:
            drafts_folder_candidates.insert(0, flag_folder)

        # Try to find and use the Drafts folder
        for folder in drafts_folder_candidates:
            try:
                logger.debug(f"Trying Drafts folder: '{folder}'")
                result = await imap.select(_quote_mailbox(folder))

                status = result[0] if isinstance(result, tuple) else result
                if str(status).upper() == "OK":
                    msg_bytes = msg.as_bytes()
                    logger.debug(f"Appending draft to '{folder}'")
                    append_result = await imap.append(
                        msg_bytes,
                        mailbox=_quote_mailbox(folder),
                        flags=r"(\Draft)",
                    )
                    append_status = append_result[0] if isinstance(append_result, tuple) else append_result
                    if str(append_status).upper() == "OK":
                        logger.info(f"Saved draft to '{folder}'")
                        return folder
                    else:
                        logger.warning(f"Failed to append draft to '{folder}': {append_status}")
                else:
                    logger.debug(f"Folder '{folder}' select returned: {status}")
            except Exception as e:
                logger.debug(f"Folder '{folder}' not available: {e}")
                continue

        msg = "Could not find a valid Drafts folder to save the message"
        logger.error(msg)
        raise RuntimeError(msg)

    finally:
        try:
            await imap.logout()
        except Exception as e:
            logger.debug(f"Error during logout: {e}")
