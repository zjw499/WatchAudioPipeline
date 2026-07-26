import pytest

from watch_audio_pipeline import emailer
from watch_audio_pipeline.emailer import SmtpEmailClient


class FakeSMTP:
    def __init__(self, refused=None):
        self.refused = refused or {}
        self.message = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def starttls(self):
        return None

    def login(self, username, password):
        return None

    def send_message(self, message):
        self.message = message
        return self.refused


def build_client():
    return SmtpEmailClient(
        host="smtp.example.com",
        port=587,
        username="sender@example.com",
        password="secret",
        from_address="sender@example.com",
        to_address="required@example.com, second@example.com",
    )


def test_configured_recipients_cannot_be_replaced_by_memo_override(monkeypatch):
    smtp = FakeSMTP()
    monkeypatch.setattr(emailer.smtplib, "SMTP", lambda *args, **kwargs: smtp)

    build_client().send_text(
        "Transcript",
        "Body",
        "custom@example.com; REQUIRED@example.com",
    )

    assert smtp.message["To"] == (
        "required@example.com, second@example.com, custom@example.com"
    )


def test_any_rejected_recipient_fails_delivery(monkeypatch):
    smtp = FakeSMTP({"required@example.com": (550, b"rejected")})
    monkeypatch.setattr(emailer.smtplib, "SMTP", lambda *args, **kwargs: smtp)

    with pytest.raises(RuntimeError, match="required@example.com"):
        build_client().send_text("Transcript", "Body")


def test_exact_recipient_bypasses_legacy_default_recipients(monkeypatch):
    smtp = FakeSMTP()
    monkeypatch.setattr(emailer.smtplib, "SMTP", lambda *args, **kwargs: smtp)

    build_client().send_text_exact("Transcript", "Body", "tester@example.com")

    assert smtp.message["To"] == "tester@example.com"
