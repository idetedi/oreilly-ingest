import re
from urllib.parse import quote

import config
from core.types import BookInfo

from .base import Plugin

_COVER_WIDTH_RE = re.compile(r'/\d+w/?$')
_HIGH_RES_COVER_WIDTH = '1200w'


def _upgrade_cover_url(url: str) -> str:
    """Upgrade an O'Reilly cover URL to a high-resolution variant.

    O'Reilly serves covers at /library/cover/{isbn}/ and /covers/urn:orm:book:{id}/,
    optionally with a /{N}w/ width segment. The base URL (no width) returns a
    low-res thumbnail (~160x184). Appending /1200w/ returns a print-quality image.
    """
    if "/library/cover/" not in url and "/covers/urn:orm:book:" not in url:
        return url
    # Strip any existing width segment from the end, then append high-res width.
    stripped = _COVER_WIDTH_RE.sub("", url, count=1).rstrip("/")
    return f"{stripped}/{_HIGH_RES_COVER_WIDTH}/"


class BookPlugin(Plugin):
    def fetch(self, book_id: str) -> BookInfo:
        search_data = self._fetch_search(book_id)
        epub_data = self._fetch_epub(book_id)

        cover_url = search_data.get("cover_url")
        if cover_url:
            cover_url = _upgrade_cover_url(cover_url)

        return {
            "id": book_id,
            "ourn": epub_data.get("ourn", ""),
            "title": epub_data.get("title") or "Unknown",
            "authors": search_data.get("authors", []),
            "publishers": search_data.get("publishers", []),
            "description": (epub_data.get("descriptions") or {}).get("text/html", ""),
            "cover_url": cover_url,
            "isbn": epub_data.get("isbn", ""),
            "language": epub_data.get("language", "en"),
            "publication_date": epub_data.get("publication_date", ""),
            "virtual_pages": epub_data.get("virtual_pages"),
            "chapters_url": epub_data.get("chapters"),
            "toc_url": epub_data.get("table_of_contents"),
            "spine_url": epub_data.get("spine"),
            "files_url": epub_data.get("files"),
        }

    def _fetch_search(self, book_id: str) -> dict:
        url = f"{config.API_V2}/search/?query={quote(book_id)}&limit=1"
        data = self.http.get_json(url)
        results = data.get("results", [])
        if not results:
            return {}
        return results[0]

    def _fetch_epub(self, book_id: str) -> dict:
        url = f"{config.API_V2}/epubs/urn:orm:book:{book_id}/"
        return self.http.get_json(url)

    def search(self, query: str, limit: int = 10) -> list[dict]:
        url = f"{config.API_V2}/search/?query={quote(query)}&limit={limit}"
        data = self.http.get_json(url)
        results = []
        for item in data.get("results", []):
            if item.get("content_format") != "book":
                continue
            results.append({
                "id": item.get("archive_id"),
                "title": item.get("title"),
                "authors": item.get("authors", []),
                "cover_url": item.get("cover_url"),
                "publishers": item.get("publishers", []),
            })
        return results
