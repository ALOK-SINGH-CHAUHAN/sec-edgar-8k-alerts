"""Alert formatting and delivery channels (Telegram, Email, Console) with quiet-hours."""

import asyncio
from datetime import datetime, time as dtime, timezone
import html
import logging
import smtplib
from email.mime.text import MIMEText
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo
import httpx

from config import AlertsConfig, AppConfig, QuietHoursConfig
from edgar_client import build_filing_index_url
from items import (
    ParsedItem,
    get_form_description,
    is_high_priority_filing,
    parse_items,
)

logger = logging.getLogger(__name__)

ET_TZ = ZoneInfo("America/New_York")
IST_TZ = ZoneInfo("Asia/Kolkata")
UTC_TZ = timezone.utc


def parse_acceptance_datetime(dt_str: str) -> Optional[datetime]:
    """Parse acceptance datetime from SEC submissions string."""
    if not dt_str:
        return None
    cleaned = dt_str.strip()
    # SEC formats typically:
    # '2026-10-16T16:30:12.000Z'
    # '20261016163012'
    # '2026-10-16 16:30:12'
    try:
        if "T" in cleaned:
            dt = datetime.fromisoformat(cleaned.replace("Z", "+00:00"))
        elif len(cleaned) == 14 and cleaned.isdigit():
            dt = datetime.strptime(cleaned, "%Y%m%d%H%M%S").replace(tzinfo=UTC_TZ)
        else:
            dt = datetime.fromisoformat(cleaned)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=UTC_TZ)
        return dt.astimezone(UTC_TZ)
    except Exception as e:
        logger.debug("Failed parsing acceptance date '%s': %s", dt_str, e)
        return None


def calculate_latency_seconds(acceptance_dt: Optional[datetime], now_dt: Optional[datetime] = None) -> float:
    """
    Calculate latency in seconds between acceptance time and alert delivery.
    Clamps negative values (clock skew) to 0.0s and logs a warning.
    """
    if not acceptance_dt:
        return 0.0

    now = now_dt or datetime.now(UTC_TZ)
    delta = (now - acceptance_dt).total_seconds()

    if delta < 0:
        logger.warning(
            "Negative latency detected (%.2fs), likely local or server clock skew. Clamping to 0.0s.",
            delta,
        )
        return 0.0

    if delta > 60.0:
        logger.warning(
            "High latency warning: filing acceptance-to-alert latency is %.1fs (>60s)",
            delta,
        )

    return round(delta, 2)


def is_in_quiet_hours(quiet_cfg: QuietHoursConfig, check_dt: Optional[datetime] = None) -> bool:
    """
    Check if the given time falls within configured quiet hours.
    Correctly handles intervals that cross midnight (e.g. 22:00 to 08:00).
    """
    if not quiet_cfg.enabled:
        return False

    tz = ZoneInfo(quiet_cfg.timezone)
    now = (check_dt or datetime.now(timezone.utc)).astimezone(tz)
    current_time = now.time()

    start_parts = [int(p) for p in quiet_cfg.start.split(":")]
    end_parts = [int(p) for p in quiet_cfg.end.split(":")]
    start_time = dtime(start_parts[0], start_parts[1])
    end_time = dtime(end_parts[0], end_parts[1])

    if start_time < end_time:
        # Standard window within same day (e.g. 01:00 to 06:00)
        return start_time <= current_time < end_time
    else:
        # Window spanning midnight (e.g. 22:00 to 08:00)
        return current_time >= start_time or current_time < end_time


