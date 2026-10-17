"""CLI entrypoint for SEC EDGAR 8-K Alert Poller service."""

import argparse
import asyncio
from datetime import datetime, timedelta, timezone
import logging
from pathlib import Path
import signal
import sys
from typing import List, Optional
from ruamel.yaml import YAML

from config import AppConfig, load_config
from edgar_client import EdgarClient, normalize_ticker
from notifier import AlertMessage, Notifier
from poller import EdgarPoller
from store import FilingStore

logger = logging.getLogger("edgar_alerts")


def setup_logging(level_name: str = "INFO") -> None:
    """Configure structured logging."""
    level = getattr(logging, level_name.upper(), logging.INFO)
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


async def cmd_run(config_path: str = "config.yaml") -> int:
    """Start the 24/7 background polling service."""
    try:
        config = load_config(config_path)
    except Exception as e:
        print(f"\n[FATAL CONFIG ERROR] {e}\n", file=sys.stderr)
        return 1

    setup_logging(config.log_level)
    logger.info("Initializing EDGAR 8-K Alert Poller...")

    store = FilingStore(config.database_path)
    await store.connect()

    client = EdgarClient(config=config.sec)
    notifier = Notifier(config)
    poller = EdgarPoller(config=config, store=store, client=client, notifier=notifier)

    # Attach signal handlers for graceful termination
    loop = asyncio.get_running_loop()
    stop_called = False

    def _signal_handler():
        nonlocal stop_called
        if not stop_called:
            stop_called = True
            logger.info("Termination signal received. Shutting down gracefully...")
            poller.stop()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _signal_handler)
        except (NotImplementedError, RuntimeError):
            # Fallback for platforms where add_signal_handler isn't supported
            signal.signal(sig, lambda s, f: _signal_handler())

    try:
        await poller.run()
    finally:
        await poller.flush_pending_alerts()
        await client.close()
        await notifier.close()
        await store.close()
        logger.info("Shutdown complete.")

    return 0


async def cmd_add_ticker(ticker: str, config_path: str = "config.yaml") -> int:
    """Validate ticker against SEC CIK map, append to config.yaml safely, and seed silently."""
    cfg_file = Path(config_path)
    if not cfg_file.exists():
        print(f"Error: {config_path} does not exist.", file=sys.stderr)
        return 1

    try:
        config = load_config(config_path)
    except Exception as e:
        print(f"Error reading configuration: {e}", file=sys.stderr)
        return 1

    setup_logging("INFO")
    norm_symbol = normalize_ticker(ticker)

    client = EdgarClient(config=config.sec)
    print(f"Resolving ticker '{ticker}' (normalized: {norm_symbol}) against SEC CIK map...")
    await client.get_company_tickers_map()
    cik = client.resolve_ticker(norm_symbol)

    if cik is None:
        print(
            f"\n❌ Ticker '{ticker}' not found in official SEC CIK map.\n"
            "Possible reasons:\n"
            "  1. The security is an ETF or fund (tracked under fund CIKs, not corporate 8-Ks).\n"
            "  2. Ticker has been delisted, merged, or recently changed.\n"
            "  3. Symbol spelling mistake.\n"
            "No changes were made to config.yaml.\n",
            file=sys.stderr,
        )
        await client.close()
        return 1

    company_title = client.cik_to_name.get(cik, "Unknown")
    print(f"✓ Found: CIK {cik:010d} — {company_title}")

    # Use ruamel.yaml to preserve comments and structure
    yaml = YAML()
    yaml.preserve_quotes = True
    with open(cfg_file, "r", encoding="utf-8") as f:
        data = yaml.load(f)

    watchlist = data.get("watchlist", [])
    if norm_symbol in watchlist:
        print(f"Ticker '{norm_symbol}' is already present on the watchlist.")
        await client.close()
        return 0

    watchlist.append(norm_symbol)
    data["watchlist"] = watchlist

    with open(cfg_file, "w", encoding="utf-8") as f:
        yaml.dump(data, f)

    print(f"✓ Added '{norm_symbol}' to {config_path} watchlist (comments preserved).")

    # Silent initial seeding for newly added ticker/CIK
    store = FilingStore(config.database_path)
    await store.connect()
    try:
        is_seeded = await store.is_cik_seeded(cik)
        if not is_seeded:
            print(f"Seeding existing filings silently for CIK {cik:010d}...")
            filings = await client.fetch_submissions(cik)
            if filings:
                seeded_count = await store.seed_filings(cik, filings, [norm_symbol])
                print(f"✓ Seeded {seeded_count} past filings silently (0 alerts sent).")
        else:
            print(f"✓ CIK {cik:010d} is already seeded in local database.")
    finally:
        await client.close()
        await store.close()

    # Automatically backfill and dispatch alerts for recent 8-Ks (last 90 days)
    print(f"Triggering initial 90-day alert backfill for '{norm_symbol}'...")
    await cmd_backfill(days=90, config_path=config_path, target_ticker=norm_symbol)

    return 0


