"""Download orchestration plugin."""

import json
import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import config
from core.errors import DownloadCancelled
from plugins.base import Plugin
from plugins.chunking import ChunkConfig

logger = logging.getLogger(__name__)


@dataclass
class DownloadProgress:
    """Progress state for download operations."""

    status: str
    percentage: int = 0
    message: str = ""
    eta_seconds: int | None = None
    current_chapter: int = 0
    total_chapters: int = 0
    chapter_title: str = ""
    book_id: str = ""


ProgressCallback = Callable[[DownloadProgress], None]


@dataclass
class DownloadResult:
    """Result of a completed download."""

    book_id: str
    title: str
    output_dir: Path
    files: dict = field(default_factory=dict)  # {"epub": Path, "markdown": Path, ...}
    chapters_count: int = 0


@dataclass
class DownloadContext:
    """Mutable state shared by the phases of a single download."""

    book_id: str
    output_dir: Path
    formats: list[str]
    selected_chapters: list[int] | None = None
    skip_images: bool = False
    chunk_config: ChunkConfig | None = None
    progress_callback: ProgressCallback | None = None
    cancel_check: Callable[[], bool] | None = None
    resume: bool = True

    book_info: dict = field(default_factory=dict)
    toc: list[dict] = field(default_factory=list)
    chapters: list[dict] = field(default_factory=list)
    book_dir: Path | None = None
    css_urls: set[str] = field(default_factory=set)
    image_urls: set[str] = field(default_factory=set)
    css_list: list[str] = field(default_factory=list)
    # (filename, title, processed_html) per chapter, in reading order
    chapters_data: list[tuple[str, str, str]] = field(default_factory=list)

    @property
    def oebps(self) -> Path:
        return self.book_dir / "OEBPS"

    def report(self, status: str, percentage: int = 0, message: str = "", **extra):
        if self.progress_callback:
            self.progress_callback(
                DownloadProgress(
                    status=status,
                    percentage=percentage,
                    message=message,
                    book_id=self.book_id,
                    **extra,
                )
            )

    def is_cancelled(self) -> bool:
        return bool(self.cancel_check and self.cancel_check())

    def raise_if_cancelled(self):
        # Partial files are kept on purpose: the book folder is shared by all
        # downloads of this book, and the partial chapters allow resuming.
        if self.is_cancelled():
            raise DownloadCancelled("Download cancelled by user")


