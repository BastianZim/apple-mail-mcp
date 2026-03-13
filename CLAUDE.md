# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project overview

Read-only MCP server for Apple Mail on macOS. Reads email metadata from Mail's SQLite database (`~/Library/Mail/V10/MailData/Envelope Index`) and message bodies from `.emlx` files. No AppleScript dependency. Designed for large mailboxes (100K+ messages).

## Commands

```bash
# Run the server
uv run apple-mail-mcp

# Install dependencies
uv sync
```

There are no tests or linting configured yet.

## Architecture

Two-module design under `apple_mail_mcp/`:

- **`server.py`** — MCP tool definitions using `FastMCP`. Lazily initializes a single `MailDatabase` instance. Each tool returns JSON strings. Entry point: `main()`.
- **`maildb.py`** — `MailDatabase` class. All data access lives here: read-only SQLite connections, `.emlx` file parsing, timestamp conversion (Core Data epoch → ISO 8601), MIME header decoding, HTML-to-text fallback.

Data flow: MCP tool → `_get_db()` → `MailDatabase` method → SQLite query + optional `.emlx` read → JSON response.

Key details:
- SQLite is opened read-only via URI (`?mode=ro`)
- `.emlx` lookup: at startup, `MailDatabase` caches all `Messages/` directories (typically 10–50). Finding a message is O(dirs) stat calls, not a filesystem search.
- Core Data timestamps are offset by `978307200` seconds from Unix epoch
- Only dependency beyond stdlib: `mcp` SDK (uses `mcp.server.fastmcp.FastMCP`)
- Build system: hatchling
