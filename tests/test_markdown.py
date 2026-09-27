import re

import pytest

from plugins.markdown import _IMAGE_LINK_RE
from tests.fakes import BOOK_ID, make_kernel


def test_image_link_pattern():
    text = '![a](./Images/x.png) ![b](../../Images/y.png "t") [c](Images/z.png) ![d](http://q/Images/w.png)'
    assert _IMAGE_LINK_RE.findall(text) == ["x.png", "y.png", "z.png"]


@pytest.mark.parametrize("formats", [["markdown"], ["markdown", "epub"]])
def test_markdown_images_resolve(tmp_path, formats):
    """Markdown links must point at files inside Markdown/, even after EPUB cleanup."""
    result = make_kernel()["downloader"].download(book_id=BOOK_ID, output_dir=tmp_path, formats=formats)
    md_dir = result.output_dir / "Markdown"

    links = []
    for md_file in md_dir.glob("*.md"):
        for target in re.findall(r"!\[[^\]]*\]\(([^)\s]+)", md_file.read_text(encoding="utf-8")):
            links.append((md_file.parent / target).resolve())

    assert links, "the fake book has images"
    assert all(path.is_file() for path in links), links
    assert {p.name for p in (md_dir / "Images").iterdir()} == {"fig1.png", "fig2.png"}
