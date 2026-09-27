"""Unit tests for pure helpers and small plugin behaviours."""

import base64
import json
import time
from pathlib import Path

import pytest

import config
from core.http_client import HttpClient
from plugins.book import _upgrade_cover_url
from plugins.downloader import DownloaderPlugin
from tests.fakes import FakeHttp, make_kernel
from utils import image_filename, sanitize_filename, slugify

# -- formats ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("spec", "expected"),
    [
        ("md,txt", ["markdown", "plaintext"]),
        ("MD, Markdown", ["markdown"]),
        ("jsonl", ["json", "jsonl"]),
        ("json,jsonl", ["json", "jsonl"]),
        ("bogus", ["epub"]),
        (["PDF", "epub"], ["pdf", "epub"]),
        ("all", ["epub", "markdown", "pdf", "plaintext", "json", "toon", "chunks"]),
    ],
)
def test_parse_formats(spec, expected):
    assert DownloaderPlugin.parse_formats(spec) == expected


def test_book_only_formats():
    assert not DownloaderPlugin.supports_chapter_selection("epub")
    assert DownloaderPlugin.supports_chapter_selection("md")


# -- utils ---------------------------------------------------------------------


def test_slugify():
    assert slugify("Clean Code: A Handbook (2nd Edition)") == "clean-code-a-handbook-2nd-edition"
    assert len(slugify("x" * 300)) == 100


def test_sanitize_filename():
    assert sanitize_filename('a/b\\c:d?*"<>|. ') == "a-b-c -d'-"


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://x/files/assets/fig1.png", "fig1.png"),
        ("assets/fig2.png?v=2#frag", "fig2.png"),
        ("/api/images/fig%203.jpg", "fig 3.jpg"),
        ("https://x/dir/", "image"),
    ],
)
def test_image_filename(url, expected):
    assert image_filename(url) == expected


def test_cover_url_upgrade():
    assert _upgrade_cover_url("https://x/library/cover/123/") == "https://x/library/cover/123/1200w/"
    assert _upgrade_cover_url("https://x/covers/urn:orm:book:123/400w/") == "https://x/covers/urn:orm:book:123/1200w/"
    assert _upgrade_cover_url("https://cdn/other.jpg") == "https://cdn/other.jpg"


# -- HTTP client -------------------------------------------------------------------


def _jwt(payload: dict) -> str:
    body = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    return f"header.{body}.signature"


def _client(tmp_path, cookies=None) -> HttpClient:
    path = tmp_path / "cookies.json"
    if cookies is not None:
        path.write_text(json.dumps(cookies), encoding="utf-8")
    return HttpClient(path)


def test_jwt_payload_is_base64url(tmp_path):
    # "??>" encodes to "_" / "-" characters in base64url
    token = _jwt({"exp": time.time() + 3600, "name": "??>>??"})
    assert HttpClient._decode_jwt_payload(token)["name"] == "??>>??"
    assert _client(tmp_path, {"orm-jwt": token}).get_jwt_status()["valid"] is True


def test_jwt_expired(tmp_path):
    status = _client(tmp_path, {"orm-jwt": _jwt({"exp": time.time() - 10})}).get_jwt_status()
    assert status == {"valid": False, "reason": "token_expired", "expires_at": status["expires_at"]}


def test_missing_or_invalid_cookie_file(tmp_path):
    assert _client(tmp_path).get_jwt_status() is None
    (tmp_path / "cookies.json").write_text("not json", encoding="utf-8")
    assert HttpClient(tmp_path / "cookies.json").get_jwt_status() is None


class _Response:
    def __init__(self, status, headers=None):
        self.status_code = status
        self.headers = headers or {}
        self.url = "https://example"
        self.text = ""


class _Session:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = 0
        self.cookies = _Cookies()

    def get(self, url, **kwargs):
        self.calls += 1
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class _Cookies(dict):
    def clear(self):
        super().clear()


@pytest.fixture
def fast_retries(monkeypatch):
    monkeypatch.setattr(config, "REQUEST_DELAY", 0)
    monkeypatch.setattr(config, "ASSET_REQUEST_DELAY", 0)
    monkeypatch.setattr(config, "RETRY_BACKOFF", 0.001)
    monkeypatch.setattr(config, "MAX_RETRIES", 3)
    sleeps = []
    monkeypatch.setattr("core.http_client.time.sleep", sleeps.append)
    return sleeps


def test_retries_transient_status_then_succeeds(tmp_path, fast_retries):
    client = _client(tmp_path)
    session = _Session([_Response(503), _Response(429, {"Retry-After": "7"}), _Response(200)])
    client._local.session = session
    assert client.get("/x").status_code == 200
    assert session.calls == 3
    assert 7.0 in fast_retries, "Retry-After must be honoured"


def test_retries_network_errors_and_returns_last_response(tmp_path, fast_retries):
    client = _client(tmp_path)
    client._local.session = _Session([TimeoutError("t"), _Response(502), _Response(502)])
    assert client.get("/x").status_code == 502


