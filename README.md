# mcp-email-server (ykorets fork)

Fork of [ai-zerolab/mcp-email-server](https://github.com/ai-zerolab/mcp-email-server) with custom extensions for multi-account email management via Claude Desktop.

## What This Fork Adds

Three enhancements on top of the upstream MCP email server:

### 1. `mark_as_read` Tool

New MCP tool that marks emails as read (IMAP `\Seen` flag) by their UID.

**Usage from Claude:**
```
mark_as_read(account_name="gmail-personal", email_ids=["154051", "154052"])
```

Files changed: `mcp_email_server/app.py`, `mcp_email_server/emails/classic.py`

### 2. macOS Keychain Password Support

Instead of storing plaintext passwords in `config.toml`, you can reference macOS Keychain entries:

```toml
[[emails]]
account_name = "gmail-personal"

[emails.incoming]
password = "keychain:gmail-personal"
```

The server resolves `keychain:<account>` at startup by calling:
```
security find-generic-password -s 'mcp-email-server' -a '<account>' -w
```

To store a password in Keychain:
```bash
security add-generic-password -s 'mcp-email-server' -a 'gmail-personal' -w 'your-app-password'
```

The real password is never written back to `config.toml` — serialization preserves the `keychain:` reference.

Files changed: `mcp_email_server/config.py`

### 3. `create_draft` Tool

Saves email drafts to the IMAP Drafts folder without sending. Already present in upstream — included here for completeness.

## Installation

This fork is installed from source on macOS:

```bash
cd ~/Projects/mcp-email-server
git pull
pip3 install . --break-system-packages
```

Claude Desktop config (`~/Library/Application Support/Claude/claude_desktop_config.json`):
```json
{
  "mcpServers": {
    "email": {
      "command": "mcp-email-server",
      "args": ["stdio"]
    }
  }
}
```

After installing, restart Claude Desktop to pick up the new tools.

## Patching Workflow

When Claude makes changes to MCP server code, the established deployment method is:

**For small files** — push via GitHub API (`mcp__github__push_files`), then `git pull` locally.

**For large files** (>40KB, e.g. `classic.py`) — use a heredoc Python patch script:

```bash
python3 << 'PATCH'
import pathlib
p = pathlib.Path('mcp_email_server/emails/classic.py')
s = p.read_text()
if 'target_function' in s:
    print('Already patched'); exit()
# ... string replacement logic ...
p.write_text(s)
print('Patched!')
PATCH
```

**Full deploy sequence:**
```bash
cd ~/Projects/mcp-email-server
git pull                                    # get small file changes
python3 << 'PATCH' ... PATCH                # apply large file patches (if any)
pip3 install . --break-system-packages      # reinstall
# Restart Claude Desktop
```

## Configuration

Email accounts are configured in `~/.config/zerolib/mcp_email_server/config.toml`. This fork supports 11 accounts with Keychain-backed passwords.

See the [upstream README](https://github.com/ai-zerolab/mcp-email-server#readme) for full configuration options including environment variables, SSL settings, attachment downloads, and SMTP configuration.

## Upstream

All original features from [ai-zerolab/mcp-email-server](https://github.com/ai-zerolab/mcp-email-server) are preserved: IMAP/SMTP via MCP, multi-account support, email threading, attachment downloads, self-signed certificate support, and more.

## License

Same as upstream — see [LICENSE](LICENSE).
