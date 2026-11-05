import csv
from pathlib import Path

import pytest
import yaml
from openpyxl import load_workbook
from typer.testing import CliRunner

from main import app
from models import Fact
from settings import SettingsError, load_settings, validate_settings
from storage.db import Database
from conftest import make_settings

runner = CliRunner()
PROJECT_CONFIG = Path(__file__).parent.parent / "config.yaml"


def write_config(tmp_path, **sections) -> Path:
    raw = {
        "identity": {"contact_email": "ops@acme-marketing.ng"},
        "storage": {"db_path": str(tmp_path / "t.db"), "export_dir": str(tmp_path / "out")},
    }
    raw.update(sections)
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    return path


class TestSettings:
    def test_defaults_match_the_brief(self):
        settings = make_settings()
        assert settings.crawl.max_depth == 2
        assert settings.crawl.max_pages_per_domain == 30
        assert settings.politeness.min_delay == 1 and settings.politeness.max_delay == 3
        assert settings.politeness.concurrency == 5
        assert settings.target.default_region == "NG"

    def test_placeholder_contact_email_is_refused(self):
        with pytest.raises(SettingsError, match="contact_email.*placeholder"):
            load_settings(PROJECT_CONFIG, {"identity": {"contact_email": "you@example.com"}})

    def test_shipped_config_is_otherwise_valid(self):
        settings = load_settings(PROJECT_CONFIG, {"identity": {"contact_email": "real@acme-marketing.ng"}})
        assert settings.identity.contact_email == "real@acme-marketing.ng"
        assert "facebook.com" in settings.crawl.skip_domains

    @pytest.mark.parametrize(
        ("sections", "message"),
        [
            ({"politeness": {"min_delay": 0.1}}, "min_delay"),
            ({"politeness": {"min_delay": 5, "max_delay": 2}}, "max_delay"),
            ({"politeness": {"concurrency": 0}}, "concurrency"),
            ({"crawl": {"max_dpeth": 3}}, "max_dpeth"),
            ({"target": {"default_region": "ZZ"}}, "default_region"),
            ({"identity": {"contact_email": "a@b.ng", "bot_name": "Mozilla"}}, "bot_name"),
            ({"identity": {"contact_email": "not-an-email"}}, "contact_email"),
        ],
    )
    def test_rejects_bad_values(self, sections, message):
        with pytest.raises(SettingsError, match=message):
            make_settings(**sections)

    def test_there_is_no_way_to_turn_off_robots(self):
        with pytest.raises(SettingsError, match="obey_robots"):
            make_settings(crawl={"obey_robots": False})

    def test_user_agent_includes_contact(self):
        settings = validate_settings({"identity": {"contact_email": "a@b.ng", "info_url": "https://b.ng/bot"}})
        assert settings.identity.user_agent == "LeadCrawler/1.0 (+https://b.ng/bot; a@b.ng)"


