import json
import re
from datetime import datetime
from typing import Annotated, Any, Literal
from urllib.parse import quote

from loguru import logger
from mcp.server.fastmcp import Context, FastMCP
from mcp.types import BlobResourceContents, CallToolResult, EmbeddedResource, TextContent
from pydantic import Field

from mcp_email_server.config import (
    AccountAttributes,
    EmailSettings,
    ProviderSettings,
    get_settings,
)
from mcp_email_server.emails.attachments import (
    CHUNK_BYTES,
    MAX_ATTACHMENTS,
    MAX_FILE_BYTES,
    MAX_MESSAGE_BYTES,
    TRANSFER_TTL_SECONDS,
    MailAttachmentSource,
    TransferStore,
)
from mcp_email_server.emails.dispatcher import dispatch_handler
from mcp_email_server.emails.models import (
    AttachmentDownloadResponse,
    EmailContentBatchResponse,
    EmailMetadataPageResponse,
)

mcp = FastMCP("email")

attachment_transfers = TransferStore()


def _attachment_caller(ctx: Context | None) -> str:
    if ctx is None:
        raise ValueError("Attachment transfers require an MCP request context")
    request = ctx.request_context.request
    if request is None:
        return f"stdio:{id(ctx.request_context.session)}"
    # The private container is reached only through the existing OAuth proxy.
    # It overwrites this header AFTER bearer validation, and never forwards the token.
    caller = request.headers.get("x-email-mcp-caller", "")
    if not re.fullmatch(r"[0-9a-f]{64}", caller):
        raise ValueError("Trusted attachment caller missing; use the authenticated MCP endpoint")
    return caller


async def _attachment_inputs(ctx, upload_ids, sources):
    if len(upload_ids or []) + len(sources or []) > MAX_ATTACHMENTS:
        raise ValueError("At most 20 attachments per message")
    # Remove temporary uploads BEFORE trying IMAP/SMTP. Never reuse them after
    # an uncertain send/APPEND outcome. There is no automatic provider retry.
    items = attachment_transfers.consume(_attachment_caller(ctx), upload_ids) if upload_ids else []
    if sum(len(i.data) for i in items) > MAX_FILE_BYTES:
        raise ValueError("Combined raw attachments exceed 16 MiB")
    for source in sources or []:
        handler = dispatch_handler(source.account_name)
        if not hasattr(handler, "incoming_client"):
            raise ValueError("Remote attachments require an IMAP account")
        items.append(
            await handler.incoming_client.get_attachment(source.email_id, source.attachment_id, source.mailbox)
        )
        if sum(len(i.data) for i in items) > MAX_FILE_BYTES:
            raise ValueError("Combined raw attachments exceed 16 MiB")
    return items


@mcp.tool(description="Get attachment transfer limits. Files stay only in bounded process memory, never on disk.")
def attachment_limits() -> dict[str, Any]:
    return {
        "max_file_bytes": MAX_FILE_BYTES,
        "max_total_raw_attachment_bytes": MAX_FILE_BYTES,
        "max_encoded_message_bytes": MAX_MESSAGE_BYTES,
        "chunk_bytes": CHUNK_BYTES,
        "max_attachments": MAX_ATTACHMENTS,
        "expires_after_seconds": TRANSFER_TTL_SECONDS,
    }


@mcp.tool(
    description="Start uploading a file through MCP. Supply the exact byte size and SHA-256. Append base64 chunks, then use upload_id in create_draft/send_email. No server path is needed. Transfers expire in ten minutes and are private to this connection credential."
)
def begin_attachment_upload(
    name: str, mime_type: str, size: Annotated[int, Field(ge=0, le=MAX_FILE_BYTES)], sha256: str, ctx: Context = None
) -> dict[str, Any]:
    handle = attachment_transfers.begin(_attachment_caller(ctx), name, mime_type, size, sha256)
    return {"upload_id": handle, "chunk_bytes": CHUNK_BYTES, "expires_after_seconds": TRANSFER_TTL_SECONDS}


@mcp.tool(
    description="Append at most 256 KiB of strict base64 at the next byte offset. Duplicate identical chunks may be retried safely. Ready is true only after size and SHA-256 verification."
)
def upload_attachment_chunk(
    upload_id: str, offset: Annotated[int, Field(ge=0)], content_base64: str, ctx: Context = None
) -> dict[str, Any]:
    return attachment_transfers.append(_attachment_caller(ctx), upload_id, offset, content_base64)


