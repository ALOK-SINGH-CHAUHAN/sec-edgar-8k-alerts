"""Pytest fixtures and recorded sample SEC data."""

import json
from typing import Any, Dict
import pytest
from config import AppConfig


@pytest.fixture
def sample_company_tickers_json() -> Dict[str, Any]:
    """Sample SEC company_tickers.json response."""
    return {
        "0": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."},
        "1": {"cik_str": 789019, "ticker": "MSFT", "title": "MICROSOFT CORP"},
        "2": {"cik_str": 1045810, "ticker": "NVDA", "title": "NVIDIA CORP"},
        "3": {"cik_str": 1067983, "ticker": "BRK-B", "title": "BERKSHIRE HATHAWAY INC"},
        "4": {"cik_str": 1652044, "ticker": "GOOG", "title": "Alphabet Inc."},
        "5": {"cik_str": 1652044, "ticker": "GOOGL", "title": "Alphabet Inc."},
    }


@pytest.fixture
def sample_submissions_aapl_json() -> Dict[str, Any]:
    """Sample data.sec.gov submissions response for AAPL (CIK 0000320193)."""
    return {
        "cik": "0000320193",
        "entityType": "Operating",
        "sic": "3571",
        "sicDescription": "Electronic Computers",
        "name": "Apple Inc.",
        "tickers": ["AAPL"],
        "exchanges": ["Nasdaq"],
        "filings": {
            "recent": {
                "accessionNumber": [
                    "0000320193-26-000001",
                    "0000320193-26-000002",
                    "0000320193-26-000003",
                    "0000320193-26-000004",
                    "0000320193-26-000005",
                ],
                "filingDate": [
                    "2026-10-16",
                    "2026-10-15",
                    "2026-10-14",
                    "2026-10-13",
                    "2026-10-12",
                ],
                "reportDate": [
                    "2026-10-16",
                    "2026-10-15",
                    "2026-10-14",
                    "2026-09-30",
                    "2026-10-12",
                ],
                "acceptanceDateTime": [
                    "2026-10-16T16:30:12.000Z",
                    "2026-10-15T09:15:00.000Z",
                    "2026-10-14T11:00:25.000Z",
                    "2026-10-13T17:45:00.000Z",
                    "2026-10-12T18:20:00.000Z",
                ],
                "act": ["34", "34", "34", "34", "34"],
                "form": [
                    "8-K",
                    "8-K",
                    "8-K/A",
                    "10-Q",
                    "4",
                ],
                "fileNumber": [
                    "001-36743",
                    "001-36743",
                    "001-36743",
                    "001-36743",
                    "001-36743",
                ],
                "filmNumber": [
                    "26123456",
                    "26123457",
                    "26123458",
                    "26123459",
                    "26123460",
                ],
                "items": [
                    "2.02,9.01",
                    "5.02",
                    "8.01",
                    "",
                    "",
                ],
                "size": [123456, 234567, 345678, 456789, 56789],
                "isXBRL": [1, 1, 1, 1, 0],
                "isInlineXBRL": [1, 1, 1, 1, 0],
                "primaryDocument": [
                    "aapl-20261016.htm",
                    "aapl-20261015.htm",
                    "aapl-20261014.htm",
                    "aapl-20260930.htm",
                    "form4.xml",
                ],
                "primaryDocDescription": [
                    "8-K",
                    "8-K",
                    "8-K/A",
                    "10-Q",
                    "FORM 4",
                ],
            }
        },
    }


@pytest.fixture
def sample_atom_feed_xml() -> str:
    """Sample EDGAR 8-K Atom feed response."""
    return """<?xml version="1.0" encoding="ISO-8859-1" ?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>Current 8-K Filings</title>
  <updated>2026-10-16T16:35:00-04:00</updated>
  <entry>
    <title>8-K - Apple Inc. (0000320193) (Filer)</title>
    <link rel="alternate" type="text/html" href="https://www.sec.gov/Archives/edgar/data/320193/000032019326000001/0000320193-26-000001-index.htm"/>
    <summary type="html">&lt;b&gt;Filed:&lt;/b&gt; 2026-10-16 &lt;b&gt;AccNo:&lt;/b&gt; 0000320193-26-000001 &lt;b&gt;Size:&lt;/b&gt; 120 KB</summary>
    <updated>2026-10-16T16:30:12-04:00</updated>
    <category scheme="http://www.sec.gov/" term="8-K" label="form type"/>
    <id>urn:tag:sec.gov,2008:accession-number=0000320193-26-000001</id>
  </entry>
  <entry>
    <title>8-K - Random Other Corp (0001999999) (Filer)</title>
    <link rel="alternate" type="text/html" href="https://www.sec.gov/Archives/edgar/data/1999999/000199999926000001/0001999999-26-000001-index.htm"/>
    <summary type="html">&lt;b&gt;Filed:&lt;/b&gt; 2026-10-16 &lt;b&gt;AccNo:&lt;/b&gt; 0001999999-26-000001</summary>
    <updated>2026-10-16T16:25:00-04:00</updated>
    <category scheme="http://www.sec.gov/" term="8-K" label="form type"/>
    <id>urn:tag:sec.gov,2008:accession-number=0001999999-26-000001</id>
  </entry>
</feed>
"""


@pytest.fixture
def sample_primary_doc_html() -> str:
    """Sample HTML document for preview extraction."""
    return """<!DOCTYPE html>
<html>
<head><title>Apple Inc. Form 8-K</title></head>
<body>
<div style="font-family: Arial;">
  <p>UNITED STATES SECURITIES AND EXCHANGE COMMISSION</p>
  <p>Washington, D.C. 20549</p>
  <p>FORM 8-K</p>
  <p>CURRENT REPORT PURSUANT TO SECTION 13 OR 15(d) OF THE SECURITIES EXCHANGE ACT OF 1934</p>
  <p>Date of Report: October 16, 2026</p>
  <p>Item 2.02 Results of Operations and Financial Condition.</p>
  <p>On October 16, 2026, Apple Inc. issued a press release announcing its financial results for its fourth fiscal quarter ended September 26, 2026. The press release is attached hereto as Exhibit 99.1 and is incorporated herein by reference.</p>
</div>
</body>
</html>
"""


@pytest.fixture
def valid_app_config(tmp_path) -> AppConfig:
    """Fixture providing a validated AppConfig for testing."""
    db_file = tmp_path / "test.db"
    return AppConfig(
        watchlist=["AAPL", "MSFT", "BRK-B"],
        sec={"user_agent": "TestApp/1.0 (test@domain.com)", "max_requests_per_second": 8.0},
        database_path=str(db_file),
    )
