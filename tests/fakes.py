"""Offline stand-ins for the O'Reilly API used by the tests."""

import threading

import config
from core.kernel import Kernel, create_default_kernel

BOOK_ID = "9781234567890"
FILES = f"{config.API_V2}/epubs/urn:orm:book:{BOOK_ID}/files"

CHAPTERS = [
    ("ch01.html", "Chapter 1: Überblick", "<p>First chapter — café</p><img src='assets/fig1.png'/>"),
    ("ch02.html", "Chapter 2", "<p>Second chapter</p>"),
    ("ch03.html", "Chapter 3", "<p>Third chapter</p><img src='assets/fig2.png?v=2'/>"),
]


def book_routes() -> dict[str, object]:
    """URL -> JSON/text/bytes payload for a small three-chapter book."""
    results = []
    for name, title, _ in CHAPTERS:
        results.append({
            "ourn": f"urn:{name}",
            "title": title,
            "reference_id": f"{BOOK_ID}-/{name}",
            "content_url": f"{FILES}/{name}",
            "related_assets": {
                "images": [f"{FILES}/assets/{img}" for img in ("fig1.png", "fig2.png") if img[3] == name[3]],
                "stylesheets": [f"{FILES}/style.css"],
            },
            "virtual_pages": 3,
            "minutes_required": 1.0,
        })
    routes: dict[str, object] = {
        f"{config.API_V2}/search/?query={BOOK_ID}&limit=1": {
            "results": [{"authors": ["Ana Pérez"], "publishers": ["O'Reilly"], "cover_url": None}]
        },
        f"{config.API_V2}/epubs/urn:orm:book:{BOOK_ID}/": {
            "ourn": f"urn:orm:book:{BOOK_ID}", "title": "Test Book: Ñandú", "isbn": BOOK_ID, "language": "en",
        },
        f"{config.API_V2}/epub-chapters/?epub_identifier=urn:orm:book:{BOOK_ID}": {"results": results, "next": None},
        f"{config.API_V2}/epubs/urn:orm:book:{BOOK_ID}/table-of-contents/": [
            {"reference_id": f"{BOOK_ID}-/{name}", "title": title, "children": []} for name, title, _ in CHAPTERS
        ],
        f"{FILES}/style.css": "body { color: black; }",
        f"{FILES}/assets/fig1.png": b"PNG1",
        f"{FILES}/assets/fig2.png": b"PNG2",
    }
    for name, _, body in CHAPTERS:
        routes[f"{FILES}/{name}"] = f"<html><body><div id='sbo-rt-content'>{body}</div></body></html>"
    return routes


class FakeHttp:
    """Implements the HttpClient methods plugins use, served from a dict."""

    def __init__(self, routes: dict[str, object] | None = None):
        self.routes = routes if routes is not None else book_routes()
        self.calls: list[str] = []
        self._lock = threading.Lock()

    def _lookup(self, url: str):
        if not url.startswith("http"):
            url = config.BASE_URL + url
        with self._lock:
            self.calls.append(url)
        if url not in self.routes:
            raise RuntimeError(f"HTTP 404 fetching {url}")
        return self.routes[url]

    def get_json(self, url, **kwargs):
        return self._lookup(url)

    def get_text(self, url, **kwargs):
        return self._lookup(url)

    def get_bytes(self, url, **kwargs):
        return self._lookup(url)

    def get_jwt_status(self):
        return {"valid": True, "reason": None}

    def reload_cookies(self):
        pass


def make_kernel(http: FakeHttp | None = None) -> Kernel:
    kernel = create_default_kernel()
    kernel.http = http or FakeHttp()
    return kernel
