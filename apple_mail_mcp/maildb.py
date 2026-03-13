"""
Read-only access to Apple Mail's local SQLite database and .emlx files.
Designed for large mailboxes (100K+ messages).
"""

import email
import email.policy
import logging
import re
import sqlite3
from datetime import datetime, timezone
from email.header import decode_header
from pathlib import Path
from typing import Any, Optional
from urllib.parse import unquote

logger = logging.getLogger(__name__)

# Core Data epoch offset: seconds between 1970-01-01 and 2001-01-01
_CORE_DATA_EPOCH = 978307200


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

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def list_accounts(self) -> list[dict[str, Any]]:
        """List mail accounts derived from mailbox URLs."""
        conn = self._connect()
        try:
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
        finally:
            conn.close()

    def list_mailboxes(self) -> list[dict[str, Any]]:
        """List all mailboxes with message and unread counts."""
        conn = self._connect()
        try:
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
            results = []
            for row in cursor:
                results.append({
                    "id": row["id"],
                    "name": self._mailbox_display_name(row["url"]),
                    "url": row["url"] or "",
                    "total_messages": row["total_messages"],
                    "unread": row["unread_count"] or 0,
                })
            return results
        finally:
            conn.close()

    def search_emails(
        self,
        *,
        query: Optional[str] = None,
        sender: Optional[str] = None,
        subject: Optional[str] = None,
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

        Args:
            query:       Free-text across sender and subject.
            sender:      Substring match on sender address / display name.
            subject:     Substring match on subject line.
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
            like = f"%{sender}%"
            params.extend([like, like])

        if subject:
            conditions.append("subj.subject LIKE ? ESCAPE '\\'")
            params.append(f"%{subject}%")

        if query:
            conditions.append(
                "(addr.address LIKE ? ESCAPE '\\'"
                " OR addr.comment LIKE ? ESCAPE '\\'"
                " OR subj.subject LIKE ? ESCAPE '\\')"
            )
            like_q = f"%{query}%"
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

        sql = f"""
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
            LEFT JOIN mailboxes mb   ON m.mailbox  = mb.ROWID
            WHERE {where}
            ORDER BY m.date_received DESC
            LIMIT ? OFFSET ?
        """
        params.extend([limit, offset])

        conn = self._connect()
        try:
            rows = conn.execute(sql, params).fetchall()
            results = []
            for row in rows:
                results.append({
                    "id": row["id"],
                    "sender": self._format_sender(
                        row["sender_name"], row["sender_address"]
                    ),
                    "subject": self._decode_mime_header(row["subject"]),
                    "date": self._core_data_to_iso(row["date_received"]),
                    "mailbox": self._mailbox_display_name(row["mailbox_url"]),
                    "mailbox_id": row["mailbox_id"],
                    "read": bool(row["read"]),
                })
            return results
        finally:
            conn.close()

    def read_email(self, message_id: int) -> dict[str, Any]:
        """Read full email content by message ID.

        Fetches metadata from the DB and body from the .emlx file on disk.
        """
        conn = self._connect()
        try:
            row = conn.execute("""
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
                LEFT JOIN mailboxes mb   ON m.mailbox  = mb.ROWID
                WHERE m.ROWID = ?
            """, (message_id,)).fetchone()

            if not row:
                return {"error": f"Message {message_id} not found"}

            body, extra_headers = self._read_emlx(message_id)

            result: dict[str, Any] = {
                "id": row["id"],
                "sender": self._format_sender(
                    row["sender_name"], row["sender_address"]
                ),
                "subject": self._decode_mime_header(row["subject"]),
                "date": self._core_data_to_iso(row["date_received"]),
                "mailbox": self._mailbox_display_name(row["mailbox_url"]),
                "read": bool(row["read"]),
                "body": body,
            }

            for key in ("to", "cc", "reply-to"):
                if key in extra_headers:
                    result[key.replace("-", "_")] = extra_headers[key]

            return result
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # .emlx handling
    # ------------------------------------------------------------------

    def _read_emlx(self, message_id: int) -> tuple[str, dict[str, str]]:
        """Locate and parse an .emlx file by message ID.

        Checks each cached Messages/ directory.  Typically only ~10-50
        directories exist, so this is effectively O(1) per message.
        """
        for messages_dir in self._messages_dirs:
            for suffix in (".emlx", ".partial.emlx"):
                candidate = messages_dir / f"{message_id}{suffix}"
                if candidate.exists():
                    return self._parse_emlx(candidate)
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
            for hdr in ("to", "cc", "reply-to"):
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
            raw = re.sub(r"<br\s*/?>", "\n", raw, flags=re.IGNORECASE)
            raw = re.sub(r"</(p|div|tr|li)>", "\n", raw, flags=re.IGNORECASE)
            raw = re.sub(r"<[^>]+>", "", raw)
            for entity, char in (("&nbsp;", " "), ("&amp;", "&"),
                                  ("&lt;", "<"), ("&gt;", ">")):
                raw = raw.replace(entity, char)
            raw = re.sub(r"\n{3,}", "\n\n", raw)
            return raw.strip()

        return "(no text content)"
