import io
import json
import zipfile
from pathlib import Path

import openpyxl
import pytest
import xlwt

from conftest import make_pdf
from crawler.links import extract_links
from extractors import address, email, jsonld, metadata, phone, socials
from extractors.document import UnreadableDocument, extract_document
from extractors.email import classify_email, decode_cloudflare, normalize_email
from extractors.metadata import name_from_title
from extractors.page import ParsedPage
from extractors.phone import normalize_phone
from extractors.region import country_code, detect_region, region_from_domain

URL = "https://acme.com.ng/contact"
FIXTURES = Path(__file__).parent / "fixtures"


def page(body: str, head: str = "", url: str = URL) -> ParsedPage:
    return ParsedPage(f"<html><head>{head}</head><body>{body}</body></html>", url)


def values(facts, field=None) -> set[str]:
    return {fact.value for fact in facts if field is None or fact.field == field}


class TestEmail:
    def test_finds_plain_text_and_mailto(self):
        facts = email.extract(page('Write to Info@Acme.com.ng. <a href="mailto:sales@acme.com.ng?subject=Hi">Sales</a>'))
        assert values(facts) == {"info@acme.com.ng", "sales@acme.com.ng"}
        assert all(fact.source_url == URL for fact in facts)

    @pytest.mark.parametrize(
        "text",
        [
            "info [at] acme [dot] com", "info(at)acme(dot)com", "info {at} acme {dot} com", "INFO [AT] ACME [DOT] COM",
            "info[at]acme.com", "info@acme[dot]com", "info at acme dot com", "info AT acme DOT com",
        ],
    )
    def test_decodes_obfuscation(self, text):
        assert values(email.extract(page(f"<p>{text}</p>"))) == {"info@acme.com"}

    def test_decodes_multi_part_domains(self):
        assert values(email.extract(page("sales [at] acme [dot] com [dot] ng"))) == {"sales@acme.com.ng"}

    @pytest.mark.parametrize("real", ["frank@acme.com", "lastminute@acme.com", "f.last@acme.com", "names@acme.com", "m@acme.com"])
    def test_real_addresses_that_resemble_formats_are_kept(self, real):
        assert normalize_email(real) == real

    def test_percent_codes_and_ampersands_do_not_mangle_addresses(self):
        found = values(email.extract(page("mailto:%20ann.lee@acme.com and info.f&b@acme.co.uk")))
        assert "20ann.lee@acme.com" not in found and "b@acme.co.uk" not in found

    def test_ordinary_sentences_are_not_emails(self):
        assert values(email.extract(page("Email us at acme dot com or look at site dot com"))) == set()

    @pytest.mark.parametrize(
        "junk",
        [
            "example@gmail.com", "you@example.com", "logo@2x.png", "icon-sprite@3x.webp", "noreply@acme.com",
            "no-reply@acme.com", "abc123@sentry.io", "605a7baede844d278b89dc95ae0a9123@sentry-next.wixpress.com",
            "user@domain.com", "bootstrap@5.3.0", "a@b", "x@y.notarealtld",
            # Address formats, not addresses.
            "firstnamelastname@acme.com", "firstname.lastname@acme.com", "firstinitiallastname@acme.com",
            "firstname_middleinitial_lastname@acme.com", "lastname.firstname@acme.com", "first.last@acme.com",
            "flast@acme.com", "firstname@acme.com", "lastnamefirstinitial@acme.com", "firstinitial.lastname@acme.com",
            "firstname.lastnamen@acme.com", "firstinitialfirstthreelettersoflastname@acme.com", "firstnamelastnam@acme.com",
        ],
    )
    def test_filters_junk(self, junk):
        assert normalize_email(junk) is None

    def test_scripts_are_not_read_as_page_text(self):
        assert values(email.extract(page('<script>var a="secret@acme.com";</script>hello'))) == set()

    def test_cloudflare_protection(self):
        key = 0x2a
        encoded = bytes([key]) + bytes(byte ^ key for byte in b"info@acme.com")
        assert decode_cloudflare(encoded.hex()) == "info@acme.com"
        body = f'<a class="__cf_email__" data-cfemail="{encoded.hex()}">[protected]</a>'
        assert "info@acme.com" in values(email.extract(page(body)))
        link = f'<a href="/cdn-cgi/l/email-protection#{encoded.hex()}">mail</a>'
        assert "info@acme.com" in values(email.extract(page(link)))

    @pytest.mark.parametrize(
        ("address_", "kind"),
        [
            ("info@acme.com.ng", "role"), ("ada.obi@acme.com.ng", "named"), ("acme@gmail.com", "freemail"),
            ("dev@webagency.com", "external"), ("sales@mail.acme.com.ng", "role"),
        ],
    )
    def test_classify(self, address_, kind):
        assert classify_email(address_, "acme.com.ng") == kind


