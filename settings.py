"""Loads and validates config.yaml. Bad settings stop the run before any request is sent."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

import phonenumbers
import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from models import LEAD_COLUMNS

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[A-Za-z]{2,}$")
_PLACEHOLDER_DOMAINS = {"example.com", "example.org", "example.net", "domain.com", "yourdomain.com"}
_SPOOFED_AGENTS = re.compile(r"mozilla|chrome|safari|firefox|edge|googlebot|bingbot", re.I)


class SettingsError(Exception):
    """The config file is missing or invalid. The message is written for the person running the tool."""


class _Section(BaseModel):
    model_config = ConfigDict(extra="forbid")


class IdentitySettings(_Section):
    bot_name: str = "LeadCrawler"
    bot_version: str = "1.0"
    contact_email: str
    info_url: str | None = None

    @field_validator("bot_name")
    @classmethod
    def _honest_name(cls, value: str) -> str:
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]*", value):
            raise ValueError("must be a single word made of letters, digits, - or _")
        if _SPOOFED_AGENTS.search(value):
            raise ValueError("must not imitate a browser or search engine")
        return value

    @field_validator("contact_email")
    @classmethod
    def _real_contact(cls, value: str) -> str:
        if not _EMAIL_RE.match(value):
            raise ValueError("is not a valid email address")
        if value.rsplit("@", 1)[1].lower() in _PLACEHOLDER_DOMAINS:
            raise ValueError("is still a placeholder. Set it to a real inbox you read")
        return value

    @property
    def user_agent(self) -> str:
        details = f"+{self.info_url}; {self.contact_email}" if self.info_url else f"+mailto:{self.contact_email}"
        return f"{self.bot_name}/{self.bot_version} ({details})"


class CrawlSettings(_Section):
    max_depth: int = Field(default=2, ge=0, le=10)
    max_pages_per_domain: int = Field(default=30, ge=1, le=1000)
    max_links_per_page: int = Field(default=200, ge=1)
    skip_domains: list[str] = Field(default_factory=list)


class PolitenessSettings(_Section):
    min_delay: float = Field(default=1.0, ge=1.0)
    max_delay: float = Field(default=3.0, ge=1.0)
    concurrency: int = Field(default=5, ge=1, le=50)
    max_retries: int = Field(default=3, ge=0, le=10)
    backoff_base: float = Field(default=2.0, gt=0)
    backoff_max: float = Field(default=60.0, gt=0)
    max_retry_after: float = Field(default=120.0, ge=0)
    max_consecutive_blocks: int = Field(default=3, ge=1)
    max_crawl_delay: float = Field(default=30.0, ge=1.0)

    @model_validator(mode="after")
    def _delay_order(self) -> "PolitenessSettings":
        if self.max_delay < self.min_delay:
            raise ValueError("max_delay must not be smaller than min_delay")
        return self


class FetchSettings(_Section):
    timeout: float = Field(default=20.0, gt=0)
    max_bytes: int = Field(default=5_000_000, gt=0)
    max_redirects: int = Field(default=5, ge=0)
    allow_private_hosts: bool = False


class BrowserSettings(_Section):
    enabled: bool = True
    concurrency: int = Field(default=2, ge=1)
    timeout: float = Field(default=30.0, gt=0)
    max_renders_per_domain: int = Field(default=5, ge=0)
    min_text_chars: int = Field(default=200, ge=0)


class TargetSettings(_Section):
    location: str | None = None
    default_region: str = "US"
    fallback_regions: list[str] = Field(default_factory=list)
    default_category: str | None = None
    location_hints: list[str] = Field(default_factory=list)
    category_keywords: dict[str, list[str]] = Field(default_factory=dict)

    @field_validator("default_region")
    @classmethod
    def _known_region(cls, value: str) -> str:
        return _region_code(value)

    @field_validator("fallback_regions")
    @classmethod
    def _known_regions(cls, values: list[str]) -> list[str]:
        return [_region_code(value) for value in values]


class StorageSettings(_Section):
    db_path: str = "~/.leadcrawler/leads.db"
    export_dir: str = "exports"
    export_columns: list[str] = Field(default_factory=lambda: ["website", "email", "other_emails", "phone", "other_phones"])
    log_file: str | None = None

    @field_validator("export_columns")
    @classmethod
    def _known_columns(cls, values: list[str]) -> list[str]:
        unknown = [value for value in values if value not in LEAD_COLUMNS]
        if unknown:
            raise ValueError(f"unknown columns {unknown}. Choose from: {', '.join(LEAD_COLUMNS)}")
        if not values:
            raise ValueError("needs at least one column")
        return values

    @property
    def resolved_db_path(self) -> Path:
        return _resolve(self.db_path)

    @property
    def resolved_export_dir(self) -> Path:
        return _resolve(self.export_dir)


class Settings(_Section):
    identity: IdentitySettings
    crawl: CrawlSettings = Field(default_factory=CrawlSettings)
    politeness: PolitenessSettings = Field(default_factory=PolitenessSettings)
    fetch: FetchSettings = Field(default_factory=FetchSettings)
    browser: BrowserSettings = Field(default_factory=BrowserSettings)
    target: TargetSettings = Field(default_factory=TargetSettings)
    storage: StorageSettings = Field(default_factory=StorageSettings)


def _region_code(value: str) -> str:
    value = value.upper()
    if value not in phonenumbers.SUPPORTED_REGIONS:
        raise ValueError(f"{value!r} is not a known phone region code")
    return value


def _resolve(path: str) -> Path:
    return Path(os.path.expandvars(path)).expanduser()


def load_settings(path: str | Path, overrides: dict[str, dict[str, Any]] | None = None) -> Settings:
    """Read a YAML config, apply CLI overrides, and validate the result."""
    path = Path(path)
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except FileNotFoundError as exc:
        raise SettingsError(f"Config file not found: {path}") from exc
    except yaml.YAMLError as exc:
        raise SettingsError(f"{path} is not valid YAML: {exc}") from exc

    if not isinstance(raw, dict):
        raise SettingsError(f"{path} must contain a YAML mapping of settings")

    for section, values in (overrides or {}).items():
        raw.setdefault(section, {}).update({k: v for k, v in values.items() if v is not None})

    return validate_settings(raw, str(path))


def validate_settings(raw: dict[str, Any], source: str = "settings") -> Settings:
    try:
        return Settings.model_validate(raw)
    except ValidationError as exc:
        problems = "\n".join(
            f"  - {'.'.join(str(p) for p in err['loc'])}: {err['msg']}" for err in exc.errors()
        )
        raise SettingsError(f"{source} has problems:\n{problems}") from exc
