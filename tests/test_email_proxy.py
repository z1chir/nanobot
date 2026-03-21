"""Tests for email channel proxy configuration and proxied socket creation."""

import socket
import ssl
from email.message import EmailMessage

import pytest

from nanobot.bus.events import OutboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.channels.email import EmailChannel, EmailConfig
from nanobot.utils.proxy import create_proxy_socket


# ---------------------------------------------------------------------------
# Config defaults include proxy fields
# ---------------------------------------------------------------------------


def test_email_config_defaults_have_proxy_fields() -> None:
    cfg = EmailConfig()
    assert cfg.imap_proxy is None
    assert cfg.smtp_proxy is None


def test_email_config_accepts_proxy_urls() -> None:
    cfg = EmailConfig(
        enabled=True,
        consent_granted=True,
        imap_host="imap.example.com",
        imap_username="bot@example.com",
        imap_password="secret",
        smtp_host="smtp.example.com",
        smtp_username="bot@example.com",
        smtp_password="secret",
        imap_proxy="socks5://127.0.0.1:1080",
        smtp_proxy="http://proxy.local:8080",
    )
    assert cfg.imap_proxy == "socks5://127.0.0.1:1080"
    assert cfg.smtp_proxy == "http://proxy.local:8080"


# ---------------------------------------------------------------------------
# Proxy socket helper
# ---------------------------------------------------------------------------


def test_create_proxy_socket_rejects_invalid_scheme() -> None:
    with pytest.raises(ValueError, match="Unsupported proxy scheme"):
        create_proxy_socket("ftp://127.0.0.1:21", "example.com", 993)


def test_create_proxy_socket_rejects_missing_port() -> None:
    with pytest.raises(ValueError, match="Invalid proxy URL"):
        create_proxy_socket("socks5://127.0.0.1", "example.com", 993)


def test_create_proxy_socket_rejects_empty_host() -> None:
    with pytest.raises(ValueError, match="Invalid proxy URL"):
        create_proxy_socket("socks5://:1080", "example.com", 993)


# ---------------------------------------------------------------------------
# IMAP proxy integration — subclass plumbing
# ---------------------------------------------------------------------------


def _make_config(
    *,
    imap_proxy: str | None = None,
    smtp_proxy: str | None = None,
    imap_use_ssl: bool = True,
) -> EmailConfig:
    return EmailConfig(
        enabled=True,
        consent_granted=True,
        imap_host="imap.example.com",
        imap_port=993,
        imap_username="bot@example.com",
        imap_password="secret",
        imap_use_ssl=imap_use_ssl,
        smtp_host="smtp.example.com",
        smtp_port=587,
        smtp_username="bot@example.com",
        smtp_password="secret",
        imap_proxy=imap_proxy,
        smtp_proxy=smtp_proxy,
    )


def _make_raw_email(
    from_addr: str = "alice@example.com",
    subject: str = "Hello",
    body: str = "This is the body.",
) -> bytes:
    msg = EmailMessage()
    msg["From"] = from_addr
    msg["To"] = "bot@example.com"
    msg["Subject"] = subject
    msg["Message-ID"] = "<m1@example.com>"
    msg.set_content(body)
    return msg.as_bytes()


def test_fetch_uses_proxied_imap4_ssl(monkeypatch) -> None:
    """When imap_proxy is set and imap_use_ssl is True, _ProxiedIMAP4SSL is used."""
    raw = _make_raw_email()

    created: list[dict] = []

    class FakeProxiedIMAP4SSL:
        def __init__(self, host, port, proxy_url):
            self.host = host
            self.port = port
            self.proxy_url = proxy_url
            self.store_calls: list[tuple] = []
            created.append({"host": host, "port": port, "proxy": proxy_url})

        def login(self, _user, _pw):
            return "OK", [b""]

        def select(self, _mailbox):
            return "OK", [b"1"]

        def search(self, *_args):
            return "OK", [b"1"]

        def fetch(self, _id, _parts):
            return "OK", [(b"1 (UID 100 BODY[] {200})", raw), b")"]

        def store(self, imap_id, op, flags):
            self.store_calls.append((imap_id, op, flags))
            return "OK", [b""]

        def logout(self):
            return "BYE", [b""]

    monkeypatch.setattr(
        "nanobot.channels.email._ProxiedIMAP4SSL",
        FakeProxiedIMAP4SSL,
    )

    cfg = _make_config(imap_proxy="socks5://127.0.0.1:1080")
    channel = EmailChannel(cfg, MessageBus())
    items = channel._fetch_new_messages()

    assert len(created) == 1
    assert created[0]["proxy"] == "socks5://127.0.0.1:1080"
    assert len(items) == 1
    assert items[0]["sender"] == "alice@example.com"


