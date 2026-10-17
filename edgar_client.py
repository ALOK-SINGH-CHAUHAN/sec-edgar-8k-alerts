"""SEC EDGAR HTTP client with rate limiting, retry backoff, and caching."""

import asyncio
from datetime import datetime, timezone
import json
import logging
from pathlib import Path
import random
import re
import time
from typing import Any, Dict, List, Optional, Set, Tuple
import xml.etree.ElementTree as ET

from bs4 import BeautifulSoup
import httpx

from config import AppConfig, SECConfig
from rate_limiter import AsyncTokenBucket

logger = logging.getLogger(__name__)

SEC_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
SEC_SUBMISSIONS_BASE = "https://data.sec.gov/submissions"
SEC_ARCHIVES_BASE = "https://www.sec.gov/Archives/edgar/data"
SEC_ATOM_FEED_URL = (
    "https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=8-K&output=atom"
)


def normalize_ticker(ticker: str) -> str:
    """Normalize stock symbol: uppercase, strip, dots replaced with dashes."""
    return ticker.strip().upper().replace(".", "-")


def build_filing_index_url(cik: int | str, accession_number: str) -> str:
    """
    Construct direct URL to the filing index page:
    https://www.sec.gov/Archives/edgar/data/{cik_int}/{accession_no_dashes}/{accession}-index.htm
    """
    cik_int = int(str(cik).lstrip("0") or "0")
    accession_clean = accession_number.strip()
    accession_no_dashes = accession_clean.replace("-", "")
    return f"{SEC_ARCHIVES_BASE}/{cik_int}/{accession_no_dashes}/{accession_clean}-index.htm"


def build_primary_doc_url(cik: int | str, accession_number: str, primary_doc: str) -> str:
    """Construct direct URL to the primary document."""
    cik_int = int(str(cik).lstrip("0") or "0")
    accession_no_dashes = accession_number.strip().replace("-", "")
    primary_clean = primary_doc.strip().lstrip("/")
    return f"{SEC_ARCHIVES_BASE}/{cik_int}/{accession_no_dashes}/{primary_clean}"


