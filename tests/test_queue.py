import threading
import time
from pathlib import Path

from core.download_queue import DownloadQueue
from core.errors import DownloadCancelled
from plugins.downloader import DownloadProgress, DownloadResult


class FakeDownloader:
    """Blocks each download until released, so tests control timing."""

    def __init__(self):
        self.started: list[str] = []
        self.release = {}

    def download(self, book_id, output_dir, progress_callback, cancel_check, **kwargs):
        self.started.append(book_id)
        gate = self.release.setdefault(book_id, threading.Event())
        progress_callback(DownloadProgress(status="processing_chapters", percentage=42, book_id=book_id))
        while not gate.wait(0.01):
            if cancel_check():
                raise DownloadCancelled("Download cancelled by user")
        if book_id == "boom":
            raise RuntimeError("HTTP 500")
        return DownloadResult(book_id=book_id, title=f"Title {book_id}", output_dir=output_dir, files={"md": "x"})


def wait_for(predicate, timeout=2.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    raise AssertionError("condition not met in time")


def submit(queue, book_id):
    return queue.submit(book_id=book_id, output_dir=Path("."), formats=["markdown"])


def test_jobs_run_sequentially_in_submit_order():
    downloader = FakeDownloader()
    queue = DownloadQueue(downloader)
    a, b = submit(queue, "a"), submit(queue, "b")

    wait_for(lambda: queue.get(a.id)["status"] == "running")
    assert queue.get(b.id)["status"] == "queued"
    assert queue.get(b.id)["position"] == 1
    # The job turns "running" before the downloader reports progress, so
    # wait for the report instead of asserting on it straight away.
    wait_for(lambda: queue.get(a.id)["percentage"] == 42)

    downloader.release.setdefault("a", threading.Event()).set()
    wait_for(lambda: queue.get(a.id)["status"] == "completed")
    assert queue.get(a.id)["title"] == "Title a"
    assert queue.get(a.id)["files"] == {"md": "x"}

    wait_for(lambda: queue.get(b.id)["status"] == "running")
    downloader.release.setdefault("b", threading.Event()).set()
    wait_for(lambda: queue.get(b.id)["status"] == "completed")
    assert downloader.started == ["a", "b"]


def test_cancel_queued_job_never_runs():
    downloader = FakeDownloader()
    queue = DownloadQueue(downloader)
    a, b = submit(queue, "a"), submit(queue, "b")
    wait_for(lambda: queue.get(a.id)["status"] == "running")

    assert queue.cancel(b.id)
    assert queue.get(b.id)["status"] == "cancelled"

    downloader.release.setdefault("a", threading.Event()).set()
    wait_for(lambda: queue.get(a.id)["status"] == "completed")
    time.sleep(0.05)
    assert downloader.started == ["a"]


def test_cancel_running_job():
    queue = DownloadQueue(FakeDownloader())
    a = submit(queue, "a")
    wait_for(lambda: queue.get(a.id)["status"] == "running")

    assert queue.cancel_active()
    wait_for(lambda: queue.get(a.id)["status"] == "cancelled")
    assert not queue.cancel(a.id), "finished jobs cannot be cancelled again"


def test_failed_job_reports_error_and_queue_continues():
    downloader = FakeDownloader()
    queue = DownloadQueue(downloader)
    boom, ok = submit(queue, "boom"), submit(queue, "ok")
    downloader.release.setdefault("boom", threading.Event()).set()
    downloader.release.setdefault("ok", threading.Event()).set()

    wait_for(lambda: queue.get(ok.id)["status"] == "completed")
    assert queue.get(boom.id)["status"] == "error"
    assert queue.get(boom.id)["error"] == "HTTP 500"


def test_history_is_bounded():
    downloader = FakeDownloader()
    queue = DownloadQueue(downloader, max_history=2)
    for name in ("a", "b", "c"):
        downloader.release.setdefault(name, threading.Event()).set()
        job = submit(queue, name)
        wait_for(lambda job=job: queue.get(job.id)["status"] == "completed")
    submit(queue, "d")
    assert [j["book_id"] for j in queue.list_jobs()][-3:] == ["b", "c", "d"]
    assert len(queue.list_jobs()) == 3