class TestPhone:
    @pytest.mark.parametrize(
        "raw", ["0803 123 4567", "08031234567", "+234 803 123 4567", "+234(0)8031234567", "234-803-123-4567"]
    )
    def test_normalises_to_e164(self, raw):
        assert normalize_phone(raw, "NG") == "+2348031234567"

    def test_reads_text_and_tel_links(self):
        body = 'Call 0803 123 4567 or <a href="tel:+2348091234567">now</a>'
        assert values(phone.extract(page(body), "NG")) == {"+2348031234567", "+2348091234567"}

    def test_reads_whatsapp_links(self):
        body = '<a href="https://wa.me/2348031234567">Chat</a><a href="https://api.whatsapp.com/send?phone=2348091234567">Chat</a>'
        assert values(phone.extract(page(body), "NG")) == {"+2348031234567", "+2348091234567"}

    def test_keeps_foreign_numbers_with_country_code(self):
        assert values(phone.extract(page("London: +44 20 7946 0958"), "NG")) == {"+442079460958"}

    def test_ignores_dates_ids_and_short_numbers(self):
        assert values(phone.extract(page("Founded 2019-2024. Ref 12345. Sold 1,000,000 units."), "NG")) == set()

    def test_bad_tel_link_is_dropped(self):
        assert values(phone.extract(page('<a href="tel:123">x</a>'), "NG")) == set()


class TestSocials:
    @pytest.mark.parametrize(
        ("raw", "network", "clean"),
        [
            ("https://www.facebook.com/AcmeRealty/?ref=page&fbclid=abc", "facebook", "https://facebook.com/AcmeRealty"),
            ("https://web.facebook.com/AcmeRealty/posts/123", "facebook", "https://facebook.com/AcmeRealty"),
            ("https://facebook.com/profile.php?id=1000&ref=x", "facebook", "https://facebook.com/profile.php?id=1000"),
            ("https://www.instagram.com/AcmeRealty/?hl=en&igshid=x", "instagram", "https://instagram.com/acmerealty"),
            ("https://www.linkedin.com/company/acme-realty/about/?trk=x", "linkedin", "https://linkedin.com/company/acme-realty"),
            ("https://ng.linkedin.com/in/ada-obi", "linkedin", "https://linkedin.com/in/ada-obi"),
            ("https://twitter.com/AcmeRealty?lang=en", "x", "https://x.com/AcmeRealty"),
            ("https://x.com/@AcmeRealty/status/99", "x", "https://x.com/AcmeRealty"),
            ("https://www.tiktok.com/@acmerealty/video/1?is_from_webapp=1", "tiktok", "https://tiktok.com/@acmerealty"),
        ],
    )
    def test_cleans_profile_urls(self, raw, network, clean):
        assert socials.canonicalize(raw) == (network, clean)

    @pytest.mark.parametrize(
        "raw",
        [
            "https://www.facebook.com/sharer/sharer.php?u=x", "https://www.facebook.com/plugins/page.php", "https://facebook.com/",
            "https://twitter.com/intent/tweet?text=x", "https://x.com/share", "https://instagram.com/p/ABC123/",
            "https://www.linkedin.com/shareArticle?url=x", "https://www.linkedin.com/feed/", "https://tiktok.com/discover",
            "https://notfacebook.com/acme", "https://acme.com/facebook.com/acme",
        ],
    )
    def test_rejects_non_profiles(self, raw):
        assert socials.canonicalize(raw) is None

    def test_extract_scores_footer_above_body_and_company_above_person(self):
        body = (
            '<a href="https://instagram.com/inbody">a</a>'
            '<footer><a href="https://instagram.com/infooter">b</a></footer>'
            '<a href="https://linkedin.com/company/acme">c</a><a href="https://linkedin.com/in/someone">d</a>'
        )
        scores = {fact.value: fact.confidence for fact in socials.extract(page(body))}
        assert scores["https://instagram.com/infooter"] > scores["https://instagram.com/inbody"]
        assert scores["https://linkedin.com/company/acme"] > scores["https://linkedin.com/in/someone"]


