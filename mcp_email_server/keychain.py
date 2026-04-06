"""macOS Keychain integration for secure password storage (READ-ONLY).

Passwords in config.toml can use the format:
    password = "keychain:service_name/account_name"

This module resolves those references by calling the macOS
`security` CLI tool. The actual password lives only in Keychain
(encrypted, protected by biometrics or system password) and in
process memory — never written to disk in plaintext.

This module is intentionally READ-ONLY. It cannot write, update,
or delete Keychain entries. Password management is done by the
user via `security add-generic-password` or the setup script.

On non-macOS systems, keychain references will raise an error
with a clear message.
"""

from __future__ import annotations

import platform
import subprocess

from mcp_email_server.log import logger

KEYCHAIN_PREFIX = "keychain:"
DEFAULT_SERVICE = "mcp-email-server"


def is_keychain_ref(value: str) -> bool:
    """Check if a password value is a Keychain reference."""
    return value.startswith(KEYCHAIN_PREFIX)


def _parse_keychain_ref(ref: str) -> tuple[str, str]:
    """Parse a keychain reference into (service, account).

    Formats:
        keychain:account_name          -> (mcp-email-server, account_name)
        keychain:service_name/account   -> (service_name, account)

    Args:
        ref: A string starting with 'keychain:'

    Returns:
        Tuple of (service_name, account_name)
    """
    path = ref[len(KEYCHAIN_PREFIX):]
    if "/" in path:
        service, account = path.split("/", 1)
        return service, account
    return DEFAULT_SERVICE, path


def resolve_keychain_password(ref: str) -> str:
    """Resolve a keychain reference to the actual password.

    Uses macOS `security find-generic-password` CLI to read from
    the login keychain. The password is returned as a string and
    exists only in process memory.

    Args:
        ref: A keychain reference string (e.g., 'keychain:camp@kurenivka.ua'
             or 'keychain:mcp-email-server/camp@kurenivka.ua')

    Returns:
        The password string from Keychain.

    Raises:
        RuntimeError: If not on macOS or if Keychain lookup fails.
    """
    if platform.system() != "Darwin":
        msg = (
            f"Keychain references ('{ref}') are only supported on macOS. "
            f"Please use a plaintext password in config.toml on this platform."
        )
        raise RuntimeError(msg)

    service, account = _parse_keychain_ref(ref)

    try:
        result = subprocess.run(
            [
                "security",
                "find-generic-password",
                "-s", service,
                "-a", account,
                "-w",  # output only the password
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )

        if result.returncode != 0:
            stderr = result.stderr.strip()
            msg = (
                f"Failed to read password from Keychain for "
                f"service='{service}', account='{account}': {stderr}\n"
                f"\nTo add this password to Keychain, run:\n"
                f"  security add-generic-password -s '{service}' -a '{account}' -w 'YOUR_PASSWORD'"
            )
            raise RuntimeError(msg)

        password = result.stdout.strip()
        if not password:
            msg = (
                f"Empty password returned from Keychain for "
                f"service='{service}', account='{account}'"
            )
            raise RuntimeError(msg)

        logger.info(f"Resolved password from Keychain: service='{service}', account='{account}'")
        return password

    except subprocess.TimeoutExpired:
        msg = (
            f"Timeout reading from Keychain for service='{service}', "
            f"account='{account}'. The Keychain may be locked \u2014 "
            f"try unlocking it first."
        )
        raise RuntimeError(msg)
    except FileNotFoundError:
        msg = "macOS `security` command not found. Is this a macOS system?"
        raise RuntimeError(msg)