@mcp.tool(description="Immediately discard your unused upload or unfinished download from temporary memory.")
def discard_attachment_transfer(transfer_id: str, ctx: Context = None) -> dict[str, Any]:
    attachment_transfers.discard(transfer_id, _attachment_caller(ctx))
    return {"discarded": True}


@mcp.tool(
    description="Get one attachment through MCP by its attachment_id (MIME section) from attachment_info. Preserves unread flags. Returns base64 bytes, exact decoded size and SHA-256. For larger files follow download_id/next_offset with read_attachment_chunk. No filesystem download or public link."
)
async def get_attachment(
    account_name: str, email_id: str, attachment_id: str, mailbox: str = "INBOX", ctx: Context = None
) -> CallToolResult:
    phase = "prepare"
    try:
        caller = _attachment_caller(ctx)
        handler = dispatch_handler(account_name)
        if not hasattr(handler, "incoming_client"):
            raise ValueError("Remote attachments require an IMAP account")  # noqa: TRY301 - validation stays beside its diagnostic phase
        phase = "mailbox_read"
        item = await handler.incoming_client.get_attachment(email_id, attachment_id, mailbox)
        phase = "response"
        result = attachment_transfers.download(caller, item)
        summary = {k: v for k, v in result.items() if k != "content_base64"} if result["complete"] else result
        content = [TextContent(type="text", text=json.dumps(summary))]
        if result["complete"]:
            uri = f"attachment://mail/{quote(account_name, safe='')}/{email_id}/{attachment_id}/{quote(item.name, safe='')}"
            content.append(
                EmbeddedResource(
                    type="resource",
                    resource=BlobResourceContents(uri=uri, mimeType=item.mime_type, blob=result["content_base64"]),
                )
            )
        return CallToolResult(content=content, structuredContent=result)
    except Exception as exc:
        if isinstance(exc, ValueError) and str(exc):
            raise
        failure = f"Attachment read failed at {phase} ({type(exc).__name__})"
        logger.warning(failure)
        raise ValueError(failure) from exc


@mcp.tool(
    description="Read the next base64 chunk of your attachment download. The last chunk deletes the temporary copy; abandoned downloads expire automatically. After a lost final response, get the original attachment again (safe read)."
)
def read_attachment_chunk(download_id: str, offset: Annotated[int, Field(ge=0)], ctx: Context = None) -> dict[str, Any]:
    return attachment_transfers.read(_attachment_caller(ctx), download_id, offset)


@mcp.resource("email://{account_name}")
async def get_account(account_name: str) -> EmailSettings | ProviderSettings | None:
    settings = get_settings()
    return settings.get_account(account_name, masked=True)


@mcp.tool(description="List all configured email accounts with masked credentials.")
async def list_available_accounts() -> list[AccountAttributes]:
    settings = get_settings()
    return [account.masked() for account in settings.get_accounts()]


@mcp.tool(description="Add a new email account configuration to the settings.")
async def add_email_account(email: EmailSettings) -> str:
    settings = get_settings()
    settings.add_email(email)
    settings.store()
    return f"Successfully added email account '{email.account_name}'"


@mcp.tool(
    description="List email metadata (email_id, subject, sender, recipients, date) without body content. Returns email_id for use with get_emails_content."
)
async def list_emails_metadata(
    account_name: Annotated[str, Field(description="The name of the email account.")],
    page: Annotated[
        int,
        Field(default=1, description="The page number to retrieve (starting from 1)."),
    ] = 1,
    page_size: Annotated[int, Field(default=10, description="The number of emails to retrieve per page.")] = 10,
    before: Annotated[
        datetime | None,
        Field(default=None, description="Retrieve emails before this datetime (UTC)."),
    ] = None,
    since: Annotated[
        datetime | None,
        Field(default=None, description="Retrieve emails since this datetime (UTC)."),
    ] = None,
    subject: Annotated[str | None, Field(default=None, description="Filter emails by subject.")] = None,
    from_address: Annotated[str | None, Field(default=None, description="Filter emails by sender address.")] = None,
    to_address: Annotated[
        str | None,
        Field(default=None, description="Filter emails by recipient address."),
    ] = None,
    order: Annotated[
        Literal["asc", "desc"],
        Field(default=None, description="Order emails by field. `asc` or `desc`."),
    ] = "desc",
    mailbox: Annotated[str, Field(default="INBOX", description="The mailbox to search.")] = "INBOX",
    seen: Annotated[
        bool | None,
        Field(default=None, description="Filter by read status: True=read, False=unread, None=all."),
    ] = None,
    flagged: Annotated[
        bool | None,
        Field(default=None, description="Filter by flagged/starred status: True=flagged, False=unflagged, None=all."),
    ] = None,
    answered: Annotated[
        bool | None,
        Field(default=None, description="Filter by replied status: True=replied, False=not replied, None=all."),
    ] = None,
) -> EmailMetadataPageResponse:
    handler = dispatch_handler(account_name)

    return await handler.get_emails_metadata(
        page=page,
        page_size=page_size,
        before=before,
        since=since,
        subject=subject,
        from_address=from_address,
        to_address=to_address,
        order=order,
        mailbox=mailbox,
        seen=seen,
        flagged=flagged,
        answered=answered,
    )


