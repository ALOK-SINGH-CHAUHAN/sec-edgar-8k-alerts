"""Async SQLite storage layer for EDGAR filings, deduplication, and alert queues."""

from datetime import datetime, timezone
import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional
import aiosqlite

logger = logging.getLogger(__name__)


class FilingStore:
    """SQLite data store using WAL mode for concurrent, atomic filing tracking."""

    def __init__(self, db_path: str | Path = "data/edgar_alerts.db"):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn: Optional[aiosqlite.Connection] = None

    async def connect(self) -> aiosqlite.Connection:
        """Establish connection with WAL mode enabled and initialize schema."""
        if self._conn is None:
            self._conn = await aiosqlite.connect(str(self.db_path))
            self._conn.row_factory = aiosqlite.Row
            # Enable WAL mode for high concurrency and write safety
            await self._conn.execute("PRAGMA journal_mode=WAL;")
            await self._conn.execute("PRAGMA synchronous=NORMAL;")
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

    async def init_db(self) -> None:
        """Create database tables and indices if they do not exist."""
        conn = self._conn or await self.connect()

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
        await conn.execute(
            "INSERT OR REPLACE INTO seeded_ciks (cik, seeded_at) VALUES (?, ?);",
            (cik, now),
        )
        await conn.commit()

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
        Also marks the CIK as seeded.
        """
        conn = self._conn or await self.connect()
        now = datetime.now(timezone.utc).isoformat()
        ticker_str = ", ".join(tickers) if tickers else str(cik)
        count = 0

        for f in filings:
            acc = f.get("accession_number", "").strip()
            if not acc:
                continue
            try:
                await conn.execute(
                    """
                    INSERT OR IGNORE INTO seen_filings (
                        accession_number, cik, ticker, form, filing_date,
                        acceptance_datetime, items, primary_document,
                        first_seen_at, alerted_at, alert_status
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'seeded');
                    """,
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
                    ),
                )
                count += 1
            except Exception as e:
                logger.error("Error seeding filing %s: %s", acc, e)

        await self.mark_cik_seeded(cik)
        await conn.commit()
        return count

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

        try:
            async with conn.execute(
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
            ):
                await conn.commit()
                return True
        except aiosqlite.IntegrityError:
            # Already seen
            return False

    async def mark_filing_alerted(
        self, accession_number: str, status: str = "sent"
    ) -> None:
        """Update filing status after sending alert."""
        conn = self._conn or await self.connect()
        now = datetime.now(timezone.utc).isoformat()
        await conn.execute(
            """
            UPDATE seen_filings
            SET alert_status = ?, alerted_at = ?
            WHERE accession_number = ?;
            """,
            (status, now, accession_number.strip()),
        )
        await conn.commit()

    async def enqueue_pending_alert(
        self, accession_number: str, payload: Dict[str, Any]
    ) -> None:
        """Enqueue alert for quiet-hours or send retry."""
        conn = self._conn or await self.connect()
        now = datetime.now(timezone.utc).isoformat()
        payload_str = json.dumps(payload)
        await conn.execute(
            """
            INSERT OR REPLACE INTO pending_alerts (
                accession_number, payload_json, enqueued_at, retry_count
            ) VALUES (?, ?, ?, 0);
            """,
            (accession_number.strip(), payload_str, now),
        )
        await conn.commit()

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
        await conn.execute(
            "DELETE FROM pending_alerts WHERE accession_number = ?;",
            (accession_number.strip(),),
        )
        await conn.commit()

    async def increment_pending_retry(
        self, accession_number: str, error_msg: str
    ) -> None:
        """Increment retry count and record error on failure."""
        conn = self._conn or await self.connect()
        await conn.execute(
            """
            UPDATE pending_alerts
            SET retry_count = retry_count + 1, last_error = ?
            WHERE accession_number = ?;
            """,
            (error_msg, accession_number.strip()),
        )
        await conn.commit()

    async def get_recent_filings(self, limit: int = 50) -> List[Dict[str, Any]]:
        """Retrieve most recently accepted filings ordered by acceptance_datetime descending."""
        conn = self._conn or await self.connect()
        async with conn.execute(
            """
            SELECT accession_number, cik, ticker, form, filing_date,
                   acceptance_datetime, items, primary_document,
                   first_seen_at, alerted_at, alert_status
            FROM seen_filings
            ORDER BY acceptance_datetime DESC
            LIMIT ?;
            """,
            (limit,),
        ) as cursor:
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
        await conn.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?);", (key, value)
        )
        await conn.commit()