class TestRegion:
    @pytest.mark.parametrize(
        ("domain", "region"),
        [
            ("acme.de", "DE"), ("acme.co.uk", "GB"), ("acme.ca", "CA"), ("acme.com.ng", "NG"), ("acme.fr", "FR"),
            ("acme.com", None), ("acme.io", None), ("acme.ai", None), ("acme.co", None), ("acme.tech", None),
        ],
    )
    def test_from_domain(self, domain, region):
        assert region_from_domain(domain) == region

    @pytest.mark.parametrize(
        ("raw", "code"),
        [("DE", "DE"), ("de", "DE"), ("Germany", "DE"), ("UK", "GB"), ("USA", "US"), ("Atlantis", None), (None, None)],
    )
    def test_country_code(self, raw, code):
        assert country_code(raw) == code

    @pytest.mark.parametrize(
        ("html", "region"),
        [
            ('<html lang="en-GB"><body>Hello</body></html>', "GB"),
            ('<html lang="de"><body>Hallo</body></html>', "DE"),
            ("<html><body>910 Louisiana St, Houston, TX 77002</body></html>", "US"),
            ("<html><body>100 King St W, Toronto, ON M5X 1A9</body></html>", "CA"),
            ("<html><body>Proudly based in Nigeria. Nigeria's best. We also ship to Canada.</body></html>", "NG"),
            ('<html><head><script type="application/ld+json">{"@type":"Store","address":{"addressCountry":"Italy"}}</script></head></html>', "IT"),
        ],
    )
    def test_detects_from_page(self, html, region):
        assert detect_region(ParsedPage(html, "https://acme.com/"), None, "US") == (region, True)

    @pytest.mark.parametrize(
        ("name", "code"),
        [
            ("Japan", "JP"), ("Brazil", "BR"), ("India", "IN"), ("South Africa", "ZA"), ("Australia", "AU"),
            ("Mexico", "MX"), ("United Arab Emirates", "AE"), ("Kenya", "KE"), ("Myanmar", "MM"), ("Uzbekistan", "UZ"),
        ],
    )
    def test_every_country_is_known_by_name(self, name, code):
        assert country_code(name) == code
        assert detect_region(ParsedPage(f"<body>Based in {name}.</body>", "https://acme.com/"), None, "US").region == code

    @pytest.mark.parametrize(
        "text",
        [
            "Serving Georgia since 1990", "Founded by Jordan Smith", "Call Chad today", "Roast turkey on the menu",
            "Offices across New Mexico",
        ],
    )
    def test_ambiguous_words_are_not_read_as_countries(self, text):
        assert detect_region(ParsedPage(f"<body>{text}</body>", "https://acme.com/"), None, "US") == ("US", False)

    def test_order_of_trust(self):
        page = ParsedPage('<html lang="en-GB"><body>Germany</body></html>', "https://acme.fr/")
        assert detect_region(page, "CA", "US").region == "CA"
        assert detect_region(page, None, "US").region == "FR"

    @pytest.mark.parametrize(
        "html",
        ["<html><body>Hello</body></html>", '<html lang="en"><body>Hi</body></html>', "<body>We ship to Canada and Germany</body>"],
    )
    def test_no_clues_is_not_confident(self, html):
        assert detect_region(ParsedPage(html, "https://acme.com/"), None, "US") == ("US", False)


