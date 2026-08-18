"""Web arama ve sayfa içeriği çekme.

Serper.dev üzerinden Google araması yapar, ardından organik sonuç
sayfalarından düz metin çıkarır.
"""

from __future__ import annotations

import logging
import re
import time
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urlparse

import requests
from pydantic import BaseModel, Field, HttpUrl

from config import Settings

logger = logging.getLogger(__name__)

SERPER_SEARCH_URL = "https://google.serper.dev/search"
DEFAULT_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "tr-TR,tr;q=0.9,en;q=0.8",
}
SKIP_EXTENSIONS = (
    ".pdf",
    ".doc",
    ".docx",
    ".xls",
    ".xlsx",
    ".ppt",
    ".pptx",
    ".zip",
    ".rar",
    ".jpg",
    ".jpeg",
    ".png",
    ".gif",
    ".webp",
    ".mp4",
    ".mp3",
)
SKIP_TAGS = frozenset({"script", "style", "noscript", "svg", "iframe", "canvas"})


class ScraperError(RuntimeError):
    """Arama veya içerik çekme hatası."""


class SearchResult(BaseModel):
    """Tek bir organik arama sonucu."""

    rank: int = Field(ge=1)
    title: str
    url: str
    snippet: str = ""
    source: str = ""
    page_text: str = ""


class SearchBundle(BaseModel):
    """Bir arama oturumunun toplanmış çıktısı."""

    query: str
    results: list[SearchResult] = Field(default_factory=list)
    knowledge_graph: dict[str, Any] = Field(default_factory=dict)
    related_queries: list[str] = Field(default_factory=list)


