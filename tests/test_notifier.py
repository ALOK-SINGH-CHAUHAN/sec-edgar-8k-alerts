"""Unit tests for notifier.py formatting, timezone conversion, and quiet-hours."""

from datetime import datetime, timezone
import pytest
import respx
import httpx

from config import AlertsConfig, AppConfig, QuietHoursConfig, SECConfig
from notifier import (
    AlertMessage,
    Notifier,
    calculate_latency_seconds,
    is_in_quiet_hours,
    parse_acceptance_datetime,
)


def test_parse_acceptance_datetime_and_timezone_rendering():
    # ISO string
    dt = parse_acceptance_datetime("2026-10-16T16:30:12.000Z")
    assert dt is not None
    assert dt.year == 2026
    assert dt.month == 10
    assert dt.day == 16

    # 14-digit format 'YYYYMMDDHHMMSS'
    dt2 = parse_acceptance_datetime("20261016163012")
    assert dt2 is not None
    assert dt2.hour == 16
    assert dt2.minute == 30
    assert dt2.second == 12


def test_calculate_latency():
    acc_dt = datetime(2026, 10, 16, 16, 30, 0, tzinfo=timezone.utc)
    now_dt = datetime(2026, 10, 16, 16, 30, 5, 500000, tzinfo=timezone.utc)

    # 5.5 seconds latency
    assert calculate_latency_seconds(acc_dt, now_dt) == 5.5

    # Negative latency due to clock skew clamped to 0.0s
    earlier_now = datetime(2026, 10, 16, 16, 29, 50, tzinfo=timezone.utc)
    assert calculate_latency_seconds(acc_dt, earlier_now) == 0.0


def test_alert_formatting_high_priority():
    alert = AlertMessage(
        tickers=["AAPL"],
        cik=320193,
        company_name="Apple Inc.",
        form="8-K",
        items_raw="2.02,9.01",
        acceptance_dt_str="2026-10-16T16:30:12.000Z",
        accession_number="0000320193-26-000001",
        preview="On October 16, 2026, Apple announced earnings.",
    )

    text = alert.render_text()
    assert "🚨 HIGH PRIORITY" in text
    assert "AAPL" in text
    assert "Apple Inc." in text
    assert "2.02 Results of Operations" in text
    assert "9.01 Financial Statements" in text
    assert "Accepted:" in text
    assert "ET" in text
    assert "IST" in text
    assert "Preview: On October 16" in text
    assert "https://www.sec.gov/Archives/edgar/data/320193" in text


def test_alert_html_escaping():
    alert = AlertMessage(
        tickers=["TEST"],
        cik=12345,
        company_name="A & B <Holdings> Inc.",
        form="8-K",
        items_raw="8.01",
        acceptance_dt_str="2026-10-16T16:30:12.000Z",
        accession_number="0000012345-26-000001",
        preview="<script>alert('hack')</script> & more",
    )

    html_out = alert.render_telegram_html()
    assert "A &amp; B &lt;Holdings&gt; Inc." in html_out
    assert "&lt;script&gt;alert(&#x27;hack&#x27;)&lt;/script&gt; &amp; more" in html_out
    # Not high priority since 8.01 only
    assert "🚨 <b>HIGH PRIORITY</b>" not in html_out


def test_quiet_hours_detection():
    # Spanning midnight: 22:00 to 08:00 IST
    cfg = QuietHoursConfig(
        enabled=True,
        start="22:00",
        end="08:00",
        timezone="Asia/Kolkata",
    )

    # 23:30 IST is during quiet hours
    dt_quiet = datetime(2026, 10, 16, 23, 30, tzinfo=timezone.utc)
    # in IST (UTC+5:30), UTC 23:30 is next day 05:00 IST -> inside quiet hours
    assert is_in_quiet_hours(cfg, dt_quiet) is True

    # UTC 09:00 is 14:30 IST -> daytime outside quiet hours
    dt_day = datetime(2026, 10, 16, 9, 0, tzinfo=timezone.utc)
    assert is_in_quiet_hours(cfg, dt_day) is False


@pytest.mark.asyncio
@respx.mock
async def test_telegram_send_with_retry(tmp_path):
    config = AppConfig(
        sec={"user_agent": "Test/1.0 (tester@domain.com)"},
        alerts={
            "channels": ["telegram"],
            "telegram": {"bot_token": "fake_token", "chat_id": "12345"},
        },
    )

    url = "https://api.telegram.org/botfake_token/sendMessage"
    route = respx.post(url)
    route.side_effect = [
        httpx.Response(429, json={"parameters": {"retry_after": 1}}),
        httpx.Response(200, json={"ok": True}),
    ]

    notifier = Notifier(config)
    alert = AlertMessage(
        tickers=["AAPL"],
        cik=320193,
        company_name="Apple Inc.",
        form="8-K",
        items_raw="2.02",
        acceptance_dt_str="2026-10-16T16:30:12.000Z",
        accession_number="0000320193-26-000001",
    )

    res = await notifier.send_telegram(alert)
    assert res is True
    assert route.call_count == 2
    await notifier.close()
