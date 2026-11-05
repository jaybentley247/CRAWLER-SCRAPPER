import csv
import sqlite3
from pathlib import Path

import pytest
from openpyxl import load_workbook

from models import LEAD_COLUMNS, Business, Fact, FieldValue
from pipeline import build_leads
from pipeline.cleaning import clean_fact, clean_text, shorten
from pipeline.dedupe import consolidate, dedupe_businesses
from pipeline.enrichment import keyword_category, quality_score
from pipeline.validation import is_valid_fact
from settings import TargetSettings, load_settings
from storage.db import Database
from storage.export import EVIDENCE_COLUMNS, export_csv, export_excel, safe_cell

PROJECT_CONFIG = Path(__file__).parent.parent / "config.yaml"
TARGET =TargetSettings(category_keywords={"Real Estate": ["property", "realtor"], "Restaurant": ["cuisine"]})


def fact(field, value, source="https://acme.com/", confidence=0.8, method="test") -> Fact:
    return Fact(field=field, value=value, source_url=source, method=method, confidence=confidence)


class TestCleaning:
    def test_whitespace_entities_and_control_characters(self):
        assert clean_text("  Acme &amp;\n Sons​\x07 ") == "Acme & Sons"

    def test_shorten_cuts_on_a_word(self):
        result = shorten("word " * 100, 50)
        assert len(result) <= 51 and result.endswith("word…")

    def test_empty_values_are_dropped(self):
        assert clean_fact(fact("name", "   ")) is None

    def test_source_is_kept(self):
        assert clean_fact(fact("name", " Acme ", source="https://acme.com/x")).source_url == "https://acme.com/x"


class TestValidation:
    def test_junk_names(self):
        assert not is_valid_fact(fact("name", "Home"))
        assert not is_valid_fact(fact("name", "Just a moment..."))
        assert is_valid_fact(fact("name", "Acme Realty"))

    def test_contacts(self):
        assert is_valid_fact(fact("email", "info@acme.com"))
        assert not is_valid_fact(fact("email", "you@example.com"))
        assert is_valid_fact(fact("phone", "+2348031234567"))
        assert not is_valid_fact(fact("phone", "0803"))


class TestConsolidate:
    def test_highest_confidence_wins_and_keeps_its_source(self):
        business = consolidate(
            "acme.com",
            [
                fact("name", "From Title", "https://acme.com/", 0.6),
                fact("name", "From JSON-LD", "https://acme.com/about", 0.95),
            ],
        )
        assert business.name.value == "From JSON-LD"
        assert business.name.source_url == "https://acme.com/about"

    def test_repeat_sightings_break_ties(self):
        business = consolidate(
            "acme.com",
            [
                fact("facebook", "https://facebook.com/once", "https://acme.com/a", 0.7),
                fact("facebook", "https://facebook.com/often", "https://acme.com/b", 0.7),
                fact("facebook", "https://facebook.com/often", "https://acme.com/c", 0.7),
            ],
        )
        assert business.facebook.value == "https://facebook.com/often"

    def test_primary_email_prefers_role_on_own_domain(self):
        business = consolidate(
            "acme.com",
            [
                fact("email", "dev@webagency.com", confidence=0.95),
                fact("email", "acme@gmail.com", confidence=0.95),
                fact("email", "ada@acme.com", confidence=0.95),
                fact("email", "info@acme.com", confidence=0.7),
            ],
        )
        assert [email.value for email in business.emails] == [
            "info@acme.com", "ada@acme.com", "acme@gmail.com", "dev@webagency.com",
        ]
        assert business.email_type == "role"

    def test_values_differing_only_by_case_are_one(self):
        business = consolidate("acme.com", [fact("email", "info@acme.com"), fact("phone", "+2348031234567"), fact("phone", "+2348031234567", "https://acme.com/b")])
        assert len(business.phones) == 1


def lead(domain, name=None, email=None, phone=None) -> Business:
    facts = []
    if name:
        facts.append(fact("name", name, f"https://{domain}/"))
    if email:
        facts.append(fact("email", email, f"https://{domain}/"))
    if phone:
        facts.append(fact("phone", phone, f"https://{domain}/"))
    business = consolidate(domain, facts)
    business.quality_score = quality_score(business)
    return business