class AlertMessage:
    """Represents a formatted filing alert."""

    def __init__(
        self,
        tickers: List[str],
        cik: int,
        company_name: str,
        form: str,
        items_raw: str,
        acceptance_dt_str: str,
        accession_number: str,
        preview: Optional[str] = None,
        is_backfill: bool = False,
    ):
        self.tickers = tickers
        self.cik = cik
        self.company_name = company_name
        self.form = form
        self.items_raw = items_raw
        self.accession_number = accession_number
        self.preview = preview
        self.is_backfill = is_backfill

        self.parsed_items = parse_items(items_raw)
        self.is_high_priority = is_high_priority_filing(form, self.parsed_items)

        self.acceptance_dt = parse_acceptance_datetime(acceptance_dt_str)
        self.latency_seconds = calculate_latency_seconds(self.acceptance_dt)
        self.filing_url = build_filing_index_url(cik, accession_number)

    def render_text(self) -> str:
        """Render plain-text alert for console or email."""
        banner = "🚨 HIGH PRIORITY — " if self.is_high_priority else "📄 "
        if self.is_backfill:
            banner = "[BACKFILL] " + banner

        ticker_display = " / ".join(self.tickers) if self.tickers else f"CIK {self.cik}"
        header = f"{banner}{ticker_display} ({self.company_name}) Form {self.form}"

        lines = [header]

        if self.form.startswith("8-K"):
            if self.parsed_items:
                for item in self.parsed_items:
                    lines.append(f"• {item.code} {item.description}")
            else:
                lines.append("• (No item codes specified)")
        else:
            lines.append(f"• {get_form_description(self.form)}")

        if self.acceptance_dt:
            et_str = self.acceptance_dt.astimezone(ET_TZ).strftime("%Y-%m-%d %H:%M:%S ET")
            ist_str = self.acceptance_dt.astimezone(IST_TZ).strftime("%Y-%m-%d %H:%M:%S IST")
            lines.append(f"Accepted: {et_str} / {ist_str}")
        lines.append(f"Latency: {self.latency_seconds}s")
        lines.append(f"Filing index: {self.filing_url}")

        if self.preview:
            lines.append(f"Preview: {self.preview}")

        return "\n".join(lines)

    def render_telegram_html(self) -> str:
        """
        Render Telegram HTML formatted message.
        Dynamic values are HTML-escaped to prevent parsing errors.
        """
        banner = "🚨 <b>HIGH PRIORITY</b> — " if self.is_high_priority else "📄 "
        if self.is_backfill:
            banner = "<b>[BACKFILL]</b> " + banner

        ticker_display = " / ".join(self.tickers) if self.tickers else f"CIK {self.cik}"
        company_esc = html.escape(self.company_name)
        form_esc = html.escape(self.form)
        header = f"{banner}<b>{html.escape(ticker_display)}</b> ({company_esc}) Form <b>{form_esc}</b>"

        lines = [header]

        if self.form.startswith("8-K"):
            if self.parsed_items:
                for item in self.parsed_items:
                    code_esc = html.escape(item.code)
                    desc_esc = html.escape(item.description)
                    if item.is_high_priority:
                        lines.append(f"• <b>{code_esc} {desc_esc}</b>")
                    else:
                        lines.append(f"• {code_esc} {desc_esc}")
            else:
                lines.append("• <i>(No item codes specified)</i>")
        else:
            lines.append(f"• {html.escape(get_form_description(self.form))}")

        if self.acceptance_dt:
            et_str = self.acceptance_dt.astimezone(ET_TZ).strftime("%Y-%m-%d %H:%M:%S ET")
            ist_str = self.acceptance_dt.astimezone(IST_TZ).strftime("%Y-%m-%d %H:%M:%S IST")
            lines.append(f"<b>Accepted:</b> {et_str} / {ist_str}")
        lines.append(f"<b>Latency:</b> {self.latency_seconds}s")
        lines.append(f'<b>Filing index:</b> <a href="{self.filing_url}">View on SEC EDGAR</a>')

        if self.preview:
            preview_esc = html.escape(self.preview)
            lines.append(f"<b>Preview:</b> <i>{preview_esc}</i>")

        return "\n".join(lines)


