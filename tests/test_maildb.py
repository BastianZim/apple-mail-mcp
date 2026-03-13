"""Tests for MailDatabase utility methods.

These tests exercise static helpers without needing an actual Apple Mail database.
"""

import email
import email.policy
from pathlib import Path

from apple_mail_mcp.maildb import MailDatabase, _CORE_DATA_EPOCH


# -- Timestamp conversion -----------------------------------------------------


class TestCoreDataToIso:
    def test_known_date(self):
        # 2025-01-01T00:00:00+00:00 as Core Data timestamp
        cd_ts = 1735689600.0 - _CORE_DATA_EPOCH
        result = MailDatabase._core_data_to_iso(cd_ts)
        assert result == "2025-01-01T00:00:00+00:00"

    def test_none_returns_none(self):
        assert MailDatabase._core_data_to_iso(None) is None

    def test_invalid_timestamp_returns_none(self):
        assert MailDatabase._core_data_to_iso(-1e18) is None


class TestIsoToCoreData:
    def test_roundtrip(self):
        iso = "2025-06-15T12:00:00+00:00"
        cd_ts = MailDatabase._iso_to_core_data(iso)
        assert cd_ts is not None
        result = MailDatabase._core_data_to_iso(cd_ts)
        assert result == iso

    def test_naive_datetime_treated_as_utc(self):
        cd1 = MailDatabase._iso_to_core_data("2025-01-01")
        cd2 = MailDatabase._iso_to_core_data("2025-01-01T00:00:00+00:00")
        assert cd1 == cd2

    def test_invalid_string_returns_none(self):
        assert MailDatabase._iso_to_core_data("not-a-date") is None


# -- MIME header decoding ------------------------------------------------------


class TestDecodeMimeHeader:
    def test_plain_ascii(self):
        assert MailDatabase._decode_mime_header("Hello World") == "Hello World"

    def test_empty_string(self):
        assert MailDatabase._decode_mime_header("") == ""

    def test_encoded_utf8(self):
        encoded = "=?utf-8?B?SMOpbGxv?="  # "Héllo" base64-encoded
        result = MailDatabase._decode_mime_header(encoded)
        assert "H" in result  # at minimum decodes without error

    def test_encoded_subject(self):
        encoded = "=?iso-8859-1?Q?Re=3A_Meeting?="
        result = MailDatabase._decode_mime_header(encoded)
        assert "Re:" in result
        assert "Meeting" in result


# -- Mailbox display name -----------------------------------------------------


class TestMailboxDisplayName:
    def test_extracts_last_segment(self):
        url = "imap://user@example.com/INBOX.mbox"
        assert MailDatabase._mailbox_display_name(url) == "INBOX"

    def test_strips_mbox_suffix(self):
        url = "imap://user@example.com/Sent%20Messages.mbox"
        assert MailDatabase._mailbox_display_name(url) == "Sent Messages"

    def test_empty_returns_unknown(self):
        assert MailDatabase._mailbox_display_name("") == "Unknown"

    def test_no_slash_returns_url(self):
        assert MailDatabase._mailbox_display_name("INBOX") == "INBOX"


# -- Sender formatting --------------------------------------------------------


class TestFormatSender:
    def setup_method(self):
        # Create instance without hitting __init__ (which needs a real DB)
        self.db = object.__new__(MailDatabase)

    def test_name_and_address(self):
        result = self.db._format_sender("Alice", "alice@example.com")
        assert result == "Alice <alice@example.com>"

    def test_address_only(self):
        result = self.db._format_sender("", "alice@example.com")
        assert result == "alice@example.com"

    def test_name_only(self):
        result = self.db._format_sender("Alice", "")
        assert result == "Alice"

    def test_neither(self):
        result = self.db._format_sender("", "")
        assert result == "Unknown"


# -- Email text extraction ----------------------------------------------------


class TestExtractText:
    def _make_message(self, content: str, content_type: str = "text/plain") -> email.message.EmailMessage:
        msg = email.message.EmailMessage()
        msg.set_content(content, subtype=content_type.split("/")[1])
        return msg

    def test_plain_text(self):
        msg = self._make_message("Hello, world!")
        result = MailDatabase._extract_text(msg)
        assert "Hello, world!" in result

    def test_html_fallback(self):
        msg = self._make_message("<p>Hello</p>", "text/html")
        result = MailDatabase._extract_text(msg)
        assert "Hello" in result

    def test_multipart_prefers_plain(self):
        msg = email.message.EmailMessage()
        msg.make_mixed()
        plain = email.message.EmailMessage()
        plain.set_content("Plain text body")
        html = email.message.EmailMessage()
        html.set_content("<b>HTML body</b>", subtype="html")
        msg.attach(plain)
        msg.attach(html)
        result = MailDatabase._extract_text(msg)
        assert "Plain text body" in result


# -- .emlx parsing ------------------------------------------------------------


class TestParseEmlx:
    def test_parse_valid_emlx(self, tmp_path: Path):
        # .emlx format: first line is byte count, then raw RFC822 message
        raw_email = (
            b"From: sender@example.com\r\n"
            b"To: recipient@example.com\r\n"
            b"Subject: Test\r\n"
            b"\r\n"
            b"This is the body.\r\n"
        )
        emlx_content = f"{len(raw_email)}\n".encode() + raw_email
        emlx_file = tmp_path / "12345.emlx"
        emlx_file.write_bytes(emlx_content)

        body, headers = MailDatabase._parse_emlx(emlx_file)
        assert "This is the body." in body
        assert headers["to"] == "recipient@example.com"

    def test_parse_missing_byte_count(self, tmp_path: Path):
        # Gracefully handles .emlx without a valid byte count on line 1
        raw_email = (
            b"From: sender@example.com\r\n"
            b"Subject: No count\r\n"
            b"\r\n"
            b"Body here.\r\n"
        )
        emlx_file = tmp_path / "99999.emlx"
        emlx_file.write_bytes(raw_email)

        body, headers = MailDatabase._parse_emlx(emlx_file)
        assert "Body here." in body