class DownloaderPlugin(Plugin):
    """Orchestrates the complete book download workflow."""

    # Format vocabulary - discoverable by any client
    SUPPORTED_FORMATS = frozenset([
        "epub",
        "markdown",
        "markdown-chapters",
        "pdf",
        "pdf-chapters",
        "plaintext",
        "plaintext-chapters",
        "json",
        "jsonl",
        "toon",
        "chunks",
    ])

    # Aliases for user convenience (e.g., CLI shorthand)
    FORMAT_ALIASES = {
        "md": "markdown",
        "txt": "plaintext",
    }

    # Formats that only support entire book (no chapter selection)
    BOOK_ONLY_FORMATS = frozenset(["epub", "chunks"])

    # Output generators, run in this order: (formats that trigger it, method)
    _GENERATORS = (
        (frozenset({"epub"}), "_generate_epub"),
        (frozenset({"markdown", "markdown-chapters"}), "_generate_markdown"),
        (frozenset({"pdf", "pdf-chapters"}), "_generate_pdf"),
        (frozenset({"plaintext", "plaintext-chapters"}), "_generate_plaintext"),
        (frozenset({"json"}), "_generate_json"),
        (frozenset({"toon"}), "_generate_toon"),
        (frozenset({"chunks"}), "_generate_chunks"),
    )

    @classmethod
    def parse_formats(cls, format_input: str | list[str]) -> list[str]:
        """Parse format specification into canonical format names."""
        if isinstance(format_input, list):
            raw_formats = [str(f).strip().lower() for f in format_input if str(f).strip()]
        else:
            if format_input.strip().lower() == "all":
                return ["epub", "markdown", "pdf", "plaintext", "json", "toon", "chunks"]
            raw_formats = [f.strip().lower() for f in format_input.split(",") if f.strip()]

        formats = []
        seen = set()

        for fmt in raw_formats:
            canonical = cls.FORMAT_ALIASES.get(fmt, fmt)

            # jsonl is written alongside json, so it implies json
            if canonical == "jsonl":
                for implied in ("json", "jsonl"):
                    if implied not in seen:
                        formats.append(implied)
                        seen.add(implied)
                continue

            # Skip invalid or duplicate
            if canonical not in cls.SUPPORTED_FORMATS or canonical in seen:
                continue

            formats.append(canonical)
            seen.add(canonical)

        return formats if formats else ["epub"]

    @classmethod
    def get_format_help(cls) -> dict[str, str]:
        """Return format descriptions for CLI help or UI display."""
        return {
            "epub": "Standard EPUB format (default)",
            "markdown": "Markdown files (alias: md)",
            "markdown-chapters": "Separate Markdown file per chapter",
            "pdf": "Single PDF file",
            "pdf-chapters": "Separate PDF per chapter",
            "plaintext": "Plain text (alias: txt)",
            "plaintext-chapters": "Separate text file per chapter",
            "json": "Structured JSON export",
            "jsonl": "JSON Lines format (includes json)",
            "toon": "Token-Oriented Object Notation (token-efficient JSON for LLMs)",
            "chunks": "Chunked content for LLM processing",
        }

    @classmethod
    def supports_chapter_selection(cls, fmt: str) -> bool:
        """Check if a format supports chapter selection."""
        canonical = cls.FORMAT_ALIASES.get(fmt, fmt)
        return canonical not in cls.BOOK_ONLY_FORMATS

    @classmethod
    def get_formats_info(cls) -> dict:
        """Return complete format information for discovery endpoints."""
        return {
            "formats": sorted(cls.SUPPORTED_FORMATS),
            "aliases": cls.FORMAT_ALIASES,
            "book_only": sorted(cls.BOOK_ONLY_FORMATS),
            "descriptions": cls.get_format_help(),
        }

    def download(
        self,
        book_id: str,
        output_dir: Path,
        formats: list[str] | None = None,
        selected_chapters: list[int] | None = None,
        skip_images: bool = False,
        chunk_config: ChunkConfig | None = None,
        progress_callback: ProgressCallback | None = None,
        cancel_check: Callable[[], bool] | None = None,
        resume: bool = True,
    ) -> DownloadResult:
        """Orchestrate full download pipeline for a book.

        With resume=True, chapters already written to the book folder by an
        earlier run (recorded in .download_state.json) are not fetched again.
        """
        ctx = DownloadContext(
            book_id=book_id,
            output_dir=Path(output_dir),
            formats=formats or ["epub"],
            selected_chapters=selected_chapters,
            skip_images=skip_images,
            chunk_config=chunk_config,
            progress_callback=progress_callback,
            cancel_check=cancel_check,
            resume=resume,
        )

        ctx.report("starting", 0)
        self._fetch_metadata(ctx)
        self._prepare_book_dir(ctx)
        self._process_chapters(ctx)
        self._download_assets(ctx)
        ctx.raise_if_cancelled()

        result = DownloadResult(
            book_id=book_id,
            title=ctx.book_info.get("title", ""),
            output_dir=ctx.book_dir,
            chapters_count=len(ctx.chapters_data),
        )
        self._generate_outputs(ctx, result)

        ctx.report("completed", 100)
        return result

    # ------------------------------------------------------------------
    # Phases
    # ------------------------------------------------------------------

    def _fetch_metadata(self, ctx: DownloadContext):
        """Fetch book metadata, chapter list and TOC; apply chapter selection.

        We intentionally do NOT hard-fail here on the local JWT expiry check.
        O'Reilly serves content for a grace period past the token's `exp`, and
        the local clock check can lag the real session (e.g. cookies read from
        the browser's on-disk store trail the live in-memory token). Let the
        actual request be the source of truth — it raises a descriptive error
        (expired vs. Akamai bot-block) if the session really is rejected.
        """
        chapters_plugin = self.kernel["chapters"]

        ctx.report("fetching_metadata", 5)
        ctx.book_info = self.kernel["book"].fetch(ctx.book_id)

        ctx.report("fetching_chapters", 10)
        all_chapters = chapters_plugin.fetch_list(ctx.book_id)
        ctx.toc = chapters_plugin.fetch_toc(ctx.book_id)
        all_chapters = chapters_plugin.reorder_by_toc(all_chapters, ctx.toc)

        if ctx.selected_chapters is not None:
            selected = set(ctx.selected_chapters)
            ctx.chapters = [ch for i, ch in enumerate(all_chapters) if i in selected]
        else:
            ctx.chapters = all_chapters

    def _prepare_book_dir(self, ctx: DownloadContext):
        """Create the output folder and download the cover."""
        output_plugin = self.kernel["output"]
        ctx.book_dir = output_plugin.create_book_dir(
            output_dir=ctx.output_dir,
            book_id=ctx.book_id,
            title=ctx.book_info.get("title", ""),
            authors=ctx.book_info.get("authors"),
        )

        cover_url = ctx.book_info.get("cover_url")
        if not ctx.skip_images and cover_url:
            ctx.report("downloading_cover", 12)
            images_dir = output_plugin.get_images_dir(ctx.book_dir)
            images_dir.mkdir(parents=True, exist_ok=True)
            self.kernel["assets"].download_image(cover_url, images_dir / "cover.jpg")

    def _process_chapters(self, ctx: DownloadContext):
        """Fetch, clean and write every chapter as XHTML (15%-80% of progress).

        Chapter HTML is fetched in parallel (the HTTP client's rate limiter
        still spaces the requests) but processed and written strictly in order.
        Chapters already written by an earlier (interrupted) run are reused
        instead of fetched again, unless resume is disabled.
        """
        chapters_plugin = self.kernel["chapters"]
        total = len(ctx.chapters)
        state = self._load_state(ctx)
        done = state["chapters"]
        reusable = [self._can_reuse(ctx, ch, done) for ch in ctx.chapters]
        if any(reusable):
            logger.info("Resuming %s: reusing %d/%d chapters", ctx.book_id, sum(reusable), total)
        chapter_times: list[float] = []
        started = time.time()

        with ThreadPoolExecutor(max_workers=config.DOWNLOAD_WORKERS) as pool:
            pending = [
                None if reuse else pool.submit(chapters_plugin.fetch_content, ch["content_url"])
                for ch, reuse in zip(ctx.chapters, reusable, strict=True)
            ]
            try:
                for i, ch in enumerate(ctx.chapters):
                    ctx.raise_if_cancelled()
                    pct = 15 + int((i / total) * 65) if total else 15
                    position = {
                        "current_chapter": i + 1,
                        "total_chapters": total,
                        "chapter_title": ch.get("title", ""),
                    }
                    ctx.report("processing_chapters", pct, **position)

                    if reusable[i]:
                        self._reuse_chapter(ctx, ch, done[ch["filename"]])
                        continue

                    images = self._process_chapter(ctx, ch, pending[i].result())
                    done[ch["filename"]] = {"images": images}
                    self._save_state(ctx, state)

                    # ETA from a rolling average of the last fetched chapters
                    chapter_times.append(time.time() - started)
                    started = time.time()
                    recent = chapter_times[-5:]
                    remaining = sum(1 for reuse in reusable[i + 1 :] if not reuse)
                    eta = int(sum(recent) / len(recent) * remaining)
                    ctx.report("processing_chapters", pct, eta_seconds=eta, **position)
            except BaseException:
                # Don't keep fetching the remaining chapters after a failure/cancel.
                pool.shutdown(wait=False, cancel_futures=True)
                raise

    # -- resume state ----------------------------------------------------

    _STATE_FILE = ".download_state.json"
    _STATE_VERSION = 1

    def _load_state(self, ctx: DownloadContext) -> dict:
        """Load the per-book resume state, or start a fresh one.

        The state is only trusted when it was written for the same book and
        the same skip_images setting (which changes the chapter HTML).
        """
        fresh = {
            "version": self._STATE_VERSION,
            "book_id": ctx.book_id,
            "skip_images": ctx.skip_images,
            "chapters": {},
        }
        if not ctx.resume:
            return fresh
        try:
            state = json.loads((ctx.book_dir / self._STATE_FILE).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return fresh
        if (
            not isinstance(state, dict)
            or state.get("version") != self._STATE_VERSION
            or state.get("book_id") != ctx.book_id
            or state.get("skip_images") != ctx.skip_images
            or not isinstance(state.get("chapters"), dict)
        ):
            return fresh
        return state

    def _save_state(self, ctx: DownloadContext, state: dict):
        path = ctx.book_dir / self._STATE_FILE
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(state), encoding="utf-8")
        os.replace(tmp, path)  # atomic: an interrupted write never corrupts it

    def _can_reuse(self, ctx: DownloadContext, ch: dict, done: dict) -> bool:
        xhtml = ctx.oebps / ch["filename"].replace(".html", ".xhtml")
        return ch["filename"] in done and xhtml.exists()

    def _reuse_chapter(self, ctx: DownloadContext, ch: dict, entry: dict):
        """Register a chapter written by an earlier run without refetching it."""
        xhtml = ctx.oebps / ch["filename"].replace(".html", ".xhtml")
        processed = self.kernel["html_processor"].extract_body(xhtml)
        ctx.css_urls.update(ch["stylesheets"])
        ctx.image_urls.update(ch["images"])
        ctx.image_urls.update(entry.get("images", []))
        ctx.chapters_data.append((ch["filename"], ch["title"], processed))

    # --------------------------------------------------------------------

    def _process_chapter(self, ctx: DownloadContext, ch: dict, raw_html: str) -> list[str]:
        """Process and write one chapter; returns the image URLs it references."""
        html_processor = self.kernel["html_processor"]

        # Relative path prefix based on the chapter's depth inside OEBPS
        filename = ch["filename"].replace(".html", ".xhtml")
        path_prefix = "../" * filename.count("/")

        processed, images = html_processor.process(
            raw_html, ctx.book_id, skip_images=ctx.skip_images, path_prefix=path_prefix
        )
        images = [url for url in images if url.startswith(("http", "/"))]

        ctx.css_urls.update(ch["stylesheets"])
        ctx.image_urls.update(ch["images"])
        ctx.image_urls.update(images)

        css_refs = [f"{path_prefix}Styles/Style{j:02d}.css" for j in range(len(ctx.css_urls))]
        xhtml = html_processor.wrap_xhtml(processed, css_refs, ch["title"])

        file_path = ctx.oebps / filename
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text(xhtml, encoding="utf-8")

        ctx.chapters_data.append((ch["filename"], ch["title"], processed))
        return images

    def _download_assets(self, ctx: DownloadContext):
        """Download CSS, CSS-referenced assets and images (80%-90%)."""
        assets_plugin = self.kernel["assets"]
        html_processor = self.kernel["html_processor"]
        ctx.report("downloading_assets", 80)

        image_list = [f"{config.BASE_URL}{url}" if url.startswith("/") else url for url in ctx.image_urls]
        # Sorted so StyleNN.css maps to the same URL on every run (resume reuses
        # the files already on disk).
        ctx.css_list = sorted(ctx.css_urls)
        css_count = len(ctx.css_list)
        total_assets = css_count + len(image_list)

        def progress(offset: int, label: str, count: int):
            width = len(str(count))

            def callback(completed: int, total: int):
                if total_assets > 0:
                    pct = 80 + int(((offset + completed) / total_assets) * 10)
                    ctx.report(
                        "downloading_assets",
                        pct,
                        f"{pct:2d}% - Downloading {label} ({completed:>{width}}/{count})",
                    )

            return callback

        assets_plugin.download_all_css(
            ctx.css_list,
            ctx.oebps,
            progress_callback=progress(0, "CSS", css_count),
            cancel_check=ctx.cancel_check,
        )

        if ctx.skip_images:
            return

        # Assets referenced in CSS (e.g. url() images in ::after), then inline
        # CSS content:url() images as <img> tags (Apple Books compat)
        assets_plugin.download_css_assets(ctx.css_list, ctx.oebps)
        html_processor.inline_css_content_images(ctx.oebps)

        assets_plugin.download_all_images(
            image_list,
            ctx.oebps,
            progress_callback=progress(css_count, "images", len(image_list)),
            cancel_check=ctx.cancel_check,
        )

    def _generate_outputs(self, ctx: DownloadContext, result: DownloadResult):
        """Run every generator whose trigger formats were requested (90%-100%)."""
        requested = set(ctx.formats)
        for triggers, method_name in self._GENERATORS:
            if triggers & requested:
                ctx.raise_if_cancelled()
                getattr(self, method_name)(ctx, result)

        # The EPUB build tree (OEBPS/) is shared with other generators (PDF),
        # so it is only removed once every requested format has been written.
        if "epub" in requested:
            self.kernel["epub"].cleanup_build_artifacts(ctx.book_dir)

    # ------------------------------------------------------------------
    # Output generators
    # ------------------------------------------------------------------

    def _generate_epub(self, ctx: DownloadContext, result: DownloadResult):
        ctx.report("generating_epub", 90)
        epub_path = self.kernel["epub"].generate(
            book_info=ctx.book_info,
            chapters=ctx.chapters,
            toc=ctx.toc,
            output_dir=ctx.book_dir,
            css_files=ctx.css_list,
            cover_image="cover.jpg",
            cleanup=False,
        )
        result.files["epub"] = str(epub_path)

    def _generate_markdown(self, ctx: DownloadContext, result: DownloadResult):
        ctx.report("generating_markdown", 92)
        self.kernel["markdown"].generate_book(ctx.book_info, ctx.chapters_data, ctx.book_dir)
        result.files["markdown"] = str(ctx.book_dir / "Markdown")

    def _generate_pdf(self, ctx: DownloadContext, result: DownloadResult):
        pdf_plugin = self.kernel["pdf"]
        if "pdf-chapters" in ctx.formats:
            ctx.report("generating_pdf_chapters", 95)
            pdf_paths = pdf_plugin.generate_chapters(
                book_info=ctx.book_info,
                chapters=ctx.chapters,
                output_dir=ctx.book_dir,
                css_files=ctx.css_list,
            )
            result.files["pdf"] = [str(p) for p in pdf_paths]
        else:
            ctx.report("generating_pdf", 95)
            pdf_path = pdf_plugin.generate(
                book_info=ctx.book_info,
                chapters=ctx.chapters,
                toc=ctx.toc,
                output_dir=ctx.book_dir,
                css_files=ctx.css_list,
                cover_image="cover.jpg",
            )
            result.files["pdf"] = str(pdf_path)

    def _generate_plaintext(self, ctx: DownloadContext, result: DownloadResult):
        ctx.report("generating_plaintext", 96)
        txt_path = self.kernel["plaintext"].generate(
            book_dir=ctx.book_dir,
            book_metadata=ctx.book_info,
            chapters_data=ctx.chapters_data,
            single_file="plaintext-chapters" not in ctx.formats,
        )
        result.files["plaintext"] = str(txt_path)

    def _generate_json(self, ctx: DownloadContext, result: DownloadResult):
        ctx.report("generating_json", 97)
        json_path = self.kernel["json_export"].generate(
            book_dir=ctx.book_dir,
            book_metadata=ctx.book_info,
            chapters_data=ctx.chapters_data,
            include_jsonl="jsonl" in ctx.formats,
        )
        result.files["json"] = str(json_path)

    def _generate_toon(self, ctx: DownloadContext, result: DownloadResult):
        ctx.report("generating_toon", 97)
        toon_path = self.kernel["toon_export"].generate(
            book_dir=ctx.book_dir,
            book_metadata=ctx.book_info,
            chapters_data=ctx.chapters_data,
        )
        result.files["toon"] = str(toon_path)

    def _generate_chunks(self, ctx: DownloadContext, result: DownloadResult):
        ctx.report("generating_chunks", 98)
        chunks_path = self.kernel["chunking"].generate(
            book_dir=ctx.book_dir,
            book_metadata=ctx.book_info,
            chapters_data=ctx.chapters_data,
            config=ctx.chunk_config,
        )
        result.files["chunks"] = str(chunks_path)
