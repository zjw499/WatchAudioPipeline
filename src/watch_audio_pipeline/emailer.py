from email.message import EmailMessage
import logging
import smtplib


email_logger = logging.getLogger("email")


class SmtpEmailClient:
    def __init__(
        self,
        *,
        host: str,
        port: int,
        username: str,
        password: str,
        from_address: str,
        to_address: str,
    ) -> None:
        self.host = host
        self.port = port
        self.username = username
        self.password = password
        self.from_address = from_address
        self.to_address = to_address

    def send_text(self, subject: str, body: str, to_address: str | None = None) -> None:
        message = EmailMessage()
        message["From"] = self.from_address
        recipients = []
        seen = set()
        for value in (self.to_address, to_address or ""):
            for raw_address in value.replace(";", ",").split(","):
                address = raw_address.strip()
                key = address.casefold()
                if address and key not in seen:
                    recipients.append(address)
                    seen.add(key)
        if not recipients:
            raise ValueError("at least one SMTP recipient is required")
        message["To"] = ", ".join(recipients)
        message["Subject"] = subject
        message.set_content(body)

        with smtplib.SMTP(self.host, self.port, timeout=30) as smtp:
            smtp.starttls()
            if self.username:
                smtp.login(self.username, self.password)
            refused = smtp.send_message(message)
            if refused:
                raise RuntimeError(
                    "SMTP rejected recipient(s): " + ", ".join(sorted(refused))
                )
        email_logger.info("smtp sent subject=%s recipients=%s", subject, len(recipients))


def build_subject(job_id: str, title: str | None = None, prefix: str = "") -> str:
    subject = title.strip() if title and title.strip() else f"Transcript ready ({job_id[:8]})"
    return f"{prefix.strip()} {subject}".strip()


def build_memo_email(
    *,
    title: str,
    summary: str | None,
    transcript: str,
    remove_footer: bool = False,
) -> str:
    sections = [title.strip()]
    if summary and summary.strip():
        sections.extend(["Summary", summary.strip()])
    sections.extend(["Transcript", transcript.strip()])
    if not remove_footer:
        sections.extend(["", "Generated locally by Watch Audio Pipeline."])
    return "\n\n".join(section for section in sections if section is not None)