def test_fetch_uses_proxied_imap4_plain(monkeypatch) -> None:
    """When imap_proxy is set and imap_use_ssl is False, _ProxiedIMAP4 is used."""
    raw = _make_raw_email()

    created: list[dict] = []

    class FakeProxiedIMAP4:
        def __init__(self, host, port, proxy_url):
            self.host = host
            self.port = port
            self.proxy_url = proxy_url
            created.append({"host": host, "port": port, "proxy": proxy_url})

        def login(self, _user, _pw):
            return "OK", [b""]

        def select(self, _mailbox):
            return "OK", [b"1"]

        def search(self, *_args):
            return "OK", [b"1"]

        def fetch(self, _id, _parts):
            return "OK", [(b"1 (UID 200 BODY[] {200})", raw), b")"]

        def store(self, imap_id, op, flags):
            return "OK", [b""]

        def logout(self):
            return "BYE", [b""]

    monkeypatch.setattr(
        "nanobot.channels.email._ProxiedIMAP4",
        FakeProxiedIMAP4,
    )

    cfg = _make_config(imap_proxy="http://proxy.local:3128", imap_use_ssl=False)
    channel = EmailChannel(cfg, MessageBus())
    items = channel._fetch_new_messages()

    assert len(created) == 1
    assert created[0]["proxy"] == "http://proxy.local:3128"
    assert len(items) == 1


def test_fetch_without_proxy_uses_standard_imap(monkeypatch) -> None:
    """When imap_proxy is None, standard imaplib classes are used."""
    raw = _make_raw_email()

    created: list[str] = []

    class FakeIMAP4SSL:
        def __init__(self, host, port):
            created.append("standard")
            self.host = host
            self.port = port

        def login(self, _user, _pw):
            return "OK", [b""]

        def select(self, _mailbox):
            return "OK", [b"1"]

        def search(self, *_args):
            return "OK", [b"1"]

        def fetch(self, _id, _parts):
            return "OK", [(b"1 (UID 300 BODY[] {200})", raw), b")"]

        def store(self, imap_id, op, flags):
            return "OK", [b""]

        def logout(self):
            return "BYE", [b""]

    monkeypatch.setattr("nanobot.channels.email.imaplib.IMAP4_SSL", FakeIMAP4SSL)

    cfg = _make_config(imap_proxy=None)
    channel = EmailChannel(cfg, MessageBus())
    items = channel._fetch_new_messages()

    assert created == ["standard"]
    assert len(items) == 1


# ---------------------------------------------------------------------------
# SMTP proxy integration
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_send_uses_proxied_smtp_ssl(monkeypatch) -> None:
    """When smtp_proxy is set and smtp_use_ssl is True, _ProxiedSMTP_SSL is used."""
    created: list[dict] = []

    class FakeProxiedSMTP_SSL:
        def __init__(self, host, port, proxy_url, timeout=30):
            created.append({"cls": "ssl", "proxy": proxy_url, "timeout": timeout})
            self.sent_messages: list[EmailMessage] = []

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def login(self, _user, _pw):
            pass

        def send_message(self, msg):
            self.sent_messages.append(msg)

    monkeypatch.setattr("nanobot.channels.email._ProxiedSMTP_SSL", FakeProxiedSMTP_SSL)

    cfg = _make_config(smtp_proxy="socks5://127.0.0.1:1080")
    cfg.smtp_use_ssl = True
    channel = EmailChannel(cfg, MessageBus())

    await channel.send(
        OutboundMessage(channel="email", chat_id="alice@example.com", content="Hi"),
    )

    assert len(created) == 1
    assert created[0]["cls"] == "ssl"
    assert created[0]["proxy"] == "socks5://127.0.0.1:1080"


@pytest.mark.asyncio
async def test_send_uses_proxied_smtp_plain(monkeypatch) -> None:
    """When smtp_proxy is set and smtp_use_ssl is False, _ProxiedSMTP is used."""
    created: list[dict] = []

    class FakeProxiedSMTP:
        def __init__(self, host, port, proxy_url, timeout=30):
            created.append({"cls": "plain", "proxy": proxy_url})
            self.started_tls = False
            self.sent_messages: list[EmailMessage] = []

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def starttls(self, context=None):
            self.started_tls = True

        def login(self, _user, _pw):
            pass

        def send_message(self, msg):
            self.sent_messages.append(msg)

    monkeypatch.setattr("nanobot.channels.email._ProxiedSMTP", FakeProxiedSMTP)

    cfg = _make_config(smtp_proxy="http://proxy.local:8080")
    cfg.smtp_use_ssl = False
    cfg.smtp_use_tls = True
    channel = EmailChannel(cfg, MessageBus())

    await channel.send(
        OutboundMessage(channel="email", chat_id="bob@example.com", content="Hello"),
    )

    assert len(created) == 1
    assert created[0]["cls"] == "plain"
    assert created[0]["proxy"] == "http://proxy.local:8080"


@pytest.mark.asyncio
async def test_send_without_proxy_uses_standard_smtp(monkeypatch) -> None:
    """When smtp_proxy is None, standard smtplib classes are used."""
    created: list[str] = []

    class FakeSMTP:
        def __init__(self, host, port, timeout=30):
            created.append("standard")
            self.sent_messages: list[EmailMessage] = []

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def starttls(self, context=None):
            pass

        def login(self, _user, _pw):
            pass

        def send_message(self, msg):
            self.sent_messages.append(msg)

    monkeypatch.setattr("nanobot.channels.email.smtplib.SMTP", FakeSMTP)

    cfg = _make_config(smtp_proxy=None)
    channel = EmailChannel(cfg, MessageBus())

    await channel.send(
        OutboundMessage(channel="email", chat_id="carol@example.com", content="Test"),
    )

    assert created == ["standard"]