class Notifier:
    """Manages dispatching alerts to Telegram, Email, and Console."""

    def __init__(self, config: AppConfig, http_client: Optional[httpx.AsyncClient] = None):
        self.config = config
        self.alerts_cfg = config.alerts
        self.http_client = http_client or httpx.AsyncClient(timeout=10.0)
        self._owns_http_client = http_client is None

    async def close(self) -> None:
        if self._owns_http_client:
            await self.http_client.aclose()

    async def send_alert(self, alert: AlertMessage) -> Dict[str, bool]:
        """
        Send alert across all configured channels.
        Returns dictionary of channel name -> success boolean.
        Always falls back to console if other channels fail or are empty.
        """
        results: Dict[str, bool] = {}
        active_channels = set(self.alerts_cfg.channels)

        # 1. Telegram
        if "telegram" in active_channels:
            results["telegram"] = await self.send_telegram(alert)

        # 2. Email
        if "email" in active_channels:
            results["email"] = await self.send_email(alert)

        # 3. Console fallback
        # Always output to console if requested, or if all active external channels failed
        console_requested = "console" in active_channels
        external_failed = bool(results) and not any(results.values())
        if console_requested or external_failed or not active_channels:
            results["console"] = self.send_console(alert)

        return results

    async def send_telegram(self, alert: AlertMessage, max_retries: int = 3) -> bool:
        """
        Send alert message via Telegram Bot API with HTML parse mode,
        handling 4096-character limit and 429 rate limit retries.
        """
        token = self.alerts_cfg.telegram.bot_token
        chat_id = self.alerts_cfg.telegram.chat_id

        if not token or not chat_id:
            logger.warning("Telegram alert skipped: bot_token or chat_id not configured.")
            return False

        url = f"https://api.telegram.org/bot{token}/sendMessage"
        text = alert.render_telegram_html()

        # Enforce Telegram 4096 character limit
        if len(text) > 4090:
            text = text[:4080] + "\n...</i>"

        payload = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": False,
        }

        attempt = 0
        while attempt <= max_retries:
            try:
                resp = await self.http_client.post(url, json=payload)
                if resp.status_code == 200:
                    logger.info("Telegram alert sent successfully for %s", alert.accession_number)
                    return True

                if resp.status_code == 429:
                    attempt += 1
                    err_json = resp.json()
                    retry_after = err_json.get("parameters", {}).get("retry_after", 2)
                    logger.warning("Telegram rate limited (429). Retrying in %ds...", retry_after)
                    await asyncio.sleep(retry_after)
                    continue

                logger.error(
                    "Telegram sendMessage failed (HTTP %d): %s", resp.status_code, resp.text
                )
                return False

            except Exception as e:
                attempt += 1
                logger.warning("Telegram send attempt %d error: %s", attempt, e)
                if attempt > max_retries:
                    logger.error("Telegram send failed after %d retries: %s", max_retries, e)
                    return False
                await asyncio.sleep(1.0 * attempt)

        return False

    async def send_email(self, alert: AlertMessage) -> bool:
        """Send alert email via SMTP in non-blocking thread."""
        email_cfg = self.alerts_cfg.email
        if not email_cfg.enabled or not email_cfg.to_email or not email_cfg.smtp_host:
            logger.warning("Email alert skipped: SMTP configuration incomplete.")
            return False

        subject = f"{'🚨 HIGH PRIORITY: ' if alert.is_high_priority else ''}SEC 8-K Alert: {', '.join(alert.tickers)} ({alert.form})"
        body = alert.render_text()

        msg = MIMEText(body, "plain", "utf-8")
        msg["Subject"] = subject
        msg["From"] = email_cfg.from_email or email_cfg.smtp_user
        msg["To"] = email_cfg.to_email

        def _sync_send():
            with smtplib.SMTP(email_cfg.smtp_host, email_cfg.smtp_port, timeout=10) as server:
                server.starttls()
                if email_cfg.smtp_user and email_cfg.smtp_password:
                    server.login(email_cfg.smtp_user, email_cfg.smtp_password)
                server.send_message(msg)

        try:
            await asyncio.to_thread(_sync_send)
            logger.info("Email alert sent successfully for %s", alert.accession_number)
            return True
        except Exception as e:
            logger.error("Failed to send email alert for %s: %s", alert.accession_number, e)
            return False

    def send_console(self, alert: AlertMessage) -> bool:
        """Print alert to console/stdout."""
        border = "=" * 70
        print(f"\n{border}\n{alert.render_text()}\n{border}\n")
        return True
