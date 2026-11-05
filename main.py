"""LeadCrawler command line. Run `python main.py --help` for the commands."""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Annotated, Optional

import typer
from rich.console import Console
from rich.logging import RichHandler
from rich.progress import BarColumn, MofNCompleteColumn, Progress, TextColumn, TimeElapsedColumn
from rich.table import Table

from crawler.frontier import Frontier
from crawler.runner import run_crawl
from crawler.seeds import RejectedSeed, Seed, load_seeds, parse_seed
from pipeline import build_leads
from settings import Settings, SettingsError, load_settings, validate_settings
from storage.db import Database
from storage.export import export_csv, export_excel

app = typer.Typer(add_completion=False, no_args_is_help=True, help="Crawl business websites and export leads.")
console = Console()

ConfigOption = Annotated[Path, typer.Option("--config", "-c", help="Path to the settings file.")]

_COLUMN_FIELDS = {"business_name": "name", "other_emails": "email", "other_phones": "phone"}
_CONTACT_COLUMNS = {"email", "other_emails", "phone", "other_phones"}


def _load(config: Path, overrides: dict | None = None) -> Settings:
    try:
        return load_settings(config, overrides)
    except SettingsError as exc:
        console.print(f"[bold red]Config problem[/bold red]\n{exc}", highlight=False)
        raise typer.Exit(2) from exc


