"""Data shapes shared across the crawler, extractors, pipeline and storage."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

FieldName = Literal[
    "name", "website", "email", "phone", "address",
    "facebook", "instagram", "linkedin", "x", "tiktok",
    "category", "description",
]

SOCIAL_FIELDS: tuple[FieldName, ...] = ("facebook", "instagram", "linkedin", "x", "tiktok")


class Fact(BaseModel):
    """One piece of evidence: this value was found for this field on this page."""

    model_config = ConfigDict(frozen=True)

    field: FieldName
    value: str
    source_url: str
    method: str
    confidence: float = Field(ge=0.0, le=1.0)


class FieldValue(BaseModel):
    value: str
    source_url: str
    confidence: float = 0.0


class Business(BaseModel):
    """A consolidated lead for one website."""

    domain: str
    crawl_status: str = ""
    country: str = ""
    name: FieldValue | None = None
    website: FieldValue | None = None
    emails: list[FieldValue] = Field(default_factory=list)
    email_type: str = ""
    phones: list[FieldValue] = Field(default_factory=list)
    address: FieldValue | None = None
    facebook: FieldValue | None = None
    instagram: FieldValue | None = None
    linkedin: FieldValue | None = None
    x: FieldValue | None = None
    tiktok: FieldValue | None = None
    category: FieldValue | None = None
    description: FieldValue | None = None
    quality_score: int = 0
    merged_domains: list[str] = Field(default_factory=list)
    possible_duplicate_of: list[str] = Field(default_factory=list)

    def to_row(self) -> dict[str, str | int]:
        """Flatten to one spreadsheet row, keeping a source URL beside each value."""

        def single(field: FieldValue | None) -> tuple[str, str]:
            return (field.value, field.source_url) if field else ("", "")

        email, email_source = single(self.emails[0] if self.emails else None)
        phone, phone_source = single(self.phones[0] if self.phones else None)
        row: dict[str, str | int] = {
            "business_name": single(self.name)[0],
            "business_name_source": single(self.name)[1],
            "website": single(self.website)[0],
            "website_source": single(self.website)[1],
            "email": email,
            "email_type": self.email_type,
            "email_source": email_source,
            "other_emails": "; ".join(e.value for e in self.emails[1:]),
            "phone": phone,
            "phone_source": phone_source,
            "other_phones": "; ".join(p.value for p in self.phones[1:]),
            "address": single(self.address)[0],
            "address_source": single(self.address)[1],
            "country": self.country,
        }
        for social in SOCIAL_FIELDS:
            row[social], row[f"{social}_source"] = single(getattr(self, social))
        row.update(
            category=single(self.category)[0],
            category_source=single(self.category)[1],
            description=single(self.description)[0],
            description_source=single(self.description)[1],
            quality_score=self.quality_score,
            crawl_status=self.crawl_status,
            domain=self.domain,
            merged_domains="; ".join(self.merged_domains),
            possible_duplicate_of="; ".join(self.possible_duplicate_of),
        )
        return row


LEAD_COLUMNS: tuple[str, ...] = tuple(Business(domain="").to_row().keys())
