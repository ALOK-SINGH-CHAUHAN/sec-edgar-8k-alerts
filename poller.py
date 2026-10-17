"""Poller service: scheduler, staggering, market-hours, and SEC filing orchestration."""

import asyncio
from datetime import datetime, time as dtime, timezone
import logging
import signal
import time
from typing import Any, Dict, List, Optional, Set
from zoneinfo import ZoneInfo

from config import AppConfig, PollConfig
from edgar_client import EdgarClient, normalize_ticker
from notifier import AlertMessage, Notifier, is_in_quiet_hours
from store import FilingStore

logger = logging.getLogger(__name__)


def is_market_fast_hours(poll_cfg: PollConfig, check_dt: Optional[datetime] = None) -> bool:
    """
    Determine if current time is within market fast-poll hours (weekdays, non-holidays,
    typically 04:00 to 20:00 ET).
    """
    tz = ZoneInfo(poll_cfg.timezone)
    now = (check_dt or datetime.now(timezone.utc)).astimezone(tz)

    # 1. Weekday check (Monday=0 ... Sunday=6)
    if now.weekday() >= 5:
        return False

    # 2. Holiday check
    date_str = now.strftime("%Y-%m-%d")
    if date_str in poll_cfg.holidays:
        return False

    # 3. Market hours check
    current_time = now.time()
    s_h, s_m = [int(p) for p in poll_cfg.fast_window_start.split(":")]
    e_h, e_m = [int(p) for p in poll_cfg.fast_window_end.split(":")]
    start_time = dtime(s_h, s_m)
    end_time = dtime(e_h, e_m)

    return start_time <= current_time <= end_time


