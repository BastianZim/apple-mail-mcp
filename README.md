# Apple Mail MCP Server

A lightweight, **read-only** MCP server for Apple Mail on macOS.
Reads directly from Mail's local SQLite database and `.emlx` files — no
AppleScript needed for reading. Designed for large mailboxes (100K+ messages).

## Features

- **Fast search** — SQL-level filtering pushed down to SQLite, not Python-side iteration
- **Pagination** — `offset` / `limit` support for large result sets
- **Deterministic file lookup** — `.emlx` files located via a cached directory map, no `rglob` per message
- **Read-only** — no send capability, no AppleScript, minimal attack surface
- **Minimal dependencies** — just the `mcp` SDK and Python stdlib

## Tools

| Tool | Description |
|------|-------------|
| `list_accounts` | List configured mail accounts |
| `list_mailboxes` | List all folders with message counts and unread counts |
| `search_emails` | Search / filter by sender, subject, date range, mailbox, read status. Paginated. |
| `read_email` | Fetch full content of a single email by ID (including To, CC, body) |

## Installation

### With `uv` (recommended)

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