@mcp.tool(
    description="Get the full content (including body) of one or more emails by their email_id. Use list_emails_metadata first to get the email_id."
)
async def get_emails_content(
    account_name: Annotated[str, Field(description="The name of the email account.")],
    email_ids: Annotated[
        list[str],
        Field(
            description="List of email_id to retrieve (obtained from list_emails_metadata). Can be a single email_id or multiple email_ids."
        ),
    ],
    mailbox: Annotated[str, Field(default="INBOX", description="The mailbox to retrieve emails from.")] = "INBOX",
) -> EmailContentBatchResponse:
    handler = dispatch_handler(account_name)
    return await handler.get_emails_content(email_ids, mailbox)


@mcp.tool(
    description="Send only after explicit owner approval. Attach remote files using attachment_upload_ids, or explicitly copy original mailbox parts using attachment_sources. Temporary uploads are consumed even if the provider operation fails; inspect Sent before retrying an uncertain send. Existing local attachments paths remain supported.",
)
async def send_email(
    account_name: Annotated[str, Field(description="The name of the email account to send from.")],
    recipients: Annotated[list[str], Field(description="A list of recipient email addresses.")],
    subject: Annotated[str, Field(description="The subject of the email.")],
    body: Annotated[str, Field(description="The body of the email.")],
    cc: Annotated[
        list[str] | None,
        Field(default=None, description="A list of CC email addresses."),
    ] = None,
    bcc: Annotated[
        list[str] | None,
        Field(default=None, description="A list of BCC email addresses."),
    ] = None,
    html: Annotated[
        bool,
        Field(default=False, description="Whether to send the email as HTML (True) or plain text (False)."),
    ] = False,
    attachments: Annotated[
        list[str] | None,
        Field(
            default=None,
            description="A list of absolute file paths to attach to the email. Supports common file types (documents, images, archives, etc.).",
        ),
    ] = None,
    in_reply_to: Annotated[
        str | None,
        Field(
            default=None,
            description="Message-ID of the email being replied to. Enables proper threading in email clients.",
        ),
    ] = None,
    references: Annotated[
        str | None,
        Field(
            default=None,
            description="Space-separated Message-IDs for the thread chain. Usually includes in_reply_to plus ancestors.",
        ),
    ] = None,
    attachment_upload_ids: list[str] | None = None,
    attachment_sources: list[MailAttachmentSource] | None = None,
    ctx: Context = None,
) -> str:
    binary = (
        await _attachment_inputs(ctx, attachment_upload_ids, attachment_sources)
        if attachment_upload_ids or attachment_sources
        else []
    )
    handler = dispatch_handler(account_name)
    delivery = await handler.send_email(
        recipients,
        subject,
        body,
        cc,
        bcc,
        html,
        attachments,
        in_reply_to,
        references,
        **({"binary_attachments": binary} if binary else {}),
    )
    recipient_str = ", ".join(recipients)
    attachment_info = f" with {(len(attachments or []) + len(binary))} attachment(s)" if attachments or binary else ""
    result = f"Email sent successfully to {recipient_str}{attachment_info}"
    if isinstance(delivery, dict) and delivery.get("sent_copy") == "unconfirmed":
        result += f". SMTP delivered; Sent copy unconfirmed (Message-ID: {delivery.get('message_id')}). Do not resend; reconcile Sent."
    return result