class TestDedupe:
    def test_merges_same_name_and_shared_contact(self):
        result = dedupe_businesses(
            [
                lead("acme.com", "Acme Realty Ltd", "info@acme.com", "+2348031234567"),
                lead("acme.ng", "Acme Realty Limited", "sales@acme.ng", "+2348031234567"),
            ]
        )
        assert len(result) == 1
        assert result[0].merged_domains in (["acme.ng"], ["acme.com"])
        assert {email.value for email in result[0].emails} == {"info@acme.com", "sales@acme.ng"}

    def test_merged_email_keeps_its_type_and_counts_towards_the_score(self):
        # The better record has no email of its own, so it's the one that receives it.
        no_email = lead("acme.com", "Acme Realty", phone="+2348031234567")
        for field in ("address", "description", "category", "facebook"):
            setattr(no_email, field, FieldValue(value="A value long enough", source_url="https://acme.com/"))
        no_email.quality_score = quality_score(no_email)
        has_email = lead("acme.ng", "Acme Realty", "dev@webagency.com", "+2348031234567")
        assert no_email.quality_score > has_email.quality_score

        [merged] = dedupe_businesses([no_email, has_email])

        assert merged.domain == "acme.com"
        assert merged.emails[0].value == "dev@webagency.com"
        assert merged.email_type == "external"
        assert merged.quality_score == 10 + 15 + 25 + 10 + 10 + 5 + 5

    def test_shared_contact_with_different_names_is_only_flagged(self):
        result = dedupe_businesses(
            [
                lead("one.com", "Golden Foods", phone="+2348031234567"),
                lead("two.com", "Prime Logistics", phone="+2348031234567"),
            ]
        )
        assert len(result) == 2
        assert {tuple(business.possible_duplicate_of) for business in result} == {("one.com",), ("two.com",)}

    def test_same_name_without_shared_contact_stays_separate(self):
        result = dedupe_businesses(
            [lead("a.com", "Royal Kitchen", phone="+2348031234567"), lead("b.com", "Royal Kitchen", phone="+2348091234567")]
        )
        assert len(result) == 2

    def test_chain_of_merges_collapses_to_one(self):
        result = dedupe_businesses(
            [
                lead("a.com", "Acme Realty", "a@a.com", "+2348031234567"),
                lead("b.com", "Acme Realty", "a@a.com", "+2348091234567"),
                lead("c.com", "Acme Realty", "c@c.com", "+2348091234567"),
            ]
        )
        assert len(result) == 1 and len(result[0].merged_domains) == 2


class TestEnrichment:
    def test_keyword_category(self):
        assert keyword_category("Top property and realtor services", TARGET.category_keywords) == "Real Estate"
        assert keyword_category("We sell shoes", TARGET.category_keywords) is None
        assert keyword_category("improperty", TARGET.category_keywords) is None

    def test_plurals_count(self):
        assert keyword_category("Licensed realtors", TARGET.category_keywords) == "Real Estate"

    @pytest.mark.parametrize(
        ("text", "category"),
        [
            ("Upstream oil and gas drilling services in Texas", "Oil & Gas"),
            ("We are a licensed general contractor for commercial projects", "General Contracting"),
            ("Precision CNC machining and metal fabrication manufacturer", "Manufacturing"),
            ("Family-owned car dealership and auto repair shop", "Automotive"),
            ("Freight forwarding and trucking across Canada", "Logistics & Transport"),
            ("Corporate law firm with attorneys in Frankfurt", "Legal"),
            ("Cloud software for small teams", "Software & IT"),
            ("Residential roofing and concrete construction", "Construction"),
        ],
    )
    def test_shipped_categories_cover_all_kinds_of_company(self, text, category):
        settings = load_settings(PROJECT_CONFIG)
        assert keyword_category(text, settings.target.category_keywords) == category

    def test_unmatched_company_is_kept_and_labelled_other(self):
        settings = load_settings(PROJECT_CONFIG)
        [business] = build_leads({"a.com": [fact("name", "Zzyzx Holdings")]}, settings.target)
        assert business.category.value == "Other"

    @pytest.mark.parametrize(
        ("phone", "domain", "country"),
        [
            ("+17139671111", "a.com", "US"), ("+14169671111", "a.com", "CA"), ("+493012345678", "a.com", "DE"),
            (None, "a.de", "DE"), (None, "a.com", ""),
        ],
    )
    def test_country_comes_from_phone_then_domain(self, phone, domain, country):
        facts = [fact("phone", phone)] if phone else [fact("name", "Acme")]
        [business] = build_leads({domain: facts}, TARGET)
        assert business.country == country

    def test_score_rewards_contact_details(self):
        assert quality_score(lead("a.com", "Name", "info@a.com", "+2348031234567")) > quality_score(lead("a.com", "Name"))


