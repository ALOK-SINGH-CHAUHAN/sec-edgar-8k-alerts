"""Async SQLite storage layer for EDGAR filings, deduplication, and alert queues."""

import asyncio
from datetime import datetime, timezone
import json
import logging
from pathlib import Path
import random
import sqlite3
from typing import Any, Dict, List, Optional
import aiosqlite

logger = logging.getLogger(__name__)


class FilingStore:
    """SQLite data store using WAL mode for concurrent, atomic filing tracking."""

    def __init__(self, db_path: str | Path = "data/edgar_alerts.db"):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn: Optional[aiosqlite.Connection] = None
        self._write_lock = asyncio.Lock()

    async def connect(self) -> aiosqlite.Connection:
        """Establish connection with WAL mode, busy timeout, and schema initialization."""
        if self._conn is None:
            # 60s timeout in sqlite3 driver to prevent fast 'database is locked' errors
            self._conn = await aiosqlite.connect(str(self.db_path), timeout=60.0)
            self._conn.row_factory = aiosqlite.Row
            # Enable WAL mode for high concurrency
            await self._conn.execute("PRAGMA journal_mode=WAL;")
            await self._conn.execute("PRAGMA synchronous=NORMAL;")
            # 60,000 ms busy timeout so SQLite waits automatically if another writer is active
            await self._conn.execute("PRAGMA busy_timeout=60000;")
            await self.init_db()
        return self._conn

    async def close(self) -> None:
        """Close SQLite connection cleanly."""
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    async def __aenter__(self) -> "FilingStore":
        await self.connect()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        await self.close()

    async def _execute_with_retry(self, func, max_retries: int = 5):
        """Execute a database mutating callable with exponential backoff on lock contention."""
        attempt = 0
        while True:
            try:
                async with self._write_lock:
                    return await func()
            except (sqlite3.OperationalError, aiosqlite.OperationalError) as e:
                err_str = str(e).lower()
                if ("locked" in err_str or "busy" in err_str) and attempt < max_retries:
                    attempt += 1
                    delay = 0.05 * (2**attempt) + random.uniform(0.02, 0.1)
                    logger.warning(
                        "SQLite busy/locked. Backing off for %.2fs (attempt %d/%d)...",
                        delay,
                        attempt,
                        max_retries,
                    )
                    await asyncio.sleep(delay)
                else:
                    raise

    async def init_db(self) -> None:
        """Create database tables and indices if they do not exist."""
        conn = self._conn or await self.connect()

        async def _do_init():
            # 1. Seen filings table
            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS seen_filings (
                    accession_number TEXT PRIMARY KEY,
                    cik INTEGER NOT NULL,
                    ticker TEXT NOT NULL,
                    form TEXT NOT NULL,
                    filing_date TEXT NOT NULL,
                    acceptance_datetime TEXT NOT NULL,
                    items TEXT,
                    primary_document TEXT,
                    first_seen_at TEXT NOT NULL,
                    alerted_at TEXT,
                    alert_status TEXT NOT NULL DEFAULT 'pending'
                );
                """
            )
            await conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_seen_filings_cik ON seen_filings(cik);"
            )

            # 2. Seeded CIKs table (for silent initial seeding per CIK)
            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS seeded_ciks (
                    cik INTEGER PRIMARY KEY,
                    seeded_at TEXT NOT NULL
                );
                """
            )

            # 3. Metadata key-value table
            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                """
            )

            # 4. Pending alerts queue (for quiet hours & retries)
            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS pending_alerts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    accession_number TEXT UNIQUE NOT NULL,
                    payload_json TEXT NOT NULL,
                    enqueued_at TEXT NOT NULL,
                    retry_count INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT
                );
                """
            )

            await conn.commit()

        await self._execute_with_retry(_do_init)

    async def is_cik_seeded(self, cik: int) -> bool:
        """Check if a CIK has already performed its initial silent seeding."""
        conn = self._conn or await self.connect()
        async with conn.execute(
            "SELECT 1 FROM seeded_ciks WHERE cik = ? LIMIT 1;", (cik,)
        ) as cursor:
            row = await cursor.fetchone()
            return row is not None

    async def mark_cik_seeded(self, cik: int) -> None:
        """Mark a CIK as seeded."""
        conn = self._conn or await self.connect()
        now = datetime.now(timezone.utc).isoformat()

        async def _do_mark():
            await conn.execute(
                "INSERT OR REPLACE INTO seeded_ciks (cik, seeded_at) VALUES (?, ?);",
                (cik, now),
            )
            await conn.commit()

        await self._execute_with_retry(_do_mark)

    async def is_filing_seen(self, accession_number: str) -> bool:
        """Check if accession number has already been recorded in seen_filings."""
        conn = self._conn or await self.connect()
        async with conn.execute(
            "SELECT 1 FROM seen_filings WHERE accession_number = ? LIMIT 1;",
            (accession_number.strip(),),
        ) as cursor:
            row = await cursor.fetchone()
            return row is not None

    async def seed_filings(
        self, cik: int, filings: List[Dict[str, Any]], tickers: List[str]
    ) -> int:
        """
        Record existing filings silently with alert_status='seeded' (produces zero alerts).
        Uses atomic executemany batch insertion to prevent transaction locking.
        """
        conn = self._conn or await self.connect()
        now = datetime.now(timezone.utc).isoformat()
        ticker_str = ", ".join(tickers) if tickers else str(cik)

        records = []
        for f in filings:
            acc = f.get("accession_number", "").strip()
            if not acc:
                continue
            records.append(
                (
                    acc,
                    cik,
                    ticker_str,
                    f.get("form", ""),
                    f.get("filing_date", ""),
                    f.get("acceptance_datetime", ""),
                    f.get("items", ""),
                    f.get("primary_document", ""),
                    now,
                    now,
                )
            )

        if not records:
            await self.mark_cik_seeded(cik)
            return 0

        async def _do_seed():
            await conn.executemany(
                """
                INSERT OR IGNORE INTO seen_filings (
                    accession_number, cik, ticker, form, filing_date,
                    acceptance_datetime, items, primary_document,
                    first_seen_at, alerted_at, alert_status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'seeded');
                """,
                records,
            )
            await conn.execute(
                "INSERT OR REPLACE INTO seeded_ciks (cik, seeded_at) VALUES (?, ?);",
                (cik, now),
            )
            await conn.commit()
            return len(records)

        return await self._execute_with_retry(_do_seed)

    async def record_new_filing(
        self, filing: Dict[str, Any], tickers: List[str]
    ) -> bool:
        """
        Insert-before-alert pattern: atomically insert filing with alert_status='pending'.
        Returns True if newly inserted, False if already present (deduplication).
        """
        conn = self._conn or await self.connect()
        acc = filing.get("accession_number", "").strip()
        if not acc:
            return False

        now = datetime.now(timezone.utc).isoformat()
        ticker_str = ", ".join(tickers) if tickers else str(filing.get("cik", ""))

        async def _do_record():
            try:
                await conn.execute(
                    """
                    INSERT INTO seen_filings (
                        accession_number, cik, ticker, form, filing_date,
                        acceptance_datetime, items, primary_document,
                        first_seen_at, alert_status
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending');
                    """,
                    (
                        acc,
                        int(filing.get("cik", 0)),
                        ticker_str,
                        filing.get("form", ""),
                        filing.get("filing_date", ""),
                        filing.get("acceptance_datetime", ""),
                        filing.get("items", ""),
                        filing.get("primary_document", ""),
                        now,
                    ),
                )
                await conn.commit()
                return True
            except (sqlite3.IntegrityError, aiosqlite.IntegrityError):
                return False

        return await self._execute_with_retry(_do_record)

    async def mark_filing_alerted(
        self, accession_number: str, status: str = "sent"
    ) -> None:
        """Update filing status after sending alert."""
        conn = self._conn or await self.connect()
        now = datetime.now(timezone.utc).isoformat()

        async def _do_mark():
            await conn.execute(
                """
                UPDATE seen_filings
                SET alert_status = ?, alerted_at = ?
                WHERE accession_number = ?;
                """,
                (status, now, accession_number.strip()),
            )
            await conn.commit()

        await self._execute_with_retry(_do_mark)

    async def enqueue_pending_alert(
        self, accession_number: str, payload: Dict[str, Any]
    ) -> None:
        """Enqueue alert for quiet-hours or send retry."""
        conn = self._conn or await self.connect()
        now = datetime.now(timezone.utc).isoformat()
        payload_str = json.dumps(payload)

        async def _do_enqueue():
            await conn.execute(
                """
                INSERT OR REPLACE INTO pending_alerts (
                    accession_number, payload_json, enqueued_at, retry_count
                ) VALUES (?, ?, ?, 0);
                """,
                (accession_number.strip(), payload_str, now),
            )
            await conn.commit()

        await self._execute_with_retry(_do_enqueue)

    async def get_pending_alerts(self, limit: int = 50) -> List[Dict[str, Any]]:
        """Retrieve queue of pending alerts."""
        conn = self._conn or await self.connect()
        async with conn.execute(
            """
            SELECT id, accession_number, payload_json, enqueued_at, retry_count, last_error
            FROM pending_alerts
            ORDER BY id ASC
            LIMIT ?;
            """,
            (limit,),
        ) as cursor:
            rows = await cursor.fetchall()
            results = []
            for row in rows:
                try:
                    payload = json.loads(row["payload_json"])
                except Exception:
                    payload = {}
                results.append(
                    {
                        "id": row["id"],
                        "accession_number": row["accession_number"],
                        "payload": payload,
                        "enqueued_at": row["enqueued_at"],
                        "retry_count": row["retry_count"],
                        "last_error": row["last_error"],
                    }
                )
            return results

    async def delete_pending_alert(self, accession_number: str) -> None:
        """Remove successfully delivered alert from queue."""
        conn = self._conn or await self.connect()

        async def _do_delete():
            await conn.execute(
                "DELETE FROM pending_alerts WHERE accession_number = ?;",
                (accession_number.strip(),),
            )
            await conn.commit()

        await self._execute_with_retry(_do_delete)

    async def increment_pending_retry(
        self, accession_number: str, error_msg: str
    ) -> None:
        """Increment retry count and record error on failure."""
        conn = self._conn or await self.connect()

        async def _do_inc():
            await conn.execute(
                """
                UPDATE pending_alerts
                SET retry_count = retry_count + 1, last_error = ?
                WHERE accession_number = ?;
                """,
                (error_msg, accession_number.strip()),
            )
            await conn.commit()

        await self._execute_with_retry(_do_inc)

    async def is_filing_alerted(self, accession_number: str) -> bool:
        """Check if accession number has already been alerted (alert_status == 'sent')."""
        conn = self._conn or await self.connect()
        async with conn.execute(
            "SELECT 1 FROM seen_filings WHERE accession_number = ? AND alert_status = 'sent' LIMIT 1;",
            (accession_number.strip(),),
        ) as cursor:
            row = await cursor.fetchone()
            return row is not None

    async def get_recent_filings(
        self,
        forms: Optional[List[str]] = None,
        ticker: Optional[str] = None,
        limit: int = 50,
    ) -> List[Dict[str, Any]]:
        """Retrieve most recently accepted filings filtered by tracked forms and optional ticker."""
        conn = self._conn or await self.connect()
        clauses = []
        params: List[Any] = []

        if forms:
            placeholders = ",".join("?" for _ in forms)
            clauses.append(f"form IN ({placeholders})")
            params.extend(forms)
        else:
            clauses.append("(form LIKE '8-K%')")

        if ticker and ticker.strip().upper() != "ALL":
            clean_ticker = ticker.strip().upper()
            clauses.append("(ticker = ? OR ticker LIKE ?)")
            params.extend([clean_ticker, f"%{clean_ticker}%"])

        where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        query = f"""
            SELECT accession_number, cik, ticker, form, filing_date,
                   acceptance_datetime, items, primary_document,
                   first_seen_at, alerted_at, alert_status
            FROM seen_filings
            {where_sql}
            ORDER BY acceptance_datetime DESC
            LIMIT ?;
        """
        params.append(limit)

        async with conn.execute(query, tuple(params)) as cursor:
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]

    async def get_meta(self, key: str, default: Optional[str] = None) -> Optional[str]:
        """Fetch value from meta key-value store."""
        conn = self._conn or await self.connect()
        async with conn.execute(
            "SELECT value FROM meta WHERE key = ? LIMIT 1;", (key,)
        ) as cursor:
            row = await cursor.fetchone()
            return row["value"] if row else default

    async def set_meta(self, key: str, value: str) -> None:
        """Store key-value in meta store."""
        conn = self._conn or await self.connect()

        async def _do_set():
            await conn.execute(
                "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?);",
                (key, value),
            )
            await conn.commit()

        await self._execute_with_retry(_do_set)

    async def get_watchlist_stats(self, watchlist: List[str]) -> Dict[str, Dict[str, Any]]:
        """Return counts of total 8-Ks, alerted 8-Ks, and latest filing date per ticker."""
        conn = self._conn or await self.connect()
        stats: Dict[str, Dict[str, Any]] = {}
        for ticker in watchlist:
            clean_ticker = ticker.strip().upper()
            async with conn.execute(
                """
                SELECT count(*) as total_8k,
                       sum(CASE WHEN alert_status = 'sent' THEN 1 ELSE 0 END) as alerted_count,
                       max(filing_date) as latest_date
                FROM seen_filings
                WHERE (ticker = ? OR ticker LIKE ?) AND form LIKE '8-K%';
                """,
                (clean_ticker, f"%{clean_ticker}%"),
            ) as cursor:
                row = await cursor.fetchone()
                if row:
                    stats[clean_ticker] = {
                        "total_8k": row["total_8k"] or 0,
                        "alerted_count": row["alerted_count"] or 0,
                        "latest_date": row["latest_date"] or None,
                    }
                else:
                    stats[clean_ticker] = {"total_8k": 0, "alerted_count": 0, "latest_date": None}
        return stats
