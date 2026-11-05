"""URL rules: normalising, identifying duplicates, same-domain checks and skip lists."""

from __future__ import annotations

import ipaddress
import posixpath
import re
from urllib.parse import parse_qsl, quote, urlencode, urljoin, urlsplit, urlunsplit

import tldextract

# Uses the suffix list bundled with tldextract, so no download happens at runtime.
# Private suffixes matter: foo.blogspot.com and bar.blogspot.com are different businesses.
_EXTRACT = tldextract.TLDExtract(suffix_list_urls=(), cache_dir=None, include_psl_private_domains=True)

_TRACKING_PARAMS = {
    "fbclid", "gclid", "gclsrc", "dclid", "msclkid", "mc_cid", "mc_eid", "igshid", "igsh",
    "yclid", "_ga", "_gl", "ref", "ref_src", "ref_url", "spm", "si", "s_kwcid", "trk", "trkinfo",
}
_TRACKING_PREFIXES = ("utm_", "hsa_", "pk_", "mtm_")

_SKIP_EXTENSIONS = {
    "png", "jpg", "jpeg", "gif", "svg", "webp", "bmp", "ico", "tif", "tiff", "avif", "heic",
    "pdf", "doc", "docx", "xls", "xlsx", "xlsm", "xlsb", "ppt", "pptx", "odt", "csv", "rtf",
    "zip", "rar", "7z", "tar", "gz", "exe", "dmg", "apk", "msi",
    "mp3", "wav", "ogg", "m4a", "mp4", "avi", "mov", "wmv", "webm", "mkv",
    "css", "js", "json", "xml", "rss", "atom", "woff", "woff2", "ttf", "eot", "map",
}
DOCUMENT_EXTENSIONS = (".pdf", ".xls", ".xlsx", ".xlsm", ".xlsb")
_SKIP_PATH_RE = re.compile(
    r"(?:^|/)(?:log-?in|sign-?in|sign-?up|sign-?out|log-?out|register|registration|cart|basket|checkout|"
    r"my-?account|account|wp-admin|wp-login\.php|wp-json|xmlrpc\.php|admin|forgot-password|"
    r"password-reset|wishlist|feed|anmelden|warenkorb|kasse|mein-konto|konto|panier|connexion|mon-compte|"
    r"carrito|carrello|winkelwagen)(?:/|$)",
    re.IGNORECASE,
)
_INDEX_PAGE_RE = re.compile(r"/(?:index|default)\.(?:html?|php|aspx?)$", re.IGNORECASE)
_SKIP_QUERY_RE = re.compile(r"(?:^|&)(?:add-to-cart|add_to_cart|replytocom|action=(?:login|logout|register))", re.I)


def normalize_url(url: str, base: str | None = None) -> str | None:
    """Return a canonical http(s) URL, or None if it isn't one we could fetch."""
    url = (url or "").strip()
    if not url:
        return None
    try:
        if base:
            url = urljoin(base, url)
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return None

    scheme = parts.scheme.lower()
    host = (parts.hostname or "").rstrip(".").lower()
    if scheme not in {"http", "https"} or not host:
        return None
    try:
        host = host.encode("idna").decode("ascii")
    except UnicodeError:
        return None
    if ":" in host:
        host = f"[{host}]"
    if port and not (scheme == "http" and port == 80) and not (scheme == "https" and port == 443):
        host = f"{host}:{port}"

    return urlunsplit((scheme, host, _normalize_path(parts.path), _normalize_query(parts.query), ""))


def _normalize_path(path: str) -> str:
    trailing = path.endswith("/")
    path = re.sub(r"/{2,}", "/", path or "/")
    path = posixpath.normpath(path) if path != "/" else "/"
    if not path.startswith("/"):
        path = "/" + path
    if trailing and not path.endswith("/"):
        path += "/"
    path = quote(path, safe="/%:@!$&'()*+,;=-._~")
    return re.sub(r"%[0-9a-fA-F]{2}", lambda m: m.group(0).upper(), path)


def _normalize_query(query: str) -> str:
    pairs = [
        (key, value)
        for key, value in parse_qsl(query, keep_blank_values=True)
        if key.lower() not in _TRACKING_PARAMS and not key.lower().startswith(_TRACKING_PREFIXES)
    ]
    return urlencode(sorted(pairs))


def url_key(url: str) -> str:
    """Identity used for "have we seen this page?".

    http/https, www/non-www, a trailing slash and /index.html almost always serve the
    same page, so they count as one URL. Otherwise we'd spend a site's page budget on copies.
    """
    parts = urlsplit(url)
    host = parts.netloc.lower().removeprefix("www.")
    path = _INDEX_PAGE_RE.sub("", parts.path).rstrip("/")
    return f"{host}{path}?{parts.query}" if parts.query else f"{host}{path}"


def registrable_domain(url: str) -> str:
    """The domain a business owns: blog.acme.com.ng -> acme.com.ng. Empty if there's no host."""
    host = _hostname(url)
    if not host:
        return ""
    if _is_ip(host):
        return host
    extracted = _EXTRACT(host)
    if extracted.domain and extracted.suffix:
        return f"{extracted.domain}.{extracted.suffix}"
    return host


def has_public_suffix(url: str) -> bool:
    host = _hostname(url)
    return bool(host) and bool(_EXTRACT(host).suffix)


def same_site(url: str, domain: str) -> bool:
    return registrable_domain(url) == domain


def origin(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}"


def skip_reason(url: str, allow_documents: bool = False) -> str | None:
    """Why a URL shouldn't be crawled (file, login, cart and so on), or None if it's fine.

    `allow_documents` lets a PDF or spreadsheet through, for ones given as seeds.
    """
    parts = urlsplit(url)
    last_segment = parts.path.rsplit("/", 1)[-1]
    if _SKIP_PATH_RE.search(parts.path):
        return "private_area"
    if _SKIP_QUERY_RE.search(parts.query):
        return "private_area"
    if allow_documents and is_document_url(url):
        return None
    if "." in last_segment and last_segment.rsplit(".", 1)[-1].lower() in _SKIP_EXTENSIONS:
        return "file_type"
    return None


def is_document_url(url: str) -> bool:
    """PDFs and spreadsheets are read only when given as seeds. Links to them on sites are still skipped."""
    return urlsplit(url).path.lower().endswith(DOCUMENT_EXTENSIONS)


def _hostname(url: str) -> str:
    try:
        return (urlsplit(url).hostname or "").lower()
    except ValueError:
        return ""


def _is_ip(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return False
    return True
