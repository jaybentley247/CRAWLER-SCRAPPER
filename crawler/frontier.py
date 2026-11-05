"""The crawl queue, kept in SQLite so an interrupted run can carry on where it stopped."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from crawler.links import SEED_PRIORITY, Link
from crawler.seeds import Seed
from crawler.urls import is_document_url, url_key
from models import Fact
from storage.db import Database, utc_now

# Statuses that used up part of a site's page budget.
_ATTEMPTED = ("done", "failed", "redirected")


@dataclass(frozen=True)
class Page:
    id: int
    run_id: int
    domain: str
    url: str
    depth: int


class Frontier:
    def __init__(self, db: Database, run_id: int):
        self.db = db
        self.conn = db.conn
        self.run_id = run_id

    def add_seeds(self, seeds: Iterable[Seed]) -> None:
        with self.conn:
            for seed in seeds:
                self.conn.execute(
                    "INSERT OR IGNORE INTO domains (run_id, domain, region) VALUES (?, ?, ?)",
                    (self.run_id, seed.domain, seed.country),
                )
                self._insert_page(seed.domain, seed.url, 0, SEED_PRIORITY, None)
                if is_document_url(seed.url):
                    continue  # A document's leads are the businesses it lists, not the site hosting it.
                hints = [Fact(field="website", value=seed.url, source_url=seed.url, method="seed", confidence=0.5)]
                if seed.name:
                    hints.append(Fact(field="name", value=seed.name, source_url="seed_file", method="seed", confidence=1.0))
                if seed.category:
                    hints.append(Fact(field="category", value=seed.category, source_url="seed_file", method="seed", confidence=1.0))
                self.db.insert_facts(self.run_id, seed.domain, hints)

    def reset_interrupted(self) -> int:
        """Pages being fetched when the last run died go back in the queue."""
        with self.conn:
            cursor = self.conn.execute(
                "UPDATE pages SET status = 'pending' WHERE run_id = ? AND status = 'in_progress'", (self.run_id,)
            )
        return cursor.rowcount

    def active_domains(self) -> list[str]:
        rows = self.conn.execute(
            "SELECT domain FROM domains WHERE run_id = ? AND status = 'active' ORDER BY domain", (self.run_id,)
        )
        return [row["domain"] for row in rows]

    def domain_region(self, domain: str) -> str | None:
        """The country given for this site in the seed file, if there was one."""
        row = self.conn.execute(
            "SELECT region FROM domains WHERE run_id = ? AND domain = ?", (self.run_id, domain)
        ).fetchone()
        return row["region"] if row else None

    def claim_next(self, domain: str) -> Page | None:
        """Take the most promising unvisited page: contact-like first, then shallowest."""
        row = self.conn.execute(
            """
            SELECT id, url, depth FROM pages
            WHERE run_id = ? AND domain = ? AND status = 'pending'
            ORDER BY priority DESC, depth, id LIMIT 1
            """,
            (self.run_id, domain),
        ).fetchone()
        if row is None:
            return None
        with self.conn:
            self.conn.execute(
                "UPDATE pages SET status = 'in_progress', updated_at = ? WHERE id = ?", (utc_now(), row["id"])
            )
        return Page(row["id"], self.run_id, domain, row["url"], row["depth"])

    def pages_attempted(self, domain: str) -> int:
        marks = ", ".join("?" for _ in _ATTEMPTED)
        row = self.conn.execute(
            f"SELECT COUNT(*) AS n FROM pages WHERE run_id = ? AND domain = ? AND status IN ({marks})",
            (self.run_id, domain, *_ATTEMPTED),
        ).fetchone()
        return row["n"]

    def record_success(
        self,
        page: Page,
        *,
        final_url: str,
        http_status: int | None,
        rendered: bool,
        facts: list[Fact],
        links: list[Link],
        other_leads: Mapping[str, list[Fact]] | None = None,
    ) -> None:
        """Save a page's results and its new links together, so a crash can't half-record it.

        `other_leads` holds facts about businesses other than this site, such as those a PDF lists.
        """
        with self.conn:
            self.db.insert_facts(self.run_id, page.domain, facts)
            for lead, lead_facts in (other_leads or {}).items():
                self.db.insert_facts(self.run_id, lead, lead_facts)
            for link in links:
                self._insert_page(page.domain, link.url, page.depth + 1, link.priority, page.url)
            self._set_page(page.id, "done", http_status=http_status, rendered=rendered)
            self.conn.execute(
                "UPDATE domains SET consecutive_failures = 0 WHERE run_id = ? AND domain = ?",
                (self.run_id, page.domain),
            )
            if url_key(final_url) != url_key(page.url):
                self._mark_redirect_target(page, final_url)

    def _mark_redirect_target(self, page: Page, final_url: str) -> None:
        """We've already read the page a redirect landed on, so don't fetch it again."""
        key = url_key(final_url)
        self.conn.execute(
            """
            INSERT INTO pages (run_id, domain, url, url_key, depth, priority, status, discovered_from, updated_at)
            VALUES (?, ?, ?, ?, ?, 0, 'alias', ?, ?)
            ON CONFLICT (run_id, url_key) DO UPDATE SET status = 'alias'
            WHERE status = 'pending'
            """,
            (self.run_id, page.domain, final_url, key, page.depth, page.url, utc_now()),
        )

    def finish_page(self, page: Page, status: str, error: str | None = None, http_status: int | None = None) -> None:
        with self.conn:
            self._set_page(page.id, status, error=error, http_status=http_status)

    def record_failure(self, page: Page, error: str, http_status: int | None) -> int:
        """Mark a page failed and return how many block/failure results the site has had in a row."""
        with self.conn:
            self._set_page(page.id, "failed", error=error, http_status=http_status)
            self.conn.execute(
                "UPDATE domains SET consecutive_failures = consecutive_failures + 1 WHERE run_id = ? AND domain = ?",
                (self.run_id, page.domain),
            )
        row = self.conn.execute(
            "SELECT consecutive_failures AS n FROM domains WHERE run_id = ? AND domain = ?",
            (self.run_id, page.domain),
        ).fetchone()
        return row["n"]

    def rewrite_url(self, page: Page, new_url: str) -> None:
        """Requeue a page under a new address, such as http:// when https:// is unavailable."""
        with self.conn:
            self.conn.execute(
                "UPDATE pages SET url = ?, status = 'pending', updated_at = ? WHERE id = ?",
                (new_url, utc_now(), page.id),
            )

    def finish_domain(self, domain: str, status: str, reason: str | None = None) -> None:
        with self.conn:
            self.conn.execute(
                "UPDATE domains SET status = ?, reason = ? WHERE run_id = ? AND domain = ?",
                (status, reason, self.run_id, domain),
            )
            self.conn.execute(
                """
                UPDATE pages SET status = 'skipped', error = ?, updated_at = ?
                WHERE run_id = ? AND domain = ? AND status IN ('pending', 'in_progress')
                """,
                (f"domain_{status}", utc_now(), self.run_id, domain),
            )

    def renders_used(self, domain: str) -> int:
        row = self.conn.execute(
            "SELECT renders AS n FROM domains WHERE run_id = ? AND domain = ?", (self.run_id, domain)
        ).fetchone()
        return row["n"]

    def add_render(self, domain: str) -> None:
        with self.conn:
            self.conn.execute(
                "UPDATE domains SET renders = renders + 1 WHERE run_id = ? AND domain = ?", (self.run_id, domain)
            )

    def _insert_page(self, domain: str, url: str, depth: int, priority: int, discovered_from: str | None) -> None:
        self.conn.execute(
            """
            INSERT OR IGNORE INTO pages (run_id, domain, url, url_key, depth, priority, discovered_from, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (self.run_id, domain, url, url_key(url), depth, priority, discovered_from, utc_now()),
        )

    def _set_page(
        self,
        page_id: int,
        status: str,
        *,
        error: str | None = None,
        http_status: int | None = None,
        rendered: bool = False,
    ) -> None:
        self.conn.execute(
            "UPDATE pages SET status = ?, error = ?, http_status = ?, rendered = ?, updated_at = ? WHERE id = ?",
            (status, error, http_status, int(rendered), utc_now(), page_id),
        )