class TestAddress:
    HINTS = ["Lagos", "Ikeja"]

    @pytest.mark.parametrize(
        ("body", "expected"),
        [
            ("<p>910 Louisiana St, Houston, TX 77002</p>", "910 Louisiana St, Houston, TX 77002"),
            ("<p>910 Louisiana St</p><p>Houston, TX 77002</p>", "910 Louisiana St, Houston, TX 77002"),
            ("<p>100 King St W, Toronto, ON M5X 1A9</p>", "100 King St W, Toronto, ON M5X 1A9"),
            ("<p>10 Downing Street, London SW1A 2AA</p>", "10 Downing Street, London SW1A 2AA"),
            ("<p>Hauptstraße 12</p><p>10115 Berlin</p>", "Hauptstraße 12, 10115 Berlin"),
            ("<p>12 rue de la Paix, 75002 Paris</p>", "12 rue de la Paix, 75002 Paris"),
            ("<p>Via Roma 1, 00184 Roma</p>", "Via Roma 1, 00184 Roma"),
            ("<p>Anschrift: Kerkstraat 5, 1017 Amsterdam</p>", "Kerkstraat 5, 1017 Amsterdam"),
        ],
    )
    def test_international_formats(self, body, expected):
        assert expected in values(address.extract(page(body), []))

    def test_hints_match_whole_words_only(self):
        assert values(address.extract(page("<p>Office: thousand, more</p>"), ["usa"])) == set()

    def test_any_country_counts_as_a_place_without_configuring_hints(self):
        assert values(address.extract(page("<p>Office: Shinjuku, Tokyo, Japan</p>"), [])) == {"Shinjuku, Tokyo, Japan"}

    def test_a_country_alone_is_not_an_address(self):
        assert values(address.extract(page("<p>Location: United States</p>"), ["United States"])) == set()

    def test_address_tag_drops_contact_details(self):
        body = "<address>12 Allen Avenue<br>Ikeja, Lagos<br>Tel: 0803 123 4567<br>info@acme.com</address>"
        assert values(address.extract(page(body), self.HINTS)) == {"12 Allen Avenue, Ikeja, Lagos"}

    def test_microdata(self):
        body = '<div itemprop="address"><span>5 Marina Road</span> <span>Lagos</span></div>'
        assert values(address.extract(page(body), self.HINTS)) == {"5 Marina Road, Lagos"}

    def test_label_on_same_line_and_next_line(self):
        assert values(address.extract(page("<p>Address: 7 Broad Street, Lagos</p>"), self.HINTS)) == {"7 Broad Street, Lagos"}
        assert values(address.extract(page("<p>Head Office:</p><p>7 Broad Street, Lagos</p>"), self.HINTS)) == {"7 Broad Street, Lagos"}

    def test_street_pattern(self):
        assert values(address.extract(page("<p>23 Admiralty Way, Lekki Phase 1</p>"), self.HINTS)) == {"23 Admiralty Way, Lekki Phase 1"}

    @pytest.mark.parametrize(
        "text",
        [
            "We have 20 years on the road, serving you well", "Location: nearby", "Our 5 way plan, for you",
            "Copyright 2024. All rights reserved.",
        ],
    )
    def test_ignores_prose(self, text):
        assert values(address.extract(page(f"<p>{text}</p>"), self.HINTS)) == set()


