# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

O'Reilly book ingest tool using a microkernel architecture. Converts books to LLM-ready formats (Markdown, plain text, JSON/JSONL, TOON, RAG chunks) and to PDF and EPUB. Web UI, CLI and a small JSON API. Python 3.10+, vanilla Python with minimal dependencies.

This repository is a fork (`idetedi/oreilly-ingest`) of `Mosaibah/oreilly-ingest`.

## Workflow Conventions

- `main` is protected: every change goes on a new branch and is merged through a PR **against the fork** (`idetedi/oreilly-ingest`), never against the upstream project.
- Commits follow [Conventional Commits](https://www.conventionalcommits.org/es/v1.0.0/) (`fix(scope): ...`, `feat: ...`), written in English.
- Do not add AI attribution to commits or PRs (no `Co-Authored-By: Claude`, no "Generated with Claude Code").
- Run `ruff check .` and `pytest` before committing; CI (`.github/workflows/ci.yml`) runs both on Python 3.10 and 3.12.

## Commands

```bash
# Setup
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt    # add requirements-dev.txt for tests/lint

# Web server
python main.py                     # http://localhost:8000
python main.py --port 9000 -v      # custom port, debug logging

# Docker (rebuilds the image on every `up`: pull_policy: build)
docker compose up -d

# Command line (no server)
python -m cli download <book_id> -f md,epub --chapters 1-3
python -m cli search "query" | formats | status

# Lint and tests (offline: fake O'Reilly API in tests/fakes.py)
ruff check .
pytest
```

## Architecture

### Microkernel Pattern
- **Kernel** (`core/kernel.py`): plugin registry; provides the shared `HttpClient` to all plugins
- **Plugins** (`plugins/`): extend `Plugin` from `plugins/base.py`; `kernel.register()` sets `plugin.kernel`, so plugins use `self.http` and `self.kernel["other_plugin"]`
- `create_default_kernel()` returns a kernel with every plugin registered

### Plugins

**Data acquisition**:
- `AuthPlugin` - session status (local JWT check, falls back to /profile/)
- `BookPlugin` - metadata (V2 Search + V2 Epubs) and catalogue search
- `ChaptersPlugin` - chapter list (paginated), TOC, TOC-based reordering, chapter content
- `AssetsPlugin` - CSS and images, downloaded concurrently; failures are skipped and logged
- `HtmlProcessorPlugin` - extract `sbo-rt-content`, rewrite links and image paths, wrap/unwrap XHTML

**Output formats**:
- `EpubPlugin` - EPUB 3 structure and ZIP
- `MarkdownPlugin` - Markdown per chapter; copies referenced images into `Markdown/Images/`
- `PdfPlugin` - PDF via WeasyPrint (lazy import; needs system libraries)
- `PlainTextPlugin` - plain text (single file or per chapter)
- `JsonExportPlugin` - structured JSON (+ optional JSONL)
- `ToonExportPlugin` - TOON (token-efficient encoding of the JSON export)
- `ChunkingPlugin` - chunked JSONL for RAG (word-count token heuristic)
- `TokenPlugin` - tiktoken-based token counting

**Orchestration & system**:
- `OutputPlugin` - output folders: validation, book folder naming (slug + `.book_id`), same-title conflicts
- `SystemPlugin` - native folder picker and "reveal in file manager" (macOS/Windows/Linux)
- `DownloaderPlugin` - full download workflow. `download()` runs phases over a `DownloadContext`
  (`_fetch_metadata`, `_prepare_book_dir`, `_process_chapters`, `_download_assets`, `_generate_outputs`).
  Chapters are fetched in parallel and processed in order. `.download_state.json` in the book folder
  enables resume. Cancellation raises `core.errors.DownloadCancelled` and never deletes files.

### Core Modules
- `core/http_client.py` - thread-safe: one curl_cffi session per thread (Safari impersonation, replayed
  browser cookies including Akamai `_abck`/`bm_*`), a shared rate limiter with a fixed `"api"` lane and an
  adaptive (AIMD) `"asset"` lane, retries on network errors and 429/5xx (honours `Retry-After`)
- `core/download_queue.py` - `DownloadQueue`: runs jobs sequentially on a worker thread; used by the web server
- `core/errors.py` - `DownloadCancelled`
- `core/types.py` - `TypedDict` contracts (`BookInfo`, `ChapterInfo`, ...)
- `utils/files.py` - `sanitize_filename`, `slugify`, `image_filename` (shared by the link rewriter and the downloader)

### Web Server
- `web/server.py`: `ThreadingHTTPServer`; serves `web/static/` and the JSON API
- Endpoints: `/api/status`, `/api/search`, `/api/book/{id}`, `/api/book/{id}/chapters`, `/api/formats`,
  `/api/settings`, `/api/settings/output-dir`, `/api/cookies`, `/api/reveal`, `/api/download` (enqueue, returns `job_id`),
  `/api/jobs`, `/api/jobs/{id}`, `/api/jobs/{id}/cancel`, legacy `/api/progress` and `/api/cancel`
- Every `/api/*` request must have a local `Host` (`config.ALLOWED_HOSTS`) and POSTs with an `Origin`
  must be same-origin. No CORS headers are sent. Escape remote data in `app.js` with `escapeHtml()`
  (and `sanitizeHtml()` for rich text).

### CLI
- `cli/main.py` (`python -m cli`): `download`, `search`, `formats`, `status` on top of the same kernel.
  First Ctrl+C cancels cleanly (resumable), second aborts.

### Key Files
- `config.py` - BASE_URL, API_V1, API_V2, OUTPUT_DIR, COOKIES_FILE, rate limits, retries, workers
  (all overridable via environment variables), ALLOWED_HOSTS
- `cookies.json` - O'Reilly session cookies (`orm-jwt` plus the Akamai cookies). Stored in `data/`
  when that folder exists (Docker volume), otherwise in the project root
- `output/<slugified-title>/` - one folder per book: `.book_id`, `.download_state.json`, `OEBPS/`
  (build tree, removed after an EPUB is built), `Markdown/`, and the generated files
- `scripts/refresh_cookies.py` - sync cookies from Chrome; `scripts/patch_chunk_titles.py` - backfill chunk titles
- `tests/fakes.py` - `FakeHttp` (in-memory O'Reilly API) and `make_kernel()` for offline tests

## API Strategy

Use V2 APIs primarily:
- **V2 Search** (`/api/v2/search/?query={ISBN}`) - authors, publishers, topics, cover_url
- **V2 Epubs** (`/api/v2/epubs/urn:orm:book:{ID}/`) - spine, chapters URL, TOC URL
- **V2 Chapters** (`/api/v2/epub-chapters/`) - full chapter metadata with `related_assets`
  (absolute asset URLs; chapter HTML references the same files root-relative)
- **V2 Content** returns the `sbo-rt-content` div directly (no full HTML parsing needed)

## Content Pipeline

1. Fetch book metadata (BookPlugin), chapter list and TOC (ChaptersPlugin); reorder by TOC; apply chapter selection
2. Create the book folder; download the cover (EPUB/PDF only)
3. Fetch chapter HTML in parallel, process in order: extract `sbo-rt-content`, rewrite links
   (.html → .xhtml, images → `Images/`), write XHTML, record progress in `.download_state.json`
   (chapters already on disk are reused)
4. Download assets only for formats that use them: images for EPUB/PDF/Markdown, CSS for EPUB/PDF
   (`DownloaderPlugin._IMAGE_FORMATS` / `_STYLE_FORMATS`)
5. Run the requested generators (`DownloaderPlugin._GENERATORS`); the EPUB build tree is cleaned up last

## Adding a New Output Format

1. Create the plugin (below) and register it in `create_default_kernel()`
2. In `DownloaderPlugin`: add the name to `SUPPORTED_FORMATS` and `get_format_help()`, add a
   `_generate_<name>(ctx, result)` method and an entry in `_GENERATORS`; add it to `_IMAGE_FORMATS` /
   `_STYLE_FORMATS` if it needs images or CSS
3. Add the format option to `web/static/app.js` and a test in `tests/test_downloader.py`

## Adding a New Plugin

```python
from plugins.base import Plugin

class NewPlugin(Plugin):
    def do_something(self, params):
        # Access HTTP client via self.http
        response = self.http.get(url)
        # Access other plugins via self.kernel["plugin_name"]
        return result
```

Register in `core/kernel.py`:
```python
kernel.register("new_plugin", NewPlugin())
```

## Gotchas

- Always pass `encoding="utf-8"` to `read_text`/`write_text`/`open` (Windows defaults to cp1252).
- `curl_cffi` sessions are not thread-safe: go through `HttpClient`, never share `session` across threads.
- Don't set a `User-Agent` header: it must match the Safari TLS impersonation or Akamai blocks requests.
