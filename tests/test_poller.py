"""Unit tests for poller.py scheduler, market-hours, and seeding orchestration."""

from datetime import datetime, timezone
import pytest
from zoneinfo import ZoneInfo

from config import AppConfig, PollConfig
from edgar_client import EdgarClient
from notifier import Notifier
from poller import EdgarPoller, is_market_fast_hours
from store import FilingStore


def test_market_fast_hours():
    cfg = PollConfig(
        fast_interval_seconds=10,
        slow_interval_seconds=60,
        fast_window_start="04:00",
        fast_window_end="20:00",
        timezone="America/New_York",
        holidays=["2026-07-04"],
    )

    # 1. Friday 10:00 AM ET -> Fast
    # 2026-10-16 was a Friday. 10:00 ET = 14:00 UTC
    dt_fast = datetime(2026, 10, 16, 14, 0, tzinfo=timezone.utc)
    assert is_market_fast_hours(cfg, dt_fast) is True

    # 2. Friday 02:00 AM ET -> Slow (pre-premarket)
    dt_slow_night = datetime(2026, 10, 16, 6, 0, tzinfo=timezone.utc)
    assert is_market_fast_hours(cfg, dt_slow_night) is False

    # 3. Saturday -> Slow
    dt_saturday = datetime(2026, 10, 17, 14, 0, tzinfo=timezone.utc)
    assert is_market_fast_hours(cfg, dt_saturday) is False

    # 4. Holiday -> Slow
    dt_holiday = datetime(2026, 7, 4, 14, 0, tzinfo=timezone.utc)
    assert is_market_fast_hours(cfg, dt_holiday) is False


@pytest.mark.asyncio
async def test_cik_grouping_and_silent_seeding(tmp_path, sample_submissions_aapl_json):
    db_file = tmp_path / "poller_test.db"
    store = FilingStore(db_file)
    await store.connect()

    config = AppConfig(
        watchlist=["AAPL", "GOOG", "GOOGL"],
        sec={"user_agent": "PollerTest/1.0 (tester@domain.com)"},
        database_path=str(db_file),
    )

    client = EdgarClient(config=config.sec, cache_dir=tmp_path)
    # Manually populate client mappings
    client.ticker_to_cik = {"AAPL": 320193, "GOOG": 1652044, "GOOGL": 1652044}
    client.cik_to_tickers = {320193: {"AAPL"}, 1652044: {"GOOG", "GOOGL"}}

    notifier = Notifier(config)
    poller = EdgarPoller(config=config, store=store, client=client, notifier=notifier)

    # Verify grouping: GOOG and GOOGL map to one CIK (1652044)
    cik_map = poller.get_cik_watchlist_mapping()
    assert len(cik_map) == 2
    assert 320193 in cik_map
    assert 1652044 in cik_map
    assert set(cik_map[1652044]) == {"GOOG", "GOOGL"}

    # Mock fetch_submissions for AAPL to return sample submissions
    async def mock_fetch_submissions(cik):
        if cik == 320193:
            recent = sample_submissions_aapl_json["filings"]["recent"]
            return [
                {
                    "accession_number": recent["accessionNumber"][0],
                    "cik": 320193,
                    "form": recent["form"][0],
                    "filing_date": recent["filingDate"][0],
                    "acceptance_datetime": recent["acceptanceDateTime"][0],
                    "items": recent["items"][0],
                    "primary_document": recent["primaryDocument"][0],
                }
            ]
        return []

    client.fetch_submissions = mock_fetch_submissions

    # First poll: CIK is unseeded -> seed silently, returns 0 new filings
    assert await store.is_cik_seeded(320193) is False
    new_filings = await poller.poll_cik(320193, ["AAPL"])
    assert len(new_filings) == 0
    assert await store.is_cik_seeded(320193) is True

    # Second poll with a brand new filing: should be detected!
    async def mock_fetch_new_filing(cik):
        return [
            {
                "accession_number": "0000320193-26-999999",
                "cik": 320193,
                "form": "8-K",
                "filing_date": "2026-10-17",
                "acceptance_datetime": "2026-10-17T15:00:00.000Z",
                "items": "2.02",
                "primary_document": "aapl_new.htm",
            }
        ]

    client.fetch_submissions = mock_fetch_new_filing
    new_filings_2 = await poller.poll_cik(320193, ["AAPL"])
    assert len(new_filings_2) == 1
    assert new_filings_2[0]["accession_number"] == "0000320193-26-999999"

    # Test Atom feed backup triggering submission fetch for unseen filing
    atom_called = False

    async def mock_fetch_atom():
        return [
            {
                "accession_number": "0000320193-26-888888",
                "cik": 320193,
                "form": "8-K",
                "acceptance_datetime": "2026-10-17T16:00:00-04:00",
                "title": "8-K - Apple Inc. (0000320193)",
            }
        ]

    async def mock_poll_cik_from_atom(cik, tickers):
        nonlocal atom_called
        atom_called = True
        return []

    client.fetch_atom_feed = mock_fetch_atom
    poller.poll_cik = mock_poll_cik_from_atom
    await poller.poll_atom_feed({320193: ["AAPL"]})
    assert atom_called is True

    await client.close()
    await store.close()
    await notifier.close()

