"""Finds public business emails: mailto links, page text and common obfuscation."""

from __future__ import annotations

import re
from urllib.parse import unquote, urlsplit

from crawler.urls import has_public_suffix, registrable_domain
from extractors.page import ParsedPage
from models import Fact

# Nothing matches just after % or &, so "mailto:%20ann@acme.com" doesn't give 20ann@
# and "f&b@acme.com" doesn't give b@.
_NOT_AFTER = r"(?<![\w.+%&-])"
_EMAIL_RE = re.compile(_NOT_AFTER + r"[A-Za-z0-9][A-Za-z0-9._%+-]*@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,24}(?![\w-])")
_STRICT_RE = re.compile(r"^[a-z0-9][a-z0-9._%+-]*@[a-z0-9-]+(?:\.[a-z0-9-]+)*\.[a-z]{2,24}$")

_LOCAL = r"[A-Za-z0-9][A-Za-z0-9._%+-]*"
_LABEL = r"[A-Za-z0-9-]+"
_BRACKET_AT = r"(?:@|\s*[\[({<]\s*at\s*[\])}>]\s*)"
_BRACKET_DOT = r"(?:\.|\s*[\[({<]\s*dot\s*[\])}>]\s*)"
_BRACKETED_RE = re.compile(
    rf"{_NOT_AFTER}({_LOCAL}){_BRACKET_AT}({_LABEL}(?:{_BRACKET_DOT}{_LABEL})*{_BRACKET_DOT}[A-Za-z]{{2,24}})(?![\w-])",
    re.IGNORECASE,
)
_SPELLED_RE = re.compile(
    rf"{_NOT_AFTER}({_LOCAL})\s+(?:at|AT)\s+({_LABEL}(?:\s+(?:dot|DOT)\s+{_LABEL})*\s+(?:dot|DOT)\s+[A-Za-z]{{2,24}})\b"
)
_ANY_DOT_RE = re.compile(r"\s*[\[({<]\s*dot\s*[\])}>]\s*|\s+dot\s+", re.IGNORECASE)
# "Email us at acme dot com" isn't an address, even though it has the same shape.
_SPELLED_NOT_LOCAL = {
    "us", "me", "them", "you", "him", "her", "it", "mail", "email", "contact", "call", "reach",
    "visit", "look", "located", "based", "site", "online", "here", "write",
}

_JUNK_LOCALS = {
    "example", "test", "testing", "email", "e-mail", "name", "yourname", "your", "youremail", "your.email",
    "your-email", "user", "username", "someone", "john.doe", "johndoe", "jane.doe", "firstname.lastname",
    "noreply", "no-reply", "no_reply", "donotreply", "do-not-reply", "do_not_reply", "mailer-daemon",
    "postmaster", "abuse", "sentry", "wordpress", "admin@admin",
}
_JUNK_DOMAINS = {
    "example.com", "example.org", "example.net", "domain.com", "yourdomain.com", "yoursite.com", "website.com",
    "mysite.com", "email.com", "test.com", "sentry.io", "wixpress.com", "wix.com", "wixsite.com",
    "sentry-next.wixpress.com", "squarespace.com", "shopify.com", "cloudflare.com", "schema.org", "w3.org",
    "godaddy.com", "wordpress.com", "wordpress.org", "googleapis.com", "gstatic.com", "mysite.co",
}
_FILE_ENDINGS = (
    ".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".ico", ".bmp", ".avif", ".css", ".js",
    ".json", ".pdf", ".mp4", ".woff", ".woff2", ".ttf",
)
_HASH_LOCAL_RE = re.compile(r"^[0-9a-f]{16,}$")
# Address formats published in place of addresses, like firstname.lastname@acme.com or flast@acme.com.
_PATTERN_WORD = r"(?:first|last|middle|given|sur)?(?:name|initial)|fname|lname|first|last|middle|flast|firstlast"
_PATTERN_LOCAL_RE = re.compile(rf"^(?:{_PATTERN_WORD})(?:[._-]?(?:{_PATTERN_WORD}))*$")
# Words no real address contains, which also catch misspelt formats like firstname.lastnamen@.
_PATTERN_HINT_RE = re.compile(r"firstname|lastnam|middlename|firstinitial|lastinitial|middleinitial")