LOCAL_BUSINESS = {
    "@context": "https://schema.org",
    "@type": "RealEstateAgent",
    "name": "Acme Realty Ltd",
    "description": "Trusted property agents in Lagos.",
    "email": "mailto:Info@acme.com.ng",
    "telephone": "0803 123 4567",
    "address": {
        "@type": "PostalAddress", "streetAddress": "12 Allen Avenue", "addressLocality": "Ikeja",
        "addressRegion": "Lagos", "addressCountry": {"@type": "Country", "name": "NG"},
    },
    "sameAs": ["https://www.facebook.com/acme/?ref=x", "https://x.com/acme", "https://acme.com.ng/other"],
    "contactPoint": [{"@type": "ContactPoint", "telephone": "+234 809 123 4567", "email": "sales@acme.com.ng"}],
}


def ld(data, raw: bool = False) -> str:
    return f'<script type="application/ld+json">{data if raw else json.dumps(data)}</script>'


class TestJsonLd:
    def test_local_business_fields(self):
        facts = jsonld.extract(page("", ld(LOCAL_BUSINESS)), "NG")
        assert values(facts, "name") == {"Acme Realty Ltd"}
        assert values(facts, "email") == {"info@acme.com.ng", "sales@acme.com.ng"}
        assert values(facts, "phone") == {"+2348031234567", "+2348091234567"}
        assert values(facts, "address") == {"12 Allen Avenue, Ikeja, Lagos, NG"}
        assert values(facts, "facebook") == {"https://facebook.com/acme"}
        assert values(facts, "x") == {"https://x.com/acme"}
        assert values(facts, "category") == {"Real Estate Agent"}
        assert all(fact.source_url == URL and fact.method == "json_ld" for fact in facts)

    def test_graph_and_lists(self):
        graph = {"@graph": [{"@type": "WebSite", "name": "Not this"}, {"@type": "Organization", "name": "Acme"}]}
        assert values(jsonld.extract(page("", ld(graph)), "NG"), "name") == {"Acme"}
        assert values(jsonld.extract(page("", ld([{"@type": "Bakery", "name": "Buns", "address": "1 Bun Road, Lagos"}])), "NG"), "name") == {"Buns"}

    @pytest.mark.parametrize("kind", ["Person", "Article", "Product", "WebSite", "BreadcrumbList"])
    def test_ignores_non_businesses(self, kind):
        assert jsonld.extract(page("", ld({"@type": kind, "name": "Nope", "telephone": "08031234567"})), "NG") == []

    def test_broken_json_does_not_stop_other_blocks(self):
        head = ld("{ not json", raw=True) + ld({"@type": "LocalBusiness", "name": "Good"})
        assert values(jsonld.extract(page("", head), "NG"), "name") == {"Good"}

    def test_cdata_and_raw_newlines_are_tolerated(self):
        block = '//<![CDATA[\n{"@type":"LocalBusiness","name":"Wrapped","description":"line one\nline two here"}\n//]]>'
        assert values(jsonld.extract(page("", ld(block, raw=True)), "NG"), "name") == {"Wrapped"}

    def test_scores_higher_than_every_other_source(self):
        head = ld(LOCAL_BUSINESS) + '<meta property="og:site_name" content="Other Name"><title>Third Name</title>'
        json_score = max(fact.confidence for fact in jsonld.extract(page("", head), "NG") if fact.field == "name")
        meta_score = max(fact.confidence for fact in metadata.extract(page("", head)) if fact.field == "name")
        assert json_score > meta_score