class EdgarClient:
    """
    Client for querying official SEC EDGAR endpoints adhering strictly
    to SEC fair-access policies.
    """

    def __init__(
        self,
        config: SECConfig,
        cache_dir: str | Path = "data/cache",
        rate_limiter: Optional[AsyncTokenBucket] = None,
        transport: Optional[httpx.AsyncBaseTransport] = None,
    ):
        self.config = config
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)

        self.rate_limiter = rate_limiter or AsyncTokenBucket(
            rate=config.max_requests_per_second
        )

        headers = {
            "User-Agent": config.user_agent,
            "Accept-Encoding": "gzip, deflate",
            "Accept": "application/json, text/html, application/atom+xml, */*",
        }

        self.client = httpx.AsyncClient(
            headers=headers,
            follow_redirects=True,
            timeout=httpx.Timeout(15.0, connect=5.0),
            transport=transport,
        )

        # Mapping caches
        self.ticker_to_cik: Dict[str, int] = {}
        self.cik_to_tickers: Dict[int, Set[str]] = {}
        self.cik_to_name: Dict[int, str] = {}
        self._last_ticker_map_refresh: float = 0.0

        # Conditional request validators per CIK (submissions)
        # {cik_padded: {"etag": ..., "last_modified": ...}}
        self._conditional_validators: Dict[str, Dict[str, str]] = {}

        # 403 cooldown state
        self._consecutive_403: int = 0
        self._cooldown_until: float = 0.0

    async def close(self) -> None:
        """Close the underlying HTTP client."""
        await self.client.aclose()

    async def __aenter__(self) -> "EdgarClient":
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        await self.close()

    async def _check_and_wait_cooldown(self) -> None:
        """Pause requests if a 403 cooldown is currently active."""
        now = time.monotonic()
        if now < self._cooldown_until:
            wait_remaining = self._cooldown_until - now
            logger.warning(
                "SEC 403 Cooldown in effect. Pausing request for %.1fs (IP protection)",
                wait_remaining,
            )
            await asyncio.sleep(wait_remaining)

    async def request_with_retry(
        self,
        method: str,
        url: str,
        headers: Optional[Dict[str, str]] = None,
        max_retries: int = 3,
        **kwargs,
    ) -> httpx.Response:
        """
        Execute request under global token bucket with exponential backoff on 429/403/5xx.
        """
        attempt = 0
        backoff_base = 1.0

        while attempt <= max_retries:
            await self._check_and_wait_cooldown()

            # Enforce global rate limiter
            await self.rate_limiter.acquire(1.0)

            try:
                response = await self.client.request(
                    method=method, url=url, headers=headers, **kwargs
                )

                # Reset 403 counter on success or valid not-modified
                if response.status_code in (200, 304):
                    self._consecutive_403 = 0
                    return response

                # Handle 429 Too Many Requests
                if response.status_code == 429:
                    attempt += 1
                    retry_after = response.headers.get("Retry-After")
                    delay = (
                        float(retry_after)
                        if retry_after and retry_after.isdigit()
                        else (backoff_base * (2**attempt) + random.uniform(0.1, 0.5))
                    )
                    logger.warning(
                        "SEC EDGAR 429 Too Many Requests for %s. Backing off for %.2fs (attempt %d/%d)",
                        url,
                        delay,
                        attempt,
                        max_retries,
                    )
                    await asyncio.sleep(delay)
                    continue

                # Handle 403 Forbidden
                if response.status_code == 403:
                    self._consecutive_403 += 1
                    attempt += 1
                    if self._consecutive_403 >= 3:
                        cooldown = self.config.cooldown_seconds_on_403
                        self._cooldown_until = time.monotonic() + cooldown
                        logger.error(
                            "CRITICAL: SEC returned %d consecutive 403 Forbidden responses. "
                            "Activating cooldown for %ds to prevent persistent IP ban.",
                            self._consecutive_403,
                            cooldown,
                        )
                        await asyncio.sleep(cooldown)
                        continue

                    delay = backoff_base * (2**attempt) + random.uniform(0.5, 1.5)
                    logger.warning(
                        "SEC EDGAR 403 Forbidden on %s. Backing off for %.2fs (attempt %d/%d)",
                        url,
                        delay,
                        attempt,
                        max_retries,
                    )
                    await asyncio.sleep(delay)
                    continue

                # Handle 5xx Server Errors
                if 500 <= response.status_code < 600:
                    attempt += 1
                    delay = backoff_base * (2**attempt) + random.uniform(0.1, 0.5)
                    logger.warning(
                        "SEC EDGAR server error %d on %s. Retrying in %.2fs (attempt %d/%d)",
                        response.status_code,
                        url,
                        delay,
                        attempt,
                        max_retries,
                    )
                    await asyncio.sleep(delay)
                    continue

                # Any other response (e.g. 404, 400)
                return response

            except (httpx.TransportError, httpx.TimeoutException) as exc:
                attempt += 1
                if attempt > max_retries:
                    logger.error(
                        "Network error querying %s after %d retries: %s",
                        url,
                        max_retries,
                        exc,
                    )
                    raise
                delay = backoff_base * (2**attempt) + random.uniform(0.1, 0.5)
                logger.warning(
                    "Network error (%s) on %s. Retrying in %.2fs (attempt %d/%d)",
                    exc.__class__.__name__,
                    url,
                    delay,
                    attempt,
                    max_retries,
                )
                await asyncio.sleep(delay)

        raise RuntimeError(f"Exceeded max retries ({max_retries}) for {url}")

    async def get_company_tickers_map(self, force_refresh: bool = False) -> Dict[str, int]:
        """
        Download and cache SEC company_tickers.json.
        Returns a dictionary mapping normalized ticker symbols to integer CIKs.
        """
        cache_file = self.cache_dir / "company_tickers.json"
        now = time.time()
        refresh_interval_sec = self.config.ticker_map_refresh_hours * 3600

        need_fetch = force_refresh or not cache_file.exists()
        if not need_fetch and cache_file.exists():
            file_age = now - cache_file.stat().st_mtime
            if file_age > refresh_interval_sec:
                need_fetch = True

        data: Optional[Dict[str, Any]] = None

        if need_fetch:
            logger.info("Fetching fresh SEC company_tickers.json from %s", SEC_TICKERS_URL)
            try:
                resp = await self.request_with_retry("GET", SEC_TICKERS_URL)
                if resp.status_code == 200:
                    data = resp.json()
                    with open(cache_file, "w", encoding="utf-8") as f:
                        json.dump(data, f)
                    self._last_ticker_map_refresh = now
                else:
                    logger.error(
                        "Failed to fetch company_tickers.json: HTTP %d", resp.status_code
                    )
            except Exception as e:
                logger.error("Error fetching company_tickers.json: %s", e)

        # Fallback to local cache if fetch failed or wasn't needed
        if data is None and cache_file.exists():
            try:
                with open(cache_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except Exception as e:
                logger.error("Failed to read cached company_tickers.json: %s", e)

        if not data:
            logger.warning("No company tickers data available!")
            return self.ticker_to_cik

        # Populate internal mappings
        self.ticker_to_cik.clear()
        self.cik_to_tickers.clear()
        self.cik_to_name.clear()

        for entry in data.values():
            if not isinstance(entry, dict):
                continue
            cik_int = int(entry.get("cik_str", 0))
            raw_ticker = str(entry.get("ticker", "")).strip().upper()
            title = str(entry.get("title", "")).strip()

            if not cik_int or not raw_ticker:
                continue

            norm_ticker = normalize_ticker(raw_ticker)

            # Map both raw and normalized versions
            self.ticker_to_cik[raw_ticker] = cik_int
            self.ticker_to_cik[norm_ticker] = cik_int

            if cik_int not in self.cik_to_tickers:
                self.cik_to_tickers[cik_int] = set()
            self.cik_to_tickers[cik_int].add(norm_ticker)
            self.cik_to_name[cik_int] = title

        logger.info(
            "Loaded CIK map: %d unique tickers across %d CIKs",
            len(self.ticker_to_cik),
            len(self.cik_to_tickers),
        )
        return self.ticker_to_cik

    def resolve_ticker(self, ticker: str) -> Optional[int]:
        """Resolve a ticker symbol to its CIK integer."""
        norm = normalize_ticker(ticker)
        if norm in self.ticker_to_cik:
            return self.ticker_to_cik[norm]
        raw = ticker.strip().upper()
        return self.ticker_to_cik.get(raw)

    async def fetch_submissions(self, cik: int | str) -> Optional[List[Dict[str, Any]]]:
        """
        Fetch recent filings for a CIK from data.sec.gov/submissions/CIK{10-digit}.json.
        Uses conditional request headers (ETag / If-None-Match, Last-Modified / If-Modified-Since).
        Returns list of parsed filing dictionaries or None if 304 Not Modified.
        """
        cik_str = str(cik).lstrip("0")
        cik_padded = cik_str.zfill(10)
        url = f"{SEC_SUBMISSIONS_BASE}/CIK{cik_padded}.json"

        # Prepare conditional request headers
        headers: Dict[str, str] = {}
        validators = self._conditional_validators.get(cik_padded)
        if validators:
            if "etag" in validators:
                headers["If-None-Match"] = validators["etag"]
            if "last_modified" in validators:
                headers["If-Modified-Since"] = validators["last_modified"]

        resp = await self.request_with_retry("GET", url, headers=headers)

        if resp.status_code == 304:
            logger.debug("CIK %s: 304 Not Modified", cik_padded)
            return None

        if resp.status_code != 200:
            logger.warning("CIK %s: unexpected HTTP status %d", cik_padded, resp.status_code)
            return []

        # Save new conditional validators if present
        new_etag = resp.headers.get("ETag")
        new_lm = resp.headers.get("Last-Modified")
        if new_etag or new_lm:
            val = {}
            if new_etag:
                val["etag"] = new_etag
            if new_lm:
                val["last_modified"] = new_lm
            self._conditional_validators[cik_padded] = val

        try:
            payload = resp.json()
        except Exception as e:
            logger.error("Failed to parse submissions JSON for CIK %s: %s", cik_padded, e)
            return []

        recent = payload.get("filings", {}).get("recent", {})
        accession_numbers = recent.get("accessionNumber", [])
        if not accession_numbers:
            return []

        forms = recent.get("form", [])
        filing_dates = recent.get("filingDate", [])
        acceptance_dts = recent.get("acceptanceDateTime", [])
        items_list = recent.get("items", [])
        primary_docs = recent.get("primaryDocument", [])

        total = len(accession_numbers)
        filings: List[Dict[str, Any]] = []

        cik_int = int(cik_str)
        company_name = payload.get("name") or self.cik_to_name.get(cik_int, f"CIK {cik_int}")

        for i in range(total):
            acc_num = accession_numbers[i] if i < len(accession_numbers) else ""
            form = forms[i] if i < len(forms) else ""
            f_date = filing_dates[i] if i < len(filing_dates) else ""
            acc_dt = acceptance_dts[i] if i < len(acceptance_dts) else ""
            items_str = items_list[i] if i < len(items_list) else ""
            prim_doc = primary_docs[i] if i < len(primary_docs) else ""

            if not acc_num or not form:
                continue

            filings.append(
                {
                    "accession_number": acc_num,
                    "cik": cik_int,
                    "company_name": company_name,
                    "form": form,
                    "filing_date": f_date,
                    "acceptance_datetime": acc_dt,
                    "items": items_str,
                    "primary_document": prim_doc,
                }
            )

        return filings

    async def fetch_primary_document_preview(
        self,
        cik: int | str,
        accession_number: str,
        primary_document: str,
        max_chars: int = 500,
        timeout: float = 3.0,
    ) -> Optional[str]:
        """
        Fetch primaryDocument, strip HTML, collapse whitespace, and return first max_chars.
        Catches all errors and timeouts without raising to guarantee alerts are never blocked.
        """
        if not primary_document or primary_document.endswith(".xml"):
            return None

        url = build_primary_doc_url(cik, accession_number, primary_document)
        try:
            # Enforce rate limiter and execute with short timeout
            await self.rate_limiter.acquire(1.0)
            resp = await self.client.get(url, timeout=timeout)
            if resp.status_code != 200:
                logger.debug("Failed preview fetch for %s: HTTP %d", url, resp.status_code)
                return None

            text = resp.text
            soup = BeautifulSoup(text, "html.parser")

            # Remove scripts, styles, headers, comments
            for tag in soup(["script", "style", "meta", "noscript"]):
                tag.decompose()

            raw_text = soup.get_text(separator=" ")
            # Collapse multiple spaces and linebreaks
            cleaned = re.sub(r"\s+", " ", raw_text).strip()

            # Attempt to skip generic boilerplate header
            # Find first occurrence of "Item" or "Section" or meaningful text
            item_pos = re.search(r"Item\s+\d+\.\d+", cleaned, re.IGNORECASE)
            if item_pos and item_pos.start() > 50:
                preview_body = cleaned[item_pos.start() :]
            else:
                preview_body = cleaned

            if len(preview_body) > max_chars:
                return preview_body[:max_chars].rstrip() + "..."
            return preview_body or None

        except Exception as e:
            logger.debug("Preview fetch suppressed exception for %s: %s", url, e)
            return None

    async def fetch_atom_feed(self) -> List[Dict[str, Any]]:
        """
        Poll SEC 8-K Atom feed:
        https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=8-K&output=atom
        Returns list of parsed entries with accession_number, cik, form, acceptance_datetime.
        """
        try:
            resp = await self.request_with_retry("GET", SEC_ATOM_FEED_URL)
            if resp.status_code != 200:
                logger.warning("Atom feed returned HTTP %d", resp.status_code)
                return []

            root = ET.fromstring(resp.content)
            namespace = {"atom": "http://www.w3.org/2005/Atom"}
            entries: List[Dict[str, Any]] = []

            for entry in root.findall("atom:entry", namespace):
                title_elem = entry.find("atom:title", namespace)
                updated_elem = entry.find("atom:updated", namespace)
                id_elem = entry.find("atom:id", namespace)
                category_elem = entry.find("atom:category", namespace)

                title = title_elem.text if title_elem is not None and title_elem.text else ""
                updated = (
                    updated_elem.text
                    if updated_elem is not None and updated_elem.text
                    else ""
                )
                entry_id = id_elem.text if id_elem is not None and id_elem.text else ""
                form = (
                    category_elem.get("term", "8-K")
                    if category_elem is not None
                    else "8-K"
                )

                # Extract accession number from id (e.g. urn:tag:sec.gov,2008:accession-number=0000320193-26-000001)
                acc_match = re.search(r"accession-number=([0-9\-]+)", entry_id)
                accession_number = acc_match.group(1) if acc_match else ""

                # Extract CIK from title (e.g. "8-K - Apple Inc. (0000320193) (Filer)")
                cik_match = re.search(r"\((\d{10})\)", title)
                cik_int = int(cik_match.group(1)) if cik_match else 0

                if accession_number and cik_int:
                    entries.append(
                        {
                            "accession_number": accession_number,
                            "cik": cik_int,
                            "form": form,
                            "acceptance_datetime": updated,
                            "title": title,
                        }
                    )

            return entries

        except Exception as e:
            logger.error("Error fetching/parsing Atom feed: %s", e)
            return []
