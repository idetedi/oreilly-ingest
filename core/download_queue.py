"""Sequential download queue shared by the web server (and any other client)."""

import logging
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .errors import DownloadCancelled

logger = logging.getLogger(__name__)

QUEUED = "queued"
RUNNING = "running"
COMPLETED = "completed"
ERROR = "error"
CANCELLED = "cancelled"
FINISHED = frozenset({COMPLETED, ERROR, CANCELLED})


@dataclass
class DownloadJob:
    """One requested book download and its live state."""

    book_id: str
    output_dir: Path
    formats: list[str]
    title: str = ""
    selected_chapters: list[int] | None = None
    skip_images: bool = False
    chunk_config: Any = None
    resume: bool = True

    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    status: str = QUEUED
    progress: dict = field(default_factory=dict)
    files: dict = field(default_factory=dict)
    error: str | None = None
    created_at: float = field(default_factory=time.time)
    finished_at: float | None = None
    cancel_requested: bool = False

    def to_dict(self, position: int | None = None) -> dict:
        return {
            "id": self.id,
            "book_id": self.book_id,
            "title": self.title,
            "formats": self.formats,
            "status": self.status,
            "position": position,
            "percentage": 100 if self.status == COMPLETED else self.progress.get("percentage", 0),
            "phase": self.progress.get("status"),
            "message": self.progress.get("message", ""),
            "eta_seconds": self.progress.get("eta_seconds"),
            "current_chapter": self.progress.get("current_chapter", 0),
            "total_chapters": self.progress.get("total_chapters", 0),
            "chapter_title": self.progress.get("chapter_title", ""),
            "files": self.files,
            "error": self.error,
            "created_at": self.created_at,
            "finished_at": self.finished_at,
        }


class DownloadQueue:
    """Runs download jobs one at a time on a background worker thread.

    Jobs run sequentially on purpose: every job shares the same HTTP client
    and rate limits, so parallel books would not finish sooner and would
    look more bot-like to O'Reilly's CDN.
    """

    def __init__(self, downloader, max_history: int = 50):
        self._downloader = downloader
        self._max_history = max_history
        self._jobs: dict[str, DownloadJob] = {}  # insertion order == submit order
        self._cond = threading.Condition()
        self._worker: threading.Thread | None = None

    # -- public API -------------------------------------------------------

    def submit(self, **params) -> DownloadJob:
        job = DownloadJob(**params)
        with self._cond:
            self._jobs[job.id] = job
            self._prune_history()
            self._ensure_worker()
            self._cond.notify()
        logger.info("Queued %s (%s) as job %s", job.book_id, ",".join(job.formats), job.id)
        return job

    def get(self, job_id: str) -> dict | None:
        with self._cond:
            job = self._jobs.get(job_id)
            return job.to_dict(self._position(job)) if job else None

    def list_jobs(self) -> list[dict]:
        with self._cond:
            return [job.to_dict(self._position(job)) for job in self._jobs.values()]

    def active(self) -> dict | None:
        """The running job, else the next queued one, else the latest finished."""
        with self._cond:
            jobs = list(self._jobs.values())
            for wanted in (RUNNING, QUEUED):
                for job in jobs:
                    if job.status == wanted:
                        return job.to_dict(self._position(job))
            return jobs[-1].to_dict() if jobs else None

    def cancel(self, job_id: str) -> bool:
        """Cancel a queued job immediately, or ask a running one to stop."""
        with self._cond:
            job = self._jobs.get(job_id)
            if not job or job.status in FINISHED:
                return False
            if job.status == QUEUED:
                job.status = CANCELLED
                job.error = "Removed from queue"
                job.finished_at = time.time()
            else:
                job.cancel_requested = True
            return True

    def cancel_active(self) -> bool:
        with self._cond:
            running = next((j for j in self._jobs.values() if j.status == RUNNING), None)
        return self.cancel(running.id) if running else False

    # -- worker -----------------------------------------------------------

    def _ensure_worker(self):
        if self._worker is None or not self._worker.is_alive():
            self._worker = threading.Thread(target=self._run, name="download-queue", daemon=True)
            self._worker.start()

    def _next_job(self) -> DownloadJob:
        with self._cond:
            while True:
                job = next((j for j in self._jobs.values() if j.status == QUEUED), None)
                if job:
                    job.status = RUNNING
                    return job
                self._cond.wait()

    def _run(self):
        while True:
            job = self._next_job()
            self._execute(job)

    def _execute(self, job: DownloadJob):
        def on_progress(progress):
            with self._cond:
                job.progress = asdict(progress)

        try:
            result = self._downloader.download(
                book_id=job.book_id,
                output_dir=job.output_dir,
                formats=job.formats,
                selected_chapters=job.selected_chapters,
                skip_images=job.skip_images,
                chunk_config=job.chunk_config,
                progress_callback=on_progress,
                cancel_check=lambda: job.cancel_requested,
                resume=job.resume,
            )
            status, error, files = COMPLETED, None, dict(result.files)
            title = result.title
        except DownloadCancelled as e:
            status, error, files, title = CANCELLED, str(e), {}, None
        except Exception as e:
            logger.exception("Download of %s failed", job.book_id)
            status, error, files, title = ERROR, str(e), {}, None

        with self._cond:
            job.status = status
            job.error = error
            job.files = files
            job.title = title or job.title
            job.finished_at = time.time()

    # -- helpers (call with the lock held) --------------------------------

    def _position(self, job: DownloadJob) -> int | None:
        """1-based position among queued jobs, or None when not queued."""
        if job.status != QUEUED:
            return None
        queued = [j for j in self._jobs.values() if j.status == QUEUED]
        return queued.index(job) + 1

    def _prune_history(self):
        finished = [j for j in self._jobs.values() if j.status in FINISHED]
        for job in finished[: max(0, len(finished) - self._max_history)]:
            del self._jobs[job.id]
