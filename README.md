# O'Reilly Ingest

We're in the AI era. You want to chat with your favorite technical books using Claude Code, Cursor, or any LLM tool. This gets you there.

Export any O'Reilly book to Markdown, PDF, EPUB, JSON, TOON, or plain text. Download by chapters so you don't burn through your context window.

> Requires a valid O'Reilly Learning subscription.

## Disclaimer

For personal and educational use only. Please read the [O'Reilly Terms of Service](https://www.oreilly.com/terms/).

## Credits

Inspired by [safaribooks](https://github.com/lorenzodifuccia/safaribooks) by [@lorenzodifuccia](https://github.com/lorenzodifuccia).


## Features

- **Export by chapters** - save tokens, focus on what matters
- **LLM-ready formats** - Markdown, JSON, TOON, plain text optimized for AI
- **Traditional formats** - PDF and EPUB 3
- **O'Reilly V2 API** - fast and reliable
- **Images & styles included** - complete book experience
- **Web UI** - search, preview, download, with a download queue
- **CLI** - script downloads without the browser
- **Fast & resumable** - parallel chapter/asset downloads; interrupted downloads pick up where they stopped

<img src="docs/main.png" alt="Main Page">

## Quick Start

### Docker

```bash
git clone https://github.com/mosaibah/oreilly-downloader.git
cd oreilly-downloader
docker compose up -d
```

### Python

```bash
git clone https://github.com/mosaibah/oreilly-downloader.git
cd oreilly-downloader
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python main.py
```

Then open http://localhost:8000 (add `-v` for debug logging).

### Command line

```bash
python -m cli status                                   # check the saved session
python -m cli search "designing data-intensive"
python -m cli download 9781098119058 -f md,epub        # several ids run in sequence
python -m cli download 9781098119058 -f md --chapters 1,3,5-9 --skip-images
python -m cli formats                                  # list formats and aliases
```

Press Ctrl+C once to cancel cleanly; running the same command again resumes
(use `--no-resume` to refetch everything).

## Setting Up Cookies

Click "Set Cookies" in the web interface and follow the steps:

<img src="docs/cookie-modal.png" alt="Cookie Setup" style="max-width:320px; height:auto;">

O'Reilly's session token is short-lived. On macOS/Linux with Chrome,
`python scripts/refresh_cookies.py --watch` keeps the cookies in sync
(needs `pip install pycookiecheat`).

## Configuration

Environment variables (all optional):

| Variable | Default | Purpose |
|----------|---------|---------|
| `OUTPUT_DIR` | `./output` | Where books are written |
| `DOWNLOAD_WORKERS` | `4` | Parallel chapter/asset downloads |
| `REQUEST_DELAY` | `0.5` | Min. seconds between API/chapter requests (all threads) |
| `ASSET_REQUEST_DELAY` | `0.25` | Min. seconds between image/CSS requests (all threads) |
| `MAX_RETRIES` / `RETRY_BACKOFF` | `4` / `1.5` | Retries for network errors and 429/5xx responses |
| `REQUEST_TIMEOUT` | `30` | Per-request timeout in seconds |
| `ALLOWED_HOSTS` | – | Extra host names the web server accepts (comma-separated) |

Keep the delays conservative: O'Reilly's CDN (Akamai) blocks bursty clients.

## Security

The web server is meant for your own machine. It only answers requests whose
`Host` is `localhost`/`127.0.0.1` (plus `ALLOWED_HOSTS`) and rejects
cross-origin POSTs, so other websites cannot drive it. Docker publishes the
port on `127.0.0.1` only. `cookies.json` holds your session: keep it private.

## Architecture

Plugin-based microkernel design:

| Layer | Components |
|-------|------------|
| **Kernel** | Plugin registry, shared HTTP client |
| **Core** | Auth, Book, Chapters, Assets, HtmlProcessor |
| **Output** | Epub, Markdown, Pdf, PlainText, JsonExport, ToonExport |
| **Utility** | Chunking, Token, Downloader |

### API

```
GET  /api/status                 - auth check
GET  /api/search?q=              - find books
GET  /api/book/{id}              - metadata
GET  /api/book/{id}/chapters     - chapter list
GET  /api/formats                - formats, aliases, descriptions
POST /api/download               - queue a download -> 202 {"job_id": ...}
GET  /api/jobs                   - all jobs (queued, running, finished)
GET  /api/jobs/{id}              - one job's status, progress and files
POST /api/jobs/{id}/cancel       - remove a queued job / stop the running one
GET  /api/progress               - (legacy) the active job
POST /api/cancel                 - (legacy) cancel the active job
```

`POST /api/download` body: `{"book_id", "format": "md,epub", "chapters": [0, 2],
"output_dir", "skip_images", "chunking": {"chunk_size", "overlap"}, "title", "resume"}`.

## Development

```bash
pip install -r requirements-dev.txt
ruff check .
pytest            # offline: uses a fake O'Reilly API (tests/fakes.py)
```

## Contributing

Found a bug or have an idea? PRs and issues are always welcome!


## Recent Changes

- **Chunking: streaming & memory fix** — `chunk_book()` now streams chunks directly to disk instead of accumulating in memory. Replaced `tiktoken` tokenizer with a word-count heuristic to avoid memory spikes on large books. (@zirkleta)
- **System: command injection fix** — `_show_macos_picker()` rejects paths containing `"` before interpolating into osascript, preventing command injection via crafted directory names. (@zirkleta)
- **`scripts/patch_chunk_titles.py`** — New utility script that backfills `book_title` into existing `*_chunks.jsonl` files in the output directory. (@zirkleta)

## License

MIT

## Star History

<picture>
  <source
    media="(prefers-color-scheme: dark)"
    srcset="
      https://api.star-history.com/svg?repos=Mosaibah/oreilly-ingest&type=Date&theme=dark
    "
  />
  <source
    media="(prefers-color-scheme: light)"
    srcset="
      https://api.star-history.com/svg?repos=Mosaibah/oreilly-ingest&type=Date
    "
  />
  <img
    alt="Star History Chart"
    src="https://api.star-history.com/svg?repos=Mosaibah/oreilly-ingest&type=Date"
  />
</picture>
