import logging
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Callable

import config
from core.errors import DownloadCancelled
from utils import image_filename

from .base import Plugin

logger = logging.getLogger(__name__)

ProgressCallback = Callable[[int, int], None]
CancelCheck = Callable[[], bool]


class AssetsPlugin(Plugin):
    def download_image(self, url: str, save_path: Path) -> bool:
        if save_path.exists():
            return True

        save_path.parent.mkdir(parents=True, exist_ok=True)
        content = self.http.get_bytes(url, lane="asset")
        save_path.write_bytes(content)
        return True

    def download_css(self, url: str, save_path: Path) -> bool:
        if save_path.exists():
            return True

        save_path.parent.mkdir(parents=True, exist_ok=True)
        content = self.http.get_text(url, lane="asset")
        save_path.write_text(content, encoding='utf-8')
        return True

    def download_all_images(
        self,
        urls: list[str],
        output_dir: Path,
        progress_callback: ProgressCallback | None = None,
        cancel_check: CancelCheck | None = None,
    ) -> dict[str, Path]:
        downloaded = {}
        failed = []
        total = len(urls)

        def fetch(url: str):
            filename = image_filename(url)
            save_path = output_dir / "Images" / filename
            try:
                self.download_image(url, save_path)
                downloaded[url] = save_path
            except Exception as e:
                # A single unreachable/timed-out image must not abort the whole
                # book. Skip it, keep going, and report the count at the end.
                failed.append(url)
                logger.warning("Skipping image after retries: %s (%s)", filename, e)

        self._run_parallel(urls, fetch, progress_callback, cancel_check)
        if failed:
            logger.warning("%d/%d images could not be downloaded and were skipped", len(failed), total)
        return downloaded

    def download_all_css(
        self,
        urls: list[str],
        output_dir: Path,
        progress_callback: ProgressCallback | None = None,
        cancel_check: CancelCheck | None = None,
    ) -> dict[str, Path]:
        downloaded = {}
        failed = []

        def fetch(item: tuple[int, str]):
            i, url = item
            save_path = output_dir / "Styles" / f"Style{i:02d}.css"
            try:
                self.download_css(url, save_path)
                downloaded[url] = save_path
            except Exception as e:
                # Missing styling is cosmetic; don't lose the whole book over it.
                failed.append(url)
                logger.warning("Skipping stylesheet after retries: %s (%s)", url, e)

        self._run_parallel(list(enumerate(urls)), fetch, progress_callback, cancel_check)
        if failed:
            logger.warning("%d/%d stylesheets could not be downloaded and were skipped", len(failed), len(urls))
        return downloaded

    @staticmethod
    def _run_parallel(
        items: list,
        fn: Callable,
        progress_callback: ProgressCallback | None,
        cancel_check: CancelCheck | None = None,
    ):
        """Run fn over items with DOWNLOAD_WORKERS threads, reporting progress.

        The HTTP client's shared rate limiter still spaces the requests; the
        pool only overlaps network latency. On cancel (or any exception from
        fn) the remaining queued items are dropped and the error propagates.
        """
        total = len(items)
        completed = 0
        lock = threading.Lock()

        def run(item):
            nonlocal completed
            if cancel_check and cancel_check():
                raise DownloadCancelled("Download cancelled by user")
            fn(item)
            with lock:
                completed += 1
                done = completed
            if progress_callback:
                progress_callback(done, total)

        with ThreadPoolExecutor(max_workers=config.DOWNLOAD_WORKERS) as pool:
            futures = [pool.submit(run, item) for item in items]
            try:
                for future in futures:
                    future.result()
            except BaseException:
                pool.shutdown(wait=False, cancel_futures=True)
                raise

    def download_css_assets(self, css_urls: list[str], oebps: Path):
        """Download assets referenced by url() in CSS files."""
        styles_dir = oebps / "Styles"
        if not styles_dir.exists():
            return
        root = oebps.resolve()

        for i, css_url in enumerate(css_urls):
            css_path = styles_dir / f"Style{i:02d}.css"
            if not css_path.exists():
                continue

            css_text = css_path.read_text(encoding="utf-8")
            for match in re.finditer(r'url\(["\']?([^)"\']+)["\']?\)', css_text):
                ref = match.group(1)
                if ref.startswith("data:") or ref.startswith("http"):
                    continue

                # Resolve relative to CSS file location, download from source.
                # Never write outside the book's OEBPS folder (e.g. url(../../../x)).
                save_path = (styles_dir / ref).resolve()
                if not save_path.is_relative_to(root):
                    logger.warning("Ignoring CSS asset outside the book folder: %s", ref)
                    continue
                if save_path.exists():
                    continue

                # Build download URL from the CSS source URL
                css_base = css_url.rsplit("/", 1)[0]
                asset_url = f"{css_base}/{ref}"
                try:
                    self.download_image(asset_url, save_path)
                except Exception as e:
                    logger.debug("Could not download CSS asset %s: %s", asset_url, e)

    def get_cover_url(self, book_id: str) -> str:
        return f"{config.BASE_URL}/library/cover/{book_id}/"