class TestBuildLeads:
    def test_full_pipeline(self):
        facts = {
            "acme.com": [
                fact("website", "https://acme.com", confidence=0.9),
                fact("name", "  Acme Realty  ", confidence=0.95),
                fact("name", "Home", confidence=1.0),
                fact("email", "info@acme.com"),
                fact("phone", "+2348031234567"),
                fact("description", "Leading property experts in Lagos, Nigeria."),
            ],
        }
        [business] = build_leads(facts, TARGET, {"acme.com": "done"})
        assert business.name.value == "Acme Realty"
        assert business.category.value == "Real Estate"
        assert business.crawl_status == "done"
        assert business.quality_score > 50

    def test_default_category_is_labelled_as_config(self):
        target = TargetSettings(default_category="Logistics")
        [business] = build_leads({"a.com": [fact("name", "Acme")]}, target)
        assert business.category.value == "Logistics"
        assert business.category.source_url == "config:target.default_category"


class TestExport:
    def test_every_value_column_keeps_its_own_source_url(self):
        singles = ("name", "website", "address", "facebook", "instagram", "linkedin", "x", "tiktok", "category", "description")
        fields = {field: FieldValue(value=f"value-{field}", source_url=f"https://acme.com/{field}") for field in singles}
        fields["emails"] = [FieldValue(value="value-email", source_url="https://acme.com/email")]
        fields["phones"] = [FieldValue(value="value-phone", source_url="https://acme.com/phone")]
        row = Business(domain="acme.com", **fields).to_row()

        for field in (*singles, "email", "phone"):
            column = "business_name" if field == "name" else field
            assert row[column] == f"value-{field}"
            assert row[f"{column}_source"] == f"https://acme.com/{field}"

    def rows(self):
        business = Business(
            domain="acme.com",
            name=FieldValue(value="=HYPERLINK(\"http://evil\",\"click\")", source_url="https://acme.com/"),
            emails=[FieldValue(value="info@acme.com", source_url="https://acme.com/contact"), FieldValue(value="b@acme.com", source_url="x")],
            phones=[FieldValue(value="+2348031234567", source_url="https://acme.com/contact")],
            description=FieldValue(value="₦500 homes — Café", source_url="https://acme.com/"),
        )
        leads = [business.to_row()]
        evidence = [dict(domain="acme.com", field="email", value="info@acme.com", source_url="https://acme.com/contact", method="mailto", confidence=0.95)]
        return leads, evidence

    def test_safe_cell(self):
        assert safe_cell("=1+1") == "'=1+1"
        assert safe_cell("@SUM(A1)") == "'@SUM(A1)"
        assert safe_cell("-2+3") == "'-2+3"
        assert safe_cell("+cmd|x") == "'+cmd|x"
        assert safe_cell("+2348031234567") == "+2348031234567"
        assert safe_cell("Acme") == "Acme"
        assert safe_cell(5) == 5
        assert safe_cell(None) == ""

    def test_csv_output(self, tmp_path):
        leads, evidence = self.rows()
        leads_path, evidence_path = export_csv(leads, evidence, tmp_path, "t")
        assert leads_path.read_bytes().startswith(b"\xef\xbb\xbf")
        with leads_path.open(encoding="utf-8-sig", newline="") as handle:
            [row] = list(csv.DictReader(handle))
        assert tuple(row) == LEAD_COLUMNS
        assert row["email"] == "info@acme.com"
        assert row["email_source"] == "https://acme.com/contact"
        assert row["other_emails"] == "b@acme.com"
        assert row["phone"] == "+2348031234567"
        assert row["business_name"].startswith("'=")
        assert "₦" in row["description"]
        with evidence_path.open(encoding="utf-8-sig", newline="") as handle:
            assert tuple(next(csv.DictReader(handle))) == EVIDENCE_COLUMNS

    def test_excel_output_has_no_formulas(self, tmp_path):
        leads, evidence = self.rows()
        leads[0]["address"] = "=cmd()\x07 bad"
        path = export_excel(leads, evidence, tmp_path, "t")
        workbook = load_workbook(path)
        assert workbook.sheetnames == ["Leads", "Evidence"]
        sheet = workbook["Leads"]
        headers = [cell.value for cell in sheet[1]]
        assert tuple(headers) == LEAD_COLUMNS
        cells = {header: sheet.cell(row=2, column=index + 1) for index, header in enumerate(headers)}
        assert cells["business_name"].data_type == "s"
        assert cells["business_name"].value.startswith("'=")
        assert cells["address"].value == "'=cmd()  bad"
        assert cells["phone"].value == "+2348031234567"
        assert sheet.freeze_panes == "A2"
        for row in sheet.iter_rows():
            assert all(cell.data_type != "f" for cell in row)

    def test_empty_export_still_works(self, tmp_path):
        assert export_excel([], [], tmp_path, "t").exists()
        assert all(path.exists() for path in export_csv([], [], tmp_path, "t"))


