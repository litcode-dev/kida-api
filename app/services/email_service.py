import asyncio
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import structlog

from app.config import get_settings

log = structlog.get_logger()


async def send_email(to: str, subject: str, html: str) -> None:
    """Send a transactional email via SMTP. Silently skips when SMTP is not configured."""
    settings = get_settings()
    if not settings.smtp_user or not settings.smtp_password:
        log.warning("email.skipped", reason="SMTP not configured", to=to, subject=subject)
        return

    def _send():
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = settings.smtp_from
        msg["To"] = to
        msg.attach(MIMEText(html, "html"))
        with smtplib.SMTP(settings.smtp_host, settings.smtp_port) as smtp:
            smtp.ehlo()
            smtp.starttls()
            smtp.login(settings.smtp_user, settings.smtp_password)
            smtp.sendmail(settings.smtp_from, to, msg.as_string())

    try:
        await asyncio.to_thread(_send)
        log.info("email.sent", to=to, subject=subject)
    except Exception as exc:
        log.error("email.failed", to=to, subject=subject, error=str(exc))


# ── Templates ─────────────────────────────────────────────────────────────────

def registration_html(full_name: str) -> str:
    return f"""<!DOCTYPE html>
<html lang="en">
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0;padding:0;background:#e8e3d9;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;">
<table width="100%" cellpadding="0" cellspacing="0" style="background:#e8e3d9;">
  <tr><td align="center" style="padding:32px 16px;">
    <table width="520" cellpadding="0" cellspacing="0" style="max-width:520px;width:100%;">

      <!-- HEADER -->
      <tr><td style="background:#0a0a0a;padding:24px 32px 0 32px;border-radius:8px 8px 0 0;">
        <table width="100%" cellpadding="0" cellspacing="0">
          <tr>
            <td style="color:#fff;font-size:11px;font-weight:700;letter-spacing:0.12em;text-transform:uppercase;">KIDA</td>
            <td align="right" style="color:#555;font-size:11px;letter-spacing:0.08em;text-transform:uppercase;">WELCOME</td>
          </tr>
        </table>
      </td></tr>

      <!-- HERO -->
      <tr><td style="background:#0a0a0a;padding:32px 32px 8px 32px;">
        <p style="margin:0 0 16px 0;color:#6c3bdb;font-size:12px;font-weight:800;letter-spacing:0.15em;text-transform:uppercase;">YOU'RE IN.</p>
        <p style="margin:0;font-size:52px;font-weight:800;line-height:1.05;color:#fff;letter-spacing:-0.02em;">Make your</p>
        <p style="margin:0;font-size:52px;font-weight:800;line-height:1.05;color:#6c3bdb;font-style:italic;letter-spacing:-0.02em;">sound.</p>
      </td></tr>

      <!-- META STRIP -->
      <tr><td style="background:#0a0a0a;padding:24px 32px 28px 32px;border-bottom:1px solid #1f1f1f;">
        <p style="margin:0;color:#444;font-size:11px;letter-spacing:0.12em;text-transform:uppercase;">2026 &nbsp;&middot;&nbsp; PREMIUM LOOPS &nbsp;&middot;&nbsp; STEM PACKS</p>
      </td></tr>

      <!-- BODY -->
      <tr><td style="background:#f2ede4;padding:36px 32px 28px 32px;">
        <p style="margin:0 0 8px 0;font-size:17px;font-weight:700;color:#0a0a0a;">Hi {full_name},</p>
        <p style="margin:0 0 28px 0;font-size:15px;line-height:1.6;color:#333;">
          Your kida account is ready. Browse premium loops and stem packs built for serious producers — no subscriptions, just the sounds you need.
        </p>
        <a href="https://kida.litcode.com.ng"
           style="display:inline-block;padding:14px 28px;background:#0a0a0a;color:#fff;
                  font-size:14px;font-weight:700;text-decoration:none;border-radius:4px;
                  letter-spacing:0.02em;">
          Browse Loops &rarr;
        </a>
      </td></tr>

      <!-- STATS -->
      <tr><td style="background:#f2ede4;padding:28px 32px;border-top:1px solid #ddd8ce;">
        <table width="100%" cellpadding="0" cellspacing="0">
          <tr>
            <td style="text-align:left;">
              <p style="margin:0;font-size:28px;font-weight:800;color:#0a0a0a;">500+</p>
              <p style="margin:4px 0 0 0;font-size:11px;letter-spacing:0.1em;text-transform:uppercase;color:#888;">Loops</p>
            </td>
            <td style="text-align:center;">
              <p style="margin:0;font-size:28px;font-weight:800;color:#0a0a0a;">10</p>
              <p style="margin:4px 0 0 0;font-size:11px;letter-spacing:0.1em;text-transform:uppercase;color:#888;">Genres</p>
            </td>
            <td style="text-align:right;">
              <p style="margin:0;font-size:28px;font-weight:800;color:#0a0a0a;">0</p>
              <p style="margin:4px 0 0 0;font-size:11px;letter-spacing:0.1em;text-transform:uppercase;color:#888;">Subscriptions</p>
            </td>
          </tr>
        </table>
      </td></tr>

      <!-- FOOTER -->
      <tr><td style="background:#0a0a0a;padding:24px 32px;border-radius:0 0 8px 8px;">
        <table width="100%" cellpadding="0" cellspacing="0">
          <tr>
            <td style="width:36px;vertical-align:middle;">
              <div style="width:32px;height:32px;background:#6c3bdb;border-radius:4px;
                          text-align:center;line-height:32px;color:#fff;
                          font-size:12px;font-weight:800;letter-spacing:0.05em;">LM</div>
            </td>
            <td style="padding-left:12px;vertical-align:middle;">
              <p style="margin:0;font-size:11px;font-weight:700;color:#fff;letter-spacing:0.1em;text-transform:uppercase;">KIDA</p>
              <p style="margin:2px 0 0 0;font-size:11px;color:#555;">Professional Music Production Samples</p>
            </td>
          </tr>
        </table>
      </td></tr>

    </table>
  </td></tr>
</table>
</body>
</html>"""


def purchase_html(full_name: str, product_title: str, product_type: str, amount: str) -> str:
    return f"""<!DOCTYPE html>
<html>
<body style="font-family:sans-serif;max-width:600px;margin:auto;padding:24px;color:#1a1a1a;">
  <h1 style="color:#6c3bdb;">Purchase Confirmed ✓</h1>
  <p>Hi {full_name}, thanks for your purchase on Kida!</p>
  <table style="width:100%;border-collapse:collapse;margin:20px 0;border-radius:8px;overflow:hidden;">
    <tr style="background:#f5f0ff;">
      <td style="padding:12px 16px;font-weight:bold;width:40%;">Item</td>
      <td style="padding:12px 16px;">{product_title}</td>
    </tr>
    <tr>
      <td style="padding:12px 16px;font-weight:bold;">Type</td>
      <td style="padding:12px 16px;">{product_type}</td>
    </tr>
    <tr style="background:#f5f0ff;">
      <td style="padding:12px 16px;font-weight:bold;">Amount Paid</td>
      <td style="padding:12px 16px;">${amount}</td>
    </tr>
  </table>
  <p>Your purchase is available in your library immediately.</p>
  <a href="https://kida.litcode.com.ng"
     style="display:inline-block;margin-top:8px;padding:12px 28px;background:#6c3bdb;
            color:#fff;border-radius:6px;text-decoration:none;font-weight:bold;">
    Go to Library
  </a>
  <p style="margin-top:40px;color:#999;font-size:12px;">
    Kida &mdash; Professional Music Production Samples
  </p>
</body>
</html>"""
