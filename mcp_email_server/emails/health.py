"""On-demand, read-only mailbox health. No message fetches or flag changes."""

import asyncio
import time
from datetime import datetime, timezone

from mcp_email_server.config import EmailSettings
from mcp_email_server.emails.classic import EmailClient
from mcp_email_server.emails.reliability import MailboxConnectionError, bounded_step, close_imap, login_imap


async def check_account(account):
    result = {"account_name": account.account_name, "checked_at": datetime.now(timezone.utc).isoformat()}
    if not isinstance(account, EmailSettings):
        return {**result, "status": "unsupported", "phase": "configuration"}
    result["email_address"] = account.email_address
    started = time.monotonic()
    imap = None
    try:
        imap = EmailClient(account.incoming)._imap_connect()
        await login_imap(imap, account.incoming)
        # EXAMINE explicitly selects read-only; no body or headers are retrieved.
        response = await bounded_step(imap.examine('"INBOX"'), "examine")
        if response[0] != "OK":
            raise MailboxConnectionError("examine", "mailbox_rejected")  # noqa: TRY301
        result.update(
            status="ok",
            phase="examine",
            message="IMAP login and read-only INBOX access succeeded. SMTP was not tested.",
        )
    except MailboxConnectionError as error:
        result.update(status="error", phase=error.phase, code=error.code, message=str(error))
    except Exception:
        result.update(
            status="error",
            phase="connection",
            code="unexpected_error",
            message="Mailbox check failed; inspect sanitized server diagnostics.",
        )
    finally:
        if imap is not None:
            await close_imap(imap)
    return {**result, "duration_ms": round((time.monotonic() - started) * 1000)}


async def check_accounts(accounts):
    semaphore = asyncio.Semaphore(3)

    async def check(account):
        async with semaphore:
            return await check_account(account)

    results = await asyncio.gather(*(check(account) for account in accounts))
    return {
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "total": len(results),
        "healthy": sum(r["status"] == "ok" for r in results),
        "accounts": results,
    }
