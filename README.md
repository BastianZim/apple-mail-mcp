# Apple Mail MCP Server

A lightweight, **read-only** MCP server for Apple Mail on macOS.
Reads directly from Mail's local SQLite database and `.emlx` files — no
AppleScript needed for reading. Designed for large mailboxes (tested with
290K+ messages across multiple accounts).

## Features

- **Fast search** — SQL-level filtering pushed down to SQLite, not Python-side iteration
- **Pagination** — `offset` / `limit` support for large result sets
- **Deterministic file lookup** — `.emlx` files located via a cached directory map, no `rglob` per message
- **Read-only** — no send capability, no AppleScript, minimal attack surface
- **Minimal dependencies** — just the `mcp` SDK and Python stdlib
- **Multi-account support** — works with iCloud (IMAP), Exchange (EWS), Gmail, and other accounts configured in Mail.app

## Tools

| Tool | Description |
|------|-------------|
| `list_accounts` | List configured mail accounts |
| `list_mailboxes` | List all folders with message counts and unread counts |
| `search_emails` | Search / filter by sender, subject, date range, mailbox, read status. Paginated. |
| `read_email` | Fetch full content of a single email by ID (including To, CC, body) |

## Installation

### With `uvx` (recommended)

Add to your Claude Desktop config (`~/Library/Application Support/Claude/claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "apple-mail": {
      "command": "uvx",
      "args": [
        "--from",
        "git+https://github.com/BastianZim/apple-mail-mcp",
        "apple-mail-mcp"
      ]
    }
  }
}
```

Restart Claude Desktop after saving.

### From source

```bash
git clone https://github.com/BastianZim/apple-mail-mcp
cd apple-mail-mcp
uv run apple-mail-mcp
```

## Requirements

- macOS 10.15+ (Catalina or later)
- Python 3.10+
- Apple Mail configured with at least one account
- **Full Disk Access** for the process running the server (see below)

### macOS permissions

Apple Mail's database lives in `~/Library/Mail/`, which macOS protects.
You must grant **Full Disk Access** to the process that runs the MCP server.

For **Claude Desktop** using `uvx`:

1. Open **System Settings → Privacy & Security → Full Disk Access**
2. Click the **+** button
3. Add the `uvx` binary (typically at `/opt/homebrew/bin/uvx`)
4. Restart Claude Desktop

Without this, the server will fail with `unable to open database file`.

> **Tip:** To find where `uvx` lives on your system, run `which uvx` in your terminal.

## How it works

Apple Mail stores email metadata in a SQLite database at
`~/Library/Mail/V10/MailData/Envelope Index` and message bodies in `.emlx`
files inside `.mbox` directories.

This server:

1. Opens the SQLite DB **read-only** for fast, indexed queries.
2. Caches the list of `.mbox/Messages/` directories at startup (typically
   10–50 dirs regardless of email count).
3. Resolves `.emlx` files by message ID with simple `stat()` calls —
   no filesystem search.

## search_emails parameters

All filters are AND-combined.

| Parameter | Type | Description |
|-----------|------|-------------|
| `query` | `str` | Free-text across sender + subject |
| `sender` | `str` | Substring match on address or display name |
| `subject` | `str` | Substring match on subject line |
| `mailbox_id` | `int` | Restrict to a mailbox (from `list_mailboxes`) |
| `unread_only` | `bool` | Only unread messages |
| `date_from` | `str` | ISO date, inclusive start (`2025-01-01`) |
| `date_to` | `str` | ISO date, inclusive end (`2025-12-31`) |
| `limit` | `int` | Max results, default 20, max 200 |
| `offset` | `int` | Pagination offset |

## License

MIT