class EdgarPoller:
    """Orchestrates periodic polling of SEC EDGAR for watchlist tickers."""

    def __init__(
        self,
        config: AppConfig,
        store: FilingStore,
        client: EdgarClient,
        notifier: Notifier,
    ):
        self.config = config
        self.store = store
        self.client = client
        self.notifier = notifier

        self.running = False
        self._shutdown_event = asyncio.Event()

        # Metrics & Heartbeat
        self.start_time: float = 0.0
        self.cycles_completed: int = 0
        self.alerts_sent: int = 0
        self.requests_made: int = 0
        self.errors_count: int = 0
        self.last_heartbeat_time: float = 0.0

    def get_cik_watchlist_mapping(self) -> Dict[int, List[str]]:
        """
        Group watchlist tickers by their resolved CIK so each CIK is polled only once.
        Returns dict of {cik: [ticker1, ticker2, ...]}.
        """
        cik_map: Dict[int, List[str]] = {}
        for ticker in self.config.watchlist:
            cik = self.client.resolve_ticker(ticker)
            if cik is None:
                logger.warning(
                    "Watchlist ticker '%s' could not be resolved to a CIK. Skipping.",
                    ticker,
                )
                continue
            norm_ticker = normalize_ticker(ticker)
            if cik not in cik_map:
                cik_map[cik] = []
            if norm_ticker not in cik_map[cik]:
                cik_map[cik].append(norm_ticker)
        return cik_map

    async def poll_cik(
        self, cik: int, watchlist_tickers: List[str]
    ) -> List[Dict[str, Any]]:
        """
        Poll recent filings for a specific CIK.
        If CIK is unseeded, seed existing filings silently (0 alerts).
        If already seeded, identify and return newly published filings matching tracked forms.
        """
        filings = await self.client.fetch_submissions(cik)
        self.requests_made += 1

        if filings is None:
            # 304 Not Modified
            return []

        # Check if this CIK has been seeded
        is_seeded = await self.store.is_cik_seeded(cik)
        if not is_seeded:
            seeded_count = await self.store.seed_filings(cik, filings, watchlist_tickers)
            logger.info(
                "Initial silent seeding completed: %d filings recorded for CIK %d (%s) — 0 alerts sent.",
                seeded_count,
                cik,
                ", ".join(watchlist_tickers),
            )
            return []

        # Process new filings in chronological order (oldest to newest)
        tracked_forms = set(self.config.forms)
        new_filings: List[Dict[str, Any]] = []

        for filing in reversed(filings):
            form = filing.get("form", "")
            if form not in tracked_forms:
                continue

            # Atomic insert-before-alert pattern
            is_new = await self.store.record_new_filing(filing, watchlist_tickers)
            if is_new:
                new_filings.append(filing)

        return new_filings

    async def process_new_filing(
        self, filing: Dict[str, Any], watchlist_tickers: List[str], is_backfill: bool = False
    ) -> bool:
        """Fetch preview if enabled, construct alert, and dispatch or queue."""
        accession_number = filing["accession_number"]
        cik = filing["cik"]
        form = filing["form"]
        items_raw = filing.get("items", "")
        acc_dt_str = filing.get("acceptance_datetime", "")
        company_name = filing.get("company_name", f"CIK {cik}")

        preview: Optional[str] = None
        if self.config.document_preview.enabled and filing.get("primary_document"):
            try:
                preview = await self.client.fetch_primary_document_preview(
                    cik=cik,
                    accession_number=accession_number,
                    primary_document=filing["primary_document"],
                    max_chars=self.config.document_preview.max_chars,
                    timeout=self.config.document_preview.timeout_seconds,
                )
                self.requests_made += 1
            except Exception as e:
                logger.debug("Suppressing preview fetch error for %s: %s", accession_number, e)

        alert = AlertMessage(
            tickers=watchlist_tickers,
            cik=cik,
            company_name=company_name,
            form=form,
            items_raw=items_raw,
            acceptance_dt_str=acc_dt_str,
            accession_number=accession_number,
            preview=preview,
            is_backfill=is_backfill,
        )

        # Check quiet hours
        in_quiet = is_in_quiet_hours(self.config.quiet_hours)
        bypass = self.config.quiet_hours.high_priority_bypasses and alert.is_high_priority

        if in_quiet and not bypass:
            if self.config.quiet_hours.mode == "suppress":
                logger.info(
                    "Quiet hours active: Suppressing alert for %s (%s)",
                    ", ".join(watchlist_tickers),
                    form,
                )
                await self.store.mark_filing_alerted(accession_number, "suppressed")
                return True
            else:  # "queue"
                logger.info(
                    "Quiet hours active: Queuing alert for %s (%s)",
                    ", ".join(watchlist_tickers),
                    form,
                )
                payload = {
                    "tickers": watchlist_tickers,
                    "cik": cik,
                    "company_name": company_name,
                    "form": form,
                    "items_raw": items_raw,
                    "acceptance_dt_str": acc_dt_str,
                    "accession_number": accession_number,
                    "preview": preview,
                    "is_backfill": is_backfill,
                }
                await self.store.enqueue_pending_alert(accession_number, payload)
                await self.store.mark_filing_alerted(accession_number, "queued")
                return True

        # Send alert
        results = await self.notifier.send_alert(alert)
        success = any(results.values())

        if success:
            await self.store.mark_filing_alerted(accession_number, "sent")
            self.alerts_sent += 1
            return True
        else:
            logger.error("Failed to deliver alert for %s. Enqueuing for retry.", accession_number)
            payload = {
                "tickers": watchlist_tickers,
                "cik": cik,
                "company_name": company_name,
                "form": form,
                "items_raw": items_raw,
                "acceptance_dt_str": acc_dt_str,
                "accession_number": accession_number,
                "preview": preview,
                "is_backfill": is_backfill,
            }
            await self.store.enqueue_pending_alert(accession_number, payload)
            await self.store.mark_filing_alerted(accession_number, "failed")
            return False

    async def flush_pending_alerts(self) -> None:
        """Retry sending queued alerts when outside quiet hours."""
        if is_in_quiet_hours(self.config.quiet_hours):
            return

        pending = await self.store.get_pending_alerts(limit=20)
        for item in pending:
            payload = item["payload"]
            acc = item["accession_number"]
            try:
                alert = AlertMessage(
                    tickers=payload.get("tickers", []),
                    cik=payload.get("cik", 0),
                    company_name=payload.get("company_name", ""),
                    form=payload.get("form", ""),
                    items_raw=payload.get("items_raw", ""),
                    acceptance_dt_str=payload.get("acceptance_dt_str", ""),
                    accession_number=acc,
                    preview=payload.get("preview"),
                    is_backfill=payload.get("is_backfill", False),
                )
                results = await self.notifier.send_alert(alert)
                if any(results.values()):
                    await self.store.delete_pending_alert(acc)
                    await self.store.mark_filing_alerted(acc, "sent")
                    self.alerts_sent += 1
                else:
                    await self.store.increment_pending_retry(acc, "Delivery failed across channels")
            except Exception as e:
                logger.error("Error retrying pending alert %s: %s", acc, e)
                await self.store.increment_pending_retry(acc, str(e))

    def log_heartbeat_if_due(self, is_fast_mode: bool) -> None:
        """Log structured heartbeat status every 10 minutes."""
        now = time.monotonic()
        if now - self.last_heartbeat_time >= 600:
            uptime_seconds = int(now - self.start_time)
            mode_str = "FAST" if is_fast_mode else "SLOW"
            logger.info(
                "[HEARTBEAT] uptime=%ds cycles=%d requests=%d alerts_sent=%d errors=%d mode=%s",
                uptime_seconds,
                self.cycles_completed,
                self.requests_made,
                self.alerts_sent,
                self.errors_count,
                mode_str,
            )
            self.last_heartbeat_time = now

    async def run_cycle(self) -> None:
        """Execute one complete polling cycle across all unique CIKs."""
        is_fast = is_market_fast_hours(self.config.poll)
        interval = (
            self.config.poll.fast_interval_seconds
            if is_fast
            else self.config.poll.slow_interval_seconds
        )

        # Refresh tickers map if needed
        await self.client.get_company_tickers_map()

        cik_map = self.get_cik_watchlist_mapping()
        unique_ciks = list(cik_map.keys())

        if not unique_ciks:
            logger.warning("No valid watchlist CIKs to poll.")
            await asyncio.sleep(interval)
            return

        # Stagger requests evenly across interval
        stagger_delay = max(0.05, float(interval) / float(len(unique_ciks)))

        for cik in unique_ciks:
            if self._shutdown_event.is_set():
                break

            tickers = cik_map[cik]
            try:
                new_filings = await self.poll_cik(cik, tickers)
                for filing in new_filings:
                    await self.process_new_filing(filing, tickers)
            except Exception as e:
                self.errors_count += 1
                logger.error("Error polling CIK %d (%s): %s", cik, ", ".join(tickers), e)

            # Sleep stagger delay between CIKs unless shutting down
            if not self._shutdown_event.is_set():
                try:
                    await asyncio.wait_for(self._shutdown_event.wait(), timeout=stagger_delay)
                except asyncio.TimeoutError:
                    pass

        # Flush any pending/queued alerts
        await self.flush_pending_alerts()

        self.cycles_completed += 1
        self.log_heartbeat_if_due(is_fast)

    async def run(self) -> None:
        """Run poller main loop until shutdown signal."""
        self.running = True
        self.start_time = time.monotonic()
        self.last_heartbeat_time = self.start_time

        logger.info(
            "Starting SEC EDGAR Alert Poller. Watchlist: %s",
            ", ".join(self.config.watchlist),
        )

        while not self._shutdown_event.is_set():
            try:
                await self.run_cycle()
            except Exception as e:
                self.errors_count += 1
                logger.error("Unexpected error in poll cycle: %s", e, exc_info=True)
                await asyncio.sleep(5.0)

        logger.info("EDGAR Alert Poller stopped cleanly.")

    def stop(self) -> None:
        """Signal poller to stop gracefully."""
        logger.info("Shutdown requested. Finishing active tasks...")
        self.running = False
        self._shutdown_event.set()
