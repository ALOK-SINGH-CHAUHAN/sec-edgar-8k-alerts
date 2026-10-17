"""FastAPI Web Server and Interactive Localhost Dashboard for SEC EDGAR 8-K Poller."""

import asyncio
from collections import deque
from datetime import datetime, timezone
import json
import logging
from pathlib import Path
import time
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

from fastapi import BackgroundTasks, FastAPI, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel
import uvicorn

from config import AppConfig, load_config
from edgar_client import EdgarClient, build_filing_index_url, normalize_ticker
from items import ParsedItem, get_form_description, is_high_priority_filing, parse_items
from main import cmd_add_ticker, cmd_backfill, cmd_remove_ticker, cmd_test_alert
from notifier import AlertMessage, Notifier, calculate_latency_seconds, parse_acceptance_datetime
from poller import EdgarPoller, is_market_fast_hours
from store import FilingStore

logger = logging.getLogger("edgar_alerts")

# In-memory log buffer for the web UI terminal
LOG_BUFFER: deque = deque(maxlen=200)


class InMemoryLogHandler(logging.Handler):
    def emit(self, record):
        try:
            msg = self.format(record)
            LOG_BUFFER.append(
                {
                    "timestamp": datetime.now().strftime("%H:%M:%S"),
                    "level": record.levelname,
                    "name": record.name,
                    "message": record.getMessage(),
                    "raw": msg,
                }
            )
        except Exception:
            pass


log_handler = InMemoryLogHandler()
log_handler.setFormatter(
    logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
)
logging.getLogger().addHandler(log_handler)

app = FastAPI(title="SEC EDGAR 8-K Alert Poller Dashboard")

# Global service references
GLOBAL_CONFIG: Optional[AppConfig] = None
GLOBAL_STORE: Optional[FilingStore] = None
GLOBAL_CLIENT: Optional[EdgarClient] = None
GLOBAL_NOTIFIER: Optional[Notifier] = None
GLOBAL_POLLER: Optional[EdgarPoller] = None
POLLER_TASK: Optional[asyncio.Task] = None

ET_TZ = ZoneInfo("America/New_York")
IST_TZ = ZoneInfo("Asia/Kolkata")


class AddTickerRequest(BaseModel):
    ticker: str


class RemoveTickerRequest(BaseModel):
    ticker: str


class BackfillRequest(BaseModel):
    days: int = 3


@app.on_event("startup")
async def startup_event():
    global GLOBAL_CONFIG, GLOBAL_STORE, GLOBAL_CLIENT, GLOBAL_NOTIFIER, GLOBAL_POLLER, POLLER_TASK
    try:
        GLOBAL_CONFIG = load_config("config.yaml")
        GLOBAL_STORE = FilingStore(GLOBAL_CONFIG.database_path)
        await GLOBAL_STORE.connect()

        GLOBAL_CLIENT = EdgarClient(config=GLOBAL_CONFIG.sec)
        # Preload tickers map
        await GLOBAL_CLIENT.get_company_tickers_map()

        GLOBAL_NOTIFIER = Notifier(GLOBAL_CONFIG)
        GLOBAL_POLLER = EdgarPoller(
            config=GLOBAL_CONFIG,
            store=GLOBAL_STORE,
            client=GLOBAL_CLIENT,
            notifier=GLOBAL_NOTIFIER,
        )

        # Launch background poller loop
        POLLER_TASK = asyncio.create_task(GLOBAL_POLLER.run())
        logger.info("Background EDGAR alert poller started with web dashboard.")
    except Exception as e:
        logger.error("Error during startup: %s", e, exc_info=True)


@app.on_event("shutdown")
async def shutdown_event():
    global GLOBAL_POLLER, GLOBAL_STORE, GLOBAL_CLIENT, GLOBAL_NOTIFIER, POLLER_TASK
    if GLOBAL_POLLER:
        GLOBAL_POLLER.stop()
    if POLLER_TASK:
        try:
            await asyncio.wait_for(POLLER_TASK, timeout=5.0)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            pass
    if GLOBAL_CLIENT:
        await GLOBAL_CLIENT.close()
    if GLOBAL_NOTIFIER:
        await GLOBAL_NOTIFIER.close()
    if GLOBAL_STORE:
        await GLOBAL_STORE.close()
    logger.info("Web server and poller shutdown complete.")


