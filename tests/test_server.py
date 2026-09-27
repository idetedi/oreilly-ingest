import http.client
import json
import threading
import time

import pytest

from tests.fakes import BOOK_ID, make_kernel
from web.server import create_server


@pytest.fixture
def server(tmp_path, monkeypatch):
    import config

    monkeypatch.setattr(config, "OUTPUT_DIR", tmp_path)
    srv = create_server("127.0.0.1", 0, kernel=make_kernel())
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield srv
    srv.shutdown()


def request(server, method, path, body=None, headers=None):
    port = server.server_address[1]
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    headers = {"Host": f"127.0.0.1:{port}", **(headers or {})}
    payload = json.dumps(body) if body is not None else None
    if payload is not None:
        headers.setdefault("Content-Type", "application/json")
    conn.request(method, path, body=payload, headers=headers)
    resp = conn.getresponse()
    raw = resp.read()
    try:
        data = json.loads(raw)
    except ValueError:
        data = raw
    return resp.status, data, dict(resp.getheaders())


def test_rejects_foreign_host(server):
    status, data, _ = request(server, "GET", "/api/settings", headers={"Host": "evil.example:8000"})
    assert status == 403


def test_rejects_cross_origin_post(server):
    status, _, _ = request(server, "POST", "/api/cookies", {"a": "b"}, headers={"Origin": "http://evil.example"})
    assert status == 403


def test_same_origin_and_originless_posts_are_allowed(server):
    port = server.server_address[1]
    status, _, _ = request(server, "POST", "/api/cancel", {}, headers={"Origin": f"http://127.0.0.1:{port}"})
    assert status == 200
    status, _, _ = request(server, "POST", "/api/cancel", {})
    assert status == 200


def test_no_wildcard_cors(server):
    _, _, headers = request(server, "GET", "/api/settings")
    assert "Access-Control-Allow-Origin" not in headers


def test_invalid_json_is_a_400(server):
    port = server.server_address[1]
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    conn.request("POST", "/api/download", body="{not json", headers={"Content-Type": "application/json"})
    assert conn.getresponse().status == 400


def test_download_is_queued_and_completes(server):
    status, data, _ = request(server, "POST", "/api/download", {"book_id": BOOK_ID, "format": "md,json"})
    assert status == 202
    job_id = data["job_id"]

    deadline = time.time() + 10
    while time.time() < deadline:
        _, job, _ = request(server, "GET", f"/api/jobs/{job_id}")
        if job["status"] not in ("queued", "running"):
            break
        time.sleep(0.05)

    assert job["status"] == "completed", job
    assert set(job["files"]) == {"markdown", "json"}
    _, jobs, _ = request(server, "GET", "/api/jobs")
    assert [j["id"] for j in jobs["jobs"]] == [job_id]
    _, legacy, _ = request(server, "GET", "/api/progress")
    assert legacy["status"] == "completed" and "markdown" in legacy


def test_download_validates_chapters(server):
    status, data, _ = request(server, "POST", "/api/download", {"book_id": BOOK_ID, "chapters": ["x"]})
    assert status == 400


def test_unknown_job_is_404(server):
    status, _, _ = request(server, "GET", "/api/jobs/abc123")
    assert status == 404
