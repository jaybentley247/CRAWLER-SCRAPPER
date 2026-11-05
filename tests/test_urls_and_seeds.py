import pytest

from crawler.links import score_link
from crawler.seeds import RejectedSeed, Seed, load_seeds, parse_seed
from crawler.urls import normalize_url, registrable_domain, same_site, skip_reason, url_key


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("HTTPS://Acme.COM/About", "https://acme.com/About"),
        ("https://acme.com", "https://acme.com/"),
        ("https://acme.com:443/a", "https://acme.com/a"),
        ("http://acme.com:80/a", "http://acme.com/a"),
        ("https://acme.com:8443/a", "https://acme.com:8443/a"),
        ("https://acme.com/a/../b/./c", "https://acme.com/b/c"),
        ("https://acme.com//a///b/", "https://acme.com/a/b/"),
        ("https://acme.com/a#section", "https://acme.com/a"),
        ("https://acme.com/a?utm_source=x&gclid=1&fbclid=2", "https://acme.com/a"),
        ("https://acme.com/a?b=2&a=1", "https://acme.com/a?a=1&b=2"),
        ("https://acme.com/a b", "https://acme.com/a%20b"),
        ("https://acme.com/%7ea", "https://acme.com/%7Ea"),
    ],
)
def test_normalize_url(raw, expected):
    assert normalize_url(raw) == expected


@pytest.mark.parametrize("raw", ["mailto:a@b.com", "javascript:void(0)", "tel:+234", "ftp://a.com/x", "", "http://", "https://[bad/"])
def test_normalize_url_rejects_unfetchable(raw):
    assert normalize_url(raw) is None


def test_normalize_resolves_relative_links():
    assert normalize_url("../contact", base="https://acme.com/a/b/") == "https://acme.com/a/contact"


def test_url_key_treats_variants_as_the_same_page():
    variants = ["https://www.acme.com/about/", "http://acme.com/about", "https://acme.com/about"]
    assert len({url_key(normalize_url(url)) for url in variants}) == 1


@pytest.mark.parametrize(
    ("first", "second"),
    [
        ("https://acme.com/", "https://acme.com/index.html"),
        ("https://acme.com/", "https://acme.com/INDEX.PHP"),
        ("https://acme.com/about/", "https://acme.com/about/default.aspx"),
    ],
)
def test_url_key_treats_index_files_as_their_folder(first, second):
    assert url_key(first) == url_key(second)


def test_url_key_keeps_pages_that_only_resemble_index_files_apart():
    assert url_key("https://acme.com/") != url_key("https://acme.com/index-of-homes.html")
    assert url_key("https://acme.com/") != url_key("https://acme.com/myindex.html")


def test_url_key_keeps_different_queries_apart():
    assert url_key("https://acme.com/a?p=1") != url_key("https://acme.com/a?p=2")


@pytest.mark.parametrize(
    ("url", "domain"),
    [
        ("https://blog.acme.com/x", "acme.com"),
        ("https://www.acme.com.ng/", "acme.com.ng"),
        ("https://acme.ng/", "acme.ng"),
        ("https://acme.blogspot.com/", "acme.blogspot.com"),
        ("http://127.0.0.1:8000/", "127.0.0.1"),
    ],
)
def test_registrable_domain(url, domain):
    assert registrable_domain(url) == domain


def test_same_site_separates_businesses_on_a_shared_suffix():
    assert same_site("https://foo.com.ng/", "foo.com.ng")
    assert not same_site("https://bar.com.ng/", "foo.com.ng")
    assert not same_site("https://acme.com.evil.net/", "acme.com")


@pytest.mark.parametrize(
    "url",
    [
        "https://a.com/logo.PNG", "https://a.com/brochure.pdf", "https://a.com/login", "https://a.com/wp-login.php",
        "https://a.com/cart/", "https://a.com/shop/checkout", "https://a.com/my-account/orders", "https://a.com/sign-in",
        "https://a.com/?add-to-cart=12",
    ],
)
def test_skip_reason_blocks_files_and_private_areas(url):
    assert skip_reason(url)