def _setup_logging(settings: Settings, verbose: bool) -> None:
    handlers: list[logging.Handler] = [RichHandler(console=console, show_path=False, rich_tracebacks=True)]
    if settings.storage.log_file:
        file_handler = logging.FileHandler(Path(settings.storage.log_file).expanduser(), encoding="utf-8")
        file_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
        handlers.append(file_handler)
    logging.basicConfig(level=logging.DEBUG if verbose else logging.INFO, format="%(message)s", handlers=handlers, force=True)
    for noisy in ("httpx", "httpcore", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def _collect_seeds(
    settings: Settings, seeds_file: Path | None, url: str | None, country: str | None = None
) -> tuple[list[Seed], list[RejectedSeed], str]:
    if bool(seeds_file) == bool(url):
        console.print("[red]Give exactly one of --seeds FILE or --url URL.[/red]")
        raise typer.Exit(2)
    skip, private = settings.crawl.skip_domains, settings.fetch.allow_private_hosts
    if url:
        parsed = parse_seed(url, skip, private, country=country)
        if isinstance(parsed, RejectedSeed):
            return [], [parsed], url
        return [parsed], [], url
    if not seeds_file.is_file():
        console.print(f"[red]Seed file not found: {seeds_file}[/red]")
        raise typer.Exit(2)
    seeds, rejected = load_seeds(seeds_file, skip, private)
    return seeds, rejected, str(seeds_file)


def _execute(settings: Settings, db: Database, run_id: int) -> None:
    frontier = Frontier(db, run_id)
    total_domains = db.conn.execute("SELECT COUNT(*) AS n FROM domains WHERE run_id = ?", (run_id,)).fetchone()["n"]
    remaining = len(frontier.active_domains())

    with Progress(
        TextColumn("[bold]{task.description}"), BarColumn(), MofNCompleteColumn(), TimeElapsedColumn(), console=console
    ) as progress:
        domains_task = progress.add_task("Sites", total=total_domains, completed=total_domains - remaining)
        pages_task = progress.add_task("Pages", total=None)
        try:
            asyncio.run(
                run_crawl(
                    settings,
                    frontier,
                    on_page=lambda _page, _status: progress.advance(pages_task),
                    on_domain_done=lambda _domain: progress.advance(domains_task),
                )
            )
        except KeyboardInterrupt:
            db.set_run_status(run_id, "interrupted")
            progress.stop()
            console.print(f"\n[yellow]Stopped. Progress is saved. Continue with:[/yellow] python main.py resume --run-id {run_id}")
            raise typer.Exit(130) from None

    db.set_run_status(run_id, "completed")
    _print_summary(db, run_id)
    _build_and_export(settings, db, run_id, fmt="both", only_with_contact=True)


def _build_and_export(settings: Settings, db: Database, run_id: int, fmt: str, only_with_contact: bool, out: Path | None = None) -> None:
    leads = build_leads(db.facts_by_domain(run_id), settings.target, db.domain_statuses(run_id))
    db.replace_leads(run_id, [lead.to_row() for lead in leads])

    rows = db.lead_rows(run_id, only_with_contact)
    columns = settings.storage.export_columns
    # A lead whose only contact detail isn't being exported would come out as a blank row.
    exported_contacts = _CONTACT_COLUMNS.intersection(columns)
    if only_with_contact and exported_contacts:
        rows = [row for row in rows if any(row[column] for column in exported_contacts)]
    # The evidence covers just the fields being exported, with the page each value came from.
    wanted_fields = {_COLUMN_FIELDS.get(column, column) for column in columns}
    evidence = [row for row in db.evidence_rows(run_id) if row["field"] in wanted_fields]
    out_dir = out or settings.storage.resolved_export_dir
    stem = f"leads_run{run_id}"
    paths: list[Path] = []
    try:
        if fmt in {"csv", "both"}:
            paths.extend(export_csv(rows, evidence, out_dir, stem, columns))
        if fmt in {"xlsx", "both"}:
            paths.append(export_excel(rows, evidence, out_dir, stem, columns))
    except PermissionError as exc:
        console.print(f"[red]Could not write {exc.filename}. Close it in Excel, then run:[/red] python main.py export --run-id {run_id}")
        raise typer.Exit(1) from exc

    with_email = sum(1 for row in rows if row["email"])
    with_phone = sum(1 for row in rows if row["phone"])
    console.print(f"[bold green]{len(rows)} leads[/bold green] ({with_email} with email, {with_phone} with phone)")
    for path in paths:
        console.print(f"  {path}", highlight=False)


def _print_summary(db: Database, run_id: int) -> None:
    summary = db.run_summary(run_id)
    table = Table(title=f"Run {run_id}")
    table.add_column("What")
    table.add_column("Status")
    table.add_column("Count", justify="right")
    for group, counts in summary.items():
        for status, count in counts.items():
            table.add_row(group.capitalize(), status, str(count))
    console.print(table)


@app.command()
def crawl(
    seeds: Annotated[Optional[Path], typer.Option("--seeds", "-s", help="A .txt or .csv file of websites.")] = None,
    url: Annotated[Optional[str], typer.Option("--url", "-u", help="A single website to crawl.")] = None,
    country: Annotated[Optional[str], typer.Option(help="Country of the --url site, like US or DE. Usually detected.")] = None,
    config: ConfigOption = Path("config.yaml"),
    max_depth: Annotated[Optional[int], typer.Option(help="How many links deep to follow.")] = None,
    max_pages: Annotated[Optional[int], typer.Option(help="Page limit per site.")] = None,
    concurrency: Annotated[Optional[int], typer.Option(help="Sites crawled at once.")] = None,
    no_browser: Annotated[bool, typer.Option("--no-browser", help="Never start the Playwright fallback.")] = False,
    verbose: Annotated[bool, typer.Option("--verbose", "-v")] = False,
) -> None:
    """Start a new crawl, then clean the results and export them."""
    settings = _load(
        config,
        {
            "crawl": {"max_depth": max_depth, "max_pages_per_domain": max_pages},
            "politeness": {"concurrency": concurrency},
            "browser": {"enabled": False if no_browser else None},
        },
    )
    _setup_logging(settings, verbose)
    seed_list, rejected, source = _collect_seeds(settings, seeds, url, country)
    for item in rejected:
        console.print(f"[yellow]Skipped seed[/yellow] {item.raw}: {item.reason}", highlight=False)
    if not seed_list:
        console.print("[red]No usable seeds, so nothing to crawl.[/red]")
        raise typer.Exit(1)

    db = Database(settings.storage.resolved_db_path)
    try:
        run_id = db.create_run(source, settings.model_dump_json())
        Frontier(db, run_id).add_seeds(seed_list)
        console.print(f"Run [bold]{run_id}[/bold]: {len(seed_list)} seeds. Sending as {settings.identity.user_agent}", highlight=False)
        _execute(settings, db, run_id)
    finally:
        db.close()


@app.command()
def resume(
    run_id: Annotated[Optional[int], typer.Option(help="Run to continue. Defaults to the latest unfinished run.")] = None,
    config: ConfigOption = Path("config.yaml"),
    verbose: Annotated[bool, typer.Option("--verbose", "-v")] = False,
) -> None:
    """Continue an interrupted run with the settings it started with."""
    file_settings = _load(config)
    db = Database(file_settings.storage.resolved_db_path)
    try:
        run = db.get_run(run_id) if run_id else db.latest_run(unfinished_only=True)
        if run is None:
            console.print("[yellow]No matching unfinished run to resume.[/yellow]")
            raise typer.Exit(1)
        if run["status"] == "completed":
            console.print(f"[yellow]Run {run['id']} has already finished.[/yellow]")
            raise typer.Exit(1)
        try:
            settings = validate_settings(json.loads(run["config_json"]), f"run {run['id']}")
        except SettingsError as exc:
            console.print(f"[red]{exc}[/red]", highlight=False)
            raise typer.Exit(2) from exc
        _setup_logging(settings, verbose)
        console.print(f"Resuming run [bold]{run['id']}[/bold]")
        db.set_run_status(run["id"], "running")
        _execute(settings, db, run["id"])
    finally:
        db.close()


@app.command()
def export(
    run_id: Annotated[Optional[int], typer.Option(help="Run to export. Defaults to the latest run.")] = None,
    fmt: Annotated[str, typer.Option("--format", "-f", help="csv, xlsx or both.")] = "both",
    out: Annotated[Optional[Path], typer.Option(help="Output folder.")] = None,
    include_empty: Annotated[bool, typer.Option("--include-empty", help="Also list sites with no email or phone.")] = False,
    config: ConfigOption = Path("config.yaml"),
) -> None:
    """Rebuild and export leads from a run's saved data, without crawling again."""
    if fmt not in {"csv", "xlsx", "both"}:
        console.print("[red]--format must be csv, xlsx or both.[/red]")
        raise typer.Exit(2)
    settings = _load(config)
    db = Database(settings.storage.resolved_db_path)
    try:
        run = db.get_run(run_id) if run_id else db.latest_run()
        if run is None:
            console.print("[yellow]No matching run found.[/yellow]")
            raise typer.Exit(1)
        if run["status"] != "completed":
            console.print(f"[yellow]Run {run['id']} is {run['status']}, so this export is partial.[/yellow]")
        _build_and_export(settings, db, run["id"], fmt, not include_empty, out)
    finally:
        db.close()


@app.command()
def runs(config: ConfigOption = Path("config.yaml")) -> None:
    """List past runs."""
    settings = _load(config)
    db = Database(settings.storage.resolved_db_path)
    try:
        table = Table()
        for column in ("Run", "Status", "Source", "Started", "Sites", "Pages", "Leads"):
            table.add_column(column)
        for row in db.list_runs():
            table.add_row(
                str(row["id"]), row["status"], row["source"], row["started_at"],
                str(row["domains"]), str(row["pages"]), str(row["leads"]),
            )
        console.print(table)
    finally:
        db.close()


if __name__ == "__main__":
    app()