class TestExportColumns:
    def populated(self, tmp_path, **storage) -> Path:
        config = write_config(tmp_path)
        raw = yaml.safe_load(config.read_text(encoding="utf-8"))
        raw["storage"].update(storage)
        config.write_text(yaml.safe_dump(raw), encoding="utf-8")

        def fact(field, value, source):
            return Fact(field=field, value=value, source_url=source, method="test", confidence=0.9)

        db = Database(tmp_path / "t.db")
        run = db.create_run("t", "{}")
        with db.conn:
            db.insert_facts(run, "full.com", [
                fact("website", "https://full.com", "https://full.com/"),
                fact("name", "Full Co", "https://full.com/"),
                fact("address", "1 Long Street, Houston, TX 77002", "https://full.com/"),
                fact("email", "info@full.com", "https://full.com/contact"),
                fact("email", "sales@full.com", "https://full.com/contact"),
                fact("phone", "+17139671111", "https://full.com/contact"),
            ])
            db.insert_facts(run, "empty.com", [fact("website", "https://empty.com", "https://empty.com/")])
        db.close()
        return config

    def read(self, path: Path) -> list[dict]:
        with path.open(encoding="utf-8-sig", newline="") as handle:
            return list(csv.DictReader(handle))

    def test_only_contact_columns_and_only_sites_with_contacts(self, tmp_path):
        config = self.populated(tmp_path)
        assert runner.invoke(app, ["export", "-c", str(config)]).exit_code == 0

        assert self.read(tmp_path / "out" / "leads_run1.csv") == [
            {
                "website": "https://full.com", "email": "info@full.com", "other_emails": "sales@full.com",
                "phone": "+17139671111", "other_phones": "",
            }
        ]
        workbook = load_workbook(tmp_path / "out" / "leads_run1.xlsx")
        assert [cell.value for cell in workbook["Leads"][1]] == ["website", "email", "other_emails", "phone", "other_phones"]

    def test_evidence_keeps_source_pages_for_exported_fields_only(self, tmp_path):
        config = self.populated(tmp_path)
        runner.invoke(app, ["export", "-c", str(config)])
        rows = self.read(tmp_path / "out" / "leads_run1_evidence.csv")
        assert {row["field"] for row in rows} == {"website", "email", "phone"}
        assert {row["source_url"] for row in rows if row["field"] == "email"} == {"https://full.com/contact"}

    def test_include_empty_lists_every_site(self, tmp_path):
        config = self.populated(tmp_path)
        runner.invoke(app, ["export", "-c", str(config), "--include-empty"])
        assert len(self.read(tmp_path / "out" / "leads_run1.csv")) == 2

    def test_other_columns_can_be_added_back_without_crawling(self, tmp_path):
        config = self.populated(tmp_path, export_columns=["business_name", "address", "email"])
        runner.invoke(app, ["export", "-c", str(config)])
        assert self.read(tmp_path / "out" / "leads_run1.csv") == [
            {"business_name": "Full Co", "address": "1 Long Street, Houston, TX 77002", "email": "info@full.com"}
        ]

    def test_email_only_export_leaves_out_phone_only_sites(self, tmp_path):
        config = self.populated(tmp_path, export_columns=["email", "other_emails"])
        db = Database(tmp_path / "t.db")
        with db.conn:
            db.insert_facts(1, "phoneonly.com", [
                Fact(field="phone", value="+17139672222", source_url="https://phoneonly.com/", method="test", confidence=0.9),
            ])
        db.close()

        assert runner.invoke(app, ["export", "-c", str(config)]).exit_code == 0

        assert self.read(tmp_path / "out" / "leads_run1.csv") == [{"email": "info@full.com", "other_emails": "sales@full.com"}]
        assert {row["field"] for row in self.read(tmp_path / "out" / "leads_run1_evidence.csv")} == {"email"}

    def test_unknown_column_is_refused(self):
        with pytest.raises(SettingsError, match="unknown columns"):
            make_settings(storage={"export_columns": ["emial"]})


class TestCli:
    def test_missing_config(self, tmp_path):
        result = runner.invoke(app, ["crawl", "--url", "acme.com", "-c", str(tmp_path / "nope.yaml")])
        assert result.exit_code == 2 and "not found" in result.output

    def test_needs_exactly_one_seed_source(self, tmp_path):
        config = write_config(tmp_path)
        assert runner.invoke(app, ["crawl", "-c", str(config)]).exit_code == 2
        assert runner.invoke(app, ["crawl", "-c", str(config), "--url", "a.com", "--seeds", "b.txt"]).exit_code == 2

    def test_all_seeds_rejected(self, tmp_path):
        config = write_config(tmp_path, crawl={"skip_domains": ["facebook.com"]})
        result = runner.invoke(app, ["crawl", "-c", str(config), "--url", "https://facebook.com/acme"])
        assert result.exit_code == 1 and "skip_domains" in result.output

    def test_override_is_still_validated(self, tmp_path):
        config = write_config(tmp_path)
        result = runner.invoke(app, ["crawl", "-c", str(config), "--url", "a.com", "--concurrency", "0"])
        assert result.exit_code == 2 and "concurrency" in result.output

    def test_resume_and_export_with_nothing_to_do(self, tmp_path):
        config = write_config(tmp_path)
        assert runner.invoke(app, ["resume", "-c", str(config)]).exit_code == 1
        assert runner.invoke(app, ["export", "-c", str(config)]).exit_code == 1
        assert runner.invoke(app, ["runs", "-c", str(config)]).exit_code == 0