@app.get("/api/status")
async def get_status():
    if not GLOBAL_CONFIG or not GLOBAL_POLLER:
        return {"status": "starting"}

    is_fast = is_market_fast_hours(GLOBAL_CONFIG.poll)
    uptime_sec = int(time.monotonic() - GLOBAL_POLLER.start_time) if GLOBAL_POLLER.start_time else 0

    # Build rich watchlist info
    watchlist_info = []
    for ticker in GLOBAL_CONFIG.watchlist:
        cik = GLOBAL_CLIENT.resolve_ticker(ticker) if GLOBAL_CLIENT else None
        title = GLOBAL_CLIENT.cik_to_name.get(cik, "Unknown") if (GLOBAL_CLIENT and cik) else "Unknown"
        is_seeded = await GLOBAL_STORE.is_cik_seeded(cik) if (GLOBAL_STORE and cik) else False
        watchlist_info.append(
            {
                "ticker": ticker,
                "cik": cik,
                "company_name": title,
                "seeded": is_seeded,
            }
        )

    return {
        "running": GLOBAL_POLLER.running,
        "mode": "FAST (Market Hours)" if is_fast else "SLOW (Off-Hours)",
        "interval_seconds": (
            GLOBAL_CONFIG.poll.fast_interval_seconds if is_fast else GLOBAL_CONFIG.poll.slow_interval_seconds
        ),
        "uptime_seconds": uptime_sec,
        "cycles_completed": GLOBAL_POLLER.cycles_completed,
        "requests_made": GLOBAL_POLLER.requests_made,
        "alerts_sent": GLOBAL_POLLER.alerts_sent,
        "errors_count": GLOBAL_POLLER.errors_count,
        "watchlist": watchlist_info,
        "tracked_forms": GLOBAL_CONFIG.forms,
        "user_agent": GLOBAL_CONFIG.sec.user_agent,
        "channels": GLOBAL_CONFIG.alerts.channels,
        "quiet_hours_enabled": GLOBAL_CONFIG.quiet_hours.enabled,
    }


@app.get("/api/filings")
async def get_filings(limit: int = 50):
    if not GLOBAL_STORE:
        return []

    raw_filings = await GLOBAL_STORE.get_recent_filings(limit=limit)
    formatted = []

    for f in raw_filings:
        items_raw = f.get("items", "")
        parsed = parse_items(items_raw)
        is_high = is_high_priority_filing(f.get("form", ""), parsed)

        acc_dt = parse_acceptance_datetime(f.get("acceptance_datetime", ""))
        et_str = acc_dt.astimezone(ET_TZ).strftime("%Y-%m-%d %H:%M:%S ET") if acc_dt else ""
        ist_str = acc_dt.astimezone(IST_TZ).strftime("%Y-%m-%d %H:%M:%S IST") if acc_dt else ""
        latency = calculate_latency_seconds(acc_dt) if acc_dt else 0.0

        cik_int = int(f.get("cik", 0))
        acc_num = f.get("accession_number", "")
        index_url = build_filing_index_url(cik_int, acc_num)

        formatted.append(
            {
                "accession_number": acc_num,
                "cik": cik_int,
                "ticker": f.get("ticker", ""),
                "form": f.get("form", ""),
                "filing_date": f.get("filing_date", ""),
                "acceptance_datetime": f.get("acceptance_datetime", ""),
                "et_time": et_str,
                "ist_time": ist_str,
                "latency_seconds": latency,
                "items_raw": items_raw,
                "parsed_items": [
                    {"code": item.code, "description": item.description, "is_high_priority": item.is_high_priority}
                    for item in parsed
                ],
                "is_high_priority": is_high,
                "filing_url": index_url,
                "alert_status": f.get("alert_status", ""),
                "alerted_at": f.get("alerted_at", ""),
            }
        )

    return formatted


@app.get("/api/logs")
async def get_logs():
    return list(LOG_BUFFER)


@app.post("/api/watchlist/add")
async def add_ticker_endpoint(req: AddTickerRequest):
    ticker = req.ticker.strip().upper()
    if not ticker:
        raise HTTPException(status_code=400, detail="Ticker symbol cannot be empty")

    exit_code = await cmd_add_ticker(ticker, "config.yaml")
    if exit_code != 0:
        raise HTTPException(
            status_code=400,
            detail=f"Ticker '{ticker}' could not be resolved or added against SEC CIK map.",
        )

    # Reload global config
    global GLOBAL_CONFIG
    GLOBAL_CONFIG = load_config("config.yaml")
    if GLOBAL_POLLER:
        GLOBAL_POLLER.config = GLOBAL_CONFIG
    return {"status": "success", "message": f"Ticker '{ticker}' added to watchlist."}


