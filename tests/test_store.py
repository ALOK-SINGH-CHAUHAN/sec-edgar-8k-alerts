"""Unit tests for SQLite storage, deduplication, seeding, and queues."""

import pytest
from store import FilingStore


@pytest.mark.asyncio
async def test_deduplication_and_persistence(tmp_path):
    db_file = tmp_path / "test_filings.db"
    store = FilingStore(db_file)
    await store.connect()

    filing = {
        "accession_number": "0000320193-26-000001",
        "cik": 320193,
        "form": "8-K",
        "filing_date": "2026-10-16",
        "acceptance_datetime": "2026-10-16T16:30:12.000Z",
        "items": "2.02,9.01",
        "primary_document": "aapl-20261016.htm",
    }

    # First insert succeeds
    is_new = await store.record_new_filing(filing, ["AAPL"])
    assert is_new is True
    assert await store.is_filing_seen("0000320193-26-000001") is True

    # Duplicate insert returns False
    is_duplicate = await store.record_new_filing(filing, ["AAPL"])
    assert is_duplicate is False

    await store.mark_filing_alerted("0000320193-26-000001", "sent")
    await store.close()

    # Reopen database and verify state persisted
    store_reopened = FilingStore(db_file)
    await store_reopened.connect()
    assert await store_reopened.is_filing_seen("0000320193-26-000001") is True

    # Inserting again still returns False
    assert await store_reopened.record_new_filing(filing, ["AAPL"]) is False
    await store_reopened.close()


@pytest.mark.asyncio
async def test_first_run_seeding(tmp_path):
    db_file = tmp_path / "test_seeding.db"
    store = FilingStore(db_file)
    await store.connect()

    cik = 320193
    assert await store.is_cik_seeded(cik) is False

    recent_filings = [
        {
            "accession_number": "0000320193-26-000001",
            "cik": cik,
            "form": "8-K",
            "filing_date": "2026-10-16",
            "acceptance_datetime": "2026-10-16T16:30:12.000Z",
            "items": "2.02,9.01",
            "primary_document": "aapl.htm",
        },
        {
            "accession_number": "0000320193-26-000002",
            "cik": cik,
            "form": "8-K",
            "filing_date": "2026-10-15",
            "acceptance_datetime": "2026-10-15T12:00:00.000Z",
            "items": "5.02",
            "primary_document": "aapl2.htm",
        },
    ]

    # Seed filings silently
    count = await store.seed_filings(cik, recent_filings, ["AAPL"])
    assert count == 2
    assert await store.is_cik_seeded(cik) is True
    assert await store.is_filing_seen("0000320193-26-000001") is True
    assert await store.is_filing_seen("0000320193-26-000002") is True

    # Newly added filing (not in seeded batch) should be detected as new
    new_filing = {
        "accession_number": "0000320193-26-000003",
        "cik": cik,
        "form": "8-K",
        "filing_date": "2026-10-17",
        "acceptance_datetime": "2026-10-17T09:00:00.000Z",
        "items": "1.01",
        "primary_document": "aapl3.htm",
    }
    assert await store.record_new_filing(new_filing, ["AAPL"]) is True

    # Newly added unseeded CIK (e.g. MSFT = 789019)
    assert await store.is_cik_seeded(789019) is False
    await store.close()


@pytest.mark.asyncio
async def test_pending_alerts_queue(tmp_path):
    db_file = tmp_path / "test_queue.db"
    store = FilingStore(db_file)
    await store.connect()

    payload = {"ticker": "AAPL", "form": "8-K", "message": "Test Alert"}
    await store.enqueue_pending_alert("0000320193-26-000001", payload)

    pending = await store.get_pending_alerts()
    assert len(pending) == 1
    assert pending[0]["accession_number"] == "0000320193-26-000001"
    assert pending[0]["payload"]["ticker"] == "AAPL"
    assert pending[0]["retry_count"] == 0

    # Increment retry on failure
    await store.increment_pending_retry("0000320193-26-000001", "Connection refused")
    pending_after_err = await store.get_pending_alerts()
    assert pending_after_err[0]["retry_count"] == 1
    assert pending_after_err[0]["last_error"] == "Connection refused"

    # Delete upon success
    await store.delete_pending_alert("0000320193-26-000001")
    assert len(await store.get_pending_alerts()) == 0
    await store.close()


@pytest.mark.asyncio
async def test_meta_key_value(tmp_path):
    db_file = tmp_path / "test_meta.db"
    store = FilingStore(db_file)
    await store.connect()

    assert await store.get_meta("last_refresh") is None
    assert await store.get_meta("last_refresh", "default_val") == "default_val"

    await store.set_meta("last_refresh", "1729000000")
    assert await store.get_meta("last_refresh") == "1729000000"

    await store.close()