@pytest.mark.parametrize("url", ["https://a.com/", "https://a.com/contact", "https://a.com/cartography", "https://a.com/about.html"])
def test_skip_reason_allows_normal_pages(url):
    assert skip_reason(url) is None


def test_contact_pages_outrank_about_team_and_the_rest():
    contact = score_link("https://a.com/contact-us")
    about = score_link("https://a.com/about")
    team = score_link("https://a.com/team")
    plain = score_link("https://a.com/portfolio")
    blog = score_link("https://a.com/blog/some-post")
    assert contact > about > team > plain > blog


def test_anchor_text_and_footer_raise_priority():
    assert score_link("https://a.com/p1", "Contact us") > score_link("https://a.com/p1", "Read more")
    assert score_link("https://a.com/x", in_footer=True) > score_link("https://a.com/x")


def test_parse_seed_adds_https_and_rejects_social_sites():
    seed = parse_seed("acme.com.ng", ["facebook.com"])
    assert isinstance(seed, Seed) and seed.url == "https://acme.com.ng/" and seed.domain == "acme.com.ng"
    assert isinstance(parse_seed("https://facebook.com/acme", ["facebook.com"]), RejectedSeed)
    assert isinstance(parse_seed("localhost:8000", []), RejectedSeed)
    assert isinstance(parse_seed("https://acme.com/brochure.docx", []), RejectedSeed)


def test_pdf_seed_is_accepted_but_pdf_links_are_still_skipped():
    seed = parse_seed("https://www.college.edu/files/2021-10/WP%20LIST.pdf", [])
    assert isinstance(seed, Seed) and seed.domain == "college.edu"
    assert seed.url == "https://www.college.edu/files/2021-10/WP%20LIST.pdf"
    assert skip_reason("https://acme.com/brochure.pdf") == "file_type"
    for ending in ("xls", "xlsx", "xlsm", "xlsb", "XLSX"):
        assert isinstance(parse_seed(f"https://acme.com/forms/contacts.{ending}", []), Seed)
        assert skip_reason(f"https://acme.com/forms/contacts.{ending}") == "file_type"
    assert isinstance(parse_seed("https://acme.com/login/list.pdf", []), RejectedSeed)


def test_load_txt_skips_comments_blanks_and_duplicates(tmp_path):
    path = tmp_path / "seeds.txt"
    path.write_text("# my list\n\nacme.com\nhttps://acme.com/\nbeta.ng\nnot a url at all\n", encoding="utf-8")
    seeds, rejected = load_seeds(path, [])
    assert [seed.url for seed in seeds] == ["https://acme.com/", "https://beta.ng/"]
    assert len(rejected) == 1


def test_load_csv_with_header_reads_hints(tmp_path):
    path = tmp_path / "seeds.csv"
    path.write_text("﻿Name,Website,Category\nAcme Realty,acme.com,Real Estate\n,beta.ng,\n", encoding="utf-8")
    seeds, _ = load_seeds(path, [])
    assert seeds[0] == Seed("https://acme.com/", "acme.com", "Acme Realty", "Real Estate")
    assert seeds[1].name is None


def test_load_csv_reads_country_and_rejects_unknown_ones(tmp_path):
    path = tmp_path / "seeds.csv"
    path.write_text("website,country\nacme.com,Germany\nbeta.com,uk\ngamma.com,\ndelta.com,Atlantis\n", encoding="utf-8")
    seeds, rejected = load_seeds(path, [])
    assert [(seed.domain, seed.country) for seed in seeds] == [("acme.com", "DE"), ("beta.com", "GB"), ("gamma.com", None)]
    assert len(rejected) == 1 and "Atlantis" in rejected[0].reason


@pytest.mark.parametrize("url", ["https://a.de/warenkorb", "https://a.de/mein-konto/x", "https://a.fr/panier", "https://a.fr/connexion"])
def test_skip_reason_covers_european_private_areas(url):
    assert skip_reason(url)


def test_load_csv_without_header_uses_first_column(tmp_path):
    path = tmp_path / "seeds.csv"
    path.write_text("acme.com,anything\nbeta.ng,else\n", encoding="utf-8")
    seeds, _ = load_seeds(path, [])
    assert [seed.domain for seed in seeds] == ["acme.com", "beta.ng"]
