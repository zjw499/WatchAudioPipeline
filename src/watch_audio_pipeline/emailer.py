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

    def send_text(self, subject: str, body: str) -> None:
        message = EmailMessage()
        message["From"] = self.from_address
        message["To"] = self.to_address
        message["Subject"] = subject
        message.set_content(body)

        with smtplib.SMTP(self.host, self.port, timeout=30) as smtp:
            smtp.starttls()
            if self.username:
                smtp.login(self.username, self.password)
            smtp.send_message(message)
        email_logger.info("smtp sent subject=%s", subject)


def build_subject(job_id: str) -> str:
    return f"Transcript ready ({job_id[:8]})"