@mcp.tool(
    description="Create a draft email and save it to the Drafts folder without sending. The draft will appear in the email client's Drafts folder, ready for the user to review and send manually. Use attachment_upload_ids or attachment_sources to save attachments inside the IMAP draft. No SMTP required \u2014 this is a pure IMAP operation.",
)
async def create_draft(
    account_name: Annotated[str, Field(description="The name of the email account to create the draft in.")],
    recipients: Annotated[list[str], Field(description="A list of recipient email addresses.")],
    subject: Annotated[str, Field(description="The subject of the email.")],
    body: Annotated[str, Field(description="The body of the email.")],
    cc: Annotated[
        list[str] | None,
        Field(default=None, description="A list of CC email addresses."),
    ] = None,
    bcc: Annotated[
        list[str] | None,
        Field(default=None, description="A list of BCC email addresses."),
    ] = None,
    html: Annotated[
        bool,
        Field(default=False, description="Whether the body is HTML (True) or plain text (False)."),
    ] = False,
    in_reply_to: Annotated[
        str | None,
        Field(
            default=None,
            description="Message-ID of the email being replied to. Enables proper threading in email clients.",
        ),
    ] = None,
    references: Annotated[
        str | None,
        Field(
            default=None,
            description="Space-separated Message-IDs for the thread chain. Usually includes in_reply_to plus ancestors.",
        ),
    ] = None,
    attachment_upload_ids: list[str] | None = None,
    attachment_sources: list[MailAttachmentSource] | None = None,
    ctx: Context = None,
) -> str:
    binary = (
        await _attachment_inputs(ctx, attachment_upload_ids, attachment_sources)
        if attachment_upload_ids or attachment_sources
        else []
    )
    handler = dispatch_handler(account_name)
    result = await handler.create_draft(
        recipients,
        subject,
        body,
        cc,
        bcc,
        html,
        in_reply_to,
        references,
        **({"binary_attachments": binary} if binary else {}),
    )
    return result


@mcp.tool(
    description="Move messages to the mailbox Trash folder. Never permanently deletes or empties Trash. Unsupported atomic moves fail without deletion."
)
async def move_to_trash(
    account_name: Annotated[str, Field(description="The name of the email account.")],
    email_ids: Annotated[
        list[str],
        Field(description="Individual IMAP UIDs to move to Trash."),
    ],
    mailbox: Annotated[str, Field(default="INBOX", description="Source mailbox to move messages from.")] = "INBOX",
) -> str:
    handler = dispatch_handler(account_name)
    if not hasattr(handler, "move_to_trash"):
        raise ValueError("Safe Trash moves are unavailable for this provider")
    deleted_ids, failed_ids = await handler.move_to_trash(email_ids, mailbox)

    result = f"Successfully moved {len(deleted_ids)} email(s) to Trash"
    if failed_ids:
        result += (
            f", not confirmed moved; reconcile before retrying {len(failed_ids)} email(s): {', '.join(failed_ids)}"
        )
    return result


# Python compatibility only; the MCP catalog exposes move_to_trash, never delete.
delete_emails = move_to_trash


@mcp.tool(
    description="Mark one or more emails as read (seen) by their email_id. Use list_emails_metadata first to get the email_id."
)
async def mark_as_read(
    account_name: Annotated[str, Field(description="The name of the email account.")],
    email_ids: Annotated[
        list[str],
        Field(description="List of email_id to mark as read (obtained from list_emails_metadata)."),
    ],
    mailbox: Annotated[str, Field(default="INBOX", description="The mailbox containing the emails.")] = "INBOX",
) -> str:
    handler = dispatch_handler(account_name)
    marked_ids, failed_ids = await handler.mark_as_read(email_ids, mailbox)

    result = f"Successfully marked {len(marked_ids)} email(s) as read"
    if failed_ids:
        result += f", failed to mark {len(failed_ids)} email(s): {', '.join(failed_ids)}"
    return result


@mcp.tool(
    description="Download an email attachment and save it to the specified path. This feature must be explicitly enabled in settings (enable_attachment_download=true) due to security considerations.",
)
async def download_attachment(
    account_name: Annotated[str, Field(description="The name of the email account.")],
    email_id: Annotated[
        str, Field(description="The email ID (obtained from list_emails_metadata or get_emails_content).")
    ],
    attachment_name: Annotated[
        str, Field(description="The name of the attachment to download (as shown in the attachments list).")
    ],
    save_path: Annotated[str, Field(description="The absolute path where the attachment should be saved.")],
    mailbox: Annotated[str, Field(description="The mailbox to search in (default: INBOX).")] = "INBOX",
) -> AttachmentDownloadResponse:
    settings = get_settings()
    if not settings.enable_attachment_download:
        msg = (
            "Attachment download is disabled. Set 'enable_attachment_download=true' in settings to enable this feature."
        )
        raise PermissionError(msg)

    handler = dispatch_handler(account_name)
    return await handler.download_attachment(email_id, attachment_name, save_path, mailbox)
