"""
Read-only access to Apple Mail's local SQLite database and .emlx files.
Designed for large mailboxes (100K+ messages).
"""

import email
import email.policy
import logging
import re
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from email.header import decode_header
from pathlib import Path
from typing import Any, Optional
from urllib.parse import unquote

logger = logging.getLogger(__name__)

# Core Data epoch offset: seconds between 1970-01-01 and 2001-01-01
_CORE_DATA_EPOCH = 978307200

_EMLX_SUFFIXES = (".emlx", ".partial.emlx")
_EXTRA_HEADERS = ("to", "cc", "reply-to")

_MESSAGE_SELECT = """
    SELECT
        m.ROWID                        AS id,
        COALESCE(addr.address, '')      AS sender_address,
        COALESCE(addr.comment, '')      AS sender_name,
        COALESCE(subj.subject, '(no subject)') AS subject,
        m.date_received,
        COALESCE(mb.url, '')            AS mailbox_url,
        m.read,
        m.mailbox                       AS mailbox_id
    FROM messages m
    LEFT JOIN addresses addr ON m.sender   = addr.ROWID
    LEFT JOIN subjects  subj ON m.subject  = subj.ROWID
    LEFT JOIN mailboxes mb   ON m.mailbox  = mb.ROWID"""

# Pre-compiled regexes for HTML-to-text stripping
_RE_BR = re.compile(r"<br\s*/?>", re.IGNORECASE)
_RE_BLOCK_CLOSE = re.compile(r"</(p|div|tr|li)>", re.IGNORECASE)
_RE_TAG = re.compile(r"<[^>]+>")
_RE_MULTI_NEWLINE = re.compile(r"\n{3,}")


def _escape_like(value: str) -> str:
    """Escape LIKE special characters so they match literally."""
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


