"""SQLite storage for runs, raw facts and finished leads.

The crawl queue's SQL lives in crawler/frontier.py. All database calls happen on the
event-loop thread, so workers can't interleave inside a transaction.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from datetime import datetime, timezone
from pathlib import Path

from models import LEAD_COLUMNS, Fact

_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY,
    status TEXT NOT NULL,
    source TEXT NOT NULL,
    config_json TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT
);
CREATE TABLE IF NOT EXISTS domains (
    run_id INTEGER NOT NULL REFERENCES runs(id),
    domain TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    reason TEXT,
    region TEXT,
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    renders INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (run_id, domain)
);
CREATE TABLE IF NOT EXISTS pages (
    id INTEGER PRIMARY KEY,
    run_id INTEGER NOT NULL REFERENCES runs(id),
    domain TEXT NOT NULL,
    url TEXT NOT NULL,
    url_key TEXT NOT NULL,
    depth INTEGER NOT NULL,
    priority INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    http_status INTEGER,
    error TEXT,
    rendered INTEGER NOT NULL DEFAULT 0,
    discovered_from TEXT,
    updated_at TEXT,
    UNIQUE (run_id, url_key)
);
CREATE INDEX IF NOT EXISTS pages_next ON pages (run_id, domain, status, priority DESC, depth, id);
CREATE TABLE IF NOT EXISTS facts (
    id INTEGER PRIMARY KEY,
    run_id INTEGER NOT NULL REFERENCES runs(id),
    domain TEXT NOT NULL,
    field TEXT NOT NULL,
    value TEXT NOT NULL,
    source_url TEXT NOT NULL,
    method TEXT NOT NULL,
    confidence REAL NOT NULL,
    UNIQUE (run_id, domain, field, value, source_url)
);
CREATE TABLE IF NOT EXISTS leads (
    run_id INTEGER NOT NULL REFERENCES runs(id),
    {", ".join(f"{column} {'INTEGER' if column == 'quality_score' else 'TEXT'}" for column in LEAD_COLUMNS)},
    PRIMARY KEY (run_id, domain)
);
"""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Database:
    def __init__(self, path: str | Path):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path, timeout=30)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("PRAGMA synchronous = NORMAL")
        self.conn.executescript(_SCHEMA)
        self._add_missing_columns()

    def _add_missing_columns(self) -> None:
        """Bring a database made by an older version up to date, keeping its data."""
        wanted = {"domains": ["region"], "leads": list(LEAD_COLUMNS)}
        with self.conn:
            for table, columns in wanted.items():
                existing = {row["name"] for row in self.conn.execute(f"PRAGMA table_info({table})")}
                for column in columns:
                    if column not in existing:
                        self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} TEXT")

    def close(self) -> None:
        self.conn.close()

    # Runs

    def create_run(self, source: str, config_json: str) -> int:
        with self.conn:
            cursor = self.conn.execute(
                "INSERT INTO runs (status, source, config_json, started_at) VALUES ('running', ?, ?, ?)",
                (source, config_json, utc_now()),
            )
        return int(cursor.lastrowid)

    def set_run_status(self, run_id: int, status: str) -> None:
        finished = utc_now() if status == "completed" else None
        with self.conn:
            self.conn.execute("UPDATE runs SET status = ?, finished_at = ? WHERE id = ?", (status, finished, run_id))

    def get_run(self, run_id: int) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()

    def latest_run(self, unfinished_only: bool = False) -> sqlite3.Row | None:
        where = "WHERE status != 'completed'" if unfinished_only else ""
        return self.conn.execute(f"SELECT * FROM runs {where} ORDER BY id DESC LIMIT 1").fetchone()

    def list_runs(self) -> list[sqlite3.Row]:
        return self.conn.execute(
            """
            SELECT r.id, r.status, r.source, r.started_at, r.finished_at,
                   (SELECT COUNT(*) FROM domains d WHERE d.run_id = r.id) AS domains,
                   (SELECT COUNT(*) FROM pages p WHERE p.run_id = r.id AND p.status = 'done') AS pages,
                   (SELECT COUNT(*) FROM leads l WHERE l.run_id = r.id) AS leads
            FROM runs r ORDER BY r.id DESC
            """
        ).fetchall()

    # Facts

    def insert_facts(self, run_id: int, domain: str, facts: Iterable[Fact]) -> None:
        """Add facts inside the caller's transaction. A repeat keeps its best confidence."""
        self.conn.executemany(
            """
            INSERT INTO facts (run_id, domain, field, value, source_url, method, confidence)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (run_id, domain, field, value, source_url) DO UPDATE SET
                method = CASE WHEN excluded.confidence > confidence THEN excluded.method ELSE method END,
                confidence = MAX(confidence, excluded.confidence)
            """,
            [(run_id, domain, f.field, f.value, f.source_url, f.method, f.confidence) for f in facts],
        )

    def facts_by_domain(self, run_id: int) -> dict[str, list[Fact]]:
        grouped: dict[str, list[Fact]] = {}
        rows = self.conn.execute(
            "SELECT domain, field, value, source_url, method, confidence FROM facts WHERE run_id = ? ORDER BY id",
            (run_id,),
        )
        for row in rows:
            grouped.setdefault(row["domain"], []).append(
                Fact(
                    field=row["field"], value=row["value"], source_url=row["source_url"],
                    method=row["method"], confidence=row["confidence"],
                )
            )
        return grouped

    def evidence_rows(self, run_id: int) -> list[dict[str, str | float]]:
        rows = self.conn.execute(
            """
            SELECT domain, field, value, source_url, method, confidence FROM facts
            WHERE run_id = ? ORDER BY domain, field, confidence DESC
            """,
            (run_id,),
        )
        return [dict(row) for row in rows]

    def domain_statuses(self, run_id: int) -> dict[str, str]:
        rows = self.conn.execute("SELECT domain, status FROM domains WHERE run_id = ?", (run_id,))
        return {row["domain"]: row["status"] for row in rows}

    # Leads

    def replace_leads(self, run_id: int, rows: list[dict[str, str | int]]) -> None:
        columns = ", ".join(LEAD_COLUMNS)
        placeholders = ", ".join("?" for _ in LEAD_COLUMNS)
        with self.conn:
            self.conn.execute("DELETE FROM leads WHERE run_id = ?", (run_id,))
            self.conn.executemany(
                f"INSERT INTO leads (run_id, {columns}) VALUES (?, {placeholders})",
                [(run_id, *(row[column] for column in LEAD_COLUMNS)) for row in rows],
            )

    def lead_rows(self, run_id: int, only_with_contact: bool = False) -> list[dict[str, str | int]]:
        where = "AND (email != '' OR phone != '')" if only_with_contact else ""
        columns = ", ".join(LEAD_COLUMNS)
        rows = self.conn.execute(
            f"SELECT {columns} FROM leads WHERE run_id = ? {where} ORDER BY quality_score DESC, domain",
            (run_id,),
        )
        return [dict(row) for row in rows]

    # Summary

    def run_summary(self, run_id: int) -> dict[str, dict[str, int]]:
        def counts(table: str) -> dict[str, int]:
            rows = self.conn.execute(
                f"SELECT status, COUNT(*) AS n FROM {table} WHERE run_id = ? GROUP BY status ORDER BY status",
                (run_id,),
            )
            return {row["status"]: row["n"] for row in rows}

        return {"domains": counts("domains"), "pages": counts("pages")}
