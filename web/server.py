"""Web server for O'Reilly Ingest."""

import json
import logging
import os
import re
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import config
from core import DownloadQueue, Kernel, create_default_kernel
from plugins import ChunkConfig
from plugins.downloader import DownloaderPlugin

logger = logging.getLogger(__name__)


class DownloaderHandler(SimpleHTTPRequestHandler):
    """HTTP request handler for the downloader web interface."""

    kernel: Kernel = None
    queue: DownloadQueue = None

    def __init__(self, *args, **kwargs):
        self.static_dir = Path(__file__).parent / "static"
        super().__init__(*args, directory=str(self.static_dir), **kwargs)

    @staticmethod
    def _hostname(netloc: str) -> str:
        """Return the lowercase host part of a netloc (drops port, IPv6 brackets)."""
        return (urlparse(f"//{netloc}").hostname or "").lower()

    def _check_request_allowed(self) -> bool:
        """Reject cross-site and DNS-rebinding requests to the local API.

        - The Host header must name an allowed host (localhost by default).
        - Browser requests carry an Origin header on POST; it must match Host.
          Non-browser clients (curl, scripts/refresh_cookies.py) send no Origin.
        """
        host = self.headers.get("Host", "")
        if self._hostname(host) not in config.ALLOWED_HOSTS:
            self._send_json({"error": "Host not allowed"}, 403)
            return False
        origin = self.headers.get("Origin")
        if self.command == "POST" and origin is not None:
            if urlparse(origin).netloc.lower() != host.lower():
                self._send_json({"error": "Cross-origin request rejected"}, 403)
                return False
        return True

    def do_GET(self):
        try:
            self._route_get()
        except Exception as e:
            logger.exception("Unhandled error")
            self._send_json({"error": str(e)}, 500)

    def do_POST(self):
        try:
            self._route_post()
        except Exception as e:
            logger.exception("Unhandled error")
            self._send_json({"error": str(e)}, 500)

    def _route_get(self):
        parsed = urlparse(self.path)
        path = parsed.path

        if path.startswith("/api/") and not self._check_request_allowed():
            return

        if path == "/api/status":
            self._handle_status()
        elif path == "/api/search":
            params = parse_qs(parsed.query)
            query = params.get("q", params.get("query", [""]))[0]
            self._handle_search(query)
        elif match := re.match(r"/api/book/([^/]+)/chapters$", path):
            self._handle_chapters_list(match.group(1))
        elif match := re.match(r"/api/book/([^/]+)$", path):
            self._handle_book_info(match.group(1))
        elif path == "/api/jobs":
            self._send_json({"jobs": self.queue.list_jobs()})
        elif match := re.match(r"/api/jobs/([0-9a-f]+)$", path):
            self._handle_job(match.group(1))
        elif path == "/api/progress":
            self._handle_progress()
        elif path == "/api/settings":
            self._handle_get_settings()
        elif path == "/api/formats":
            self._send_json(DownloaderPlugin.get_formats_info())
        else:
            super().do_GET()

    def _route_post(self):
        # Always consume the body first: replying before reading it makes the
        # client see a connection reset instead of the error response.
        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length).decode("utf-8")

        if not self._check_request_allowed():
            return

        try:
            data = json.loads(body) if body else {}
        except json.JSONDecodeError:
            self._send_json({"error": "Invalid JSON body"}, 400)
            return
        if not isinstance(data, dict):
            self._send_json({"error": "JSON body must be an object"}, 400)
            return

        path = urlparse(self.path).path
        if path == "/api/download":
            self._handle_download(data)
        elif path == "/api/cookies":
            self._handle_cookies(data)
        elif match := re.match(r"/api/jobs/([0-9a-f]+)/cancel$", path):
            self._handle_job_cancel(match.group(1))
        elif path == "/api/cancel":
            self._handle_cancel()
        elif path == "/api/reveal":
            self._handle_reveal(data)
        elif path == "/api/settings/output-dir":
            self._handle_set_output_dir(data)
        else:
            self._send_json({"error": "Not found"}, 404)

    # -- book information --------------------------------------------------

    def _handle_status(self):
        self._send_json(self.kernel["auth"].get_status())

    def _handle_search(self, query: str):
        if not query:
            self._send_json({"results": []})
            return

        try:
            results = self.kernel["book"].search(query)
        except Exception as e:
            self._send_json({"error": str(e), "results": []}, 502)
            return
        self._send_json({"results": results})

    def _handle_book_info(self, book_id: str):
        try:
            self._send_json(self.kernel["book"].fetch(book_id))
        except Exception as e:
            self._send_json({"error": str(e)}, 400)

    def _handle_chapters_list(self, book_id: str):
        """Return list of chapters for chapter selection UI."""
        try:
            chapters = self.kernel["chapters"].fetch_list(book_id)
        except Exception as e:
            self._send_json({"error": str(e)}, 400)
            return
        self._send_json(
            {
                "chapters": [
                    {
                        "index": i,
                        "title": ch.get("title", f"Chapter {i + 1}"),
                        "pages": ch.get("virtual_pages"),
                        "minutes": ch.get("minutes_required"),
                    }
                    for i, ch in enumerate(chapters)
                ],
                "total": len(chapters),
            }
        )

    # -- download queue ----------------------------------------------------

    def _handle_download(self, data: dict):
        """Validate a download request and add it to the queue."""
        book_id = str(data.get("book_id") or "").strip()
        if not book_id:
            self._send_json({"error": "book_id required"}, 400)
            return

        selected_chapters = data.get("chapters")
        if selected_chapters is not None and (
            not isinstance(selected_chapters, list)
            or not all(isinstance(i, int) and i >= 0 for i in selected_chapters)
        ):
            self._send_json({"error": "chapters must be a list of chapter indexes"}, 400)
            return

        chunk_config = None
        chunking_opts = data.get("chunking") or {}
        if chunking_opts:
            try:
                chunk_config = ChunkConfig(
                    chunk_size=int(chunking_opts.get("chunk_size", 4000)),
                    overlap=int(chunking_opts.get("overlap", 200)),
                    respect_boundaries=True,
                )
            except (TypeError, ValueError):
                self._send_json({"error": "chunk_size and overlap must be integers"}, 400)
                return

        output_plugin = self.kernel["output"]
        output_dir_str = data.get("output_dir")
        if output_dir_str:
            success, message, output_dir = output_plugin.validate_dir(output_dir_str)
            if not success:
                self._send_json({"error": message}, 400)
                return
        else:
            output_dir = output_plugin.get_default_dir()

        formats = DownloaderPlugin.parse_formats(data.get("format", "epub"))
        job = self.queue.submit(
            book_id=book_id,
            title=str(data.get("title") or ""),
            output_dir=output_dir,
            formats=formats,
            selected_chapters=selected_chapters,
            skip_images=bool(data.get("skip_images", False)),
            chunk_config=chunk_config,
            resume=bool(data.get("resume", True)),
        )
        self._send_json({"status": "queued", "job_id": job.id, "book_id": book_id, "formats": formats}, 202)

    def _handle_job(self, job_id: str):
        job = self.queue.get(job_id)
        if job is None:
            self._send_json({"error": "Job not found"}, 404)
        else:
            self._send_json(job)

    def _handle_job_cancel(self, job_id: str):
        if self.queue.cancel(job_id):
            self._send_json({"success": True, "message": "Cancel requested"})
        else:
            self._send_json({"success": False, "message": "Job is not active"}, 409)

    def _handle_progress(self):
        """Legacy single-download endpoint: the running/next/last job, flattened."""
        job = self.queue.active()
        if job is None:
            self._send_json({})
            return
        status = job["phase"] if job["status"] == "running" and job["phase"] else job["status"]
        self._send_json({**job, **job["files"], "status": status})

    def _handle_cancel(self):
        """Legacy endpoint: cancel the running job."""
        if self.queue.cancel_active():
            self._send_json({"success": True, "message": "Cancel requested"})
        else:
            self._send_json({"success": False, "message": "No active download"})

    # -- settings & local helpers -------------------------------------------

    def _handle_get_settings(self):
        self._send_json({"output_dir": str(config.OUTPUT_DIR)})

    def _handle_set_output_dir(self, data: dict):
        """Handle output directory selection - browse or direct path."""
        if data.get("browse"):
            # Open native folder picker dialog
            selected = self.kernel["system"].show_folder_picker(config.OUTPUT_DIR)
            if selected:
                self._send_json({"success": True, "path": str(selected)})
            else:
                self._send_json({"cancelled": True})
            return

        path_str = str(data.get("path") or "").strip()
        if not path_str:
            self._send_json({"error": "path required"}, 400)
            return

        success, message, path = self.kernel["output"].validate_dir(path_str)
        if not success:
            self._send_json({"error": message}, 400)
            return
        self._send_json({"success": True, "path": str(path)})

    def _handle_cookies(self, data: dict):
        """Save cookies from user input."""
        if not data:
            self._send_json({"error": "Invalid cookie data"}, 400)
            return

        try:
            config.COOKIES_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")
            # Session cookies are credentials: keep them private (no-op on Windows).
            os.chmod(config.COOKIES_FILE, 0o600)
            self.kernel.http.reload_cookies()
            self._send_json({"success": True})
        except Exception as e:
            self._send_json({"error": str(e)}, 500)

    def _handle_reveal(self, data: dict):
        """Open file manager and select the specified file."""
        path_str = str(data.get("path") or "")
        if not path_str:
            self._send_json({"error": "path required"}, 400)
            return

        path = Path(path_str).resolve()
        if not path.exists():
            self._send_json({"error": "Path does not exist"}, 404)
            return

        if self.kernel["system"].reveal_in_file_manager(path):
            self._send_json({"success": True})
        else:
            self._send_json({"error": "Failed to reveal file"}, 500)

    def _send_json(self, data: dict, status: int = 200):
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        logger.debug("%s - %s", self.address_string(), format % args)


def create_server(host: str = "localhost", port: int = 8000, kernel: Kernel | None = None) -> ThreadingHTTPServer:
    """Create and configure the HTTP server.

    Uses a threading server so a single slow or stalled client (or a long
    download) cannot block status/progress/cookie requests.
    """
    kernel = kernel or create_default_kernel()
    DownloaderHandler.kernel = kernel
    DownloaderHandler.queue = DownloadQueue(kernel["downloader"])

    return ThreadingHTTPServer((host, port), DownloaderHandler)


def run_server(host: str = "localhost", port: int = 8000):
    """Start the HTTP server."""
    server = create_server(host, port)
    logger.info("Server running at http://%s:%s", host, port)
    server.serve_forever()