async def cmd_remove_ticker(ticker: str, config_path: str = "config.yaml") -> int:
    """Remove ticker from watchlist in config.yaml while preserving comments."""
    cfg_file = Path(config_path)
    if not cfg_file.exists():
        print(f"Error: {config_path} does not exist.", file=sys.stderr)
        return 1

    norm_symbol = normalize_ticker(ticker)

    yaml = YAML()
    yaml.preserve_quotes = True
    with open(cfg_file, "r", encoding="utf-8") as f:
        data = yaml.load(f)

    watchlist = data.get("watchlist", [])
    if norm_symbol not in watchlist and ticker not in watchlist:
        print(f"Ticker '{ticker}' was not found in watchlist.")
        return 0

    # Remove all instances of ticker or norm_symbol
    new_watchlist = [t for t in watchlist if t not in (ticker, norm_symbol)]
    data["watchlist"] = new_watchlist

    with open(cfg_file, "w", encoding="utf-8") as f:
        yaml.dump(data, f)

    print(f"✓ Removed '{ticker}' from watchlist in {config_path}.")
    return 0


async def cmd_test_alert(config_path: str = "config.yaml") -> int:
    """Send a sample mock alert to all configured channels and report status."""
    try:
        config = load_config(config_path)
    except Exception as e:
        print(f"Config error: {e}", file=sys.stderr)
        return 1

    setup_logging(config.log_level)
    print("Sending sample test alert to configured channels...")
    notifier = Notifier(config)

    now_iso = datetime.now(timezone.utc).isoformat()
    sample_alert = AlertMessage(
        tickers=["AAPL", "SAMPLE"],
        cik=320193,
        company_name="Apple Inc. (Test Alert)",
        form="8-K",
        items_raw="2.02,9.01",
        acceptance_dt_str=now_iso,
        accession_number="0000320193-26-999999",
        preview="Sample Preview: Apple Inc. announced preliminary operational highlights. This is a simulated test alert verifying channel delivery.",
    )

    results = await notifier.send_alert(sample_alert)
    print("\n--- Test Alert Delivery Results ---")
    all_success = True
    for ch, success in results.items():
        icon = "✓" if success else "❌"
        print(f"  [{icon}] Channel '{ch}': {'SUCCESS' if success else 'FAILED'}")
        if not success:
            all_success = False

    await notifier.close()
    return 0 if all_success else 1