FREEMAIL_DOMAINS = {
    "gmail.com", "googlemail.com", "yahoo.com", "yahoo.co.uk", "ymail.com", "rocketmail.com", "hotmail.com",
    "hotmail.co.uk", "outlook.com", "live.com", "msn.com", "icloud.com", "me.com", "aol.com", "proton.me",
    "protonmail.com", "zoho.com", "gmx.com", "mail.com", "gmx.de", "gmx.net", "web.de", "t-online.de", "orange.fr",
    "free.fr", "wanadoo.fr", "laposte.net", "yahoo.fr", "yahoo.de", "yahoo.es", "yahoo.it", "yahoo.ca", "libero.it",
    "virgilio.it", "btinternet.com", "sky.com", "comcast.net", "verizon.net", "att.net", "sbcglobal.net",
    "bellnet.ca", "rogers.com", "shaw.ca", "telus.net", "mail.ru", "yandex.ru", "yandex.com", "qq.com", "163.com",
    "126.com", "naver.com", "daum.net", "yahoo.co.jp", "yahoo.co.in", "yahoo.com.br", "uol.com.br", "rediffmail.com",
}
_ROLE_LOCALS = {
    "info", "contact", "contactus", "hello", "hi", "sales", "enquiries", "enquiry", "inquiries", "inquiry",
    "support", "help", "admin", "office", "mail", "team", "bookings", "booking", "reservations", "customercare",
    "customerservice", "care", "service", "services", "marketing", "accounts", "billing", "hr", "careers",
    "jobs", "press", "media", "partnerships", "business", "general", "reception", "orders", "ops",
    "estimating", "quotes", "quote", "dispatch", "parts", "kontakt", "vertrieb", "verkauf", "contacto", "ventas",
    "ventes", "contatto", "vendite", "commercial", "bureau", "buero", "büro",
}


def normalize_email(raw: str) -> str | None:
    """Lowercase and validate an address. Returns None for invalid or junk addresses."""
    email = unquote(raw).strip().strip(".,;:!?'\"<>()[]{}").lower()
    if not _STRICT_RE.match(email) or len(email) > 254:
        return None
    local, domain = email.rsplit("@", 1)
    if len(local) > 64 or ".." in email:
        return None
    if email.endswith(_FILE_ENDINGS):
        return None
    if local in _JUNK_LOCALS or _HASH_LOCAL_RE.match(local):
        return None
    if _PATTERN_LOCAL_RE.match(local) or _PATTERN_HINT_RE.search(local):
        return None
    if domain in _JUNK_DOMAINS or registrable_domain(f"https://{domain}") in _JUNK_DOMAINS:
        return None
    if not has_public_suffix(f"https://{domain}"):
        return None
    return email


def classify_email(email: str, site_domain: str) -> str:
    """role: info@acme.com, named: ada@acme.com, freemail: acme@gmail.com, external: another company's domain."""
    local, domain = email.rsplit("@", 1)
    if domain in FREEMAIL_DOMAINS:
        return "freemail"
    if registrable_domain(f"https://{domain}") != site_domain:
        return "external"
    return "role" if local.split("+")[0] in _ROLE_LOCALS else "named"


def decode_cloudflare(encoded: str) -> str | None:
    """Decode Cloudflare's email protection: the first byte is an XOR key for the rest."""
    try:
        data = bytes.fromhex(encoded)
    except ValueError:
        return None
    if len(data) < 2:
        return None
    key = data[0]
    try:
        return bytes(byte ^ key for byte in data[1:]).decode("utf-8")
    except UnicodeDecodeError:
        return None


def extract(page: ParsedPage) -> list[Fact]:
    found: dict[str, tuple[str, float]] = {}

    def add(raw: str | None, method: str, confidence: float) -> None:
        email = normalize_email(raw) if raw else None
        if email and (email not in found or found[email][1] < confidence):
            found[email] = (method, confidence)

    for raw in _from_links(page):
        add(raw, "mailto", 0.95)
    for raw in _from_cloudflare(page):
        add(raw, "cloudflare_decoded", 0.9)
    for match in _EMAIL_RE.finditer(page.flat_text):
        add(match.group(0), "text", 0.8)
    for match in _BRACKETED_RE.finditer(page.flat_text):
        add(f"{match.group(1)}@{_ANY_DOT_RE.sub('.', match.group(2))}", "deobfuscated", 0.75)
    for match in _SPELLED_RE.finditer(page.flat_text):
        if match.group(1).lower() not in _SPELLED_NOT_LOCAL:
            add(f"{match.group(1)}@{_ANY_DOT_RE.sub('.', match.group(2))}", "deobfuscated", 0.6)

    return [
        Fact(field="email", value=email, source_url=page.url, method=method, confidence=confidence)
        for email, (method, confidence) in found.items()
    ]


def _from_links(page: ParsedPage) -> list[str]:
    results: list[str] = []
    for anchor in page.anchors:
        if anchor.href.lower().startswith("mailto:"):
            address = urlsplit(anchor.href).path or anchor.href[7:].split("?")[0]
            results.extend(part for part in address.split(",") if part)
    return results


def _from_cloudflare(page: ParsedPage) -> list[str | None]:
    encoded = [node.attributes.get("data-cfemail") or "" for node in page.tree.css("[data-cfemail]")]
    marker = "/cdn-cgi/l/email-protection#"
    encoded.extend(a.href.split(marker, 1)[1] for a in page.anchors if marker in a.href)
    return [decode_cloudflare(value) for value in encoded if value]
