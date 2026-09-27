"""Command-line interface: python -m cli <command> ..."""

import argparse
import logging
import signal
import sys
from pathlib import Path

import config
from core import DownloadCancelled, create_default_kernel
from plugins import ChunkConfig
from plugins.downloader import DownloaderPlugin, DownloadProgress


def parse_chapters(spec: str) -> list[int]:
    """Parse a 1-based chapter spec like "1,3,5-9" into sorted 0-based indexes."""
    indexes: set[int] = set()
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            start_s, end_s = part.split("-", 1)
            start, end = int(start_s), int(end_s)
            if start < 1 or end < start:
                raise ValueError(f"invalid chapter range: {part}")
            indexes.update(range(start - 1, end))
        else:
            number = int(part)
            if number < 1:
                raise ValueError(f"chapter numbers start at 1: {part}")
            indexes.add(number - 1)
    if not indexes:
        raise ValueError("no chapters selected")
    return sorted(indexes)


def _chapters_arg(value: str) -> list[int]:
    try:
        return parse_chapters(value)
    except ValueError as e:
        raise argparse.ArgumentTypeError(str(e)) from e


class ProgressPrinter:
    """Single-line progress bar on a TTY, plain lines otherwise."""

    WIDTH = 30

    def __init__(self, stream=sys.stderr):
        self.stream = stream
        self.tty = stream.isatty()
        self._last = None

    def __call__(self, progress: DownloadProgress):
        detail = progress.message or progress.status.replace("_", " ")
        if progress.current_chapter and progress.total_chapters:
            detail = f"chapter {progress.current_chapter}/{progress.total_chapters}"
            if progress.eta_seconds:
                detail += f", ~{progress.eta_seconds}s left"
        if self.tty:
            filled = int(self.WIDTH * progress.percentage / 100)
            bar = "#" * filled + "-" * (self.WIDTH - filled)
            self.stream.write(f"\r[{bar}] {progress.percentage:3d}% {detail[:60]:<60}")
            self.stream.flush()
        elif (progress.status, progress.current_chapter) != self._last:
            self._last = (progress.status, progress.current_chapter)
            self.stream.write(f"{progress.percentage:3d}% {detail}\n")

    def finish(self):
        if self.tty:
            self.stream.write("\n")


def cmd_download(kernel, args) -> int:
    downloader = kernel["downloader"]
    formats = DownloaderPlugin.parse_formats(args.format)

    output_plugin = kernel["output"]
    ok, message, output_dir = output_plugin.validate_dir(args.output)
    if not ok:
        print(f"error: {message}", file=sys.stderr)
        return 2

    chunk_config = None
    if "chunks" in formats:
        chunk_config = ChunkConfig(chunk_size=args.chunk_size, overlap=args.overlap, respect_boundaries=True)

    # First Ctrl+C asks the downloader to stop cleanly (files are kept so the
    # download can be resumed); a second one aborts immediately.
    cancelled = False

    def on_sigint(signum, frame):
        nonlocal cancelled
        if cancelled:
            raise KeyboardInterrupt
        cancelled = True
        print("\nCancelling... (press Ctrl+C again to abort)", file=sys.stderr)

    previous = signal.signal(signal.SIGINT, on_sigint)
    failures = 0
    try:
        for book_id in args.book_ids:
            if cancelled:
                break
            print(f"Downloading {book_id} as {', '.join(formats)}", file=sys.stderr)
            printer = ProgressPrinter()
            try:
                result = downloader.download(
                    book_id=book_id,
                    output_dir=output_dir,
                    formats=formats,
                    selected_chapters=args.chapters,
                    skip_images=args.skip_images,
                    chunk_config=chunk_config,
                    progress_callback=printer,
                    cancel_check=lambda: cancelled,
                    resume=not args.no_resume,
                )
            except DownloadCancelled:
                printer.finish()
                print("Cancelled. Run the same command again to resume.", file=sys.stderr)
                return 130
            except Exception as e:
                printer.finish()
                failures += 1
                print(f"error: {book_id}: {e}", file=sys.stderr)
                continue
            printer.finish()
            print(f"{result.title} ({result.chapters_count} chapters)")
            for fmt, path in result.files.items():
                paths = path if isinstance(path, list) else [path]
                print(f"  {fmt}: {paths[0]}" + (f" (+{len(paths) - 1} more)" if len(paths) > 1 else ""))
    finally:
        signal.signal(signal.SIGINT, previous)
    return 1 if failures else 0


def cmd_search(kernel, args) -> int:
    results = kernel["book"].search(args.query, limit=args.limit)
    if not results:
        print("No books found.")
        return 1
    for book in results:
        authors = ", ".join(book.get("authors") or []) or "Unknown author"
        print(f"{book['id']:<16} {book['title']} — {authors}")
    return 0


def cmd_formats(kernel, args) -> int:
    info = DownloaderPlugin.get_formats_info()
    for name, description in info["descriptions"].items():
        print(f"{name:<20} {description}")
    print("\nAliases: " + ", ".join(f"{k}={v}" for k, v in info["aliases"].items()) + ", all")
    return 0


def cmd_status(kernel, args) -> int:
    status = kernel["auth"].get_status()
    if status.get("valid"):
        expires = status.get("expires_at")
        print("Session valid" + (f" (token expires {expires})" if expires else ""))
        return 0
    print(f"Session invalid: {status.get('reason') or 'unknown'}")
    print(f"Update the cookies in {config.COOKIES_FILE} (see README).")
    return 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m cli", description="O'Reilly book downloader (command line)")
    parser.add_argument("-v", "--verbose", action="store_true", help="show debug logging")
    sub = parser.add_subparsers(dest="command", required=True)

    download = sub.add_parser("download", help="download one or more books")
    download.add_argument("book_ids", nargs="+", metavar="BOOK_ID", help="book id / ISBN (several run in sequence)")
    download.add_argument("-f", "--format", default="epub", help="comma-separated formats, e.g. md,epub or 'all'")
    download.add_argument("-o", "--output", default=str(config.OUTPUT_DIR), help="output directory")
    download.add_argument("--chapters", type=_chapters_arg, help="1-based chapters, e.g. 1,3,5-9")
    download.add_argument("--skip-images", action="store_true", help="do not download images")
    download.add_argument("--chunk-size", type=int, default=4000, help="tokens per chunk (chunks format)")
    download.add_argument("--overlap", type=int, default=200, help="token overlap between chunks")
    download.add_argument("--no-resume", action="store_true", help="refetch chapters already downloaded")
    download.set_defaults(func=cmd_download)

    search = sub.add_parser("search", help="search the catalogue")
    search.add_argument("query")
    search.add_argument("-n", "--limit", type=int, default=10)
    search.set_defaults(func=cmd_search)

    sub.add_parser("formats", help="list output formats").set_defaults(func=cmd_formats)
    sub.add_parser("status", help="check the saved session cookies").set_defaults(func=cmd_status)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )
    logging.getLogger("curl_cffi").setLevel(logging.WARNING)
    if hasattr(args, "output"):
        args.output = Path(args.output)
    return args.func(create_default_kernel(), args)