class TestDatabase:
    def test_older_database_is_upgraded_and_keeps_its_data(self, tmp_path):
        path = tmp_path / "old.db"
        Database(path).close()
        old = sqlite3.connect(path)
        old.execute("ALTER TABLE leads DROP COLUMN country")
        old.execute("ALTER TABLE domains DROP COLUMN region")
        old.execute("INSERT INTO runs (id, status, source, config_json, started_at) VALUES (1, 'completed', 's', '{}', 't')")
        old.execute("INSERT INTO domains (run_id, domain) VALUES (1, 'a.com')")
        old.commit()
        old.close()

        db = Database(path)
        assert db.domain_statuses(1) == {"a.com": "active"}
        db.replace_leads(1, [lead("a.com", "Kept").to_row()])
        assert db.lead_rows(1)[0]["business_name"] == "Kept"
        db.close()

    def test_leads_round_trip_and_contact_filter(self, tmp_path):
        db = Database(tmp_path / "x.db")
        run = db.create_run("t", "{}")
        db.conn.execute("INSERT INTO domains (run_id, domain) VALUES (?, 'a.com')", (run,))
        rows = [
            lead("a.com", "With", "info@a.com").to_row(),
            lead("b.com", "Without").to_row(),
        ]
        db.replace_leads(run, rows)
        db.replace_leads(run, rows)  # Rebuilding must replace, not duplicate.
        assert len(db.lead_rows(run)) == 2
        assert [row["domain"] for row in db.lead_rows(run, only_with_contact=True)] == ["a.com"]
        db.close()

    def test_repeat_fact_keeps_best_confidence(self, tmp_path):
        db = Database(tmp_path / "x.db")
        run = db.create_run("t", "{}")
        with db.conn:
            db.insert_facts(run, "a.com", [fact("email", "i@a.com", confidence=0.5, method="low")])
            db.insert_facts(run, "a.com", [fact("email", "i@a.com", confidence=0.9, method="high")])
            db.insert_facts(run, "a.com", [fact("email", "i@a.com", confidence=0.7, method="mid")])
        [stored] = db.facts_by_domain(run)["a.com"]
        assert stored.confidence == 0.9 and stored.method == "high"
        db.close()
