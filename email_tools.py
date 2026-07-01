from __future__ import annotations

import imaplib
import os
import smtplib
from dataclasses import dataclass
from email import message_from_bytes
from email.message import EmailMessage
from email.header import decode_header
from email.utils import parseaddr
from typing import Iterable

from oauth_tools import MsalTokenStore


@dataclass(frozen=True)
class EmailConfig:
    address: str
    imap_host: str
    imap_port: int
    imap_username: str
    smtp_host: str
    smtp_port: int
    smtp_username: str
    token_store: MsalTokenStore
    mailbox: str = "INBOX"

    @classmethod
    def from_env(cls) -> "EmailConfig":
        address = os.getenv("ARGOWAKE_EMAIL_ADDRESS", "help@argowake.com")
        imap_host = os.getenv("ARGOWAKE_IMAP_HOST", "outlook.office365.com")
        imap_username = os.getenv("ARGOWAKE_IMAP_USERNAME", address)
        smtp_host = os.getenv("ARGOWAKE_SMTP_HOST", "smtp-mail.outlook.com")
        smtp_username = os.getenv("ARGOWAKE_SMTP_USERNAME", address)
        return cls(
            address=address,
            imap_host=imap_host,
            imap_port=int(os.getenv("ARGOWAKE_IMAP_PORT", "993")),
            imap_username=imap_username,
            smtp_host=smtp_host,
            smtp_port=int(os.getenv("ARGOWAKE_SMTP_PORT", "587")),
            smtp_username=smtp_username,
            token_store=MsalTokenStore.from_env(),
            mailbox=os.getenv("ARGOWAKE_EMAIL_MAILBOX", "INBOX"),
        )

    def validate_inbound(self) -> None:
        missing = [name for name, value in (
            ("ARGOWAKE_IMAP_HOST", self.imap_host),
        ) if not value]
        if missing:
            raise ValueError(f"Missing email inbound configuration: {', '.join(missing)}")

    def validate_outbound(self) -> None:
        missing = [name for name, value in (
            ("ARGOWAKE_SMTP_HOST", self.smtp_host),
        ) if not value]
        if missing:
            raise ValueError(f"Missing email outbound configuration: {', '.join(missing)}")


@dataclass(frozen=True)
class EmailMessageSummary:
    uid: str
    from_address: str
    subject: str
    date: str
    snippet: str
    body: str

    def as_prompt(self) -> str:
        return (
            f"From: {self.from_address}\n"
            f"Subject: {self.subject}\n"
            f"Date: {self.date}\n"
            f"Snippet: {self.snippet}\n"
            f"Body:\n{self.body}"
        )


def _decode_header_value(value: str | None) -> str:
    if not value:
        return ""
    parts = decode_header(value)
    decoded = []
    for part, encoding in parts:
        if isinstance(part, bytes):
            decoded.append(part.decode(encoding or "utf-8", errors="replace"))
        else:
            decoded.append(part)
    return "".join(decoded).strip()


def _extract_text_body(message) -> str:
    if message.is_multipart():
        preferred: list[str] = []
        for part in message.walk():
            content_type = part.get_content_type()
            disposition = part.get_content_disposition()
            if disposition == "attachment":
                continue
            if content_type == "text/plain":
                payload = part.get_payload(decode=True)
                if payload:
                    preferred.append(payload.decode(part.get_content_charset() or "utf-8", errors="replace"))
        if preferred:
            return "\n\n".join(preferred).strip()
    payload = message.get_payload(decode=True)
    if not payload:
        return ""
    return payload.decode(message.get_content_charset() or "utf-8", errors="replace").strip()


def _truncate(text: str, max_chars: int = 1500) -> str:
    clean = " ".join(text.split())
    if len(clean) <= max_chars:
        return clean
    return clean[: max_chars - 3] + "..."


def _xoauth2_raw(username: str, access_token: str) -> bytes:
    payload = f"user={username}\x01auth=Bearer {access_token}\x01\x01"
    return payload.encode("utf-8")


def _access_token(config: EmailConfig) -> str:
    return config.token_store.get_access_token_silent()


def fetch_recent_messages(config: EmailConfig, limit: int = 10) -> list[EmailMessageSummary]:
    config.validate_inbound()
    summaries: list[EmailMessageSummary] = []
    with imaplib.IMAP4_SSL(config.imap_host, config.imap_port) as mailbox:
        access_token = _access_token(config)
        mailbox.authenticate("XOAUTH2", lambda _: _xoauth2_raw(config.imap_username, access_token))
        mailbox.select(config.mailbox)
        status, data = mailbox.search(None, "ALL")
        if status != "OK" or not data or not data[0]:
            return []
        message_ids = data[0].split()[-limit:]
        for uid in message_ids:
            status, message_data = mailbox.fetch(uid, "(RFC822)")
            if status != "OK" or not message_data:
                continue
            raw_message = message_data[0][1]
            parsed = message_from_bytes(raw_message)
            body = _extract_text_body(parsed)
            summaries.append(
                EmailMessageSummary(
                    uid=uid.decode("utf-8", errors="replace"),
                    from_address=parseaddr(_decode_header_value(parsed.get("From")))[1] or _decode_header_value(parsed.get("From")),
                    subject=_decode_header_value(parsed.get("Subject")),
                    date=_decode_header_value(parsed.get("Date")),
                    snippet=_truncate(body),
                    body=body,
                )
            )
    return summaries


def build_reply_message(
    config: EmailConfig,
    to_address: str,
    subject: str,
    body_text: str,
    cc_addresses: Iterable[str] | None = None,
) -> EmailMessage:
    message = EmailMessage()
    message["From"] = config.address
    message["To"] = to_address
    if cc_addresses:
        cc_list = [address for address in cc_addresses if address]
        if cc_list:
            message["Cc"] = ", ".join(cc_list)
    message["Subject"] = subject
    message.set_content(body_text)
    return message


def send_email(config: EmailConfig, message: EmailMessage) -> None:
    config.validate_outbound()
    access_token = _access_token(config)
    with smtplib.SMTP(config.smtp_host, config.smtp_port, timeout=30) as client:
        client.ehlo()
        client.starttls()
        client.ehlo()
        import base64

        auth_string = base64.b64encode(_xoauth2_raw(config.smtp_username, access_token)).decode("ascii")
        code, response = client.docmd("AUTH", "XOAUTH2 " + auth_string)
        if code not in (235, 250):
            raise smtplib.SMTPAuthenticationError(code, response)
        recipients = []
        for header in ("To", "Cc", "Bcc"):
            value = message.get(header, "")
            if value:
                recipients.extend(part.strip() for part in value.split(",") if part.strip())
        client.send_message(message, from_addr=config.address, to_addrs=recipients or None)