class _HTMLTextExtractor(HTMLParser):
    """HTML içeriğinden okunabilir düz metin üretir."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._skip_depth = 0
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in SKIP_TAGS:
            self._skip_depth += 1

    def handle_endtag(self, tag: str) -> None:
        if tag in SKIP_TAGS and self._skip_depth > 0:
            self._skip_depth -= 1

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        text = " ".join(data.split())
        if text:
            self.parts.append(text)

    def get_text(self) -> str:
        return " ".join(self.parts)


def _hostname(url: str) -> str:
    try:
        return urlparse(url).hostname or ""
    except ValueError:
        return ""


def _is_fetchable(url: str) -> bool:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        return False
    path = parsed.path.lower()
    return not any(path.endswith(ext) for ext in SKIP_EXTENSIONS)


def html_to_text(html: str, max_chars: int) -> str:
    extractor = _HTMLTextExtractor()
    try:
        extractor.feed(html)
        extractor.close()
    except Exception:
        logger.debug("HTML ayrıştırma başarısız, regex yedeklenecek.", exc_info=True)
        stripped = re.sub(r"(?is)<(script|style|noscript).*?>.*?</\1>", " ", html)
        stripped = re.sub(r"(?s)<[^>]+>", " ", stripped)
        text = " ".join(stripped.split())
        return text[:max_chars]

    text = extractor.get_text()
    return text[:max_chars]


class WebScraper:
    """Serper araması ve sayfa içeriği toplayıcısı."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": settings.user_agent, **DEFAULT_HEADERS})

    def search(
        self,
        query: str,
        num_results: int = 10,
        *,
        country: str | None = None,
        language: str | None = None,
        tbs: str | None = None,
    ) -> SearchBundle:
        """Serper API ile web araması yapar."""
        cleaned = query.strip()
        if not cleaned:
            raise ScraperError("Arama sorgusu boş olamaz.")

        payload: dict[str, Any] = {
            "q": cleaned,
            "num": max(1, min(num_results, 20)),
            "gl": (country or self.settings.search_country).lower(),
            "hl": (language or self.settings.search_language).lower(),
        }
        if tbs:
            payload["tbs"] = tbs

        headers = {
            "X-API-KEY": self.settings.serper_api_key,
            "Content-Type": "application/json",
        }

        try:
            response = self.session.post(
                SERPER_SEARCH_URL,
                json=payload,
                headers=headers,
                timeout=self.settings.request_timeout,
            )
            response.raise_for_status()
            data = response.json()
        except requests.HTTPError as exc:
            status = exc.response.status_code if exc.response is not None else "?"
            body = ""
            if exc.response is not None:
                body = exc.response.text[:400]
            raise ScraperError(
                f"Serper araması başarısız (HTTP {status}). "
                "SERPER_API_KEY değerini kontrol edin. "
                f"Yanıt: {body}"
            ) from exc
        except requests.RequestException as exc:
            raise ScraperError(f"Serper ağına bağlanılamadı: {exc}") from exc
        except ValueError as exc:
            raise ScraperError(f"Serper yanıtı JSON değil: {exc}") from exc

        results: list[SearchResult] = []
        for index, item in enumerate(data.get("organic") or [], start=1):
            url = str(item.get("link") or "").strip()
            if not url:
                continue
            results.append(
                SearchResult(
                    rank=index,
                    title=str(item.get("title") or "").strip() or url,
                    url=url,
                    snippet=str(item.get("snippet") or "").strip(),
                    source=_hostname(url),
                )
            )

        related = [
            str(item.get("query") or item).strip()
            for item in (data.get("relatedSearches") or [])
            if str(item.get("query") or item).strip()
        ]

        bundle = SearchBundle(
            query=cleaned,
            results=results,
            knowledge_graph=data.get("knowledgeGraph") or {},
            related_queries=related,
        )
        logger.info("Arama tamamlandı: %s (%s sonuç)", cleaned, len(results))
        return bundle

    def fetch_page(self, url: str) -> str:
        """Tek bir URL'den düz metin çeker. Başarısız olursa boş string döner."""
        if not _is_fetchable(url):
            logger.debug("Atlanan URL (desteklenmeyen şema/uzantı): %s", url)
            return ""

        try:
            HttpUrl(url)
        except Exception:
            logger.warning("Geçersiz URL atlandı: %s", url)
            return ""

        try:
            response = self.session.get(
                url,
                timeout=self.settings.request_timeout,
                allow_redirects=True,
            )
            response.raise_for_status()
        except requests.RequestException as exc:
            logger.warning("Sayfa çekilemedi (%s): %s", url, exc)
            return ""

        content_type = (response.headers.get("Content-Type") or "").lower()
        if "text/html" not in content_type and "application/xhtml" not in content_type:
            if "text/plain" in content_type:
                return response.text[: self.settings.max_page_chars]
            logger.debug("HTML olmayan içerik atlandı (%s): %s", content_type, url)
            return ""

        encoding = response.encoding or "utf-8"
        try:
            html = response.content.decode(encoding, errors="replace")
        except LookupError:
            html = response.content.decode("utf-8", errors="replace")

        return html_to_text(html, self.settings.max_page_chars)

    def enrich(
        self,
        bundle: SearchBundle,
        *,
        fetch_pages: bool = True,
        delay_seconds: float = 0.4,
    ) -> SearchBundle:
        """Organik sonuçlara sayfa metni ekler."""
        if not fetch_pages:
            return bundle

        enriched: list[SearchResult] = []
        for index, result in enumerate(bundle.results):
            page_text = self.fetch_page(result.url)
            enriched.append(result.model_copy(update={"page_text": page_text}))
            if index < len(bundle.results) - 1 and delay_seconds > 0:
                time.sleep(delay_seconds)

        return bundle.model_copy(update={"results": enriched})

    def collect(
        self,
        query: str,
        num_results: int = 10,
        *,
        fetch_pages: bool = True,
        country: str | None = None,
        language: str | None = None,
        tbs: str | None = None,
    ) -> SearchBundle:
        """Arama + isteğe bağlı sayfa zenginleştirme."""
        bundle = self.search(
            query,
            num_results,
            country=country,
            language=language,
            tbs=tbs,
        )
        return self.enrich(bundle, fetch_pages=fetch_pages)
