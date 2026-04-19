"""Исходящая почта по настройкам организации (раздел «Связь»)."""
from __future__ import annotations

import smtplib
import ssl
from email.message import EmailMessage
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .models import OrganizationSettings


class MailConfigurationError(ValueError):
    """Неполные или некорректные настройки SMTP."""


def send_organization_email(
    to_addrs: list[str],
    subject: str,
    body: str,
    *,
    body_html: str | None = None,
    settings: OrganizationSettings | None = None,
) -> None:
    """
    Отправить письмо через SMTP из OrganizationSettings.
    Порт 465 — implicit SSL; иначе SMTP + STARTTLS (если smtp_use_tls).
    """
    from .models import OrganizationSettings

    s = settings or OrganizationSettings.get_settings()
    host = (s.smtp_host or "").strip()
    if not host:
        raise MailConfigurationError("Не указан SMTP сервер (раздел «Связь»).")

    to_clean = [a.strip() for a in to_addrs if (a or "").strip()]
    if not to_clean:
        raise MailConfigurationError("Нет получателей.")

    from_addr = (s.smtp_from or s.email or s.smtp_user or "").strip()
    if not from_addr:
        raise MailConfigurationError(
            "Укажите «Адрес отправителя (From)» или «Email для связи», или SMTP логин."
        )

    port = int(s.smtp_port or 587)
    user = (s.smtp_user or "").strip()
    password = (s.smtp_password or "") or ""
    use_tls = bool(getattr(s, "smtp_use_tls", True))

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = from_addr
    msg["To"] = ", ".join(to_clean)
    msg.set_content(body, charset="utf-8")
    if body_html:
        msg.add_alternative(body_html, subtype="html", charset="utf-8")

    timeout = 30

    if port == 465:
        context = ssl.create_default_context()
        with smtplib.SMTP_SSL(host, port, timeout=timeout, context=context) as smtp:
            if user:
                smtp.login(user, password)
            smtp.send_message(msg)
        return

    with smtplib.SMTP(host, port, timeout=timeout) as smtp:
        if use_tls:
            context = ssl.create_default_context()
            smtp.starttls(context=context)
        if user:
            smtp.login(user, password)
        smtp.send_message(msg)
