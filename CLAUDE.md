# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

O'Reilly book ingest tool using a microkernel architecture. Converts books to LLM-ready formats (Markdown, Text, JSON) for AI interaction. Also supports PDF and EPUB. Python 3.10+, vanilla Python with minimal dependencies.

## Commands

```bash
# Setup
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# Run the web server
python main.py                    # Default: localhost:8000
python main.py --port 9000        # Custom port

# Run with venv directly
.venv/bin/python main.py

# Command line (no server)
python -m cli download <book_id> -f md,epub --chapters 1-3
python -m cli search "query" | formats | status

# Lint and tests (offline, fake O'Reilly API in tests/fakes.py)
pip install -r requirements-dev.txt
ruff check .
pytest
```

## Architecture

### Microkernel Pattern
- **Kernel** (`core/kernel.py`): Plugin registry, provides shared `HttpClient` to all plugins
- **Plugins** (`plugins/`): Independent modules registered with the kernel, access HTTP via `self.http`
- Plugins extend `Plugin` base class from `plugins/base.py`
- Use `create_default_kernel()` to get a fully configured kernel with all plugins

### Plugin Categories

**Core Plugins** (data acquisition):
- `AuthPlugin` - Session validation via /profile/
- `BookPlugin` - Fetch metadata (V2 Search + V2 Epubs APIs)
- `ChaptersPlugin` - Fetch chapter list and content
- `AssetsPlugin` - Download CSS and images
- `HtmlProcessorPlugin` - Extract sbo-rt-content, rewrite links, wrap XHTML

**Output Format Plugins**:
- `EpubPlugin` - Generate EPUB structure and ZIP
- `MarkdownPlugin` - Convert HTML to Markdown
- `PdfPlugin` - Generate PDF output
- `PlainTextPlugin` - Extract plain text
- `JsonExportPlugin` - Export structured JSON
- `ChunkingPlugin` - Split content into chunks
- `TokenPlugin` - Token counting

**Orchestration Plugins**:
- `OutputPlugin` - Coordinate output generation
- `SystemPlugin` - System information
- `DownloaderPlugin` - Orchestrate full download workflow. `download()` runs phases over a
  `DownloadContext`; output generators are listed in `_GENERATORS` (add a format there).
  Chapters are fetched in parallel and processed in order; `.download_state.json` in the
  book folder enables resume. Cancellation raises `core.errors.DownloadCancelled` and never
  deletes files.

**Queue**: `core/download_queue.py` (`DownloadQueue`) runs jobs sequentially on a worker
thread; the web server enqueues through it.

**HTTP**: `core/http_client.py` is thread-safe (one curl_cffi session per thread, shared
rate limiter with "api"/"asset" lanes, retries on network errors and 429/5xx).

### Web Server
- `web/server.py`: HTTP server using `http.server`, serves static files from `web/static/`
- JSON API endpoints: `/api/status`, `/api/search`, `/api/book/{id}`, `/api/download` (enqueue),
  `/api/jobs`, `/api/jobs/{id}`, `/api/jobs/{id}/cancel`, legacy `/api/progress` and `/api/cancel`
- Every `/api/*` request must have a local `Host` (see `config.ALLOWED_HOSTS`) and POSTs with an
  `Origin` must be same-origin. No CORS headers are sent. Escape remote data in `app.js`
  with `escapeHtml()`.

### CLI
- `cli/main.py` (`python -m cli`): download/search/formats/status on top of the same kernel

### Key Files
- `config.py`: BASE_URL, API_V1, API_V2, OUTPUT_DIR, COOKIES_FILE, rate limits/retries/workers
  (overridable via environment variables), ALLOWED_HOSTS
- `cookies.json`: User-provided O'Reilly session cookies (BrowserCookie, OptanonConsent)
- `output/`: Downloaded books stored here as `{book_title}/` directories

## API Strategy

Use V2 APIs primarily:
- **V2 Search** (`/api/v2/search/?query={ISBN}`) - authors, publishers, topics, cover_url
- **V2 Epubs** (`/api/v2/epubs/urn:orm:book:{ID}/`) - spine, chapters URL, TOC URL
- **V2 Chapters** (`/api/v2/epub-chapters/`) - full chapter metadata with `related_assets`
- **V2 Content** returns `sbo-rt-content` div directly (no full HTML parsing needed)

## Content Pipeline

1. Fetch book metadata (BookPlugin)
2. Fetch chapters list with pagination (ChaptersPlugin)
3. For each chapter: fetch HTML, extract `sbo-rt-content`, collect CSS/images
4. Rewrite links (.html → .xhtml, image paths → Images/)
5. Download all CSS and images (AssetsPlugin)
6. Generate output format: EPUB, PDF, Markdown, plain text, or JSON

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

## Reference Documentation

- `TECHNICAL_DOCUMENTATION.md` - Complete V1 API specification, EPUB generation details
- `API_V2_DOCUMENTATION.md` - V2 API endpoints with differences from V1
