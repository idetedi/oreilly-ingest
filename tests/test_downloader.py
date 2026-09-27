import json

import pytest

from core.errors import DownloadCancelled
from tests.fakes import BOOK_ID, FakeHttp, make_kernel


def download(kernel, tmp_path, **kwargs):
    kwargs.setdefault("formats", ["markdown", "json", "jsonl", "plaintext", "toon", "chunks", "epub"])
    return kernel["downloader"].download(book_id=BOOK_ID, output_dir=tmp_path, **kwargs)


def test_full_download_generates_every_format(tmp_path):
    kernel = make_kernel()
    events = []
    result = download(kernel, tmp_path, progress_callback=events.append)

    assert result.chapters_count == 3
    assert set(result.files) == {"markdown", "json", "plaintext", "toon", "chunks", "epub"}
    book_dir = result.output_dir
    assert (book_dir / "Markdown" / "ch01.md").read_text(encoding="utf-8").startswith("# Chapter 1: Überblick")
    data = json.loads((book_dir / "Test Book - Ñandú.json").read_text(encoding="utf-8"))
    assert [c["title"] for c in data["chapters"]] == ["Chapter 1: Überblick", "Chapter 2", "Chapter 3"]
    assert events[-1].status == "completed" and events[-1].percentage == 100


def test_images_are_saved_under_the_rewritten_name(tmp_path):
    result = download(make_kernel(), tmp_path, formats=["markdown"])
    oebps = result.output_dir / "OEBPS"
    assert (oebps / "Images" / "fig2.png").read_bytes() == b"PNG2"
    assert 'src="Images/fig2.png"' in (oebps / "ch03.xhtml").read_text(encoding="utf-8")


def test_chapter_selection(tmp_path):
    result = download(make_kernel(), tmp_path, formats=["markdown"], selected_chapters=[0, 2])
    md_dir = result.output_dir / "Markdown"
    assert sorted(p.name for p in md_dir.glob("ch*.md")) == ["ch01.md", "ch03.md"]


def test_cancel_keeps_existing_files(tmp_path):
    kernel = make_kernel()
    first = download(kernel, tmp_path, formats=["markdown"])
    marker = first.output_dir / "Markdown" / "README.md"
    assert marker.exists()

    with pytest.raises(DownloadCancelled):
        download(kernel, tmp_path, formats=["json"], cancel_check=lambda: True)
    assert marker.exists(), "cancelling must not delete earlier downloads of the same book"


def test_failed_stylesheet_does_not_abort(tmp_path):
    http = FakeHttp()
    del http.routes[next(u for u in http.routes if u.endswith("style.css"))]
    result = download(make_kernel(http), tmp_path, formats=["markdown"])
    assert result.chapters_count == 3


def test_epub_keeps_build_tree_until_other_formats_are_done(tmp_path):
    """PDF runs after EPUB and reads OEBPS/, which EPUB used to delete."""
    kernel = make_kernel()
    seen = {}

    class FakePdf:
        def generate(self, output_dir, **kwargs):
            seen["chapter_exists"] = (output_dir / "OEBPS" / "ch01.xhtml").exists()
            path = output_dir / "book.pdf"
            path.write_bytes(b"%PDF")
            return path

    kernel.register("pdf", FakePdf())
    result = download(kernel, tmp_path, formats=["epub", "pdf"])

    assert seen["chapter_exists"]
    assert {"epub", "pdf"} <= set(result.files)
    assert not (result.output_dir / "OEBPS").exists(), "build tree is still cleaned up at the end"


def _interrupted_download(tmp_path):
    """Run a download that fails on chapter 3, leaving chapters 1-2 on disk."""
    http = FakeHttp()
    ch3 = next(u for u in http.routes if u.endswith("/ch03.html"))
    body = http.routes.pop(ch3)
    with pytest.raises(RuntimeError):
        download(make_kernel(http), tmp_path, formats=["markdown"])
    http.routes[ch3] = body
    http.calls.clear()
    return http


def _chapter_fetches(http):
    return sorted(u.rsplit("/", 1)[-1] for u in http.calls if u.endswith(".html"))


def test_resume_skips_chapters_already_written(tmp_path):
    http = _interrupted_download(tmp_path)
    result = download(make_kernel(http), tmp_path, formats=["markdown"])

    assert _chapter_fetches(http) == ["ch03.html"]
    assert result.chapters_count == 3
    md = (result.output_dir / "Markdown" / "ch01.md").read_text(encoding="utf-8")
    assert "First chapter — café" in md


def test_no_resume_refetches_everything(tmp_path):
    http = _interrupted_download(tmp_path)
    download(make_kernel(http), tmp_path, formats=["markdown"], resume=False)
    assert _chapter_fetches(http) == ["ch01.html", "ch02.html", "ch03.html"]


def test_resume_ignored_when_skip_images_changes(tmp_path):
    http = _interrupted_download(tmp_path)
    download(make_kernel(http), tmp_path, formats=["markdown"], skip_images=True)
    assert _chapter_fetches(http) == ["ch01.html", "ch02.html", "ch03.html"]
