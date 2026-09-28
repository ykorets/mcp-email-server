import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest

from mcp_email_server.emails.classic import EmailClient
from mcp_email_server.emails.health import check_account, check_accounts
from mcp_email_server.emails.reliability import MailboxConnectionError, bounded_step, close_imap, login_imap


def imap_fixture():
    connected = asyncio.get_running_loop().create_future()
    connected.set_result(None)
    return SimpleNamespace(
        _client_task=connected,
        wait_hello_from_server=AsyncMock(),
        login=AsyncMock(return_value=("OK", [])),
        logout=AsyncMock(),
        examine=AsyncMock(return_value=("OK", [])),
        protocol=SimpleNamespace(transport=Mock()),
    )


@pytest.mark.asyncio
async def test_login_rejection_is_actionable_and_redacted(email_server):
    imap = imap_fixture()
    imap.login.return_value = ("NO", [b"private provider response"])
    with pytest.raises(MailboxConnectionError) as caught:
        await login_imap(imap, email_server)
    assert caught.value.code == "authentication_rejected"
    assert "private" not in str(caught.value)
    assert email_server.password.get_secret_value() not in str(caught.value)


@pytest.mark.asyncio
async def test_timeout_is_bounded_and_names_phase():
    with patch("mcp_email_server.emails.reliability.STEP_TIMEOUT", 0.01):
        with pytest.raises(MailboxConnectionError, match="login: timeout"):
            await bounded_step(asyncio.sleep(5), "login")


@pytest.mark.asyncio
async def test_cleanup_timeout_closes_transport():
    imap = imap_fixture()
    imap.logout.side_effect = lambda: None

    async def slow_logout():
        await asyncio.sleep(5)

    imap.logout.side_effect = slow_logout
    with patch("mcp_email_server.emails.reliability.CLEANUP_TIMEOUT", 0.01):
        await close_imap(imap)
    imap.protocol.transport.close.assert_called_once()


@pytest.mark.asyncio
async def test_read_formats_never_mark_seen(email_server):
    imap = SimpleNamespace(uid=AsyncMock(return_value=("NO", [])))
    assert await EmailClient(email_server)._fetch_email_with_formats(imap, "42") is None
    assert [call.args[2] for call in imap.uid.call_args_list] == ["BODY.PEEK[]", "(BODY.PEEK[])"]


@pytest.mark.asyncio
async def test_health_uses_readonly_examine(email_settings):
    imap = imap_fixture()
    with patch.object(EmailClient, "_imap_connect", return_value=imap):
        result = await check_account(email_settings)
    assert result["status"] == "ok"
    imap.examine.assert_awaited_once_with('"INBOX"')
    imap.protocol.transport.close.assert_called_once()


@pytest.mark.asyncio
async def test_health_failure_does_not_hide_good_account(email_settings):
    bad, good = imap_fixture(), imap_fixture()
    bad.login.side_effect = TimeoutError()
    with patch.object(EmailClient, "_imap_connect", side_effect=[bad, good]):
        result = await check_accounts([email_settings, email_settings])
    assert result["total"] == 2
    assert result["healthy"] == 1
    assert result["accounts"][0]["code"] == "timeout"
    assert result["accounts"][0]["phase"] == "login"


@pytest.mark.asyncio
async def test_health_tool_registered_readonly():
    from mcp_email_server.app import mcp

    names = {tool.name for tool in await mcp.list_tools()}
    assert "check_accounts_health" in names
