import pytest

from cli.main import build_parser, cmd_download, parse_chapters
from tests.fakes import BOOK_ID, make_kernel


def test_parse_chapters_ranges_and_singles():
    assert parse_chapters("1,3,5-7") == [0, 2, 4, 5, 6]
    assert parse_chapters(" 2 , 2,1-2") == [0, 1]


@pytest.mark.parametrize("spec", ["", "0", "3-1", "a", "1-x"])
def test_parse_chapters_rejects_bad_input(spec):
    with pytest.raises(ValueError):
        parse_chapters(spec)


def test_download_command(tmp_path, capsys):
    args = build_parser().parse_args(
        ["download", BOOK_ID, "-f", "md,json", "-o", str(tmp_path), "--chapters", "1-2"]
    )
    assert cmd_download(make_kernel(), args) == 0

    out = capsys.readouterr().out
    assert "Test Book: Ñandú (2 chapters)" in out
    assert "markdown:" in out and "json:" in out
    book_dir = next(tmp_path.iterdir())
    assert sorted(p.name for p in (book_dir / "Markdown").glob("ch*.md")) == ["ch01.md", "ch02.md"]


def test_download_command_reports_failures(tmp_path, capsys):
    args = build_parser().parse_args(["download", "unknown-book", "-f", "md", "-o", str(tmp_path)])
    assert cmd_download(make_kernel(), args) == 1
    assert "error: unknown-book" in capsys.readouterr().err
