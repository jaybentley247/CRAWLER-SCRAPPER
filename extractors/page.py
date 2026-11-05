"""Parses a page once and gives every extractor the same view of it."""

from __future__ import annotations

from typing import NamedTuple
from urllib.parse import urlsplit

from selectolax.lexbor import LexborHTMLParser as HTMLParser

from crawler.urls import normalize_url, registrable_domain

_NON_CONTENT_TAGS = ["script", "style", "noscript", "template", "svg"]
_FOOTER_LINKS = "footer a[href], #footer a[href], .footer a[href], [class*='footer'] a[href]"


class Anchor(NamedTuple):
    href: str
    text: str
    in_footer: bool


class ParsedPage:
    def __init__(self, html: str, url: str):
        self.url = url
        self.domain = registrable_domain(url)
        self.is_homepage = urlsplit(url).path in {"", "/"} and not urlsplit(url).query
        tree = HTMLParser(html)
        self.tree = tree

        # These need the raw tree, so read them before stripping scripts.
        self.jsonld_blocks = [
            node.text(deep=True) for node in tree.css("script[type='application/ld+json' i]")
        ]
        self.base_url = self._find_base_url()
        footer_hrefs = {node.attributes.get("href") for node in tree.css(_FOOTER_LINKS)}
        self.anchors = [
            Anchor(href, node.text(strip=True), href in footer_hrefs)
            for node in tree.css("a[href]")
            if (href := (node.attributes.get("href") or "").strip())
        ]

        title = tree.css_first("title")
        self.title = title.text(strip=True) if title else ""
        self.metas = self._read_metas()

        tree.strip_tags(_NON_CONTENT_TAGS)
        body = tree.body or tree.root
        self.lines = [line for line in (body.text(separator="\n", strip=True) if body else "").splitlines() if line.strip()]
        self.flat_text = body.text(separator=" ", strip=True) if body else ""

    def _find_base_url(self) -> str:
        base = self.tree.css_first("base[href]")
        if base:
            resolved = normalize_url(base.attributes.get("href") or "", base=self.url)
            if resolved:
                return resolved
        return self.url

    def _read_metas(self) -> dict[str, str]:
        metas: dict[str, str] = {}
        for node in self.tree.css("meta[content]"):
            key = node.attributes.get("property") or node.attributes.get("name")
            content = node.attributes.get("content")
            if key and content:
                metas.setdefault(key.lower(), content.strip())
        return metas