@app.post("/api/watchlist/remove")
async def remove_ticker_endpoint(req: RemoveTickerRequest):
    ticker = req.ticker.strip().upper()
    exit_code = await cmd_remove_ticker(ticker, "config.yaml")
    if exit_code != 0:
        raise HTTPException(status_code=400, detail="Failed to remove ticker")

    global GLOBAL_CONFIG
    GLOBAL_CONFIG = load_config("config.yaml")
    if GLOBAL_POLLER:
        GLOBAL_POLLER.config = GLOBAL_CONFIG
    return {"status": "success", "message": f"Ticker '{ticker}' removed from watchlist."}


@app.post("/api/action/test-alert")
async def test_alert_endpoint():
    exit_code = await cmd_test_alert("config.yaml")
    return {"status": "success" if exit_code == 0 else "partial_failure"}


@app.post("/api/action/backfill")
async def backfill_endpoint(req: BackfillRequest, background_tasks: BackgroundTasks):
    days = max(1, min(req.days, 90))
    background_tasks.add_task(cmd_backfill, days, "config.yaml")
    return {"status": "success", "message": f"Backfill triggered for past {days} days in background."}


@app.get("/", response_class=HTMLResponse)
async def dashboard_html():
    return """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>SEC EDGAR 8-K Alert Poller • Terminal</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=Plus+Jakarta+Sans:wght@400;500;600;700;800&family=JetBrains+Mono:wght@400;500;600&display=swap" rel="stylesheet">
  <style>
    :root {
      --bg-primary: #090b10;
      --bg-surface: #11141c;
      --bg-surface-elevated: #181d29;
      --bg-glass: rgba(17, 20, 28, 0.75);
      --border-subtle: rgba(255, 255, 255, 0.08);
      --border-accent: rgba(99, 102, 241, 0.35);
      
      --text-main: #f3f4f6;
      --text-muted: #9ca3af;
      --text-dim: #6b7280;

      --accent-primary: #6366f1;
      --accent-cyan: #06b6d4;
      --accent-emerald: #10b981;
      --accent-rose: #f43f5e;
      --accent-amber: #f59e0b;
      
      --badge-red-bg: rgba(244, 63, 94, 0.15);
      --badge-red-border: rgba(244, 63, 94, 0.4);
      --badge-green-bg: rgba(16, 185, 129, 0.15);
      --badge-green-border: rgba(16, 185, 129, 0.4);
      --badge-blue-bg: rgba(99, 102, 241, 0.15);
      --badge-blue-border: rgba(99, 102, 241, 0.4);
      
      --radius-sm: 8px;
      --radius-md: 12px;
      --radius-lg: 18px;
      --font-sans: 'Plus Jakarta Sans', -apple-system, BlinkMacSystemFont, sans-serif;
      --font-mono: 'JetBrains Mono', monospace;
    }

    * {
      box-sizing: border-box;
      margin: 0;
      padding: 0;
    }

    body {
      background-color: var(--bg-primary);
      color: var(--text-main);
      font-family: var(--font-sans);
      min-height: 100vh;
      line-height: 1.5;
      background-image: 
        radial-gradient(circle at 15% 15%, rgba(99, 102, 241, 0.08) 0%, transparent 40%),
        radial-gradient(circle at 85% 25%, rgba(6, 182, 212, 0.06) 0%, transparent 45%),
        radial-gradient(circle at 50% 85%, rgba(16, 185, 129, 0.04) 0%, transparent 50%);
      background-attachment: fixed;
    }

    /* Top Navigation */
    header {
      background: var(--bg-glass);
      backdrop-filter: blur(16px);
      border-bottom: 1px solid var(--border-subtle);
      position: sticky;
      top: 0;
      z-index: 50;
      padding: 1rem 2rem;
    }

    .nav-container {
      max-width: 1500px;
      margin: 0 auto;
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: 1.5rem;
    }

    .brand {
      display: flex;
      align-items: center;
      gap: 0.85rem;
    }

    .brand-icon {
      width: 40px;
      height: 40px;
      border-radius: var(--radius-md);
      background: linear-gradient(135deg, #4f46e5, #06b6d4);
      display: flex;
      align-items: center;
      justify-content: center;
      box-shadow: 0 4px 16px rgba(79, 70, 229, 0.35);
      font-size: 1.25rem;
    }

    .brand-title {
      font-size: 1.25rem;
      font-weight: 800;
      letter-spacing: -0.02em;
      background: linear-gradient(120deg, #ffffff 40%, #a5b4fc);
      -webkit-background-clip: text;
      -webkit-text-fill-color: transparent;
    }

    .brand-tag {
      font-family: var(--font-mono);
      font-size: 0.72rem;
      background: rgba(255, 255, 255, 0.07);
      padding: 2px 8px;
      border-radius: 6px;
      color: var(--accent-cyan);
      border: 1px solid rgba(6, 182, 212, 0.25);
    }

    .header-actions {
      display: flex;
      align-items: center;
      gap: 0.75rem;
    }

    .btn {
      font-family: var(--font-sans);
      font-size: 0.85rem;
      font-weight: 600;
      padding: 0.5rem 1rem;
      border-radius: var(--radius-sm);
      border: 1px solid transparent;
      cursor: pointer;
      display: inline-flex;
      align-items: center;
      gap: 0.5rem;
      transition: all 0.2s cubic-bezier(0.16, 1, 0.3, 1);
    }

    .btn:hover {
      transform: translateY(-1px);
    }

    .btn-primary {
      background: linear-gradient(135deg, #4f46e5, #4338ca);
      color: white;
      box-shadow: 0 4px 12px rgba(79, 70, 229, 0.3);
    }
    .btn-primary:hover {
      box-shadow: 0 6px 16px rgba(79, 70, 229, 0.45);
    }

    .btn-secondary {
      background: var(--bg-surface-elevated);
      color: var(--text-main);
      border-color: var(--border-subtle);
    }
    .btn-secondary:hover {
      border-color: rgba(255, 255, 255, 0.2);
      background: #202738;
    }

    /* Main Container */
    main {
      max-width: 1500px;
      margin: 2rem auto;
      padding: 0 2rem;
      display: flex;
      flex-direction: column;
      gap: 2rem;
    }

    /* Metrics Grid */
    .metrics-grid {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(210px, 1fr));
      gap: 1.25rem;
    }

    .metric-card {
      background: var(--bg-surface);
      border: 1px solid var(--border-subtle);
      border-radius: var(--radius-md);
      padding: 1.25rem;
      position: relative;
      overflow: hidden;
      transition: border-color 0.2s;
    }
    .metric-card:hover {
      border-color: rgba(255, 255, 255, 0.18);
    }

    .metric-header {
      display: flex;
      justify-content: space-between;
      align-items: center;
      margin-bottom: 0.5rem;
    }

    .metric-label {
      font-size: 0.78rem;
      font-weight: 600;
      text-transform: uppercase;
      letter-spacing: 0.05em;
      color: var(--text-dim);
    }

    .metric-value {
      font-family: var(--font-mono);
      font-size: 1.75rem;
      font-weight: 700;
      color: #ffffff;
      letter-spacing: -0.03em;
    }

    .metric-sub {
      font-size: 0.75rem;
      color: var(--text-muted);
      margin-top: 0.25rem;
      display: flex;
      align-items: center;
      gap: 0.4rem;
    }

    .pulse-dot {
      width: 8px;
      height: 8px;
      border-radius: 50%;
      background: var(--accent-emerald);
      box-shadow: 0 0 10px var(--accent-emerald);
      animation: pulse 2s infinite;
    }

    @keyframes pulse {
      0% { opacity: 1; transform: scale(1); }
      50% { opacity: 0.4; transform: scale(0.85); }
      100% { opacity: 1; transform: scale(1); }
    }

    /* Content Layout */
    .content-layout {
      display: grid;
      grid-template-columns: 340px 1fr;
      gap: 2rem;
      align-items: start;
    }

    @media (max-width: 1080px) {
      .content-layout {
        grid-template-columns: 1fr;
      }
    }

    /* Panels */
    .panel {
      background: var(--bg-surface);
      border: 1px solid var(--border-subtle);
      border-radius: var(--radius-lg);
      padding: 1.5rem;
    }

    .panel-header {
      display: flex;
      justify-content: space-between;
      align-items: center;
      margin-bottom: 1.25rem;
      padding-bottom: 0.85rem;
      border-bottom: 1px solid var(--border-subtle);
    }

    .panel-title {
      font-size: 1.05rem;
      font-weight: 700;
      display: flex;
      align-items: center;
      gap: 0.5rem;
    }

    /* Watchlist Sidebar */
    .ticker-add-box {
      display: flex;
      gap: 0.5rem;
      margin-bottom: 1rem;
    }

    .input-text {
      flex: 1;
      background: var(--bg-surface-elevated);
      border: 1px solid var(--border-subtle);
      border-radius: var(--radius-sm);
      padding: 0.5rem 0.85rem;
      color: white;
      font-family: var(--font-mono);
      font-size: 0.85rem;
      outline: none;
      transition: border-color 0.2s;
    }

    .input-text:focus {
      border-color: var(--accent-primary);
    }

    .watchlist-list {
      display: flex;
      flex-direction: column;
      gap: 0.6rem;
      max-height: 480px;
      overflow-y: auto;
    }

    .watchlist-item {
      display: flex;
      justify-content: space-between;
      align-items: center;
      background: var(--bg-surface-elevated);
      border: 1px solid var(--border-subtle);
      padding: 0.75rem 1rem;
      border-radius: var(--radius-sm);
      transition: all 0.15s ease;
    }

    .watchlist-item:hover {
      border-color: rgba(255, 255, 255, 0.15);
      background: #1c2230;
    }

    .ticker-symbol {
      font-family: var(--font-mono);
      font-weight: 700;
      font-size: 1rem;
      color: #ffffff;
    }

    .ticker-meta {
      font-size: 0.75rem;
      color: var(--text-dim);
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
      max-width: 170px;
    }

    .btn-icon {
      background: transparent;
      border: none;
      color: var(--text-dim);
      cursor: pointer;
      padding: 4px;
      border-radius: 4px;
      transition: color 0.15s;
    }

    .btn-icon:hover {
      color: var(--accent-rose);
      background: rgba(244, 63, 94, 0.1);
    }

    /* Filings Table / Cards */
    .filings-list {
      display: flex;
      flex-direction: column;
      gap: 1rem;
    }

    .filing-card {
      background: var(--bg-surface-elevated);
      border: 1px solid var(--border-subtle);
      border-radius: var(--radius-md);
      padding: 1.25rem;
      transition: transform 0.15s, border-color 0.15s;
      position: relative;
    }

    .filing-card:hover {
      border-color: rgba(255, 255, 255, 0.2);
      transform: translateY(-1px);
    }

    .filing-card.high-priority {
      border-left: 4px solid var(--accent-rose);
      background: linear-gradient(90deg, rgba(244, 63, 94, 0.04) 0%, var(--bg-surface-elevated) 40%);
    }

    .filing-top {
      display: flex;
      justify-content: space-between;
      align-items: flex-start;
      margin-bottom: 0.75rem;
      gap: 1rem;
      flex-wrap: wrap;
    }

    .filing-identity {
      display: flex;
      align-items: center;
      gap: 0.65rem;
      flex-wrap: wrap;
    }

    .badge {
      font-family: var(--font-mono);
      font-size: 0.75rem;
      font-weight: 700;
      padding: 3px 8px;
      border-radius: 6px;
      display: inline-flex;
      align-items: center;
      gap: 0.35rem;
    }

    .badge-high {
      background: var(--badge-red-bg);
      color: #ff6b81;
      border: 1px solid var(--badge-red-border);
    }

    .badge-std {
      background: rgba(255, 255, 255, 0.05);
      color: var(--text-muted);
      border: 1px solid var(--border-subtle);
    }

    .badge-form {
      background: var(--badge-blue-bg);
      color: #a5b4fc;
      border: 1px solid var(--badge-blue-border);
    }

    .badge-latency {
      background: rgba(16, 185, 129, 0.1);
      color: var(--accent-emerald);
      border: 1px solid rgba(16, 185, 129, 0.3);
    }

    .filing-ticker {
      font-family: var(--font-mono);
      font-size: 1.15rem;
      font-weight: 800;
      color: #ffffff;
    }

    .filing-company {
      color: var(--text-muted);
      font-size: 0.9rem;
      font-weight: 500;
    }

    .filing-items {
      display: flex;
      flex-direction: column;
      gap: 0.35rem;
      margin: 0.75rem 0;
      padding-left: 0.25rem;
    }

    .filing-item-line {
      font-size: 0.88rem;
      color: #e5e7eb;
      display: flex;
      align-items: center;
      gap: 0.5rem;
    }

    .item-bullet {
      color: var(--accent-cyan);
      font-size: 0.75rem;
    }

    .item-code {
      font-family: var(--font-mono);
      font-weight: 700;
      color: #cbd5e1;
    }

    .filing-footer {
      display: flex;
      justify-content: space-between;
      align-items: center;
      font-size: 0.78rem;
      color: var(--text-dim);
      border-top: 1px solid rgba(255, 255, 255, 0.05);
      padding-top: 0.75rem;
      margin-top: 0.75rem;
      flex-wrap: wrap;
      gap: 0.5rem;
    }

    .filing-timestamps {
      font-family: var(--font-mono);
      color: var(--text-muted);
    }

    .link-sec {
      color: var(--accent-cyan);
      text-decoration: none;
      font-weight: 600;
      display: inline-flex;
      align-items: center;
      gap: 0.3rem;
    }

    .link-sec:hover {
      text-decoration: underline;
    }

    /* Terminal Console */
    .terminal-panel {
      background: #08090d;
      border: 1px solid var(--border-subtle);
      border-radius: var(--radius-lg);
      padding: 1.25rem;
      font-family: var(--font-mono);
      font-size: 0.78rem;
    }

    .terminal-body {
      max-height: 220px;
      overflow-y: auto;
      display: flex;
      flex-direction: column;
      gap: 0.25rem;
      color: #cbd5e1;
    }

    .log-line {
      display: flex;
      gap: 0.75rem;
      word-break: break-all;
    }

    .log-time {
      color: var(--text-dim);
      flex-shrink: 0;
    }

    .log-level {
      font-weight: 700;
      flex-shrink: 0;
      width: 48px;
    }
    .log-level.INFO { color: var(--accent-cyan); }
    .log-level.WARNING { color: var(--accent-amber); }
    .log-level.ERROR { color: var(--accent-rose); }

    .empty-state {
      text-align: center;
      padding: 3rem 1rem;
      color: var(--text-dim);
    }
  </style>
</head>
<body>

  <!-- Top Navigation -->
  <header>
    <div class="nav-container">
      <div class="brand">
        <div class="brand-icon">⚡</div>
        <div>
          <div style="display: flex; align-items: center; gap: 0.5rem;">
            <span class="brand-title">EDGAR 8-K Poller</span>
            <span class="brand-tag">REAL-TIME</span>
          </div>
          <div style="font-size: 0.72rem; color: var(--text-dim);">SEC Fair-Access Compliant Alert Engine</div>
        </div>
      </div>

      <div class="header-actions">
        <button class="btn btn-secondary" onclick="triggerTestAlert()">🔔 Test Alert</button>
        <button class="btn btn-secondary" onclick="triggerBackfill()">↺ Backfill (7d)</button>
        <button class="btn btn-primary" onclick="refreshData()">⚡ Refresh Now</button>
      </div>
    </div>
  </header>

  <!-- Main Content -->
  <main>

    <!-- Metrics Cards -->
    <div class="metrics-grid">
      <div class="metric-card">
        <div class="metric-header">
          <span class="metric-label">Status</span>
          <div class="pulse-dot"></div>
        </div>
        <div class="metric-value" id="val-status">ACTIVE</div>
        <div class="metric-sub" id="sub-mode">Mode: FAST (10s interval)</div>
      </div>

      <div class="metric-card">
        <div class="metric-header">
          <span class="metric-label">Uptime</span>
        </div>
        <div class="metric-value" id="val-uptime">0m</div>
        <div class="metric-sub" id="sub-cycles">0 cycles completed</div>
      </div>

      <div class="metric-card">
        <div class="metric-header">
          <span class="metric-label">Alerts Sent</span>
        </div>
        <div class="metric-value" id="val-alerts" style="color: #a5b4fc;">0</div>
        <div class="metric-sub">Across all channels</div>
      </div>

      <div class="metric-card">
        <div class="metric-header">
          <span class="metric-label">SEC Requests</span>
        </div>
        <div class="metric-value" id="val-requests" style="color: var(--accent-cyan);">0</div>
        <div class="metric-sub">Max rate: 8.0 req/s</div>
      </div>

      <div class="metric-card">
        <div class="metric-header">
          <span class="metric-label">Watchlist</span>
        </div>
        <div class="metric-value" id="val-watchlist-count">0</div>
        <div class="metric-sub">Unique corporate CIKs</div>
      </div>
    </div>

    <!-- Content Layout: Watchlist + Filings Feed -->
    <div class="content-layout">

      <!-- Left Column: Watchlist Manager -->
      <div class="panel">
        <div class="panel-header">
          <span class="panel-title">📈 Watchlist</span>
          <span class="badge badge-std" id="badge-ticker-count">0 Tickers</span>
        </div>

        <div class="ticker-add-box">
          <input type="text" id="new-ticker-input" class="input-text" placeholder="Add Ticker (e.g. NVDA)" onkeydown="if(event.key==='Enter') addTicker()">
          <button class="btn btn-primary" onclick="addTicker()">+ Add</button>
        </div>

        <div class="watchlist-list" id="watchlist-container">
          <!-- Tickers rendered here -->
        </div>
      </div>

      <!-- Right Column: Live Filings Feed -->
      <div class="panel">
        <div class="panel-header">
          <span class="panel-title">🚨 Recent Filings & 8-K Alerts</span>
          <span style="font-size: 0.8rem; color: var(--text-dim); font-family: var(--font-mono);">Dual ET / IST Timestamps</span>
        </div>

        <div class="filings-list" id="filings-container">
          <div class="empty-state">Loading recent SEC corporate filings...</div>
        </div>
      </div>

    </div>

    <!-- Live Terminal / Logs Panel -->
    <div class="terminal-panel">
      <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 0.75rem;">
        <span style="font-weight: 700; color: var(--text-dim); text-transform: uppercase;">📡 Poller Activity Stream</span>
        <span style="font-size: 0.7rem; color: var(--text-dim);">Live In-Memory Buffer</span>
      </div>
      <div class="terminal-body" id="terminal-body">
        <div style="color: var(--text-dim);">Connecting to poller activity stream...</div>
      </div>
    </div>

  </main>

  <script>
    async function fetchStatus() {
      try {
        const res = await fetch('/api/status');
        const data = await res.json();
        
        document.getElementById('val-status').innerText = data.running ? 'ACTIVE' : 'IDLE';
        document.getElementById('sub-mode').innerText = `Mode: ${data.mode}`;
        
        // Format uptime
        const mins = Math.floor(data.uptime_seconds / 60);
        const hrs = Math.floor(mins / 60);
        document.getElementById('val-uptime').innerText = hrs > 0 ? `${hrs}h ${mins%60}m` : `${mins}m`;
        document.getElementById('sub-cycles').innerText = `${data.cycles_completed} cycles completed`;
        
        document.getElementById('val-alerts').innerText = data.alerts_sent;
        document.getElementById('val-requests').innerText = data.requests_made;
        document.getElementById('val-watchlist-count').innerText = data.watchlist.length;
        document.getElementById('badge-ticker-count').innerText = `${data.watchlist.length} Tickers`;

        // Render watchlist
        const wlContainer = document.getElementById('watchlist-container');
        wlContainer.innerHTML = '';
        data.watchlist.forEach(item => {
          const div = document.createElement('div');
          div.className = 'watchlist-item';
          div.innerHTML = `
            <div>
              <div class="ticker-symbol">${item.ticker}</div>
              <div class="ticker-meta" title="${item.company_name}">${item.company_name}</div>
            </div>
            <div style="display: flex; align-items: center; gap: 0.5rem;">
              <span class="badge ${item.seeded ? 'badge-std' : 'badge-high'}" style="font-size: 0.65rem;">
                ${item.seeded ? 'SEEDED' : 'NEW'}
              </span>
              <button class="btn-icon" title="Remove ${item.ticker}" onclick="removeTicker('${item.ticker}')">✕</button>
            </div>
          `;
          wlContainer.appendChild(div);
        });

      } catch (e) {
        console.error("Status fetch error", e);
      }
    }

    async function fetchFilings() {
      try {
        const res = await fetch('/api/filings?limit=50');
        const filings = await res.json();
        const container = document.getElementById('filings-container');
        
        if (!filings || filings.length === 0) {
          container.innerHTML = '<div class="empty-state">No filings recorded yet. Run a Backfill or wait for the next market cycle.</div>';
          return;
        }

        container.innerHTML = '';
        filings.forEach(f => {
          const card = document.createElement('div');
          card.className = `filing-card ${f.is_high_priority ? 'high-priority' : ''}`;

          let itemsHtml = '';
          if (f.parsed_items && f.parsed_items.length > 0) {
            itemsHtml = f.parsed_items.map(it => `
              <div class="filing-item-line">
                <span class="item-bullet">▸</span>
                <span class="item-code">${it.code}</span>
                <span>${it.description}</span>
                ${it.is_high_priority ? '<span class="badge badge-high" style="font-size: 0.65rem;">CATALYST</span>' : ''}
              </div>
            `).join('');
          } else if (f.items_raw) {
            itemsHtml = `<div class="filing-item-line">• Items: ${f.items_raw}</div>`;
          }

          card.innerHTML = `
            <div class="filing-top">
              <div class="filing-identity">
                <span class="filing-ticker">${f.ticker}</span>
                <span class="filing-company">(${f.form})</span>
                <span class="badge ${f.is_high_priority ? 'badge-high' : 'badge-std'}">
                  ${f.is_high_priority ? '🚨 HIGH PRIORITY' : '📄 STANDARD'}
                </span>
                <span class="badge badge-latency">${f.latency_seconds}s Latency</span>
              </div>
              <a href="${f.filing_url}" target="_blank" rel="noopener" class="link-sec">
                View on EDGAR ↗
              </a>
            </div>

            <div class="filing-items">
              ${itemsHtml}
            </div>

            <div class="filing-footer">
              <div class="filing-timestamps">
                <strong>Accepted:</strong> ${f.et_time} &bull; <strong>IST:</strong> ${f.ist_time}
              </div>
              <div style="font-family: var(--font-mono); color: var(--text-dim); font-size: 0.72rem;">
                ${f.accession_number}
              </div>
            </div>
          `;
          container.appendChild(card);
        });

      } catch (e) {
        console.error("Filings fetch error", e);
      }
    }

    async function fetchLogs() {
      try {
        const res = await fetch('/api/logs');
        const logs = await res.json();
        const term = document.getElementById('terminal-body');
        if (!logs || logs.length === 0) return;

        term.innerHTML = logs.slice(-50).map(l => `
          <div class="log-line">
            <span class="log-time">${l.timestamp}</span>
            <span class="log-level ${l.level}">[${l.level}]</span>
            <span>${l.message}</span>
          </div>
        `).join('');

        term.scrollTop = term.scrollHeight;
      } catch (e) {
        console.error("Logs fetch error", e);
      }
    }

    async function addTicker() {
      const input = document.getElementById('new-ticker-input');
      const val = input.value.trim().toUpperCase();
      if (!val) return;

      input.disabled = true;
      try {
        const res = await fetch('/api/watchlist/add', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ ticker: val })
        });
        const data = await res.json();
        if (res.ok) {
          input.value = '';
          refreshData();
        } else {
          alert(data.detail || "Error adding ticker");
        }
      } catch (e) {
        alert("Failed to communicate with server");
      } finally {
        input.disabled = false;
      }
    }

    async function removeTicker(ticker) {
      if (!confirm(`Remove ${ticker} from watchlist?`)) return;
      try {
        await fetch('/api/watchlist/remove', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ ticker: ticker })
        });
        refreshData();
      } catch (e) {
        alert("Error removing ticker");
      }
    }

    async function triggerTestAlert() {
      try {
        await fetch('/api/action/test-alert', { method: 'POST' });
        alert("Simulated test alert dispatched across configured channels!");
        refreshData();
      } catch (e) {
        alert("Test alert failed");
      }
    }

    async function triggerBackfill() {
      try {
        await fetch('/api/action/backfill', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ days: 7 })
        });
        alert("Backfill triggered for past 7 days in background!");
        setTimeout(refreshData, 2000);
      } catch (e) {
        alert("Backfill trigger failed");
      }
    }

    function refreshData() {
      fetchStatus();
      fetchFilings();
      fetchLogs();
    }

    // Initial load & periodic poll
    refreshData();
    setInterval(fetchStatus, 4000);
    setInterval(fetchFilings, 6000);
    setInterval(fetchLogs, 3000);
  </script>
</body>
</html>
"""


def start_server(host: str = "127.0.0.1", port: int = 8000):
    """Run uvicorn server serving the interactive dashboard."""
    uvicorn.run("web_server:app", host=host, port=port, log_level="info", reload=False)


if __name__ == "__main__":
    start_server()
