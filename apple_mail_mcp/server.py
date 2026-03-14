#!/usr/bin/env python3
"""
Apple Mail MCP Server — read-only access to Apple Mail.

Reads directly from Mail's local SQLite database and .emlx files.
No AppleScript dependency for reading. Designed for large mailboxes.
"""

import json
import logging
import sys
from typing import Optional

from mcp.server.fastmcp import FastMCP

from .maildb import MailDatabase

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[logging.StreamHandler(sys.stderr)],
)
logger = logging.getLogger(__name__)

app = FastMCP("Apple Mail (read-only)")

# Lazy initialisation — server starts even if Mail isn't set up yet.
_db: Optional[MailDatabase] = None


def _get_db() -> MailDatabase:
    global _db
    if _db is None:
        _db = MailDatabase()
    return _db


@app.tool()
def list_accounts() -> str:
    """List configured mail accounts."""
    try:
        return json.dumps(_get_db().list_accounts(), indent=2)
    except Exception as exc:
        return f"Error: {exc}"


@app.tool()
def list_mailboxes() -> str:
    """List all mailboxes / folders with message counts and unread counts.

    Returns mailbox id, display name, full URL, total messages, and unread
    count.  Use the mailbox id with search_emails to restrict results to a
    single folder.
    """
    try:
        return json.dumps(_get_db().list_mailboxes(), indent=2)
    except Exception as exc:
        return f"Error: {exc}"


@app.tool()
def search_emails(
    query: Optional[str] = None,
    sender: Optional[str] = None,
    subject: Optional[str] = None,
    body: Optional[str] = None,
    mailbox_id: Optional[int] = None,
    unread_only: bool = False,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    limit: int = 20,
    offset: int = 0,
) -> str:
    """Search emails with flexible filtering.  All filters are AND-combined.

    Returns metadata only (sender, subject, date, mailbox, read status).
    Use read_email with the returned id to fetch the full body.

    Args:
        query:       Free-text search across sender and subject.
        sender:      Substring match on sender address or display name.
        subject:     Substring match on subject line.
        body:        Full-text search in message body (scans .emlx files).
                     Combine with other filters for faster results.
                     If used alone, scans the 5000 most recent messages.
        mailbox_id:  Restrict to a specific mailbox (get ids from list_mailboxes).
        unread_only: Only return unread messages.
        date_from:   ISO date for start of range, e.g. '2025-01-01'.
        date_to:     ISO date for end of range, e.g. '2025-12-31'.
        limit:       Max results to return (default 20, max 200).
        offset:      Skip this many results for pagination.
    """
    try:
        emails = _get_db().search_emails(
            query=query,
            sender=sender,
            subject=subject,
            body=body,
            mailbox_id=mailbox_id,
            unread_only=unread_only,
            date_from=date_from,
            date_to=date_to,
            limit=limit,
            offset=offset,
        )
        return json.dumps(emails, indent=2, default=str)
    except Exception as exc:
        return f"Error: {exc}"


@app.tool()
def read_email(message_id: int) -> str:
    """Read the full content of a single email by its message ID.

    Returns sender, recipients (to, cc), subject, date, mailbox, and the
    full body text.

    Args:
        message_id: The email's ID (from search_emails results).
    """
    try:
        return json.dumps(_get_db().read_email(message_id), indent=2, default=str)
    except Exception as exc:
        return f"Error: {exc}"


def main() -> None:
    """Entry point."""
    if sys.platform != "darwin":
        logger.error("This MCP server requires macOS.")
        sys.exit(1)
    logger.info("Starting Apple Mail MCP server (read-only)...")
    app.run()


if __name__ == "__main__":
    main()
