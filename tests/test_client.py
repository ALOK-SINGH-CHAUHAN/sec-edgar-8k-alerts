"""Tests for edgar_client.py with mocked SEC responses via respx."""

import json
from pathlib import Path
import pytest
import respx
import httpx

from config import SECConfig
from edgar_client import (
    SEC_ATOM_FEED_URL,
    SEC_SUBMISSIONS_BASE,
    SEC_TICKERS_URL,
    EdgarClient,
    build_filing_index_url,
    build_primary_doc_url,
    normalize_ticker,
)
from rate_limiter import AsyncTokenBucket


@pytest.fixture
def test_sec_config() -> SECConfig:
    return SECConfig(
        user_agent="TestApp/1.0 (tester@domain.com)",
        max_requests_per_second=8.0,
        cooldown_seconds_on_403=2,
    )


def test_normalize_ticker():
    assert normalize_ticker("aapl") == "AAPL"
    assert normalize_ticker("BRK.B") == "BRK-B"
    assert normalize_ticker("  brk-b  ") == "BRK-B"


def test_build_filing_index_url():
    url = build_filing_index_url(320193, "0000320193-26-000123")
    assert (
        url
        == "https://www.sec.gov/Archives/edgar/data/320193/000032019326000123/0000320193-26-000123-index.htm"
    )

    # With zero-padded CIK string
    url2 = build_filing_index_url("0000320193", "0000320193-26-000123")
    assert (
        url2
        == "https://www.sec.gov/Archives/edgar/data/320193/000032019326000123/0000320193-26-000123-index.htm"
    )


@pytest.mark.asyncio
@respx.mock
async def test_get_company_tickers_map(
    tmp_path, test_sec_config, sample_company_tickers_json
):
    respx.get(SEC_TICKERS_URL).respond(
        status_code=200, json=sample_company_tickers_json
    )

    client = EdgarClient(config=test_sec_config, cache_dir=tmp_path)
    mapping = await client.get_company_tickers_map(force_refresh=True)

    assert mapping["AAPL"] == 320193
    assert mapping["MSFT"] == 789019
    # Both BRK-B and BRK.B resolve
    assert client.resolve_ticker("BRK-B") == 1067983
    assert client.resolve_ticker("BRK.B") == 1067983
    # Multiple tickers mapping to same CIK
    assert client.resolve_ticker("GOOG") == 1652044
    assert client.resolve_ticker("GOOGL") == 1652044
    # Missing ticker returns None without crash
    assert client.resolve_ticker("NONEXISTENT") is None

    # Verify cache file saved on disk
    cache_file = tmp_path / "company_tickers.json"
    assert cache_file.exists()
    await client.close()


@pytest.mark.asyncio
@respx.mock
async def test_fetch_submissions_and_conditional_requests(
    tmp_path, test_sec_config, sample_submissions_aapl_json
):
    submissions_url = f"{SEC_SUBMISSIONS_BASE}/CIK0000320193.json"

    # First request returns 200 with ETag
    route = respx.get(submissions_url)
    route.side_effect = [
        httpx.Response(
            200,
            json=sample_submissions_aapl_json,
            headers={"ETag": '"abc123etag"', "Last-Modified": "Fri, 16 Oct 2026 16:30:00 GMT"},
        ),
        # Second request with If-None-Match responds with 304 Not Modified
        httpx.Response(304),
    ]

    client = EdgarClient(config=test_sec_config, cache_dir=tmp_path)
    filings = await client.fetch_submissions(320193)

    assert filings is not None
    assert len(filings) == 5
    assert filings[0]["accession_number"] == "0000320193-26-000001"
    assert filings[0]["form"] == "8-K"
    assert filings[0]["items"] == "2.02,9.01"

    # Verify second call sends conditional header and returns None (304)
    second_result = await client.fetch_submissions(320193)
    assert second_result is None

    # Check that route was called twice and second request included If-None-Match
    assert route.call_count == 2
    assert route.calls[1].request.headers["If-None-Match"] == '"abc123etag"'
    await client.close()


@pytest.mark.asyncio
@respx.mock
async def test_backoff_on_429_with_retry_after(tmp_path, test_sec_config):
    test_url = "https://data.sec.gov/submissions/CIK0000320193.json"

    route = respx.get(test_url)
    route.side_effect = [
        httpx.Response(429, headers={"Retry-After": "1"}),
        httpx.Response(200, json={"filings": {"recent": {}}}),
    ]

    client = EdgarClient(config=test_sec_config, cache_dir=tmp_path)
    resp = await client.request_with_retry("GET", test_url)

    assert resp.status_code == 200
    assert route.call_count == 2
    await client.close()


@pytest.mark.asyncio
@respx.mock
async def test_repeated_403_triggers_cooldown(tmp_path, test_sec_config):
    test_url = "https://data.sec.gov/submissions/CIK0000320193.json"

    # Set cooldown to a small value for test
    test_sec_config.cooldown_seconds_on_403 = 1
    route = respx.get(test_url).respond(status_code=403)

    client = EdgarClient(config=test_sec_config, cache_dir=tmp_path)

    with pytest.raises(RuntimeError, match="Exceeded max retries"):
        await client.request_with_retry("GET", test_url, max_retries=2)

    assert client._consecutive_403 >= 3
    assert client._cooldown_until > 0
    await client.close()


@pytest.mark.asyncio
@respx.mock
async def test_fetch_atom_feed(tmp_path, test_sec_config, sample_atom_feed_xml):
    respx.get(SEC_ATOM_FEED_URL).respond(
        status_code=200,
        content=sample_atom_feed_xml.encode("utf-8"),
        headers={"Content-Type": "application/atom+xml"},
    )

    client = EdgarClient(config=test_sec_config, cache_dir=tmp_path)
    entries = await client.fetch_atom_feed()

    assert len(entries) == 2
    assert entries[0]["accession_number"] == "0000320193-26-000001"
    assert entries[0]["cik"] == 320193
    assert entries[0]["form"] == "8-K"
    assert entries[1]["cik"] == 1999999
    await client.close()


@pytest.mark.asyncio
@respx.mock
async def test_fetch_primary_doc_preview(
    tmp_path, test_sec_config, sample_primary_doc_html
):
    url = build_primary_doc_url(320193, "0000320193-26-000001", "aapl-20261016.htm")
    respx.get(url).respond(
        status_code=200,
        text=sample_primary_doc_html,
        headers={"Content-Type": "text/html"},
    )

    client = EdgarClient(config=test_sec_config, cache_dir=tmp_path)
    preview = await client.fetch_primary_document_preview(
        cik=320193,
        accession_number="0000320193-26-000001",
        primary_document="aapl-20261016.htm",
        max_chars=200,
    )

    assert preview is not None
    assert "Item 2.02 Results of Operations" in preview
    assert "Apple Inc." in preview
    assert len(preview) <= 203  # with ellipsis
    await client.close()