class TestMetadata:
    @pytest.mark.parametrize(
        ("title", "domain", "name"),
        [
            ("Contact Us | Acme Realty", "acmerealty.com", "Acme Realty"),
            ("Acme Realty - Home", "acmerealty.com", "Acme Realty"),
            ("Welcome to Acme Realty", "acmerealty.com", "Acme Realty"),
            ("Best Homes in Lagos | Acme Realty", "acmerealty.com", "Acme Realty"),
            ("Home", "acmerealty.com", None),
        ],
    )
    def test_name_from_title(self, title, domain, name):
        assert name_from_title(title, domain) == name

    def test_reads_og_title_logo_and_description(self):
        head = (
            '<title>Home | Acme</title><meta property="og:site_name" content="Acme Site">'
            '<meta name="description" content="We sell homes in Lagos.">'
        )
        facts = metadata.extract(page('<header><a href="/"><img src="l.png" alt="Acme Logo"></a></header>', head, "https://acme.com/"))
        assert values(facts, "name") == {"Acme Site", "Acme"}
        assert values(facts, "description") == {"We sell homes in Lagos."}

    def test_homepage_description_outranks_subpage(self):
        head = '<meta name="description" content="Some description here.">'
        home = metadata.extract(page("", head, "https://acme.com/"))[0]
        inner = metadata.extract(page("", head, "https://acme.com/blog/post"))[0]
        assert home.confidence > inner.confidence


class TestLinks:
    def test_stays_on_site_skips_junk_and_sorts_by_priority(self):
        body = """
          <a href="/blog/x">Blog</a> <a href="/contact">Contact</a> <a href="https://other.com/contact">Other</a>
          <a href="/logo.png">Logo</a> <a href="/login">Login</a> <a href="mailto:a@b.com">Mail</a>
          <a href="/about#team">About</a> <a href="https://www.acme.com.ng/about">About again</a>
        """
        links = extract_links(page(body, url="https://acme.com.ng/"), "acme.com.ng", 50)
        assert [link.url for link in links][0] == "https://acme.com.ng/contact"
        assert len(links) == 3
        assert not any("other.com" in link.url or "login" in link.url or "png" in link.url for link in links)

    def test_limit_keeps_the_best_links(self):
        body = "".join(f'<a href="/p{i}">x</a>' for i in range(50)) + '<a href="/contact">c</a>'
        links = extract_links(page(body, url="https://acme.com.ng/"), "acme.com.ng", 5)
        assert len(links) == 5 and links[0].url.endswith("/contact")

    def test_respects_base_href(self):
        links = extract_links(page('<a href="contact">c</a>', '<base href="https://acme.com.ng/sub/">'), "acme.com.ng", 5)
        assert links[0].url == "https://acme.com.ng/sub/contact"


class TestPdf:
    URL = "https://college.edu/files/contractors.pdf"

    def test_each_business_in_the_list_is_its_own_lead(self):
        body = make_pdf([
            "Cooley Construction  Ben Builder  760-555-0100  ben@cooley-builders.com",
            "Kerns Inc.  estimating@kerns-paving.com  sales@mail.kerns-paving.com",
            "Desert Construction  desert.builder.test@aol.com",
            "Jergensens  jergensen.test@msn.com",
        ])
        leads = extract_document(body, self.URL)
        assert {key: values(facts) for key, facts in leads.items()} == {
            "cooley-builders.com": {"ben@cooley-builders.com"},
            "kerns-paving.com": {"estimating@kerns-paving.com", "sales@mail.kerns-paving.com"},
            # Freemail says nothing about the company, so each address stands alone.
            "desert.builder.test@aol.com": {"desert.builder.test@aol.com"},
            "jergensen.test@msn.com": {"jergensen.test@msn.com"},
        }
        assert all(fact.source_url == self.URL for facts in leads.values() for fact in facts)

    def test_reads_mailto_links_as_well_as_text(self):
        leads = extract_document(make_pdf(["Contact the office"], links=("mailto:office@acme.com",)), self.URL)
        assert [(fact.value, fact.method) for fact in leads["acme.com"]] == [("office@acme.com", "pdf_mailto")]

    def test_junk_and_incomplete_addresses_are_dropped(self):
        leads = extract_document(make_pdf(["eric@roadway-engineering  noreply@acme.com  you@example.com"]), self.URL)
        assert leads == {}

    @pytest.mark.parametrize("body", [b"<html>Access denied</html>", b"%PDF-1.4 truncated nonsense"])
    def test_not_a_readable_pdf(self, body):
        with pytest.raises(UnreadableDocument):
            extract_document(body, self.URL)


