import os
from pathlib import Path


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ[name])
    except (KeyError, ValueError):
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ[name])
    except (KeyError, ValueError):
        return default


BASE_DIR = Path(__file__).parent
OUTPUT_DIR = Path(os.environ["OUTPUT_DIR"]) if os.environ.get("OUTPUT_DIR") else BASE_DIR / "output"

# Use data/ directory if it exists (Docker), otherwise use root (local dev)
DATA_DIR = BASE_DIR / "data"
if DATA_DIR.exists():
    COOKIES_FILE = DATA_DIR / "cookies.json"
else:
    COOKIES_FILE = BASE_DIR / "cookies.json"

BASE_URL = "https://learning.oreilly.com"
API_V1 = f"{BASE_URL}/api/v1"
API_V2 = f"{BASE_URL}/api/v2"

# Minimum seconds between the start of two requests, shared by all threads.
# API/chapter requests use a fixed REQUEST_DELAY. Images and CSS start at
# ASSET_REQUEST_DELAY and adapt: faster (down to ASSET_MIN_DELAY, ~10/s) while
# the server answers fine, twice as slow (up to ASSET_MAX_DELAY) on any sign of
# throttling. Keep these conservative: Akamai blocks bursty clients.
REQUEST_DELAY = _env_float("REQUEST_DELAY", 0.5)
ASSET_REQUEST_DELAY = _env_float("ASSET_REQUEST_DELAY", 0.25)
ASSET_MIN_DELAY = _env_float("ASSET_MIN_DELAY", 0.1)
ASSET_MAX_DELAY = _env_float("ASSET_MAX_DELAY", 2.0)
ASSET_SPEEDUP_EVERY = max(1, _env_int("ASSET_SPEEDUP_EVERY", 20))
REQUEST_TIMEOUT = _env_float("REQUEST_TIMEOUT", 30)

# Parallel workers for chapter and asset downloads (rate limits still apply).
DOWNLOAD_WORKERS = max(1, _env_int("DOWNLOAD_WORKERS", 6))

# Retry transient failures (timeouts, connection resets, 429/5xx responses)
# so a single bad response doesn't abort a whole download.
MAX_RETRIES = max(1, _env_int("MAX_RETRIES", 4))
RETRY_BACKOFF = _env_float("RETRY_BACKOFF", 1.5)
RETRY_STATUS_CODES = frozenset({429, 500, 502, 503, 504})

# Host names the local web server answers to. Anything else is rejected to
# block DNS-rebinding attacks. Extend with ALLOWED_HOSTS="myhost,other".
ALLOWED_HOSTS = {"localhost", "127.0.0.1", "::1"} | {
    h.strip().lower() for h in os.environ.get("ALLOWED_HOSTS", "").split(",") if h.strip()
}

HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Accept-Encoding": "gzip, deflate",
    "Accept-Language": "en-US,en;q=0.5",
    "Referer": BASE_URL,
    # User-Agent is intentionally omitted — curl_cffi sets it to match the
    # browser impersonation (safari17_0), and overriding it would cause a
    # TLS-fingerprint/UA mismatch that Akamai detects as a bot.
}