class MailDatabase:
    """Read-only interface to Apple Mail's local database."""

    def __init__(self, mail_dir: Optional[str] = None):
        self.mail_dir = Path(mail_dir) if mail_dir else Path.home() / "Library" / "Mail"
        self.v10_dir = self.mail_dir / "V10"
        self.db_path = self.v10_dir / "MailData" / "Envelope Index"

        if not self.db_path.exists():
            raise FileNotFoundError(
                f"Mail database not found at {self.db_path}. "
                "Make sure Apple Mail is configured."
            )

        # Cache list of Messages/ directories for fast .emlx lookup
        self._messages_dirs: list[Path] = []
        self._scan_messages_dirs()

    def _scan_messages_dirs(self) -> None:
        """Find all Messages/ directories under V10 (excluding MailData).

        This is run once at startup. Typically finds 10-50 directories,
        regardless of how many emails exist.
        """
        self._messages_dirs = []
        if not self.v10_dir.exists():
            return
        for mbox_dir in self.v10_dir.rglob("*.mbox"):
            messages_dir = mbox_dir / "Messages"
            if messages_dir.is_dir():
                self._messages_dirs.append(messages_dir)
        logger.info("Found %d mailbox message directories", len(self._messages_dirs))

    def _connect(self) -> sqlite3.Connection:
        """Open a read-only connection to the mail database."""
        conn = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        return conn

    @staticmethod
    def _core_data_to_iso(timestamp: Optional[float]) -> Optional[str]:
        """Convert Core Data timestamp to ISO 8601 string."""
        if timestamp is None:
            return None
        try:
            dt = datetime.fromtimestamp(
                timestamp + _CORE_DATA_EPOCH, tz=timezone.utc
            )
            return dt.isoformat()
        except (OSError, ValueError):
            return None

    @staticmethod
    def _iso_to_core_data(iso_str: str) -> Optional[float]:
        """Convert an ISO date string to a Core Data timestamp."""
        try:
            dt = datetime.fromisoformat(iso_str)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.timestamp() - _CORE_DATA_EPOCH
        except ValueError:
            return None

    @staticmethod
    def _decode_mime_header(value: str) -> str:
        """Decode MIME-encoded header value."""
        if not value:
            return ""
        try:
            parts = decode_header(value)
            decoded = []
            for part, encoding in parts:
                if isinstance(part, bytes):
                    decoded.append(
                        part.decode(encoding or "utf-8", errors="replace")
                    )
                else:
                    decoded.append(str(part))
            return "".join(decoded)
        except Exception:
            return str(value)

    @staticmethod
    def _mailbox_display_name(url: str) -> str:
        """Extract a human-readable mailbox name from its URL."""
        if not url:
            return "Unknown"
        if "/" in url:
            return unquote(url.split("/")[-1]).replace(".mbox", "")
        return url

    def _format_sender(self, name: str, address: str) -> str:
        """Format sender for display."""
        decoded_name = self._decode_mime_header(name)
        if decoded_name:
            return f"{decoded_name} <{address}>" if address else decoded_name
        return address or "Unknown"

    def _row_to_summary(self, row: sqlite3.Row) -> dict[str, Any]:
        """Convert a message SQL row to a summary dict."""
        return {
            "id": row["id"],
            "sender": self._format_sender(
                row["sender_name"], row["sender_address"]
            ),
            "subject": self._decode_mime_header(row["subject"]),
            "date": self._core_data_to_iso(row["date_received"]),
            "mailbox": self._mailbox_display_name(row["mailbox_url"]),
            "mailbox_id": row["mailbox_id"],
            "read": bool(row["read"]),
        }

    def _find_emlx_path(self, message_id: int) -> Optional[Path]:
        """Locate the .emlx file for a message across cached directories."""
        for messages_dir in self._messages_dirs:
            for suffix in _EMLX_SUFFIXES:
                candidate = messages_dir / f"{message_id}{suffix}"
                if candidate.exists():
                    return candidate
        return None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def list_accounts(self) -> list[dict[str, Any]]:
        """List mail accounts derived from mailbox URLs."""
        with closing(self._connect()) as conn:
            cursor = conn.execute(
                "SELECT DISTINCT url FROM mailboxes WHERE url IS NOT NULL AND url != ''"
            )
            accounts: dict[str, dict[str, str]] = {}
            for row in cursor:
                url: str = row["url"]
                for segment in url.split("/"):
                    if "@" in segment:
                        acct = unquote(segment)
                        if acct not in accounts:
                            accounts[acct] = {"account": acct}
                        break
            if not accounts:
                return [{"info": "Could not extract accounts. Use list_mailboxes instead."}]
            return list(accounts.values())

    def list_mailboxes(self) -> list[dict[str, Any]]:
        """List all mailboxes with message and unread counts."""
        with closing(self._connect()) as conn:
            cursor = conn.execute("""
                SELECT
                    mb.ROWID   AS id,
                    mb.url,
                    COUNT(m.ROWID) AS total_messages,
                    SUM(CASE WHEN m.read = 0 AND m.deleted = 0 THEN 1 ELSE 0 END)
                        AS unread_count
                FROM mailboxes mb
                LEFT JOIN messages m
                    ON m.mailbox = mb.ROWID AND m.deleted = 0
                GROUP BY mb.ROWID, mb.url
                ORDER BY total_messages DESC
            """)
            return [
                {
                    "id": row["id"],
                    "name": self._mailbox_display_name(row["url"]),
                    "url": row["url"] or "",
                    "total_messages": row["total_messages"],
                    "unread": row["unread_count"] or 0,
                }
                for row in cursor
            ]

    # ------------------------------------------------------------------
    # Body search via .emlx file scanning
    # ------------------------------------------------------------------

    def _body_search(
        self,
        body_text: str,
        candidate_ids: list[int],
        max_matches: int,
    ) -> list[int]:
        """Filter candidate message IDs by body content.

        Scans .emlx files for candidates and returns IDs whose body
        contains the search text (case-insensitive).  Stops after
        collecting ``max_matches`` hits.
        """
        search_lower = body_text.lower()
        matched: list[int] = []
        for msg_id in candidate_ids:
            path = self._find_emlx_path(msg_id)
            if path is None:
                continue
            body, _ = self._parse_emlx(path)
            if search_lower in body.lower():
                matched.append(msg_id)
                if len(matched) >= max_matches:
                    break
        return matched

    def search_emails(
        self,
        *,
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
    ) -> list[dict[str, Any]]:
        """Search emails with SQL-level filtering.

        All filters are AND-combined.  Returns metadata only — use
        ``read_email`` to fetch the full body.

        When ``body`` is provided, the search works in two passes:
        1. SQL filters narrow down candidates (sender, subject, date, etc.)
        2. .emlx files of candidates are scanned for the body text.

        If ``body`` is the *only* filter, the most recent 5000 messages
        are scanned.  Combine with other filters for faster results.

        Args:
            query:       Free-text across sender and subject.
            sender:      Substring match on sender address / display name.
            subject:     Substring match on subject line.
            body:        Full-text search in message body (scans .emlx files).
            mailbox_id:  Restrict to a specific mailbox (from list_mailboxes).
            unread_only: If True, only unread messages.
            date_from:   ISO date string, inclusive lower bound.
            date_to:     ISO date string, inclusive upper bound.
            limit:       Max results (default 20, capped at 200).
            offset:      Pagination offset.
        """
        limit = max(1, min(limit, 200))
        offset = max(0, offset)

        conditions = ["m.deleted = 0"]
        params: list[Any] = []

        if unread_only:
            conditions.append("m.read = 0")

        if mailbox_id is not None:
            conditions.append("m.mailbox = ?")
            params.append(mailbox_id)

        if sender:
            conditions.append("(addr.address LIKE ? ESCAPE '\\' OR addr.comment LIKE ? ESCAPE '\\')")
            like = f"%{_escape_like(sender)}%"
            params.extend([like, like])

        if subject:
            conditions.append("subj.subject LIKE ? ESCAPE '\\'")
            params.append(f"%{_escape_like(subject)}%")

        if query:
            conditions.append(
                "(addr.address LIKE ? ESCAPE '\\'"
                " OR addr.comment LIKE ? ESCAPE '\\'"
                " OR subj.subject LIKE ? ESCAPE '\\')"
            )
            like_q = f"%{_escape_like(query)}%"
            params.extend([like_q, like_q, like_q])

        if date_from:
            ts = self._iso_to_core_data(date_from)
            if ts is not None:
                conditions.append("m.date_received >= ?")
                params.append(ts)

        if date_to:
            ts = self._iso_to_core_data(date_to)
            if ts is not None:
                conditions.append("m.date_received <= ?")
                params.append(ts)

        where = " AND ".join(conditions)

        # When body search is requested, we need a two-pass approach:
        # 1. Get candidate IDs from SQL (larger set)
        # 2. Scan their .emlx files for the body text
        # 3. Then fetch final metadata for matches with limit/offset
        if body:
            # Determine how many candidates to fetch for body scanning.
            # If other filters are present, they'll narrow the set.
            # If body is the only filter, cap at 5000 most recent.
            has_other_filters = any([
                query, sender, subject, mailbox_id,
                unread_only, date_from, date_to,
            ])
            candidate_limit = 50000 if has_other_filters else 5000

            candidate_sql = f"""
                SELECT m.ROWID AS id
                FROM messages m
                LEFT JOIN addresses addr ON m.sender   = addr.ROWID
                LEFT JOIN subjects  subj ON m.subject  = subj.ROWID
                LEFT JOIN mailboxes mb   ON m.mailbox  = mb.ROWID
                WHERE {where}
                ORDER BY m.date_received DESC
                LIMIT ?
            """
            with closing(self._connect()) as conn:
                rows = conn.execute(candidate_sql, params + [candidate_limit]).fetchall()
                candidate_ids = [row["id"] for row in rows]

            if not candidate_ids:
                return []

            # Scan .emlx files for body matches, stopping once we have
            # enough to satisfy offset + limit
            matched_ids = self._body_search(body, candidate_ids, offset + limit)

            # Apply offset and limit to matched IDs
            # (IDs are already in date_received DESC order)
            paged_ids = matched_ids[offset : offset + limit]

            if not paged_ids:
                return []

            # Fetch full metadata for the paged results using parameterized query
            placeholders = ", ".join("?" for _ in paged_ids)
            meta_sql = f"{_MESSAGE_SELECT}\n    WHERE m.ROWID IN ({placeholders})\n    ORDER BY m.date_received DESC"
            with closing(self._connect()) as conn:
                rows = conn.execute(meta_sql, paged_ids).fetchall()
                return [self._row_to_summary(row) for row in rows]

        # Standard search without body (fast, SQL-only)
        sql = f"{_MESSAGE_SELECT}\n    WHERE {where}\n    ORDER BY m.date_received DESC\n    LIMIT ? OFFSET ?"
        params.extend([limit, offset])

        with closing(self._connect()) as conn:
            rows = conn.execute(sql, params).fetchall()
            return [self._row_to_summary(row) for row in rows]

    def read_email(self, message_id: int) -> dict[str, Any]:
        """Read full email content by message ID.

        Fetches metadata from the DB and body from the .emlx file on disk.
        """
        with closing(self._connect()) as conn:
            row = conn.execute(
                f"{_MESSAGE_SELECT}\n    WHERE m.ROWID = ?",
                (message_id,),
            ).fetchone()

            if not row:
                return {"error": f"Message {message_id} not found"}

            body, extra_headers = self._read_emlx(message_id)

            result = self._row_to_summary(row)
            result["body"] = body

            for key in _EXTRA_HEADERS:
                if key in extra_headers:
                    result[key.replace("-", "_")] = extra_headers[key]

            return result

    # ------------------------------------------------------------------
    # .emlx handling
    # ------------------------------------------------------------------

    def _read_emlx(self, message_id: int) -> tuple[str, dict[str, str]]:
        """Locate and parse an .emlx file by message ID.

        Checks each cached Messages/ directory.  Typically only ~10-50
        directories exist, so this is effectively O(1) per message.
        """
        path = self._find_emlx_path(message_id)
        if path is not None:
            return self._parse_emlx(path)
        return "(message body not found on disk)", {}

    @staticmethod
    def _parse_emlx(path: Path) -> tuple[str, dict[str, str]]:
        """Parse an .emlx file and return (body_text, extra_headers)."""
        try:
            with open(path, "rb") as fh:
                first_line = fh.readline()
                try:
                    byte_count = int(first_line.strip())
                    email_bytes = fh.read(byte_count)
                except ValueError:
                    email_bytes = first_line + fh.read()

            msg = email.message_from_bytes(
                email_bytes, policy=email.policy.default
            )
            body = MailDatabase._extract_text(msg)

            headers: dict[str, str] = {}
            for hdr in _EXTRA_HEADERS:
                val = msg.get(hdr)
                if val:
                    headers[hdr] = str(val)

            return body, headers
        except Exception as exc:
            logger.debug("Error parsing %s: %s", path, exc)
            return f"(error reading message: {exc})", {}

    @staticmethod
    def _extract_text(msg: Any) -> str:
        """Extract plain text from email, falling back to stripped HTML."""
        plain_parts: list[str] = []
        html_parts: list[str] = []

        def _decode_part(part: Any) -> Optional[str]:
            payload = part.get_payload(decode=True)
            if not payload:
                return None
            charset = part.get_content_charset() or "utf-8"
            return payload.decode(charset, errors="replace")

        if msg.is_multipart():
            for part in msg.walk():
                ct = part.get_content_type()
                if ct == "text/plain":
                    text = _decode_part(part)
                    if text:
                        plain_parts.append(text)
                elif ct == "text/html" and not plain_parts:
                    text = _decode_part(part)
                    if text:
                        html_parts.append(text)
        else:
            text = _decode_part(msg)
            if text:
                if msg.get_content_type() == "text/plain":
                    plain_parts.append(text)
                elif msg.get_content_type() == "text/html":
                    html_parts.append(text)

        if plain_parts:
            return "\n".join(plain_parts).strip()

        if html_parts:
            raw = "\n".join(html_parts)
            raw = _RE_BR.sub("\n", raw)
            raw = _RE_BLOCK_CLOSE.sub("\n", raw)
            raw = _RE_TAG.sub("", raw)
            for entity, char in (("&nbsp;", " "), ("&amp;", "&"),
                                  ("&lt;", "<"), ("&gt;", ">")):
                raw = raw.replace(entity, char)
            raw = _RE_MULTI_NEWLINE.sub("\n\n", raw)
            return raw.strip()

        return "(no text content)"