def test_raises_after_repeated_network_errors(tmp_path, fast_retries):
    client = _client(tmp_path)
    client._local.session = _Session([OSError("a"), OSError("b"), OSError("c")])
    with pytest.raises(OSError, match="c"):
        client.get("/x")


def test_does_not_retry_client_errors(tmp_path, fast_retries):
    client = _client(tmp_path)
    session = _Session([_Response(404)])
    client._local.session = session
    assert client.get("/x").status_code == 404
    assert session.calls == 1


# -- plugins -----------------------------------------------------------------------


def test_search_query_is_url_encoded():
    http = FakeHttp(routes={})
    kernel = make_kernel(http)
    with pytest.raises(RuntimeError):
        kernel["book"].search("c++ & rust #1")
    assert http.calls[-1] == f"{config.API_V2}/search/?query=c%2B%2B%20%26%20rust%20%231&limit=10"


def test_reorder_by_toc_keeps_front_and_back_matter():
    chapters_plugin = make_kernel()["chapters"]

    def ch(name, title):
        return {"filename": name, "title": title}

    chapters = [ch("appendix.html", "Appendix"), ch("ch02.html", "Two"), ch("cover.html", "Cover"), ch("ch01.html", "One")]
    toc = [{"reference_id": "b-/ch01.html", "children": [{"reference_id": "b-/ch02.html"}]}]
    ordered = chapters_plugin.reorder_by_toc(chapters, toc)
    assert [c["filename"] for c in ordered] == ["cover.html", "ch01.html", "ch02.html", "appendix.html"]


def test_css_assets_cannot_escape_the_book_folder(tmp_path):
    kernel = make_kernel(FakeHttp(routes={}))
    oebps = tmp_path / "book" / "OEBPS"
    styles = oebps / "Styles"
    styles.mkdir(parents=True)
    (styles / "Style00.css").write_text("a { background: url(../../../evil.png) }", encoding="utf-8")

    kernel["assets"].download_css_assets(["https://x/css/style.css"], oebps)
    assert not (tmp_path / "evil.png").exists()
    assert kernel.http.calls == [], "no request is made for paths outside the book"


def test_extract_body_round_trips_wrap_xhtml(tmp_path):
    processor = make_kernel()["html_processor"]
    path = Path(tmp_path / "c.xhtml")
    path.write_text(processor.wrap_xhtml("<p>Hola — ñ</p>", [], "T"), encoding="utf-8")
    assert processor.extract_body(path) == "<p>Hola — ñ</p>"


# -- adaptive asset rate -------------------------------------------------------------


@pytest.fixture
def adaptive(monkeypatch):
    monkeypatch.setattr(config, "ASSET_REQUEST_DELAY", 0.25)
    monkeypatch.setattr(config, "ASSET_MIN_DELAY", 0.1)
    monkeypatch.setattr(config, "ASSET_MAX_DELAY", 2.0)
    monkeypatch.setattr(config, "ASSET_SPEEDUP_EVERY", 5)


def test_asset_lane_speeds_up_to_the_floor(tmp_path, adaptive):
    client = _client(tmp_path)
    for _ in range(4):
        client._record_outcome("asset", 200)
    assert client.lane_delay("asset") == 0.25, "no change before a full streak"
    for _ in range(500):
        client._record_outcome("asset", 200)
    assert client.lane_delay("asset") == pytest.approx(0.1)


@pytest.mark.parametrize("signal", [429, 403, 503, None])
def test_asset_lane_backs_off_on_throttling(tmp_path, adaptive, signal):
    client = _client(tmp_path)
    client._record_outcome("asset", signal)
    assert client.lane_delay("asset") == pytest.approx(0.5)
    for _ in range(10):
        client._record_outcome("asset", signal)
    assert client.lane_delay("asset") == pytest.approx(2.0)


def test_throttling_resets_the_success_streak(tmp_path, adaptive):
    client = _client(tmp_path)
    for _ in range(4):
        client._record_outcome("asset", 200)
    client._record_outcome("asset", 429)
    for _ in range(4):
        client._record_outcome("asset", 200)
    assert client.lane_delay("asset") == pytest.approx(0.5)


def test_api_lane_and_not_found_are_not_adaptive(tmp_path, adaptive, monkeypatch):
    monkeypatch.setattr(config, "REQUEST_DELAY", 0.5)
    client = _client(tmp_path)
    client._record_outcome("api", 429)
    client._record_outcome("asset", 404)
    assert client.lane_delay("api") == 0.5
    assert client.lane_delay("asset") == 0.25


def test_get_feeds_the_adaptive_lane(tmp_path, fast_retries, adaptive):
    client = _client(tmp_path)
    client._local.session = _Session([_Response(429), _Response(200)])
    client.get("/img.png", lane="asset")
    assert client.lane_delay("asset") == pytest.approx(0.5)


def test_401_is_reported_as_expired_session(tmp_path, fast_retries):
    client = _client(tmp_path)
    client._local.session = _Session([_Response(401)])
    with pytest.raises(RuntimeError, match="Session expired"):
        client.get_json("/x")
