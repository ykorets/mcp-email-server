"""Bounded IMAP setup and sanitized, actionable failures (no automatic writes)."""

import asyncio
import ssl
from contextlib import suppress

STEP_TIMEOUT = 15
CLEANUP_TIMEOUT = 2


class MailboxConnectionError(RuntimeError):
    def __init__(self, phase: str, code: str):
        self.phase, self.code = phase, code
        guidance = {
            "timeout": "The mail provider did not respond. Check mailbox status and whether IMAP is enabled; reconnecting MCP will not fix this.",
            "authentication_rejected": "The provider rejected login. Check mailbox credentials and IMAP access in the provider's settings.",
            "tls_error": "TLS verification failed. Check the server configuration; do not disable certificate verification.",
            "connection_error": "Cannot connect to the mail provider. Check server availability and network access.",
            "mailbox_rejected": "The provider could not open this folder. Check the folder name and permissions.",
        }
        super().__init__(f"IMAP {phase}: {code}. {guidance.get(code, 'Check the mail provider configuration.')}")


async def bounded_step(awaitable, phase: str):
    try:
        return await asyncio.wait_for(awaitable, timeout=STEP_TIMEOUT)
    except asyncio.TimeoutError:
        raise MailboxConnectionError(phase, "timeout") from None
    except ssl.SSLError:
        raise MailboxConnectionError(phase, "tls_error") from None
    except (OSError, ConnectionError):
        raise MailboxConnectionError(phase, "connection_error") from None


async def login_imap(imap, server):
    await bounded_step(imap._client_task, "connect")
    await bounded_step(imap.wait_hello_from_server(), "greeting")
    response = await bounded_step(imap.login(server.user_name, server.password.get_secret_value()), "login")
    if response[0] != "OK":
        raise MailboxConnectionError("login", "authentication_rejected")


async def close_imap(imap):
    """Cleanup never masks the original failure or waits indefinitely."""
    try:
        with suppress(Exception):
            await asyncio.wait_for(imap.logout(), timeout=CLEANUP_TIMEOUT)
    finally:
        task = getattr(imap, "_client_task", None)
        if task and not task.done():
            task.cancel()
        transport = getattr(getattr(imap, "protocol", None), "transport", None)
        if transport is not None:
            with suppress(Exception):
                transport.close()