class TestSpreadsheets:
    URL = "https://college.edu/files/contractors.xlsx"
    EXPECTED = {
        "cooley-builders.com": {"ben@cooley-builders.com"},
        "desert.builder.test@aol.com": {"desert.builder.test@aol.com"},
        "kerns-paving.com": {"estimating@kerns-paving.com"},
    }

    @staticmethod
    def xlsx() -> bytes:
        workbook = openpyxl.Workbook()
        sheet = workbook.active
        sheet.append(["Company", "Email", "Licence"])
        sheet.append(["Cooley Builders", "ben@cooley-builders.com", 812345])
        sheet.append(["Desert Builder", "desert.builder.test@aol.com"])
        sheet["D2"] = "Email the office"
        sheet["D2"].hyperlink = "mailto:estimating@kerns-paving.com"
        buffer = io.BytesIO()
        workbook.save(buffer)
        return buffer.getvalue()

    @staticmethod
    def xls() -> bytes:
        workbook = xlwt.Workbook()
        sheet = workbook.add_sheet("Contractors")
        for row, values in enumerate([
            ("Cooley Builders", "ben@cooley-builders.com"),
            ("Desert Builder", "desert.builder.test@aol.com"),
        ]):
            for column, value in enumerate(values):
                sheet.write(row, column, value)
        workbook.add_sheet("Second").write(0, 0, "estimating@kerns-paving.com")
        buffer = io.BytesIO()
        workbook.save(buffer)
        return buffer.getvalue()

    def found(self, body: bytes) -> dict[str, set[str]]:
        return {key: values(facts) for key, facts in extract_document(body, self.URL).items()}

    def test_xlsx_reads_cells_and_mailto_links(self):
        leads = extract_document(self.xlsx(), self.URL)
        assert {key: values(facts) for key, facts in leads.items()} == self.EXPECTED
        assert [fact.method for fact in leads["kerns-paving.com"]] == ["xlsx_mailto"]

    def test_mailto_written_as_cell_text(self):
        workbook = openpyxl.Workbook()
        workbook.active.append(["ITT", "mailto:%20ann.lee@itt-example.com"])
        buffer = io.BytesIO()
        workbook.save(buffer)
        assert self.found(buffer.getvalue()) == {"itt-example.com": {"ann.lee@itt-example.com"}}

    def test_xls_reads_every_sheet(self):
        assert self.found(self.xls()) == self.EXPECTED

    @pytest.mark.parametrize("name", ["contacts.xlsb", "contacts.xlsm"])
    def test_xlsb_and_xlsm_saved_by_excel(self, name):
        assert self.found((FIXTURES / name).read_bytes()) == self.EXPECTED

    def test_file_type_comes_from_the_content_not_the_link(self):
        leads = extract_document(self.xlsx(), "https://college.edu/files/contractors.xls")
        assert {fact.method for facts in leads.values() for fact in facts} <= {"xlsx_text", "xlsx_mailto"}

    def test_zip_that_is_not_a_spreadsheet(self):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("word/document.xml", "<w/>")
        with pytest.raises(UnreadableDocument, match="not_a_pdf_or_spreadsheet"):
            extract_document(buffer.getvalue(), self.URL)

    def test_refuses_a_spreadsheet_that_unpacks_to_something_huge(self):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("xl/workbook.xml", "<w/>")
            archive.writestr("xl/worksheets/sheet1.xml", b"\0" * 201_000_000)
        with pytest.raises(UnreadableDocument, match="too_large_unpacked"):
            extract_document(buffer.getvalue(), self.URL)

    def test_damaged_spreadsheet(self):
        with pytest.raises(UnreadableDocument, match="unreadable_xls"):
            extract_document(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\0" * 100, self.URL)
