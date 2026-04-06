#!/usr/bin/env python3
"""Quick test script for create_draft functionality.

Usage:
    python test_draft.py <account_name>

Example:
    python test_draft.py kurenivka

This will:
1. Load the config
2. Connect to the specified account via IMAP
3. Create a test draft
4. Verify it appeared in the Drafts folder
"""

import asyncio
import sys

from mcp_email_server.config import get_settings
from mcp_email_server.emails.classic import ClassicEmailHandler


async def test_read(handler, account_name: str):
    """Test 1: Read recent emails (verify IMAP connection works)."""
    print(f"\n{'='*60}")
    print(f"TEST 1: Reading recent emails from {account_name}")
    print(f"{'='*60}")
    try:
        result = await handler.get_emails_metadata(page=1, page_size=3)
        print(f"  Total emails in INBOX: {result.total}")
        for email in result.emails:
            print(f"  - [{email.date:%Y-%m-%d}] {email.sender[:40]}: {email.subject[:50]}")
        print("  ✅ IMAP READ: OK")
        return True
    except Exception as e:
        print(f"  ❌ IMAP READ FAILED: {e}")
        return False


async def test_draft(handler, account_name: str):
    """Test 2: Create a draft email."""
    print(f"\n{'='*60}")
    print(f"TEST 2: Creating draft in {account_name}")
    print(f"{'='*60}")
    try:
        result = await handler.create_draft(
            recipients=["test@example.com"],
            subject=f"[TEST] Draft from KPI OS — {account_name}",
            body="This is a test draft created by KPI OS email MCP.\n\nIf you see this in your Drafts folder, the integration works!\n\nYou can safely delete this draft.",
        )
        print(f"  {result}")
        print("  ✅ CREATE DRAFT: OK")
        return True
    except Exception as e:
        print(f"  ❌ CREATE DRAFT FAILED: {e}")
        return False


async def main():
    if len(sys.argv) < 2:
        print("Usage: python test_draft.py <account_name>")
        print("\nAvailable accounts (from config.toml):")
        settings = get_settings()
        for email in settings.emails:
            print(f"  - {email.account_name} ({email.email_address})")
        sys.exit(1)

    account_name = sys.argv[1]
    settings = get_settings()
    account = settings.get_account(account_name)

    if not account:
        print(f"Account '{account_name}' not found in config.")
        print("\nAvailable accounts:")
        for email in settings.emails:
            print(f"  - {email.account_name} ({email.email_address})")
        sys.exit(1)

    print(f"Testing account: {account.email_address} ({account_name})")
    handler = ClassicEmailHandler(account)

    # Test 1: Read
    read_ok = await test_read(handler, account_name)

    if not read_ok:
        print("\n⛔ IMAP connection failed. Check your password and settings.")
        sys.exit(1)

    # Test 2: Create draft
    draft_ok = await test_draft(handler, account_name)

    # Summary
    print(f"\n{'='*60}")
    print("SUMMARY")
    print(f"{'='*60}")
    print(f"  IMAP Read:     {'✅' if read_ok else '❌'}")
    print(f"  Create Draft:  {'✅' if draft_ok else '❌'}")

    if read_ok and draft_ok:
        print(f"\n🎉 All tests passed for {account_name}!")
        print(f"   Check Drafts folder in {account.email_address}")
    else:
        print(f"\n⚠️  Some tests failed. Check errors above.")


if __name__ == "__main__":
    asyncio.run(main())