async def cmd_backfill(
    days: int = 3, config_path: str = "config.yaml", target_ticker: Optional[str] = None
) -> int:
    """Fetch filings from the last N days for the watchlist and alert on unalerted ones."""
    try:
        config = load_config(config_path)
    except Exception as e:
        print(f"Config error: {e}", file=sys.stderr)
        return 1

    setup_logging(config.log_level)
    scan_watchlist = [target_ticker.upper()] if target_ticker else config.watchlist
    print(f"Running backfill for last {days} days across: {', '.join(scan_watchlist)}")

    store = FilingStore(config.database_path)
    await store.connect()
    client = EdgarClient(config=config.sec)
    notifier = Notifier(config)

    cutoff_date = (datetime.now(timezone.utc) - timedelta(days=days)).date().isoformat()
    print(f"Filtering filings filed on or after: {cutoff_date}")

    await client.get_company_tickers_map()
    poller = EdgarPoller(config=config, store=store, client=client, notifier=notifier)
    cik_map = poller.get_cik_watchlist_mapping()

    total_checked = 0
    new_alerts_sent = 0
    tracked_forms = set(config.forms)

    for cik, tickers in cik_map.items():
        if target_ticker and not any(t.upper() == target_ticker.upper() for t in tickers):
            continue

        print(f"\nScanning filings for CIK {cik:010d} ({', '.join(tickers)})...")
        filings = await client.fetch_submissions(cik)
        if not filings:
            print("  No filings returned or 304 Not Modified.")
            continue

        for filing in reversed(filings):
            f_date = filing.get("filing_date", "")
            form = filing.get("form", "")
            acc = filing.get("accession_number", "")

            if f_date < cutoff_date or form not in tracked_forms:
                continue

            total_checked += 1
            is_alerted = await store.is_filing_alerted(acc)
            if is_alerted:
                print(f"  • {form} ({f_date}) {acc} — Already alerted. Skipping.")
                continue

            print(f"  ★ ALERTING FILING: {form} ({f_date}) {acc} — Dispatched as [BACKFILL] alert.")
            await store.record_new_filing(filing, tickers)
            await poller.process_new_filing(filing, tickers, is_backfill=True)
            await store.mark_filing_alerted(acc, status="sent")
            new_alerts_sent += 1

    print(
        f"\nBackfill completed: {total_checked} filings in date window evaluated, "
        f"{new_alerts_sent} new alerts delivered."
    )

    await client.close()
    await store.close()
    await notifier.close()
    return 0


def main() -> None:
    """CLI dispatcher."""
    parser = argparse.ArgumentParser(
        description="SEC EDGAR 8-K Real-Time Alert Poller CLI"
    )
    parser.add_argument(
        "--config",
        default="config.yaml",
        help="Path to YAML configuration file (default: config.yaml)",
    )

    subparsers = parser.add_subparsers(dest="command", required=True)

    # run command
    subparsers.add_parser("run", help="Start the 24/7 background alert poller")

    # add-ticker command
    add_parser = subparsers.add_parser("add-ticker", help="Add ticker to watchlist")
    add_parser.add_argument("ticker", help="Stock symbol to add (e.g. AAPL, BRK-B)")

    # remove-ticker command
    rem_parser = subparsers.add_parser("remove-ticker", help="Remove ticker from watchlist")
    rem_parser.add_argument("ticker", help="Stock symbol to remove")

    # test-alert command
    subparsers.add_parser("test-alert", help="Send a test alert to verify channels")

    # backfill command
    backfill_parser = subparsers.add_parser("backfill", help="Backfill filings from last N days")
    backfill_parser.add_argument(
        "--days",
        type=int,
        default=90,
        help="Number of past days to check for unalerted filings (default: 90)",
    )
    backfill_parser.add_argument(
        "--ticker",
        type=str,
        default=None,
        help="Optional specific ticker symbol to backfill (default: all watchlist tickers)",
    )

    # web command
    web_parser = subparsers.add_parser("web", help="Start web dashboard on localhost")
    web_parser.add_argument("--host", default="127.0.0.1", help="Host address (default: 127.0.0.1)")
    web_parser.add_argument("--port", type=int, default=8000, help="Port (default: 8000)")

    args = parser.parse_args()

    if args.command == "run":
        sys.exit(asyncio.run(cmd_run(args.config)))
    elif args.command == "add-ticker":
        sys.exit(asyncio.run(cmd_add_ticker(args.ticker, args.config)))
    elif args.command == "remove-ticker":
        sys.exit(asyncio.run(cmd_remove_ticker(args.ticker, args.config)))
    elif args.command == "test-alert":
        sys.exit(asyncio.run(cmd_test_alert(args.config)))
    elif args.command == "backfill":
        sys.exit(asyncio.run(cmd_backfill(args.days, args.config, args.ticker)))
    elif args.command == "web":
        from web_server import start_server
        start_server(args.host, args.port)


if __name__ == "__main__":
    main()
